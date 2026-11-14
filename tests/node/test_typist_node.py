"""The vault typist on real Camoufox against local login fixtures (run on the node).

Two fixture origins (two ports on 127.0.0.1) serve tests/fixtures/site and record form
posts, so a test can check what a login form really submitted. Ports come from
AGENTOS_TEST_TYPIST_PORT (origin A) and the next port (origin B); the display and the
browser come from conftest.py. Only the two origins are reachable from the test pages.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import pyotp
import pytest
import pytest_asyncio

from agentos.core.errors import InvalidRequest, RefNotFound
from agentos.core.runtime import Runtime
from agentos.node.interaction import PlaywrightInteraction
from agentos.node.snapshot import find_refs
from agentos.node.typist import Locator, PlaywrightTypist, typist_factory
from agentos.vault import crypto
from agentos.vault.errors import ResolutionDenied, TypistAborted
from agentos.vault.login import fill_flow, login_flow
from agentos.vault.policy import FieldRole, Reason, Sink, classify_fields, decide
from agentos.vault.service import VaultService, keyfile_path
from agentos.vault.store import VaultStore

pytestmark = [pytest.mark.node, pytest.mark.asyncio(loop_scope="session")]

SITE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "site"
PORT_A = int(os.environ.get("AGENTOS_TEST_TYPIST_PORT", str(int(os.environ.get("AGENTOS_TEST_SITE_PORT", "6120")) + 2)))
PORT_B = PORT_A + 1

LANE = "test/typist"
USERNAME = "ada@example.org"
SECRET = "Tr0ub4dor&3 é-correct#horse"
SEED = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

# Runs in Camoufox's isolated world, invisible to the page.
WATCH_MUTATIONS_JS = """
() => {
  window.__agentosTestMutations = [];
  new MutationObserver((records) => {
    for (const r of records) window.__agentosTestMutations.push(r.type + ':' + (r.attributeName || ''));
  }).observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  return document.documentElement.outerHTML;
}
"""


class _Handler(SimpleHTTPRequestHandler):
    """Static fixture pages; POST /login/submit?next=/page.html records the form and redirects."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        length = min(int(self.headers.get("Content-Length") or 0), 65536)
        body = self.rfile.read(length).decode("utf-8", "replace")
        form = {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}
        self.server.posts.append((parts.path, form))  # type: ignore[attr-defined]
        target = parse_qs(parts.query).get("next", ["/login-done.html"])[-1]
        if not target.startswith("/") or target.startswith("//"):
            target = "/login-done.html"
        self.send_response(303)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()


@dataclass
class Sites:
    a: str
    b: str
    servers: list[ThreadingHTTPServer]

    @property
    def posts(self) -> list[tuple[str, dict[str, str]]]:
        return self.servers[0].posts  # type: ignore[attr-defined]


@pytest.fixture(scope="session")
def sites() -> Iterator[Sites]:
    servers = []
    for port in (PORT_A, PORT_B):
        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        server.daemon_threads = True
        server.posts = []  # type: ignore[attr-defined]
        threading.Thread(target=server.serve_forever, name=f"typist-site-{port}", daemon=True).start()
        servers.append(server)
    try:
        yield Sites(a=f"http://127.0.0.1:{PORT_A}", b=f"http://127.0.0.1:{PORT_B}", servers=servers)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest_asyncio.fixture(loop_scope="session")
async def tpage(browser: Any, sites: Sites) -> AsyncIterator[Any]:
    """A fresh page in its own context. Only the two fixture origins are reachable."""
    pg = await browser.new_page()
    allowed = (sites.a + "/", sites.b + "/")
    await pg.context.route(lambda url: not url.startswith(allowed), lambda route: route.abort())
    for server in sites.servers:
        server.posts.clear()  # type: ignore[attr-defined]
    try:
        yield pg
    finally:
        await pg.context.close()


def typist(page: Any) -> PlaywrightTypist:
    return PlaywrightTypist(page, interaction=PlaywrightInteraction(allow_private_targets=True))


async def goto(page: Any, url: str) -> None:
    await page.goto(url, wait_until="load")


