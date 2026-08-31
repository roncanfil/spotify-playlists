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

This folder builds the image from source, which is what you want for
development. **To just run the app on a server or NAS, don't build it here** —
pull the published image instead: see [`../deploy/README.md`](../deploy/README.md).

```bash
cp .env.example .env      # edit port, paths, password
docker compose up -d --build
```

Then open `http://<host>:8765`.

## ⚙️ Configuration

All via `.env` (see [`.env.example`](.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8765` | Port the UI listens on |
| `MUSIC_PATH` | `./data/music` | Where finished music is written |
| `PLAYLIST_PATH` | `./data/playlists` | Where CSVs live; uploads land here |
| `STATE_PATH` | `./data/state` | Persisted yt-dlp upgrades |
| `APP_PASSWORD` | *(empty)* | Blank = no login. Any username; password is checked |
| `AUTO_UPDATE_YTDLP` | `1` | Re-install latest yt-dlp on each start |
| `SPOTIFY_CLIENT_ID` | *(empty)* | Blank hides the Spotify tab |
| `SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:8765/callback` | Must match the Spotify dashboard exactly |

## 🎧 Spotify setup

Optional — without it the CSV tab still works. Create a free app at
[developer.spotify.com/dashboard](https://developer.spotify.com/dashboard), tick
**Web API**, register the redirect URI above **exactly**, and put the Client ID
in `.env`. No client secret: the app uses Authorization Code + PKCE.

**The one-time connect must happen from a browser on the server.** Spotify
refuses `localhost` and requires HTTPS for anything that is not a loopback
address, so `http://127.0.0.1:PORT/callback` is the only plain-HTTP redirect it
accepts — and a phone redirecting there would hit its own loopback, not this
server. If you normally reach the box over SSH, forward the port for the setup
step:

```bash
ssh -L 8765:127.0.0.1:8765 you@server
# then open http://127.0.0.1:8765 locally and click Connect
```

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

Output is one subfolder per CSV, so `lenox.csv` writes to `<MUSIC_PATH>/lenox/`.
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
same without a rebuild. If it still fails, rebuild: `docker compose up -d --build`.

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
