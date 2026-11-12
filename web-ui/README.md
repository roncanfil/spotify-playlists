# Spotify Playlists, web UI

Turns a Spotify playlist, album or song, or an Exportify CSV, into a folder of
tagged MP3s. Runs
as a Docker service, so anyone on the network can use it from a browser with
nothing installed. Audio comes from Soulseek first when its optional sidecar is
set up, and from YouTube otherwise.

## What it does

- Spotify tab: paste a playlist, album or song link, check what it found, and
  hit Download; no account needed. Below it, optionally, connect your account
  once to browse your own playlists as a grid or a list and hit Download.
- CSV tab: upload a CSV instead and hit Process Playlist.
- Queue tab: one job at a time with live progress, filters for Saved, Skipped
  and Failed, a separate list of failures with their errors, and a per-track
  history for finished playlists that sorts failures first. Each saved track
  carries a tag naming its source, and a track YouTube supplied after Soulseek
  missed says why Soulseek missed.
- Settings tab: output format, the audio sources (each Connected, Not set up,
  Not installed or Error, in the order they are tried, which you can change),
  the Soulseek account and sharing, and the yt-dlp updater.
- A setup screen on first visit: Soulseek or YouTube first, and the Soulseek
  account if Soulseek. It writes slskd's settings, so the compose file holds
  nothing about Soulseek but the service itself.

The queue's track lists are built from the per-track events each job already
records, so nothing extra is stored to support them.

## Run it

[`docker-compose.yml`](docker-compose.yml) points at the published image, so
there is nothing to build:

```bash
docker compose up -d
```

See [`../deploy/README.md`](../deploy/README.md) for CasaOS, Proxmox and
storage paths.

### Building from source

Build under the tag the compose file expects and compose will use your local
image instead of pulling:

```bash
docker build -t ghcr.io/roncanfil/spotify-playlists:latest .
docker compose up -d
```

Repeat both after any change to the `.py` files or `templates/`.

## Configuration

There is no `.env`. Every setting is a literal value in the `environment:` and
`volumes:` blocks of [`docker-compose.yml`](docker-compose.yml), edited in
place. That is what lets the same file be pasted into CasaOS, Portainer or
Dockge, none of which can supply a `.env`.

| Setting | Default | Purpose |
|---|---|---|
| host port in `ports:` | `8765` | Port the UI listens on. Left side only |
| `/music` mount | `./music` | Everything that persists. The only volume |
| `APP_PASSWORD` | empty | Blank means no login. Otherwise a password page guards the app |
| `AUTO_UPDATE_YTDLP` | `1` | Reinstall the latest yt-dlp on each start |
| `SYNC_INTERVAL` | `3600` | Seconds between checks of playlists kept in sync; `0` turns automatic checks off |
| `SPOTIFY_CLIENT_ID` | empty | Blank hides the Spotify tab |
| `SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:8765/callback` | Must match the Spotify dashboard exactly, port included |
| `SLSKD_URL` | `http://slskd:5030` | Not in the compose file. Only for an slskd somewhere other than the bundled service |

