"""
slskd.py against a fake HTTP session. The canned bodies are trimmed copies of
what slskd 0.26.2 actually returned during development, so field names and
state strings here are the real ones, not guesses.
"""

import json
import os
import time

import pytest

import download as core
import slskd
from match import Cand, SharedFile
from slskd import SearchLimiter, SlskdClient, SlskdError
from sources import Cancelled, PeerUnreachable


class Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")
        self.content = self.text.encode()

    def json(self):
        return self._body if self._body is not None else json.loads(self.text)


class FakeSession:
    """Routes (METHOD, path) to a handler; records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kw):
        path = url.split("/api/v0", 1)[1]
        self.calls.append((method, path, kw.get("json"), kw.get("params")))
        for (m, prefix), handler in self.routes.items():
            if m == method and (path == prefix or (prefix.endswith("*") and path.startswith(prefix[:-1]))):
                return handler(path, kw)
        return Resp(404, text="no route")

    def called(self, method, prefix):
        return [c for c in self.calls if c[0] == method and c[1].startswith(prefix)]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(routes, tmp_path, limiter=None):
    clock = Clock()
    session = FakeSession(routes)
    client = SlskdClient(
        "http://slskd:5030", "k" * 32, str(tmp_path),
        limiter=limiter or SearchLimiter(clock=clock, sleep=clock.sleep),
        session=session, clock=clock, sleep=clock.sleep,
    )
    return client, session, clock


# ---------------------------------------------------------------- limiter


def test_limiter_allows_the_limit_then_waits_for_the_oldest_to_age_out():
    clock = Clock()
    lim = SearchLimiter(limit=3, window=100, clock=clock, sleep=clock.sleep)
    for i in range(3):
        lim.acquire(f"s{i}")
        clock.t += 10
    # Searches at 1000, 1010, 1020; now 1030. The next waits for 1000 to age out.
    assert lim.wait_time() == pytest.approx(70)
    lim.acquire("s3")
    assert clock.t >= 1100


def test_limiter_default_stays_under_the_soulseek_ban_threshold():
    # 34 searches in 220 s bans the account. Ours allows at most 30 in any 240 s.
    clock = Clock()
    lim = SearchLimiter(clock=clock, sleep=clock.sleep)
    times = []
    for i in range(80):
        lim.acquire(str(i))
        times.append(clock.t)
    for i, t in enumerate(times):
        assert sum(1 for u in times[i:] if u < t + 220) <= 30


def test_limiter_wait_honours_cancel():
    clock = Clock()
    lim = SearchLimiter(limit=1, window=100, clock=clock, sleep=clock.sleep)
    lim.acquire("a")
    with pytest.raises(Cancelled):
        lim.acquire("b", should_cancel=lambda: clock.t > 1010)


# ---------------------------------------------------------------- search

RESPONSES = [
    {
        "fileCount": 3, "hasFreeUploadSlot": True, "lockedFileCount": 1,
        "queueLength": 0, "token": 0, "uploadSpeed": 3646087, "username": "M3H9X",
        "files": [
            {"bitRate": 320, "code": 1, "extension": "", "filename":
             "@@mfapl\\Music (320)\\Daft Punk\\Discovery\\01 One More Time.mp3",
             "length": 320, "size": 12912942, "isLocked": False},
            {"code": 1, "extension": "", "filename":
             "@@mfapl\\Music\\Daft Punk\\Discovery\\01 One More Time.FLAC",
             "length": 320, "size": 38000000, "sampleRate": 44100, "bitDepth": 16, "isLocked": False},
            {"code": 1, "extension": "", "filename": "@@mfapl\\Music\\cover", "size": 90000, "isLocked": False},
            {"code": 1, "extension": "mp3", "filename": "@@x\\locked.mp3", "size": 1, "isLocked": True},
        ],
        "lockedFiles": [],
    },
    {
        "fileCount": 1, "hasFreeUploadSlot": False, "lockedFileCount": 0,
        "queueLength": 794, "token": 0, "uploadSpeed": 840979, "username": "blitor22",
        "files": [{"bitRate": 128, "code": 1, "extension": "", "filename":
                   "@@emths\\House\\Daft Punk - One More Time (Remix).mp3",
                   "length": 465, "size": 7449325, "isLocked": False}],
    },
]


def search_routes(polls_until_complete=2, searches=None):
    state = {"polls": 0}

    def get_search(path, kw):
        state["polls"] += 1
        done = state["polls"] >= polls_until_complete
        return Resp(200, {"id": path.split("/")[2], "isComplete": done,
                          "state": "Completed, TimedOut" if done else "InProgress"})

    return {
        ("GET", "/searches"): lambda p, kw: Resp(200, searches or []),
        ("POST", "/searches"): lambda p, kw: Resp(200, {"id": kw["json"]["id"], "isComplete": False}),
        ("GET", "/searches/*"): lambda p, kw: (
            Resp(200, RESPONSES) if p.endswith("/responses") else get_search(p, kw)
        ),
        ("PUT", "/searches/*"): lambda p, kw: Resp(200, {}),
        ("DELETE", "/searches/*"): lambda p, kw: Resp(204, text=""),
    }


def test_search_posts_milliseconds_polls_and_maps_responses(tmp_path):
    client, session, _ = make(search_routes(), tmp_path)
    out = client.search("daft punk one more time", None)
    (post,) = session.called("POST", "/searches")
    body = post[2]
    assert body["searchText"] == "daft punk one more time"
    # slskd hands this to Soulseek.NET as milliseconds, whatever its docs say.
    assert body["searchTimeout"] == 10_000
    assert body["id"].startswith(slskd.OUR_SEARCH_PREFIX)
    assert [r.user for r in out] == ["M3H9X", "blitor22"]
    a = out[0]
    assert a.free_slot and a.avg_speed == 3646087 and a.queue_len == 0
    # Locked files are skipped; extensions come from the filename.
    assert [(f.ext, f.bitrate, f.duration) for f in a.files] == [
        ("mp3", 320, 320), ("flac", None, 320), ("", None, None)
    ]
    assert a.files[1].sample_rate == 44100 and a.files[1].bit_depth == 16
    assert out[1].queue_len == 794 and not out[1].free_slot


def test_search_is_stopped_after_the_hard_cap(tmp_path):
    client, session, clock = make(search_routes(polls_until_complete=10**9), tmp_path)
    start = clock.t
    client.search("x", None)
    assert session.called("PUT", "/searches/")
    assert clock.t - start == pytest.approx(slskd.SEARCH_MAX_WAIT, abs=2)


def test_search_cancel_stops_the_remote_search(tmp_path):
    client, session, clock = make(search_routes(polls_until_complete=10**9), tmp_path)
    start = clock.t
    with pytest.raises(Cancelled):
        client.search("x", lambda: clock.t > start + 3)
    assert session.called("PUT", "/searches/")


def test_searches_already_in_slskd_count_and_old_ones_of_ours_are_deleted(tmp_path):
    now = time.time()

    def iso(t):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".1234567Z"

    ours_old = slskd.OUR_SEARCH_PREFIX + "-0000-0000-0000-000000000001"
    existing = [
        {"id": "aaaaaaaa-recent-from-the-ui", "startedAt": iso(now - 30), "isComplete": True},
        {"id": ours_old, "startedAt": iso(now - 7200), "isComplete": True},
        {"id": "bbbbbbbb-old-from-the-ui", "startedAt": iso(now - 7200), "isComplete": True},
    ]
    lim = SearchLimiter()
    client, session, _ = make(search_routes(searches=existing), tmp_path, limiter=lim)
    client.search("x", None)
    assert "aaaaaaaa-recent-from-the-ui" in lim._seen
    deleted = [c[1] for c in session.called("DELETE", "/searches/")]
    assert deleted == [f"/searches/{ours_old}"]


# ---------------------------------------------------------------- download

FILE = SharedFile(
    filename="@@mfapl\\Music (320)\\Daft Punk\\Discovery\\01 One More Time.mp3",
    size=12, ext="mp3", bitrate=320, duration=320,
)
CAND = Cand(id=0, user="M3H9X", file=FILE, free=True, speed=1, queue=0, fit=100, score=1)


def transfer_routes(tmp_path, states, enqueue=None, write_file=True):
    """`states` is a list of (state, bytesTransferred[, placeInQueue]) served in order, the last repeating."""
    seq = {"i": 0, "dest": None, "cancelled": False}

    def post_batch(path, kw):
        if enqueue:
            return enqueue
        seq["dest"] = kw["json"]["options"]["destination"]
        return Resp(201, {"batch": {"id": "b1", "transfers": [
            {"id": "t1", "username": "M3H9X", "filename": FILE.filename, "size": FILE.size,
             "state": "Requested", "bytesTransferred": 0}]}, "failures": []})

    def get_transfer(path, kw):
        # As slskd does: once cancelled, the transfer reports it.
        if seq["cancelled"]:
            return Resp(200, {"id": "t1", "state": "Completed, Cancelled", "bytesTransferred": 0})
        st = states[min(seq["i"], len(states) - 1)]
        seq["i"] += 1
        if "Succeeded" in st[0] and write_file:
            folder = tmp_path / seq["dest"]
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "01 One More Time.mp3").write_bytes(b"x" * FILE.size)
        body = {"id": "t1", "state": st[0], "bytesTransferred": st[1]}
        if len(st) > 2:
            body["placeInQueue"] = st[2]
        return Resp(200, body)

    def delete_transfer(path, kw):
        if (kw.get("params") or {}).get("remove") == "false":
            seq["cancelled"] = True
        return Resp(204, text="")

    routes = {
        ("POST", "/transfers/downloads/batches"): post_batch,
        ("GET", "/transfers/downloads/M3H9X/t1"): get_transfer,
        ("DELETE", "/transfers/downloads/M3H9X/t1"): delete_transfer,
    }
    return routes, seq


def test_download_returns_the_finished_file_and_discard_tidies_up(tmp_path):
    routes, seq = transfer_routes(tmp_path, [
        ("Requested", 0), ("Queued, Remotely", 0), ("InProgress", 4), ("Completed, Succeeded", 12),
    ])
    client, session, _ = make(routes, tmp_path)
    path = client.download(CAND, None)
    assert path == os.path.join(str(tmp_path), seq["dest"], "01 One More Time.mp3")
    (post,) = session.called("POST", "/transfers/downloads/batches")
    assert post[2]["files"] == [{"filename": FILE.filename, "size": 12}]
    assert seq["dest"].startswith("spotify-playlists/")
    client.discard(path)
    assert not os.path.exists(os.path.dirname(path))
    assert [c[3] for c in session.called("DELETE", "/transfers/")] == [{"remove": "true"}]


def test_a_transfer_that_never_starts_is_cancelled_after_the_start_timeout(tmp_path):
    routes, seq = transfer_routes(tmp_path, [("Queued, Remotely", 0, 57739)])
    client, session, clock = make(routes, tmp_path)
    start = clock.t
    with pytest.raises(SlskdError) as err:
        client.download(CAND, None)
    assert str(err.value) == (
        "the transfer did not start within 60 s (queued, remotely, place 57739 in the peer's queue)"
    )
    assert clock.t - start == pytest.approx(slskd.START_TIMEOUT, abs=2)
    # Cancelled remotely first, then cleared from slskd's list.
    assert [c[3] for c in session.called("DELETE", "/transfers/")] == [
        {"remove": "false"}, {"remove": "true"}
    ]


def test_a_stalled_transfer_is_cancelled(tmp_path):
    routes, _ = transfer_routes(tmp_path, [("InProgress", 0), ("InProgress", 5)])
    client, session, _ = make(routes, tmp_path)
    with pytest.raises(SlskdError) as err:
        client.download(CAND, None)
    assert str(err.value) == "the transfer stalled at 41% for 45 s"
    assert session.called("DELETE", "/transfers/")


def test_a_transfer_slskd_ends_badly_says_how(tmp_path):
    routes, _ = transfer_routes(tmp_path, [("Requested", 0), ("Completed, Rejected", 0)])
    client, _, _ = make(routes, tmp_path)
    with pytest.raises(SlskdError, match="^the transfer rejected$"):
        client.download(CAND, None)


def test_cancel_during_a_transfer_cancels_it_remotely(tmp_path):
    routes, seq = transfer_routes(tmp_path, [("InProgress", 1), ("InProgress", 2), ("InProgress", 3)])
    client, session, clock = make(routes, tmp_path)
    start = clock.t
    with pytest.raises(Cancelled):
        client.download(CAND, lambda: clock.t > start + 1.5)
    assert [c[3] for c in session.called("DELETE", "/transfers/")] == [
        {"remove": "false"}, {"remove": "true"}
    ]
    assert not (tmp_path / seq["dest"]).exists() if seq["dest"] else True


@pytest.mark.parametrize("resp, kind, message", [
    (Resp(404, text="User M3H9X appears to be offline"), PeerUnreachable, "the peer is offline"),
    (Resp(500, text='"The wait timed out after 5000 milliseconds"'), PeerUnreachable, "the peer did not answer"),
    (Resp(200, {"batch": {"transfers": []}, "failures": [{"filename": "f", "message": "already in progress"}]}),
     SlskdError, "slskd could not queue it (already in progress)"),
])
def test_enqueue_failures_read_well_in_the_queue(tmp_path, resp, kind, message):
    routes, _ = transfer_routes(tmp_path, [("Requested", 0)], enqueue=resp)
    client, _, _ = make(routes, tmp_path)
    with pytest.raises(kind) as err:
        client.download(CAND, None)
    assert str(err.value) == message


def test_a_run_of_empty_searches_reconnects_slskd(tmp_path, monkeypatch):
    monkeypatch.setattr(slskd, "_empty_streaks", {})
    routes = search_routes()
    routes[("GET", "/searches/*")] = lambda p, kw: (
        Resp(200, []) if p.endswith("/responses") else Resp(200, {"isComplete": True})
    )
    routes[("DELETE", "/server")] = lambda p, kw: Resp(204, text="")
    routes[("PUT", "/server")] = lambda p, kw: Resp(200, text="")
    routes[("GET", "/server")] = lambda p, kw: Resp(200, {"isLoggedIn": True})
    client, session, _ = make(routes, tmp_path)
    for _ in range(slskd.EMPTY_STREAK_RECONNECT - 1):
        client.search("x", None)
    assert not session.called("PUT", "/server")
    client.search("x", None)
    assert len(session.called("DELETE", "/server")) == 1
    assert len(session.called("PUT", "/server")) == 1


def test_one_search_with_results_resets_the_empty_streak(tmp_path, monkeypatch):
    monkeypatch.setattr(slskd, "_empty_streaks", {})
    state = {"empty": True}
    routes = search_routes()
    routes[("GET", "/searches/*")] = lambda p, kw: (
        Resp(200, [] if state["empty"] else RESPONSES) if p.endswith("/responses")
        else Resp(200, {"isComplete": True})
    )
    client, session, _ = make(routes, tmp_path)
    client.search("x", None)
    client.search("x", None)
    state["empty"] = False
    client.search("x", None)
    state["empty"] = True
    client.search("x", None)
    client.search("x", None)
    assert not session.called("PUT", "/server")


def test_succeeded_but_no_file_is_an_error_not_a_hang(tmp_path):
    routes, _ = transfer_routes(tmp_path, [("Completed, Succeeded", 12)], write_file=False)
    client, _, _ = make(routes, tmp_path)
    with pytest.raises(SlskdError, match="not in"):
        client.download(CAND, None)


# ---------------------------------------------------------------- status


def app_routes(logged_in=True, status=200):
    body = {"server": {"isLoggedIn": logged_in, "state": "Connected, LoggedIn" if logged_in else "Disconnected"},
            "user": {"username": "someone"}}
    return {("GET", "/application"): lambda p, kw: Resp(status, body)}


def test_status_connected(tmp_path):
    client, _, _ = make(app_routes(), tmp_path)
    assert client.status() == ("connected", "logged in as someone")
    assert client.check() is None


def test_status_not_logged_in(tmp_path):
    client, _, _ = make(app_routes(logged_in=False), tmp_path)
    assert client.status() == ("logged out", "Disconnected")


def test_status_bad_key(tmp_path):
    client, _, _ = make(app_routes(status=401), tmp_path)
    assert client.status() == (
        "error", "slskd refused the app's key; restart slskd so it rereads its settings"
    )


def test_status_missing_shared_folder(tmp_path):
    client, _, _ = make(app_routes(), tmp_path / "nope")
    state, msg = client.status()
    assert state == "error" and "is not visible here" in msg


def test_status_unreachable(tmp_path):
    import requests

    class Down(FakeSession):
        def request(self, *a, **kw):
            raise requests.ConnectionError("refused")

    client = SlskdClient("http://slskd:5030", "k" * 32, str(tmp_path), session=Down({}))
    assert client.status() == ("error", "slskd is not answering (it may still be starting)")


def test_status_when_the_slskd_name_does_not_resolve(tmp_path):
    import socket

    import requests

    class NoHost(FakeSession):
        def request(self, *a, **kw):
            try:
                raise socket.gaierror(-2, "Name or service not known")
            except socket.gaierror as e:
                raise requests.ConnectionError("Max retries exceeded") from e

    client = SlskdClient("http://slskd:5030", "k" * 32, str(tmp_path), session=NoHost({}))
    state, msg = client.status()
    assert state == "not installed" and "name slskd does not resolve" in msg


def server_routes(states):
    """GET /server serves `states` in turn, the last repeating."""
    seq = {"i": 0}

    def get_server(p, kw):
        st = states[min(seq["i"], len(states) - 1)]
        seq["i"] += 1
        return Resp(200, st)

    return {
        ("DELETE", "/server"): lambda p, kw: Resp(204, text=""),
        ("PUT", "/server"): lambda p, kw: Resp(200, text=""),
        ("GET", "/server"): get_server,
    }


def test_connect_waits_for_the_login(tmp_path):
    client, session, _ = make(server_routes([
        {"state": "Connecting", "isConnecting": True},
        {"state": "Connected, LoggingIn", "isLoggingIn": True},
        {"state": "Connected, LoggedIn", "isLoggedIn": True},
    ]), tmp_path)
    assert client.connect() is True
    assert [c[0] for c in session.calls[:2]] == ["DELETE", "PUT"]


def test_connect_reports_a_refused_login(tmp_path):
    # slskd answers the connect call with 200 even for a wrong password; the
    # refusal only shows as the state settling on a plain Disconnected.
    client, _, clock = make(server_routes([
        {"state": "Connecting", "isConnecting": True},
        {"state": "Disconnected"},
    ]), tmp_path)
    start = clock.t
    assert client.connect() is False
    assert clock.t - start < 5


def test_wait_for_options_polls_until_slskd_has_the_setting(tmp_path):
    seq = {"i": 0}

    def get_options(p, kw):
        seq["i"] += 1
        return Resp(200, {"soulseek": {"username": "someone" if seq["i"] >= 3 else None}})

    client, _, _ = make({("GET", "/options"): get_options}, tmp_path)
    assert client.wait_for_options(lambda o: o["soulseek"]["username"] == "someone")
    assert seq["i"] == 3


def test_wait_for_options_gives_up(tmp_path):
    client, _, _ = make({("GET", "/options"): lambda p, kw: Resp(200, {})}, tmp_path)
    assert client.wait_for_options(lambda o: False, wait=3) is False


def test_status_not_configured():
    assert SlskdClient("", "", "/x").status()[0] == "not configured"


def test_default_sources_puts_soulseek_first_only_when_configured():
    assert [s.name for s in core.default_sources({})] == ["youtube"]
    env = {"SLSKD_URL": "http://slskd:5030", "SLSKD_API_KEY": "k" * 32}
    assert [s.name for s in core.default_sources(env)] == ["soulseek", "youtube"]
