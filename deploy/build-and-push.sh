#!/usr/bin/env bash
# Build the web-ui image on this machine and push it to the registry, so the
# server only ever pulls. Run from a dev machine with Docker running.
#
#   ./deploy/build-and-push.sh 1.0.0              # amd64 (most VPS/x86 servers)
#   PLATFORMS=linux/arm64 ./deploy/build-and-push.sh 1.0.0   # Pi, Graviton, ARM VPS
#   PLATFORMS=linux/amd64,linux/arm64 ./deploy/build-and-push.sh 1.0.0   # both
#
# Every push also moves the `latest` tag, so deploy/.env can stay on `latest`
# for convenience or pin the version tag for reproducibility.
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/roncanfil/spotify-playlists-to-mp3}"
PLATFORMS="${PLATFORMS:-linux/amd64}"
TAG="${1:-}"

if [ -z "$TAG" ]; then
  echo "usage: $0 <version-tag>   e.g. $0 1.0.0" >&2
  exit 1
fi

cd "$(dirname "$0")/.."

if ! docker info >/dev/null 2>&1; then
  echo "error: Docker daemon is not running -- start Docker Desktop first." >&2
  exit 1
fi

# The registry credential has to already be in place; this script never handles
# a token itself. For GHCR that is a classic PAT with write:packages:
#   echo "$CR_PAT" | docker login ghcr.io -u <github-username> --password-stdin
if ! grep -q "$(echo "$IMAGE" | cut -d/ -f1)" ~/.docker/config.json 2>/dev/null \
   && [ "$(docker system info --format '{{.CredentialsStore}}' 2>/dev/null)" = "" ]; then
  echo "warning: no login found for $(echo "$IMAGE" | cut -d/ -f1); push may fail" >&2
fi

# A named builder is required for multi-platform work and harmless otherwise.
docker buildx inspect playlist-builder >/dev/null 2>&1 \
  || docker buildx create --name playlist-builder --driver docker-container >/dev/null

echo "==> building $IMAGE:$TAG for $PLATFORMS"

# --push, not --load: a multi-arch manifest cannot live in the local image
# store, so it goes straight to the registry either way.
docker buildx build \
  --builder playlist-builder \
  --platform "$PLATFORMS" \
  --tag "$IMAGE:$TAG" \
  --tag "$IMAGE:latest" \
  --push \
  web-ui

echo
echo "==> pushed $IMAGE:$TAG  ($PLATFORMS)"
echo "    on the server:  docker compose pull && docker compose up -d"
