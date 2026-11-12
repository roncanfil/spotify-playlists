"""
Web UI for Spotify Playlists.

Wraps download.process_playlist so the browser and the standalone script run
identical logic. Music comes from an uploaded CSV, a playlist of the connected
Spotify account, or a pasted Spotify link; every path ends up as a CSV and a
job on one serial queue. Where the tracks land:

    playlist   MUSIC_DIR/<Playlist>/      with its CSV and .m3u
    album      MUSIC_DIR/<Artist>/<Album>/ with its CSV and .m3u
    song       MUSIC_DIR/<Artist>/         just the file; its CSV is kept in
                                           the state folder
"""

import functools
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections import deque

from flask import (
    Flask, Response, jsonify, redirect, render_template, request, session, url_for
)

# download.py in this folder is the engine: search, download, transcode, tag.
# It also still runs standalone as a deprecated CLI; see ../cli/README.md.
import download as core
import slskd
import slskd_config
import spotify as sp
import spotify_link
from sources import P2PSource

app = Flask(__name__)

APP_VERSION = os.environ.get("APP_VERSION", "dev")

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")

# Every playlist is one folder under MUSIC_DIR holding its own CSV, its tracks
# and its .m3u, so there is no separate playlist directory to configure.
#
def _state_dir():
    """
    Where the Spotify refresh token lives: a hidden folder inside MUSIC_DIR, so
    the music mount stays the only volume anyone has to set up. Dot-prefixed to
    keep it out of file browsers and media scanners.

    Renamed from .playlist-downloader in 1.9.0. An existing one is moved rather
    than abandoned, so nobody has to reconnect Spotify. If the move fails we
    keep using the old folder instead of starting fresh and silently losing the
    token.

    yt-dlp's self-updates deliberately do NOT live here: the entrypoint
    reinstalls them on every boot, so they belong in the container's own layer
    (/data/ytdlp, where the image points PYTHONPATH) and need no volume.
    """
    current = os.path.join(MUSIC_DIR, ".spotify-playlists")
    legacy = os.path.join(MUSIC_DIR, ".playlist-downloader")
    if not os.path.isdir(current) and os.path.isdir(legacy):
        try:
            os.rename(legacy, current)
            print(f"[boot] moved {legacy} -> {current}", file=sys.stderr)
        except OSError as e:
            print(f"[warn] could not move {legacy} to {current} ({e}); "
                  "still using the old folder", file=sys.stderr)
            return legacy
    return current


STATE_DIR = _state_dir()

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

def _secret_key():
    """
    Stable signing key for the login cookie, kept beside the Spotify token.

    Generated once and reused, so restarting the container does not sign
    everyone out. If STATE_DIR is unwritable we fall back to a per-process key
    rather than refusing to start -- logins then last until the next restart.
    """
    path = os.path.join(STATE_DIR, "secret_key")
    try:
        with open(path, "rb") as fh:
            key = fh.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_bytes(32)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(key)
        os.chmod(path, 0o600)
    except OSError as e:
        print(f"[warn] could not persist the session key ({e}); "
              "logins will not survive a restart", file=sys.stderr)
    return key


app.secret_key = _secret_key()
# Lax rather than Strict: Spotify redirects the browser back to /callback from
# accounts.spotify.com, and Strict would drop the cookie on that hop.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

spotify = sp.SpotifyClient(
    SPOTIFY_CLIENT_ID,
    SPOTIFY_REDIRECT_URI,
    os.path.join(STATE_DIR, "spotify_token.json"),
)


def _password_ok(candidate):
    """Constant-time compare, so a wrong guess reveals nothing by timing."""
    return secrets.compare_digest(str(candidate or ""), APP_PASSWORD)


def _authenticated():
    if not APP_PASSWORD:
        return True
    if session.get("authed"):
        return True
    # HTTP Basic is still honoured so curl and scripts keep working.
    auth = request.authorization
    return bool(auth and _password_ok(auth.password))


