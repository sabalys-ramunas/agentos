"""Browser version drift and the keep-device refresh: only the version tokens change."""

from __future__ import annotations

import json

from test_ops_support import fingerprint

from agentos.stealth import fingerprint as fp


def test_versions_from_ua_and_camoufox():
    assert fp.ua_major("Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0") == 150
    assert fp.ua_major("Mozilla/5.0 (X11; rv:149.0) Gecko/20100101") == 149
    assert fp.ua_major("curl/8") is None and fp.ua_major(None) is None
    assert fp.browser_major("158.0.1-beta.41") == 158 and fp.browser_major(None) is None
    assert fp.compare(158, 158) == "same"
    assert fp.compare(158, 150) == "older"
    assert fp.compare(158, 160) == "newer"
    assert fp.compare(None, 150) == "unknown"


def test_bump_changes_only_the_version_tokens():
    old = fingerprint(150, **{"headers.User-Agent": fingerprint(150)["navigator.userAgent"]})
    new, changed = fp.bump(old, 158)
    assert changed == ["navigator.userAgent", "headers.User-Agent"]
    assert new["navigator.userAgent"] == "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0"
    assert new["headers.User-Agent"] == new["navigator.userAgent"]
    assert {k: v for k, v in new.items() if k not in fp.VERSION_KEYS} == \
        {k: v for k, v in old.items() if k not in fp.VERSION_KEYS}
    assert new["navigator.appVersion"] == "5.0 (X11; Ubuntu)"  # Firefox puts no version there
    assert "Gecko/20100101" in new["navigator.userAgent"]  # the frozen Gecko token stays


def test_bump_is_a_no_op_when_current():
    same, changed = fp.bump(fingerprint(158), 158)
    assert changed == [] and same == fingerprint(158)


def test_refresh_writes_atomically_and_keeps_the_old_file(tmp_path):
    path = tmp_path / "fingerprint.json"
    original = json.dumps(fingerprint(150), indent=1)
    path.write_text(original)
    path.chmod(0o600)
    dry = fp.refresh(path, "a/b", 158, dry_run=True)
    assert dry.changed == ["navigator.userAgent"] and path.read_text() == original
    result = fp.refresh(path, "a/b", 158)
    assert result.old == 150 and result.new == 158
    assert result.backup == tmp_path / "fingerprint.json.v150.bak"
    assert result.backup.read_text() == original
    assert result.backup.stat().st_mode & 0o777 == 0o600 and path.stat().st_mode & 0o777 == 0o600
    data = json.loads(path.read_text())
    assert fp.pinned_major(data) == 158
    assert data["fonts"] == ["Arial", "DejaVu Sans"]
    assert not list(tmp_path.glob(".*.tmp"))
    again = fp.refresh(path, "a/b", 158)
    assert again.changed == [] and again.backup is None