def by_name(fields: list, name: str, *, origins: tuple[str, ...] | None = None):
    found = [f for f in fields if f.name == name and (origins is None or f.frame_origins == origins)]
    assert len(found) == 1, f"expected one field named {name!r}, got {found}"
    return found[0]


async def watch_frames(page: Any) -> dict[Any, str]:
    return {frame: await frame.evaluate(WATCH_MUTATIONS_JS) for frame in page.frames}


async def assert_untouched(page: Any, before: dict[Any, str]) -> None:
    await page.wait_for_timeout(300)
    assert set(page.frames) == set(before)
    for frame, html in before.items():
        assert await frame.evaluate("document.documentElement.outerHTML") == html, frame.url
        assert await frame.evaluate("window.__agentosTestMutations") == [], frame.url


def value_of(selector: str) -> str:
    return f"document.querySelector({json.dumps(selector)}).value"


# ---- reading ------------------------------------------------------------------------

async def test_fields_reads_the_login_form_without_touching_the_page(tpage, sites):
    await goto(tpage, f"{sites.a}/login.html")
    before = await watch_frames(tpage)
    t = typist(tpage)
    fields = await t.fields()
    assert await t.page_url() == f"{sites.a}/login.html"
    await assert_untouched(tpage, before)

    assert [(f.tag, f.type, f.name) for f in fields] == [
        ("input", "search", "q"), ("input", "hidden", "csrf"),
        ("input", "text", "username"), ("input", "password", "password")]
    q, csrf, user, pw = fields
    assert all(f.frame_origins == (sites.a,) for f in fields)
    assert user.autocomplete == "username" and user.id == "username"
    assert pw.autocomplete == "current-password" and pw.autocomplete_tokens == ("current-password",)
    assert user.form is not None and user.form == pw.form == csrf.form and q.form != user.form
    assert user.visible and pw.visible and not csrf.visible
    assert not (user.disabled or user.readonly or pw.disabled or pw.readonly)
    assert user.maxlength is None and user.ref is None
    assert len({f.locator for f in fields}) == 4
    assert Locator.parse(pw.locator).frames == () and Locator.parse(pw.locator).index == 3
    assert classify_fields(fields) == {user.locator: FieldRole.USERNAME, pw.locator: FieldRole.PASSWORD}
    assert await t.fields() == fields  # stable while the page does not change


async def test_fields_in_a_same_origin_frame_come_in_document_order(tpage, sites):
    await goto(tpage, f"{sites.a}/login-frame.html")
    before = await watch_frames(tpage)
    fields = await typist(tpage).fields()
    await assert_untouched(tpage, before)

    a = sites.a
    assert [(f.name, f.frame_origins) for f in fields] == [
        ("q", (a,)), ("q", (a, a)), ("csrf", (a, a)), ("username", (a, a)), ("password", (a, a)),
        ("newsletter", (a,))]
    pw = by_name(fields, "password")
    assert Locator.parse(pw.locator).frames == (0,)
    assert pw.form != by_name(fields, "newsletter").form and pw.visible


async def test_cross_origin_and_nested_frames_report_every_origin_on_the_way(tpage, sites):
    a, b = sites.a, sites.b
    inner = quote(f"{a}/login.html", safe="")
    await goto(tpage, f"{a}/login-frame.html?src={quote(f'{b}/login-frame.html?src={inner}', safe='')}")
    for _ in range(50):  # the frames are added by script, one level at a time
        if len(tpage.frames) == 3 and all(f.url != "about:blank" for f in tpage.frames):
            break
        await tpage.wait_for_timeout(100)
    for frame in tpage.frames:
        await frame.wait_for_load_state("load")
    before = await watch_frames(tpage)
    fields = await typist(tpage).fields()
    await assert_untouched(tpage, before)

    assert [(f.name, f.frame_origins) for f in fields] == [
        ("q", (a,)), ("q", (a, b)), ("q", (a, b, a)), ("csrf", (a, b, a)), ("username", (a, b, a)),
        ("password", (a, b, a)), ("newsletter", (a, b)), ("newsletter", (a,))]
    pw = by_name(fields, "password")
    assert Locator.parse(pw.locator).frames == (0, 0)


