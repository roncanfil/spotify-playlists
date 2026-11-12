"""slskd_config: the settings file the app writes for slskd."""

import json
import os
import stat

from slskd_config import HEADER, SHARE_FILTERS, Settings


def settings(tmp_path):
    return Settings(str(tmp_path / ".spotify-playlists" / "slskd"), "/music")


def body(st):
    text = open(st.path).read()
    assert text.startswith(HEADER)
    return json.loads(text[len(HEADER):])


def test_ensure_writes_a_key_and_shares_the_library_on_a_new_install(tmp_path):
    st = settings(tmp_path)
    key = st.ensure()
    assert len(key) == 48
    d = body(st)
    assert d["web"]["authentication"]["api_keys"]["spotify_playlists"] == {"key": key, "role": "administrator"}
    assert d["shares"] == {"directories": ["/music"], "filters": SHARE_FILTERS}
    assert d["retention"] == {"search": 1440}
    # No account until onboarding provides one: slskd then stays idle.
    assert "soulseek" not in d
    assert stat.S_IMODE(os.stat(st.path).st_mode) == 0o600


def test_ensure_keeps_an_existing_key_and_settings(tmp_path):
    st = settings(tmp_path)
    key = st.ensure()
    st.update(username="someone", password="pw", share=False)
    assert st.ensure() == key
    assert st.read() == {"api_key": key, "username": "someone", "password": "pw", "share": False}


def test_slskds_own_example_file_is_replaced(tmp_path):
    # slskd copies a commented-out example into place if it starts first.
    st = settings(tmp_path)
    os.makedirs(st.home)
    with open(st.path, "w") as fh:
        fh.write("# debug: false\n# soulseek:\n#   username: ~\n")
    key = st.ensure()
    assert body(st)["web"]["authentication"]["api_keys"]["spotify_playlists"]["key"] == key
    assert st.read()["share"] is True


def test_removing_the_account_drops_the_password_too(tmp_path):
    st = settings(tmp_path)
    st.ensure()
    st.update(username="someone", password="pw")
    st.update(username="")
    assert "soulseek" not in body(st)
    assert st.read()["password"] == ""


def test_turning_sharing_off_lists_no_folders(tmp_path):
    st = settings(tmp_path)
    st.ensure()
    st.update(share=False)
    assert body(st)["shares"]["directories"] == []


def test_downloads_folder_is_slskds_default_under_its_home(tmp_path):
    st = settings(tmp_path)
    assert st.downloads_dir == os.path.join(st.home, "downloads")
