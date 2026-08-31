# 🎵 Spotify playlists → MP3

Takes a Spotify playlist and produces a folder of tagged MP3s, sourcing the
audio from YouTube.

## 📂 Layout

| Folder | What it is |
|---|---|
| [`web-ui/`](web-ui) | **The app.** A Dockerised web UI: connect Spotify, browse playlists, queue downloads. Start here. |
| [`cli/`](cli) | **Historical.** The original command-line script and a record of why the project moved to a web UI. Not maintained. |

## 🚀 Quick start

```bash
cd web-ui
cp .env.example .env      # set the port, paths, and optionally Spotify
docker compose up -d --build
```

Open `http://<host>:8765`. Full setup — including the Spotify app registration
and its redirect-URI constraint — is in [`web-ui/README.md`](web-ui/README.md).

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
