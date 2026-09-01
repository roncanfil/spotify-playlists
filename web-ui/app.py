"""
Web UI for the playlist downloader.

Wraps download.process_playlist so the browser and the standalone script run
identical logic. Playlists come either from an uploaded CSV or straight from
Spotify; both paths end up as one folder under MUSIC_DIR holding that
playlist's CSV, its tracks and its .m3u, plus a job on one serial queue.
"""

import functools
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque

from flask import Flask, Response, jsonify, redirect, render_template, request

# download.py in this folder is the engine: search, download, transcode, tag.
# It also still runs standalone as a deprecated CLI; see ../cli/README.md.
import download as core
import spotify as sp

app = Flask(__name__)

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")

# Every playlist is one folder under MUSIC_DIR holding its own CSV, its tracks
# and its .m3u, so there is no separate playlist directory to configure.
#
# /data is fixed rather than configurable: the image sets
# PYTHONPATH=/data/ytdlp so yt-dlp upgrades outrank the bundled copy, and a
# STATE_DIR that pointed elsewhere would silently disable those upgrades.
STATE_DIR = "/data"

APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
PORT = os.environ.get("PORT", "8765")

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
# Spotify rejects `localhost` and demands HTTPS for anything but a loopback
# address, so this is the only plain-HTTP redirect it will accept.
SPOTIFY_REDIRECT_URI = os.environ.get(
    "SPOTIFY_REDIRECT_URI", f"http://127.0.0.1:{PORT}/callback"
)

MAX_EVENTS = 2000
MAX_RECENT = 25

# These are volume mounts inside the container. Outside it (local dev, or a
# missing mount) they may be uncreatable -- warn rather than refuse to import,
# so the app still starts and reports the problem through the UI.
STARTUP_WARNINGS = []
for _label, _path in (
    ("MUSIC_DIR", MUSIC_DIR),
    ("STATE_DIR", STATE_DIR),
):
    try:
        os.makedirs(_path, exist_ok=True)
    except OSError as _e:
        STARTUP_WARNINGS.append(f"{_label} {_path!r} is unavailable: {_e}")
        print(f"[warn] {STARTUP_WARNINGS[-1]}", file=sys.stderr)

spotify = sp.SpotifyClient(
    SPOTIFY_CLIENT_ID,
    SPOTIFY_REDIRECT_URI,
    os.path.join(STATE_DIR, "spotify_token.json"),
)


def require_auth(view):
    """Optional HTTP Basic gate; open when APP_PASSWORD is unset."""

    @functools.wraps(view)
    def wrapped(*a, **kw):
        if not APP_PASSWORD:
            return view(*a, **kw)
        auth = request.authorization
        if auth and auth.password == APP_PASSWORD:
            return view(*a, **kw)
        return Response(
            "Authentication required",
            401,
            {"WWW-Authenticate": 'Basic realm="Playlist Downloader"'},
        )

    return wrapped


def safe_playlist_name(name):
    """A filesystem-safe playlist name: the folder, the CSV stem and the .m3u."""
    stem = re.sub(r"[^A-Za-z0-9._ &()-]", "_", (name or "playlist").strip())
    stem = re.sub(r"\s+", " ", stem).strip(" .") or "playlist"
    return stem[:80]


def playlist_csv_path(stem):
    """<MUSIC_DIR>/<stem>/<stem>.csv -- the CSV lives with the tracks it made.

    download.py derives the output folder from the CSV's own filename, so
    naming the CSV after its folder makes the tracks land beside it.
    """
    return os.path.join(MUSIC_DIR, stem, f"{stem}.csv")


