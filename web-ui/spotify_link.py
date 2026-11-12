"""
Read a pasted Spotify link (playlist, album or track) without a Spotify
account, and turn it into the same CSV the account export writes, so the rest
of the app runs it like any other playlist.

Only Spotify's public web pages are read, the ones any browser gets for the
link, with no token and no API call:

  open.spotify.com/<kind>/<id>
      Served to a non-browser client as a rendered page carrying an
      "initialState" block (base64 JSON). For an album or a track it has
      everything: names, artists, track and disc numbers, lengths, the
      release date. For a playlist it lists only the first 30 tracks.
  open.spotify.com/embed/<kind>/<id>
      The embeddable player. Its __NEXT_DATA__ block lists up to 100 tracks
      of a playlist with title, artists and length, but no album.

So a playlist link reads at most its first 100 tracks; the result says how
many Spotify has, and the app tells the user to use the account connection or
a CSV for the rest. Album names come from the rendered page and are left
empty for tracks it does not list. Checked against Spotify's pages in
November 2026; when Spotify changes them, read() fails with a LinkError that
says so rather than returning half a playlist.
"""

import base64
import csv
import json
import os
import re
from dataclasses import dataclass, field

import requests

from spotify import CSV_COLUMNS

WEB = "https://open.spotify.com"
TIMEOUT = 20
# An honest name. Spotify sends the rendered page to clients that are not a
# browser; a browser user agent gets an empty app shell instead.
USER_AGENT = "spotify-playlists-to-mp3"
# The embed's artist line separates artists with a comma and a no-break space,
# so a name that itself has a comma ("Earth, Wind & Fire") stays whole.
EMBED_ARTIST_SEP = ", "

KINDS = ("playlist", "album", "track")
_LINK = re.compile(
    r"(?:https?://)?(?:open|play)\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?"
    r"(?:embed/)?(?P<kind>[a-z]+)/(?P<id>[A-Za-z0-9]{22})",
    re.I,
)
_URI = re.compile(r"spotify:(?P<kind>[a-z]+):(?P<id>[A-Za-z0-9]{22})", re.I)


class LinkError(Exception):
    """A link that cannot be read, with a message for the person who pasted it."""


@dataclass
class Link:
    kind: str
    id: str

    @property
    def uri(self):
        return f"spotify:{self.kind}:{self.id}"

    @property
    def url(self):
        return f"{WEB}/{self.kind}/{self.id}"


@dataclass
class Collection:
    """What a link points at, as CSV rows ready for process_playlist."""

    kind: str
    id: str
    name: str
    # The playlist's owner, or the first artist of the album or track.
    artist: str
    rows: list
    # Tracks Spotify says it has, and how many of them the public pages
    # listed: fewer for a long playlist. Rows can be fewer again, as podcast
    # episodes and untitled entries are left out.
    total: int
    listed: int
    columns: list = field(default_factory=lambda: list(CSV_COLUMNS))
    image: str = ""

    @property
    def truncated(self):
        return self.total > self.listed


def parse(text):
    """The Link in pasted text, or LinkError saying what is wrong with it."""
    text = (text or "").strip()
    if not text:
        raise LinkError("Paste a Spotify link first.")
    if re.search(r"spotify\.link/|spotify\.app\.link/", text, re.I):
        raise LinkError(
            "That is a shortened link. Open it, then copy the open.spotify.com "
            "address from the browser and paste that."
        )
    m = _LINK.search(text) or _URI.search(text)
    if not m:
        raise LinkError(
            "That does not look like a Spotify link. Use Share > Copy link on a "
            "playlist, album or song."
        )
    kind = m.group("kind").lower()
    if kind not in KINDS:
        what = {"artist": "an artist", "show": "a podcast", "episode": "a podcast episode"}.get(
            kind, f"a {kind}"
        )
        raise LinkError(f"That link is to {what}. Paste a playlist, album or song link instead.")
    return Link(kind, m.group("id"))


# ---------------------------------------------------------------- fetching


def _get(url, session):
    try:
        r = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise LinkError(f"Could not reach Spotify: {e}") from None
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise LinkError(f"Spotify answered HTTP {r.status_code}. Try again in a minute.")
    return r.text


def _initial_state(html, uri):
    """The entity for `uri` from a rendered page, or None."""
    m = re.search(r'<script id="initialState" type="text/plain">\s*([^<]+?)\s*</script>', html or "")
    if not m:
        return None
    try:
        state = json.loads(base64.b64decode(m.group(1)))
    except (ValueError, TypeError):
        return None
    item = ((state.get("entities") or {}).get("items") or {}).get(uri)
    return item if isinstance(item, dict) else None


