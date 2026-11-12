"""
Which peer-to-peer file to download for a track.

Three questions per file: is it the same song (name and length), is the sound
good enough for the MP3 we are making (never settle for less when better is on
offer), and will the peer send it soon and fast.

Ported from spotseek's engine/src/rank.rs
(https://github.com/lopezlav/spotseek), which is under the MIT licence below.
The port keeps its weights and thresholds as they are; spotseek tuned them
against real Soulseek results and this app has nothing better to tune them
with. What is ours, not spotseek's, is marked "Not in spotseek" below.

    MIT License

    Copyright (c) 2026 Spotseek contributors

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to
    deal in the Software without restriction, including without limitation the
    rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
    sell copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
    FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
    IN THE SOFTWARE.

"""

import dataclasses
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Track:
    """What we are looking for. duration is whole seconds; 0 when unknown."""

    artist: str
    title: str
    duration: int = 0


@dataclass
class SharedFile:
    """
    One file a peer offers. bitrate is kbps and duration seconds, each None
    when the network did not say. `ref` is whatever the client needs to fetch
    the file again, if the network needs more than its name; the ranker never
    reads it.
    """

    filename: str
    size: int
    ext: str
    bitrate: Optional[int] = None
    duration: Optional[int] = None
    sample_rate: Optional[int] = None
    bit_depth: Optional[int] = None
    # Not in spotseek: True when bitrate was worked out by estimate(), not
    # reported, so the queue can say the figure is a guess.
    estimated: bool = False
    ref: Any = None


@dataclass
class SearchResponse:
    """One peer's answer: who, how busy, how fast (bytes/s), and the files."""

    user: str
    free_slot: bool
    avg_speed: int
    queue_len: int
    files: list = field(default_factory=list)


def norm(s):
    """Lower case, no accents, letters and digits split by single spaces."""
    out = []
    space = True
    for c in unicodedata.normalize("NFD", s or ""):
        # Rust's is_combining_mark is the Unicode Mark category, M*.
        if unicodedata.category(c).startswith("M"):
            continue
        if c.isalnum():
            out.append(c.lower())
            space = False
        elif not space:
            out.append(" ")
            space = True
    return "".join(out).rstrip()


def base_title(s):
    """
    The title without the parts file names often leave out: "(feat. X)",
    "[Remastered]", and " - Remix" style suffixes. The full title when nothing
    would be left.
    """
    cut = len(s)
    for sep in (" (", " [", " - "):
        i = s.find(sep)
        if 0 < i < cut:
            cut = i
    b = s[:cut].strip()
    return b if b else s


def main_artist(s):
    """The first artist ("A, B" and "A feat. B" give "A")."""
    a = s.split(",")[0]
    for sep in (" feat. ", " feat ", " ft. ", " & ", " x "):
        # Case-insensitive like the original, but searched in place so the
        # index always belongs to `a` (lower() can change a string's length).
        m = re.search(re.escape(sep), a, re.IGNORECASE)
        if m:
            a = a[: m.start()]
    return a.strip()


def queries(t):
    """
    The search words: the main artist and the base title. Soulseek finds files
    whose path has every word, so fewer, surer words find more.
    """
    title = norm(base_title(t.title))
    artist = norm(main_artist(t.artist))
    q = [f"{artist} {title}".strip()]
    # Without the artist: for files named only by title, in folders named
    # another way. Length in UTF-8 bytes, as Rust's str::len counts it.
    distinctive = len(title.split(" ")) >= 2 or len(title.encode("utf-8")) >= 7
    if distinctive and artist:
        q.append(title)
    q = [s for s in q if s]
    out = []
    for s in q:
        if not out or out[-1] != s:
            out.append(s)
    return out


# Words that make a different version, when the wanted title does not have them.
VERSION_WORDS = (
    "remix", "live", "karaoke", "instrumental", "acapella", "a cappella",
    "cover", "sped up", "slowed", "nightcore", "8d", "mashup", "bootleg",
    "extended", "club mix", "dub mix", "edit", "reprise", "demo", "acoustic",
    "unplugged", "version", "vip", "rework", "flip", "chopped",
)