@app.after_request
def _no_store(resp):
    """
    Stop browsers caching the HTML and the API.

    All of this app's JavaScript is inline in index.html, so a cached page
    keeps running the previous release's front end after an image update --
    indistinguishable, from the user's side, from the update not working.
    API responses are live state and must never be cached either.
    """
    if resp.mimetype in ("text/html", "application/json"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def require_auth(view):
    """
    Optional gate; wide open when APP_PASSWORD is unset.

    A browser gets the login page rather than the OS Basic-auth dialog, which
    cannot be styled and gives no way to show an error. Anything under /api/
    gets a 401 JSON instead, because a redirect to an HTML page is useless to
    fetch() -- the front end turns that 401 into a page load of /login.
    """

    @functools.wraps(view)
    def wrapped(*a, **kw):
        if _authenticated():
            return view(*a, **kw)
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not signed in.", "login": True}), 401
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    return wrapped


def safe_playlist_name(name):
    """
    A filesystem-safe playlist name: the folder, the CSV stem and the .m3u.

    Removes what a filesystem actually objects to, rather than everything
    outside ASCII. The old allowlist turned "Mañanas alegre Sing-Along 🎵🎉"
    into "Ma_anas alegre Sing-Along __", which is lossy for anyone whose
    playlists are not in English, and leaves underscores where an emoji was.
    Matches download.py's sanitize_filename, which was already Unicode-safe.
    """
    stem = unicodedata.normalize("NFC", str(name or ""))
    # Reserved on Windows and over SMB, and "/" everywhere.
    stem = re.sub(r'[<>:"/\\|?*]', "", stem)
    # Control characters, which no filesystem wants in a name.
    stem = "".join(c for c in stem if unicodedata.category(c) != "Cc")
    stem = re.sub(r"\s+", " ", stem).strip()
    # A leading dot would hide the folder and make list_playlists() skip it;
    # trailing dots and spaces are invalid on Windows and over SMB.
    stem = stem.strip(" .")[:80].strip(" .")
    return stem or "playlist"


def playlist_csv_path(stem):
    """<MUSIC_DIR>/<stem>/<name>.csv -- the CSV lives with the tracks it made.

    `stem` is a folder under MUSIC_DIR: "Mix" for a playlist, "Artist/Album"
    for an album. The CSV is named after the last part, which is also what
    the .m3u is called.
    """
    return os.path.join(MUSIC_DIR, *stem.split("/"), f"{stem.split('/')[-1]}.csv")


class Job:
    """One playlist run. Its own lock; read by polling requests."""

    def __init__(self, csv_path, bitrate, audio_format=core.DEFAULT_FORMAT,
                 source="upload", name=None, output_dir=None, write_playlist=True):
        self.id = uuid.uuid4().hex[:12]
        self.csv_path = csv_path
        self.name = name or os.path.splitext(os.path.basename(csv_path))[0]
        # The CSV's own folder unless told otherwise (a song's CSV is kept
        # out of the music library).
        self.target_dir = output_dir or os.path.dirname(csv_path)
        self.write_playlist = write_playlist
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
        # Saved tracks per source label, for the Recent row.
        self.by_source = {}
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

    def _add(self, kind, text, detail=None, source=None):
        with self._lock:
            self.events.append(
                {"kind": kind, "text": text, "detail": detail, "source": source,
                 "t": time.time()}
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
                "by_source": dict(self.by_source),
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
                # Read when the job starts, so a change in Settings applies to
                # the next job rather than reshuffling one mid-run.
                sources=ordered_sources(),
                output_dir=self.target_dir,
                write_playlist=self.write_playlist,
            ):
                self._handle(ev)
            if self.downloaded:
                rescan_share()
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
            label = ev.get("source_name") or "YouTube"
            with self._lock:
                self.downloaded += 1
                self.done += 1
                self.current = None
                self.by_source[label] = self.by_source.get(label, 0) + 1
            detail = ev.get("source_label", "")
            # The row carries the source as a tag; the detail adds who sent it.
            if ev.get("peer"):
                detail = f"{ev['peer']}: {detail}"
            if ev.get("used_fallback"):
                detail += f" - SABR fallback via '{core.FALLBACK_PLAYER_CLIENT}'"
            # Say so when the full "A, B - Title" query found nothing and a
            # trimmed one did, since that changes which track you may have got.
            used_q = ev.get("used_query")
            if used_q and used_q != ev.get("query"):
                detail += f" - matched on '{used_q}'"
            # Which sources missed before this one, so a YouTube copy of a
            # track Soulseek could not find says so rather than passing silently.
            for a in ev.get("attempts") or []:
                detail += f" - {a['label']} missed: {a['error']}"
            self._add("ok", ev["query"], detail, source=label)
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
                f"Finished: {ev['downloaded']} saved, "
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


# ---------------------------------------------------------------- sources

SETTINGS_PATH = os.path.join(STATE_DIR, "settings.json")
# Where the source order was kept before onboarding existed; read if present.
LEGACY_ORDER_PATH = os.path.join(STATE_DIR, "sources.json")
# Every source the app knows, in the default order.
KNOWN_SOURCES = {"soulseek": "Soulseek", "youtube": "YouTube"}
SOURCE_NOTES = {
    "soulseek": "Real 320 kbps and lossless files from people's collections. "
    "MP3 output only; M4A always comes from YouTube.",
    "youtube": "Nothing to set up. Audio tops out around 130 kbps.",
}
STATUS_TTL = 15
_status_cache = {"at": 0.0, "items": None}
_status_lock = threading.Lock()
_settings_lock = threading.Lock()

# slskd is reached by its compose service name; override for other setups.
SLSKD_URL = os.environ.get("SLSKD_URL", "").strip() or "http://slskd:5030"
# slskd keeps its settings, database and downloads here, in the same place
# inside both containers. See slskd_config.
slsk_settings = slskd_config.Settings(os.path.join(STATE_DIR, "slskd"), MUSIC_DIR)
try:
    # Writes the API key slskd and this app share. slskd picks up the file
    # whenever it changes, so the order the two containers start in does not
    # matter.
    slsk_settings.ensure()
except OSError as _e:
    STARTUP_WARNINGS.append(f"Could not write slskd's settings: {_e}")
    print(f"[warn] {STARTUP_WARNINGS[-1]}", file=sys.stderr)


def load_settings():
    """{"order": [...], "onboarded": bool}, cleaned; defaults when missing or broken."""
    data = {}
    for path in (SETTINGS_PATH, LEGACY_ORDER_PATH):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                break
        except (OSError, ValueError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    saved = data.get("order") or []
    order = [n for n in saved if n in KNOWN_SOURCES]
    order += [n for n in KNOWN_SOURCES if n not in order]
    return {"order": order, "onboarded": bool(data.get("onboarded"))}


def save_settings(**changes):
    with _settings_lock:
        cur = load_settings()
        cur.update(changes)
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cur, fh)
        os.replace(tmp, SETTINGS_PATH)
    return cur


def load_source_order():
    return load_settings()["order"]


def save_source_order(order):
    save_settings(order=order)


def soulseek_client():
    return slskd.SlskdClient(
        SLSKD_URL, slsk_settings.read()["api_key"], slsk_settings.downloads_dir,
    )


def ordered_sources():
    """
    The sources a job uses, in the order set in Settings. Soulseek only once
    an account is set up: an slskd with no account would only add a warning
    to every job.
    """
    found = [core.YouTubeSource()]
    if slsk_settings.read()["username"]:
        found.append(P2PSource(soulseek_client()))
    order = load_source_order()
    return sorted(found, key=lambda s: order.index(s.name) if s.name in order else len(order))


def soulseek_status():
    """
    (state, message) for the UI: "connected", "connecting", "not set up",
    "not installed" or "error".
    """
    acct = slsk_settings.read()
    user = acct["username"]
    state, message = soulseek_client().status(timeout=3)
    if state == "not installed":
        return state, message
    if state in ("error", "not configured"):
        return "error", message
    if not user:
        return "not set up", "No Soulseek account yet"
    if state == "logged out":
        if message == "Disconnected":
            return "error", (
                f"Soulseek refused the login for {user}: wrong password, or "
                "someone else has that name"
            )
        return "connecting", f"Connecting as {user}"
    return "connected", message


def invalidate_status():
    with _status_lock:
        _status_cache.update(at=0.0, items=None)


def source_status():
    """
    {name: {state, message}} for every known source. Cached: the UI polls
    every second or two while a job runs, and asking slskd that often would be
    pointless load on it.
    """
    with _status_lock:
        if _status_cache["items"] and time.time() - _status_cache["at"] < STATUS_TTL:
            return _status_cache["items"]
    state, message = soulseek_status()
    items = {
        "soulseek": {"state": state, "message": message},
        "youtube": {"state": "connected", "message": f"yt-dlp {ytdlp_version()}"},
    }
    with _status_lock:
        _status_cache.update(at=time.time(), items=items)
    return items


def sources_block():
    status = source_status()
    acct = slsk_settings.read()
    order = load_source_order()
    items = []
    for name in order:
        item = {"name": name, "label": KNOWN_SOURCES[name], "note": SOURCE_NOTES[name],
                **status[name]}
        if name == "soulseek":
            item["account"] = {"username": acct["username"], "share": acct["share"]}
        items.append(item)
    return {"order": order, "items": items}


def rescan_share():
    """After new downloads, so they are offered on Soulseek too. Best effort."""
    acct = slsk_settings.read()
    if acct["username"] and acct["share"]:
        soulseek_client().rescan_shares()


def ytdlp_version():
    try:
        import yt_dlp

        return yt_dlp.version.__version__
    except Exception:
        return "unknown"


def _subfolders(path):
    try:
        return [n for n in os.listdir(path)
                if not n.startswith(".") and os.path.isdir(os.path.join(path, n))]
    except OSError:
        return []


def list_playlists():
    """
    Folders under MUSIC_DIR that hold their own <name>.csv: playlists
    ("Mix") and, one level down, albums ("Artist/Album").

    Skips dot-prefixed names so our own state folder, and anything else
    hidden, never shows up as a playlist.
    """
    found = []
    for name in _subfolders(MUSIC_DIR):
        if os.path.isfile(playlist_csv_path(name)):
            found.append(name)
        for sub in _subfolders(os.path.join(MUSIC_DIR, name)):
            if os.path.isfile(playlist_csv_path(f"{name}/{sub}")):
                found.append(f"{name}/{sub}")
    return sorted(found)


# ---------------------------------------------------------------- pages


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect("/")

    # Only ever redirect within this site; an absolute URL here would be an
    # open redirect.
    target = request.values.get("next") or "/"
    if not target.startswith("/") or target.startswith("//"):
        target = "/"

    if request.method == "POST":
        if _password_ok(request.form.get("password")):
            session["authed"] = True
            session.permanent = True
            return redirect(target)
        return (
            render_template("login.html", error="Wrong password.", next_url=target),
            401,
        )

    if session.get("authed"):
        return redirect(target)
    return render_template("login.html", error=None, next_url=target)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@require_auth
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    # Version is here as well as in /api/state: this endpoint needs no login,
    # so "which build is actually running?" is answerable without one.
    return jsonify({"ok": True, "version": APP_VERSION})


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
            # Drives the Sign out link; there is nothing to sign out of when
            # no password is configured.
            "auth_enabled": bool(APP_PASSWORD),
            "app_version": APP_VERSION,
            "formats": list(core.SUPPORTED_FORMATS),
            "default_format": core.DEFAULT_FORMAT,
            "format_notes": dict(core.FORMAT_NOTES),
            "spotify": {
                "configured": spotify.configured,
                "connected": spotify.connected,
                "redirect_uri": SPOTIFY_REDIRECT_URI,
            },
            "sources": sources_block(),
            "setup": {"onboarded": load_settings()["onboarded"]},
        }
    )
    return jsonify(snap)


