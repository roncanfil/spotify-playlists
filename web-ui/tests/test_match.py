"""
match.py: spotseek's rank.rs tests ported one for one, then tests for the
size-based estimate, which is ours.
"""

import match
from match import Ranker, SearchResponse, SharedFile, Track


def track(a, t, d):
    return Track(artist=a, title=t, duration=d)


def file(name, size, br, dur):
    ext = name.rsplit(".", 1)[-1].lower()
    return SharedFile(filename=name, size=size, ext=ext, bitrate=br, duration=dur)


# ---------------------------------------------------------------- ported


def test_normalizes():
    assert match.norm("  Beyoncé — Halo (Live!) ") == "beyonce halo live"
    assert match.base_title("Halo (feat. X) - Remastered") == "Halo"
    assert match.main_artist("Calvin Harris feat. Rihanna") == "Calvin Harris"
    assert match.stem(r"@@music\Daft Punk\Discovery\01 - One More Time.flac") == "01 - One More Time"


def test_fits_the_same_song_not_a_remix():
    t = track("Daft Punk", "One More Time", 320)
    good = file(r"music\Daft Punk\Discovery\01 - One More Time.mp3", 12_000_000, 320, 321)
    remix = file(r"music\Daft Punk\01 - One More Time (Remix).mp3", 12_000_000, 320, 321)
    long = file(r"music\Daft Punk\01 - One More Time.mp3", 12_000_000, 320, 600)
    other = file(r"music\Daft Punk\02 - Aerodynamic.mp3", 12_000_000, 320, 320)
    assert match.fit(t, good) >= 95, match.fit(t, good)
    assert match.fit(t, remix) < 75
    assert match.fit(t, long) < 75
    assert match.fit(t, other) < 50
    # A remix that is wanted is not a penalty.
    tr = track("Daft Punk", "One More Time (Remix)", 321)
    assert match.fit(tr, remix) >= 90


def test_quality_never_counts_low_files_as_good():
    flac = file("a.flac", 30_000_000, None, 200)
    mp3_320 = file("a.mp3", 8_000_000, 320, 200)
    mp3_128 = file("a.mp3", 3_200_000, 128, 200)
    m4a = file("a.m4a", 6_000_000, 256, 200)
    assert match.quality(flac, 320) == 1.0
    assert match.quality(mp3_320, 320) == 1.0
    assert match.quality(mp3_128, 320) < 0.5
    assert match.quality(m4a, 320) < match.quality(mp3_320, 320)
    assert match.quality(mp3_128, 128) == 1.0
    # No bitrate: from size and length.
    assert match.kbps(file("a.mp3", 8_000_000, None, 200)) == 320


def test_a_fast_free_peer_beats_a_queued_one_and_failures_go_last():
    t = track("Daft Punk", "One More Time", 320)
    r = Ranker(t, 320)
    f = file(r"music\Daft Punk\01 - One More Time.mp3", 12_800_000, 320, 320)
    r.add(SearchResponse(user="queued", free_slot=False, avg_speed=2_000_000, queue_len=30, files=[f]))
    r.add(SearchResponse(user="free", free_slot=True, avg_speed=900_000, queue_len=0, files=[f]))
    r.add(SearchResponse(
        user="slowflac", free_slot=True, avg_speed=60_000, queue_len=0,
        files=[file(r"x\Daft Punk - One More Time.flac", 40_000_000, None, 320)],
    ))
    r.add(SearchResponse(
        user="junk", free_slot=True, avg_speed=900_000, queue_len=0,
        files=[file(r"x\cover.jpg", 900_000, None, None), file(r"x\other.mp3", 9_000_000, 320, 200)],
    ))
    assert [c.user for c in r.cands] == ["free", "slowflac", "queued"]
    r.mark_failed(r.next().id)
    assert r.next().user == "slowflac"
    assert r.cands[-1].user == "free"
    assert r.good() == 1
    # spotseek's infos()/folder assertions are not ported: they test its
    # desktop window's list, which this app does not have.
    assert len(r.cands) == 3
    assert r.cands[-1].failed