LOSSLESS = ("flac", "wav", "aif", "aiff", "ape", "wv", "alac")
LOSSY = ("mp3", "m4a", "aac", "ogg", "opus", "wma")


def is_lossless(ext):
    return ext in LOSSLESS


def is_audio(ext):
    return is_lossless(ext) or ext in LOSSY


def stem(path):
    """The file name without folders and extension."""
    base = re.split(r"[\\/]", path)[-1]
    i = base.rfind(".")
    return base[:i] if i > 0 else base


def has_words(hay, words):
    return f" {words} " in f" {hay} "


def fit(t, f):
    """How well the file fits the track, 0 to 100. Below 50 it is most likely another song."""
    path = norm(f.filename)
    name = norm(stem(f.filename))
    full_title = norm(t.title)
    title = norm(base_title(t.title))
    artist = norm(main_artist(t.artist))
    s = 0.0

    # The title, in the file name.
    words = [w for w in title.split(" ") if w]
    cap = 100.0
    if words:
        if has_words(name, title):
            s += 45.0
        else:
            found = sum(1 for w in words if has_words(name, w)) / len(words)
            s += 30.0 * found
            # Most of the title is not in the name: another song, whatever its length.
            if found < 0.6:
                cap = 40.0
    # The artist, anywhere in the path (folders are often "Artist/Album").
    aw = [w for w in artist.split(" ") if w]
    if aw:
        if has_words(path, artist):
            s += 25.0
        else:
            found = sum(1 for w in aw if has_words(path, w))
            s += 15.0 * found / len(aw)
    else:
        s += 15.0
    # The length.
    d, w = f.duration, t.duration
    if d and w and d > 0 and w > 0:
        diff = abs(d - w)
        if diff <= 2:
            s += 30.0
        elif diff <= 5:
            s += 24.0
        elif diff <= 10:
            s += 12.0
        elif diff <= 20:
            s += 0.0
        else:
            s -= 40.0
    else:
        s += 10.0
    # Another version of the song.
    for vw in VERSION_WORDS:
        if has_words(name, vw) and not has_words(full_title, vw):
            s -= 35.0
            break
    return int(min(max(s, 0.0), cap))


def kbps(f):
    """The kbps of the file: what the peer said, else from the size and the length."""
    if f.bitrate and f.bitrate > 0:
        return f.bitrate
    if f.duration and f.duration > 0:
        return f.size * 8 // f.duration // 1000
    return 0


def quality(f, target):
    """
    How good the sound is for an mp3 of `target` kbps, 0 to 1. Lossless and
    mp3 at or above the target are 1; lower mp3 counts by its bitrate; other
    lossy formats count a little less.
    """
    if is_lossless(f.ext):
        return 1.0
    b = kbps(f)
    if b == 0:
        return 0.5
    q = min(b / target, 1.0)
    return q if f.ext == "mp3" else q * 0.85


def score(fit_, f, free, speed, queue, target):
    """The rank score: the fit first, then the sound, then how soon and how fast it comes."""
    s = float(fit_)
    s += 40.0 * quality(f, target)
    # An mp3 at the target bitrate is copied as it is: no conversion.
    if f.ext == "mp3" and kbps(f) == target:
        s += 5.0
    if free:
        s += 25.0
    else:
        # Every upload in the queue is a wait of a minute or more.
        s -= 10.0 + min(queue, 60) * 0.8
    # Time to download: a big lossless file from a slow peer takes long.
    eta = f.size / max(speed, 40_000)
    s -= min(eta / 4.0, 30.0)
    # A known fast peer.
    s += min(speed / 200_000.0, 8.0)
    return s


@dataclass
class Cand:
    """One file for a track, with its scores."""

    id: int
    user: str
    file: SharedFile
    free: bool
    speed: int
    queue: int
    fit: int
    score: float
    failed: bool = False
    # The peer sent us good files before.
    known: bool = False

    def eta(self):
        """Seconds to download at the peer's speed (a slow floor when it is not known)."""
        return self.file.size // max(self.speed, 40_000)