@app.post("/api/sources/order")
@require_auth
def api_sources_order():
    order = [n.strip() for n in request.form.get("order", "").split(",") if n.strip()]
    if sorted(order) != sorted(KNOWN_SOURCES):
        return jsonify({"error": "List every source exactly once."}), 400
    try:
        save_source_order(order)
    except OSError as e:
        return jsonify({"error": f"Could not save the order: {e}"}), 500
    return jsonify({"ok": True, "sources": sources_block()})


# ---------------------------------------------------------------- soulseek setup


@app.post("/api/soulseek/account")
@require_auth
def api_soulseek_account():
    """
    Save a Soulseek account and log in with it. The first login creates the
    account, so there is nothing to sign up for. A blank password keeps the
    saved one when the username is unchanged, so sharing can be toggled
    without retyping it.
    """
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    share = request.form.get("share")
    if not username:
        return jsonify({"error": "Choose a Soulseek username."}), 400
    if any(c.isspace() for c in username) or len(username) > 30:
        return jsonify({"error": "Soulseek usernames have no spaces and at most 30 characters."}), 400
    current = slsk_settings.read()
    if not password and username != current["username"]:
        return jsonify({"error": "Enter a password for this account."}), 400
    client = soulseek_client()
    state, message = client.status(timeout=5)
    if state == "not installed":
        return jsonify({"error": message}), 409
    if state == "error":
        return jsonify({"error": message}), 502
    try:
        slsk_settings.update(
            username=username,
            password=password or None,
            share=None if share is None else share in ("1", "true", "on"),
        )
    except OSError as e:
        return jsonify({"error": f"Could not save slskd's settings: {e}"}), 500
    if not client.wait_for_options(
        lambda o: (o.get("soulseek") or {}).get("username") == username
    ):
        return jsonify({"error": "slskd did not pick up the new account; try again."}), 502
    ok = client.connect(wait=20)
    invalidate_status()
    if not ok:
        return jsonify({"error": (
            f"Soulseek refused the login for {username}. Either the password is "
            "wrong or someone else already has that name; try another."
        )}), 400
    rescan_share()
    return jsonify({"ok": True, "message": f"Logged in as {username}", "sources": sources_block()})


