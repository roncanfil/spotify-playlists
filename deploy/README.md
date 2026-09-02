# Deploying on a server or NAS

The image is prebuilt, public, and multi-arch (`amd64` and `arm64`). Nothing is
compiled on the server and pulling needs no login.

```
ghcr.io/roncanfil/spotify-playlists:latest
```

One compose file covers every target:
[`../web-ui/docker-compose.yml`](../web-ui/docker-compose.yml). It uses no
`${VARIABLE}` substitution and needs no `.env`, since the paste-a-file
installers below cannot supply one. Every setting is a literal you edit in
place, and the file's comments mark the four worth changing.

## CasaOS, ZimaOS, Portainer, Dockge

Paste the file as-is.

- CasaOS: App Store, Custom Install, Import, paste, Install
- Portainer: Stacks, Add stack, Web editor, paste, Deploy

Set the music path to an absolute one, such as `/DATA/Media/Music` on CasaOS.
The default is relative and CasaOS picks the project directory, so a relative
path lands somewhere you did not choose.

## Any Linux box, Proxmox guest, or VPS

```bash
mkdir -p ~/spotify-playlists && cd ~/spotify-playlists
curl -fsSLO https://raw.githubusercontent.com/roncanfil/spotify-playlists/main/web-ui/docker-compose.yml
nano docker-compose.yml    # music path, and a password if exposed
docker compose up -d
```

On Proxmox, run that inside a Docker LXC or VM. Nothing here needs privileged
mode, host networking, or a particular kernel. For an unprivileged LXC with
host media bind-mounted, map the UID and GID so the container can write.

## Storage

One volume: your music library. Each playlist becomes a folder inside it
holding its tracks, the `.csv` it came from, and a generated `.m3u`.

The Spotify token lives in a hidden `.spotify-playlists/` folder there too, so
connecting Spotify survives updates without a second volume. yt-dlp's
self-updates are deliberately not persisted; they are reinstalled on every boot.

## A login page, if you want one

Set `APP_PASSWORD` and the app shows a password page before anything else.
Leave it blank and the app is open. Only the password is checked, there is no
username, and a Sign out link appears in the header. `/healthz` stays outside
the gate so healthchecks and uptime probes work without credentials.

Set it if the app is reachable from anywhere but a LAN you trust.

## Updating

```bash
docker compose pull && docker compose up -d
```

Both halves matter. `pull` fetches the image, `up -d` recreates the container
from it.

A restart updates nothing. Not `docker compose restart`, and not stopping and
starting the app in CasaOS. They reuse the image already on disk, so the app
comes back identical and it looks as though the release did nothing. In CasaOS
use its Update action, not the power toggle.

Editing settings behaves the same way: `restart` will not pick up a changed
environment variable, `up -d` will.

To confirm which build is running:

```bash
curl -s http://<host>:8765/healthz
# {"ok":true,"version":"1.11.0"}
```

No login needed, so this is the quickest way to tell a stale deploy from a real
bug. The version also shows in the UI footer. If it changed but the page looks
the same, hard-refresh once. The front end is inline in the HTML, though
`Cache-Control: no-store` means that is only needed to escape a page cached by
an older build.

To find the compose file CasaOS created, if you would rather use a shell:

```bash
cd "$(docker inspect spotify-playlists \
  --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
```

Prefer CasaOS's settings UI for config changes, since editing the file by hand
can leave its record out of sync with what is running.

Failing tracks usually need no update at all. yt-dlp is the piece YouTube keeps
breaking, and `AUTO_UPDATE_YTDLP=1` reinstalls it on every boot, so a restart is
the first thing to try. That is the one job a restart is right for.

## Spotify

Optional. Leave `SPOTIFY_CLIENT_ID` blank and the Playlists tab is hidden,
while CSVs from [Exportify](https://exportify.net) still work.

The redirect URI is the fiddly part. It must be HTTPS, except that a loopback
address may use plain HTTP, and `localhost` is rejected outright. Register
whichever you use in the [Spotify dashboard](https://developer.spotify.com/dashboard)
character for character: scheme, host, port and path. No client secret is ever
needed, since the app uses Authorization Code with PKCE.

Without a domain, only `http://127.0.0.1:<PORT>/callback` is accepted, so the
one-time Connect has to come from a browser that reaches the app as `127.0.0.1`:

```bash
ssh -L 8765:127.0.0.1:8765 you@server
# then open http://127.0.0.1:8765 locally and click Connect
```

A NAS hostname or LAN IP will not work. The token is saved afterwards and
shared by everyone.

With a real domain over HTTPS, put the app behind a reverse proxy holding a
certificate and the tunnel is unnecessary. Anyone can connect from any browser.

```yaml
SPOTIFY_REDIRECT_URI: "https://music.example.com/callback"
```

A plain `http://` domain will not work. Caddy is the least effort and gets a
Let's Encrypt certificate automatically:

```
music.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

The app needs no changes for this. It generates no absolute URLs, so there is
no `X-Forwarded-Proto` or ProxyFix problem to solve; `/callback` only reads the
`code` and `state` query parameters.

### Development Mode limits

Spotify apps start in Development Mode. Since their February 2026 API
migration, that can mean playlists you merely follow are refused with a 403
while ones you created work fine. Spotify's own algorithmic playlists, such as
Discover Weekly, Daily Mix and Release Radar, have been off-limits since late
2024 and never will work.

If your own playlists work and followed ones do not, that is the account
restriction rather than a bug here. A quota extension request on the Spotify
dashboard is the only fix.

## Publishing a new image (maintainers)

Tagging is all it takes.
[`.github/workflows/publish-image.yml`](../.github/workflows/publish-image.yml)
builds `linux/amd64` and `linux/arm64` and pushes to GHCR, authenticating with
the per-run `GITHUB_TOKEN`. No personal access token is involved.

```bash
git tag v1.12.0 && git push origin v1.12.0
```

`workflow_dispatch` publishes an arbitrary tag by hand.
[`build-and-push.sh`](build-and-push.sh) does the same from a dev machine but
needs a classic PAT with `write:packages`, so prefer the workflow.

## A note on registries

Two images, from two places:

- `ghcr.io/roncanfil/spotify-playlists`, this project, on GitHub Container
  Registry.
- `brainicism/bgutil-ytdlp-pot-provider`, the POT provider sidecar, from Docker
  Hub because upstream publishes nowhere else. It answers YouTube's "confirm
  you're not a bot" challenge, which a server IP trips far more often than a
  home connection, and supports amd64 and arm64.

To drop the sidecar, delete that service, its `depends_on` entry, and
`BGUTIL_POT_BASE_URL`. Everything still works, you will just see more bot
checks.
