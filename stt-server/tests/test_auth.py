import os
import time

import pytest

from alefa_stt.auth import SLOW_AFTER, KeyStore, add_key_line, new_key, revoke_key


def test_key_round_trip(tmp_path):
    path = str(tmp_path / "keys")
    key, line = new_key("alice")
    add_key_line(path, line)
    store = KeyStore(path)
    assert store.verify(key) == "alice"
    assert key not in open(path).read()


def test_wrong_keys_fail_and_slow_down_after_a_run(tmp_path):
    path = str(tmp_path / "keys")
    key, line = new_key("alice")
    add_key_line(path, line)
    store = KeyStore(path)
    assert store.verify(key[:-1] + ("A" if key[-1] != "A" else "B")) is None
    assert store.verify("stt.bob.whatever") is None
    assert store.verify("not-a-key") is None
    assert store.verify(None) is None
    for _ in range(SLOW_AFTER):
        store.verify("nope")
    assert store.should_slow_down
    assert store.verify(key) == "alice"
    assert not store.should_slow_down


def test_revoke_and_reload_without_restart(tmp_path):
    path = str(tmp_path / "keys")
    key, line = new_key("alice")
    add_key_line(path, line)
    store = KeyStore(path)
    assert store.verify(key) == "alice"
    assert revoke_key(path, "alice")
    # Make sure the modification time moves even on coarse file systems.
    later = time.time() + 5
    os.utime(path, (later, later))
    assert store.verify(key) is None
    assert not revoke_key(path, "alice")


def test_names_are_checked_and_unique(tmp_path):
    path = str(tmp_path / "keys")
    with pytest.raises(ValueError):
        new_key("Bad Name")
    _, line = new_key("alice")
    add_key_line(path, line)
    with pytest.raises(ValueError):
        add_key_line(path, new_key("alice")[1])
