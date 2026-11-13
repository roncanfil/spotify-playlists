# Spotify playlists to MP3

Takes a Spotify playlist, album or song and produces a folder of tagged MP3s.
Paste a Spotify link, connect your account, or upload a CSV. The audio comes
from Soulseek, for real 320 kbps and lossless sources, and from YouTube, which
needs no account. A setup screen on first visit asks which to try first; the
other fills in whatever the first cannot find.

## Layout

| Folder | What it is |
|---|---|
| [`web-ui/`](web-ui) | The app. A Dockerised web UI: paste Spotify links or browse your playlists, queue jobs. Start here. |
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

- Spotify tab: paste a link to a playlist, an album or a song and hit
  Download. No Spotify account needed; without one a link reads up to the
  first 100 songs of a playlist. Connect your account once and links are read
  through it, complete and with genres, and your own playlists are listed
  below as a grid or a list.
- Keep a playlist in sync: every hour the app checks Spotify and downloads
  songs added since, so a playlist someone keeps adding to stays mirrored in
  its folder. Songs removed on Spotify stay in the folder and leave the `.m3u`.
- A CSV file, at the bottom of the Spotify tab, for a playlist neither a link
  nor your account can reach (an Exportify export, for example).
- Queue tab: one job at a time with live progress, Pause and Resume (the
  queue is saved, so you can update the app while paused and carry on after), filters for Saved, Skipped
  and Failed, a separate list of failures with their errors, and a per-track
  history for finished playlists that sorts failures first. Each saved track
  shows which source it came from.
- Settings tab: MP3 up to 128, 192 or 320 kbps (a maximum: nothing is padded
  above its source's quality), or M4A stream copy; the audio sources with their
  status and order; the Soulseek account and sharing; and the yt-dlp updater.
- Soulseek: set up from the app, nothing to edit in the compose file. See
  [`deploy/README.md`](deploy/README.md#soulseek) for port forwarding and what
  sharing means.
- Optional password: set `APP_PASSWORD` and the app shows a login page.

### What you get on disk

One self-contained folder per playlist, albums under their artist, and songs
pasted on their own in the artist's folder:

```
music/
├── Lenox Ave/                        a playlist
│   ├── Lenox Ave.csv                 the playlist it came from
│   ├── Lenox Ave.m3u                 in the playlist's order
│   ├── Tycho - Sunrise.mp3
│   └── Bonobo - Café Solo.mp3
└── Daft Punk/
    ├── Daft Punk - Get Lucky.mp3     a song pasted on its own
    └── Random Access Memories/       an album
        ├── Random Access Memories.csv
        ├── Random Access Memories.m3u
        └── Daft Punk - 01 - Give Life Back to Music.mp3
```

Only album tracks are numbered. A playlist is a changing mix, so its files are
plain `Artist - Title.mp3` and the `.m3u` carries the order; files named with a
number by older versions are renamed in place, not downloaded again.
Tracks you already have are skipped, so re-runs only fetch what is missing.
Every file is checked against Spotify's length (within 10 seconds) from
either source, so a music video with a long intro or a live take is passed
over. Files get full tags: title, artist, album, album artist, track number, year,
genre, and a comment saying where the audio came from (the YouTube URL, or the
Soulseek peer). The `.m3u` uses relative paths, so the
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

For personal use only. Respect copyright, the terms of YouTube, Spotify and
Soulseek, and rights holders. On Soulseek, peers see your IP address, and
sharing a folder distributes it to others. Only take, and only share, what you
are entitled to use.
