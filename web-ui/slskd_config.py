"""
The slskd sidecar's settings file, which this app owns.

slskd runs with SLSKD_APP_DIR pointing at a hidden folder inside the music
library (<music>/.spotify-playlists/slskd), and the music folder is mounted at
the same path in both containers. So slskd's settings, its database, and its
in-progress and finished downloads all live under the one path the user sets,
and this app reads and writes them directly. Nothing about Soulseek has to go
in the compose file: the onboarding screen collects the account, and this
module turns it into slskd.yml.

The file is written as JSON, which is also valid YAML. slskd reads it as YAML
(checked on 0.26.2) and this module reads it back with the standard library,
so no YAML package is needed. slskd watches the file and applies a changed API
key, account and shares without a restart. Connecting with a new account and
rescanning shares are separate API calls; see slskd.SlskdClient.
"""

import json
import os
import secrets
import threading

FILE_NAME = "slskd.yml"
HEADER = (
    "# Managed by Spotify Playlists: set the Soulseek account and sharing in\n"
    "# the app's Settings tab. Edits made here by hand are overwritten.\n"
)
KEY_NAME = "spotify_playlists"
# What the share leaves out. Hidden folders, such as .spotify-playlists with
# the Spotify token and slskd's own files, are never shared by slskd in the
# first place (checked on 0.26.2), so they need no filter.
SHARE_FILTERS = [r"\.csv$", r"\.m3u8?$"]
# Minutes slskd keeps a finished search. It otherwise keeps every search, with
# all its responses, forever; this option exists only in the settings file.
SEARCH_RETENTION = 1440


class Settings:
    """Read and write slskd.yml. Thread-safe; one instance per process."""

    def __init__(self, home, music_dir):
        self.home = home
        self.music_dir = music_dir
        self.path = os.path.join(home, FILE_NAME)
        self._lock = threading.Lock()

    @property
    def downloads_dir(self):
        """slskd's default downloads folder, which it creates on start."""
        return os.path.join(self.home, "downloads")

    # ------------------------------------------------------------ reading

    def _load(self):
        """
        The parsed file, or {} when it is missing, empty, or not ours. slskd
        copies a commented-out example into place when it starts before this
        app has written anything; that, too, reads as {}.
        """
        try:
            with open(self.path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return {}
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")).strip()
        if not body:
            return {}
        try:
            data = json.loads(body)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def read(self):
        """{"api_key", "username", "password", "share"} with "" / False for unset."""
        with self._lock:
            d = self._load()
        keys = ((d.get("web") or {}).get("authentication") or {}).get("api_keys") or {}
        slsk = d.get("soulseek") or {}
        shares = d.get("shares") or {}
        return {
            "api_key": str((keys.get(KEY_NAME) or {}).get("key") or ""),
            "username": str(slsk.get("username") or ""),
            "password": str(slsk.get("password") or ""),
            "share": self.music_dir in (shares.get("directories") or []),
        }

    # ------------------------------------------------------------ writing

    def _write(self, api_key, username, password, share):
        cfg = {
            "web": {"authentication": {"api_keys": {
                KEY_NAME: {"key": api_key, "role": "administrator"},
            }}},
            "shares": {
                "directories": [self.music_dir] if share else [],
                "filters": SHARE_FILTERS,
            },
            "retention": {"search": SEARCH_RETENTION},
        }
        if username and password:
            cfg["soulseek"] = {"username": username, "password": password}
        os.makedirs(self.home, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(HEADER + json.dumps(cfg, indent=2) + "\n")
        # Holds the Soulseek password and slskd's admin key.
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def ensure(self):
        """
        Make sure the file exists with an API key, keeping everything else.
        Called at app start; returns the key.
        """
        with self._lock:
            cur = self._load()
        s = self.read()
        if s["api_key"] and cur:
            return s["api_key"]
        key = s["api_key"] or secrets.token_hex(24)
        with self._lock:
            # New installs share by default: Soulseek runs on reciprocity.
            self._write(key, s["username"], s["password"], s["share"] or not cur)
        return key

    def update(self, username=None, password=None, share=None):
        """
        Change any of the account and sharing settings, keeping the rest.
        username="" removes the account.
        """
        s = self.read()
        key = s["api_key"] or secrets.token_hex(24)
        user = s["username"] if username is None else username.strip()
        pw = s["password"] if password is None else password
        if not user:
            pw = ""
        with self._lock:
            self._write(key, user, pw, s["share"] if share is None else bool(share))
        return self.read()