async def test_policy_refuses_a_login_form_in_a_cross_origin_frame(tpage, sites, tmp_path, monkeypatch):
    vault = await vault_with_entry(tmp_path, monkeypatch)
    await goto(tpage, f"{sites.a}/login-frame.html?src={quote(sites.b + '/login.html', safe='')}")
    fields = await typist(tpage).fields()
    pw = by_name(fields, "password", origins=(sites.a, sites.b))
    entry = (await vault.list_entries())[0]
    d = decide(entry, lane_id=LANE, sink=Sink.FORM_PASSWORD, url=tpage.url, field=pw, fields=fields)
    assert not d.allowed and d.reason is Reason.CROSS_ORIGIN_FRAME

    ctx = Ctx()
    with pytest.raises(ResolutionDenied) as info:
        await login_flow(ctx, vault, typist(tpage), lane_id=LANE)
    assert info.value.reason == Reason.CROSS_ORIGIN_FRAME.value
    assert ctx.authorized == []
    frame = next(f for f in tpage.frames if f.url.startswith(sites.b))
    assert await frame.evaluate(value_of("#username")) == "" and await frame.evaluate(value_of("#password")) == ""


async def test_fields_inside_an_open_shadow_root(tpage, sites):
    await goto(tpage, f"{sites.a}/login-shadow.html")
    fields = await typist(tpage).fields()
    assert [(f.name, f.type) for f in fields] == [("username", "text"), ("password", "password"), ("after", "text")]
    user, pw, after = fields
    assert user.form is not None and user.form == pw.form and after.form is None
    assert classify_fields(fields) == {user.locator: FieldRole.USERNAME, pw.locator: FieldRole.PASSWORD}


async def test_one_time_code_pages(tpage, sites):
    await goto(tpage, f"{sites.a}/login-otp.html")
    fields = await typist(tpage).fields()
    otp = by_name(fields, "otp")
    assert (otp.autocomplete, otp.inputmode, otp.maxlength) == ("one-time-code", "numeric", 6)
    assert classify_fields(fields) == {otp.locator: FieldRole.OTP}

    await goto(tpage, f"{sites.a}/login-otp-split.html")
    boxes = await typist(tpage).fields()
    assert [f.name for f in boxes] == [f"d{i}" for i in range(1, 7)]
    assert all(f.maxlength == 1 and f.inputmode == "numeric" for f in boxes)
    assert set(classify_fields(boxes).values()) == {FieldRole.OTP} and len(classify_fields(boxes)) == 6


async def test_page_overrides_of_dom_apis_do_not_fool_the_reads_or_the_guard(tpage, sites):
    await goto(tpage, f"{sites.a}/login-liar.html")
    t = typist(tpage)
    fields = await t.fields()
    decoy = by_name(fields, "comment")
    assert (decoy.type, decoy.autocomplete) == ("text", "")
    assert by_name(fields, "username").type == "text"
    pw = await t.focus(by_name(fields, "password"))
    with pytest.raises(TypistAborted, match="focus left the field"):
        await t.type_secret(pw, SECRET)
    assert len(await tpage.evaluate(value_of("#decoy"))) <= 1


# ---- typing -------------------------------------------------------------------------

async def test_focus_and_type_fill_the_right_fields(tpage, sites):
    await goto(tpage, f"{sites.a}/login.html")
    before = await watch_frames(tpage)
    t = typist(tpage)
    fields = await t.fields()
    user = await t.focus(replace(by_name(fields, "username"), ref="e7"))
    assert user.ref == "e7" and user.locator == by_name(fields, "username").locator
    await t.type_secret(user, USERNAME)
    pw = await t.focus(by_name(fields, "password"))
    await t.type_secret(pw, SECRET)
    assert await tpage.evaluate(value_of("#username")) == USERNAME
    assert await tpage.evaluate(value_of("#password")) == SECRET
    assert await tpage.evaluate(value_of("#q")) == ""
    await assert_untouched(tpage, before)  # values changed, the DOM did not


