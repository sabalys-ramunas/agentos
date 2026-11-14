"""Per-lane identity keys and settings: file modes, refusals, the owner-only host list, the profile binding."""

from __future__ import annotations

import os
import stat
import threading

import pytest

from agentos.webbotauth import keys, settings
from agentos.webbotauth.keys import IdentityError
from agentos.webbotauth.settings import LaneIdentity
from agentos.webbotauth.verify import verify_request

LANE = "acme/research"


def mode(path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_create_makes_private_files_and_directories(tmp_path):
    secrets = tmp_path / "secrets"
    key = keys.create_key(secrets, LANE)
    path = keys.key_path(secrets, LANE)
    assert path == secrets / "webbotauth" / "acme" / "research.key"
    assert mode(path) == 0o600
    assert mode(path.parent) == mode(path.parent.parent) == mode(secrets) == 0o700
    assert path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    loaded = keys.load_key(secrets, LANE)
    assert loaded is not None and loaded.keyid == key.keyid
    assert keys.create_key(secrets, LANE).keyid == key.keyid  # kept, not replaced
    assert keys.create_key(secrets, LANE, replace=True).keyid != key.keyid
    assert keys.load_key(secrets, "acme/other") is None


def test_the_key_never_prints_and_signs_only_web_bot_auth(tmp_path):
    key = keys.create_key(tmp_path / "secrets", LANE)
    assert "PRIVATE" not in repr(key) and repr(key) == f"LaneKey(lane_id={LANE!r}, keyid={key.keyid!r})"
    assert set(key.public_jwk()) == {"kty", "crv", "x", "kid", "use", "alg"} and "d" not in key.public_jwk()
    assert not hasattr(key, "sign")
    signed = key.sign_request(method="GET", url="https://a.example/", signature_agent="https://agents.example")
    assert verify_request("GET", "https://a.example/", signed.headers, {key.keyid: key.public_key()}).valid


def test_racing_creators_end_with_one_key(tmp_path):
    secrets = tmp_path / "secrets"
    ids, errors = [], []

    def make():
        try:
            ids.append(keys.create_key(secrets, LANE).keyid)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=make) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(set(ids)) == 1
    assert [p.name for p in keys.key_path(secrets, LANE).parent.iterdir()] == ["research.key"]


@pytest.mark.parametrize("target", ["file", "dir", "root"])
def test_readable_files_and_directories_are_refused(tmp_path, target):
    secrets = tmp_path / "secrets"
    keys.create_key(secrets, LANE)
    path = keys.key_path(secrets, LANE)
    chosen = {"file": path, "dir": path.parent, "root": path.parent.parent}[target]
    os.chmod(chosen, 0o644 if target == "file" else 0o755)
    with pytest.raises(IdentityError, match="accessible by other users"):
        keys.load_key(secrets, LANE)


def test_a_symlinked_key_is_refused(tmp_path):
    secrets = tmp_path / "secrets"
    keys.create_key(secrets, LANE)
    path = keys.key_path(secrets, LANE)
    other = tmp_path / "elsewhere.key"
    os.replace(path, other)
    path.symlink_to(other)
    with pytest.raises(IdentityError, match="symlink"):
        keys.load_key(secrets, LANE)


def test_a_broken_key_file_is_refused(tmp_path):
    secrets = tmp_path / "secrets"
    keys.create_key(secrets, LANE)
    keys.key_path(secrets, LANE).write_bytes(b"not a key")
    with pytest.raises(IdentityError, match="not a valid identity key"):
        keys.load_key(secrets, LANE)


@pytest.mark.parametrize(("given", "want"), [
    ("example.com", "https://example.com"),
    ("HTTPS://Example.com/", "https://example.com"),
    ("https://example.com:443", "https://example.com"),
    ("https://example.com:8443", "https://example.com:8443"),
    ("http://127.0.0.1:6351", "http://127.0.0.1:6351"),
    ("http://localhost:6351/", "http://localhost:6351"),
    ("xn--bcher-kva.example", "https://xn--bcher-kva.example"),
])
def test_origins_are_normalized(given, want):
    assert settings.normalize_origin(given) == want


