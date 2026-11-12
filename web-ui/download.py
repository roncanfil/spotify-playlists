import csv
import yt_dlp
import os
import re
import argparse
import sys
import threading
import time
import shutil
import subprocess

import match
from sources import (
    MAX_DURATION_DIFF, AudioInfo, Cancelled, Rejected, Request, check_length, convert,
    honest_bitrate, mmss, parse_duration_ms, probe_audio,
)


# YouTube's proof-of-origin requirement means yt-dlp's default player-client
# rotation (visionos / android_vr / web / tv) gets either "This video is not
# available" or an empty format list for some uploads -- notably "made for
# kids" videos. The plain "android" client still serves those, but only as
# legacy format 18 (360p mp4, ~128 kbps AAC), so it is a fallback, not a
# default: normal videos keep giving audio-only opus (format 251).
FALLBACK_PLAYER_CLIENT = "android"

# The MP3 bitrate is a ceiling, not a target: nothing is encoded above what its
# source holds (sources.honest_bitrate). YouTube's ~130 kbps Opus therefore
# comes out at 160 at a 192 or 320 setting, and a Soulseek MP3 at or under the
# setting is kept as it is. 192 is the default: above anything YouTube gives,
# and close to transparent for Soulseek files encoded down to it.
SUPPORTED_FORMATS = ("mp3", "m4a")
DEFAULT_FORMAT = "mp3"
FORMAT_NOTES = {
    "mp3": "Plays anywhere. Nothing is encoded above its source's quality.",
    "m4a": "Copies YouTube's AAC stream untouched -- no re-encode, no quality "
    "loss, smaller files. Always from YouTube; bitrate does not apply.",
}

SUPPORTED_BITRATES = (128, 192, 320)
DEFAULT_BITRATE = 192
BITRATE_NOTES = {
    128: "the most any file gets. Smallest files, but YouTube tracks lose a "
    "little: MP3 needs more than 128 to hold YouTube's ~130 kbps audio",
    192: "the most any file gets. YouTube tracks come out at about 160, "
    "Soulseek's 320s and lossless files are encoded down to 192",
    320: "the most any file gets. Soulseek's 320s are kept as they are; "
    "YouTube tracks still come out at about 160, never padded to 320",
}

_BLOCKED_CLIENT_SIGNS = (
    "this video is not available",
    "requested format is not available",
    "the page needs to be reloaded",
    "no video formats found",
    "unable to extract player response",
    "sign in to confirm",
)


def _is_client_blocked_error(exc):
    """True when a failure looks like YouTube refusing the default clients."""
    msg = str(exc).lower()
    return any(sign in msg for sign in _BLOCKED_CLIENT_SIGNS)