async def test_type_secret_replaces_what_the_field_held(tpage, sites):
    await goto(tpage, f"{sites.a}/login.html")
    t = typist(tpage)
    user = await t.focus(by_name(await t.fields(), "username"))
    await t.type_secret(user, "someone-else")
    user = await t.focus(user)
    await t.type_secret(user, USERNAME)
    assert await tpage.evaluate(value_of("#username")) == USERNAME


async def test_typing_into_a_same_origin_frame(tpage, sites):
    await goto(tpage, f"{sites.a}/login-frame.html")
    t = typist(tpage)
    fields = await t.fields()
    pw = await t.focus(by_name(fields, "password"))
    assert pw.frame_origins == (sites.a, sites.a)
    await t.type_secret(pw, SECRET)
    frame = tpage.frames[1]
    assert await frame.evaluate(value_of("#password")) == SECRET
    assert await tpage.evaluate(value_of("#q")) == "" and await frame.evaluate(value_of("#q")) == ""


@pytest.mark.parametrize(("to", "how"), [("field", "timeout"), ("field", "sync"), ("frame", "timeout"),
                                         ("frame", "sync")])
async def test_focus_steal_stops_typing_at_once(tpage, sites, to, how):
    await goto(tpage, f"{sites.a}/login-hostile.html?after=3&to={to}&how={how}&other={quote(sites.b, safe='')}")
    t = typist(tpage)
    pw = await t.focus(by_name(await t.fields(), "password"))
    aborted: TypistAborted | None = None
    try:
        await t.type_secret(pw, SECRET)
    except TypistAborted as e:
        aborted = e

    await tpage.wait_for_timeout(1000)  # anything still queued would have arrived by now
    typed = await tpage.evaluate(value_of("#password"))
    keys = 0
    if to == "field":
        stolen = await tpage.evaluate(value_of("#decoy"))
    else:
        frame = next(f for f in tpage.frames if f.url == f"{sites.b}/capture.html")
        stolen = await frame.evaluate(value_of("#cap"))
        keys = int(await frame.evaluate("document.body.dataset.keys"))
    if aborted is None:
        # Firefox ignores iframe.focus() called inside a keydown handler: focus never left
        # the password field, so every key went there and none reached the frame.
        assert (to, how) == ("frame", "sync")
        assert typed == SECRET and stolen == "" and keys == 0
        return
    assert str(aborted) == "typing stopped: focus left the field"
    assert aborted.__context__ is None and aborted.__cause__ is None
    # Characters 1 and 2 reach the password field; the switch comes at character 3 (in its
    # keydown handler with how=sync, so that one character may land elsewhere). At most one
    # character ever reaches the other field, and nothing after it is typed anywhere.
    assert len(stolen) <= 1 and keys <= 1
    assert SECRET.startswith(typed + stolen) and 2 <= len(typed) <= 3 and len(typed + stolen) <= 4


async def test_navigation_mid_typing_stops_typing(tpage, sites):
    capture = f"{sites.b}/capture.html"
    await goto(tpage, f"{sites.a}/login-nav.html?after=3&to={quote(capture, safe='')}")
    t = typist(tpage)
    pw = await t.focus(by_name(await t.fields(), "password"))
    with pytest.raises(TypistAborted) as info:
        await t.type_secret(pw, SECRET)
    assert str(info.value).startswith("typing stopped: ")
    assert info.value.__context__ is None
    await tpage.wait_for_url(capture)
    await tpage.wait_for_timeout(1000)
    assert int(await tpage.evaluate("document.body.dataset.keys")) <= 1
    assert len(await tpage.evaluate(value_of("#cap"))) <= 1


