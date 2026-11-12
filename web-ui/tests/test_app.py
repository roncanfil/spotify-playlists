"""
app.py: the saved source order, the sources block in /api/state, and the
source tag on queue rows. No network: Soulseek is left unconfigured or its
client is faked.
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


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    try:
        os.remove(webapp.SOURCE_ORDER_PATH)
    except OSError:
        pass
    webapp._status_cache.update(at=0.0, items=None)
    yield


@pytest.fixture
def client():
    return webapp.app.test_client()


def test_default_order_puts_soulseek_first():
    assert webapp.load_source_order() == ["soulseek", "youtube"]


def test_saved_order_is_cleaned_of_unknown_and_missing_names():
    with open(webapp.SOURCE_ORDER_PATH, "w") as fh:
        json.dump({"order": ["youtube", "directconnect"]}, fh)
    assert webapp.load_source_order() == ["youtube", "soulseek"]


def test_broken_order_file_falls_back_to_default():
    with open(webapp.SOURCE_ORDER_PATH, "w") as fh:
        fh.write("{not json")
    assert webapp.load_source_order() == ["soulseek", "youtube"]


def test_state_reports_each_source(client):
    d = client.get("/api/state").get_json()["sources"]
    assert d["order"] == ["soulseek", "youtube"]
    slsk, yt = d["items"]
    assert (slsk["name"], slsk["state"]) == ("soulseek", "not configured")
    assert (yt["name"], yt["state"]) == ("youtube", "connected")
    assert yt["message"].startswith("yt-dlp ")
    assert "MP3 output only" in slsk["note"]


def test_state_shows_a_soulseek_error(client, monkeypatch):
    monkeypatch.setattr(
        webapp.slskd.SlskdClient, "status",
        lambda self, timeout=None: ("error", "could not reach slskd (ConnectionError)"),
    )
    item = client.get("/api/state").get_json()["sources"]["items"][0]
    assert item == {
        "name": "soulseek", "label": "Soulseek", "state": "error",
        "message": "could not reach slskd (ConnectionError)",
        "note": webapp.SOURCE_NOTES["soulseek"],
    }


def test_status_is_cached_between_polls(client, monkeypatch):
    calls = []

    def status(self, timeout=None):
        calls.append(1)
        return "connected", "logged in as someone"

    monkeypatch.setattr(webapp.slskd.SlskdClient, "status", status)
    for _ in range(5):
        client.get("/api/state")
    assert len(calls) == 1


def test_setting_the_order(client):
    r = client.post("/api/sources/order", data={"order": "youtube,soulseek"})
    assert r.status_code == 200
    assert r.get_json()["sources"]["order"] == ["youtube", "soulseek"]
    assert webapp.load_source_order() == ["youtube", "soulseek"]


@pytest.mark.parametrize("bad", ["youtube", "youtube,youtube", "youtube,soulseek,other", ""])
def test_an_order_must_name_every_source_once(client, bad):
    r = client.post("/api/sources/order", data={"order": bad})
    assert r.status_code == 400
    assert webapp.load_source_order() == ["soulseek", "youtube"]


def test_ordered_sources_follow_the_setting(monkeypatch):
    monkeypatch.setenv("SLSKD_URL", "http://slskd:5030")
    assert [s.name for s in webapp.ordered_sources()] == ["soulseek", "youtube"]
    webapp.save_source_order(["youtube", "soulseek"])
    assert [s.name for s in webapp.ordered_sources()] == ["youtube", "soulseek"]


def test_ordered_sources_without_soulseek_is_youtube_alone():
    webapp.save_source_order(["soulseek", "youtube"])
    assert [s.name for s in webapp.ordered_sources()] == ["youtube"]


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


def test_a_youtube_only_row_reads_exactly_as_before():
    j = webapp.Job("/x/mix.csv", 192)
    j._handle({
        "type": "track_done", "query": "Enya - Orinoco Flow", "source": "youtube",
        "source_name": "YouTube", "source_label": "opus 127 kbps (audio-only, format 251)",
        "used_query": "Enya - Orinoco Flow", "attempts": [],
    })
    assert j.events[-1]["detail"] == "opus 127 kbps (audio-only, format 251)"
