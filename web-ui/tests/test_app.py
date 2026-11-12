"""
app.py: onboarding, the Soulseek account and sharing endpoints, the source
order, the sources block in /api/state, and the source tag on queue rows.
No network: slskd's client is replaced by a fake that records its calls.
"""

import json
import os
import tempfile

import pytest

# app.py reads these at import time and creates its folders under MUSIC_DIR.
_MUSIC = tempfile.mkdtemp(prefix="music-")
os.environ["MUSIC_DIR"] = _MUSIC
os.environ.pop("SLSKD_URL", None)
os.environ.pop("APP_PASSWORD", None)

import app as webapp  # noqa: E402


class FakeSlskd:
    """Stands in for slskd.SlskdClient's account-level calls."""

    name, label = "soulseek", "Soulseek"
    state = ("logged out", "Disconnected")
    login_ok = True
    calls = []

    def __init__(self, *a, **kw):
        pass

    def status(self, timeout=None):
        FakeSlskd.calls.append("status")
        return FakeSlskd.state

    def wait_for_options(self, ready, wait=15):
        FakeSlskd.calls.append("wait_for_options")
        return True

    def connect(self, should_cancel=None, wait=None):
        FakeSlskd.calls.append("connect")
        if FakeSlskd.login_ok:
            FakeSlskd.state = ("connected", "logged in as someone")
        return FakeSlskd.login_ok

    def disconnect(self):
        FakeSlskd.calls.append("disconnect")
        FakeSlskd.state = ("logged out", "Disconnected")

    def rescan_shares(self):
        FakeSlskd.calls.append("rescan")
        return True


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    for path in (webapp.SETTINGS_PATH, webapp.LEGACY_ORDER_PATH, webapp.slsk_settings.path):
        try:
            os.remove(path)
        except OSError:
            pass
    webapp.slsk_settings.ensure()
    webapp.invalidate_status()
    FakeSlskd.state = ("logged out", "Disconnected")
    FakeSlskd.login_ok = True
    FakeSlskd.calls = []
    monkeypatch.setattr(webapp.slskd, "SlskdClient", FakeSlskd)
    yield


@pytest.fixture
def client():
    return webapp.app.test_client()


def actions():
    """What the app asked slskd to do, leaving out status checks."""
    return [c for c in FakeSlskd.calls if c != "status"]


def state(client):
    return client.get("/api/state").get_json()


def soulseek(client):
    return next(i for i in state(client)["sources"]["items"] if i["name"] == "soulseek")


# ---------------------------------------------------------------- settings files


def test_slskd_settings_get_an_api_key_and_share_by_default():
    s = webapp.slsk_settings.read()
    assert len(s["api_key"]) >= 16
    assert s["share"] is True
    assert s["username"] == ""


def test_default_order_and_not_yet_onboarded(client):
    d = state(client)
    assert d["sources"]["order"] == ["soulseek", "youtube"]
    assert d["setup"] == {"onboarded": False}


def test_order_saved_before_onboarding_existed_is_still_read():
    with open(webapp.LEGACY_ORDER_PATH, "w") as fh:
        json.dump({"order": ["youtube", "soulseek"]}, fh)
    assert webapp.load_source_order() == ["youtube", "soulseek"]


def test_broken_settings_fall_back_to_defaults():
    with open(webapp.SETTINGS_PATH, "w") as fh:
        fh.write("{not json")
    assert webapp.load_settings() == {"order": ["soulseek", "youtube"], "onboarded": False}


def test_unknown_names_in_the_order_are_dropped():
    with open(webapp.SETTINGS_PATH, "w") as fh:
        json.dump({"order": ["youtube", "directconnect"]}, fh)
    assert webapp.load_source_order() == ["youtube", "soulseek"]


# ---------------------------------------------------------------- status


def test_soulseek_without_an_account_is_not_set_up(client):
    item = soulseek(client)
    assert (item["state"], item["message"]) == ("not set up", "No Soulseek account yet")
    assert item["account"] == {"username": "", "share": True}


def test_soulseek_commented_out_of_compose_is_not_installed(client):
    FakeSlskd.state = ("not installed", "slskd is not running: its service is commented out of docker-compose.yml")
    assert soulseek(client)["state"] == "not installed"


def test_a_refused_login_reads_as_an_error(client):
    webapp.slsk_settings.update(username="someone", password="wrong")
    FakeSlskd.state = ("logged out", "Disconnected")
    item = soulseek(client)
    assert item["state"] == "error"
    assert item["message"].startswith("Soulseek refused the login for someone")


def test_status_is_cached_between_polls(client):
    for _ in range(5):
        client.get("/api/state")
    assert FakeSlskd.calls.count("status") == 1


# ---------------------------------------------------------------- account


