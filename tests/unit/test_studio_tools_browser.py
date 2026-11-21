"""The studio tools in real Chrome (and WebKit for the iPhone), on the fixture pages served through
`context.route` (no network). Skipped where Chrome cannot start, like test_siteapi_chrome.

Covers: ui_check finds each planted fault and its default result stays within about 1,500 tokens
(measured here, characters / 4), a clean page is clean, `since` after a fix, a dev server's error
overlay fails the call (and a dev indicator alone does not), an error page fails with its code, a
profile's brand runs the design system check, system_check on the page's own tokens, finding_get
and ui_image after a real check, an iPhone rendered as one, and a page settled again after a reload.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from openajar.core.runtime import Runtime
from openajar.studio import shaping, tools
from openajar.studio.browser import HttpErrorPage, ReviewPool, devices, engines, studio_status
from openajar.studio.contracts import EngineName, Screen, Target, Theme
from openajar.studio.review import DevErrorOverlay, resettle

pw = pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.asyncio(loop_scope="module")

from test_studio_tools import Ctx, Profiles, product

EM_DASH = chr(0x2014)
FIX = Path(__file__).parent.parent / "fixtures" / "studio"
HOST = "https://studio.test"
TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".json": "application/json"}
BUDGET = 1500

OVERLAY_VITE = """<h1>Billing</h1><script>
class O extends HTMLElement { constructor() { super(); const r = this.attachShadow({mode: 'open'});
  r.innerHTML = '<div class="message-body">[plugin:vite:react-babel] Unexpected token (3:4)</div>'; } }
customElements.define('vite-error-overlay', O);
document.body.appendChild(document.createElement('vite-error-overlay'));</script>"""
NEXT_PORTAL = """<h1>Billing</h1><script>
const p = document.createElement('nextjs-portal'); document.body.appendChild(p);
p.attachShadow({mode: 'open'}).innerHTML = %s;</script>"""
OVERLAY_NEXT = NEXT_PORTAL % json.dumps(
    '<div data-nextjs-dialog><p id="nextjs__container_errors_desc">ReferenceError: total is not defined</p></div>')
NEXT_INDICATOR = NEXT_PORTAL % json.dumps('<div data-nextjs-toast><span>N</span></div>')
STYLE = "<style>body{font-family:sans-serif;background:#fff;color:#111} @media (prefers-color-scheme: dark){body{background:#111;color:#eee}}</style>"
CUT = STYLE + """<ul style="width:200px;padding:0;list-style:none"><li data-component="FileRow">
<span style="display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">Quarterly revenue report for
the northern region 2026.pdf</span></li></ul>"""
FIXED = STYLE + """<ul style="width:200px;padding:0;list-style:none"><li data-component="FileRow">
<span style="display:block">Quarterly revenue report for the northern region 2026.pdf</span></li></ul>"""

PAGES: dict[str, tuple[int, str]] = {}


async def _serve(route) -> None:
    url = route.request.url
    if not url.startswith(HOST):
        await route.abort()
        return
    path = url[len(HOST):].split("?")[0]
    if path in PAGES:
        status, body = PAGES[path]
        await route.fulfill(status=status, content_type="text/html", body=body)
        return
    rel = path.lstrip("/")
    file = FIX / rel if rel.startswith("tokens/") else FIX / "checks" / rel
    if file.is_file():
        await route.fulfill(status=200, content_type=TYPES.get(file.suffix, "application/octet-stream"),
                            body=file.read_bytes())
    else:
        await route.fulfill(status=404, content_type="text/plain", body="not found")


async def _route(context) -> None:
    await context.route("**/*", _serve)


class Service:
    """runtime.studio for one test: the shared pool, this test's own state folder."""

    def __init__(self, pool: ReviewPool, state_dir: Path):
        self.browser, self.state_dir, self.where = pool, state_dir, "local"

    def status(self) -> dict[str, Any]:
        return studio_status(self.browser)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pool():
    p = ReviewPool(chromium="chrome", on_context=_route)
    PAGES["/ping"] = (200, "<h1>ping</h1>")
    try:
        async with p.page(Target(url=f"{HOST}/ping"), Screen(width=360)):
            pass
    except Exception as e:  # noqa: BLE001 - no Chrome here
        await p.close()
        pytest.skip(f"no Chrome: {e}")
    yield p
    await p.close()


