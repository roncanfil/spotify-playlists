# 🎵 Playlist Downloader — Web UI

Turns a Spotify/Exportify CSV into a folder of tagged MP3s. Runs as a Docker
service so anyone on the network can use it from a browser with nothing
installed.

## ✨ What it does

- **Spotify tab** — connect your account once, see every playlist with artwork
  and track counts, and hit Download. No Exportify round-trip.
- **CSV tab** — upload or pick a CSV, exactly as before.
- **Queue tab** — one job at a time, with live progress, per-track source
  quality, and cancel.

## 🚀 Run it

To just run the app, use the published image —
[`docker-compose.yml`](docker-compose.yml) already points at it, so there is
nothing to build:

```bash
docker compose up -d
```

See [`../deploy/README.md`](../deploy/README.md) for CasaOS, Proxmox and volume
paths.

### Building from source

Contributors: build the image under the tag the compose file expects, and
compose will use your local build instead of pulling.

```bash
docker build -t ghcr.io/roncanfil/spotify-playlists-to-mp3:latest .
docker compose up -d
```

Repeat both after any change to `app.py`, `download.py`, `spotify.py` or
`templates/`. Then open `http://<host>:8765`.

## ⚙️ Configuration

There is no `.env`. Every setting is a literal value in the `environment:` and
`volumes:` blocks of [`docker-compose.yml`](docker-compose.yml) — edit it in
place. That is what lets the same file be pasted straight into CasaOS,
Portainer or Dockge, which cannot supply a `.env`.

| Setting | Default | Purpose |
|---|---|---|
| host port in `ports:` | `8765` | Port the UI listens on. Left side only |
| `/music` mount | `./music` | Finished audio, one folder per playlist |
| `/data` mount | `./data` | Playlist CSVs, the Spotify token, yt-dlp updates |
| `APP_PASSWORD` | *(empty)* | Blank = no login. Any username; password is checked |
| `AUTO_UPDATE_YTDLP` | `1` | Re-install latest yt-dlp on each start |
| `SPOTIFY_CLIENT_ID` | *(empty)* | Blank hides the Spotify tab |
| `SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:8765/callback` | Must match the Spotify dashboard exactly, port included |

`MUSIC_DIR`, `PLAYLIST_DIR`, `STATE_DIR` and `YTDLP_UPGRADE_DIR` are the paths
*inside* the container and match the mount targets; leave them alone. Note that
`STATE_DIR` cannot move off `/data` without also overriding `PYTHONPATH`, which
the image sets to `/data/ytdlp` so yt-dlp upgrades take precedence over the
bundled copy.

## 🎧 Spotify setup

