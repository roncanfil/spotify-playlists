# Spotify Playlists, web UI

Turns a Spotify playlist or an Exportify CSV into a folder of tagged MP3s. Runs
as a Docker service, so anyone on the network can use it from a browser with
nothing installed.

## What it does

- Spotify tab: connect your account once, browse playlists with artwork and
  track counts as a grid or a list, and hit Save CSV to queue one.
- CSV tab: upload a CSV instead and hit Process Playlist.
- Queue tab: one job at a time with live progress, filters for Saved, Skipped
  and Failed, a separate list of failures with their errors, and a per-track
  history for finished playlists that sorts failures first.
- Settings tab: output format and the yt-dlp updater.

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

Repeat both after any change to `app.py`, `download.py`, `spotify.py` or
`templates/`.

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
| `SPOTIFY_CLIENT_ID` | empty | Blank hides the Spotify tab |
| `SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:8765/callback` | Must match the Spotify dashboard exactly, port included |

The music mount is the only path to set. `MUSIC_DIR` is not in the compose file
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
│   └── spotify_token.json                so you connect Spotify only once
└── Lenox Ave/
    ├── Lenox Ave.csv                     the playlist it came from
    ├── Lenox Ave.m3u                     generated after each run
    ├── Tycho - 01 - Sunrise.mp3
    └── Bonobo - 02 - Café Solo.mp3
```

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

## Spotify setup

Optional; the CSV tab works without it. Create a free app at
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

`128`, `192` (default) or `320` kbps MP3, or M4A stream copy. These are
transcode targets, not source quality. YouTube's audio ceiling is around 125 to
130 kbps lossy, so `128` is the only choice that meaningfully degrades anything
and `320` mostly just costs disk. The UI explains each option as you select it.

FLAC is deliberately not offered, since YouTube has no lossless audio to give
and it only ever meant roughly four times the file size for identical sound. See
[`../cli/README.md`](../cli/README.md).

## How it fits together

```
download.py    the engine: search, fetch, transcode, tag, write the .m3u
     ^ imported by
app.py         Flask API: auth, start/cancel/poll, yt-dlp updater
spotify.py     Spotify Web API client (PKCE, no client secret)
templates/     single-page UI, no CDN, works offline on a LAN
```

`process_playlist()` in `download.py` yields progress events. `app.py` records
them per job and serves them as JSON, and the page polls for them. Those same
events back the queue's per-track status lists. `download.py` also still runs
standalone as a deprecated CLI, so one implementation sits behind both.

A playlist's output folder is derived from its CSV's own filename. That is why
naming the CSV after its folder makes the tracks, the CSV and the `.m3u` land
together, and why there is no separate playlist directory to configure.

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

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The UI |
| `GET` | `/healthz` | Health check and app version, unauthenticated |
| `GET` | `/login` | Password page, only present when `APP_PASSWORD` is set |
| `POST` | `/logout` | Clears the session |
| `GET` | `/api/state` | Job progress, playlists, yt-dlp and app versions |
| `GET` | `/api/job/<id>` | One job including its event log |
| `POST` | `/api/start` | Queue a job (`bitrate`, plus `playlist` or `csv` upload) |
| `POST` | `/api/cancel` | Cancel a job by `job_id`, running or waiting |
| `POST` | `/api/cancel-all` | Drop every waiting job, leaving the running one |
| `POST` | `/api/update-ytdlp` | Upgrade yt-dlp into the state volume |
| `GET` | `/api/spotify/login` | Redirects to Spotify's consent screen |
| `GET` | `/callback` | OAuth redirect target |
| `GET` | `/api/spotify/playlists` | Your playlists with artwork and counts |
| `POST` | `/api/spotify/download` | Export a playlist to CSV and queue it |
| `POST` | `/api/spotify/disconnect` | Forget the stored token |

Jobs queue instead of being rejected, and run strictly one at a time. Parallel
YouTube extraction is the quickest way to get rate-limited, and two people using
this at once should not be able to cause that.

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

## Running without Docker

```bash
pip install -r requirements.txt
MUSIC_DIR=/path/to/music \
  waitress-serve --host=0.0.0.0 --port=8765 --threads=8 app:app
```

Needs `ffmpeg` on `PATH`. `download.py` sits beside `app.py`, so it imports the
same way from a checkout as it does in the image.

## Legal notice

For personal use only. Respect copyright, YouTube's and Spotify's terms, and
rights holders.