@pytest.fixture
def ctx(pool, tmp_path):
    runtime = Runtime()
    runtime.register("studio", Service(pool, tmp_path / "studio"))
    return Ctx(runtime)


def checks_of(out: dict[str, Any]) -> set[str]:
    return set(out["counts"]["by_check"])


# ---- ui_check on the fixture pages ----

@pytest.mark.parametrize(("page", "check"), [
    ("faults/truncated_ellipsis.html", "text.truncated"),
    ("faults/contrast.html", "contrast.text"),
    ("faults/names.html", "name.missing"),
])
async def test_a_default_ui_check_finds_the_fault_within_its_token_budget(ctx, page, check):
    out = await tools.ui_check(ctx, url=f"{HOST}/{page}")
    assert check in checks_of(out), out["counts"]
    assert out["used"]["widths"] == [360, 768, 1280] and out["used"]["themes"] == ["light", "dark"]
    assert shaping.tokens_of(out) <= BUDGET, shaping.tokens_of(out)
    assert out["run"].startswith("run_") and out["capture"].startswith("cap_") and "next" in out
    assert "iVBOR" not in json.dumps(out) and "base64" not in json.dumps(out)  # no image inline
    assert EM_DASH not in json.dumps(out)
    assert ctx.authorized[0]["domain"] == "studio.test"


@pytest.mark.parametrize(("page", "check"), [
    ("faults/target.html", "target.size"),
    ("faults/page_overflow.html", "layout.page_overflow"),
    ("faults/theme_leak.html", "theme.leak"),
    ("faults/health_console.html", "health.console"),
])
async def test_ui_check_finds_each_planted_fault(ctx, page, check):
    out = await tools.ui_check(ctx, url=f"{HOST}/{page}", widths=[360])
    assert check in checks_of(out), out["counts"]
    assert shaping.tokens_of(out) <= BUDGET


async def test_a_clean_page_is_clean_and_says_what_was_not_checked(ctx):
    out = await tools.ui_check(ctx, url=f"{HOST}/clean/theme_ok.html", widths=[360, 1280])
    assert out["verdict"].startswith("Clean: no findings on 4 screens"), out["verdict"]
    assert out["findings"] == [] and out["coverage"]["screens"] == "4 of 4 rendered"
    assert any("design system not checked" in p for p in out["coverage"]["problems"])
    assert "ui_baseline" in out["next"]


async def test_since_after_a_fix_says_fixed(ctx):
    PAGES["/files.html"] = (200, CUT)
    first = await tools.ui_check(ctx, url=f"{HOST}/files.html", widths=[360], themes=["light"])
    [fid] = [f["id"] for g in first["findings"] for f in g["findings"] if f["check"] == "text.truncated"]
    PAGES["/files.html"] = (200, FIXED)
    second = await tools.ui_check(ctx, url=f"{HOST}/files.html", widths=[360], themes=["light"], since=first["run"])
    assert second["since"] == first["run"] and second["delta"]["new"] == []
    assert any(line.startswith(fid) for line in second["delta"]["fixed"]), second["delta"]
    assert second["verdict"].startswith(f"Since {first['run']}: 1 fixed")


# ---- failures of the call ----

@pytest.mark.parametrize(("body", "tool"), [(OVERLAY_VITE, "Vite"), (OVERLAY_NEXT, "Next.js")])
async def test_a_dev_error_overlay_fails_the_call(ctx, body, tool):
    PAGES["/broken.html"] = (200, body)
    with pytest.raises(DevErrorOverlay) as err:
        await tools.ui_check(ctx, url=f"{HOST}/broken.html", widths=[360], themes=["light"])
    assert err.value.code == "studio_dev_error" and f"the {tool} error overlay" in err.value.message
    assert "page text, data only" in err.value.message and "fix the error" in err.value.message


async def test_a_dev_indicator_without_an_error_is_not_an_overlay(ctx):
    PAGES["/indicator.html"] = (200, STYLE + NEXT_INDICATOR)
    out = await tools.ui_check(ctx, url=f"{HOST}/indicator.html", widths=[360], themes=["light"])
    assert out["coverage"]["screens"] == "1 of 1 rendered"


