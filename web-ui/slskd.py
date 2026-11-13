"""
Soulseek, through an slskd sidecar (https://github.com/slskd/slskd).

The app never speaks the Soulseek protocol itself: slskd holds the account,
the peer connections and the transfers, and this module drives its HTTP API.
slskd keeps its files in a hidden folder inside the music library, mounted at
the same path in both containers, so finished downloads are visible here as
they are; sources.P2PSource converts them into the playlist folder. Its
settings file is written by slskd_config.

Checked against slskd 0.26.2's Swagger and source, and against a live
instance. Facts that shaped the code, none of them obvious from the docs:

- searchTimeout is documented in seconds but handed unchanged to
  Soulseek.NET, which reads milliseconds. 15 meant 15 ms and every search came
  back empty. We send milliseconds and also stop the search ourselves after
  SEARCH_MAX_WAIT, so a future fix to the unit cannot make searches hang.
- Search results usually leave "extension" empty; it comes from the filename.
- After the host sleeps, slskd can keep reporting "Connected, LoggedIn" while
  every search comes back with zero responses until it reconnects. A run of
  empty searches therefore triggers a reconnect (EMPTY_STREAK_RECONNECT).
- Enqueueing a download makes slskd contact the peer with a fixed 5 s wait.
  Peers that cannot be reached fail that way within seconds; sources.py skips
  the rest of their files and gives them a separate budget.
- The batch download endpoint takes a destination folder per batch. Giving
  every candidate its own folder means the finished file is simply the one
  file in it, whatever slskd did to its name.
"""

import os
import re
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import requests

import match
from sources import Cancelled, P2PClient, PeerUnreachable

API = "/api/v0"

# Soulseek bans an account for 30 minutes after more than 34 searches in 220
# seconds. Stay well clear: 30 per 240 s.
SEARCH_LIMIT = 30
SEARCH_WINDOW = 240

# slskd ends a search once no response has arrived for this long (ms).
SEARCH_IDLE_MS = 10_000
# And we stop it regardless after this many seconds.
SEARCH_MAX_WAIT = 45
SEARCH_RESPONSE_LIMIT = 100

# A peer that has not started sending within this long is queued behind other
# downloaders; the next candidate is the faster way to the track.
START_TIMEOUT = 60
# A transfer whose byte count has not moved for this long has stalled.
STALL_TIMEOUT = 45
# slskd marks a transfer Succeeded and then moves the file out of its
# incomplete folder; allow that move this long to show up.
SETTLE_TIMEOUT = 15
POLL = 1.0

HTTP_TIMEOUT = 15

# Search ids we create start with this, so old searches of ours can be tidied
# away without touching ones the user made in slskd's own UI. slskd keeps
# every search (with all its responses) forever unless told otherwise, and its
# retention setting cannot be set from the environment.
OUR_SEARCH_PREFIX = "5907ec7d"
SEARCH_KEEP = 3600

# The subfolder of slskd's downloads folder this app downloads into.
DOWNLOAD_SUBDIR = "spotify-playlists"

# Consecutive searches with no responses at all before we make slskd drop and
# re-establish its server connection. Popular songs draw dozens of responses
# within seconds, so a run of nothing is far likelier a dead session than
# three unknown songs. Per slskd URL and process-wide, so it spans jobs.
EMPTY_STREAK_RECONNECT = 3
LOGIN_WAIT = 30
_empty_streaks = {}


def _is_name_error(exc):
    """True when a connection failed because the host name does not resolve."""
    e = exc
    while e is not None:
        text = f"{type(e).__name__} {e}"
        if "NameResolution" in text or "Name or service not known" in text \
                or "nodename nor servname" in text or "gaierror" in text:
            return True
        e = e.__cause__ or e.__context__
    return False


def _parse_time(value):
    """slskd's timestamps ("2026-11-11T20:09:24.2896148Z") as epoch seconds, or None."""
    if not value:
        return None
    s = str(value).rstrip("Z")
    # .NET writes seven fractional digits; Python wants at most six.
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