def test_queries_use_the_main_artist_and_the_base_title():
    t = track("Calvin Harris, Rihanna", "This Is What You Came For (feat. Rihanna)", 222)
    assert match.queries(t) == ["calvin harris this is what you came for", "this is what you came for"]
    t = track("Enya", "Orinoco Flow - Remastered", 266)
    assert match.queries(t) == ["enya orinoco flow", "orinoco flow"]
    assert match.queries(track("ABBA", "SOS", 200)) == ["abba sos"]


# ---------------------------------------------------------------- ours


def test_main_artist_separators_are_case_insensitive():
    assert match.main_artist("Kanye West FEAT. Jay-Z") == "Kanye West"
    assert match.main_artist("Simon & Garfunkel") == "Simon"


def test_duplicate_files_from_one_peer_are_added_once():
    t = track("Enya", "Orinoco Flow", 266)
    f = file(r"Enya\Watermark\02 Orinoco Flow.mp3", 10_000_000, 320, 266)
    r = Ranker(t, 320)
    resp = SearchResponse(user="u", free_slot=True, avg_speed=100_000, queue_len=0, files=[f])
    assert r.add(resp) is True
    assert r.add(resp) is False
    assert len(r.cands) == 1


def test_known_peers_rank_higher():
    t = track("Enya", "Orinoco Flow", 266)
    f = file(r"Enya\Watermark\02 Orinoco Flow.mp3", 10_000_000, 320, 266)
    r = Ranker(t, 320, known={"friend"})
    r.add(SearchResponse(user="stranger", free_slot=True, avg_speed=100_000, queue_len=0, files=[f]))
    r.add(SearchResponse(user="friend", free_slot=True, avg_speed=100_000, queue_len=0, files=[f]))
    assert r.next().user == "friend"


def test_estimate_fills_bitrate_from_spotify_length():
    # A 4:26 track at 320 kbps is 10.64 MB.
    f = file(r"Enya\Orinoco Flow.mp3", 10_640_000, None, None)
    e = match.estimate(f, 266)
    assert e.bitrate == 320 and e.estimated
    # The length stays unknown, so fit() gives the neutral score, not a
    # perfect match for a length it never saw.
    assert e.duration is None


def test_estimate_drops_sizes_no_real_file_could_have():
    # A whole-album FLAC (45 min) offered under a four-minute track's name.
    album = file(r"Enya\Watermark.flac", 300_000_000, None, None)
    assert match.estimate(album, 266) is None
    # A 30-second preview clip saved as MP3.
    clip = file(r"Enya\Orinoco Flow.mp3", 1_200_000, None, None)
    assert match.estimate(clip, 266) is None
    # A CD-quality FLAC of the right length passes.
    flac = file(r"Enya\02 Orinoco Flow.flac", 30_000_000, None, None)
    assert match.estimate(flac, 266) is not None


def test_estimate_leaves_reported_values_and_unknown_lengths_alone():
    f = file("a.mp3", 1_000, 320, None)
    assert match.estimate(f, 266) is f
    g = file("a.mp3", 1_000, None, None)
    assert match.estimate(g, 0) is g


def test_a_remix_is_searched_with_its_version_words_first():
    t = track("Lady Gaga, SOFI TUKKER", "911 - Sofi Tukker Remix", 200)
    assert match.search_text(t) == "Lady Gaga 911 - Sofi Tukker Remix"
    assert match.queries(t) == ["lady gaga 911 sofi tukker remix", "lady gaga 911"]
    # Guests still go; the version stays.
    t = track("Adele", "Hello (feat. Someone) - Live at the BBC", 200)
    assert match.queries(t)[0] == "adele hello live at the bbc"
    # Remastered is not another version, so nothing changes.
    assert match.search_text(track("Enya", "Orinoco Flow - Remastered", 266)) == "Enya Orinoco Flow"