class Ranker:
    """
    The files of the answers that may be the track, best first, without
    repeats. `known` holds the peers that sent us good files before.

    spotseek's window-update helpers (to_send, infos) and its library shortcut
    (enough_without_search) are left out: this app has neither.
    """

    def __init__(self, track, target, known=None):
        self.track = track
        self.target = target
        self.cands = []
        self.known = known if known is not None else set()
        self._next_id = 0

    def add(self, r):
        """Adds a peer's answer. True when a new file fits."""
        added = False
        for f in r.files:
            if not is_audio(f.ext) or f.size < 500_000:
                continue
            fit_ = fit(self.track, f)
            if fit_ < 50:
                continue
            if any(c.user == r.user and c.file.filename == f.filename for c in self.cands):
                continue
            known = r.user in self.known
            sc = score(fit_, f, r.free_slot, r.avg_speed, r.queue_len, self.target)
            sc += 8.0 if known else 0.0
            self.cands.append(
                Cand(
                    id=self._next_id, user=r.user, file=f, free=r.free_slot,
                    speed=r.avg_speed, queue=r.queue_len, fit=fit_, score=sc,
                    known=known,
                )
            )
            self._next_id += 1
            added = True
        if added:
            self._sort()
        return added

    def _sort(self):
        # Stable, like Rust's sort_by, so equal scores keep arrival order.
        self.cands.sort(key=lambda c: (c.failed, -c.score))

    def mark_failed(self, id_):
        for c in self.cands:
            if c.id == id_:
                c.failed = True
        self._sort()

    def get(self, id_):
        return next((c for c in self.cands if c.id == id_), None)

    def good(self):
        """Good candidates: likely the song, from a peer with a free slot."""
        return sum(1 for c in self.cands if not c.failed and c.free and c.fit >= 75)

    def excellent(self):
        """Very good candidates: surely the song, a free peer, the full sound, and a short download."""
        return sum(
            1 for c in self.cands
            if not c.failed and c.free and c.fit >= 90
            and quality(c.file, self.target) >= 0.99 and c.eta() <= 60
        )

    def next(self):
        """The next one to try: the best that did not fail."""
        return next((c for c in self.cands if not c.failed), None)


# --------------------------------------------------------------------------
# Not in spotseek. Some Soulseek clients report a file's size and name but
# neither its bitrate nor its length (about one result in seven in testing), so
# fit() cannot check the length and quality() has nothing to go on. Spotify's
# length stands in for the file's, which turns
# the size into a bitrate. A file whose implied bitrate no real file of that
# type could have is the wrong length (a full album, a 30-second clip), so it
# is dropped before it is ever downloaded. The duration check after download
# catches what slips through.

# kbps a whole file of each type can plausibly average, embedded cover art
# included (a 2 MB image adds ~65 kbps to a four-minute MP3).
PLAUSIBLE_KBPS = {
    "mp3": (64, 400),
    "m4a": (32, 400),
    "aac": (32, 400),
    "ogg": (32, 400),
    "opus": (32, 400),
    "wma": (32, 400),
    # Compressed lossless: quiet 16/44.1 material runs ~300, 24/192 ~6000.
    "flac": (250, 6500),
    "ape": (250, 6500),
    "wv": (250, 6500),
    "alac": (250, 6500),
    # Uncompressed PCM: 16/22 mono ~350 up to 24/192 stereo ~9200.
    "wav": (300, 9300),
    "aif": (300, 9300),
    "aiff": (300, 9300),
}


def plausible_kbps(ext, value):
    lo, hi = PLAUSIBLE_KBPS.get(ext, (1, 10_000))
    return lo <= value <= hi


def estimate(f, track_duration):
    """
    The file with a bitrate worked out from its size and Spotify's length,
    when the peer reported neither bitrate nor length. None when that
    bitrate is implausible for the file's type. Files that already carry a
    bitrate or length, and tracks of unknown length, come back unchanged.

    The length itself is left unset, so fit() still gives the neutral score
    for an unknown length rather than a perfect one for a guess.
    """
    if (f.bitrate and f.bitrate > 0) or (f.duration and f.duration > 0):
        return f
    if not track_duration or track_duration <= 0 or f.size <= 0:
        return f
    implied = f.size * 8 // track_duration // 1000
    if not plausible_kbps(f.ext, implied):
        return None
    return dataclasses.replace(f, bitrate=implied, estimated=True)