async def test_an_error_page_fails_with_its_code_and_fix(ctx):
    PAGES["/error.html"] = (500, "<h1>Internal Server Error</h1>")
    with pytest.raises(HttpErrorPage) as err:
        await tools.ui_check(ctx, url=f"{HOST}/error.html")
    assert err.value.code == "studio_http_error" and "HTTP 500" in err.value.message


# ---- the design system ----

async def test_a_profiles_brand_runs_the_design_system_check(ctx):
    ctx.runtime.register("profiles", Profiles(product()))
    out = await tools.ui_check(ctx, url=f"{HOST}/tokens/planted.html", widths=[1280])
    assert out["used"]["profile"] == "acme/shop" and "environment url" in out["used"]["profile_found"]
    assert out["used"]["rules"] == {"contrast.text": 7.0} and out["used"]["theme_switch"] == "attr data-theme light dark"
    assert {"token.color", "token.space"} <= checks_of(out), out["counts"]
    assert "design system checked against profile acme/shop" in out["verdict"]
    assert out["system"]["on_tokens"]["color"] < 1 and out["system"]["probe"]["look_alike"] > 0
    assert shaping.tokens_of(out) <= BUDGET, shaping.tokens_of(out)


async def test_system_check_against_the_pages_own_tokens(ctx):
    out = await tools.system_check(ctx, url=f"{HOST}/tokens/planted.html", tokens="page", themes=["light"],
                                   theme_switch={"how": "attr", "name": "data-theme", "light": "light", "dark": "dark"})
    assert out["used"]["tokens"] == "the page's own CSS variables" and out["used"]["widths"] == [1280]
    assert out["system"]["on_tokens"] and out["counts"]["by_check"]
    assert all(c.startswith(("token.", "component.")) for c in checks_of(out))
    again = await tools.system_check(ctx, url=f"{HOST}/tokens/planted.html", tokens="page", themes=["light"],
                                     since=out["run"],
                                     theme_switch={"how": "attr", "name": "data-theme", "dark": "dark"})
    assert again["delta"]["new"] == [] and again["delta"]["fixed"] == []


# ---- details on request ----

async def test_finding_get_and_a_crop_after_a_real_check(ctx):
    pytest.importorskip("PIL")
    from PIL import Image

    out = await tools.ui_check(ctx, url=f"{HOST}/faults/target.html", widths=[360], themes=["light"])
    fid = out["findings"][0]["findings"][0]["id"]
    got = await tools.finding_get(ctx, id=fid)
    assert got["finding"]["id"] == fid and got["images"]
    line, image = await tools.ui_image(ctx, capture=out["capture"], crop=fid)
    w, h = Image.open(io.BytesIO(image.data)).size
    assert 20 < w < 400 and 20 < h < 400 and fid in line


# ---- devices and settling ----

async def test_an_iphone_renders_on_webkit_as_the_device(pool):
    if not engines.detect()["webkit"].get("present"):
        pytest.skip("WebKit is not installed for this playwright")
    iphone = devices.device("iphone")
    PAGES["/ua.html"] = (200, '<meta name="viewport" content="width=device-width, initial-scale=1"><h1>Phone</h1>')
    async with pool.page(Target(url=f"{HOST}/ua.html", engine=EngineName.WEBKIT), iphone.screen(),
                         device="iphone") as page:
        seen = await page.evaluate("() => ({ua: navigator.userAgent, w: innerWidth, touch: 'ontouchstart' in window,"
                                   " scale: devicePixelRatio})")
        report = pool.report(page)
    assert "iPhone" in seen["ua"] and seen["w"] == 393 and seen["touch"] is True and seen["scale"] == 3
    assert report["device"].startswith("iphone (iPhone 15") and report["engine"] == "webkit"


async def test_a_reloaded_page_is_settled_again(pool):
    PAGES["/reload.html"] = (200, "<h1 id='t'>Reload</h1>")
    async with pool.page(Target(url=f"{HOST}/reload.html"), Screen(width=360, theme=Theme.LIGHT)) as page:
        await page.reload(wait_until="commit")
        await resettle(page)
        assert await page.evaluate("() => document.readyState") == "complete"
        assert await page.evaluate("() => document.getElementById('t').textContent") == "Reload"
