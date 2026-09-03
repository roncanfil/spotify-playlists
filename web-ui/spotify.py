"""
Spotify integration: OAuth (Authorization Code + PKCE), playlist listing, and
CSV export in the same column layout Exportify produces.

Why the token lives on the server
---------------------------------
Spotify rejects `localhost` as a redirect URI and requires HTTPS for anything
that is not a loopback address, so the only plain-HTTP redirect it accepts is
`http://127.0.0.1:PORT/callback`. A phone on the LAN redirecting there would hit
its own loopback, not this server -- so a per-visitor OAuth flow cannot work
over HTTP.

Instead one person connects once from a browser on the server host; the refresh
token is stored in the state volume and every later visitor uses it. That also
keeps the Spotify app out of the 25-user development-mode limit, since only the
owner's account ever authenticates.
"""

import base64
import csv
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse

import requests

AUTH_BASE = "https://accounts.spotify.com"
API_BASE = "https://api.spotify.com/v1"

# Read-only playlist access, plus saved tracks so "Liked Songs" can be listed.
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"

# Matches Exportify's export exactly, so generated CSVs and any the user already
# has parse identically. "Genres" is appended: Exportify omits it by default,
# which is why existing playlists tagged no genre.
CSV_COLUMNS = [
    "Track URI",
    "Track Name",
    "Artist URI(s)",
    "Artist Name(s)",
    "Album URI",
    "Album Name",
    "Album Artist URI(s)",
    "Album Artist Name(s)",
    "Album Release Date",
    "Album Image URL",
    "Disc Number",
    "Track Number",
    "Track Duration (ms)",
    "Track Preview URL",
    "Explicit",
    "Popularity",
    "ISRC",
    "Added By",
    "Added At",
    "Genres",
]

LIKED_SONGS_ID = "__liked__"


class SpotifyError(Exception):
    """Any failure talking to Spotify, with a message safe to show a user."""


