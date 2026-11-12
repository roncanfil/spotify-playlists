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

## Soulseek (optional)

YouTube tops out near 130 kbps. Soulseek is a peer-to-peer network where people
share their own music collections, so it usually has real 320 kbps MP3s and
lossless files. With it set up, every track is tried there first and YouTube
becomes the fallback for whatever Soulseek cannot supply. Nothing changes until
you set it up.

The app does not speak Soulseek itself. A second container,
[slskd](https://github.com/slskd/slskd), holds the account and the
connections, and the app drives it over HTTP. Both containers share a Docker
volume for finished downloads, so your music folder is still the only path you
set.

Soulseek is used for MP3 output only. M4A is a straight copy of YouTube's own
stream, which is the point of choosing it, so it keeps coming from YouTube.

### Setting it up

1. Choose a Soulseek username and password. There is no sign-up: the first
   login creates the account. Use a password you use nowhere else. If someone
   else already has the name, the login fails and Settings shows Soulseek as
   not logged in; pick another.
2. Make an API key, 16 to 255 characters. The app uses it to talk to slskd.

   ```bash
   openssl rand -hex 24
   ```

3. In the compose file, uncomment:
   - `SLSKD_URL`, `SLSKD_API_KEY` and `SLSKD_DOWNLOADS_DIR` on the app,
   - the `- p2p-downloads:/downloads` line under the app's volumes,
   - the whole `slskd:` service,
   - the `volumes:` block at the bottom.
4. Fill in `SLSKD_SLSK_USERNAME`, `SLSKD_SLSK_PASSWORD`, and the same API key
   on both services.
5. `docker compose up -d`.

The Settings tab then lists Soulseek as **Connected**, with the account it is
logged in as. **Error** comes with the reason, such as slskd being unreachable,
a wrong API key, or a failed login. Settings is also where you set the order:
move YouTube to the top and Soulseek is only asked for what YouTube cannot
find.

### Port forwarding

slskd listens for other peers on TCP port 50300. Forward that port on your
router to the machine running Docker. It works without forwarding, but less
well. A peer behind its own firewall can then neither reach you nor be reached,
and its downloads fail within seconds with "the peer did not answer". The app
moves on to the next peer when that happens, and falls back to YouTube after a
few. With the port forwarded, far fewer peers fail that way.

The compose file publishes the port already. Only the router step is yours. If
another app on the host already uses 50300, pick a new number and use it in
three places: both sides of the port mapping (`"50301:50301"`), slskd's
`SLSKD_SLSK_LISTEN_PORT`, and the router. slskd tells other peers which port to
use, so the container's port and the host's must be the same.

### Sharing

By default slskd shares nothing. Soulseek works on reciprocity, and some peers
refuse or deprioritise people who share nothing. slskd itself has a "leechers"
group for exactly this. Expect some downloads to sit queued or be refused. The
app stops waiting after 60 seconds and tries the next peer, so the cost is time
rather than failures.

To share, mount a folder into slskd read-only and name it. Sharing your library
means other people download from you. Read the legal notice below first.

```yaml
  slskd:
    environment:
      SLSKD_SHARED_DIR: /music
      # Belt and braces: slskd 0.26.2 already skips hidden folders, so the
      # Spotify token in .spotify-playlists/ is not shared, but this keeps it
      # that way if a later version changes.
      SLSKD_SHARE_FILTER: '\.spotify-playlists'
    volumes:
      - ./music:/music:ro       # the same host path as the app's music mount
```

Use the same host path as the app's `/music` line. On CasaOS that means the
absolute path.

### slskd's own web UI

Not needed day to day, but useful for seeing transfers and searches as they
happen. Set `SLSKD_USERNAME` and `SLSKD_PASSWORD` on the slskd service, because
the default is `slskd` / `slskd` and a blank password stops slskd from
starting. Then publish port 5030 and open `http://<host>:5030`.

### How it behaves

- **Search limits.** Soulseek bans an account for 30 minutes after more than 34
  searches in 220 seconds. The app spaces its searches to stay under 30 per 240
  seconds, at most two per track, so a long playlist pauses between searches
  now and then rather than risking the ban. Searches you make in slskd's own UI
  count toward the same limit, and the app takes them into account.
- **Picking a file.** Results are ranked by how well the name and length match
  the Spotify track, then by sound quality, then by how soon the peer can send.
  Up to four files are tried per track before it falls back to YouTube.
- **Wrong recordings.** A file more than 10 seconds longer or shorter than
  Spotify's version is rejected after download and the next one is tried. That
  catches live takes, extended mixes and clipped rips that the name alone does
  not.
- **Quality.** Nothing is upscaled. An MP3 at or below your chosen bitrate
  keeps its audio untouched; only its tags are replaced. Higher-bitrate MP3s and lossless files are
  encoded down to it. Lossy files under 128 kbps are refused, since YouTube does
  as well.
- **Tags.** The peer's own tags and cover art are dropped, and the file is
  tagged from the playlist like any other. The comment names the source, as in
  `Source: Soulseek, peer someone`, where YouTube tracks keep their
  `Original YouTube URL`.
- **After the host sleeps.** slskd can keep saying it is logged in while every
  search comes back empty. After three empty searches in a row the app makes it
  reconnect.
- **Version.** The compose file pins slskd to 0.26.2, the version this was
  tested against. Its API has quirks the app works around, so move to a newer
  one deliberately rather than with `:latest`.

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

If you enable Soulseek, there is a third: `slskd/slskd`, from Docker Hub, which
publishes amd64 and arm64.

## Legal notice

For personal use only. Respect copyright and the terms of YouTube, Spotify and
Soulseek. Downloading music you have no right to is illegal in many countries,
whichever network it comes from. On Soulseek, peers see your IP address, and
sharing a folder distributes its contents to other people, which carries more
legal exposure than downloading. Only take, and only share, what you are
entitled to.