@app.post("/api/soulseek/remove")
@require_auth
def api_soulseek_remove():
    try:
        slsk_settings.update(username="")
    except OSError as e:
        return jsonify({"error": f"Could not save slskd's settings: {e}"}), 500
    soulseek_client().disconnect()
    invalidate_status()
    return jsonify({"ok": True, "sources": sources_block()})


@app.post("/api/soulseek/share")
@require_auth
def api_soulseek_share():
    on = request.form.get("share") in ("1", "true", "on")
    try:
        slsk_settings.update(share=on)
    except OSError as e:
        return jsonify({"error": f"Could not save slskd's settings: {e}"}), 500
    # A rescan applies the change either way: it adds the library, or empties
    # the share now that no folder is listed. Only once slskd has the setting.
    client = soulseek_client()
    client.wait_for_options(
        lambda o: (MUSIC_DIR in ((o.get("shares") or {}).get("directories") or [])) == on
    )
    client.rescan_shares()
    invalidate_status()
    return jsonify({"ok": True, "sources": sources_block()})


@app.post("/api/setup/done")
@require_auth
def api_setup_done():
    """Finish onboarding: the chosen source goes first, the other is the fallback."""
    first = request.form.get("first", "")
    if first not in KNOWN_SOURCES:
        return jsonify({"error": "Choose Soulseek or YouTube."}), 400
    order = [first] + [n for n in KNOWN_SOURCES if n != first]
    try:
        save_settings(order=order, onboarded=True)
    except OSError as e:
        return jsonify({"error": f"Could not save the settings: {e}"}), 500
    return jsonify({"ok": True, "sources": sources_block()})


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
        '<p><a href="/">Back to Spotify Playlists</a></p>'
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
        # write_playlist_csv creates the folder itself, after the fetch
        # succeeds, so a refused playlist leaves nothing behind.
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


