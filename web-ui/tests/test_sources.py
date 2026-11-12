"""
sources.py: the conversion rule, the length check, and P2PSource's candidate
loop driven by a fake client that returns canned search results and hands
back real fixture files.
"""

import os
import shutil

import pytest

import match
import sources
from match import SearchResponse, SharedFile, Track
from sources import (
    Cancelled, P2PClient, P2PSource, PeerUnreachable, Rejected, Request, SourceError, plan_conversion,
)

from conftest import needs_ffmpeg


# ---------------------------------------------------------------- the rule


@pytest.mark.parametrize(
    "kind, kbps, target, expected",
    [
        ("mp3", 320, 320, "copy"),     # the target exactly: nothing to do
        ("mp3", 192, 320, "copy"),     # below: never upscale
        ("mp3", 128, 320, "copy"),
        ("mp3", 330, 320, "copy"),     # VBR wobble inside the tolerance
        ("mp3", 320, 192, "encode"),   # above: down to the target
        ("mp3", 245, 192, "encode"),   # a V0 at a 192 target
        ("lossless", 900, 320, "encode"),
        ("lossless", 0, 320, "encode"),  # lossless needs no bitrate
        ("lossy", 256, 320, "encode"),   # AAC/Vorbis cannot be copied into MP3
    ],
)
def test_plan_conversion(kind, kbps, target, expected):
    assert plan_conversion(kind, kbps, target) == expected


@pytest.mark.parametrize("kind, kbps", [("mp3", 96), ("lossy", 64), ("mp3", 0)])
def test_plan_conversion_rejects_poor_lossy(kind, kbps):
    with pytest.raises(Rejected):
        plan_conversion(kind, kbps, 320)


@pytest.mark.parametrize("source, target, expected", [
    (130, 320, 160),   # YouTube's Opus: the step that holds it, not the setting
    (126, 320, 160),   # live run: 128 would have lost detail
    (130, 192, 160),
    (130, 128, 128),   # never above the setting
    (128, 320, 160),   # a peer's 128 kbps AAC
    (96, 320, 128),
    (256, 320, 320),
    (256, 192, 192),
    (0, 192, 192),     # unknown: the setting
    (400, 320, 320),
])
def test_honest_bitrate(source, target, expected):
    assert sources.honest_bitrate(source, target) == expected


def test_parse_duration_ms():
    assert sources.parse_duration_ms("185733") == 186
    assert sources.parse_duration_ms("266000.0") == 266
    assert sources.parse_duration_ms("") == 0
    assert sources.parse_duration_ms("n/a") == 0
    assert sources.parse_duration_ms("-5") == 0


# ---------------------------------------------------------------- fake client


class FakeClient(P2PClient):
    """
    `results` maps query -> list of SearchResponse. `files` maps remote
    filename -> a local fixture path, or an Exception to raise from download.
    """

    name = "fakenet"
    label = "FakeNet"

    def __init__(self, results, files, cancel_on=None):
        self.results = results
        self.files = files
        self.searched = []
        self.downloaded = []
        self.cancel_on = cancel_on
        self.tmp = None

    def search(self, query, should_cancel):
        self.searched.append(query)
        return self.results.get(query, [])

    def download(self, cand, should_cancel):
        self.downloaded.append(cand.file.filename)
        if cand.file.filename == self.cancel_on:
            raise Cancelled()
        src = self.files[cand.file.filename]
        if isinstance(src, Exception):
            raise src
        # A copy, because the source deletes what it is handed.
        dest = os.path.join(self.tmp, f"dl{len(self.downloaded)}{os.path.splitext(src)[1]}")
        shutil.copy(src, dest)
        return dest


def resp(user, *files, free=True, speed=500_000, queue=0):
    return SearchResponse(user=user, free_slot=free, avg_speed=speed, queue_len=queue, files=list(files))


def sf(name, size=5_000_000, br=None, dur=30):
    return SharedFile(filename=name, size=size, ext=name.rsplit(".", 1)[-1].lower(), bitrate=br, duration=dur)


def request(tmp_path, bitrate=320, duration=30, cancel=None):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return Request(
        track=Track(artist="Enya", title="Orinoco Flow", duration=duration),
        query="Enya - Orinoco Flow",
        output_dir=str(out),
        basename="Enya - 02 - Orinoco Flow",
        bitrate=bitrate,
        audio_format="mp3",
        should_cancel=cancel,
    )


def make_source(tmp_path, results, files, **kw):
    client = FakeClient(results, files, **kw)
    client.tmp = str(tmp_path)
    return P2PSource(client), client


def mp3_kbps(path):
    from mutagen.mp3 import MP3

    return round(MP3(path).info.bitrate / 1000)


# ---------------------------------------------------------------- the loop


