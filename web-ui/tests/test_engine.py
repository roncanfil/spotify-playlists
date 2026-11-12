"""
process_playlist(): walking the source list, falling through on failure,
tagging whatever arrives, and the YouTube path with yt-dlp faked out.
"""

import csv
import os
import shutil

import pytest

import download as core
from sources import Cancelled

from conftest import needs_ffmpeg

HEADER = [
    "Track Name", "Artist Name(s)", "Album Name", "Album Release Date",
    "Track Number", "Track Duration (ms)",
]


def write_csv(path, rows, header=HEADER):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return str(path)


@pytest.fixture
def playlist(tmp_path):
    return write_csv(
        tmp_path / "mix.csv",
        [["Orinoco Flow", "Enya", "Watermark", "1988-09-19", "2", "30000"]],
    )


class Fail:
    formats = ("mp3",)

    def __init__(self, name, label, error):
        self.name, self.label, self.error = name, label, error
        self.calls = []

    def check(self):
        return None

    def fetch(self, req):
        self.calls.append(req)
        raise RuntimeError(self.error)


class Deliver:
    """Copies a fixture to the target, as a real source would leave it."""

    def __init__(self, name, label, src, formats=("mp3", "m4a")):
        self.name, self.label, self.src, self.formats = name, label, src, formats
        self.calls = []

    def check(self):
        return None

    def fetch(self, req):
        self.calls.append(req)
        shutil.copy(self.src, req.target_path)
        return {
            "path": req.target_path,
            "source_label": "mp3 192 kbps, kept as is",
            "comment": f"Source: {self.label}, peer someone",
            "peer": "peer someone",
        }


def run(csv_path, tmp_path, sources, audio_format="mp3", should_cancel=None):
    return list(
        core.process_playlist(
            csv_path, 192, audio_format, output_root=str(tmp_path / "music"),
            should_cancel=should_cancel, sources=sources,
        )
    )


def of(events, kind):
    return [e for e in events if e["type"] == kind]


@needs_ffmpeg
def test_falls_through_to_the_next_source_and_records_why(playlist, tmp_path, audio):
    slsk = Fail("soulseek", "Soulseek", "no file matched 'enya orinoco flow'")
    yt = Deliver("youtube", "YouTube", audio["mp3_192"])
    events = run(playlist, tmp_path, [slsk, yt])
    (done,) = of(events, "track_done")
    assert done["source"] == "youtube" and done["source_name"] == "YouTube"
    assert done["attempts"] == [
        {"source": "soulseek", "label": "Soulseek", "error": "no file matched 'enya orinoco flow'"}
    ]
    assert "comment" not in done
    # The duration reached the sources, in seconds.
    assert slsk.calls[0].track.duration == 30
    assert of(events, "summary")[0]["downloaded"] == 1


@needs_ffmpeg
def test_the_first_source_that_delivers_wins(playlist, tmp_path, audio):
    slsk = Deliver("soulseek", "Soulseek", audio["mp3_192"])
    yt = Fail("youtube", "YouTube", "should not be reached")
    events = run(playlist, tmp_path, [slsk, yt])
    (done,) = of(events, "track_done")
    assert done["source"] == "soulseek" and done["attempts"] == []
    assert yt.calls == []
    from mutagen.id3 import ID3

    tags = ID3(done["path"])
    assert str(tags["COMM::eng"]) == "Source: Soulseek, peer someone"
    assert str(tags["TIT2"]) == "Orinoco Flow"


def test_every_source_failing_lists_each_reason(playlist, tmp_path):
    events = run(playlist, tmp_path, [
        Fail("soulseek", "Soulseek", "tried 2 files, none usable"),
        Fail("youtube", "YouTube", "HTTP Error 403"),
    ])
    (err,) = of(events, "track_error")
    assert err["error"] == "Soulseek: tried 2 files, none usable; YouTube: HTTP Error 403"
    assert [a["source"] for a in err["attempts"]] == ["soulseek", "youtube"]


def test_a_single_source_keeps_its_own_message(playlist, tmp_path):
    # With no P2P configured the queue must read exactly as it did before.
    events = run(playlist, tmp_path, [Fail("youtube", "YouTube", "No YouTube result for 'x'")])
    assert of(events, "track_error")[0]["error"] == "No YouTube result for 'x'"


def test_m4a_skips_sources_that_only_make_mp3(playlist, tmp_path, audio):
    slsk = Fail("soulseek", "Soulseek", "should not be asked")
    yt = Deliver("youtube", "YouTube", audio["mp3_192"])
    # The fixture is an MP3 under an .m4a name, which is enough to prove the
    # walk; tagging it as MP4 fails, which is a separate concern.
    run(playlist, tmp_path, [slsk, yt], audio_format="m4a")
    assert slsk.calls == []
    assert len(yt.calls) == 1


