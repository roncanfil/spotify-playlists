"""
Where a track's audio comes from.

process_playlist() walks an ordered list of sources and takes the first that
produces a file. Every source has the same shape:

    name      short id used in events and settings ("youtube", "soulseek")
    label     what the queue calls it ("YouTube", "Soulseek")
    formats   output formats it can produce
    check()   None when usable, else a sentence saying why not; called once per
              job, so a source that cannot work is skipped with one warning
              rather than failing every track
    fetch(req)
              writes req.target_path and returns a result dict with at least
              "path", "source_label" and "comment" (the tag comment), or
              raises. Cancelled means stop the job, not try the next source.

YouTube lives in download.py next to the yt-dlp code it uses. The peer-to-peer
networks share P2PSource below, which does the ranking, the candidate loop,
the length check and the conversion; each network only supplies a client that
can search and download.
"""

import dataclasses
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

import match


class Cancelled(Exception):
    """The user cancelled the job while a source was waiting on the network."""


class SourceError(Exception):
    """This source could not supply the track; the next one may."""


class Rejected(Exception):
    """A downloaded file is not usable (wrong length, too low a bitrate...)."""


class PeerUnreachable(SourceError):
    """
    The peer could not be contacted at all (offline, or no connection either
    way). Says nothing about its files, but every other file from that peer
    will fail the same way, so the candidate loop skips them all.
    """


@dataclass
class Request:
    """One track to fetch. The output lands at output_dir/basename.ext."""

    track: match.Track
    query: str
    output_dir: str
    basename: str
    bitrate: int
    audio_format: str
    should_cancel: Optional[Callable[[], bool]] = None

    @property
    def target_path(self):
        return os.path.join(self.output_dir, f"{self.basename}.{self.audio_format}")

    def cancelled(self):
        return bool(self.should_cancel and self.should_cancel())


def parse_duration_ms(raw):
    """Whole seconds from a "Track Duration (ms)" cell; 0 when absent or unusable."""
    try:
        ms = float(str(raw or "").strip())
    except ValueError:
        return 0
    return int(round(ms / 1000)) if ms > 0 else 0