@needs_ffmpeg
def test_flac_is_encoded_down_to_the_target(tmp_path, audio):
    name = r"Enya\Watermark\02 Orinoco Flow.flac"
    source, _ = make_source(
        tmp_path, {"enya orinoco flow": [resp("alice", sf(name, 3_000_000))]}, {name: audio["flac"]}
    )
    req = request(tmp_path, bitrate=320)
    result = source.fetch(req)
    assert result["path"] == req.target_path
    assert result["conversion"] == "encode"
    assert mp3_kbps(req.target_path) == 320
    assert result["comment"] == "Source: FakeNet, peer alice"
    assert "flac" in result["source_label"] and "encoded to 320" in result["source_label"]


@needs_ffmpeg
def test_hi_res_flac_is_resampled_so_lame_can_take_it(tmp_path, audio):
    name = r"Enya\02 Orinoco Flow.flac"
    source, _ = make_source(
        tmp_path, {"enya orinoco flow": [resp("alice", sf(name, 6_000_000))]}, {name: audio["flac_96k"]}
    )
    req = request(tmp_path)
    source.fetch(req)
    from mutagen.mp3 import MP3

    assert MP3(req.target_path).info.sample_rate == 44100


@needs_ffmpeg
def test_mp3_below_the_target_is_kept_as_is(tmp_path, audio):
    name = r"Enya\02 Orinoco Flow.mp3"
    source, _ = make_source(
        tmp_path, {"enya orinoco flow": [resp("bob", sf(name, 720_000, 192))]}, {name: audio["mp3_192"]}
    )
    req = request(tmp_path, bitrate=320)
    result = source.fetch(req)
    assert result["conversion"] == "copy"
    assert mp3_kbps(req.target_path) == 192
    assert "kept as is" in result["source_label"]


@needs_ffmpeg
def test_a_peers_aac_is_encoded_for_its_own_quality_not_the_setting(tmp_path, audio):
    name = r"Enya\02 Orinoco Flow.m4a"
    source, _ = make_source(
        tmp_path, {"enya orinoco flow": [resp("bob", sf(name, 520_000, 128))]}, {name: audio["aac_128"]}
    )
    req = request(tmp_path, bitrate=320)
    result = source.fetch(req)
    assert result["conversion"] == "encode"
    assert mp3_kbps(req.target_path) == 160
    assert "aac" in result["source_label"] and "encoded to 160 kbps" in result["source_label"]


@needs_ffmpeg
def test_mp3_above_the_target_is_encoded_down(tmp_path, audio):
    name = r"Enya\02 Orinoco Flow.mp3"
    source, _ = make_source(
        tmp_path, {"enya orinoco flow": [resp("bob", sf(name, 1_200_000, 320))]}, {name: audio["mp3_320"]}
    )
    req = request(tmp_path, bitrate=192)
    result = source.fetch(req)
    assert result["conversion"] == "encode"
    assert mp3_kbps(req.target_path) == 192


@needs_ffmpeg
def test_wrong_length_file_is_rejected_and_the_next_is_tried(tmp_path, audio):
    # The 60 s file claims 30 s and outranks the other (320 vs 192 kbps), so it
    # is tried first; its real length gives it away.
    liar = r"Enya\02 Orinoco Flow.mp3"
    honest = r"Enya\Watermark\02 Orinoco Flow.mp3"
    source, client = make_source(
        tmp_path,
        {"enya orinoco flow": [resp("liar", sf(liar, 1_200_000, 320)), resp("honest", sf(honest, 720_000, 192))]},
        {liar: audio["long_mp3"], honest: audio["mp3_192"]},
    )
    result = source.fetch(request(tmp_path))
    assert client.downloaded == [liar, honest]
    assert result["peer"] == "peer honest"
    # The rejected download was deleted, not left in the shared folder.
    assert sorted(os.listdir(tmp_path)) == ["out"]


@needs_ffmpeg
def test_failures_of_every_kind_are_reported_per_peer(tmp_path, audio):
    a, b, c = (rf"Enya\{n} Orinoco Flow.mp3" for n in ("01", "02", "03"))
    source, _ = make_source(
        tmp_path,
        {"enya orinoco flow": [resp("p1", sf(a, br=320)), resp("p2", sf(b, br=256)), resp("p3", sf(c, br=192))]},
        {a: TimeoutError("transfer did not start within 60 s"), b: audio["mp3_96"], c: audio["not_audio"]},
    )
    with pytest.raises(SourceError) as err:
        source.fetch(request(tmp_path))
    msg = str(err.value)
    assert msg.startswith("tried 3 files, none usable")
    assert "peer p1: transfer did not start within 60 s" in msg
    assert "peer p2: it is only 96 kbps" in msg
    assert "peer p3: it is not an audio file" in msg