class Job:
    """One playlist run. Its own lock; read by polling requests."""

    def __init__(self, csv_path, bitrate, audio_format=core.DEFAULT_FORMAT,
                 source="upload"):
        self.id = uuid.uuid4().hex[:12]
        self.csv_path = csv_path
        self.name = os.path.splitext(os.path.basename(csv_path))[0]
        self.bitrate = bitrate
        self.audio_format = audio_format
        self.source = source
        self.state = "queued"
        self.error = None
        self.output_dir = None
        self.total = 0
        self.done = 0
        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.current = None
        self.events = []
        self.queued_at = time.time()
        self.started_at = None
        self.finished_at = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def cancel(self):
        self._cancel.set()

    def _add(self, kind, text, detail=None):
        with self._lock:
            self.events.append(
                {"kind": kind, "text": text, "detail": detail, "t": time.time()}
            )
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]

    def snapshot(self, with_events=True):
        with self._lock:
            base = {
                "id": self.id,
                "name": self.name,
                "state": self.state,
                "error": self.error,
                "source": self.source,
                "bitrate": self.bitrate,
                "audio_format": self.audio_format,
                "format": (
                    "M4A (copy)"
                    if self.audio_format == "m4a"
                    else f"MP3 @ {self.bitrate} kbps"
                ),
                "output_dir": self.output_dir,
                "total": self.total,
                "done": self.done,
                "downloaded": self.downloaded,
                "skipped": self.skipped,
                "failed": self.failed,
                "current": self.current,
                "elapsed": round(
                    (self.finished_at or time.time()) - (self.started_at or time.time())
                ),
            }
            if with_events:
                base["events"] = list(self.events)
            return base

    def run(self):
        with self._lock:
            self.state = "running"
            self.started_at = time.time()
        try:
            for ev in core.process_playlist(
                self.csv_path,
                self.bitrate,
                self.audio_format,
                output_root=MUSIC_DIR,
                should_cancel=self._cancel.is_set,
            ):
                self._handle(ev)
        except Exception as e:  # bad CSV, unreadable columns, disk errors
            with self._lock:
                self.state = "error"
                self.error = str(e)
            self._add("error", str(e))
        finally:
            with self._lock:
                if self.state == "running":
                    self.state = "cancelled" if self._cancel.is_set() else "done"
                self.finished_at = time.time()
                self.current = None

    def _handle(self, ev):
        kind = ev["type"]
        if kind == "init":
            with self._lock:
                self.total = ev["total"]
                self.output_dir = ev["output_dir"]
            self._add("info", f"{ev['total']} tracks -> {ev['output_dir']}")
        elif kind == "warning":
            self._add("warn", ev["message"])
        elif kind == "skip":
            with self._lock:
                self.skipped += 1
                self.done += 1
            self._add("skip", ev["basename"])
        elif kind == "track_start":
            with self._lock:
                self.current = ev["query"]
        elif kind == "track_done":
            with self._lock:
                self.downloaded += 1
                self.done += 1
                self.current = None
            detail = ev.get("source_label", "")
            if ev.get("used_fallback"):
                detail += f" - SABR fallback via '{core.FALLBACK_PLAYER_CLIENT}'"
            self._add("ok", ev["query"], detail)
        elif kind == "track_error":
            with self._lock:
                self.failed += 1
                self.done += 1
                self.current = None
            self._add("fail", ev["query"], ev["error"])
        elif kind == "cancelled":
            self._add("warn", "Cancelled")
        elif kind == "summary":
            self._add(
                "info",
                f"Finished: {ev['downloaded']} downloaded, "
                f"{ev['skipped']} skipped, {ev['failed']} failed",
            )


class JobQueue:
    """
    Serial queue with one worker.

    Downloads stay strictly one-at-a-time: parallel YouTube extraction is the
    fastest way to get rate-limited or bot-flagged, and two people using this at
    once should not be able to cause that.
    """

    def __init__(self):
        self._pending = deque()
        self._current = None
        self._recent = deque(maxlen=MAX_RECENT)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def add(self, job):
        with self._lock:
            self._pending.append(job)
            position = len(self._pending) + (1 if self._current else 0)
        self._wake.set()
        return position

    def find(self, job_id):
        with self._lock:
            if self._current and self._current.id == job_id:
                return self._current
            for j in list(self._pending) + list(self._recent):
                if j.id == job_id:
                    return j
        return None

    def cancel(self, job_id):
        """Cancel the running job, or drop a queued one."""
        with self._lock:
            if self._current and self._current.id == job_id:
                self._current.cancel()
                return "running"
            for j in list(self._pending):
                if j.id == job_id:
                    j.cancel()
                    with j._lock:
                        j.state = "cancelled"
                        j.finished_at = time.time()
                    self._pending.remove(j)
                    self._recent.appendleft(j)
                    return "queued"
        return None

    def cancel_all_pending(self):
        dropped = 0
        with self._lock:
            while self._pending:
                j = self._pending.pop()
                j.cancel()
                with j._lock:
                    j.state = "cancelled"
                    j.finished_at = time.time()
                self._recent.appendleft(j)
                dropped += 1
        return dropped

    def busy(self):
        with self._lock:
            return self._current is not None or bool(self._pending)

    def snapshot(self):
        with self._lock:
            current = self._current
            pending = list(self._pending)
            recent = list(self._recent)
        return {
            "current": current.snapshot() if current else None,
            "pending": [j.snapshot(with_events=False) for j in pending],
            "recent": [j.snapshot(with_events=False) for j in recent],
        }

    def _loop(self):
        while True:
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            while True:
                with self._lock:
                    if not self._pending:
                        self._current = None
                        break
                    job = self._pending.popleft()
                    self._current = job
                if job.cancelled:
                    with job._lock:
                        job.state = "cancelled"
                        job.finished_at = time.time()
                else:
                    job.run()
                with self._lock:
                    self._recent.appendleft(job)
                    self._current = None