class SpotifyClient:
    """
    Holds the stored token and talks to the Web API.

    One instance per process; guarded by a lock because the download worker and
    HTTP handlers can both trigger a refresh.
    """

    def __init__(self, client_id, redirect_uri, token_path):
        self.client_id = (client_id or "").strip()
        self.redirect_uri = (redirect_uri or "").strip()
        self.token_path = token_path
        self._lock = threading.Lock()
        self._token = self._load()
        self._pending = {}  # oauth state -> (verifier, created_at)

    # ---------- configuration ----------

    @property
    def configured(self):
        return bool(self.client_id and self.redirect_uri)

    @property
    def connected(self):
        return bool(self._token.get("refresh_token"))

    # ---------- token storage ----------

    def _load(self):
        try:
            with open(self.token_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
            tmp = self.token_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._token, fh)
            os.replace(tmp, self.token_path)
        except OSError as e:
            raise SpotifyError(f"Could not save the Spotify token: {e}") from e

    def disconnect(self):
        with self._lock:
            self._token = {}
        try:
            os.remove(self.token_path)
        except OSError:
            pass

    # ---------- PKCE authorization ----------

    def build_auth_url(self):
        """Return the URL to send the browser to. Keeps the verifier in memory."""
        if not self.configured:
            raise SpotifyError(
                "Spotify is not configured. Set SPOTIFY_CLIENT_ID and "
                "SPOTIFY_REDIRECT_URI, then restart."
            )
        verifier = secrets.token_urlsafe(64)[:96]
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        state = secrets.token_urlsafe(16)

        with self._lock:
            # Drop anything older than 10 minutes so this cannot grow unbounded.
            cutoff = time.time() - 600
            self._pending = {
                k: v for k, v in self._pending.items() if v[1] > cutoff
            }
            self._pending[state] = (verifier, time.time())

        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "scope": SCOPES,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "state": state,
            }
        )
        return f"{AUTH_BASE}/authorize?{query}"

    def exchange_code(self, code, state):
        with self._lock:
            entry = self._pending.pop(state, None)
        if not entry:
            raise SpotifyError(
                "That login link expired or was already used. Try connecting again."
            )
        verifier = entry[0]

        resp = requests.post(
            f"{AUTH_BASE}/api/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "code_verifier": verifier,
            },
            timeout=20,
        )
        self._store_token_response(resp)

    def _refresh_locked(self):
        refresh = self._token.get("refresh_token")
        if not refresh:
            raise SpotifyError("Not connected to Spotify.")
        resp = requests.post(
            f"{AUTH_BASE}/api/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": self.client_id,
            },
            timeout=20,
        )
        # Spotify may omit refresh_token on refresh; keep the existing one.
        self._store_token_response(resp, keep_refresh=refresh, locked=True)

    def _store_token_response(self, resp, keep_refresh=None, locked=False):
        if resp.status_code != 200:
            detail = ""
            try:
                body = resp.json()
                detail = body.get("error_description") or body.get("error") or ""
            except ValueError:
                detail = (resp.text or "")[:160]
            if resp.status_code in (400, 401) and keep_refresh:
                raise SpotifyError(
                    f"Spotify rejected the stored login ({detail}). Reconnect."
                )
            raise SpotifyError(f"Spotify login failed: {detail or resp.status_code}")

        body = resp.json()
        token = {
            "access_token": body.get("access_token"),
            "refresh_token": body.get("refresh_token") or keep_refresh,
            "expires_at": time.time() + int(body.get("expires_in", 3600)) - 60,
            "scope": body.get("scope", ""),
        }
        if locked:
            self._token = token
        else:
            with self._lock:
                self._token = token
        self._save()

    def _access_token(self):
        with self._lock:
            if not self._token.get("refresh_token"):
                raise SpotifyError("Not connected to Spotify.")
            if self._token.get("expires_at", 0) <= time.time():
                self._refresh_locked()
            return self._token["access_token"]

    # ---------- API helpers ----------

    def _get(self, url, params=None, _retry=True):
        token = self._access_token()
        resp = requests.get(
            url if url.startswith("http") else f"{API_BASE}{url}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        if resp.status_code == 401 and _retry:
            with self._lock:
                self._refresh_locked()
            return self._get(url, params, _retry=False)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2"))
            time.sleep(min(wait, 30))
            if _retry:
                return self._get(url, params, _retry=False)
            raise SpotifyError("Spotify is rate-limiting; try again shortly.")
        if resp.status_code != 200:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except ValueError:
                pass
            path = (url if url.startswith("http") else f"{API_BASE}{url}")
            path = path.replace(API_BASE, "") or "/"
            path = path.split("?", 1)[0]

            # A bare "403: Forbidden" told the user nothing and sent us looking
            # for a bug in this client. Spotify returns it for a playlist a
            # Development Mode app is not allowed to read, which is the common
            # case, so say so and give the way around it.
            if resp.status_code == 403 and "/playlists/" in path:
                raise SpotifyError(
                    "Spotify refused this playlist (403). Apps in Development "
                    "Mode can generally only read playlists you created "
                    "yourself. Playlists you merely follow, and Spotify's own "
                    "algorithmic ones such as Discover Weekly and Release "
                    "Radar, are refused no matter what this app does. "
                    "Workarounds: request a quota extension on the Spotify "
                    "dashboard, or export the playlist at exportify.app and "
                    "queue it from the CSV tab."
                    + (f" Spotify said: {detail}" if detail else "")
                )
            raise SpotifyError(
                f"Spotify API error {resp.status_code} on {path}"
                + (f": {detail}" if detail else "")
            )
        return resp.json()

    def _paged(self, url, params=None, limit_pages=200):
        """Yield items across a paginated endpoint."""
        page = self._get(url, params)
        pages = 0
        while page:
            for item in page.get("items", []):
                yield item
            nxt = page.get("next")
            pages += 1
            if not nxt or pages >= limit_pages:
                return
            page = self._get(nxt)

    # ---------- public data ----------

    def me(self):
        data = self._get("/me")
        return {
            "id": data.get("id"),
            "name": data.get("display_name") or data.get("id"),
            "image": (data.get("images") or [{}])[0].get("url"),
        }

    def playlists(self):
        """Every playlist the user can see, plus a synthetic Liked Songs entry."""
        out = []
        liked = self._get("/me/tracks", {"limit": 1})
        out.append(
            {
                "id": LIKED_SONGS_ID,
                "name": "Liked Songs",
                "owner": "you",
                "tracks": liked.get("total", 0),
                "image": None,
                "public": False,
                "collaborative": False,
                "description": "Your saved tracks",
            }
        )
        for pl in self._paged("/me/playlists", {"limit": 50}):
            if not pl:
                continue
            out.append(
                {
                    "id": pl.get("id"),
                    "name": pl.get("name") or "(untitled)",
                    "owner": (pl.get("owner") or {}).get("display_name") or "",
                    # Spotify's Feb 2026 migration renamed this from
                    # "tracks" to "items"; read both so it works either way.
                    "tracks": (
                        pl.get("items") or pl.get("tracks") or {}
                    ).get("total", 0),
                    "image": (pl.get("images") or [{}])[0].get("url")
                    if pl.get("images")
                    else None,
                    "public": bool(pl.get("public")),
                    "collaborative": bool(pl.get("collaborative")),
                    "description": pl.get("description") or "",
                }
            )
        return out

    def playlist_name(self, playlist_id):
        if playlist_id == LIKED_SONGS_ID:
            return "Liked Songs"
        data = self._get(f"/playlists/{playlist_id}", {"fields": "name"})
        return data.get("name") or playlist_id

    def _artist_genres(self, artist_ids):
        """Genres come from artist objects, not tracks; batched 50 at a time."""
        genres = {}
        ids = [a for a in dict.fromkeys(artist_ids) if a]
        for i in range(0, len(ids), 50):
            chunk = ids[i : i + 50]
            try:
                data = self._get("/artists", {"ids": ",".join(chunk)})
            except SpotifyError:
                continue  # genres are a bonus; never fail the export over them
            for artist in data.get("artists") or []:
                if artist:
                    genres[artist.get("id")] = artist.get("genres") or []
        return genres

    def playlist_rows(self, playlist_id, with_genres=True):
        """Flatten a playlist into CSV_COLUMNS-shaped dicts."""
        if playlist_id == LIKED_SONGS_ID:
            items = self._paged("/me/tracks", {"limit": 50})
        else:
            # /playlists/{id}/items replaced /playlists/{id}/tracks in
            # Spotify's Feb 2026 migration. The old path now returns 403 for
            # apps in Development Mode, which is every self-hosted install.
            items = self._paged(f"/playlists/{playlist_id}/items", {"limit": 100})

        rows, artist_ids = [], []
        for item in items:
            # "item" on the new playlist endpoint, "track" on /me/tracks and
            # on the old response shape.
            item = item or {}
            track = item.get("item") or item.get("track") or {}
            # Local files and podcast episodes have no usable id/artists.
            if not track or track.get("type") not in (None, "track"):
                continue
            if not track.get("name"):
                continue
            album = track.get("album") or {}
            artists = track.get("artists") or []
            album_artists = album.get("artists") or []
            added_by = (item.get("added_by") or {}).get("id") or ""

            for a in artists:
                if a.get("id"):
                    artist_ids.append(a["id"])

            rows.append(
                {
                    "Track URI": track.get("uri", ""),
                    "Track Name": track.get("name", ""),
                    "Artist URI(s)": ", ".join(
                        a.get("uri", "") for a in artists if a.get("uri")
                    ),
                    "Artist Name(s)": ", ".join(
                        a.get("name", "") for a in artists if a.get("name")
                    ),
                    "Album URI": album.get("uri", ""),
                    "Album Name": album.get("name", ""),
                    "Album Artist URI(s)": ", ".join(
                        a.get("uri", "") for a in album_artists if a.get("uri")
                    ),
                    "Album Artist Name(s)": ", ".join(
                        a.get("name", "") for a in album_artists if a.get("name")
                    ),
                    "Album Release Date": album.get("release_date", ""),
                    "Album Image URL": (album.get("images") or [{}])[0].get("url", "")
                    if album.get("images")
                    else "",
                    "Disc Number": track.get("disc_number", ""),
                    "Track Number": track.get("track_number", ""),
                    "Track Duration (ms)": track.get("duration_ms", ""),
                    "Track Preview URL": track.get("preview_url") or "",
                    "Explicit": track.get("explicit", ""),
                    "Popularity": track.get("popularity", ""),
                    "ISRC": (track.get("external_ids") or {}).get("isrc", ""),
                    "Added By": added_by,
                    "Added At": (item or {}).get("added_at", ""),
                    "Genres": "",
                    "_artist_id": (artists[0].get("id") if artists else ""),
                }
            )

        if with_genres and rows:
            genre_map = self._artist_genres(artist_ids)
            for row in rows:
                row["Genres"] = ", ".join(genre_map.get(row.pop("_artist_id"), [])[:3])
        else:
            for row in rows:
                row.pop("_artist_id", None)

        return rows

    def write_playlist_csv(self, playlist_id, path, with_genres=True):
        rows = self.playlist_rows(playlist_id, with_genres=with_genres)
        if not rows:
            raise SpotifyError("That playlist has no downloadable tracks.")
        # Create the folder only once there is something to put in it. The
        # caller used to make it first, which left an empty directory in the
        # user's music library every time Spotify refused the playlist.
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)
        return len(rows)
