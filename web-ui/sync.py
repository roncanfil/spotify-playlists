"""
Playlists kept in sync with Spotify: which ones, and what was last seen.

A playlist downloaded from Spotify (a pasted link, or one of the connected
account's playlists) can be kept in sync. Every SYNC_INTERVAL the app reads
it again, and when its songs changed it rewrites the playlist's CSV and
queues an ordinary job: songs already on disk are skipped, new ones are
downloaded, and the .m3u is rewritten in Spotify's order. Songs removed on
Spotify keep their files and only leave the .m3u.

This module only stores the list, in <state>/sync.json; app.py does the
checking, since that needs the Spotify client and the job queue.

Each entry, keyed by its source ("spotify:playlist:<id>", or "liked" for the
account's Liked Songs):

    name      what the queue calls it
    folder    its folder under the music folder, fixed when it was added, so
              renaming the playlist on Spotify does not start a second copy
    snapshot  Spotify's version id when last read through the account, which
              makes an unchanged playlist one cheap request to check
    songs     how many songs it had
    checked   when it was last checked (epoch seconds)
    changed   when a check last found a change and queued a job
    added     how many new songs that change brought
    error     why the last check failed, or ""
    bitrate, format
              output settings for its jobs
"""

import json
import os
import threading
import time

FILE_NAME = "sync.json"


class SyncStore:
    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, FILE_NAME)
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def all(self):
        """Every entry, each with its "source", oldest first."""
        with self._lock:
            data = self._load()
        return [dict(v, source=k) for k, v in data.items()]

    def get(self, source):
        with self._lock:
            entry = self._load().get(source)
        return dict(entry, source=source) if entry else None

    def add(self, source, name, folder, bitrate=None, audio_format=None):
        """
        Start syncing, or refresh one already synced. The output settings are
        those of the download that started it: the checks run on the server,
        away from any browser's settings.
        """
        with self._lock:
            data = self._load()
            entry = data.get(source) or {
                "folder": folder, "snapshot": "", "songs": 0, "checked": time.time(),
                "changed": 0, "added": 0, "error": "",
            }
            entry["name"] = name
            if bitrate:
                entry["bitrate"] = int(bitrate)
            if audio_format:
                entry["format"] = audio_format
            data[source] = entry
            self._save(data)

    def update(self, source, **fields):
        with self._lock:
            data = self._load()
            if source in data:
                data[source].update(fields)
                self._save(data)

    def remove(self, source):
        with self._lock:
            data = self._load()
            if data.pop(source, None) is not None:
                self._save(data)
                return True
        return False