Optional — without it the CSV tab still works. Create a free app at
[developer.spotify.com/dashboard](https://developer.spotify.com/dashboard), tick
**Web API**, register the redirect URI above **exactly**, and put the Client ID
in the compose file. No client secret: the app uses Authorization Code + PKCE.

Spotify requires the redirect URI to be **HTTPS**, except that a loopback
address may use plain HTTP. `localhost` is refused outright. So either:

- **No domain** — only `http://127.0.0.1:PORT/callback` works, and the one-time
  connect must come from a browser that reaches the app as `127.0.0.1`. A phone
  redirecting there would hit its own loopback, not this server. Forward the
  port over SSH for the setup step:

  ```bash
  ssh -L 8765:127.0.0.1:8765 you@server
  # then open http://127.0.0.1:8765 locally and click Connect
  ```

- **A real domain over HTTPS** — set
  `SPOTIFY_REDIRECT_URI: "https://music.example.com/callback"` and no tunnel is
  needed at all. See [`../deploy/README.md`](../deploy/README.md) for the
  reverse-proxy setup.

After that the refresh token lives in `STATE_PATH/spotify_token.json` and every
visitor uses it, from any device. Only your account ever authenticates, so the
Spotify app never needs users added to its development-mode allowlist.

Downloading a playlist writes a CSV into `PLAYLIST_PATH` first, then queues it.
The CSV uses Exportify's exact column layout plus a `Genres` column that
Exportify omits — so genre tags now populate, and the files stay compatible with
any CSV you already have.

> Spotify's developer terms restrict using their API alongside downloading from
> other sources. Practical risk: your app key could be revoked. Worth knowing
> before you rely on it.

Output is one subfolder per CSV, so `lenox.csv` writes to `<music mount>/lenox/`.
Tracks that already exist are skipped, making re-runs incremental.

## 🎚️ Bitrate

`128`, `192` (default) or `320` kbps MP3. These are transcode targets, not
source quality — YouTube's audio ceiling is ~125–130 kbps lossy, so `128` is the
only choice that meaningfully degrades anything and `320` mostly just costs
disk. The UI explains each option as you select it.

FLAC is deliberately not offered — YouTube has no lossless audio to give, so it
only ever meant ~4x the file size for identical sound. See
[`../cli/README.md`](../cli/README.md).

## 🏗️ How it fits together

```
download.py    ← the engine: search, download, transcode, tag
     ↑ imported by
app.py         ← Flask API: start/cancel/poll, yt-dlp updater
templates/     ← single-page UI, no CDN (works offline on a LAN)
```

`process_playlist()` in `download.py` yields progress events; `app.py` serves
them as JSON and the page polls for them. `download.py` also still runs
standalone as a deprecated CLI, so there is one implementation behind both.

Everything the image needs is in this folder, so the compose build context is
`.` and stays around 76 KB. See [`../cli/README.md`](../cli/README.md) for what
this replaced and why.

## 🔌 Services

- **downloader** — the app. Runs under waitress, single process and
  multi-threaded, because job state lives in memory.
- **bgutil-provider** — generates proof-of-origin tokens, which clear YouTube's
  "confirm you're not a bot" checks that a server IP trips more often than a
  laptop. Optional: delete the service, its `depends_on`, and
  `BGUTIL_POT_BASE_URL` to drop it.

## 🌐 API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The UI |
| `GET` | `/healthz` | Health check (unauthenticated, for Docker) |
| `GET` | `/api/state` | Job progress, playlists, yt-dlp version |
| `GET` | `/api/job/<id>` | One job including its event log |
| `POST` | `/api/start` | Queue a job (`bitrate`, plus `playlist` or `csv` upload) |
| `POST` | `/api/cancel` | Cancel a job by `job_id` (running or waiting) |
| `POST` | `/api/cancel-all` | Drop every *waiting* job; leaves the running one |
| `POST` | `/api/update-ytdlp` | Upgrade yt-dlp into the state volume |
| `GET` | `/api/spotify/login` | Redirects to Spotify's consent screen |
| `GET` | `/callback` | OAuth redirect target |
| `GET` | `/api/spotify/playlists` | Your playlists with artwork and counts |
| `POST` | `/api/spotify/download` | Export a playlist to CSV and queue it |
| `POST` | `/api/spotify/disconnect` | Forget the stored token |

Jobs **queue instead of being rejected**, and run strictly one at a time:
parallel YouTube extraction is the quickest way to get rate-limited, and two
people using this at once should not be able to cause that.

## 🔧 When downloads start failing

This is normal — YouTube changes and yt-dlp catches up. The fix is a yt-dlp
upgrade, not anything in this UI. Restart the container:

```bash
docker compose restart downloader
```

That re-installs the latest yt-dlp. The **Update** button in the UI does the
same without a rebuild. If it still fails, pull a newer image:
`docker compose pull && docker compose up -d`.

## 🖥️ Running without Docker

```bash
pip install -r requirements.txt
MUSIC_DIR=/path/to/music PLAYLIST_DIR=/path/to/csvs \
  waitress-serve --host=0.0.0.0 --port=8765 --threads=8 app:app
```

Needs `ffmpeg` on `PATH`. `download.py` sits beside `app.py`, so it imports the
same way from a checkout as it does in the image.

## ⚖️ Legal notice

For **personal use** only. Respect copyright, YouTube's and Spotify's terms,
and rights holders.
