"""Files, uploads, downloads, popups, tabs and dialogs on real Camoufox (run on the node).

Uses the session browser and Xvfb display from conftest.py, plus this module's own fixture
server (the static fixture site and a few dynamic download routes) on AGENTOS_TEST_IO_PORT. Each
test gets a fresh context with a PageTracker, a DialogGuard and a lane-like files directory, and
every request to anything but the fixture server is aborted. One test launches a real lane
session through CamoufoxEngine to prove the wiring.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from agentos.core.errors import InvalidRequest
from agentos.core.models import ActionKind, LaneSpec
from agentos.node.camoufox.engine import CamoufoxEngine, PageTracker
from agentos.node.files import MB, FilesConfig, FileStatus, LaneFiles
from agentos.node.interaction import DialogPending, PlaywrightInteraction
from agentos.node.snapshot import find_refs

pytestmark = [pytest.mark.node, pytest.mark.asyncio(loop_scope="session")]

SITE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "site"
IO_PORT = int(os.environ.get("AGENTOS_TEST_IO_PORT", "6201"))
DIALOG_TIMEOUT = 2.0

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 200 + b"\xff\xd9"
PDF = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"


class _Handler(SimpleHTTPRequestHandler):
    """The fixture site plus /dyn/big.bin (2.5 MB, slowly), /dyn/slow.bin (slow, small) and
    /dyn/broken.bin (promises 1 MB, sends 300 KB, then drops the connection)."""

    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _stream(self, name: str, total: int, sent: int, chunk: int, pause: float) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(total))
        self.end_headers()
        written = 0
        try:
            while written < sent:
                n = min(chunk, sent - written)
                self.wfile.write(b"\x01" * n)
                self.wfile.flush()
                written += n
                time.sleep(pause)
        except OSError:
            return
        if sent < total:
            self.close_connection = True
            self.connection.shutdown(2)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/dyn/big.bin":
            return self._stream("big.bin", 2_500_000, 2_500_000, 64 * 1024, 0.05)
        if path == "/dyn/slow.bin":
            return self._stream("slow.mp4", 4_000_000, 4_000_000, 16 * 1024, 0.1)
        if path == "/dyn/broken.bin":
            return self._stream("backup.bin", 1_000_000, 300_000, 30_000, 0.1)
        return super().do_GET()


@pytest.fixture(scope="session")
def io_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", IO_PORT), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="io-fixture-site", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{IO_PORT}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def ia() -> PlaywrightInteraction:
    return PlaywrightInteraction(allow_private_targets=True)  # fixtures are served on 127.0.0.1


@pytest.fixture
def files(tmp_path: Path) -> LaneFiles:
    lane_dir = tmp_path / "lane"
    lane_dir.mkdir(mode=0o700)
    return LaneFiles(lane_dir / "files", FilesConfig(download_max_bytes=MB, download_stall_seconds=3.0))


@pytest_asyncio.fixture(loop_scope="session")
async def lane(browser: Any, io_url: str, files: LaneFiles) -> AsyncIterator[PageTracker]:
    """A context set up the way a lane session sets one up."""
    context = await browser.new_context(accept_downloads=True)
    await context.route(lambda url: not url.startswith(io_url + "/"), lambda route: route.abort())
    tracker = PageTracker(context, files=files, dialog_timeout=DIALOG_TIMEOUT)
    try:
        yield tracker
    finally:
        await tracker.close()
        await context.close()


def one_ref(text: str, role: str, name: str) -> str:
    refs = find_refs(text, role=role, name=name)
    assert len(refs) == 1, f"expected one {role} {name!r}, got {refs}\n{text}"
    return refs[0]


async def open_page(lane: PageTracker, ia: PlaywrightInteraction, url: str) -> Any:
    page = await lane.page()
    await ia.navigate(page, url)
    return page


async def wait_for(predicate, timeout: float = 10.0, step: float = 0.1):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(step)


async def wait_text(page: Any, selector: str, expected: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while (got := await page.text_content(selector)) != expected:
        if time.monotonic() > deadline:
            raise AssertionError(f"{selector} is {got!r}, not {expected!r}")
        await asyncio.sleep(0.1)


# ---- uploads ------------------------------------------------------------------------------

async def test_upload_through_a_visible_file_input_reads_back(lane, ia, io_url, files):
    stored = files.put_bytes("cover photo.png", PNG)
    page = await open_page(lane, ia, f"{io_url}/upload.html")
    snap = await ia.snapshot(page)
    ref = one_ref(snap.text, "button", "Cover image")  # Firefox exposes a file input as a button

    result = await ia.upload(page, ref, [files.path(stored.id)], mimes=[stored.mime], root=files.root / "inbox")

    assert result.ok and result.via == "input", result
    assert result.files == [{"name": "cover photo.png", "size": len(PNG), "type": "image/png"}]
    await wait_text(page, "#cover-out", f"cover photo.png {len(PNG)} image/png")


async def test_upload_through_a_button_answers_the_file_chooser(lane, ia, io_url, files):
    a = files.put_bytes("one.png", PNG)
    b = files.put_bytes("two", JPEG)  # the extension follows the bytes: two.jpg
    page = await open_page(lane, ia, f"{io_url}/upload.html")
    ref = one_ref((await ia.snapshot(page)).text, "button", "Add photos")

    result = await ia.upload(page, ref, [files.path(a.id), files.path(b.id)], mimes=[a.mime, b.mime],
                             root=files.root / "inbox")

    assert result.ok and result.via == "chooser", result
    assert [f["name"] for f in result.files] == ["one.png", "two.jpg"]
    await wait_text(page, "#gallery-out", f"one.png {len(PNG)} image/png | two.jpg {len(JPEG)} image/jpeg")


async def test_upload_into_a_widget_that_clears_its_input(lane, ia, io_url, files):
    stored = files.put_bytes("notes.txt", b"post ideas\n")
    page = await open_page(lane, ia, f"{io_url}/upload.html")
    ref = one_ref((await ia.snapshot(page)).text, "button", "Attach a file")

    result = await ia.upload(page, ref, [files.path(stored.id)], mimes=[stored.mime], root=files.root / "inbox")

    assert result.ok, result
    assert result.files == []  # the widget emptied it
    assert result.seen_by_page == [{"name": "notes.txt", "size": 11, "type": "text/plain"}]
    assert "cleared" in (result.detail or "")
    await wait_text(page, "#attach-out li", "notes.txt 11")


async def test_upload_refuses_wrong_type_wrong_target_and_outside_paths(lane, ia, io_url, files, tmp_path):
    pdf = files.put_bytes("terms.pdf", PDF)
    page = await open_page(lane, ia, f"{io_url}/upload.html")
    snap = (await ia.snapshot(page)).text
    inbox = files.root / "inbox"

    with pytest.raises(InvalidRequest, match="accepts 'image/"):
        await ia.upload(page, one_ref(snap, "button", "Cover image"), [files.path(pdf.id)], mimes=[pdf.mime],
                        root=inbox)
    with pytest.raises(InvalidRequest, match="did not open a file chooser"):
        await ia.upload(page, one_ref(snap, "button", "Does nothing"), [files.path(pdf.id)], mimes=[pdf.mime],
                        root=inbox, chooser_timeout_ms=1500)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    with pytest.raises(InvalidRequest, match="outside the lane inbox"):
        await ia.upload(page, one_ref(snap, "button", "Add photos"), [outside], root=inbox)
    assert await page.text_content("#cover-out") == "no cover"


async def test_clicking_an_upload_button_never_shows_a_native_picker(lane, ia, io_url):
    page = await open_page(lane, ia, f"{io_url}/upload.html")
    ref = one_ref((await ia.snapshot(page)).text, "button", "Add photos")

    result = await ia.act(page, ref, ActionKind.CLICK)

    assert result.ok
    assert "file chooser" in (result.detail or "") and "browser_upload" in result.detail
    assert lane.dialogs.chooser_count(page) == 1


# ---- downloads ----------------------------------------------------------------------------

def outbox(files: LaneFiles) -> list:
    return [f for f in files.list() if f.box == "outbox"]


async def test_downloads_land_in_the_outbox_with_safe_names(lane, ia, io_url, files):
    page = await open_page(lane, ia, f"{io_url}/downloads.html")
    snap = (await ia.snapshot(page)).text
    await ia.act(page, one_ref(snap, "link", "Export CSV"), ActionKind.CLICK)
    await ia.act(page, one_ref(snap, "button", "Export JSON"), ActionKind.CLICK)

    def complete() -> list | None:
        found = [f for f in outbox(files) if f.status is FileStatus.COMPLETE]
        return found if len(found) == 2 else None

    done = await wait_for(complete)
    by_kind = {f.mime: f for f in done}
    csv = by_kind["text/csv"]
    assert csv.name == "report.csv" and csv.url == f"{io_url}/files/report.csv"
    assert files.path(csv.id).read_bytes() == (SITE_DIR / "files" / "report.csv").read_bytes()
    blob = next(f for f in done if f.mime != "text/csv")
    # the page asked for "../../export: final?.json": one path component, no leading dot
    assert "/" not in blob.name and not blob.name.startswith(".") and blob.name.endswith(".json")
    assert files.path(blob.id).parent.parent == (files.root / "outbox").resolve()
    assert blob.url == "blob:"
    for f in done:
        assert files.path(f.id).stat().st_mode & 0o777 == 0o600


async def test_a_download_over_the_cap_is_cancelled_and_marked_failed(lane, ia, io_url, files):
    page = await open_page(lane, ia, f"{io_url}/downloads.html")
    await ia.act(page, one_ref((await ia.snapshot(page)).text, "link", "Download the archive"), ActionKind.CLICK)

    failed = await wait_for(lambda: next((f for f in outbox(files) if f.status is FileStatus.FAILED), None), 20)
    assert "larger than the 1.0 MB download limit" in failed.error
    assert not (files.root / "outbox" / failed.id / failed.name).exists()
    with pytest.raises(InvalidRequest, match="failed"):
        files.path(failed.id)


async def test_a_broken_download_is_partial_not_complete(lane, ia, io_url, files):
    page = await open_page(lane, ia, f"{io_url}/downloads.html")
    await ia.act(page, one_ref((await ia.snapshot(page)).text, "link", "Download the backup"), ActionKind.CLICK)

    # Firefox never finishes a download whose connection dropped; the stall watch ends it
    entry = await wait_for(lambda: next((f for f in outbox(files) if f.status is not FileStatus.DOWNLOADING), None),
                           20)
    assert entry.status is FileStatus.PARTIAL, entry
    assert "stalled" in entry.error and 0 < entry.size < 1_000_000
    assert not (files.root / "outbox" / entry.id / entry.name).exists()


async def test_closing_the_lane_mid_download_marks_it_partial(lane, ia, io_url, files):
    page = await open_page(lane, ia, f"{io_url}/downloads.html")
    await ia.act(page, one_ref((await ia.snapshot(page)).text, "link", "Download the video"), ActionKind.CLICK)
    running = await wait_for(lambda: next((f for f in outbox(files) if f.status is FileStatus.DOWNLOADING), None))
    assert running.name == "slow.mp4"
    await asyncio.sleep(1.0)

    await lane.close()

    entry = files.get(running.id)
    assert entry.status is FileStatus.PARTIAL and "lane closed" in entry.error


# ---- popups and tabs ----------------------------------------------------------------------

async def test_a_popup_becomes_active_and_closing_it_returns_to_the_opener(lane, ia, io_url):
    opener = await open_page(lane, ia, f"{io_url}/popup.html")
    result = await ia.act(opener, one_ref((await ia.snapshot(opener)).text, "button", "Continue with Fixture ID"),
                          ActionKind.CLICK)
    assert "new tab opened" in (result.detail or ""), result

    popup = await wait_for(lambda: lane.active if lane.active is not opener else None)
    assert popup.url == f"{io_url}/auth.html"
    tabs = await lane.tabs()
    assert [(t["index"], t["active"], t["opened_by"]) for t in tabs] == [(0, False, None), (1, True, 0)]

    page = await lane.page()
    snap = await ia.snapshot(page)
    assert "tabs: 2 open, this is tab 1" in snap.text
    closing = await ia.act(page, one_ref(snap.text, "button", "Allow"), ActionKind.CLICK)
    assert closing.ok and "tab closed" in (closing.detail or ""), closing

    assert await wait_for(lambda: lane.active is opener)
    assert (await lane.page()) is opener
    await wait_text(opener, "#who", "Signed in as Ada")
    assert [t["url"] for t in await lane.tabs()] == [f"{io_url}/popup.html"]


async def test_target_blank_opens_an_active_tab_and_tabs_can_switch(lane, ia, io_url):
    first = await open_page(lane, ia, f"{io_url}/popup.html")
    await ia.act(first, one_ref((await ia.snapshot(first)).text, "link", "Open help"), ActionKind.CLICK)
    help_tab = await wait_for(lambda: lane.active if lane.active is not first else None)
    await help_tab.wait_for_load_state("domcontentloaded")
    assert help_tab.url == f"{io_url}/help.html"

    switched = await lane.switch(0)
    assert switched is first and lane.active is first
    with pytest.raises(InvalidRequest, match="no tab 5"):
        await lane.switch(5)

    await lane.switch(1)
    await help_tab.close()
    assert lane.active is first
    assert len(await lane.tabs()) == 1


# ---- dialogs ------------------------------------------------------------------------------

async def test_an_alert_is_queued_shown_in_the_snapshot_and_answered(lane, ia, io_url):
    page = await open_page(lane, ia, f"{io_url}/dialogs.html")
    save = one_ref((await ia.snapshot(page)).text, "button", "Save")

    started = time.monotonic()
    result = await ia.act(page, save, ActionKind.CLICK)
    assert time.monotonic() - started < DIALOG_TIMEOUT + 8  # returned while the alert was open
    assert "alert dialog" in (result.detail or "") and "Saved!" in result.detail

    dialog = lane.dialogs.current(page)
    assert dialog is not None and dialog.type == "alert" and dialog.message == "Saved!"
    snap = await ia.snapshot(page)
    assert snap.text.startswith('- alert dialog is open on this tab: "Saved!"') and snap.ref_count == 0
    with pytest.raises(DialogPending, match="browser_dialog_answer"):
        await ia.act(page, save, ActionKind.CLICK)

    answered = await lane.dialogs.answer(page, accept=True)
    assert answered.outcome == "accepted" and answered.answered_by == "agent"
    await wait_text(page, "#result", "saved")


async def test_confirm_accept_and_dismiss(lane, ia, io_url):
    page = await open_page(lane, ia, f"{io_url}/dialogs.html")
    delete = one_ref((await ia.snapshot(page)).text, "button", "Delete draft")

    await ia.act(page, delete, ActionKind.CLICK)
    await lane.dialogs.answer(page, accept=False)
    await wait_text(page, "#result", "kept")

    delete = one_ref((await ia.snapshot(page)).text, "button", "Delete draft")
    await ia.act(page, delete, ActionKind.CLICK)
    d = lane.dialogs.current(page)
    assert d.type == "confirm" and d.message == "Delete this draft?"
    await lane.dialogs.answer(page, accept=True)
    await wait_text(page, "#result", "deleted")


async def test_prompt_answered_with_text(lane, ia, io_url):
    page = await open_page(lane, ia, f"{io_url}/dialogs.html")
    await ia.act(page, one_ref((await ia.snapshot(page)).text, "button", "Rename"), ActionKind.CLICK)
    d = lane.dialogs.current(page)
    assert d.type == "prompt" and d.default == "Untitled"
    await lane.dialogs.answer(page, accept=True, text="Launch week")
    await wait_text(page, "#result", "renamed to Launch week")


async def test_beforeunload_on_navigate_waits_for_an_answer(lane, ia, io_url):
    page = await open_page(lane, ia, f"{io_url}/dialogs.html")
    await ia.act(page, one_ref((await ia.snapshot(page)).text, "textbox", "Draft text"), ActionKind.TYPE, "hello")

    with pytest.raises(DialogPending, match="beforeunload"):
        await ia.navigate(page, f"{io_url}/help.html")
    assert lane.dialogs.current(page).type == "beforeunload"
    await lane.dialogs.answer(page, accept=False)  # stay
    await asyncio.sleep(0.5)
    assert page.url == f"{io_url}/dialogs.html"
    assert await page.input_value("#draft") == "hello"

    with pytest.raises(DialogPending):
        await ia.navigate(page, f"{io_url}/help.html")
    await lane.dialogs.answer(page, accept=True)  # leave: the navigation that was asked for continues
    await wait_for(lambda: page.url == f"{io_url}/help.html")


async def test_beforeunload_from_a_link_click(lane, ia, io_url):
    page = await open_page(lane, ia, f"{io_url}/dialogs.html")
    snap = (await ia.snapshot(page)).text
    await ia.act(page, one_ref(snap, "textbox", "Draft text"), ActionKind.TYPE, "x")
    result = await ia.act(page, one_ref(snap, "link", "Leave"), ActionKind.CLICK)
    assert "beforeunload dialog" in (result.detail or ""), result
    await lane.dialogs.answer(page, accept=True)
    await wait_for(lambda: page.url == f"{io_url}/help.html")


async def test_unanswered_dialogs_are_answered_after_the_timeout(lane, ia, io_url):
    page = await open_page(lane, ia, f"{io_url}/dialogs.html")
    await ia.act(page, one_ref((await ia.snapshot(page)).text, "button", "Delete draft"), ActionKind.CLICK)
    assert lane.dialogs.current(page) is not None

    await asyncio.sleep(DIALOG_TIMEOUT + 1.0)

    assert lane.dialogs.current(page) is None
    last = lane.dialogs.history()[0]
    assert (last.type, last.outcome, last.answered_by) == ("confirm", "dismissed", "timeout")
    await wait_text(page, "#result", "kept")

    await ia.act(page, one_ref((await ia.snapshot(page)).text, "button", "Save"), ActionKind.CLICK)
    await asyncio.sleep(DIALOG_TIMEOUT + 1.0)
    last = lane.dialogs.history()[0]
    assert (last.type, last.outcome, last.answered_by) == ("alert", "accepted", "timeout")
    await wait_text(page, "#result", "saved")


# ---- a real lane session ------------------------------------------------------------------

async def test_a_lane_session_tracks_popups_and_fills_its_outbox(xvfb_display, io_url, tmp_path):
    lane_dir = tmp_path / "lanes" / "test" / "io"
    lane_dir.mkdir(parents=True, mode=0o700)
    engine = CamoufoxEngine(dialog_timeout=DIALOG_TIMEOUT)
    engine.allow_private_network = True  # the fixture server is on 127.0.0.1
    session = await engine.launch(LaneSpec(id="test/io"), profile_dir=lane_dir / "profile",
                                  fingerprint_path=lane_dir / "fingerprint.json", display=xvfb_display, proxy=None)
    ia = PlaywrightInteraction(allow_private_targets=True)
    try:
        page = await session.page()
        await ia.navigate(page, f"{io_url}/downloads.html")
        await ia.act(page, one_ref((await ia.snapshot(page)).text, "link", "Export CSV"), ActionKind.CLICK)
        files = LaneFiles(lane_dir / "files")
        done = await wait_for(lambda: [f for f in files.list("outbox") if f.status is FileStatus.COMPLETE])
        assert done[0].name == "report.csv"
        assert files.path(done[0].id).parent.parent == (lane_dir / "files" / "outbox").resolve()

        await ia.navigate(page, f"{io_url}/popup.html")
        await ia.act(page, one_ref((await ia.snapshot(page)).text, "button", "Continue with Fixture ID"),
                     ActionKind.CLICK)
        popup = await wait_for(lambda: session.tracker.active if session.tracker.active is not page else None)
        assert (await session.page()) is popup
        await popup.close()
        assert (await session.page()) is page
    finally:
        await session.close()