Soulseek has no settings here: the setup screen collects the account and the
app writes slskd's settings file. Port forwarding and sharing are covered in
[`../deploy/README.md`](../deploy/README.md#soulseek).

The music mount is the only path to set, though it appears twice: the app and
slskd both mount it at `/music`, because slskd keeps its files in a hidden
folder inside it (`SLSKD_APP_DIR`) and the two exchange downloads there.
`MUSIC_DIR` is not in the compose file
at all, since the image defaults it to `/music`, as it does
`YTDLP_UPGRADE_DIR`. The file therefore carries only the settings you might
change, plus `BGUTIL_POT_BASE_URL`, which depends on the compose network and so
cannot be defaulted in the image.

There is no `PLAYLIST_DIR`. A playlist is a folder under `MUSIC_DIR` holding its
tracks, the CSV it came from, and a generated `.m3u`. `/data` is fixed rather
than configurable, because the image sets `PYTHONPATH=/data/ytdlp` so yt-dlp
upgrades outrank the bundled copy, and a movable state directory would silently
disable them.

### What the music folder looks like

```
music/
├── .spotify-playlists/
│   ├── spotify_token.json                so you connect Spotify only once
│   ├── settings.json                     source order, and whether setup ran
│   ├── slskd/                            slskd's settings, database, downloads
│   │   └── slskd.yml                     written by the app; owner-only
│   ├── sync.json                         playlists kept in sync
│   └── links/                            CSVs of songs pasted on their own
├── Lenox Ave/                            a playlist
│   ├── Lenox Ave.csv                     the playlist it came from
│   ├── Lenox Ave.m3u                     in the playlist's order
│   ├── Tycho - Sunrise.mp3
│   └── Bonobo - Café Solo.mp3
└── Daft Punk/                            an artist
    ├── Daft Punk - Get Lucky.mp3         a song, pasted as a link
    └── Random Access Memories/           an album, pasted as a link
        ├── Random Access Memories.csv
        ├── Random Access Memories.m3u
        └── Daft Punk - 01 - Give Life Back to Music.mp3
```

Only albums are numbered, by their own track numbers. Playlists, from a link,
the account or a CSV, are named `Artist - Title`: a playlist is a changing mix,
and a number tied each file to a position that moved whenever songs were added,
removed or reordered, which re-downloaded everything after it. The `.m3u`
carries the order instead. Files named with a number by older versions are
renamed in place on the next run. Two songs with the same artist and title in
one playlist share a name, so only the first is downloaded. A song pasted on
its own is named the same way and gets no `.m3u`, which would only pile up in
the artist's folder. Its
CSV is kept in the hidden state folder so the artist's folder holds only music.
Albums and songs file under their first artist, so a song by "Future, Drake"
sits with Future's other music.

The Spotify refresh token is the only thing that has to outlive a container, so
it sits in a hidden folder here rather than in a second volume. It is
dot-prefixed to keep it clear of file browsers and media scanners, and
`list_playlists()` skips dot-prefixed names so it never appears as a playlist.

yt-dlp's self-updates are deliberately not persisted. The entrypoint reinstalls
them on every boot, so they stay in the container's own layer at `/data/ytdlp`,
where the image points `PYTHONPATH`, and need no volume.

The `.m3u` carries an `#EXTINF` duration and `Artist - Title` per entry, with
relative filenames, so the folder can be moved or copied and the playlist still
resolves. It is rewritten at the end of every run from the tracks actually on
disk, so a cancelled or partly failed run still leaves something valid. Tracks
skipped because you already had them are included.

## Spotify links

Pasting a link needs no account and no setup. With your account connected
(see Spotify setup below), a link is read through it, with Spotify's own API:
every song of a long playlist, and album, year and genre on each. A playlist
pasted that way gives exactly the CSV, folder and file names that picking it
from your list gives. If Spotify refuses the account, as it does a Development
Mode app for playlists you did not create, the link is read from the public
pages instead and the preview says why.

Without an account, `spotify_link.py` reads the two public pages any browser
gets for that link, with no token and no API call:

- `open.spotify.com/<kind>/<id>`, which Spotify serves to a non-browser client
  as a rendered page. For an album or a song it carries everything: names,
  artists, track and disc numbers, lengths, release date. For a playlist it
  lists only the first 30 songs.
- `open.spotify.com/embed/<kind>/<id>`, the embeddable player, which lists up
  to 100 songs of a playlist with their lengths but no album.

So without an account a link reads a playlist's first 100 songs, with album
names for the first 30, and no genre on anything. When a playlist is longer the
preview says so; connect your account, or use a CSV, for the rest. Private playlists cannot be read from a link at all.
Shortened `spotify.link` addresses are refused with a note to open them first.
The link becomes a CSV in the same layout as the account export, so the rest
of the app treats it like any other playlist. If Spotify changes those pages,
reading a link fails with a message saying so rather than returning half a
playlist.

The preview is kept for ten minutes, so pressing Download does not fetch the
pages again.

## Keeping playlists in sync

A playlist downloaded from Spotify, pasted or picked from your list, is kept
in sync unless you untick it. Every `SYNC_INTERVAL` seconds (an hour by
default; 0 turns it off) the app reads it again, and when its songs changed it
rewrites the CSV and queues an ordinary job: songs on disk are skipped, new
ones downloaded, and the `.m3u` rewritten in Spotify's order. Songs removed on
Spotify keep their files. Through the connected account an unchanged playlist
costs one small request, since Spotify gives every playlist a version id that
changes with each edit; without the account the public pages are read again.

The Kept in sync panel on the Spotify tab lists them, with Sync now, which
always queues a run and so also retries songs that failed, and Stop, which
leaves the folder as it is. Each keeps the format and bitrate it was first
downloaded with, since the checks run on the server, away from any browser's
settings. The folder is fixed when syncing starts, so renaming the playlist on
Spotify does not start a second copy.

## Spotify setup

Optional; links and the CSV tab work without it. Create a free app at
[developer.spotify.com/dashboard](https://developer.spotify.com/dashboard), tick
Web API, register the redirect URI exactly, and put the Client ID in the compose
file. No client secret is needed, since the app uses Authorization Code with
PKCE.

The redirect URI must be HTTPS, except that a loopback address may use plain
HTTP. `localhost` is refused outright. So either:

- Without a domain, only `http://127.0.0.1:PORT/callback` works, and the
  one-time connect must come from a browser that reaches the app as
  `127.0.0.1`. A phone redirecting there would hit its own loopback, not this
  server. Forward the port over SSH for the setup step:

  ```bash
  ssh -L 8765:127.0.0.1:8765 you@server
  # then open http://127.0.0.1:8765 locally and click Connect
  ```

- With a real domain over HTTPS, set
  `SPOTIFY_REDIRECT_URI: "https://music.example.com/callback"` and no tunnel is
  needed. See [`../deploy/README.md`](../deploy/README.md) for the reverse proxy.

Afterwards the refresh token lives in `.spotify-playlists/spotify_token.json`
inside the music folder, and every visitor uses it from any device. Only your
account ever authenticates, so the Spotify app never needs other users added to
its allowlist.

Queueing a playlist writes its CSV into the playlist's own folder first, then
runs the job. The CSV uses Exportify's column layout plus a `Genres` column
Exportify omits, so genre tags populate and the file stays compatible with any
CSV you already have.

Note that Spotify's developer terms restrict using their API alongside fetching
audio from other sources. The practical risk is that your app key could be
revoked. Worth knowing before relying on it.

### Development Mode limits

New Spotify apps are in Development Mode. Since Spotify's February 2026 Web API
migration, that can mean playlists you merely follow are refused with a 403
while ones you created work normally. Spotify's own algorithmic playlists, such
as Discover Weekly, Daily Mix and Release Radar, have been off-limits to the API
since late 2024 and will never work.

If your own playlists work and followed ones do not, that is the account
restriction rather than a bug here. The only fix is a quota extension request on
the Spotify dashboard.

That migration also renamed the fields this client reads: `tracks` became
`items`, and `/playlists/{id}/tracks` became `/playlists/{id}/items`.
`spotify.py` accepts both shapes, so it works whichever an account is served.

## Bitrate

`128`, `192` (default) or `320` kbps MP3, or M4A stream copy. The MP3 setting is
a maximum, not a target: nothing is encoded above what its source holds.

- An MP3 from Soulseek at or under the maximum keeps its audio untouched (only
  the container and tags are rewritten). Above it, it is encoded down, as are
  lossless files.
- YouTube's audio is Opus or AAC, never MP3, so it is always encoded, at the
  smallest MP3 bitrate that holds it: the source's bitrate plus a fifth, since
  MP3 needs that much more for the same sound (`sources.honest_bitrate`).
  YouTube's usual ~125 to 135 kbps comes out at 160 at a 192 or 320 setting,
  not as a 320 that only looks better. AAC and other lossy files from Soulseek
  follow the same rule.
- `128` caps YouTube below what its audio needs, so it audibly loses a little.

This matters more now that the library is shared on Soulseek, where a 320
kbps label on 130 kbps audio is a "fake 320" that other users rightly dislike.
Soulseek is skipped for M4A output, which exists to copy YouTube's AAC
untouched.

FLAC output is deliberately not offered. YouTube has no lossless audio to give,
and it only ever meant roughly four times the file size for identical sound. See
[`../cli/README.md`](../cli/README.md).

## How it fits together

```
download.py    the engine: walk the sources per track, tag, write the .m3u;
               also the YouTube source (yt-dlp)
sources.py     the source interface, and the P2P candidate loop: rank, try,
               check the length, copy or encode
match.py       which Soulseek file is the track; ported from spotseek (MIT)
slskd.py       Soulseek through slskd's HTTP API, with the search limiter
slskd_config.py  slskd's settings file: API key, account, sharing
sync.py        which playlists are kept in sync, and what was last seen
spotify_link.py  pasted Spotify links, read from the public pages
     ^ imported by
app.py         Flask API: auth, start/cancel/poll, source order, yt-dlp updater
spotify.py     Spotify Web API client (PKCE, no client secret)
templates/     single-page UI, no CDN, works offline on a LAN
tests/         pytest, no network; see Tests below
```

For each track `process_playlist()` tries the sources in the order set in
Settings, and the first to produce a file wins. A source that cannot work at
all, such as slskd being unreachable, is skipped with one warning at the start
of the job rather than failing every track. When every source misses, the
track's error lists each one's reason.

`process_playlist()` in `download.py` yields progress events. `app.py` records
them per job and serves them as JSON, and the page polls for them. Those same
events back the queue's per-track status lists. `download.py` also still runs
standalone as a deprecated CLI, so one implementation sits behind both.

A job's tracks land in its CSV's own folder, and the `.m3u` is named after the
CSV. That is why naming the CSV after its folder makes the tracks, the CSV and
the `.m3u` land together, and why there is no separate playlist directory to
configure. A song pasted on its own is the exception: its job names the
artist's folder and skips the `.m3u`.

With Spotify's length known, YouTube is searched for eight results without
downloading, and the first within 10 seconds of it that is not a live, cover
or remix version (when the title is not one) is downloaded. The file's own
length is checked again before encoding. Without a length it takes the first
result, as it always did.

Everything the image needs is in this folder, so the compose build context is
`.` and stays small. See [`../cli/README.md`](../cli/README.md) for what this
replaced and why.

## Services

- `spotify-playlists`, the app. Runs under waitress, single process and
  multi-threaded, because job state lives in memory.
- `bgutil-provider`, which generates proof-of-origin tokens to clear YouTube's
  "confirm you're not a bot" checks that a server IP trips more often than a
  laptop. Optional: delete the service, its `depends_on`, and
  `BGUTIL_POT_BASE_URL`.
- `slskd`, the Soulseek client. It holds the account and connections, keeps
  its files in `.spotify-playlists/slskd/` inside the music folder, and shares
  the library. Configured by the app, so the service has no settings to fill
  in. Comment it out to run without Soulseek. Pinned to 0.26.2, the version
  the client was verified against.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The UI |
| `GET` | `/healthz` | Health check and app version, unauthenticated |
| `GET` | `/login` | Password page, only present when `APP_PASSWORD` is set |
| `POST` | `/logout` | Clears the session |
| `GET` | `/api/state` | Job progress, playlists, yt-dlp and app versions, and `sources`: the order and each source's status |
| `GET` | `/api/job/<id>` | One job including its event log |
| `POST` | `/api/start` | Queue a job (`bitrate`, plus `playlist` or `csv` upload); `playlist` is a folder from `playlists`, such as `Mix` or `Artist/Album` |
| `POST` | `/api/link/preview` | Read a pasted Spotify link (`link`): what it is, how many songs, where it will be saved; 400 with the reason for a link that cannot be read |
| `POST` | `/api/link/download` | Queue a pasted link (`link`, `bitrate`, `format`; `sync=0` to not keep a playlist in sync) |
| `POST` | `/api/sync/now` | Check a synced playlist now and queue a run (`source`) |
| `POST` | `/api/sync/remove` | Stop syncing a playlist (`source`); its folder is kept |
| `POST` | `/api/cancel` | Cancel a job by `job_id`, running or waiting |
| `POST` | `/api/cancel-all` | Drop every waiting job, leaving the running one |
| `POST` | `/api/update-ytdlp` | Upgrade yt-dlp into the state volume |
| `POST` | `/api/sources/order` | Save the source order (`order=soulseek,youtube`), naming every source once |
| `POST` | `/api/setup/done` | Finish setup: `first=soulseek` or `first=youtube` goes first |
| `POST` | `/api/soulseek/account` | Save the account and log in (`username`, `password`, `share`); 400 if Soulseek refuses |
| `POST` | `/api/soulseek/remove` | Forget the account and log out |
| `POST` | `/api/soulseek/share` | Turn sharing the music folder on or off (`share=1` or `0`) |
| `GET` | `/api/spotify/login` | Redirects to Spotify's consent screen |
| `GET` | `/callback` | OAuth redirect target |
| `GET` | `/api/spotify/playlists` | Your playlists with artwork and counts |
| `POST` | `/api/spotify/download` | Export a playlist to CSV and queue it, kept in sync unless `sync=0` |
| `POST` | `/api/spotify/disconnect` | Forget the stored token |

Jobs queue instead of being rejected, and run strictly one at a time, one track
at a time. Parallel YouTube extraction is the quickest way to get rate-limited,
Soulseek bans accounts that search too fast, and two people using this at once
should not be able to cause either.

Source status is cached for 15 seconds, so polling `/api/state` does not hit
slskd every second and a half.

Every response carries `Cache-Control: no-store`. All the front-end JavaScript
is inline in the HTML, so a cached page would keep running an older release's UI
after an image update, which from the user's side looks identical to the update
having failed.

Two 401s are deliberately different. The app's own "not signed in" reply carries
`{"login": true}`, while `/api/spotify/playlists` returns a plain 401 for "not
connected to Spotify". The front end only redirects to `/login` on the former,
so an expired Spotify token cannot bounce you out of the app.

### Authentication

`APP_PASSWORD` gates the app behind `/login` using a signed session cookie. The
signing key is generated once and stored beside the Spotify token, so restarting
the container does not sign everyone out. HTTP Basic is still accepted, which
keeps `curl` and scripts working:

```bash
curl -u any:yourpassword http://host:8765/api/state
```

The cookie is `SameSite=Lax` rather than `Strict`, because Spotify redirects the
browser back to `/callback` from `accounts.spotify.com` and `Strict` would drop
the cookie on that hop.

### Polling

The UI polls `/api/state` every 1.5s while a job is running, every 20s when
idle, and not at all while the browser tab is hidden, refreshing immediately
when it becomes visible again.

## When tracks start failing

Normal, as YouTube changes and yt-dlp catches up. The fix is a yt-dlp upgrade,
not anything in this UI. Restart the container:

```bash
docker compose restart spotify-playlists
```

That reinstalls the latest yt-dlp. The Update button in the UI does the same
without a restart. If it still fails, pull a newer image:
`docker compose pull && docker compose up -d`.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

Run from this folder. Nothing touches the network: slskd and yt-dlp are faked,
with response bodies copied from what slskd 0.26.2 really returns. The tests
that convert audio generate their fixtures with `ffmpeg` and skip when it is
not on `PATH`.

## Running without Docker

```bash
pip install -r requirements.txt
MUSIC_DIR=/path/to/music \
  waitress-serve --host=0.0.0.0 --port=8765 --threads=8 app:app
```

Needs `ffmpeg` on `PATH`. `download.py` sits beside `app.py`, so it imports the
same way from a checkout as it does in the image.

## Legal notice

For personal use only. Respect copyright, the terms of YouTube, Spotify and
Soulseek, and rights holders. On Soulseek, peers see your IP address, and
sharing a folder distributes it to others.