queue = JobQueue()


def ytdlp_version():
    try:
        import yt_dlp

        return yt_dlp.version.__version__
    except Exception:
        return "unknown"


def list_playlists():
    """Playlist folders under MUSIC_DIR that contain their own <name>.csv."""
    try:
        entries = os.listdir(MUSIC_DIR)
    except OSError:
        return []
    return sorted(
        name for name in entries
        if os.path.isfile(os.path.join(MUSIC_DIR, name, f"{name}.csv"))
    )


# ---------------------------------------------------------------- pages


@app.get("/")
@require_auth
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


# ---------------------------------------------------------------- state


@app.get("/api/state")
@require_auth
def api_state():
    snap = queue.snapshot()
    snap.update(
        {
            "playlists": list_playlists(),
            "ytdlp": ytdlp_version(),
            "music_dir": MUSIC_DIR,
            "startup_warnings": STARTUP_WARNINGS,
            "bitrates": list(core.SUPPORTED_BITRATES),
            "default_bitrate": core.DEFAULT_BITRATE,
            "bitrate_notes": {str(k): v for k, v in core.BITRATE_NOTES.items()},
            "formats": list(core.SUPPORTED_FORMATS),
            "default_format": core.DEFAULT_FORMAT,
            "format_notes": dict(core.FORMAT_NOTES),
            "spotify": {
                "configured": spotify.configured,
                "connected": spotify.connected,
                "redirect_uri": SPOTIFY_REDIRECT_URI,
            },
        }
    )
    return jsonify(snap)


@app.get("/api/job/<job_id>")
@require_auth
def api_job(job_id):
    job = queue.find(job_id)
    if not job:
        return jsonify({"error": "No such job."}), 404
    return jsonify({"job": job.snapshot()})


# ---------------------------------------------------------------- jobs


def _parse_output():
    """(bitrate, audio_format) from the request; raises ValueError if invalid."""
    return (
        core.normalize_bitrate(request.form.get("bitrate", core.DEFAULT_BITRATE)),
        core.normalize_audio_format(
            request.form.get("format", core.DEFAULT_FORMAT)
        ),
    )


@app.post("/api/start")
@require_auth
def api_start():
    try:
        bitrate, audio_format = _parse_output()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    upload = request.files.get("csv")
    if upload and upload.filename:
        if not (upload.filename or "").lower().endswith(".csv"):
            return jsonify({"error": "Please upload a .csv file."}), 400
        stem = safe_playlist_name(
            os.path.splitext(os.path.basename(upload.filename))[0]
        )
        csv_path = playlist_csv_path(stem)
        try:
            os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        except OSError as e:
            return jsonify({"error": f"Could not create the playlist folder: {e}"}), 500
        upload.save(csv_path)
        source = "upload"
    else:
        chosen = request.form.get("playlist", "")
        if not chosen or chosen not in list_playlists():
            return jsonify({"error": "Pick a playlist or upload a CSV."}), 400
        csv_path = playlist_csv_path(chosen)
        source = "saved"

    job = Job(csv_path, bitrate, audio_format, source=source)
    position = queue.add(job)
    return jsonify({"job": job.snapshot(with_events=False), "position": position})


@app.post("/api/cancel")
@require_auth
def api_cancel():
    job_id = request.form.get("job_id", "")
    if not job_id:
        return jsonify({"error": "Which job?"}), 400
    what = queue.cancel(job_id)
    if not what:
        return jsonify({"error": "That job is not queued or running."}), 400
    return jsonify({"ok": True, "cancelled": what})


@app.post("/api/cancel-all")
@require_auth
def api_cancel_all():
    return jsonify({"ok": True, "dropped": queue.cancel_all_pending()})


