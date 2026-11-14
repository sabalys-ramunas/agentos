"""Assisted login on a real Camoufox, through the panel API (run on the node).

Fixture sign-in pages (tests/fixtures/site/assist) are served under the real hostnames by route
interception: anything else is aborted, so nothing reaches a real site.
A real lane manager drives the context's pages (PageTracker: popups become the active tab), the
vault's typist types, and the test plays the owner through /api/lanes/{ws}/{name}/login:

- Instagram style: email and password on one page; the screenshot paints over the password field;
  signed in by the session cookie; saved to the vault, which `login` then uses with no form.
- Google style: email, then password, then a captcha (stop, take control, the test "solves" it, hand
  back: the form comes back), then a code, then "tap 97 on your phone" (waits for the page to move).
- Reddit with "Continue with Google": the Google popup is followed like a person would, and the lane
  is signed in when the popup closes and Reddit moves on.

Every value the owner typed reached the site (the recorded form posts), and none is in a log line,
a trace or a published event.

Run on the node:  AGENTOS_TEST_SLOT=48 scripts/node-test.sh loginassist tests/node/test_login_assist_node.py -q
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from test_lanes import Clock, FakeDisplays
from test_vault_support import make_vault, private_home

from agentos.api import ApiSettings, build_api
from agentos.core.config import NodeConfig
from agentos.core.models import ControlState, LaneSpec
from agentos.node.daemon import build_services, node_runtime
from agentos.node.engine_camoufox import PageTracker
from agentos.node.interaction import PlaywrightInteraction
from agentos.node.lanes import LaneManagerImpl
from agentos.node.login_page import REDACT_RGBA
from agentos.node.store import LaneStore
from agentos.node.typist import typist_factory

pytestmark = [pytest.mark.node, pytest.mark.asyncio(loop_scope="session")]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "site" / "assist"
TOKEN = "node-token-for-the-login-assist-test"
EMAIL = "ada.lovelace@example.org"
PASSWORD = "Tr0ub4dor&3 é-correct#horse"
GPASSWORD = "an0ther-Gpass word!"
CODE = "424242"
SERVICES = ["agentos.api.events", "agentos.policy.service", "agentos.vault.service"]


class Site:
    """The fake web: fixture pages under real hostnames, form posts recorded, cookies like the real ones."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str]]] = []

    def page(self, name: str, *, cookies: list[str] = ()) -> dict[str, Any]:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        out: dict[str, Any] = {"status": 200, "headers": headers, "body": (FIXTURES / name).read_text()}
        if cookies:
            headers["Set-Cookie"] = "\n".join(cookies)
        return out

    @staticmethod
    def go(location: str, *, cookies: list[str] = ()) -> dict[str, Any]:
        """A page that moves on by script: Juggler does not route redirect hops,
        so an HTTP redirect could leave the fixture web."""
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if cookies:
            headers["Set-Cookie"] = "\n".join(cookies)
        body = f"<!doctype html><title>Moving on</title><script>location.replace({json.dumps(location)})</script>"
        return {"status": 200, "headers": headers, "body": body}

    def answer(self, method: str, url: str, body: str) -> dict[str, Any] | None:
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path
        if method == "POST":
            self.posts.append((f"{host}{path}", {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}))
        routes = {
            ("GET", "www.instagram.com", "/accounts/login/"): lambda: self.page("ig-login.html"),
            ("POST", "www.instagram.com", "/accounts/login/"): lambda: self.go(
                "https://www.instagram.com/", cookies=["sessionid=ig-session-1; Path=/; Secure; HttpOnly",
                                                       "ds_user_id=4242; Path=/; Secure"]),
            ("GET", "www.instagram.com", "/"): lambda: self.page("ig-home.html"),
            ("GET", "accounts.google.com", "/"): lambda: self.go("https://accounts.google.com/v3/signin/identifier"),
            ("GET", "accounts.google.com", "/v3/signin/identifier"): lambda: self.page("g-identifier.html"),
            ("POST", "accounts.google.com", "/v3/signin/identifier"): lambda: self.go(
                "https://accounts.google.com/v3/signin/challenge/pwd"),
            ("GET", "accounts.google.com", "/v3/signin/challenge/pwd"): lambda: self.page("g-password.html"),
            ("POST", "accounts.google.com", "/v3/signin/challenge/pwd"): lambda: self.go(
                "https://accounts.google.com/v3/signin/robot"),
            ("GET", "accounts.google.com", "/v3/signin/robot"): lambda: self.page("g-captcha.html"),
            ("GET", "www.google.com", "/recaptcha/api2/bframe"): lambda: self.page("recaptcha.html"),
            ("GET", "accounts.google.com", "/v3/signin/challenge/totp"): lambda: self.page("g-code.html"),
            ("POST", "accounts.google.com", "/v3/signin/challenge/totp"): lambda: self.go(
                "https://accounts.google.com/v3/signin/challenge/dp"),
            ("GET", "accounts.google.com", "/v3/signin/challenge/dp"): lambda: self.page("g-dp.html"),
            ("GET", "accounts.google.com", "/approved"): lambda: self.go(  # the phone said yes
                "https://myaccount.google.com/", cookies=["SID=g-session-1; Domain=.google.com; Path=/; Secure"]),
            ("GET", "myaccount.google.com", "/"): lambda: self.page("g-done.html"),
            ("GET", "www.reddit.com", "/login/"): lambda: self.page("rd-login.html"),
            ("GET", "accounts.google.com", "/o/oauth2/v2/auth"): lambda: self.page("g-oauth.html"),
            ("POST", "accounts.google.com", "/o/oauth2/v2/auth"): lambda: self.go("https://www.reddit.com/oauth-done"),
            ("GET", "www.reddit.com", "/oauth-done"): lambda: self.page(
                "rd-callback.html", cookies=["reddit_session=rd-session-1; Domain=.reddit.com; Path=/; Secure"]),
            ("GET", "www.reddit.com", "/"): lambda: self.page("rd-home.html"),
        }
        make = routes.get((method, host, path))
        return make() if make else None


