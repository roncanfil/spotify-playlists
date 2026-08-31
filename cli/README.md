# 📜 cli — historical record

This folder is a **marker, not working code.** It records the command-line tool
this project started as, before it became a web service.

The downloader now lives in [`../web-ui`](../web-ui):

```bash
cd ../web-ui && cp .env.example .env && docker compose up -d --build
```

## 📂 What is in here

| File | What it is |
|---|---|
| `download.py.original` | The original script, exactly as it was before August 2026. **Does not work against current YouTube** — kept for reference only. |
| `README.md` | This file. |

The working engine — the descendant of that script — is now
[`../web-ui/download.py`](../web-ui/download.py). It moved there because the web
app imports it, and keeping one copy next to its only consumer beats a folder
whose sole job was to be imported from elsewhere.

## 🔴 What broke in August 2026

Nearly every download started failing with:

```
ERROR: [youtube] SFE0mMWbA-Y: This video is not available
```

The videos played fine in a browser, which made the message doubly confusing.
The real cause, from yt-dlp's verbose output:

```
Some web client https formats have been skipped as they are missing a URL.
YouTube is forcing SABR streaming for this client.
```

**SABR** is YouTube's newer streaming protocol. Under it the player response
carries no direct stream URLs, so yt-dlp discarded every format and the
fallback player client reported a misleading "not available". It rolled out per
video and per client, which is why only part of the library broke — kids-channel
uploads failed while mainstream tracks kept working.

Two things that were *not* the cause, both tested and ruled out:

- **A stale yt-dlp.** Upgrading 2026.7.4 → 2026.8.19 changed nothing.
- **Missing PO tokens.** A proof-of-origin provider was set up and generated
  tokens successfully; the format lists stayed empty.

## ✅ How it was actually fixed

By retrying with the legacy `android` player client, which still receives an
old-style progressive URL (format `18`, ~128 kbps AAC muxed into 360p video).
That is a handful of lines in the downloader:

```python
FALLBACK_PLAYER_CLIENT = "android"
```

**This fix has nothing to do with the web UI.** It lives in the shared engine,
so the command-line script worked again the moment it landed. A browser front
end cannot change what YouTube serves.

## 🤔 So why move to a web UI?

For reasons unrelated to SABR:

- **Sharing.** Using the script meant Python, a virtualenv, ffmpeg on `PATH`, a
  copy of the file, and comfort with a terminal — per person. The web UI is one
  service on the home server; everyone else opens a URL and installs nothing.
- **Staying current.** YouTube breaks this tool on a recurring basis and the
  remedy is usually a yt-dlp upgrade. The container reinstalls the latest
  yt-dlp on every start into a volume that survives rebuilds, and the UI has an
  update button. With the script, that was a manual `pip install -U` you had to
  know to run.
- **Concurrency.** Two people running the script simultaneously hammered
  YouTube and risked bot-detection. The web UI runs one job at a time.
- **Visibility.** Per-track source quality, failures, and a cancel button in a
  page instead of terminal scrollback.

A Chrome extension was considered and rejected: the Chrome Web Store forbids
YouTube-downloading extensions, so it could not have been shared anyway, and it
would have meant reimplementing yt-dlp plus ffmpeg in JavaScript.

## 🗑️ FLAC was dropped along the way

The original script offered FLAC as a quality option. It never was one — YouTube
serves no lossless audio. The measured ceiling is format `140` (AAC ~129 kbps)
or `251` (opus ~125 kbps), so a FLAC file was a lossless container around lossy
audio: about **4× the size for identical sound** (a 6.8 MB MP3 became 29 MB).

The web UI offers `128`, `192` (default) and `320` kbps MP3 instead. Those are
transcode targets, not source quality — `128` sits at or below the ~130 kbps
source and is the only one that audibly degrades anything.

## 🐛 One bug worth knowing about

The original script looked for a `Release Date` column, but Exportify names it
`Album Release Date`. Every file it produced was therefore tagged with **no
year**. Fixed in the current engine — but anything downloaded before the fix
still has an empty year tag.

## ⚖️ Legal notice

For **personal use** only. Respect copyright, YouTube's and Spotify's terms,
and rights holders.
