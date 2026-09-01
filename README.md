# 🎵 Spotify playlists → MP3

Takes a Spotify playlist and produces a folder of tagged MP3s, sourcing the
audio from YouTube.

## 📂 Layout

| Folder | What it is |
|---|---|
| [`web-ui/`](web-ui) | **The app.** A Dockerised web UI: connect Spotify, browse playlists, queue downloads. Start here. |
| [`deploy/`](deploy) | **Deployment guide.** CasaOS, Proxmox, updating, and how the image is published. The compose file itself lives in `web-ui/`. |
| [`cli/`](cli) | **Historical.** The original command-line script and a record of why the project moved to a web UI. Not maintained. |

## 🚀 Install

The image is published publicly on GHCR — nothing to build, no login to pull:

```
ghcr.io/roncanfil/spotify-playlists-to-mp3:latest
```

Supports `linux/amd64` and `linux/arm64`.

### CasaOS / ZimaOS / Portainer / Dockge

Paste [`web-ui/docker-compose.yml`](web-ui/docker-compose.yml)
into the UI's custom-install / stack editor. It needs no `.env` — every value is
literal, with four `EDIT` markers for the port, password, Spotify client ID and
volume paths.

### Any Linux box with docker compose

```bash
mkdir -p ~/playlist-downloader && cd ~/playlist-downloader
curl -fsSLO https://raw.githubusercontent.com/roncanfil/spotify-playlists-to-MP3/main/web-ui/docker-compose.yml
nano docker-compose.yml     # set the volume paths and a password
docker compose up -d
```

Then open `http://<host>:8765`.

On **Proxmox**, run that inside a Docker LXC or VM — nothing here needs
privileged mode or host networking.

Deployment details, updating, and the Spotify SSH-tunnel step are in
[`deploy/README.md`](deploy/README.md).

## 🛠 Development

To build from source instead of pulling:

```bash
cd web-ui
docker build -t ghcr.io/roncanfil/spotify-playlists-to-mp3:latest .
docker compose up -d
```

Building under the tag the compose file expects means compose uses your local
build instead of pulling. See [`web-ui/README.md`](web-ui/README.md) for the app
internals and the Spotify redirect-URI constraint.

## ✨ What it does

- **Spotify tab** — connect once, see every playlist with artwork and track
  counts, click Download. No Exportify round-trip.
- **CSV tab** — drop an Exportify CSV instead, if you would rather not connect
  Spotify.
- **Queue tab** — one job at a time, live progress, per-track source quality,
  cancel.
- **Settings tab** — MP3 (128/192/320 kbps) or M4A stream-copy, plus the yt-dlp
  updater.

Output is one folder per playlist. Tracks you already have are skipped, so
re-runs only fetch what is missing. Files get full tags: title, artist, album,
album artist, track number, year, genre, and the source YouTube URL.

## 🔧 When downloads start failing

Expected, periodically — YouTube changes and yt-dlp catches up. Restart the
container (it reinstalls the latest yt-dlp on boot) or use the Settings tab's
update button. This is the fix for almost every breakage; see
[`cli/README.md`](cli/README.md) for the August 2026 SABR episode and how it was
actually resolved.

## ⚖️ Legal notice

For **personal use** only. Respect copyright, YouTube's and Spotify's terms, and
rights holders. Only download what you are entitled to use.