def _embed_entity(html):
    """The entity from an embed page, or None."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">\s*(.+?)\s*</script>', html or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        ent = data["props"]["pageProps"]["state"]["data"]["entity"]
    except (ValueError, KeyError, TypeError):
        return None
    return ent if isinstance(ent, dict) else None


def _missing(link):
    if link.kind == "playlist":
        return LinkError(
            "Spotify shows no public playlist at that link. Private playlists "
            "cannot be read from a link: make it public, or connect your Spotify "
            "account in this tab, or use a CSV."
        )
    return LinkError(f"Spotify shows no {'song' if link.kind == 'track' else link.kind} at that link.")


def _changed(link):
    return LinkError(
        f"Spotify's {link.kind} page did not have the expected track list; "
        "Spotify may have changed it. Use a CSV, or connect your account, for now."
    )


# ---------------------------------------------------------------- shaping


def _names(artists):
    """Artist names from {"items": [{"profile": {"name"}}]}."""
    out = []
    for a in (artists or {}).get("items") or []:
        name = ((a or {}).get("profile") or {}).get("name")
        if name:
            out.append(str(name))
    return out


def _uris(artists):
    return [str(a.get("uri")) for a in (artists or {}).get("items") or [] if a and a.get("uri")]


def _date(d):
    """"YYYY-MM-DD", "YYYY-MM" or "YYYY" from {"year", "month", "day"}."""
    if not isinstance(d, dict) or not d.get("year"):
        return ""
    parts = [f"{int(d['year']):04d}"]
    for key in ("month", "day"):
        if not d.get(key):
            break
        parts.append(f"{int(d[key]):02d}")
    return "-".join(parts)


def _ms(duration):
    if isinstance(duration, dict):
        duration = duration.get("totalMilliseconds")
    try:
        return int(duration or 0)
    except (TypeError, ValueError):
        return 0


def _image(entity):
    """The largest cover image URL in a rendered entity, or ""."""
    sources = (((entity or {}).get("coverArt") or {}).get("sources")) or []
    if not sources and isinstance(entity, dict):
        sources = ((entity.get("images") or {}).get("items") or [{}])[0].get("sources") or []
    best = max(sources, key=lambda s: s.get("width") or 0, default={})
    return str(best.get("url") or "")


def _row(track, album_name="", album_uri="", album_artists=(), album_artist_uris=(),
         release="", image=""):
    """One CSV row from a rendered Track object."""
    artists = _names(track.get("artists"))
    return {
        "Track URI": str(track.get("uri") or ""),
        "Track Name": str(track.get("name") or ""),
        "Artist URI(s)": ", ".join(_uris(track.get("artists"))),
        "Artist Name(s)": ", ".join(artists),
        "Album URI": album_uri,
        "Album Name": album_name,
        "Album Artist URI(s)": ", ".join(album_artist_uris),
        "Album Artist Name(s)": ", ".join(album_artists),
        "Album Release Date": release,
        "Album Image URL": image,
        "Disc Number": str(track.get("discNumber") or ""),
        "Track Number": str(track.get("trackNumber") or ""),
        "Track Duration (ms)": str(_ms(track.get("duration")) or ""),
        "Explicit": str(((track.get("contentRating") or {}).get("label")) == "EXPLICIT").lower(),
    }


def _embed_row(t):
    """One CSV row from an embed track-list entry (no album)."""
    artists = [a.strip() for a in str(t.get("subtitle") or "").split(EMBED_ARTIST_SEP) if a.strip()]
    return {
        "Track URI": str(t.get("uri") or ""),
        "Track Name": str(t.get("title") or ""),
        "Artist Name(s)": ", ".join(artists),
        "Track Duration (ms)": str(_ms(t.get("duration")) or ""),
        "Explicit": str(bool(t.get("isExplicit"))).lower(),
    }


def _playable(rows):
    """Rows that name a track and an artist; the downloader skips the rest anyway."""
    return [r for r in rows if r.get("Track Name") and r.get("Artist Name(s)")]


# ---------------------------------------------------------------- readers


def read(link, session=None):
    """The Collection a Link points at. Raises LinkError."""
    session = session or requests
    reader = {"album": _album, "track": _track, "playlist": _playlist}[link.kind]
    col = reader(link, session)
    if not col.rows:
        raise LinkError(f"That {'song' if link.kind == 'track' else link.kind} has no tracks to download.")
    return col


def _album(link, session):
    html = _get(link.url, session)
    if html is None:
        raise _missing(link)
    album = _initial_state(html, link.uri)
    if not album:
        raise _changed(link)
    artists = _names(album.get("artists"))
    artist_uris = _uris(album.get("artists"))
    release = _date(album.get("date"))
    image = _image(album)
    name = str(album.get("name") or "")
    tracks = album.get("tracksV2") or album.get("tracks") or {}
    rows = []
    for item in tracks.get("items") or []:
        t = (item or {}).get("track") or item or {}
        rows.append(_row(t, name, link.uri, artists, artist_uris, release, image))
    total = int(tracks.get("totalCount") or len(rows))
    if total > len(rows):
        # The rendered page lists a long album in parts. The embed lists more,
        # without track numbers; those rows are left unnumbered rather than
        # numbered by position, which is wrong on a multi-disc album.
        ent = _embed_entity(_get(f"{WEB}/embed/album/{link.id}", session))
        have = {r["Track URI"] for r in rows}
        for t in (ent or {}).get("trackList") or []:
            if t.get("uri") not in have:
                r = _embed_row(t)
                r.update({"Album URI": link.uri, "Album Name": name,
                          "Album Artist URI(s)": ", ".join(artist_uris),
                          "Album Artist Name(s)": ", ".join(artists),
                          "Album Release Date": release, "Album Image URL": image})
                rows.append(r)
    return Collection("album", link.id, name, artists[0] if artists else "", _playable(rows),
                      total, len(rows), image=image)


def _track(link, session):
    html = _get(link.url, session)
    if html is None:
        raise _missing(link)
    t = _initial_state(html, link.uri)
    if not t:
        raise _changed(link)
    album = t.get("albumOfTrack") or {}
    # A track's page names its artists in firstArtist + otherArtists rather
    # than "artists".
    if not t.get("artists"):
        t = dict(t)
        t["artists"] = {"items": ((t.get("firstArtist") or {}).get("items") or [])
                        + ((t.get("otherArtists") or {}).get("items") or [])}
    image = _image(album)
    row = _row(t, str(album.get("name") or ""), str(album.get("uri") or ""),
               release=_date(album.get("date")), image=image)
    # A single goes in the artist's folder as "Artist - Title": its number on
    # the album would only look odd there.
    row["Track Number"] = ""
    artists = _names(t.get("artists"))
    return Collection("track", link.id, row["Track Name"], artists[0] if artists else "",
                      _playable([row]), 1, 1, image=image)


def _playlist(link, session):
    embed = _embed_entity(_get(f"{WEB}/embed/playlist/{link.id}", session))
    if embed is None:
        raise _missing(link)
    listed = embed.get("trackList")
    if not isinstance(listed, list):
        raise _changed(link)
    # The rendered page adds album names (for its first 30 tracks) and the
    # real track count. Optional: without it the embed's list stands alone.
    page = None
    try:
        page = _initial_state(_get(link.url, session), link.uri)
    except LinkError:
        pass
    albums = {}
    content = (page or {}).get("content") or {}
    for item in content.get("items") or []:
        t = ((item or {}).get("itemV2") or {}).get("data") or {}
        if t.get("uri"):
            albums[t["uri"]] = t
    rows = []
    for t in listed:
        if (t.get("entityType") or "track") != "track":
            continue  # podcast episodes in a playlist
        r = _embed_row(t)
        full = albums.get(t.get("uri"))
        if full:
            a = full.get("albumOfTrack") or {}
            r["Album Name"] = str(a.get("name") or "")
            r["Album URI"] = str(a.get("uri") or "")
            # The rendered page's artist list is exact; prefer it.
            names = _names(full.get("artists"))
            if names:
                r["Artist Name(s)"] = ", ".join(names)
                r["Artist URI(s)"] = ", ".join(_uris(full.get("artists")))
        rows.append(r)
    owner = ((((page or {}).get("ownerV2") or {}).get("data")) or {}).get("name") or embed.get("subtitle") or ""
    total = int(content.get("totalCount") or len(listed))
    # Numbered by position in the playlist, the order it is played in: the
    # album numbers the account export carries are known here for at most 30.
    columns = [c for c in CSV_COLUMNS if c not in ("Track Number", "Disc Number")]
    return Collection("playlist", link.id, str(embed.get("name") or ""), str(owner),
                      _playable(rows), max(total, len(listed)), len(listed), columns=columns,
                      image=_image(page) if page else "")


def write_csv(col, path):
    """Write the Collection's rows to `path`, creating its folder; atomic."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=col.columns, quoting=csv.QUOTE_ALL,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(col.rows)
    os.replace(tmp, path)
    return len(col.rows)