def test_logging_in_saves_the_account_connects_and_starts_sharing(client):
    r = client.post("/api/soulseek/account", data={"username": "someone", "password": "pw", "share": "1"})
    assert r.status_code == 200, r.get_json()
    s = webapp.slsk_settings.read()
    assert (s["username"], s["password"], s["share"]) == ("someone", "pw", True)
    assert actions() == ["wait_for_options", "connect", "rescan"]
    assert soulseek(client)["state"] == "connected"


def test_a_refused_login_is_reported(client):
    FakeSlskd.login_ok = False
    r = client.post("/api/soulseek/account", data={"username": "taken", "password": "pw"})
    assert r.status_code == 400
    assert "Soulseek refused the login for taken" in r.get_json()["error"]


def test_no_account_is_saved_when_slskd_is_not_installed(client):
    FakeSlskd.state = ("not installed", "slskd is not running")
    r = client.post("/api/soulseek/account", data={"username": "someone", "password": "pw"})
    assert r.status_code == 409
    assert webapp.slsk_settings.read()["username"] == ""


@pytest.mark.parametrize("form, message", [
    ({"username": "", "password": "pw"}, "Choose a Soulseek username."),
    ({"username": "two words", "password": "pw"}, "no spaces"),
    ({"username": "newname", "password": ""}, "Enter a password"),
])
def test_account_form_is_checked(client, form, message):
    r = client.post("/api/soulseek/account", data=form)
    assert r.status_code == 400 and message in r.get_json()["error"]


def test_a_blank_password_keeps_the_saved_one_for_the_same_name(client):
    webapp.slsk_settings.update(username="someone", password="secret")
    r = client.post("/api/soulseek/account", data={"username": "someone", "password": "", "share": "0"})
    assert r.status_code == 200
    s = webapp.slsk_settings.read()
    assert (s["password"], s["share"]) == ("secret", False)


def test_removing_the_account_logs_out(client):
    webapp.slsk_settings.update(username="someone", password="pw")
    r = client.post("/api/soulseek/remove")
    assert r.status_code == 200
    assert webapp.slsk_settings.read()["username"] == ""
    assert webapp.slsk_settings.read()["password"] == ""
    assert "disconnect" in FakeSlskd.calls


def test_turning_sharing_off_rescans(client):
    r = client.post("/api/soulseek/share", data={"share": "0"})
    assert r.status_code == 200
    assert webapp.slsk_settings.read()["share"] is False
    assert actions() == ["wait_for_options", "rescan"]


# ---------------------------------------------------------------- onboarding and order


def test_finishing_setup_with_youtube_first(client):
    r = client.post("/api/setup/done", data={"first": "youtube"})
    assert r.status_code == 200
    d = state(client)
    assert d["setup"] == {"onboarded": True}
    assert d["sources"]["order"] == ["youtube", "soulseek"]


def test_finishing_setup_needs_a_known_source(client):
    assert client.post("/api/setup/done", data={"first": "napster"}).status_code == 400


def test_setting_the_order(client):
    r = client.post("/api/sources/order", data={"order": "youtube,soulseek"})
    assert r.status_code == 200
    assert webapp.load_source_order() == ["youtube", "soulseek"]


@pytest.mark.parametrize("bad", ["youtube", "youtube,youtube", "youtube,soulseek,other", ""])
def test_an_order_must_name_every_source_once(client, bad):
    assert client.post("/api/sources/order", data={"order": bad}).status_code == 400
    assert webapp.load_source_order() == ["soulseek", "youtube"]


def test_jobs_use_soulseek_only_once_an_account_exists():
    assert [s.name for s in webapp.ordered_sources()] == ["youtube"]
    webapp.slsk_settings.update(username="someone", password="pw")
    assert [s.name for s in webapp.ordered_sources()] == ["soulseek", "youtube"]
    webapp.save_source_order(["youtube", "soulseek"])
    assert [s.name for s in webapp.ordered_sources()] == ["youtube", "soulseek"]


def test_a_finished_job_with_downloads_rescans_the_share():
    webapp.slsk_settings.update(username="someone", password="pw")
    webapp.rescan_share()
    assert FakeSlskd.calls == ["rescan"]
    FakeSlskd.calls = []
    webapp.slsk_settings.update(share=False)
    webapp.rescan_share()
    assert FakeSlskd.calls == []


# ---------------------------------------------------------------- queue rows