async def test_stale_locators_are_rejected(tpage, sites):
    await goto(tpage, f"{sites.a}/login.html")
    t = typist(tpage)
    fields = await t.fields()
    user = by_name(fields, "username")
    # The page inserts a field before it (a banner, a promo box): the same place now holds
    # another element, so the old locator must not resolve to it.
    await tpage.evaluate("""() => {
      const i = document.createElement('input'); i.name = 'promo';
      document.getElementById('login').prepend(i);
    }""")
    with pytest.raises(RefNotFound):
        await t.focus(user)
    with pytest.raises(TypistAborted, match="no longer on the page"):
        await t.type_secret(user, SECRET)
    with pytest.raises(TypistAborted, match="no longer on the page"):
        await t.press(user, "Enter")

    pw = by_name(await t.fields(), "password")
    await tpage.evaluate("document.getElementById('password').type = 'text'")  # same element, other shape
    with pytest.raises(RefNotFound):
        await t.focus(pw)
    for bad in ("#password", "pw1:top:0:" + "0" * 32, "pw1:5:0:" + pw.locator[-32:]):
        with pytest.raises(RefNotFound):
            await t.focus(replace(pw, locator=bad))
    assert await tpage.evaluate(value_of("#username")) == "" and await tpage.evaluate(value_of("#password")) == ""


async def test_the_field_changing_type_mid_typing_stops_it(tpage, sites):
    await goto(tpage, f"{sites.a}/login.html")
    t = typist(tpage)
    pw = await t.focus(by_name(await t.fields(), "password"))

    async def reveal() -> None:  # a "show password" toggle flipping mid-typing
        await asyncio.sleep(0.45)
        await tpage.evaluate("document.getElementById('password').type = 'text'")

    task = asyncio.ensure_future(reveal())
    with pytest.raises(TypistAborted, match="the field changed"):
        await t.type_secret(pw, SECRET)
    await task
    assert len(await tpage.evaluate(value_of("#password"))) < len(SECRET)


async def test_field_for_ref_resolves_snapshot_refs_including_frames(tpage, sites):
    ia = PlaywrightInteraction(allow_private_targets=True)
    await goto(tpage, f"{sites.a}/login-frame.html")
    t = typist(tpage)
    fields = await t.fields()
    snap = await ia.snapshot(tpage)
    refs = find_refs(snap.text, role="textbox", name="Password")
    assert len(refs) == 1 and refs[0].startswith("f"), snap.text
    info = await t.field_for_ref(refs[0])
    assert info.ref == refs[0] and info.type == "password"
    assert info.frame_origins == (sites.a, sites.a)
    assert info.locator == by_name(fields, "password").locator

    news = find_refs(snap.text, role="textbox", name="Newsletter")
    assert (await t.field_for_ref(f"[ref={news[0]}]")).name == "newsletter"
    button = find_refs(snap.text, role="button", name="Log in")
    with pytest.raises(InvalidRequest):
        await t.field_for_ref(button[0])

    await goto(tpage, f"{sites.a}/login.html")
    with pytest.raises(RefNotFound):
        await t.field_for_ref(refs[0])


# ---- the vault, end to end ------------------------------------------------------------

class Ctx:
    def __init__(self) -> None:
        self.authorized: list[dict[str, Any]] = []
        self.traces: list[tuple[str, dict[str, Any]]] = []

    async def authorize(self, effect, **kwargs: Any) -> None:
        self.authorized.append({"effect": effect, **kwargs})

    def trace(self, kind: str, **fields: Any) -> None:
        self.traces.append((kind, fields))


async def vault_with_entry(tmp_path: Path, monkeypatch, **extra: Any) -> VaultService:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    os.chmod(home, 0o700)
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    key = os.urandom(32)
    crypto.write_keyfile(keyfile_path(home), key)
    VaultStore(home / "vault.db").initialize(mode="keyfile", master=key, kdf=None)
    vault = VaultService(home, socket_path=home / "run" / "vault.sock")
    await vault.add_entry(name="fixture", kind="password", domain="127.0.0.1", lanes=[LANE],
                          username=USERNAME, secret=SECRET, **extra)
    return vault


def assert_clean(*blobs: Any) -> None:
    text = json.dumps(blobs, default=str, ensure_ascii=False)
    assert SECRET not in text and USERNAME not in text