@needs_ffmpeg
def test_a_source_that_cannot_work_is_skipped_with_one_warning(tmp_path, audio):
    rows = [["Orinoco Flow", "Enya", "W", "1988", str(i), "30000"] for i in (1, 2)]
    csv_path = write_csv(tmp_path / "two.csv", rows)

    class LoggedOut(Fail):
        def check(self):
            return "slskd is not logged in to Soulseek"

    slsk = LoggedOut("soulseek", "Soulseek", "should not be asked")
    events = run(csv_path, tmp_path, [slsk, Deliver("youtube", "YouTube", audio["mp3_192"])])
    warnings = [e["message"] for e in of(events, "warning") if "column" not in e["message"]]
    assert warnings == [
        "Soulseek skipped for this playlist: slskd is not logged in to Soulseek"
    ]
    assert slsk.calls == []
    assert len(of(events, "track_done")) == 2


def test_cancel_inside_a_source_stops_the_job(tmp_path):
    rows = [["Orinoco Flow", "Enya", "W", "1988", str(i), "30000"] for i in (1, 2)]
    csv_path = write_csv(tmp_path / "two.csv", rows)

    class Waits(Fail):
        def fetch(self, req):
            self.calls.append(req)
            raise Cancelled()

    slsk = Waits("soulseek", "Soulseek", "")
    yt = Fail("youtube", "YouTube", "should not be reached")
    events = run(csv_path, tmp_path, [slsk, yt])
    assert [e["type"] for e in events if e["type"] not in ("init", "warning")] == [
        "track_start", "cancelled", "summary"
    ]
    assert yt.calls == [] and len(slsk.calls) == 1
    assert of(events, "summary")[0]["failed"] == 0


def test_missing_duration_column_warns_only_when_a_source_needs_it(tmp_path):
    csv_path = write_csv(
        tmp_path / "nodur.csv", [["Orinoco Flow", "Enya"]], header=["Track Name", "Artist Name(s)"]
    )
    yt = Fail("youtube", "YouTube", "x")
    before = [e["message"] for e in of(run(csv_path, tmp_path, [yt]), "warning")]
    slsk = Fail("soulseek", "Soulseek", "x")
    slsk.uses_duration = True
    after = [e["message"] for e in of(run(csv_path, tmp_path, [slsk, yt]), "warning")]
    assert after == before + [
        "No duration column found; files from Soulseek cannot be checked "
        "against the track's length."
    ]


def test_both_duration_column_names_are_recognised():
    cols, _ = core.resolve_columns(["Track Name", "Artist Name(s)", "Track Duration (ms)"])
    assert cols["duration"] == "Track Duration (ms)"
    cols, _ = core.resolve_columns(["Track Name", "Artist Name(s)", "Duration (ms)"])
    assert cols["duration"] == "Duration (ms)"
    cols, _ = core.resolve_columns(["Track Name", "Artist Name(s)"])
    assert cols["duration"] is None


def test_default_sources_is_youtube_alone_with_no_p2p_configured():
    assert [s.name for s in core.default_sources()] == ["youtube"]


# ---------------------------------------------------------------- YouTube


