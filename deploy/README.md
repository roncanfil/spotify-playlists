# Deploying on a server or NAS

The image is prebuilt and published to GitHub Container Registry, so nothing is
compiled on the server. It is public — pulling needs no login.

```
ghcr.io/roncanfil/spotify-playlists-to-mp3:latest
```

There is exactly one compose file for every deployment target:

**[`../web-ui/docker-compose.yml`](../web-ui/docker-compose.yml)**

It uses no `${VARIABLE}` substitution and needs no `.env`, because the
paste-a-file installers below cannot supply one. Every setting is a literal you
edit in place.

## CasaOS / ZimaOS / Portainer / Dockge

These import a single compose file and give you no `.env`, so
`../web-ui/docker-compose.yml` is written with literal values and no `${VARIABLE}`
substitution anywhere. Paste it as-is and it works.

**CasaOS:** App Store → **Custom Install** → **Import** → paste the file → adjust
the four `EDIT` markers → Install.

**Portainer:** Stacks → **Add stack** → **Web editor** → paste → Deploy.

The four things you may want to change are marked `EDIT 1`–`EDIT 4` in the file:

1. **Host port** — default `8765`. Change the left number only; the container
   always listens on 8765 internally. If you change it, change the port in
   `SPOTIFY_REDIRECT_URI` to match.
2. **`APP_PASSWORD`** — blank means no login. Set it if the box is reachable
   from outside your LAN.
3. **`SPOTIFY_CLIENT_ID`** — blank hides the Playlists tab; the app still works
   by CSV upload.
4. **Volume host paths** — default to the CasaOS convention:

   | Host (default) | Container | Holds |
   |---|---|---|
   | `./music` | `/music` | finished audio, one folder per playlist |
   | `./playlists` | `/playlists` | playlist CSVs |
   | `./state` | `/data` | yt-dlp updates, Spotify token |

   The defaults are relative, so everything lands next to the compose file and
   the app is self-contained. Point the music mount at your library instead —
   on CasaOS or ZimaOS that is usually `/DATA/Media/Music`, with app state under
   `/DATA/AppData/playlist-downloader/`.

## Any Linux box, Proxmox guest, or VPS

```bash
mkdir -p ~/playlist-downloader && cd ~/playlist-downloader
curl -fsSLO https://raw.githubusercontent.com/roncanfil/spotify-playlists-to-MP3/main/web-ui/docker-compose.yml
nano docker-compose.yml    # set the volume paths, and a password if exposed
docker compose up -d
```

The volume defaults are relative to the compose file, so this creates
`./music`, `./playlists` and `./state` in that directory. Point the music mount
at your actual library — `/srv/music`, `/mnt/storage/music`, wherever it is.
Docker creates any missing directories.

## Proxmox

Proxmox has no native container-app store, so run Docker inside a guest and then
follow either section above:

- **LXC** — lighter. An unprivileged container works; if you bind-mount media
  from the host, map the UID/GID so the container can write to it.
- **VM** — better isolated, and avoids LXC nesting and kernel-module quirks.

Nothing here needs privileged mode, host networking, or a specific kernel.

## Updating

```bash
docker compose pull && docker compose up -d
```

In CasaOS, the app's update button does the same thing.

Most breakage does not need an update at all. yt-dlp is the piece YouTube keeps
breaking, and `AUTO_UPDATE_YTDLP=1` reinstalls the latest into the mounted state
volume on every boot — so **restarting the app** is the first thing to try, and
usually the only thing.

## Spotify

Optional. Leave `SPOTIFY_CLIENT_ID` blank and the Playlists tab is hidden; the
app still works by uploading CSVs exported from [Exportify](https://exportify.net).

Spotify's redirect-URI rule is the only fiddly part: **it must be HTTPS**, with
a single exception — a loopback address may use plain HTTP. `localhost` is
rejected outright. That gives two valid setups.

### Option 1 — no domain (SSH tunnel)

Only `http://127.0.0.1:<PORT>/callback` works, so the one-time "Connect" click
has to come from a browser that reaches the app as `127.0.0.1`:

```bash
ssh -L 8765:127.0.0.1:8765 you@server
# then open http://127.0.0.1:8765 on your own machine and click Connect
```

A NAS hostname or LAN IP will not be accepted. This is a one-time step — the
token is saved in the state volume and every user shares it afterwards.

### Option 2 — a real domain over HTTPS (recommended)

Once the app is behind a reverse proxy with a TLS certificate, use the real URL
and the tunnel disappears entirely — anyone can connect from any browser:

```yaml
SPOTIFY_REDIRECT_URI: "https://music.example.com/callback"
```

A plain `http://` domain will **not** work; Spotify requires TLS for anything
that is not a loopback address. Caddy is the least work — two lines of Caddyfile
gets you an automatic Let's Encrypt certificate:

```
music.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

The app needs no changes for this. It generates no absolute URLs, so there is
no `X-Forwarded-Proto` / ProxyFix problem to solve — it only reads the `code`
and `state` query parameters that Spotify appends.

### Either way

Register the exact value in the [Spotify dashboard](https://developer.spotify.com/dashboard)
— scheme, host, port and path all have to match character for character. If you
change the host port or the domain, update it in both the compose file and the
dashboard. No client secret is ever needed: the app uses Authorization Code +
PKCE.

Or skip Spotify entirely: leave `SPOTIFY_CLIENT_ID` blank and upload CSVs
exported from [Exportify](https://exportify.net).

## Publishing a new image (maintainers)

Tagging is all it takes — [`.github/workflows/publish-image.yml`](../.github/workflows/publish-image.yml)
builds `linux/amd64` and `linux/arm64` and pushes to GHCR:

```bash
git tag v1.1.0 && git push origin v1.1.0
```

It authenticates with the per-run `GITHUB_TOKEN`, so no personal access token
is involved. `workflow_dispatch` lets you publish an arbitrary tag by hand.

[`build-and-push.sh`](build-and-push.sh) does the same from a dev machine and is
kept for local testing. It needs a classic PAT with `write:packages`, so prefer
the workflow.

## A note on registries

Two images are pulled, from two places:

- **`ghcr.io/roncanfil/spotify-playlists-to-mp3`** — this project, on GitHub
  Container Registry.
- **`brainicism/bgutil-ytdlp-pot-provider`** — the POT provider sidecar, from
  Docker Hub. Upstream publishes there only; there is no GHCR copy. It solves
  YouTube's "confirm you're not a bot" challenge, which a server IP trips far
  more often than a home connection does, and it supports amd64 and arm64.

To drop the sidecar, delete that service, the `depends_on` entry, and
`BGUTIL_POT_BASE_URL`. Downloads still work; you will just see more bot checks.
