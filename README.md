# Spotify playlists to MP3

Takes a Spotify playlist and produces a folder of tagged MP3s, sourcing the
audio from YouTube.

## Layout

| Folder | What it is |
|---|---|
| [`web-ui/`](web-ui) | The app. A Dockerised web UI: connect Spotify, browse playlists, queue jobs. Start here. |
| [`deploy/`](deploy) | Deployment guide: CasaOS, Proxmox, updating, publishing. The compose file itself lives in `web-ui/`. |
| [`cli/`](cli) | Historical. The original command-line script, kept as a record of why the project moved to a web UI. Not maintained. |

## Install

The image is published publicly on GHCR, so there is nothing to build and no
login needed to pull it.

```
ghcr.io/roncanfil/spotify-playlists:latest
```

Supports `linux/amd64` and `linux/arm64`.

### CasaOS, ZimaOS, Portainer, Dockge

Paste [`web-ui/docker-compose.yml`](web-ui/docker-compose.yml) into the custom
install or stack editor. It needs no `.env`; every value is a literal, and the
file marks the four worth changing.

### Any Linux box with docker compose

```bash
mkdir -p ~/spotify-playlists && cd ~/spotify-playlists
curl -fsSLO https://raw.githubusercontent.com/roncanfil/spotify-playlists/main/web-ui/docker-compose.yml
nano docker-compose.yml     # music path, and a password if exposed
docker compose up -d
```

Then open `http://<host>:8765`.

On Proxmox, run that inside a Docker LXC or VM. Nothing here needs privileged
mode or host networking.

See [`deploy/README.md`](deploy/README.md) for the rest: updating, storage, and
the Spotify redirect URI.

## What it does

- Spotify tab: connect once, browse playlists with artwork and track counts as
  a grid or a list, and hit Save CSV to queue one.
- CSV tab: upload an Exportify CSV instead, if you would rather not connect
  Spotify.
- Queue tab: one job at a time with live progress, filters for Saved, Skipped
  and Failed, a separate list of failures with their errors, and a per-track
  history for finished playlists that sorts failures first.
- Settings tab: MP3 at 128, 192 or 320 kbps, or M4A stream copy, plus the
  yt-dlp updater.
- Optional password: set `APP_PASSWORD` and the app shows a login page.

### What you get on disk

One self-contained folder per playlist:

```
music/
└── Lenox Ave/
    ├── Lenox Ave.csv                 the playlist it came from
    ├── Lenox Ave.m3u                 generated after every run
    ├── Tycho - 01 - Sunrise.mp3
    └── Bonobo - 02 - Café Solo.mp3
```

Tracks you already have are skipped, so re-runs only fetch what is missing.
Files get full tags: title, artist, album, album artist, track number, year,
genre, and the source YouTube URL. The `.m3u` uses relative paths, so the
folder stays playable if you move it.

## When tracks start failing

This happens periodically as YouTube changes and yt-dlp catches up. Restart the
container, which reinstalls the latest yt-dlp on boot, or use the Settings tab's
update button. That fixes almost everything. See [`cli/README.md`](cli/README.md)
for the August 2026 SABR episode and how it was resolved.

## Updating

```bash
docker compose pull && docker compose up -d
```

Both halves matter. A restart updates nothing, because it reuses the image
already on disk. `curl -s http://<host>:8765/healthz` reports which build is
running. More in [`deploy/README.md`](deploy/README.md).

## Legal notice

For personal use only. Respect copyright, YouTube's and Spotify's terms, and
rights holders. Only take what you are entitled to use.