# ---------------------------------------------------------------- pasted links

# A song's CSV: kept out of the music folder, which holds only music.
LINKS_DIR = os.path.join(STATE_DIR, "links")
# How long a link read for its preview is reused when Download is pressed.
LINK_CACHE_TTL = 600
_link_cache = {}
_link_lock = threading.Lock()


def read_link(text):
    """The Collection for pasted text, from the cache when it is fresh."""
    link = spotify_link.parse(text)
    now = time.time()
    with _link_lock:
        hit = _link_cache.get(link.uri)
        if hit and now - hit[0] < LINK_CACHE_TTL:
            return hit[1]
    col = spotify_link.read(link)
    with _link_lock:
        for uri in [u for u, (t, _) in _link_cache.items() if now - t >= LINK_CACHE_TTL]:
            del _link_cache[uri]
        _link_cache[link.uri] = (now, col)
    return col


def link_target(col):
    """
    Where a link's tracks go: {"csv", "folder" (relative to MUSIC_DIR),
    "output_dir", "write_playlist", "name" (for the queue)}.
    """
    if col.kind == "playlist":
        stem = safe_playlist_name(col.name)
        return {"csv": playlist_csv_path(stem), "folder": stem,
                "output_dir": os.path.join(MUSIC_DIR, stem),
                "write_playlist": True, "name": col.name}
    # Albums and songs file under their first artist, so a song by
    # "Future, Drake" sits with Future's other music.
    artist = safe_playlist_name(col.artist or "Unknown artist")
    if col.kind == "album":
        album = safe_playlist_name(col.name)
        folder = f"{artist}/{album}"
        return {"csv": playlist_csv_path(folder), "folder": folder,
                "output_dir": os.path.join(MUSIC_DIR, artist, album),
                "write_playlist": True, "name": f"{col.artist} \u2013 {col.name}"}
    return {"csv": os.path.join(LINKS_DIR, f"{col.id}.csv"), "folder": artist,
            "output_dir": os.path.join(MUSIC_DIR, artist),
            "write_playlist": False, "name": f"{col.artist} \u2013 {col.name}"}