class PageSession:
    pid = None
    alive = True

    def __init__(self, context: Any):
        self.tracker = PageTracker(context)

    async def page(self) -> Any:
        return await self.tracker.page()

    async def close(self) -> None:
        pass


class ContextEngine:
    name = "camoufox"

    def __init__(self, context: Any):
        self.context = context

    async def launch(self, spec, *, profile_dir, fingerprint_path, display, proxy):
        if not self.context.pages:
            await self.context.new_page()
        return PageSession(self.context)

    async def sweep_orphans(self, root):
        return []


class Node:
    def __init__(self, tmp_path: Path, context: Any, site: Site):
        self.site = site
        home = private_home(tmp_path)
        make_vault(home)
        self.config = NodeConfig(home=home, mcp_token=TOKEN, mcp_port=7400, self_check=False, display_base=480,
                                 vnc_base=5480, viewer_base=6480)
        self.lanes = LaneManagerImpl(self.config, LaneStore(self.config.db_path), ContextEngine(context),
                                     FakeDisplays(self.config), dict, clock=Clock(), reaper_interval=60)
        interaction = self.lanes.watch(PlaywrightInteraction(allow_private_targets=True))
        self.runtime = node_runtime(self.config, self.lanes, interaction)
        self.runtime.register("typist", typist_factory(self.runtime))
        build_services(self.runtime, SERVICES)
        self.events: list[tuple[str, Any]] = []
        bus = self.runtime.require("events")
        publish = bus.publish

        def record(kind: str, data: Any) -> Any:
            self.events.append((kind, json.loads(json.dumps(data, default=str))))
            return publish(kind, data)

        bus.publish = record
        self.app = build_api(self.runtime, ApiSettings(lane_pump_seconds=0, panel_dir=tmp_path / "none"))
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://localhost:7400",
                                      headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120)
        self.assist = None

    async def call(self, lane: str, action: str = "", body: dict | None = None, *, ok: bool = True) -> dict:
        ws, name = lane.split("/")
        path = f"/api/lanes/{ws}/{name}/login" + (f"/{action}" if action else "")
        r = await self.http.post(path, json=body or {})
        if ok:
            assert r.status_code == 200, (action, r.text)
        return r.json()

    async def get(self, lane: str, after: int | None = None, wait: int = 0) -> dict:
        ws, name = lane.split("/")
        q = f"?after={after}&wait={wait}" if after is not None else ""
        r = await self.http.get(f"/api/lanes/{ws}/{name}/login{q}")
        assert r.status_code == 200, r.text
        return r.json()

    async def until(self, lane: str, states: tuple[str, ...], login: dict, timeout: float = 60) -> dict:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while login.get("state") not in states:
            assert loop.time() < end, f"still {login.get('state')}: {login}"
            login = await self.get(lane, after=login.get("version", 0), wait=10)
        return login

    async def close(self) -> None:
        await self.http.aclose()
        svc = self.runtime.get("login_assist")
        if svc is not None:
            await svc.close()
        await self.lanes.shutdown()
        policy = self.runtime.get("policy")
        if policy is not None:
            policy.close()


