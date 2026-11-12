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

One path: your music library, mounted in both the app and slskd. Each playlist
becomes a folder inside it holding its tracks, the `.csv` it came from, and a
generated `.m3u`.

The app's own state lives in a hidden `.spotify-playlists/` folder there too:
the Spotify token, the source order, and slskd's settings, database and
downloads in progress. So everything survives updates without a second volume.
yt-dlp's self-updates are deliberately not persisted; they are reinstalled on
every boot.

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

## Soulseek

YouTube tops out near 130 kbps. Soulseek is a peer-to-peer network where people
share their own music collections, so it usually has real 320 kbps MP3s and
lossless files. The app can look there first and fall back to YouTube, or the
other way round.

The app does not speak Soulseek itself. A second container,
[slskd](https://github.com/slskd/slskd), holds the account and the
connections, and the app drives it over HTTP. It ships in the compose file and
needs nothing filled in: the app writes its settings.

### Setting it up

There is nothing to do in the compose file. The first time you open the app, a
setup screen asks where music should come from first:

- **Soulseek**: then asks for a username and password. There is no sign-up;
  the first login creates the account. Use a password you use nowhere else. If
  someone else already has the name, the screen says so and you pick another.
- **YouTube**: nothing more to set up. Soulseek can still be added later.

Whichever comes first, the other fills in what it cannot find. All of this can
be changed later under **Settings → Audio sources**: the order, the account,
sharing, or the whole setup again.

### Not using Soulseek?

Comment out the whole `slskd:` service in the compose file and the container
never starts. The app sees it is missing, offers only YouTube, and says why.

### Where its files live

slskd keeps its settings, database and downloads in
`<music>/.spotify-playlists/slskd/`, inside your music folder, which is why
the slskd service mounts the same music path as the app. Keep the two lines
identical. The folder is hidden, so file browsers and media scanners skip it,
and so does Soulseek sharing.

The settings file there, `slskd.yml`, holds the Soulseek password and the key
the app uses to talk to slskd. It is readable by its owner only, but anyone who
can browse the music folder with that owner's rights can read it, so bear that
in mind before sharing the music folder over your network. The app rewrites the
file whenever the account or sharing changes, so edit those in the app rather
than in the file.

### Port forwarding

slskd listens for other peers on TCP port 50300. Forward that port on your
router to the machine running Docker.

It matters because Soulseek connects people's computers directly, and a home
router blocks incoming connections unless a port is forwarded. When both you
and a peer are blocked, neither can reach the other, and the download fails
within seconds with "the peer did not answer". Many Soulseek users are behind
such routers, so without forwarding you lose all of them. The app moves on to
the next peer and falls back to the other source after a few, so tracks still
arrive, just less often from Soulseek. Every Soulseek client has this
limitation; it is part of how the network works.

The compose file publishes the port already. Only the router step is yours. If
another app on the host already uses 50300, pick a new number and use it in
three places: both sides of the port mapping (`"50301:50301"`), slskd's
`SLSKD_SLSK_LISTEN_PORT`, and the router. slskd tells other peers which port to
use, so the container's port and the host's must be the same.

### Sharing

Soulseek runs on reciprocity: many peers refuse or deprioritise people who share
nothing. So sharing is on by default, and what is shared is your music folder,
the same library both sources download into. New tracks join the share as soon
as a job finishes.

Playlists' `.csv` and `.m3u` files are left out, and so is everything hidden:
the Spotify token and slskd's own files in `.spotify-playlists/` are never
shared. Turn sharing off in the setup screen or under Settings. Sharing means
other people download from you; read the legal notice below.

### slskd's own web UI

Not needed day to day, but useful for seeing transfers and searches as they
happen. Set `SLSKD_USERNAME` and `SLSKD_PASSWORD` on the slskd service, because
the default is `slskd` / `slskd` and a blank password stops slskd from
starting. Then uncomment the `5030` port and open `http://<host>:5030`.

### How it behaves

- **Search limits.** Soulseek bans an account for 30 minutes after more than 34
  searches in 220 seconds. The app spaces its searches to stay under 30 per 240
  seconds, at most two per track, so a long playlist pauses between searches
  now and then rather than risking the ban. Searches you make in slskd's own UI
  count toward the same limit, and the app takes them into account.
- **Picking a file.** Results are ranked by how well the name and length match
  the Spotify track, then by sound quality, then by how soon the peer can send.
  Up to four files are tried per track before the other source takes over.
- **Wrong recordings.** A file more than 10 seconds longer or shorter than
  Spotify's version is rejected after download and the next one is tried. That
  catches live takes, extended mixes and clipped rips that the name alone does
  not.
- **Quality.** The bitrate setting is a maximum. An MP3 at or under it keeps its
  audio untouched; only its tags are replaced. Higher-bitrate MP3s and lossless
  files are encoded down to it. AAC and other lossy files are encoded at what
  holds their own quality, never padded up to the setting. Lossy files under
  128 kbps are refused, since YouTube does as well.
- **Tags.** The peer's own tags and cover art are dropped, and the file is
  tagged from the playlist like any other. The comment names the source, as in
  `Source: Soulseek, peer someone`, where YouTube tracks keep their
  `Original YouTube URL`.
- **MP3 only.** M4A output is a straight copy of YouTube's own stream, which is
  the point of choosing it, so M4A always comes from YouTube.
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

Three images, from two places:

- `ghcr.io/roncanfil/spotify-playlists`, this project, on GitHub Container
  Registry.
- `brainicism/bgutil-ytdlp-pot-provider`, the POT provider sidecar, from Docker
  Hub because upstream publishes nowhere else. It answers YouTube's "confirm
  you're not a bot" challenge, which a server IP trips far more often than a
  home connection, and supports amd64 and arm64.

To drop the sidecar, delete that service, its `depends_on` entry, and
`BGUTIL_POT_BASE_URL`. Everything still works, you will just see more bot
checks.

And a third: `slskd/slskd`, the Soulseek client, from Docker Hub, for amd64
and arm64. Comment out its service to skip it.

## Legal notice

For personal use only. Respect copyright and the terms of YouTube, Spotify and
Soulseek. Downloading music you have no right to is illegal in many countries,
whichever network it comes from. On Soulseek, peers see your IP address, and
sharing a folder distributes its contents to other people, which carries more
legal exposure than downloading. Only take, and only share, what you are
entitled to.
