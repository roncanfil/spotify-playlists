"""
Pasted links read through the connected Spotify account: spotify.py's
album/track/playlist entries, spotify_link.read_with_account, and the app's
fallback to the public pages when Spotify refuses. The Web API is faked.
"""

import pytest

import spotify as sp
import spotify_link as L


def artist(name, id_):
    return {"name": name, "id": id_, "uri": f"spotify:artist:{id_}"}


DP = artist("Daft Punk", "dp")
ALBUM = {
    "uri": "spotify:album:ram", "name": "Random Access Memories", "artists": [DP],
    "release_date": "2013-05-20", "images": [{"url": "big"}, {"url": "small"}],
    "tracks": {
        "items": [{"name": "Give Life Back to Music", "uri": "spotify:track:t1", "artists": [DP],
                   "track_number": 1, "disc_number": 1, "duration_ms": 275386}],
        "next": "https://api.spotify.com/v1/albums/ram/tracks?offset=1",
    },
}
PAGE2 = {"items": [{"name": "The Game of Love", "uri": "spotify:track:t2", "artists": [DP],
                    "track_number": 2, "disc_number": 1, "duration_ms": 322146}], "next": None}
TRACK = {
    "name": "Ya Hit", "uri": "spotify:track:yh", "artists": [artist("Future", "fu"), artist("Drake", "dr")],
    "track_number": 4, "duration_ms": 214000,
    "album": {"name": "Ya Hit", "release_date": "2026-11-06", "artists": [artist("Future", "fu")],
              "images": [{"url": "cover"}]},
}
PLAYLIST = {"name": "All Out 80s", "owner": {"display_name": "Spotify"}, "images": [{"url": "pl"}]}
ITEMS = {"items": [{"item": dict(TRACK, uri=f"spotify:track:p{i}")} for i in range(150)], "next": None}
GENRES = {"artists": [{"id": "dp", "genres": ["french house", "electro"]},
                      {"id": "fu", "genres": ["rap"]}]}


class Fake(sp.SpotifyClient):
    """A connected client whose API answers come from a table."""

    def __init__(self, refuse=None):
        self.calls, self.refuse = [], refuse

    def _get(self, url, params=None, _retry=True):
        self.calls.append(url)
        if self.refuse and self.refuse in url:
            raise sp.SpotifyError("Spotify refused this playlist (403).")
        for key, body in (("/albums/ram/tracks", PAGE2), ("/albums/ram", ALBUM),
                          ("/tracks/yh", TRACK), ("/items", ITEMS), ("/playlists/", PLAYLIST),
                          ("/artists", GENRES)):
            if key in url:
                return body
        raise AssertionError(url)


def test_an_album_through_the_account_has_every_page_and_genres():
    col = L.read_with_account(L.Link("album", "ram"), Fake())
    assert (col.via, col.name, col.artist, col.image, col.truncated) == (
        "account", "Random Access Memories", "Daft Punk", "big", False)
    assert [(r["Track Name"], r["Track Number"], r["Album Name"], r["Album Release Date"], r["Genres"])
            for r in col.rows] == [
        ("Give Life Back to Music", 1, "Random Access Memories", "2013-05-20", "french house, electro"),
        ("The Game of Love", 2, "Random Access Memories", "2013-05-20", "french house, electro"),
    ]
    assert all("_artist_id" not in r for r in col.rows)


def test_a_song_through_the_account_is_unnumbered_with_its_album():
    col = L.read_with_account(L.Link("track", "yh"), Fake())
    (row,) = col.rows
    assert (col.artist, row["Artist Name(s)"], row["Album Name"], row["Track Number"], row["Genres"]) == (
        "Future", "Future, Drake", "Ya Hit", "", "rap")


def test_a_long_playlist_through_the_account_is_complete_and_matches_the_export():
    col = L.read_with_account(L.Link("playlist", "80s"), Fake())
    assert (col.name, col.artist, len(col.rows), col.truncated) == ("All Out 80s", "Spotify", 150, False)
    # Same columns as the account export, so the same file names.
    assert col.columns == sp.CSV_COLUMNS


def test_a_refusal_is_raised_for_the_caller_to_fall_back():
    with pytest.raises(sp.SpotifyError):
        L.read_with_account(L.Link("playlist", "80s"), Fake(refuse="/playlists/"))