def test_queue_rows_carry_their_source_and_jobs_count_by_source():
    j = webapp.Job("/x/mix.csv", 192)
    j._handle({
        "type": "track_done", "query": "Daft Punk - One More Time", "source": "soulseek",
        "source_name": "Soulseek", "peer": "peer jellybeanz",
        "source_label": "mp3 225 kbps, encoded to 192 kbps", "attempts": [],
    })
    j._handle({
        "type": "track_done", "query": "Enya - Orinoco Flow", "source": "youtube",
        "source_name": "YouTube", "source_label": "opus 127 kbps (audio-only, format 251)",
        "used_query": "Enya - Orinoco Flow",
        "attempts": [{"source": "soulseek", "label": "Soulseek", "error": "no file matched"}],
    })
    ok = [e for e in j.events if e["kind"] == "ok"]
    assert [(e["source"], e["detail"]) for e in ok] == [
        ("Soulseek", "peer jellybeanz: mp3 225 kbps, encoded to 192 kbps"),
        ("YouTube", "opus 127 kbps (audio-only, format 251) - Soulseek missed: no file matched"),
    ]
    assert j.snapshot()["by_source"] == {"Soulseek": 1, "YouTube": 1}


# ---------------------------------------------------------------- pasted links

import spotify_link  # noqa: E402


def collection(kind, name="Random Access Memories", artist="Daft Punk", n=2, total=None):
    rows = [{"Track Name": f"Song {i}", "Artist Name(s)": artist, "Track Number": str(i),
             "Track Duration (ms)": "200000"} for i in range(1, n + 1)]
    cols = list(spotify_link.CSV_COLUMNS)
    if kind == "playlist":
        cols = [c for c in cols if c != "Track Number"]
    return spotify_link.Collection(kind, "abcdefghijklmnopqrstuv", name, artist, rows,
                                   total or n, n, columns=cols, image="img")


@pytest.fixture
def links(monkeypatch):
    """Serves a Collection per link kind and counts the reads."""
    served = {"reads": 0, "col": None}

    def read(link, session=None):
        served["reads"] += 1
        return served["col"]

    monkeypatch.setattr(webapp.spotify_link, "read", read)
    webapp._link_cache.clear()
    return served


ALBUM_URL = "https://open.spotify.com/album/abcdefghijklmnopqrstuv"


def test_a_link_preview_says_what_and_where(client, links):
    links["col"] = collection("album")
    j = client.post("/api/link/preview", data={"link": ALBUM_URL}).get_json()
    assert (j["kind"], j["name"], j["artist"], j["tracks"], j["truncated"]) == (
        "album", "Random Access Memories", "Daft Punk", 2, False)
    assert j["folder"] == "Daft Punk/Random Access Memories"
    assert j["sample"][0] == {"artist": "Daft Punk", "title": "Song 1", "ms": 200000}


def test_a_bad_link_is_a_400_with_the_reason(client, links):
    r = client.post("/api/link/preview", data={"link": "https://open.spotify.com/artist/abcdefghijklmnopqrstuv"})
    assert r.status_code == 400 and "artist" in r.get_json()["error"]
    assert links["reads"] == 0


def test_downloading_an_album_reuses_the_preview_and_files_it_under_the_artist(client, links, monkeypatch):
    added = []
    monkeypatch.setattr(webapp.queue, "add", lambda job: added.append(job) or 1)
    links["col"] = collection("album")
    client.post("/api/link/preview", data={"link": ALBUM_URL})
    r = client.post("/api/link/download", data={"link": ALBUM_URL + "?si=x", "bitrate": "320", "format": "mp3"})
    assert r.status_code == 200, r.get_json()
    assert links["reads"] == 1
    (job,) = added
    folder = os.path.join(_MUSIC, "Daft Punk", "Random Access Memories")
    assert job.csv_path == os.path.join(folder, "Random Access Memories.csv")
    assert (job.target_dir, job.write_playlist, job.bitrate) == (folder, True, 320)
    assert job.name == "Daft Punk \u2013 Random Access Memories"
    assert os.path.isfile(job.csv_path)
    # It can be run again from the saved list.
    assert "Daft Punk/Random Access Memories" in webapp.list_playlists()


def test_a_song_goes_in_the_artist_folder_without_a_playlist_file(links):
    links["col"] = collection("track", name="Ya Hit", artist="Future", n=1)
    t = webapp.link_target(links["col"])
    assert t["output_dir"] == os.path.join(_MUSIC, "Future")
    assert t["write_playlist"] is False
    # Its CSV stays out of the music library.
    assert t["csv"].startswith(webapp.LINKS_DIR)


def test_a_playlist_link_goes_where_an_account_playlist_would(links):
    t = webapp.link_target(collection("playlist", name="Mañanas: alegre", artist="Spotify"))
    assert t["folder"] == "Mañanas alegre"
    assert t["csv"] == webapp.playlist_csv_path("Mañanas alegre")


def test_saved_list_finds_playlists_and_albums_but_not_plain_folders():
    for rel in ("Mix", "Artist/Album"):
        path = webapp.playlist_csv_path(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
    os.makedirs(os.path.join(_MUSIC, "Artist", "Loose"), exist_ok=True)
    found = webapp.list_playlists()
    assert "Mix" in found and "Artist/Album" in found
    assert "Artist" not in found and "Artist/Loose" not in found
