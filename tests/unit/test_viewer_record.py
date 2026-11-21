"""The live view's record button: a clip made in the browser from the page's own picture, saved on this computer."""

from __future__ import annotations

from pathlib import Path

from openajar.node import viewer_app

ROOT = Path(viewer_app.__file__).parent / "viewer"


def test_the_record_button_is_there_hidden_until_the_browser_can_record():
    page = (ROOT / "page.html").read_text()
    button = page.split('id="btn-record"', 1)[1].split(">", 1)[0]
    assert 'aria-label="Record a clip"' in button and 'aria-pressed="false"' in button and " hidden" in button
    assert 'aria-keyshortcuts="Alt+Shift+R"' in button
    js = (ROOT / "assets" / "viewer.js").read_text()
    assert "$('btn-record').hidden = !RECORD_FORMAT" in js  # shown only where a format records


def test_a_clip_never_leaves_the_browser():
    rec = (ROOT / "assets" / "record.js").read_text()
    code = "\n".join(line for line in rec.splitlines() if not line.lstrip().startswith("//"))
    for word in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "EventSource"):
        assert word not in code, word
    assert "captureStream(FPS)" in code and "URL.createObjectURL(blob)" in code and "a.download = name" in code
    assert "video/mp4" in code and "video/webm" in code  # MP4 where the browser records it, WebM otherwise
    assert "export const LIMIT_S = 180" in code


def test_the_page_keeps_its_strict_policy():
    src = Path(viewer_app.__file__).read_text()
    assert "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; " in src
    assert "connect-src 'self'{sockets}; font-src 'self'" in src and "media-src" not in src