@pytest.mark.parametrize("bad", ["", "http://example.com", "*.example.com", "https://example.com/path",
                                 "https://example.com/?q=1", "https://user@example.com", "ftp://example.com",
                                 "https://[::1]:8443", "bücher.example", "https://exa mple.com",
                                 "https://-bad-.example", "https://example.com:0", "https://example.com:99999",
                                 "https://example.com/#frag", "ws://example.com"])
def test_origins_that_are_refused(bad):
    with pytest.raises(IdentityError):
        settings.normalize_origin(bad)


def test_agent_url_must_be_an_https_origin():
    assert settings.normalize_agent_url("https://Agents.example/") == "https://agents.example"
    for bad in ("http://agents.example", "http://127.0.0.1:1", "https://agents.example/dir", "agents.example"):
        with pytest.raises(IdentityError):
            settings.normalize_agent_url(bad)


def test_hosts_are_empty_by_default_and_need_a_reason(tmp_path):
    ident = LaneIdentity(lane_id=LANE, signature_agent="https://agents.example")
    assert ident.hosts == [] and ident.origins == []
    with pytest.raises(IdentityError, match="say why"):
        settings.add_host(ident, "example.com", reason="  ", by="owner:cli")
    entry = settings.add_host(ident, "Example.com", reason="read-only  research", by="owner:cli")
    assert entry.origin == "https://example.com" and entry.reason == "read-only research"
    settings.add_host(ident, "https://example.com", reason="again", by="owner:cli")
    assert ident.origins == ["https://example.com"] and ident.hosts[0].reason == "again"
    assert settings.remove_host(ident, "example.com", by="owner:cli") is True
    assert settings.remove_host(ident, "example.com", by="owner:cli") is False
    with pytest.raises(IdentityError, match="longer"):
        settings.add_host(ident, "example.com", reason="x" * 201, by="owner:cli")


def test_settings_round_trip_with_private_mode(tmp_path):
    secrets = tmp_path / "secrets"
    ident = LaneIdentity(lane_id=LANE, signature_agent="https://agents.example", profile=(1, 2))
    settings.add_host(ident, "example.com", reason="test", by="owner:cli")
    path = settings.save(secrets, ident)
    assert mode(path) == 0o600 and path.name == "research.json"
    back = settings.load(secrets, LANE)
    assert back is not None and back.origins == ["https://example.com"] and back.profile == (1, 2)
    assert settings.load(secrets, "acme/none") is None


def test_settings_refuse_unsafe_or_hand_widened_files(tmp_path):
    secrets = tmp_path / "secrets"
    ident = LaneIdentity(lane_id=LANE, signature_agent="https://agents.example")
    path = settings.save(secrets, ident)
    os.chmod(path, 0o644)
    with pytest.raises(IdentityError, match="accessible"):
        settings.load(secrets, LANE)
    os.chmod(path, 0o600)
    text = path.read_text().replace('"hosts": []', '"hosts": [{"origin": "http://example.com", "reason": "x", '
                                                  '"added_by": "agent", "added_at": "2026-11-04T00:00:00Z"}]')
    path.write_text(text)
    with pytest.raises(IdentityError):
        settings.load(secrets, LANE)
    settings.save(secrets, LaneIdentity(lane_id="acme/other"))
    os.replace(settings.settings_path(secrets, "acme/other"), path)
    with pytest.raises(IdentityError, match="belongs to lane acme/other"):
        settings.load(secrets, LANE)


def test_profile_binding_changes_with_a_new_directory(tmp_path):
    profile = tmp_path / "profile"
    assert settings.profile_binding(profile) is None
    profile.mkdir()
    first = settings.profile_binding(profile)
    assert first == settings.profile_binding(profile)
    os.rename(profile, tmp_path / "trash")
    profile.mkdir()
    assert settings.profile_binding(profile) != first