@pytest.fixture(autouse=True)
def default_results():
    FakeYDL.results = [("Enya - Orinoco Flow", 30, "abc123")]


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: "downloads" a fixture to outtmpl."""

    fixture = None
    searches = []
    # What a flat search lists: (title, seconds, video id).
    results = [("Enya - Orinoco Flow", 30, "abc123")]

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        FakeYDL.searches.append(url)
        if not download:
            return {"entries": [
                {"title": t, "duration": d, "url": f"https://www.youtube.com/watch?v={i}"}
                for t, d, i in FakeYDL.results
            ]}
        # With no postprocessor (MP3 output) yt-dlp leaves YouTube's own audio,
        # here Opus in WebM; the app encodes it.
        ext = "m4a" if self.opts["postprocessors"] else "webm"
        out = self.opts["outtmpl"].replace("%(ext)s", ext)
        shutil.copy(FakeYDL.fixture, out)
        return {"entries": [{
            "webpage_url": "https://www.youtube.com/watch?v=abc123",
            "requested_downloads": [{"format_id": "251", "acodec": "opus", "vcodec": "none",
                                     "abr": 130, "filepath": out}],
        }]}


@needs_ffmpeg
def test_youtube_track_is_tagged_and_reported(playlist, tmp_path, audio, monkeypatch):
    FakeYDL.fixture, FakeYDL.searches = audio["opus_webm"], []
    monkeypatch.setattr(core.yt_dlp, "YoutubeDL", FakeYDL)
    events = run(playlist, tmp_path, None)
    (done,) = of(events, "track_done")
    # Spotify's length is known, so the video is picked from a short search
    # list and then downloaded by its address.
    assert FakeYDL.searches == [
        "ytsearch8:Enya - Orinoco Flow", "https://www.youtube.com/watch?v=abc123",
    ]
    assert {k: done[k] for k in ("youtube_url", "source_label", "is_muxed", "used_fallback", "used_query")} == {
        "youtube_url": "https://www.youtube.com/watch?v=abc123",
        "source_label": "opus 130 kbps (audio-only, format 251), encoded to 160 kbps",
        "is_muxed": False,
        "used_fallback": False,
        "used_query": "Enya - Orinoco Flow",
    }
    assert done["source"] == "youtube" and done["attempts"] == []
    from mutagen.id3 import ID3

    assert str(ID3(done["path"])["COMM::eng"]) == "Original YouTube URL: https://www.youtube.com/watch?v=abc123"


@needs_ffmpeg
@pytest.mark.parametrize("setting, expected", [(128, 128), (192, 160), (320, 160)])
def test_youtube_is_never_encoded_above_its_source(playlist, tmp_path, audio, monkeypatch, setting, expected):
    # ~130 kbps Opus: the setting is a ceiling, and 160 is the smallest MP3
    # step that holds it with MP3's headroom, so 192 and 320 both give 160.
    FakeYDL.fixture, FakeYDL.searches = audio["opus_webm"], []
    monkeypatch.setattr(core.yt_dlp, "YoutubeDL", FakeYDL)
    events = list(core.process_playlist(
        playlist, setting, "mp3", output_root=str(tmp_path / "music"), sources=None,
    ))
    (done,) = of(events, "track_done")
    from mutagen.mp3 import MP3

    assert round(MP3(done["path"]).info.bitrate / 1000) == expected
    # YouTube's own audio file is not left behind.
    assert sorted(os.listdir(os.path.dirname(done["path"]))) == [
        "Enya - 02 - Orinoco Flow.mp3", "mix.m3u",
    ]


@needs_ffmpeg
def test_youtube_passes_over_results_of_the_wrong_length_or_version(playlist, tmp_path, audio, monkeypatch):
    FakeYDL.fixture, FakeYDL.searches = audio["opus_webm"], []
    FakeYDL.results = [
        ("Enya - Orinoco Flow (Official Video)", 75, "video"),
        ("Enya - Orinoco Flow (Live)", 31, "live"),
        ("Orinoco Flow", 29, "topic"),
    ]
    monkeypatch.setattr(core.yt_dlp, "YoutubeDL", FakeYDL)
    (done,) = of(run(playlist, tmp_path, None), "track_done")
    assert FakeYDL.searches[-1] == "https://www.youtube.com/watch?v=topic"
    assert done["youtube_url"] == "https://www.youtube.com/watch?v=abc123"


def test_youtube_with_no_result_of_the_right_length_fails_and_says_why(playlist, tmp_path, monkeypatch):
    FakeYDL.searches = []
    FakeYDL.results = [("Enya - Orinoco Flow (Official Video)", 75, "video")]
    monkeypatch.setattr(core.yt_dlp, "YoutubeDL", FakeYDL)
    (err,) = of(run(playlist, tmp_path, None), "track_error")
    assert "Spotify says 0:30, the closest is 1:15" in err["error"]
    # Nothing was downloaded.
    assert FakeYDL.searches == ["ytsearch8:Enya - Orinoco Flow"]


@needs_ffmpeg
def test_youtube_file_of_the_wrong_length_is_deleted(playlist, tmp_path, audio, monkeypatch):
    # The search listed the right length, but the file is 60 s.
    FakeYDL.fixture, FakeYDL.searches = audio["long_mp3"], []
    monkeypatch.setattr(core.yt_dlp, "YoutubeDL", FakeYDL)
    (err,) = of(run(playlist, tmp_path, None), "track_error")
    assert "wrong recording" in err["error"] and "1:00 long" in err["error"]
    assert [f for f in os.listdir(tmp_path / "music" / "mix") if not f.endswith(".m3u")] == []


def test_youtube_without_a_length_takes_the_first_result(tmp_path, audio, monkeypatch):
    if not os.path.exists(audio["opus_webm"]):
        pytest.skip("ffmpeg not on PATH")
    csv_path = write_csv(
        tmp_path / "mix.csv",
        [["Orinoco Flow", "Enya", "Watermark", "1988-09-19", "2", ""]],
    )
    FakeYDL.fixture, FakeYDL.searches = audio["opus_webm"], []
    monkeypatch.setattr(core.yt_dlp, "YoutubeDL", FakeYDL)
    (done,) = of(run(csv_path, tmp_path, None), "track_done")
    assert FakeYDL.searches == ["ytsearch1:Enya - Orinoco Flow"]


@needs_ffmpeg
def test_a_job_can_name_its_folder_and_skip_the_playlist_file(playlist, tmp_path, audio, monkeypatch):
    FakeYDL.fixture, FakeYDL.searches = audio["opus_webm"], []
    monkeypatch.setattr(core.yt_dlp, "YoutubeDL", FakeYDL)
    out = tmp_path / "music" / "Enya"
    events = list(core.process_playlist(
        playlist, 192, "mp3", output_root=str(tmp_path / "unused"), sources=None,
        output_dir=str(out), write_playlist=False,
    ))
    (done,) = of(events, "track_done")
    assert os.path.dirname(done["path"]) == str(out)
    assert os.listdir(out) == ["Enya - 02 - Orinoco Flow.mp3"]
    assert of(events, "summary")[0]["playlist_file"] is None