@pytest_asyncio.fixture(loop_scope="session")
async def node(browser: Any, tmp_path: Path):
    site = Site()
    # A dead proxy: a request that escapes the routes below fails instead of reaching a real site.
    context = await browser.new_context(proxy={"server": "http://127.0.0.1:9"})

    async def handle(route: Any) -> None:
        req = route.request
        answer = site.answer(req.method, req.url, req.post_data or "")
        if answer is None:
            await route.abort()
        else:
            await route.fulfill(**answer)

    await context.route("**/*", handle)
    n = Node(tmp_path, context, site)
    for lane in ("test/instagram", "test/google", "test/reddit"):
        await n.lanes.create(LaneSpec(id=lane))
    try:
        yield n
    finally:
        await n.close()
        await context.close()


def _no_values_anywhere(node: Node, caplog: Any, values: list[str]) -> None:
    traces = node.runtime.require("policy").traces.query(limit=2000)[0]
    dumped = json.dumps(traces, default=lambda o: o.model_dump() if hasattr(o, "model_dump") else
                        getattr(o, "__dict__", str(o)))
    events = json.dumps(node.events, default=str)
    for v in values:
        assert v not in caplog.text, f"a value is in the log: {v[:3]}..."
        assert v not in dumped, f"a value is in a trace: {v[:3]}..."
        assert v not in events, f"a value is in an event: {v[:3]}..."
    assert any(t.tool == "panel.lane.login.step" for t in traces)  # the steps are in Activity, by name only


async def _field_box(page: Any, selector: str) -> dict:
    return await page.locator(selector).bounding_box()