def test_at_most_max_candidates_are_tried(tmp_path):
    names = [rf"Enya\{i:02d} Orinoco Flow.mp3" for i in range(8)]
    source, client = make_source(
        tmp_path,
        {"enya orinoco flow": [resp(f"p{i}", sf(n, br=320)) for i, n in enumerate(names)]},
        {n: ConnectionError("peer went offline") for n in names},
    )
    with pytest.raises(SourceError):
        source.fetch(request(tmp_path))
    assert len(client.downloaded) == sources.MAX_CANDIDATES


@needs_ffmpeg
def test_an_unreachable_peer_is_skipped_entirely_and_has_its_own_budget(tmp_path, audio):
    # Two files from a peer that cannot be contacted, then four more
    # unreachable peers, then one that delivers. Unreachable ones neither
    # repeat nor use up MAX_CANDIDATES.
    gone = [rf"Enya\{n} Orinoco Flow.mp3" for n in ("01", "02")]
    others = [rf"Enya\x{i} Orinoco Flow.mp3" for i in range(4)]
    good = r"Enya\Watermark\02 Orinoco Flow.mp3"
    results = [resp("gone", *(sf(n, br=320) for n in gone))]
    results += [resp(f"far{i}", sf(n, br=320)) for i, n in enumerate(others)]
    results += [resp("near", sf(good, 720_000, 192))]
    files = {n: PeerUnreachable("the peer did not answer") for n in gone + others}
    files[good] = audio["mp3_192"]
    source, client = make_source(tmp_path, {"enya orinoco flow": results}, files)
    result = source.fetch(request(tmp_path))
    assert result["peer"] == "peer near"
    assert sum(1 for d in client.downloaded if d in gone) == 1
    assert len(client.downloaded) == 6


def test_unreachable_peers_have_a_cap_too(tmp_path):
    names = [rf"Enya\{i:02d} Orinoco Flow.mp3" for i in range(20)]
    source, client = make_source(
        tmp_path,
        {"enya orinoco flow": [resp(f"p{i}", sf(n, br=320)) for i, n in enumerate(names)]},
        {n: PeerUnreachable("the peer is offline") for n in names},
    )
    with pytest.raises(SourceError):
        source.fetch(request(tmp_path))
    assert len(client.downloaded) == sources.MAX_UNREACHABLE


def test_nothing_found_says_what_was_searched(tmp_path):
    source, client = make_source(tmp_path, {}, {})
    with pytest.raises(SourceError) as err:
        source.fetch(request(tmp_path))
    assert client.searched == ["enya orinoco flow", "orinoco flow"]
    assert "no file matched 'enya orinoco flow' or 'orinoco flow'" in str(err.value)


def test_second_query_only_when_the_first_found_nothing_good(tmp_path):
    name = r"Enya\Watermark\02 Orinoco Flow.mp3"
    source, client = make_source(
        tmp_path, {"enya orinoco flow": [resp("a", sf(name, br=320))]}, {name: ConnectionError("x")}
    )
    with pytest.raises(SourceError):
        source.fetch(request(tmp_path))
    assert client.searched == ["enya orinoco flow"]


def test_results_without_bitrate_or_length_and_implausible_sizes_are_dropped(tmp_path):
    # No bitrate, no length: a 300 MB "track" is an album image.
    name = r"Enya\Orinoco Flow.flac"
    source, client = make_source(
        tmp_path,
        {"enya orinoco flow": [resp("someone", sf(name, 300_000_000, None, None))]},
        {name: ConnectionError("should not be downloaded")},
    )
    with pytest.raises(SourceError) as err:
        source.fetch(request(tmp_path, duration=266))
    assert client.downloaded == []
    assert "dropped for a size" in str(err.value)


def test_cancel_during_download_stops_the_source(tmp_path):
    name = r"Enya\02 Orinoco Flow.mp3"
    source, _ = make_source(
        tmp_path, {"enya orinoco flow": [resp("a", sf(name, br=320))]}, {}, cancel_on=name
    )
    with pytest.raises(Cancelled):
        source.fetch(request(tmp_path))


def test_cancel_before_search_stops_the_source(tmp_path):
    source, client = make_source(tmp_path, {}, {})
    with pytest.raises(Cancelled):
        source.fetch(request(tmp_path, cancel=lambda: True))
    assert client.searched == []


@needs_ffmpeg
def test_a_peer_that_delivered_is_preferred_next_time(tmp_path, audio):
    name = r"Enya\02 Orinoco Flow.mp3"
    source, _ = make_source(
        tmp_path, {"enya orinoco flow": [resp("good", sf(name, 720_000, 192))]}, {name: audio["mp3_192"]}
    )
    source.fetch(request(tmp_path))
    assert "good" in source.known