def mmss(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# ------------------------------------------------------------ conversion

# A downloaded file this far from Spotify's length is another recording (a live
# take, an extended mix, a truncated rip), however well its name matched.
MAX_DURATION_DIFF = 10

# Lossy files below this are worse than what YouTube already gives (~130 kbps
# opus), so falling through to it is the better outcome.
MIN_LOSSY_KBPS = 128

# VBR MP3s report an average that wanders a little around their nominal rate.
# Re-encoding a 322 kbps file to 320 would cost a lossy generation for nothing.
COPY_TOLERANCE = 1.05

_LOSSLESS_TYPES = {"FLAC", "WAVE", "AIFF", "MonkeysAudio", "WavPack", "TrueAudio"}


@dataclass
class AudioInfo:
    kind: str  # "mp3", "lossless" or "lossy"
    codec: str
    kbps: int
    length: float
    sample_rate: int
    channels: int
    bits: int


def probe_audio(path):
    """What a file really is, from its contents rather than its name. None if unreadable."""
    try:
        from mutagen import File as MutagenFile

        f = MutagenFile(path)
    except Exception:
        return None
    if f is None or f.info is None or not getattr(f.info, "length", 0):
        return None
    info = f.info
    type_name = type(f).__name__
    codec = type_name.lower()
    if type_name == "MP3":
        kind, codec = "mp3", "mp3"
    elif type_name in _LOSSLESS_TYPES:
        kind = "lossless"
    elif type_name == "MP4":
        # "mp4a.40.2" is AAC; "alac" is Apple Lossless in the same container.
        codec = (getattr(info, "codec", "") or "mp4").split(".")[0]
        codec = "aac" if codec == "mp4a" else codec
        kind = "lossless" if codec == "alac" else "lossy"
    else:
        kind = "lossy"
    return AudioInfo(
        kind=kind,
        codec=codec,
        kbps=int(round((getattr(info, "bitrate", 0) or 0) / 1000)),
        length=float(info.length),
        sample_rate=int(getattr(info, "sample_rate", 0) or 0),
        channels=int(getattr(info, "channels", 0) or 0),
        bits=int(getattr(info, "bits_per_sample", 0) or 0),
    )


def plan_conversion(kind, kbps, target):
    """
    "copy" or "encode" for a file of `kind` at `kbps`, making an MP3 of
    `target` kbps. Never upscale: an MP3 at or below the target is kept as it
    is, since re-encoding it higher adds size and no sound. Lossless, and MP3
    above the target, are encoded down to it. Other lossy formats cannot be
    copied into an MP3, so they are encoded at the target, as YouTube's opus
    already is. Raises Rejected for a lossy file too poor to be worth having.
    """
    if kind == "lossless":
        return "encode"
    if kbps <= 0:
        raise Rejected("its bitrate could not be read")
    if kbps < MIN_LOSSY_KBPS:
        raise Rejected(f"it is only {kbps} kbps")
    if kind == "mp3" and kbps <= target * COPY_TOLERANCE:
        return "copy"
    return "encode"


def check_length(info, duration):
    """Raise Rejected when the file's real length is not Spotify's."""
    if duration and abs(info.length - duration) > MAX_DURATION_DIFF:
        raise Rejected(
            f"it is {mmss(info.length)} long and Spotify says {mmss(duration)}"
        )


def convert(src, dest, mode, target, info):
    """
    Write `src` to `dest` as MP3, copying or encoding per `mode`.

    Only the first audio stream is kept and every tag and picture is dropped,
    so the file ends up tagged from the playlist alone, as YouTube downloads
    are. Written beside the target and renamed into place, so a failure never
    leaves a half file that the next run would skip as already downloaded.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", src, "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1",
    ]
    if mode == "copy":
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "libmp3lame", "-b:a", f"{target}k"]
        # LAME stops at 48 kHz and two channels; hi-res and surround files
        # would otherwise fail to encode.
        if info.sample_rate > 48000:
            cmd += ["-ar", "44100"]
        if info.channels > 2:
            cmd += ["-ac", "2"]
    tmp = dest + ".part"
    cmd += ["-f", "mp3", tmp]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        os.replace(tmp, dest)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or "").strip().splitlines()
        raise Rejected(
            "ffmpeg could not read it" + (f" ({err[-1]})" if err else "")
        ) from e
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def describe_file(info, mode, target):
    """The queue's line for what was downloaded and what was done to it."""
    if info.kind == "lossless":
        bits = f"{info.bits}-bit " if info.bits else ""
        rate = f"{info.sample_rate / 1000:g} kHz" if info.sample_rate else ""
        what = f"{info.codec} {bits}{rate}".strip()
    else:
        what = f"{info.codec} {info.kbps} kbps"
    if mode == "copy":
        return f"{what}, kept as is"
    return f"{what}, encoded to {target} kbps"


# ------------------------------------------------------------ P2P

# Each candidate can cost a minute or more of waiting on a peer, so after this
# many failures the next source is the faster way to get the track.
MAX_CANDIDATES = 4
# Peers that cannot be contacted fail in seconds, not minutes, and in live
# runs often several in a row, so they have a budget of their own rather than
# using up MAX_CANDIDATES.
MAX_UNREACHABLE = 8


class P2PClient:
    """
    What P2PSource needs from a network. Subclasses implement search and
    download; the rest have sensible defaults.
    """

    name = "p2p"
    label = "P2P"

    def check(self):
        """None when the network is usable, else a sentence saying why not."""
        return None

    def search(self, query, should_cancel):
        """List of match.SearchResponse for one query. May raise Cancelled."""
        raise NotImplementedError

    def download(self, cand, should_cancel):
        """
        Local path of the finished file for a match.Cand. Raises on timeout,
        stall or refusal (the message goes in the queue), Cancelled on cancel;
        either way the remote transfer is cancelled first.
        """
        raise NotImplementedError

    def describe(self, cand):
        """Who supplied the file, for the tag comment and the queue."""
        return f"peer {cand.user}"

    def discard(self, path):
        """Remove a downloaded file once it is converted or rejected."""
        try:
            os.remove(path)
        except OSError:
            pass


class P2PSource:
    """A peer-to-peer network as a source: MP3 output only."""

    # m4a output is a stream copy of YouTube's AAC; transcoding a peer's file
    # to AAC would throw away the reason to pick m4a.
    formats = ("mp3",)
    # process_playlist warns when the CSV has no duration column, since the
    # length check is what tells the song from a live take of the same name.
    uses_duration = True

    def __init__(self, client, max_candidates=MAX_CANDIDATES):
        self.client = client
        self.name = client.name
        self.label = client.label
        self.max_candidates = max_candidates
        # Peers that sent us a good file this session rank a little higher.
        self.known = set()

    def check(self):
        return self.client.check()

    def _search(self, req, ranker):
        """Run queries until one finds a good candidate. Returns (queries, dropped)."""
        searched, dropped = [], 0
        for q in match.queries(req.track):
            if req.cancelled():
                raise Cancelled()
            for resp in self.client.search(q, req.should_cancel):
                files = []
                for f in resp.files:
                    est = match.estimate(f, req.track.duration)
                    if est is None:
                        dropped += 1
                    else:
                        files.append(est)
                ranker.add(dataclasses.replace(resp, files=files))
            searched.append(q)
            # Each search spends rate-limit budget, so stop once one has turned
            # up a likely file from a peer that can send it now.
            if ranker.good():
                break
        return searched, dropped

    def fetch(self, req):
        ranker = match.Ranker(req.track, req.bitrate, known=self.known)
        searched, dropped = self._search(req, ranker)
        if not ranker.cands:
            words = " or ".join(repr(q) for q in searched) or repr(req.query)
            extra = (
                f" ({dropped} files dropped for a size that does not fit the "
                "track's length)" if dropped else ""
            )
            raise SourceError(f"no file matched {words}{extra}")

        reasons = []
        tried = unreachable = 0
        while tried < self.max_candidates and unreachable < MAX_UNREACHABLE:
            cand = ranker.next()
            if cand is None:
                break
            who = self.client.describe(cand)
            try:
                local = self.client.download(cand, req.should_cancel)
            except Cancelled:
                raise
            except PeerUnreachable as e:
                unreachable += 1
                reasons.append(f"{who}: {e}")
                for other in ranker.cands:
                    if other.user == cand.user:
                        other.failed = True
                ranker.mark_failed(cand.id)
                continue
            except Exception as e:
                tried += 1
                reasons.append(f"{who}: {e}")
                ranker.mark_failed(cand.id)
                continue
            tried += 1
            try:
                info = probe_audio(local)
                if info is None:
                    raise Rejected("it is not an audio file that can be read")
                check_length(info, req.track.duration)
                mode = plan_conversion(info.kind, info.kbps, req.bitrate)
                convert(local, req.target_path, mode, req.bitrate, info)
            except Rejected as e:
                reasons.append(f"{who}: {e}")
                ranker.mark_failed(cand.id)
                continue
            finally:
                self.client.discard(local)
            self.known.add(cand.user)
            return {
                "path": req.target_path,
                "source_label": describe_file(info, mode, req.bitrate),
                "comment": f"Source: {self.label}, {who}",
                "peer": who,
                "remote_file": match.stem(cand.file.filename),
                "conversion": mode,
                "source_kbps": info.kbps,
            }
        n = len(reasons)
        raise SourceError(
            f"tried {n} file{'s' if n != 1 else ''}, none usable: "
            + "; ".join(reasons)
        )
