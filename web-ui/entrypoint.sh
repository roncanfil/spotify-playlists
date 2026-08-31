#!/bin/sh
# yt-dlp is the part YouTube keeps breaking, so refresh it on boot into the
# mounted volume that PYTHONPATH points at. A restart is therefore also the fix
# for most future breakage.
set -e

if [ "${AUTO_UPDATE_YTDLP:-1}" = "1" ]; then
  echo "[boot] refreshing yt-dlp into ${YTDLP_UPGRADE_DIR:-/data/ytdlp} ..."
  mkdir -p "${YTDLP_UPGRADE_DIR:-/data/ytdlp}"
  pip install --quiet --upgrade --no-cache-dir \
      --target "${YTDLP_UPGRADE_DIR:-/data/ytdlp}" yt-dlp \
    || echo "[boot] update skipped (offline?), using the bundled version"
fi

python -c "import yt_dlp; print('[boot] yt-dlp', yt_dlp.version.__version__)" || true

# Waitress, not gunicorn: job state lives in memory in one process, so the
# server must be single-process and multi-threaded.
exec waitress-serve --host=0.0.0.0 --port="${PORT:-8765}" --threads=8 app:app
