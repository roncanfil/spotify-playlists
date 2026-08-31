# Server deployment (prebuilt image)

The image is built on a dev machine and pushed to a registry; the server only
pulls. That keeps the server side to **two files** — `docker-compose.yml` and
`.env` — with no source checkout, no Dockerfile, and no build toolchain.

To build *on* the server instead, ignore this folder and use
[`../web-ui/docker-compose.yml`](../web-ui/docker-compose.yml), which builds
from source.

## One-time: registry login (dev machine)

The default registry is GHCR. Create a **classic** GitHub PAT with the
`write:packages` scope, then:

```bash
echo "$CR_PAT" | docker login ghcr.io -u <github-username> --password-stdin
```

A new GHCR package is **private** by default. Either make it public on the
package's settings page, or run the same `docker login` on the server (a
`read:packages` PAT is enough there) so it can pull privately.

## Build and push (dev machine)

```bash
./deploy/build-and-push.sh 1.0.0
```

Match the server's architecture — a `linux/amd64` image will not run on ARM:

```bash
PLATFORMS=linux/arm64                ./deploy/build-and-push.sh 1.0.0   # Pi, Graviton
PLATFORMS=linux/amd64,linux/arm64    ./deploy/build-and-push.sh 1.0.0   # both
```

Check with `uname -m` on the server: `x86_64` → amd64, `aarch64` → arm64.

## Deploy (server)

```bash
mkdir -p ~/playlist-downloader && cd ~/playlist-downloader
# copy deploy/docker-compose.yml and deploy/.env.example here, then:
cp .env.example .env
nano .env          # set the three paths, and APP_PASSWORD if exposed
docker compose up -d
docker compose logs -f
```

`MUSIC_PATH`, `PLAYLIST_PATH`, and `STATE_PATH` are required and should be
absolute — compose refuses to start without them, deliberately, so a big music
library never lands on the OS disk by accident.

Open `http://<server>:8765`.

## Updating

```bash
# dev machine
./deploy/build-and-push.sh 1.1.0
# server
docker compose pull && docker compose up -d
```

Rolling back is editing `IMAGE_TAG` in `.env` and running `up -d` again.

Most of the time you will not need any of this. yt-dlp is the piece YouTube
keeps breaking, and `AUTO_UPDATE_YTDLP=1` reinstalls it into the mounted
`STATE_PATH` volume on every boot — so `docker compose restart` fixes the usual
breakage without a new image.

## Spotify on a headless server

Spotify rejects `localhost` and requires HTTPS for any non-loopback address, so
the only plain-HTTP redirect it accepts is `http://127.0.0.1:<PORT>/callback`.
The one-time "Connect to Spotify" click therefore has to come from a browser
that reaches the server as `127.0.0.1` — use an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 you@server
# open http://127.0.0.1:8765 locally, click Connect
```

The token then persists in `STATE_PATH` and all users share it. Whatever
`SPOTIFY_REDIRECT_URI` resolves to must be registered in the Spotify dashboard
**exactly**, port included. If you change `PORT`, the compose default follows it
automatically — but the dashboard entry does not, so update it there too.