class _QuietLogger:
    """Swallow yt-dlp output: the spinner reports status, exceptions carry detail."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


# When a bgutil POT provider is reachable (the docker-compose sidecar), point
# the plugin at it. PO tokens do not defeat SABR, but they do clear YouTube's
# "sign in to confirm you're not a bot" checks, which a server IP hits more
# often than a laptop does.
POT_BASE_URL = os.environ.get("BGUTIL_POT_BASE_URL", "").strip()


def _player_client_opts(player_client):
    """extractor_args for a forced player client plus any POT provider."""
    args = {}
    if player_client:
        args["youtube"] = {"player_client": [player_client]}
    if POT_BASE_URL:
        args["youtubepot-bgutilhttp"] = {"base_url": [POT_BASE_URL]}
    return {"extractor_args": args} if args else {}


def pick_column(columns, *candidates):
    """Return the first of `candidates` present in `columns`, or None."""
    for name in candidates:
        if name in columns:
            return name
    return None


def read_playlist_csv(csv_file):
    """
    Read a playlist CSV into (header_names, list_of_row_dicts).

    utf-8-sig because Spotify/Exportify exports are BOM-prefixed, and a BOM
    would otherwise glue itself to the first header name and hide it from
    pick_column. Every value stays a string -- unlike pandas, which used to
    coerce a track called "1984" to an int and "NaN" to a float.
    """
    with open(csv_file, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return (reader.fieldnames or []), list(reader)


def cell_str(row, col):
    """Safe string from a CSV cell; empty if missing or NaN."""
    if col is None:
        return ""
    try:
        val = row[col]
    except (KeyError, TypeError):
        return ""
    if val is None:
        return ""
    return str(val).strip()


def sanitize_metadata_value(value):
    """Avoid characters that confuse ffmpeg -metadata key=value parsing."""
    if value is None:
        return ""
    s = str(value).strip().replace("\x00", "")
    # '=' splits key from value; ';' can confuse some parsers
    s = s.replace("=", " ").replace(";", ",")
    return s


def youtube_comment(youtube_url):
    """
    The tag comment for a YouTube track. Worded as it always has been, so
    files from YouTube are tagged the same as before peer-to-peer sources
    existed; those name their network and peer instead (sources.P2PSource).
    """
    url = str(youtube_url or "").strip().replace("\x00", "")
    return f"Original YouTube URL: {url or 'Unknown'}"


def sanitize_comment_text(value):
    """Comment may contain URLs; keep '=' and most characters intact."""
    if value is None:
        return ""
    return str(value).strip().replace("\x00", "").replace("\n", " ")


def safe_meta_text(value):
    """Strip for Mutagen; keep '=' and ';' (real title/album text)."""
    if value is None:
        return ""
    return str(value).strip().replace("\x00", "")


def _split_track_pair(track_tag):
    """'3/12' -> (3, 12); '3' -> (3, 0); anything unparseable -> (0, 0)."""
    raw = safe_meta_text(track_tag)
    num, total = (raw.split("/", 1) + [""])[:2]
    try:
        num_i = int(float(num.strip()))
    except ValueError:
        return 0, 0
    try:
        total_i = int(float(total.strip()))
    except ValueError:
        total_i = 0
    return num_i, total_i


def _ffmpeg_metadata_args(
    meta_title,
    meta_artist,
    meta_album,
    meta_album_artist,
    meta_track_tag,
    meta_date,
    meta_genre,
    comment,
):
    """Flat -metadata args for ffmpeg; values sanitized for key=value argv."""
    meta_title = sanitize_metadata_value(meta_title)
    meta_artist = sanitize_metadata_value(meta_artist)
    meta_album = sanitize_metadata_value(meta_album)
    meta_album_artist = sanitize_metadata_value(meta_album_artist)
    meta_date = sanitize_metadata_value(meta_date)
    meta_genre = sanitize_metadata_value(meta_genre)
    meta_track_tag = sanitize_metadata_value(meta_track_tag)
    comment = sanitize_comment_text(comment)

    args = [
        "-metadata",
        f"title={meta_title}",
        "-metadata",
        f"artist={meta_artist}",
        "-metadata",
        f"album={meta_album}",
        "-metadata",
        f"comment={comment}",
    ]
    if meta_track_tag:
        args.extend(["-metadata", f"track={meta_track_tag}"])
    if meta_album_artist:
        args.extend(["-metadata", f"album_artist={meta_album_artist}"])
    if meta_date:
        args.extend(["-metadata", f"date={meta_date}"])
    if meta_genre:
        args.extend(["-metadata", f"genre={meta_genre}"])
    return args


def _write_tags_mutagen(
    path,
    meta_title,
    meta_artist,
    meta_album,
    meta_album_artist,
    meta_track_tag,
    meta_date,
    meta_genre,
    comment,
):
    from mutagen.id3 import COMM, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, ID3
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4

    meta_title = safe_meta_text(meta_title)
    meta_artist = safe_meta_text(meta_artist)
    meta_album = safe_meta_text(meta_album)
    meta_album_artist = safe_meta_text(meta_album_artist)
    meta_track_tag = safe_meta_text(meta_track_tag)
    meta_date = safe_meta_text(meta_date)
    meta_genre = safe_meta_text(meta_genre)
    comment = safe_meta_text(comment)

    ext = os.path.splitext(path)[1].lower()
    enc = 3  # UTF-8 (ID3v2.4)

    if ext == ".mp3":
        audio = MP3(path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        tags.delall("TIT2")
        tags.delall("TPE1")
        tags.delall("TALB")
        tags.delall("TPE2")
        tags.delall("TRCK")
        tags.delall("TDRC")
        tags.delall("TCON")
        tags.delall("COMM")
        tags.add(TIT2(encoding=enc, text=meta_title))
        tags.add(TPE1(encoding=enc, text=meta_artist))
        tags.add(TALB(encoding=enc, text=meta_album))
        if meta_album_artist:
            tags.add(TPE2(encoding=enc, text=meta_album_artist))
        if meta_track_tag:
            tags.add(TRCK(encoding=enc, text=meta_track_tag))
        if meta_date:
            tags.add(TDRC(encoding=enc, text=meta_date))
        if meta_genre:
            tags.add(TCON(encoding=enc, text=meta_genre))
        tags.add(COMM(encoding=enc, lang="eng", desc="", text=comment))
        audio.save(v2_version=3)
        return

    if ext in (".m4a", ".mp4", ".m4b"):
        audio = MP4(path)
        audio["\xa9nam"] = meta_title
        audio["\xa9ART"] = meta_artist
        audio["\xa9alb"] = meta_album
        if meta_album_artist:
            audio["aART"] = meta_album_artist
        # MP4 stores track as a (number, total) tuple, not free text.
        num, total = _split_track_pair(meta_track_tag)
        if num:
            audio["trkn"] = [(num, total)]
        if meta_date:
            audio["\xa9day"] = meta_date
        if meta_genre:
            audio["\xa9gen"] = meta_genre
        audio["\xa9cmt"] = comment
        audio.save()
        return

    raise ValueError(f"Mutagen path not implemented for extension {ext!r}")


def _write_tags_ffmpeg(
    path,
    meta_title,
    meta_artist,
    meta_album,
    meta_album_artist,
    meta_track_tag,
    meta_date,
    meta_genre,
    comment,
):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH (needed for metadata fallback)")

    meta_args = _ffmpeg_metadata_args(
        meta_title,
        meta_artist,
        meta_album,
        meta_album_artist,
        meta_track_tag,
        meta_date,
        meta_genre,
        comment,
    )
    tmp_path = path + ".meta.tmp"
    try:
        cmd = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            path,
            "-map",
            "0",
            "-codec",
            "copy",
            *meta_args,
            tmp_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        os.replace(tmp_path, path)
    finally:
        if os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def write_playlist_tags(
    path,
    meta_title,
    meta_artist,
    meta_album,
    meta_album_artist,
    meta_track_tag,
    meta_date,
    meta_genre,
    comment,
):
    """
    Embed playlist-sourced tags: Mutagen first, ffmpeg remux fallback.
    `comment` names where the audio came from; see youtube_comment().
    """
    try:
        _write_tags_mutagen(
            path,
            meta_title,
            meta_artist,
            meta_album,
            meta_album_artist,
            meta_track_tag,
            meta_date,
            meta_genre,
            comment,
        )
    except Exception as e_mut:
        try:
            _write_tags_ffmpeg(
                path,
                meta_title,
                meta_artist,
                meta_album,
                meta_album_artist,
                meta_track_tag,
                meta_date,
                meta_genre,
                comment,
            )
        except subprocess.CalledProcessError as e_ff:
            err = (e_ff.stderr or "").strip()
            detail = f" {err}" if err else ""
            raise RuntimeError(
                f"Metadata write failed (mutagen: {e_mut!r}; ffmpeg exit {e_ff.returncode}){detail}"
            ) from e_ff
        except Exception as e_ff:
            raise RuntimeError(
                f"Metadata write failed (mutagen: {e_mut!r}; ffmpeg: {e_ff!r})"
            ) from e_ff
        print(f"   ⚠️  Mutagen failed ({e_mut}); applied tags with ffmpeg instead")


def parse_track_number_cell(raw):
    """
    Parse track index (and optional total) from CSV.
    Accepts '3', '3/12', '03', floats from Excel as '1.0'.
    Returns (index_int_or_none, total_str_or_none).
    """
    raw = (raw or "").strip()
    if not raw or raw.lower() == "nan":
        return None, None
    total = None
    if "/" in raw:
        left, right = raw.split("/", 1)
        raw = left.strip()
        total = right.strip() or None
    try:
        n = int(float(raw))
    except ValueError:
        return None, total
    # Spotify returns track_number 0 for tracks it has no album position for,
    # and our exporter writes that straight through. Treat it as absent rather
    # than a real position, or it renders as "000".
    if n <= 0:
        return None, total
    return n, total


def build_track_display_and_tag(
    row,
    playlist_index,
    playlist_len,
    track_num_col,
    total_tracks_col,
):
    """
    Filename uses zero-padded playlist/album track index.
    FFmpeg 'track' tag uses N or N/T when we can infer T reliably.
    """
    raw_cell = cell_str(row, track_num_col) if track_num_col else ""
    num_from_csv, total_from_slash = parse_track_number_cell(raw_cell)
    total_from_col = cell_str(row, total_tracks_col) if total_tracks_col else ""

    if num_from_csv is not None:
        n = num_from_csv
        if total_from_slash:
            tag = f"{n}/{total_from_slash}"
        elif total_from_col:
            try:
                t = int(float(total_from_col))
                tag = f"{n}/{t}"
            except ValueError:
                tag = str(n)
        else:
            # Album track without total — store index only (avoid wrong N/playlist_len).
            tag = str(n)
    elif track_num_col:
        # The CSV numbers its tracks but this row's value is unusable. Filling
        # in the playlist position would sit a positional number among album
        # numbers, so leave it out of both the filename and the tag.
        return "", ""
    else:
        # No track-number column at all, so playlist order is the only
        # ordering there is.
        n = playlist_index
        tag = f"{n}/{playlist_len}"

    width = max(2, len(str(playlist_len)))
    padded = f"{n:0{width}d}"
    return padded, tag

class LoadingSpinner:
    """Simple loading spinner with three dots animation"""
    def __init__(self, message="Loading"):
        self.message = message
        self.running = False
        self.thread = None
        
    def _animate(self):
        dots = 0
        while self.running:
            spinner = "." * (dots % 4)  # 0, 1, 2, 3 dots
            print(f"\r{self.message}{spinner}   ", end="", flush=True)
            dots += 1
            time.sleep(0.5)
    
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._animate)
        self.thread.daemon = True
        self.thread.start()
    
    def stop(self, ok=True):
        self.running = False
        if self.thread:
            self.thread.join()
        suffix = "✅" if ok else "❌"
        print(f"\r{self.message}... {suffix}", flush=True)

def get_output_dir(csv_file):
    """Get output directory name from CSV filename"""
    # Remove path and extension to get just the filename
    filename = os.path.basename(csv_file)
    name_without_ext = os.path.splitext(filename)[0]
    return name_without_ext

def sanitize_filename(filename):
    """Remove invalid characters from filename"""
    return re.sub(r'[<>:"/\\|?*]', '', filename)

def output_basename(artist, track_padded, track_title):
    """
    Filesystem stem: "artist - NN - title", or "artist - title" when there is
    no track number, rather than padding a missing one to "000".
    """
    parts = [artist, track_padded, track_title] if track_padded else [artist, track_title]
    return sanitize_filename(" - ".join(parts))


def _track_seconds(path):
    """Duration in whole seconds, or -1 if it cannot be read."""
    try:
        from mutagen import File as MutagenFile

        probe = MutagenFile(path)
        if probe is not None and probe.info is not None:
            return int(round(probe.info.length))
    except Exception:
        pass
    return -1


def write_m3u(output_dir, playlist_name, entries):
    """
    Write "<playlist_name>.m3u" into output_dir, listing `entries` in playlist
    order. Returns the path written, or None if there was nothing to write.

    `entries` is a list of (filename, artist, title). Filenames are stored
    relative, so the folder can be moved or copied and the playlist still
    resolves.

    Extended M3U (#EXTINF) rather than a bare filename list: it is what makes
    players show "Artist - Title" and a scrub length instead of a filename.
    Durations are read from the files, so a cancelled or partial run still
    yields a valid playlist of whatever actually landed. Files that have gone
    missing are skipped rather than written as dead entries.
    """
    present = [e for e in entries if os.path.exists(os.path.join(output_dir, e[0]))]
    if not present:
        return None

    lines = ["#EXTM3U"]
    for filename, artist, title in present:
        seconds = _track_seconds(os.path.join(output_dir, filename))
        label = f"{artist} - {title}" if artist and title else (title or artist or filename)
        lines.append(f"#EXTINF:{seconds},{label}")
        lines.append(filename)

    path = os.path.join(output_dir, f"{playlist_name}.m3u")
    # UTF-8 without BOM. Strictly .m3u8 is the UTF-8 variant, but .m3u is what
    # players and NAS scanners actually look for, and they read UTF-8 fine.
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def song_exists(output_dir, basename, ext):
    """Check if song already exists in the output directory"""
    path = os.path.join(output_dir, f"{basename}.{ext}")
    return os.path.exists(path)


def normalize_audio_format(audio_format):
    """Coerce an output format to one we support; raises ValueError otherwise."""
    value = str(audio_format or DEFAULT_FORMAT).strip().lower()
    if value not in SUPPORTED_FORMATS:
        allowed = ", ".join(SUPPORTED_FORMATS)
        raise ValueError(f"Unsupported format {value!r}; choose one of {allowed}")
    return value


def normalize_bitrate(bitrate):
    """Coerce a bitrate to one we support; raises ValueError on anything else."""
    try:
        value = int(bitrate)
    except (TypeError, ValueError):
        raise ValueError(f"Bitrate must be a number, got {bitrate!r}") from None
    if value not in SUPPORTED_BITRATES:
        allowed = ", ".join(str(b) for b in SUPPORTED_BITRATES)
        raise ValueError(f"Unsupported bitrate {value}; choose one of {allowed}")
    return value


# Suffix of YouTube's raw audio before it is encoded to MP3.
YT_SOURCE_SUFFIX = ".yt-source"


def create_ydl_opts(
    output_dir,
    basename,
    bitrate=DEFAULT_BITRATE,
    audio_format=DEFAULT_FORMAT,
    player_client=None,
):
    """
    yt-dlp options: audio only; tags are written afterward (Mutagen / ffmpeg).

    M4A is extracted by yt-dlp itself, as a stream copy. For MP3, yt-dlp only
    downloads YouTube's own audio, to a side file, and YouTubeSource encodes
    it: the bitrate depends on the stream yt-dlp picked, which is not known
    until it has picked one (see sources.honest_bitrate).
    """
    audio_format = normalize_audio_format(audio_format)

    if audio_format == "m4a":
        # Prefer an AAC source so FFmpegExtractAudio can stream-copy it instead
        # of transcoding -- that is the whole point of picking m4a. Falls back to
        # whatever exists (e.g. the SABR-only progressive format 18, also AAC).
        fmt = "bestaudio[ext=m4a]/bestaudio/best"
        postprocessors = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
        outtmpl = f"{output_dir}/{basename}.%(ext)s"
    else:
        normalize_bitrate(bitrate)
        fmt = "bestaudio/best"
        postprocessors = []
        # A name of its own, so an existing .m4a of the same track is never
        # mistaken by yt-dlp for this download, or overwritten by it.
        outtmpl = f"{output_dir}/{basename}{YT_SOURCE_SUFFIX}.%(ext)s"
    pp_args = {}

    return {
        "format": fmt,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": postprocessors,
        "writethumbnail": False,
        "writeinfojson": False,
        "embedsubtitles": False,
        "writeautomaticsub": False,
        "noprogress": True,
        "logger": _QuietLogger(),
        "postprocessor_args": pp_args,
        **_player_client_opts(player_client),
    }


def search_queries(query):
    """
    Search terms to try, most specific first.

    Spotify joins collaborators with ", ", so a track credited to
    "evroulth, Chillhop World" becomes a query no YouTube video matches.
    Dropping the extra artists usually finds it. We deliberately do not fall
    back to the bare title: that starts matching covers and unrelated songs,
    and a wrong file is worse than a missing one.
    """
    out = [query]
    artist, sep, title = query.partition(" - ")
    if sep and ", " in artist:
        first = artist.split(", ")[0].strip()
        if first:
            out.append(f"{first} - {title}".strip())
    seen, uniq = set(), []
    for q in out:
        if q and q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq


def _first_video_entry(info):
    """
    Unwrap a ytsearch result down to the video's info dict, or None.

    A search that matched nothing still returns a container dict, just with an
    empty "entries" list. Returning that container looked like a video to every
    caller, which is how "no results" used to surface as a missing output file
    much further down. Anything still carrying "entries" is a container, not a
    video, so it yields None.
    """
    while isinstance(info, dict) and "entries" in info:
        entries = info.get("entries")
        if not isinstance(entries, list) or not entries:
            return None
        info = entries[0]
    return info if isinstance(info, dict) else None


def describe_source(entry):
    """
    (label, is_muxed) for the stream yt-dlp actually downloaded.

    A muxed stream (progressive, e.g. format 18) carries video too, so its
    audio is the weakest option YouTube offers -- worth surfacing, because it
    is the ceiling for SABR-locked videos.
    """
    if not entry:
        return "unknown source", False
    downloads = entry.get("requested_downloads") or []
    fmt = downloads[0] if downloads else entry
    fmt_id = str(fmt.get("format_id") or "?")
    acodec = (fmt.get("acodec") or "?").split(".")[0]
    is_muxed = (fmt.get("vcodec") or "none") != "none"
    abr = fmt.get("abr")
    rate = f"{float(abr):.0f} kbps" if abr else "bitrate not reported"
    if is_muxed:
        # Progressive streams rarely report abr separately; do not guess one.
        height = fmt.get("height")
        res = f"{height}p" if height else "muxed"
        return f"{acodec} ({res} muxed, format {fmt_id}, {rate})", True
    return f"{acodec} {rate} (audio-only, format {fmt_id})", False


# Search results looked at when Spotify's length is known. The plain audio
# upload ("Artist - Topic") is nearly always in the first few.
LENGTH_SEARCH_RESULTS = 8


def pick_by_length(query, track):
    """
    (watch URL, None) for the first search result that is the track's length
    and not another version of it, or (None, reason) when none is. One search
    request; nothing is downloaded. YouTube's own order is kept, because its
    first fitting result is usually the official upload.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{LENGTH_SEARCH_RESULTS}:{query}", download=False)
    entries = [e for e in (info or {}).get("entries") or [] if isinstance(e, dict)]
    if not entries:
        return None, f"No YouTube result for {query!r}"
    wanted = match.norm(track.title)
    closest = None
    for e in entries:
        url = e.get("url") or e.get("webpage_url")
        if not url and e.get("id"):
            url = f"https://www.youtube.com/watch?v={e['id']}"
        title = match.norm(e.get("title") or "")
        if not url or any(
            match.has_words(title, w) and not match.has_words(wanted, w)
            for w in match.VERSION_WORDS
        ):
            continue
        length = e.get("duration")
        if not length:
            # Live streams and some shorts list no length; nothing to check.
            continue
        diff = abs(float(length) - track.duration)
        if diff <= MAX_DURATION_DIFF:
            return url, None
        if closest is None or diff < closest[0]:
            closest = (diff, float(length), e.get("title") or url)
    if closest is None:
        return None, f"No YouTube result for {query!r} is the song itself (only other versions)"
    return None, (
        f"No YouTube result for {query!r} is the right length: Spotify says "
        f"{mmss(track.duration)}, the closest is {mmss(closest[1])} ({closest[2]})"
    )


class YouTubeSource:
    """
    YouTube via yt-dlp: always available, every output format. The last
    source in the list, so it is the fallback when the P2P networks miss.
    """

    name = "youtube"
    label = "YouTube"
    formats = SUPPORTED_FORMATS

    def check(self):
        return None

    def fetch(self, req):
        """
        Download one song to req.target_path. Returns a result dict describing
        the source; raises on failure. Prints nothing -- callers own
        presentation, and process_playlist writes the tags.
        """
        output_dir, basename = req.output_dir, req.basename
        query, bitrate, audio_format = req.query, req.bitrate, req.audio_format
        # One extract_info(download=True) does the search, the download, and
        # hands back the watch URL plus the chosen format -- the old code
        # searched once to tag the URL and again to download, doubling
        # requests to YouTube.
        entry = None
        used_fallback = False
        used_query = query
        last_exc = None
        duration = req.track.duration if req.track else 0
        misses = []
        tried = search_queries(query)
        for candidate in tried:
            # With Spotify's length known, pick the video first, from a short
            # list, so a music video with a long intro or a live take is passed
            # over for the plain audio upload that is nearly always among the
            # first results. Without it, the first result, as always.
            target = f"ytsearch1:{candidate}"
            if duration:
                try:
                    target, why = pick_by_length(candidate, req.track)
                except Exception as e:
                    last_exc = e
                    break
                if target is None:
                    misses.append(why)
                    continue
            for player_client in (None, FALLBACK_PLAYER_CLIENT):
                opts = create_ydl_opts(
                    output_dir, basename, bitrate, audio_format, player_client
                )
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(target, download=True)
                    entry = _first_video_entry(info)
                    last_exc = None
                    used_fallback = player_client is not None
                    used_query = candidate
                    break
                except Exception as e:
                    last_exc = e
                    # Only the default attempt is worth retrying, and only when
                    # the error says YouTube rejected the client rather than
                    # the video.
                    if player_client is None and _is_client_blocked_error(e):
                        continue
                    break
            # A search that matched nothing raises nothing, so check for it
            # here rather than letting it surface later as a missing file.
            if entry is not None:
                break
            if last_exc is not None:
                break
        if last_exc is not None:
            raise last_exc
        if entry is None:
            if misses:
                raise LookupError(misses[0])
            extra = "" if len(tried) == 1 else f" (also tried {tried[-1]!r})"
            raise LookupError(f"No YouTube result for {query!r}{extra}")

        youtube_url = (entry or {}).get("webpage_url") or "Unknown"
        source_label, is_muxed = describe_source(entry)

        out_path = req.target_path
        if duration:
            # The length YouTube's search listed is checked again on the file
            # itself, before any encoding: a wrong recording is worse than a
            # missing one.
            self._check_file_length(entry, out_path if audio_format != "mp3" else None, duration)
        if audio_format == "mp3":
            source_label += self._encode(entry, req)
        if not os.path.isfile(out_path):
            # The search matched and yt-dlp reported no error, so this is the
            # extract/transcode step having produced nothing usable.
            raise FileNotFoundError(
                f"yt-dlp matched {youtube_url} but produced no "
                f"{normalize_audio_format(audio_format)} file"
            )

        return {
            "path": out_path,
            "youtube_url": youtube_url,
            "source_label": source_label,
            "is_muxed": is_muxed,
            "used_fallback": used_fallback,
            "used_query": used_query,
            "comment": youtube_comment(youtube_url),
        }

    def _check_file_length(self, entry, path, duration):
        """
        Raise LookupError, deleting the download, when the file is not the
        track's length. `path` is the finished file, or None for YouTube's raw
        audio (MP3 output). A file ffprobe cannot read is left for the next
        step to report.
        """
        path = path or _downloaded_path(entry)
        info = probe_audio(path) if path and os.path.isfile(path) else None
        if info is None or not info.length:
            return
        try:
            check_length(info, duration)
        except Rejected as e:
            try:
                os.remove(path)
            except OSError:
                pass
            raise LookupError(f"the YouTube match is the wrong recording: {e}") from None

    def _encode(self, entry, req):
        """
        Encode YouTube's own audio to req.target_path at the honest bitrate, and
        return the label suffix. The stream's bitrate comes from yt-dlp's format
        data; when YouTube did not report one, the file is probed.
        """
        raw = _downloaded_path(entry)
        if not raw or not os.path.isfile(raw):
            raise FileNotFoundError("yt-dlp reported a download but left no audio file")
        try:
            info = probe_audio(raw)
            downloads = (entry or {}).get("requested_downloads") or []
            fmt = downloads[0] if downloads else (entry or {})
            kbps = int(round(float(fmt.get("abr") or 0))) or (info.kbps if info else 0)
            if info is None:
                info = AudioInfo(kind="lossy", codec=str(fmt.get("acodec") or "?"), kbps=kbps,
                                 length=0.0, sample_rate=int(fmt.get("asr") or 0),
                                 channels=int(fmt.get("audio_channels") or 0), bits=0)
            rate = honest_bitrate(kbps, req.bitrate)
            convert(raw, req.target_path, "encode", rate, info)
        finally:
            try:
                os.remove(raw)
            except OSError:
                pass
        return f", encoded to {rate} kbps"


def _downloaded_path(entry):
    """Where yt-dlp saved the stream, or None."""
    for d in (entry or {}).get("requested_downloads") or []:
        if d.get("filepath"):
            return d["filepath"]
    return (entry or {}).get("filepath")


def default_sources(env=None):
    """
    The sources to try, in order: each peer-to-peer network whose sidecar is
    configured, then YouTube. With no P2P variables set this is YouTube alone,
    exactly as before those networks existed.
    """
    env = os.environ if env is None else env
    out = []
    if env.get("SLSKD_URL", "").strip():
        # Imported here so a YouTube-only install never loads the P2P code.
        import slskd
        from sources import P2PSource

        out.append(P2PSource(slskd.SlskdClient.from_env(env)))
    out.append(YouTubeSource())
    return out


def resolve_columns(columns):
    """
    Map a CSV's actual headers onto the fields we tag with.
    Returns (columns_dict, warnings_list); raises ValueError if unusable.
    """
    cols = {
        "track": pick_column(columns, "Track Name", "Name", "Title"),
        "artist": pick_column(columns, "Artist Name(s)", "Artist", "Artists"),
        "album": pick_column(columns, "Album Name", "Album"),
        "track_num": pick_column(
            columns, "Track Number", "Track #", "Track No.", "Track No",
            "Position", "#", "Index",
        ),
        "total_tracks": pick_column(columns, "Total Tracks", "Album Track Count"),
        "album_artist": pick_column(
            columns, "Album Artist Name(s)", "Album Artist(s)", "Album Artist"
        ),
        # Spotify/Exportify exports name this "Album Release Date"; without it
        # every track was silently tagged with no year.
        "release_date": pick_column(
            columns, "Release Date", "Album Release Date", "Year", "Date"
        ),
        "genre": pick_column(columns, "Genres", "Genre"),
        # Our own export writes "Track Duration (ms)", Exportify "Duration
        # (ms)". Optional: only the P2P sources use it, to tell the song from
        # a live take or an extended mix of the same name.
        "duration": pick_column(columns, "Track Duration (ms)", "Duration (ms)"),
    }
    if not cols["track"] or not cols["artist"]:
        raise ValueError(
            "CSV must include track and artist columns (e.g. 'Track Name' and "
            f"'Artist Name(s)'). Found columns: {columns}"
        )
    warnings = []
    if not cols["album"]:
        warnings.append("No album column found; album metadata will be empty.")
    if not cols["release_date"]:
        warnings.append("No release-date column found; year tags will be empty.")
    if not cols["genre"]:
        warnings.append("No genre column found; genre tags will be empty.")
    return cols, warnings


def process_playlist(
    csv_file,
    bitrate=DEFAULT_BITRATE,
    audio_format=DEFAULT_FORMAT,
    output_root=".",
    should_cancel=None,
    sources=None,
):
    """
    Run one playlist, yielding progress events.

    Shared by the CLI and the web UI so both behave identically -- the caller
    decides how to present each event. Event types: init, warning, skip,
    track_start, track_done, track_error, cancelled, summary.

    `sources` is the ordered list to try for each track (default_sources()
    when None); see sources.py for their shape. track_done names the source
    that supplied the file, and both track_done and track_error carry
    "attempts": the sources that failed before, each with its reason.
    """
    bitrate = normalize_bitrate(bitrate)
    audio_format = normalize_audio_format(audio_format)
    columns, rows = read_playlist_csv(csv_file)
    cols, warnings = resolve_columns(columns)

    output_dir = os.path.join(output_root, get_output_dir(csv_file))
    os.makedirs(output_dir, exist_ok=True)
    ext = audio_format

    total_songs = len(rows)
    playlist_len = max(total_songs, 1)

    yield {
        "type": "init",
        "total": total_songs,
        "output_dir": output_dir,
        "bitrate": bitrate,
        "audio_format": audio_format,
        "ext": ext,
    }
    for w in warnings:
        yield {"type": "warning", "message": w}

    active = []
    for source in default_sources() if sources is None else sources:
        if audio_format not in source.formats:
            continue
        try:
            problem = source.check()
        except Exception as e:
            problem = str(e) or type(e).__name__
        if problem:
            # Once per job, not once per track: a source that cannot work now
            # will not start working halfway through the playlist.
            yield {
                "type": "warning",
                "message": f"{source.label} skipped for this playlist: {problem}",
            }
            continue
        active.append(source)
    if not active:
        raise ValueError(f"No audio source can produce {audio_format} files")
    if not cols["duration"] and any(getattr(s, "uses_duration", False) for s in active):
        yield {
            "type": "warning",
            "message": "No duration column found; files from "
            + " and ".join(s.label for s in active if getattr(s, "uses_duration", False))
            + " cannot be checked against the track's length.",
        }

    downloaded = skipped = failed = 0
    # (filename, artist, title) in playlist order, for the .m3u at the end.
    m3u_entries = []

    for playlist_index, row in enumerate(rows, start=1):
        if should_cancel is not None and should_cancel():
            yield {"type": "cancelled", "at": playlist_index}
            break

        track_title = cell_str(row, cols["track"])
        artist = cell_str(row, cols["artist"])
        album = cell_str(row, cols["album"]) if cols["album"] else ""
        album_artist = (
            cell_str(row, cols["album_artist"]) if cols["album_artist"] else ""
        ) or artist

        if not track_title or not artist:
            continue

        track_padded, track_tag = build_track_display_and_tag(
            row, playlist_index, playlist_len, cols["track_num"], cols["total_tracks"]
        )
        basename = output_basename(artist, track_padded, track_title)

        release_raw = (
            cell_str(row, cols["release_date"]) if cols["release_date"] else ""
        )
        # Prefer ISO year for tagging; full YYYY-MM-DD when present
        meta_date = release_raw[:10] if len(release_raw) >= 10 else release_raw[:4]
        genre = cell_str(row, cols["genre"]) if cols["genre"] else ""

        query = f"{artist} - {track_title}"
        duration = parse_duration_ms(cell_str(row, cols["duration"]))

        if song_exists(output_dir, basename, ext):
            skipped += 1
            # Already on disk from an earlier run -- still belongs in the .m3u.
            m3u_entries.append((f"{basename}.{ext}", artist, track_title))
            yield {
                "type": "skip",
                "index": playlist_index,
                "basename": basename,
                "ext": ext,
            }
            continue

        yield {
            "type": "track_start",
            "index": playlist_index,
            "query": query,
            "album": album,
            "basename": basename,
        }
        req = Request(
            track=match.Track(artist=artist, title=track_title, duration=duration),
            query=query,
            output_dir=output_dir,
            basename=basename,
            bitrate=bitrate,
            audio_format=audio_format,
            should_cancel=should_cancel,
        )
        result, used, attempts, cancelled = None, None, [], False
        for source in active:
            try:
                result = source.fetch(req)
                used = source
                break
            except Cancelled:
                cancelled = True
                break
            except Exception as e:
                attempts.append(
                    {"source": source.name, "label": source.label, "error": str(e)}
                )
        if cancelled:
            yield {"type": "cancelled", "at": playlist_index}
            break

        try:
            if result is None:
                # One source: its own message, worded as it always was. Several:
                # each one's reason, so the queue shows why P2P missed too.
                if len(attempts) == 1:
                    raise RuntimeError(attempts[0]["error"])
                raise RuntimeError(
                    "; ".join(f"{a['label']}: {a['error']}" for a in attempts)
                )
            comment = result.pop("comment")
            write_playlist_tags(
                result["path"], track_title, artist, album, album_artist,
                track_tag, meta_date, genre, comment,
            )
            downloaded += 1
            m3u_entries.append((f"{basename}.{ext}", artist, track_title))
            yield {
                "type": "track_done",
                "index": playlist_index,
                "query": query,
                "basename": basename,
                **result,
                "source": used.name,
                "source_name": used.label,
                "attempts": attempts,
            }
        except Exception as e:
            failed += 1
            yield {
                "type": "track_error",
                "index": playlist_index,
                "query": query,
                "error": str(e),
                "attempts": attempts,
            }

    playlist_file = None
    try:
        playlist_file = write_m3u(
            output_dir, get_output_dir(csv_file), m3u_entries
        )
    except OSError as e:
        # Never fail a finished download over the playlist file.
        yield {"type": "warning", "message": f"Could not write the .m3u: {e}"}

    yield {
        "type": "summary",
        "total": total_songs,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "output_dir": output_dir,
        "playlist_file": os.path.basename(playlist_file) if playlist_file else None,
    }


DEPRECATION_BANNER = (
    "\u26a0\ufe0f  Running download.py directly is deprecated.",
    "   This file is the engine; the supported front end is the web UI:",
    "       docker compose up -d --build      (from this folder)",
    "   The engine is identical either way -- the UI adds browser access",
    "   for other people, one-job-at-a-time queuing, and automatic yt-dlp",
    "   updates. It does not change what YouTube serves.",
    "   History: ../cli/README.md",
)


def main():
    parser = argparse.ArgumentParser(
        description="[DEPRECATED] Download songs from YouTube using a CSV playlist. "
        "This module is the engine behind the web UI in this folder; run that "
        "instead (docker compose up -d --build).",
        epilog="Example: python download.py playlist.csv 320",
    )
    parser.add_argument("playlist_file", help="Path to the CSV playlist file")
    parser.add_argument(
        "bitrate",
        nargs="?",
        default=str(DEFAULT_BITRATE),
        choices=[str(b) for b in SUPPORTED_BITRATES] + ["flac"],
        help=f"Maximum MP3 bitrate: {', '.join(str(b) for b in SUPPORTED_BITRATES)}. "
        f"Default {DEFAULT_BITRATE}.",
    )
    parser.add_argument(
        "--format",
        dest="audio_format",
        default=DEFAULT_FORMAT,
        choices=list(SUPPORTED_FORMATS),
        help="Output container. 'm4a' copies YouTube's AAC stream without "
        "re-encoding (bitrate ignored). Default mp3.",
    )
    args = parser.parse_args()

    for line in DEPRECATION_BANNER:
        print(line, file=sys.stderr)
    print(file=sys.stderr)

    if args.bitrate == "flac":
        print(
            "\u274c FLAC support was removed. YouTube serves no lossless audio "
            "(its ceiling\n   is ~125-130 kbps opus/AAC), so a FLAC file here was "
            "a lossless wrapper\n   around lossy audio: ~4x the size for identical "
            "sound. Use 128, 192 or 320.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        bitrate = normalize_bitrate(args.bitrate)
    except ValueError as e:
        print(f"\u274c Error: {e}")
        sys.exit(1)

    audio_format = normalize_audio_format(args.audio_format)
    csv_file = args.playlist_file
    if not os.path.exists(csv_file):
        print(f"\u274c Error: CSV file '{csv_file}' not found!")
        sys.exit(1)

    spinner = None
    try:
        for ev in process_playlist(csv_file, bitrate, audio_format):
            kind = ev["type"]

            if kind == "init":
                print(f"\U0001f4c1 Output directory: {ev['output_dir']}")
                if ev["audio_format"] == "m4a":
                    print("\U0001f3bc Format: M4A (stream copy, no re-encode)")
                    print(f"   \u2139\ufe0f  {FORMAT_NOTES['m4a']}")
                else:
                    print(f"\U0001f3bc Format: MP3 @ {ev['bitrate']} kbps")
                    note = BITRATE_NOTES.get(ev["bitrate"])
                    if note:
                        print(f"   \u2139\ufe0f  {ev['bitrate']} kbps: {note}")
                print(f"\U0001f4ca Found {ev['total']} songs in playlist")
                print("=" * 50)

            elif kind == "warning":
                print(f"\u26a0\ufe0f  {ev['message']}")

            elif kind == "skip":
                print(
                    f"\u23ed\ufe0f  Skipping (already exists): "
                    f"{ev['basename']}.{ev['ext']}"
                )

            elif kind == "track_start":
                print(f"   Album: {ev['album']}")
                spinner = LoadingSpinner(f"\U0001f3b5 Downloading: {ev['query']}")
                spinner.start()

            elif kind == "track_done":
                if spinner:
                    spinner.stop(ok=True)
                    spinner = None
                suffix = (
                    f" \u2014 SABR fallback via '{FALLBACK_PLAYER_CLIENT}'"
                    if ev.get("used_fallback")
                    else ""
                )
                where = (
                    "" if ev.get("source", "youtube") == "youtube"
                    else f"{ev['source_name']}, {ev.get('peer', '')}: "
                )
                print(f"   \U0001f50e Source: {where}{ev['source_label']}{suffix}")
                for a in ev.get("attempts") or []:
                    print(f"   \u21aa {a['label']} missed: {a['error']}")

            elif kind == "track_error":
                if spinner:
                    spinner.stop(ok=False)
                    spinner = None
                print(f"\u274c Error with {ev['query']}: {ev['error']}")

            elif kind == "cancelled":
                print("\u26d4 Cancelled")

            elif kind == "summary":
                print("=" * 50)
                print("\U0001f4c8 Summary:")
                print(f"   Total songs: {ev['total']}")
                print(f"   Downloaded: {ev['downloaded']}")
                print(f"   Skipped (already exist): {ev['skipped']}")
                if ev["failed"]:
                    print(f"   Failed: {ev['failed']}")
    except ValueError as e:
        print(f"\u274c Error: {e}")
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"\u274c Error: {e}")
        sys.exit(1)
    finally:
        if spinner:
            spinner.stop(ok=False)


if __name__ == "__main__":
    main()
