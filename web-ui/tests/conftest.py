"""
Shared fixtures. Nothing here touches the network: P2P clients and yt-dlp are
replaced by fakes, and fixture audio is generated locally with ffmpeg.

Run from web-ui/:  pip install -r requirements.txt -r requirements-dev.txt
                   python -m pytest
"""

import os
import shutil
import subprocess
import sys

import pytest

# The engine modules live one folder up and are imported by bare name, as the
# container does.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not on PATH")


def _make(path, seconds, args):
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-ac", "2", *args, path,
        ],
        check=True,
    )
    return path


@pytest.fixture(scope="session")
def audio(tmp_path_factory):
    """
    Real files of known type, bitrate and length, keyed by name. 30 s is the
    "Spotify" length the tests use; long_mp3 is the wrong-length recording.
    Tests must copy these before handing them to code that deletes its input.
    """
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not on PATH")
    d = tmp_path_factory.mktemp("audio")
    return {
        "mp3_320": _make(str(d / "a320.mp3"), 30, ["-c:a", "libmp3lame", "-b:a", "320k"]),
        "mp3_192": _make(str(d / "a192.mp3"), 30, ["-c:a", "libmp3lame", "-b:a", "192k"]),
        "mp3_96": _make(str(d / "a96.mp3"), 30, ["-c:a", "libmp3lame", "-b:a", "96k"]),
        "flac": _make(str(d / "a.flac"), 30, ["-c:a", "flac"]),
        "flac_96k": _make(
            str(d / "a96k.flac"), 30, ["-ar", "96000", "-c:a", "flac", "-sample_fmt", "s32"]
        ),
        "long_mp3": _make(str(d / "long.mp3"), 60, ["-c:a", "libmp3lame", "-b:a", "320k"]),
        "not_audio": _write(str(d / "junk.mp3"), b"this is not an mp3 file at all" * 100),
    }


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)
    return path