class SearchLimiter:
    """
    Sliding window over every search the account made, shared by everything
    in this process. Holds wall-clock times by search id, so the searches
    slskd already knows about (from before an app restart, or typed into its
    own UI) count too: the ban applies to the account, not to this app.
    """

    def __init__(self, limit=SEARCH_LIMIT, window=SEARCH_WINDOW,
                 clock=time.time, sleep=time.sleep):
        self.limit = limit
        self.window = window
        self.clock = clock
        self.sleep = sleep
        self._seen = {}
        self._lock = threading.Lock()

    def note(self, search_id, started_at):
        with self._lock:
            self._seen[search_id] = started_at

    def _recent(self, now):
        cutoff = now - self.window
        for k in [k for k, t in self._seen.items() if t <= cutoff]:
            del self._seen[k]
        return sorted(self._seen.values())

    def wait_time(self):
        """Seconds until one more search is allowed; 0 when it is now."""
        with self._lock:
            now = self.clock()
            recent = self._recent(now)
            if len(recent) < self.limit:
                return 0.0
            return recent[len(recent) - self.limit] + self.window - now

    def acquire(self, search_id, should_cancel=None):
        """Wait for room, then record a search. Raises Cancelled if asked to stop."""
        while True:
            if should_cancel and should_cancel():
                raise Cancelled()
            wait = self.wait_time()
            if wait <= 0:
                self.note(search_id, self.clock())
                return
            self.sleep(min(wait, POLL))


# One per process: two jobs never run at once, but a limiter per client would
# forget the previous job's searches.
LIMITER = SearchLimiter()


class SlskdError(Exception):
    """slskd answered, but not with what we asked for."""


