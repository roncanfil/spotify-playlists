"""
spotify_link.py: parsing pasted links, and reading Spotify's public pages into
CSV rows. The pages are small stand-ins with the same structure as Spotify's
(checked November 2026); no network.
"""

import base64
import csv
import json

import pytest

import spotify_link as L

ALBUM_ID = "4m2880jivSbbyEGAKfITCa"
TRACK_ID = "0dEIca2nhcxDUV8C5QkPYb"
LIST_ID = "37i9dQZF1DX4UtSsGT1Sbe"


def artists(*names):
    return {"items": [{"profile": {"name": n}, "uri": f"spotify:artist:{n[:3]}"} for n in names]}


def rendered(uri, entity):
    blob = base64.b64encode(json.dumps({"entities": {"items": {uri: entity}}}).encode()).decode()
    return f'<html><script id="initialState" type="text/plain">{blob}</script></html>'


def embed(entity):
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def album_track(n, name, ms=200000, disc=1):
    return {"track": {"name": name, "uri": f"spotify:track:t{n}", "artists": artists("Daft Punk"),
                      "trackNumber": n, "discNumber": disc, "duration": {"totalMilliseconds": ms},
                      "contentRating": {"label": "NONE"}}}


ALBUM = {
    "name": "Random Access Memories",
    "artists": artists("Daft Punk"),
    "date": {"year": 2013, "month": 5, "day": 20},
    "coverArt": {"sources": [{"url": "small", "width": 64}, {"url": "big", "width": 640}]},
    "tracksV2": {"totalCount": 2, "items": [album_track(1, "Give Life Back to Music", 275386),
                                            album_track(2, "The Game of Love")]},
}

TRACK = {
    "name": "Ya Hit",
    "uri": f"spotify:track:{TRACK_ID}",
    "firstArtist": artists("Future"),
    "otherArtists": artists("Drake"),
    "trackNumber": 4,
    "duration": {"totalMilliseconds": 214000},
    "albumOfTrack": {"name": "Ya Hit", "uri": "spotify:album:x", "date": {"year": 2026},
                     "coverArt": {"sources": [{"url": "cover", "width": 640}]}},
}


def embed_track(n, title, subtitle, ms=180000, kind="track"):
    return {"uri": f"spotify:track:p{n}", "title": title, "subtitle": subtitle,
            "duration": ms, "entityType": kind}


PLAYLIST_EMBED = {
    "name": "All Out 80s",
    "subtitle": "Spotify",
    "trackList": [
        embed_track(1, "Thriller", "Michael Jackson", 358807),
        embed_track(2, "September", "Earth, Wind & Fire"),
        embed_track(3, "Sweet Dreams", "Eurythmics, Annie Lennox"),
        embed_track(4, "A podcast", "Someone", kind="episode"),
    ],
}

PLAYLIST_PAGE = {
    "name": "All Out 80s",
    "ownerV2": {"data": {"name": "Spotify"}},
    "content": {"totalCount": 150, "items": [{"itemV2": {"data": {
        "uri": "spotify:track:p1", "albumOfTrack": {"name": "Thriller", "uri": "spotify:album:th"},
        "artists": artists("Michael Jackson"),
    }}}]},
}


class Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class Session:
    """Answers GETs from a {url: (status, body)} map; anything else is a 404."""

    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        assert headers["User-Agent"] == L.USER_AGENT
        return Resp(*self.pages.get(url, (404, "")))


# ---------------------------------------------------------------- parse


@pytest.mark.parametrize("text, kind, id_", [
    (f"https://open.spotify.com/album/{ALBUM_ID}?si=abc", "album", ALBUM_ID),
    (f"open.spotify.com/intl-es/track/{TRACK_ID}", "track", TRACK_ID),
    (f"  Check this out https://open.spotify.com/playlist/{LIST_ID}?si=x  ", "playlist", LIST_ID),
    (f"https://open.spotify.com/embed/album/{ALBUM_ID}", "album", ALBUM_ID),
    (f"spotify:playlist:{LIST_ID}", "playlist", LIST_ID),
])
def test_links_are_recognised(text, kind, id_):
    assert L.parse(text) == L.Link(kind, id_)


@pytest.mark.parametrize("text, message", [
    ("", "Paste a Spotify link first."),
    ("hello", "does not look like a Spotify link"),
    ("https://spotify.link/AbCdEf", "shortened link"),
    (f"https://open.spotify.com/artist/{ALBUM_ID}", "to an artist"),
    (f"https://open.spotify.com/episode/{ALBUM_ID}", "podcast episode"),
])
def test_bad_links_say_what_is_wrong(text, message):
    with pytest.raises(L.LinkError, match=message):
        L.parse(text)


# ---------------------------------------------------------------- read