def _link_error(e):
    if isinstance(e, spotify_link.LinkError):
        return jsonify({"error": str(e)}), 400
    return jsonify({"error": f"Could not read the link: {e}"}), 502


@app.post("/api/link/preview")
@require_auth
def api_link_preview():
    try:
        col = read_link(request.form.get("link", ""))
    except Exception as e:
        return _link_error(e)
    target = link_target(col)
    return jsonify({
        "kind": col.kind,
        "name": col.name,
        "artist": col.artist,
        "image": col.image,
        "tracks": len(col.rows),
        "total": col.total,
        "truncated": col.truncated,
        "folder": target["folder"],
        "sample": [
            {"artist": r.get("Artist Name(s)", ""), "title": r.get("Track Name", ""),
             "ms": int(r.get("Track Duration (ms)") or 0)}
            for r in col.rows[:5]
        ],
    })


@app.post("/api/link/download")
@require_auth
def api_link_download():
    try:
        bitrate, audio_format = _parse_output()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        col = read_link(request.form.get("link", ""))
    except Exception as e:
        return _link_error(e)
    target = link_target(col)
    try:
        spotify_link.write_csv(col, target["csv"])
    except OSError as e:
        return jsonify({"error": f"Could not write the CSV: {e}"}), 500
    job = Job(target["csv"], bitrate, audio_format, source="link", name=target["name"],
              output_dir=target["output_dir"], write_playlist=target["write_playlist"])
    position = queue.add(job)
    return jsonify({
        "job": job.snapshot(with_events=False),
        "position": position,
        "tracks": len(col.rows),
        "folder": target["folder"],
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(PORT), threaded=True)