async def test_vault_login_end_to_end(tpage, sites, tmp_path, monkeypatch):
    vault = await vault_with_entry(tmp_path, monkeypatch)
    runtime = Runtime(vault=vault, interaction=PlaywrightInteraction(allow_private_targets=True))
    runtime.register("typist", typist_factory(runtime))  # how the daemon wires it
    await goto(tpage, f"{sites.a}/login.html")
    ctx = Ctx()
    result = await login_flow(ctx, vault, vault.typist_for(runtime)(tpage), lane_id=LANE)
    assert result == {"ok": True, "step": "done", "needs": None, "signal": "ok"}
    assert tpage.url == f"{sites.a}/login-done.html"
    assert sites.posts == [("/login/submit", {"csrf": "fixture-token", "username": USERNAME, "password": SECRET})]
    assert len(ctx.authorized) == 1 and "—" not in ctx.authorized[0]["summary"]
    assert [k for k, _ in ctx.traces] == ["vault.use", "vault.use"]
    assert_clean(result, ctx.authorized, ctx.traces)


async def test_vault_login_two_steps(tpage, sites, tmp_path, monkeypatch):
    vault = await vault_with_entry(tmp_path, monkeypatch)
    await goto(tpage, f"{sites.a}/login-step1.html")
    ctx = Ctx()
    result = await login_flow(ctx, vault, typist(tpage), lane_id=LANE)
    assert result == {"ok": True, "step": "done", "needs": None, "signal": "ok"}
    assert sites.posts == [("/login/submit", {"identifier": USERNAME}), ("/login/submit", {"passwd": SECRET})]
    assert_clean(result, ctx.authorized, ctx.traces)


@pytest.mark.parametrize("page", ["login-otp.html", "login-otp-split.html"])
async def test_vault_login_with_a_totp_step(tpage, sites, tmp_path, monkeypatch, page):
    vault = await vault_with_entry(tmp_path, monkeypatch, totp=SEED)
    await goto(tpage, f"{sites.a}/login.html?next=/{page}")
    ctx = Ctx()
    result = await login_flow(ctx, vault, typist(tpage), lane_id=LANE)
    assert result == {"ok": True, "step": "done", "needs": None, "signal": "ok"}
    (_, first), (_, second) = sites.posts
    assert first["password"] == SECRET
    code = second["otp"] if "otp" in second else "".join(second[f"d{i}"] for i in range(1, 7))
    assert len(code) == 6 and pyotp.TOTP(SEED).verify(code, valid_window=1)
    assert [f["sink"] for k, f in ctx.traces if k == "vault.use"] == ["form_username", "form_password", "form_otp"]
    assert_clean(result, ctx.authorized, ctx.traces)
    assert code not in json.dumps([result, ctx.traces], default=str)


async def test_vault_fill_by_snapshot_ref(tpage, sites, tmp_path, monkeypatch):
    vault = await vault_with_entry(tmp_path, monkeypatch)
    await goto(tpage, f"{sites.a}/login.html")
    snap = await PlaywrightInteraction(allow_private_targets=True).snapshot(tpage)
    ref = find_refs(snap.text, role="textbox", name="Password")[0]
    ctx = Ctx()
    result = await fill_flow(ctx, vault, typist(tpage), lane_id=LANE, field_ref=ref, entry="fixture")
    assert result == {"ok": True, "field": "password", "needs": None, "signal": "ok"}
    assert await tpage.evaluate(value_of("#password")) == SECRET
    assert await tpage.evaluate(value_of("#username")) == ""
    assert sites.posts == []  # vault_fill never submits
    assert_clean(result, ctx.authorized, ctx.traces)


async def test_vault_login_stops_when_the_page_steals_focus(tpage, sites, tmp_path, monkeypatch):
    vault = await vault_with_entry(tmp_path, monkeypatch)
    await goto(tpage, f"{sites.a}/login-hostile.html?after=5&to=field&other={quote(sites.b, safe='')}")
    ctx = Ctx()
    result = await login_flow(ctx, vault, typist(tpage), lane_id=LANE)
    assert result == {"ok": False, "step": "username", "needs": "human", "signal": "ok"}
    assert ("vault.abort", {"entry": ctx.traces[0][1]["entry"], "lane": LANE, "step": "username"}) in ctx.traces
    assert sites.posts == []
    assert len(await tpage.evaluate(value_of("#decoy"))) <= 1
    assert_clean(result, ctx.authorized, ctx.traces)