@app.post("/api/update-ytdlp")
@require_auth
def api_update_ytdlp():
    """
    Upgrade yt-dlp into the mounted volume on PYTHONPATH so it survives image
    rebuilds. The running process keeps the already-imported version, so this
    reports whether a restart is needed.
    """
    if queue.busy():
        return jsonify({"error": "Wait for the queue to finish."}), 409

    target = os.environ.get("YTDLP_UPGRADE_DIR", "/data/ytdlp")
    before = ytdlp_version()

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--no-cache-dir"]
    persisted = True
    try:
        os.makedirs(target, exist_ok=True)
        if not os.access(target, os.W_OK):
            raise OSError(f"{target} is not writable")
        cmd += ["--target", target]
    except OSError:
        # No writable volume (e.g. running outside the container): still upgrade,
        # but into site-packages, where an image rebuild will lose it.
        persisted = False
    cmd.append("yt-dlp")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:
        return jsonify({"error": f"Update failed: {e}"}), 500

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        return jsonify({"error": "pip failed: " + " ".join(tail)}), 500

    installed = "unknown"
    for line in (proc.stdout or "").splitlines():
        if "Successfully installed" in line:
            m = re.search(r"yt-dlp-(\S+)", line)
            if m:
                installed = m.group(1)

    # pip reports 2026.8.19 where yt-dlp reports 2026.08.19 -- compare on
    # numeric components so the same version does not look like an upgrade.
    def norm(v):
        return tuple(
            int(part) if part.isdigit() else part for part in re.split(r"[.\-+]", v)
        )

    changed = installed != "unknown"
    if changed:
        try:
            changed = norm(installed) != norm(before)
        except Exception:
            changed = installed != before

    return jsonify(
        {
            "ok": True,
            "running": before,
            "installed": installed,
            "persisted": persisted,
            "restart_required": changed,
        }
    )


# ---------------------------------------------------------------- spotify


@app.get("/api/spotify/login")
@require_auth
def spotify_login():
    try:
        return redirect(spotify.build_auth_url())
    except sp.SpotifyError as e:
        return jsonify({"error": str(e)}), 400


@app.get("/callback")
@require_auth
def spotify_callback():
    err = request.args.get("error")
    if err:
        return _callback_page(f"Spotify returned an error: {err}", ok=False)
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    if not code or not state:
        return _callback_page("Spotify sent no authorization code.", ok=False)
    try:
        spotify.exchange_code(code, state)
    except sp.SpotifyError as e:
        return _callback_page(str(e), ok=False)
    except Exception as e:
        return _callback_page(f"Could not reach Spotify: {e}", ok=False)
    return _callback_page("Connected. You can close this tab.", ok=True)


def _callback_page(message, ok):
    colour = "#2f6f4f" if ok else "#a3302a"
    body = (
        "<!doctype html><meta charset='utf-8'>"
        "<title>Spotify</title>"
        "<body style=\"font:15px -apple-system,sans-serif;padding:40px;"
        'text-align:center">'
        f'<p style="color:{colour};font-size:17px">{message}</p>'
        '<p><a href="/">Back to Playlist Downloader</a></p>'
        "</body>"
    )
    return Response(body, 200 if ok else 400, {"Content-Type": "text/html"})


@app.post("/api/spotify/disconnect")
@require_auth
def spotify_disconnect():
    spotify.disconnect()
    return jsonify({"ok": True})


@app.get("/api/spotify/playlists")
@require_auth
def spotify_playlists():
    if not spotify.configured:
        return jsonify({"error": "Spotify is not configured."}), 400
    if not spotify.connected:
        return jsonify({"error": "Not connected to Spotify."}), 401
    try:
        return jsonify(
            {"me": spotify.me(), "playlists": spotify.playlists()}
        )
    except sp.SpotifyError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Could not reach Spotify: {e}"}), 502


@app.post("/api/spotify/download")
@require_auth
def spotify_download():
    if not spotify.connected:
        return jsonify({"error": "Not connected to Spotify."}), 401
    try:
        bitrate, audio_format = _parse_output()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    playlist_id = request.form.get("playlist_id", "").strip()
    if not playlist_id:
        return jsonify({"error": "Which playlist?"}), 400

    # Export to a CSV first: it reuses the tested pipeline unchanged and leaves
    # the user a portable artifact, exactly like an Exportify download.
    try:
        name = request.form.get("name") or spotify.playlist_name(playlist_id)
        csv_path = playlist_csv_path(safe_playlist_name(name))
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        count = spotify.write_playlist_csv(playlist_id, csv_path)
    except sp.SpotifyError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"Could not write the CSV: {e}"}), 500
    except Exception as e:
        return jsonify({"error": f"Could not reach Spotify: {e}"}), 502

    job = Job(csv_path, bitrate, audio_format, source="spotify")
    position = queue.add(job)
    return jsonify(
        {
            "job": job.snapshot(with_events=False),
            "position": position,
            "tracks": count,
            "csv": os.path.basename(csv_path),
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(PORT), threaded=True)