def test_an_album_has_numbers_dates_and_lengths():
    s = Session({f"{L.WEB}/album/{ALBUM_ID}": (200, rendered(f"spotify:album:{ALBUM_ID}", ALBUM))})
    col = L.read(L.Link("album", ALBUM_ID), s)
    assert (col.kind, col.name, col.artist, col.image, col.truncated) == (
        "album", "Random Access Memories", "Daft Punk", "big", False)
    first = col.rows[0]
    assert {k: first[k] for k in ("Track Name", "Artist Name(s)", "Album Name", "Album Release Date",
                                  "Track Number", "Disc Number", "Track Duration (ms)")} == {
        "Track Name": "Give Life Back to Music", "Artist Name(s)": "Daft Punk",
        "Album Name": "Random Access Memories", "Album Release Date": "2013-05-20",
        "Track Number": "1", "Disc Number": "1", "Track Duration (ms)": "275386",
    }


def test_a_long_album_is_completed_from_the_embed_without_numbers():
    album = dict(ALBUM, tracksV2=dict(ALBUM["tracksV2"], totalCount=3))
    s = Session({
        f"{L.WEB}/album/{ALBUM_ID}": (200, rendered(f"spotify:album:{ALBUM_ID}", album)),
        f"{L.WEB}/embed/album/{ALBUM_ID}": (200, embed({"trackList": [
            {"uri": "spotify:track:t1", "title": "Give Life Back to Music", "subtitle": "Daft Punk"},
            {"uri": "spotify:track:t3", "title": "Giorgio by Moroder", "subtitle": "Daft Punk", "duration": 544000},
        ]})),
    })
    col = L.read(L.Link("album", ALBUM_ID), s)
    assert [r["Track Name"] for r in col.rows] == [
        "Give Life Back to Music", "The Game of Love", "Giorgio by Moroder"]
    assert col.rows[2].get("Track Number", "") == ""
    assert col.rows[2]["Album Name"] == "Random Access Memories"
    assert not col.truncated


def test_a_song_lists_every_artist_and_has_no_number():
    s = Session({f"{L.WEB}/track/{TRACK_ID}": (200, rendered(f"spotify:track:{TRACK_ID}", TRACK))})
    col = L.read(L.Link("track", TRACK_ID), s)
    (row,) = col.rows
    assert (col.kind, col.name, col.artist) == ("track", "Ya Hit", "Future")
    assert row["Artist Name(s)"] == "Future, Drake"
    assert (row["Track Number"], row["Album Release Date"], row["Album Name"]) == ("", "2026", "Ya Hit")


def test_a_playlist_joins_the_embed_and_the_page():
    s = Session({
        f"{L.WEB}/embed/playlist/{LIST_ID}": (200, embed(PLAYLIST_EMBED)),
        f"{L.WEB}/playlist/{LIST_ID}": (200, rendered(f"spotify:playlist:{LIST_ID}", PLAYLIST_PAGE)),
    })
    col = L.read(L.Link("playlist", LIST_ID), s)
    assert [(r["Track Name"], r["Artist Name(s)"], r.get("Album Name", "")) for r in col.rows] == [
        ("Thriller", "Michael Jackson", "Thriller"),
        # A comma inside a name is not a separator; the embed's ", " is.
        ("September", "Earth, Wind & Fire", ""),
        ("Sweet Dreams", "Eurythmics, Annie Lennox", ""),
    ]
    assert (col.artist, col.total, col.listed, col.truncated) == ("Spotify", 150, 4, True)
    # Numbered by playlist position: no album numbers in the CSV.
    assert "Track Number" not in col.columns


def test_a_playlist_whose_page_fails_still_reads_from_the_embed():
    s = Session({f"{L.WEB}/embed/playlist/{LIST_ID}": (200, embed(PLAYLIST_EMBED))})
    col = L.read(L.Link("playlist", LIST_ID), s)
    assert len(col.rows) == 3 and col.artist == "Spotify" and not col.truncated


def test_a_private_or_missing_playlist_says_so():
    with pytest.raises(L.LinkError, match="Private playlists cannot be read"):
        L.read(L.Link("playlist", LIST_ID), Session({}))


def test_a_page_without_its_data_says_spotify_changed_it():
    s = Session({f"{L.WEB}/album/{ALBUM_ID}": (200, "<html>an app shell</html>")})
    with pytest.raises(L.LinkError, match="may have changed"):
        L.read(L.Link("album", ALBUM_ID), s)


def test_spotify_errors_are_reported():
    s = Session({f"{L.WEB}/album/{ALBUM_ID}": (503, "")})
    with pytest.raises(L.LinkError, match="HTTP 503"):
        L.read(L.Link("album", ALBUM_ID), s)


def test_the_csv_reads_back_with_the_downloader(tmp_path):
    import download as core

    s = Session({f"{L.WEB}/album/{ALBUM_ID}": (200, rendered(f"spotify:album:{ALBUM_ID}", ALBUM))})
    col = L.read(L.Link("album", ALBUM_ID), s)
    path = tmp_path / "a" / "b.csv"
    assert L.write_csv(col, str(path)) == 2
    columns, rows = core.read_playlist_csv(str(path))
    cols, warnings = core.resolve_columns(columns)
    assert cols["duration"] and cols["track_num"] and not warnings
    assert len(rows) == 2