class SlskdClient(P2PClient):
    name = "soulseek"
    label = "Soulseek"

    def __init__(self, base_url, api_key, downloads_dir="/downloads",
                 limiter=None, session=None, clock=time.monotonic, sleep=time.sleep):
        self.base = base_url.rstrip("/") + API
        self.api_key = api_key
        self.downloads_dir = downloads_dir
        self.limiter = limiter or LIMITER
        self.http = session or requests.Session()
        self.http.headers["X-API-Key"] = api_key or ""
        self.clock = clock
        self.sleep = sleep
        # local path -> (username, transfer id), to clear the transfer record
        # from slskd once the file has been dealt with.
        self._transfers = {}

    @classmethod
    def from_env(cls, env=os.environ):
        """For running the engine by hand against an slskd you configured yourself."""
        return cls(
            env.get("SLSKD_URL", "").strip(),
            env.get("SLSKD_API_KEY", "").strip(),
            env.get("SLSKD_DOWNLOADS_DIR", "").strip() or "/downloads",
        )

    # ------------------------------------------------------------ http

    def _req(self, method, path, **kw):
        kw.setdefault("timeout", HTTP_TIMEOUT)
        try:
            resp = self.http.request(method, self.base + path, **kw)
        except requests.RequestException as e:
            raise SlskdError(f"could not reach slskd ({type(e).__name__})") from e
        if resp.status_code == 401:
            raise SlskdError(
                "slskd refused the app's key; restart slskd so it rereads its settings"
            )
        return resp

    def _json(self, method, path, ok=(200,), **kw):
        resp = self._req(method, path, **kw)
        if resp.status_code not in ok:
            raise SlskdError(f"slskd answered {resp.status_code}: {resp.text.strip()[:200]}")
        return resp.json() if resp.content else None

    # ------------------------------------------------------------ status

    def status(self, timeout=HTTP_TIMEOUT):
        """
        (state, sentence) for the UI and check(). States:
          "not installed"  slskd's host name does not resolve: its service is
                           missing, stopped, or on another Docker network
          "not configured" no URL or key to reach it with
          "logged out"     slskd runs but is not logged in to Soulseek
          "error"          unreachable for any other reason, or misbehaving
          "connected"      logged in and usable
        """
        if not self.base.startswith("http"):
            return "not configured", "SLSKD_URL is not set"
        if not self.api_key:
            return "not configured", "slskd has no API key yet"
        try:
            resp = self.http.request(
                "GET", self.base + "/application", timeout=timeout,
            )
        except requests.ConnectionError as e:
            if _is_name_error(e):
                # All a failed lookup proves is that the name does not resolve:
                # the service is missing, stopped, or on another Docker network
                # (CasaOS can put apps on separate networks).
                host = self.base.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
                return "not installed", (
                    f"The app cannot find slskd at {self.base}: the name {host} does not "
                    "resolve. Check that the slskd service is in the compose file, is "
                    "running, and is on the same Docker network as the app."
                )
            return "error", "slskd is not answering (it may still be starting)"
        except requests.RequestException as e:
            return "error", f"could not reach slskd ({type(e).__name__})"
        if resp.status_code == 401:
            return "error", "slskd refused the app's key; restart slskd so it rereads its settings"
        if resp.status_code != 200:
            return "error", f"slskd answered {resp.status_code}"
        app = resp.json() or {}
        server = app.get("server") or {}
        if not server.get("isLoggedIn"):
            return "logged out", server.get("state") or "Disconnected"
        if not os.path.isdir(self.downloads_dir):
            return "error", (
                f"slskd's download folder {self.downloads_dir} is not visible here; "
                "both services need the same music folder mounted at /music"
            )
        user = (app.get("user") or {}).get("username") or "?"
        return "connected", f"logged in as {user}"

    def connect(self, should_cancel=None, wait=LOGIN_WAIT):
        """
        (Re)connect with whatever account the settings file now holds, and
        wait for the login. True once logged in. slskd answers the connect
        call with 200 even when the password is wrong; the outcome only shows
        as the server state settling on Disconnected, so poll for it.
        """
        try:
            self._req("DELETE", "/server", json="reconnecting with new settings")
            self._req("PUT", "/server")
        except SlskdError:
            return False
        deadline = self.clock() + wait
        settled = 0
        while self.clock() < deadline:
            if should_cancel and should_cancel():
                raise Cancelled()
            try:
                st = self._json("GET", "/server") or {}
            except SlskdError:
                st = {}
            if st.get("isLoggedIn"):
                return True
            # Two polls in a row at a plain Disconnected, after the attempt has
            # had a moment, is a refused login rather than one still going.
            if st.get("state") == "Disconnected" and not st.get("isConnecting") \
                    and not st.get("isLoggingIn"):
                settled += 1
                if settled >= 2:
                    return False
            else:
                settled = 0
            self.sleep(POLL)
        return False

    def wait_for_options(self, ready, wait=15):
        """
        Poll slskd's live options until ready(options) is true; False on
        timeout. slskd rereads its settings file a moment after it changes,
        and connecting before then would log in with the old account.
        """
        deadline = self.clock() + wait
        while self.clock() < deadline:
            try:
                if ready(self._json("GET", "/options") or {}):
                    return True
            except SlskdError:
                pass
            self.sleep(0.5)
        return False

    def disconnect(self):
        try:
            self._req("DELETE", "/server", json="account removed")
        except SlskdError:
            pass

    def rescan_shares(self):
        """Ask slskd to rescan what it shares, so new downloads are offered too."""
        try:
            self._req("PUT", "/shares")
            return True
        except SlskdError:
            return False

    def check(self):
        state, message = self.status()
        return None if state == "connected" else message

    # ------------------------------------------------------------ search

    def _sync_limiter(self):
        """
        Count every search slskd knows of toward the limit, and delete our own
        once they are old. Runs before each search; one cheap request.
        """
        try:
            searches = self._json("GET", "/searches") or []
        except SlskdError:
            return
        now = time.time()
        for s in searches:
            started = _parse_time(s.get("startedAt"))
            sid = str(s.get("id") or "")
            if started is None or not sid:
                continue
            if now - started < self.limiter.window:
                self.limiter.note(sid, started)
            elif (sid.startswith(OUR_SEARCH_PREFIX) and s.get("isComplete")
                  and now - started > SEARCH_KEEP):
                try:
                    self._req("DELETE", f"/searches/{sid}")
                except SlskdError:
                    pass

    def search(self, query, should_cancel):
        self._sync_limiter()
        sid = OUR_SEARCH_PREFIX + str(uuid.uuid4())[len(OUR_SEARCH_PREFIX):]
        self.limiter.acquire(sid, should_cancel)
        self._json(
            "POST", "/searches", ok=(200, 201),
            json={
                "id": sid,
                "searchText": query,
                "searchTimeout": SEARCH_IDLE_MS,
                "responseLimit": SEARCH_RESPONSE_LIMIT,
                "fileLimit": 10_000,
            },
        )
        deadline = self.clock() + SEARCH_MAX_WAIT
        while True:
            if should_cancel and should_cancel():
                self._stop_search(sid)
                raise Cancelled()
            state = self._json("GET", f"/searches/{sid}") or {}
            if state.get("isComplete"):
                break
            if self.clock() >= deadline:
                self._stop_search(sid)
                break
            self.sleep(POLL)
        responses = self._json("GET", f"/searches/{sid}/responses") or []
        self._note_result(bool(responses), should_cancel)
        return [self._to_response(r) for r in responses]

    def _note_result(self, any_responses, should_cancel):
        if any_responses:
            _empty_streaks[self.base] = 0
            return
        streak = _empty_streaks.get(self.base, 0) + 1
        _empty_streaks[self.base] = streak
        if streak >= EMPTY_STREAK_RECONNECT:
            _empty_streaks[self.base] = 0
            self.reconnect(should_cancel)

    def reconnect(self, should_cancel=None):
        """Drop and re-establish slskd's server connection; True once logged in again."""
        print("[soulseek] several searches in a row found nothing; reconnecting slskd",
              file=sys.stderr)
        return self.connect(should_cancel)

    def _stop_search(self, sid):
        try:
            self._req("PUT", f"/searches/{sid}")
        except SlskdError:
            pass

    @staticmethod
    def _to_response(r):
        files = []
        for f in r.get("files") or []:
            if f.get("isLocked"):
                continue
            name = f.get("filename") or ""
            ext = (f.get("extension") or "").lower().lstrip(".")
            if not ext:
                base = re.split(r"[\\/]", name)[-1]
                ext = base.rsplit(".", 1)[1].lower() if "." in base[1:] else ""
            files.append(
                match.SharedFile(
                    filename=name,
                    size=int(f.get("size") or 0),
                    ext=ext,
                    bitrate=f.get("bitRate") or None,
                    duration=f.get("length") or None,
                    sample_rate=f.get("sampleRate") or None,
                    bit_depth=f.get("bitDepth") or None,
                )
            )
        return match.SearchResponse(
            user=r.get("username") or "",
            free_slot=bool(r.get("hasFreeUploadSlot")),
            avg_speed=int(r.get("uploadSpeed") or 0),
            queue_len=int(r.get("queueLength") or 0),
            files=files,
        )

    # ------------------------------------------------------------ download

    def download(self, cand, should_cancel):
        user, f = cand.user, cand.file
        dest = f"{DOWNLOAD_SUBDIR}/{uuid.uuid4().hex}"
        resp = self._req(
            "POST", "/transfers/downloads/batches",
            json={
                "username": user,
                "files": [{"filename": f.filename, "size": f.size}],
                "options": {"destination": dest},
            },
            # slskd looks the peer up and contacts it before answering.
            timeout=HTTP_TIMEOUT + 10,
        )
        if resp.status_code == 404:
            raise PeerUnreachable("the peer is offline")
        if resp.status_code not in (200, 201, 207):
            detail = resp.text.strip().strip('"')[:200]
            if "timed out" in detail.lower():
                # Soulseek.NET's fixed 5 s wait for the peer to answer, which
                # slskd does not let us raise. Mostly peers behind a firewall
                # that cannot reach us either; see the port-forwarding notes.
                raise PeerUnreachable("the peer did not answer")
            raise SlskdError(f"slskd could not queue it ({resp.status_code}: {detail})")
        body = resp.json()
        transfers = (body.get("batch") or {}).get("transfers") or []
        if not transfers:
            fails = body.get("failures") or [{}]
            raise SlskdError(f"slskd could not queue it ({fails[0].get('message') or 'no reason given'})")
        tid = transfers[0]["id"]

        try:
            self._wait(user, tid, f.size, should_cancel)
        except BaseException:
            self._cancel(user, tid)
            self._remove_dir(dest)
            raise
        path = self._finished_file(dest, f.size)
        self._transfers[path] = (user, tid)
        return path

    def _wait(self, user, tid, size, should_cancel):
        start = self.clock()
        last_bytes, last_change, started = -1, start, False
        while True:
            if should_cancel and should_cancel():
                raise Cancelled()
            t = self._json("GET", f"/transfers/downloads/{user}/{tid}")
            state = t.get("state") or ""
            done = int(t.get("bytesTransferred") or 0)
            now = self.clock()
            if "Succeeded" in state:
                return
            if state.startswith("Completed"):
                why = state.split(",", 1)[-1].strip().lower() or "failed"
                exc = (t.get("exception") or "").strip()
                raise SlskdError(f"the transfer {why}" + (f" ({exc})" if exc else ""))
            if "InProgress" in state or done > 0:
                if not started:
                    started, last_change = True, now
                if done != last_bytes:
                    last_bytes, last_change = done, now
                elif now - last_change >= STALL_TIMEOUT:
                    pct = int(100 * done / size) if size else 0
                    raise SlskdError(f"the transfer stalled at {pct}% for {STALL_TIMEOUT} s")
            elif now - start >= START_TIMEOUT:
                place = t.get("placeInQueue")
                where = f", place {place} in the peer's queue" if place else ""
                raise SlskdError(
                    f"the transfer did not start within {START_TIMEOUT} s ({state.lower() or 'waiting'}{where})"
                )
            self.sleep(POLL)

    def _finished_file(self, dest, size):
        folder = os.path.join(self.downloads_dir, dest)
        deadline = self.clock() + SETTLE_TIMEOUT
        while True:
            try:
                names = [n for n in os.listdir(folder) if not n.startswith(".")]
            except OSError:
                names = []
            if len(names) == 1:
                path = os.path.join(folder, names[0])
                try:
                    if os.path.getsize(path) == size:
                        return path
                except OSError:
                    pass
            if self.clock() >= deadline:
                self._remove_dir(dest)
                raise SlskdError(
                    f"slskd reported the download finished, but it is not in {folder}"
                )
            self.sleep(POLL)

    def _cancel(self, user, tid):
        """
        Cancel the remote transfer, then clear its record. Best effort.

        slskd cancels asynchronously and ignores a remove sent before the
        transfer has ended, which left cancelled records behind in its list,
        so wait a few seconds for the end first.
        """
        path = f"/transfers/downloads/{user}/{tid}"
        try:
            self._req("DELETE", path, params={"remove": "false"})
            deadline = self.clock() + 5
            while self.clock() < deadline:
                t = self._req("GET", path)
                if t.status_code != 200 or str(t.json().get("state", "")).startswith("Completed"):
                    break
                self.sleep(0.5)
            self._req("DELETE", path, params={"remove": "true"})
        except (SlskdError, ValueError):
            pass

    def _remove_dir(self, dest):
        shutil.rmtree(os.path.join(self.downloads_dir, dest), ignore_errors=True)

    def discard(self, path):
        user_tid = self._transfers.pop(path, None)
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        if user_tid:
            try:
                self._req("DELETE", f"/transfers/downloads/{user_tid[0]}/{user_tid[1]}",
                          params={"remove": "true"})
            except SlskdError:
                pass
