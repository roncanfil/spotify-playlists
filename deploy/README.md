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

## A login page, if you want one

Set `APP_PASSWORD` and the app serves a centred password page before anything
else; leave it blank and the app is wide open. Only the password is checked,
there is no username, and a **Sign out** link appears in the header. `/healthz`
stays outside the gate so container healthchecks and uptime probes keep working
without credentials.

Set it if the app is reachable from anywhere but a LAN you trust.

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
   | `./music` | `/music` | everything that persists |

   **One volume, one decision.** Each playlist becomes a folder under it with
   its tracks, the `.csv` it came from and a generated `.m3u`. The Spotify
   token goes in a hidden `.playlist-downloader/` folder there too, so
   connecting Spotify survives updates without a second volume. yt-dlp's
   self-updates are not persisted at all — they are reinstalled on every boot. The defaults are relative, so they land next to the compose file
   and the app is self-contained.

   Point the music mount at your actual library. Under CasaOS or ZimaOS use
   absolute paths — `/DATA/Media/Music` and
   `/DATA/AppData/playlist-downloader` — because CasaOS chooses the project
   directory, so a relative path ends up somewhere you did not pick.

## Any Linux box, Proxmox guest, or VPS

```bash
mkdir -p ~/playlist-downloader && cd ~/playlist-downloader
curl -fsSLO https://raw.githubusercontent.com/roncanfil/spotify-playlists-to-MP3/main/web-ui/docker-compose.yml
nano docker-compose.yml    # set the volume paths, and a password if exposed
docker compose up -d
```

The music mount defaults to `./music`, relative to the compose file. Point it at
your actual library — `/srv/music`, `/mnt/storage/music`, wherever it is. Docker
creates the directory if it is missing. Nothing else needs a path.

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

Both halves are required. `pull` fetches the new image; `up -d` recreates the
container from it.

> **A restart does not update anything.** Neither does stopping and starting
> the app in CasaOS, or `docker compose restart`. They all reuse the image
> already on disk, so the app comes back byte-identical and it looks as though
> the release did nothing. Only an explicit pull changes the image — in CasaOS
> that means its **Update** action, not the power toggle.

The same applies to editing settings: `docker compose restart` will *not* pick
up a changed environment variable. `docker compose up -d` will, because it
recreates the container.

### Confirming which build is running

```bash
curl -s http://<host>:8765/healthz
# {"ok":true,"version":"1.7.0"}
```

No login needed, so this works from anywhere and is the fastest way to tell a
stale deploy from a real bug. The version also appears in the UI footer beside
the yt-dlp version.

If the version changed but the page looks unchanged, hard-refresh once (⌘⇧R or
a private window). All of the front end's JavaScript is inline in the HTML;
releases from 1.6.0 send `Cache-Control: no-store`, so this is only needed once
to escape a page cached by an earlier build.

### Finding the compose file on the NAS

If CasaOS installed it and you would rather work in a shell:

```bash
cd "$(docker inspect playlist-downloader \
  --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
```

Prefer CasaOS's own settings UI for config changes, though — editing the file by
hand can leave CasaOS's record out of sync with what is running.

### Most breakage needs no update at all

yt-dlp is the piece YouTube keeps breaking, and `AUTO_UPDATE_YTDLP=1`
reinstalls the latest on every boot — so **restarting** is the first thing to
try for failed downloads, and usually the only thing. That is the one job a
restart *is* right for.

## Spotify

Optional. Leave `SPOTIFY_CLIENT_ID` blank and the Playlists tab is hidden; the
app still works by uploading CSVs exported from [Exportify](https://exportify.net).

Spotify apps start in **Development Mode**, which limits what the Web API will
return. Since their February 2026 API migration, that can mean playlists you
merely follow are refused with a 403 while ones you created work fine. Their own
algorithmic playlists — Discover Weekly, Daily Mix, Release Radar — have been
off-limits to the API since late 2024 and will never work. If your own playlists
download but followed ones do not, that is the account restriction rather than a
bug here; a quota extension request on the Spotify dashboard is the only fix.

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
