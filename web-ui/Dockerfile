FROM python:3.13-slim

# GHCR reads image.source to link the published package back to the repo, which
# is what makes the package page show the README and inherit repo permissions.
# revision/version/created are passed by deploy/build-and-push.sh, since they
# change every build and would otherwise bust the layer cache.
LABEL org.opencontainers.image.source="https://github.com/roncanfil/spotify-playlists-to-MP3" \
      org.opencontainers.image.url="https://github.com/roncanfil/spotify-playlists-to-MP3" \
      org.opencontainers.image.title="Playlist Downloader" \
      org.opencontainers.image.description="Web UI for downloading Spotify playlists as tagged MP3 or FLAC files." \
      org.opencontainers.image.licenses="NOASSERTION"

# ffmpeg does the audio extraction and the encoding. Debian's `ffmpeg` package
# drags in ~400MB of dependencies we never touch (X11, SDL, GTK, the whole
# desktop stack), so take static binaries from an image that builds nothing but
# those. Same codecs, a third of the weight, and no shared-library surface.
COPY --from=mwader/static-ffmpeg:7.1 /ffmpeg /ffprobe /usr/local/bin/

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# download.py is the engine; app.py is the web layer around it.
COPY download.py spotify.py app.py entrypoint.sh ./
COPY templates/ ./templates/
RUN chmod +x /app/entrypoint.sh

# /data/ytdlp is a mounted volume ahead of site-packages, so yt-dlp upgrades
# survive image rebuilds.
ENV PYTHONPATH=/data/ytdlp \
    PYTHONUNBUFFERED=1 \
    MUSIC_DIR=/music \
    PLAYLIST_DIR=/playlists \
    STATE_DIR=/data \
    YTDLP_UPGRADE_DIR=/data/ytdlp \
    AUTO_UPDATE_YTDLP=1 \
    PORT=8765

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8765')+'/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]