async def test_instagram_style_one_page_signs_in_and_saves_to_the_vault(node: Node, caplog: Any):
    caplog.set_level(logging.DEBUG)
    lane = "test/instagram"
    login = await node.call(lane, "", {})
    login = await node.until(lane, ("ask",), login)
    step = login["step"]
    assert step["kind"] == "sign_in" and [f["name"] for f in step["fields"]] == ["username", "password"]
    assert [f["label"] for f in step["fields"]] == ["Phone number, username, or email", "Password"]
    assert step["submit"] == "Log in" and [c["label"] for c in step["choices"]] == ["Log in with Facebook"]
    assert node.lanes.get(lane).control.state == ControlState.HUMAN  # agents stop while it runs

    # the screenshot: the password field is painted over
    page = await node.lanes._lane(lane).session.page()
    box = await _field_box(page, "input[name=password]")
    r = await node.http.get("/api/lanes/test/instagram/login/screenshot")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    from test_login_assist import _decode

    pixels = _decode(r.content)
    cx, cy = int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
    assert tuple(pixels[cy, cx][:3]) == REDACT_RGBA[:3]

    # a stale step id is refused and nothing is typed
    r = await node.http.post("/api/lanes/test/instagram/login/step", json={"step": "s0", "values": {
        "username": EMAIL, "password": PASSWORD}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "login_step_changed" and PASSWORD not in r.text
    assert node.site.posts == []

    login = await node.call(lane, "step", {"step": step["id"], "values": {"username": EMAIL, "password": PASSWORD}})
    login = await node.until(lane, ("signed_in",), login)
    assert node.site.posts == [("www.instagram.com/accounts/login/", {"username": EMAIL, "password": PASSWORD})]
    assert login["can_save"] is True and "Signed in to Instagram" in login["note"]
    assert node.lanes.get(lane).control.state == ControlState.AGENT  # handed back as it was
    assert PASSWORD not in json.dumps(login) and EMAIL not in json.dumps(login)

    saved = await node.call(lane, "save", {})
    entry = saved["entry"]
    assert entry["domain"] == "instagram.com" and entry["lanes"] == [lane] and saved["can_save"] is False
    vault = node.runtime.require("vault")
    meta = await vault.get_entry(entry["id"])
    assert meta.has_username and meta.has_secret
    _no_values_anywhere(node, caplog, [PASSWORD, EMAIL])


async def test_google_style_steps_a_captcha_a_code_and_the_phone(node: Node, caplog: Any):
    caplog.set_level(logging.DEBUG)
    lane = "test/google"
    login = await node.until(lane, ("ask",), await node.call(lane, "", {}))
    step = login["step"]
    assert step["kind"] == "sign_in" and [f["name"] for f in step["fields"]] == ["username"]
    assert step["fields"][0]["label"] == "Email or phone" and step["heading"] == "Sign in"
    assert step["submit"] is None  # Google's Next is a plain button: Enter in the field

    login = await node.until(lane, ("ask",), await node.call(lane, "step", {"step": step["id"],
                                                                           "values": {"username": EMAIL}}))
    step = login["step"]
    assert [f["name"] for f in step["fields"]] == ["password"] and step["fields"][0]["secret"] is True

    # the password leads to a captcha: stop, say so, take control
    login = await node.until(lane, ("stopped",), await node.call(lane, "step", {
        "step": step["id"], "values": {"password": GPASSWORD}}))
    assert login["step"]["kind"] == "captcha" and "captcha" in login["note"] and "—" not in login["note"]
    login = await node.call(lane, "take")
    assert login["state"] == "paused" and node.lanes.get(lane).control.state == ControlState.HUMAN
    # the owner solves it in the live view (here: the test moves the page on), then hands back
    page = await node.lanes._lane(lane).session.page()
    await page.goto("https://accounts.google.com/v3/signin/challenge/totp")
    await node.lanes.hand_back(lane, by="owner")
    login = await node.until(lane, ("ask",), await node.get(lane))
    step = login["step"]
    assert step["kind"] == "code" and step["submit"] == "Next"
    assert node.lanes.get(lane).control.state == ControlState.HUMAN  # the form took the lane again

    login = await node.until(lane, ("waiting",), await node.call(lane, "step", {
        "step": step["id"], "values": {"code": "424 242"}}))
    step = login["step"]
    assert step["kind"] == "approve" and step["number"] == "97" and "tap 97 on your phone" in step["message"]
    assert [c["label"] for c in step["choices"]] == ["Resend it", "Try another way"]
    # the phone says yes: the page moves on its own, and the login follows
    await page.goto("https://accounts.google.com/approved")
    login = await node.until(lane, ("signed_in",), await node.get(lane))
    assert login["site"] == "google"
    posts = dict(node.site.posts)
    assert posts["accounts.google.com/v3/signin/identifier"]["identifier"] == EMAIL
    assert posts["accounts.google.com/v3/signin/challenge/pwd"]["Passwd"] == GPASSWORD
    assert posts["accounts.google.com/v3/signin/challenge/totp"]["totpPin"] == CODE
    assert "code" not in login and login["can_save"] is True
    done = await node.call(lane, "cancel")  # "no thanks" to the save: what was typed is dropped
    assert done["can_save"] is False and done["state"] == "signed_in"
    _no_values_anywhere(node, caplog, [EMAIL, GPASSWORD, CODE])


async def test_a_google_popup_is_followed_and_the_lane_signs_in_when_it_closes(node: Node, caplog: Any):
    caplog.set_level(logging.DEBUG)
    lane = "test/reddit"
    login = await node.until(lane, ("ask",), await node.call(lane, "", {}))
    step = login["step"]
    assert step["kind"] == "sign_in" and [(c["label"], c["kind"]) for c in step["choices"]] == [
        ("Continue with Google", "provider")]
    google = step["choices"][0]["id"]
    login = await node.until(lane, ("ask",), await node.call(lane, "step", {"step": step["id"], "choice": google}))
    assert login["host"] == "accounts.google.com" and login["step"]["heading"] == "Sign in with Google"
    assert [f["name"] for f in login["step"]["fields"]] == ["username", "password"]
    login = await node.call(lane, "step", {"step": login["step"]["id"],
                                           "values": {"username": EMAIL, "password": GPASSWORD}})
    login = await node.until(lane, ("signed_in",), login)
    assert login["site"] == "reddit"
    assert dict(node.site.posts)["accounts.google.com/o/oauth2/v2/auth"] == {"identifier": EMAIL,
                                                                             "Passwd": GPASSWORD}
    _no_values_anywhere(node, caplog, [EMAIL, GPASSWORD])
