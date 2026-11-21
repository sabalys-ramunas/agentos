"""The self-check loop as an agent runs it, in real Chrome on pages served through `context.route`
(no network). Skipped where Chrome cannot start, like test_studio_tools_browser.

What the shake-down on a real app found wrong, kept fixed:
- a `since` result after the last fault was fixed never says "All clear" while screens failed;
  a theme the caller left out is not a gap;
- ui_diff with `widths` compares only the screens asked for, in the themes of the baseline;
- ui_baseline learns what changes on its own (a random id) and ui_diff leaves it out, while a
  real change on the same page is still named;
- a page that renders nothing fails with the page's own console error in the message;
- a page that needs a service worker (an MSW mock) renders.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from openajar.core.runtime import Runtime
from openajar.studio import tools
from openajar.studio.browser import BlankPage, ReviewPool, studio_status
from openajar.studio.contracts import Screen, Target

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.asyncio(loop_scope="module")

from test_studio_tools import Ctx

HOST = "https://loop.test"
PAGES: dict[str, tuple[str, str]] = {}
NO_DARK = "<style>body{font-family:sans-serif;background:#fff;color:#111}</style>"
DARK = NO_DARK + "<style>@media (prefers-color-scheme: dark){body{background:#111;color:#eee}}</style>"
CLIPPED = """<div style="height:40px;overflow:hidden;width:300px;display:flex;align-items:flex-start"
 data-component="FilterBar"><span>Filters</span><button style="height:64px">Apply filters</button></div>"""
GROWN = CLIPPED.replace("height:40px", "min-height:40px")
NOISY = DARK + """<h1 style="color:#111">Orders</h1><p>Request <span id="rid"></span></p>
<script>document.getElementById('rid').textContent = crypto.randomUUID();</script>"""
SW_PAGE = """<div id="root"></div><script>
navigator.serviceWorker.register('/sw.js').then((reg) => {   // blocked, it resolves to nothing (as MSW found)
  if (!reg) throw new Error('no service worker'); document.getElementById('root').textContent = 'Mocked app'; })
.catch(
(e) => { console.error('could not register: ' + e); });</script>"""


async def _serve(route) -> None:
    url = route.request.url
    if not url.startswith(HOST):
        await route.abort()
        return
    path = url[len(HOST):].split("?")[0]
    kind, body = PAGES.get(path, ("text/plain", ""))
    await route.fulfill(status=200 if path in PAGES else 404, content_type=kind, body=body or "not found")


async def _route(context) -> None:
    await context.route("**/*", _serve)


def page(path: str, body: str, kind: str = "text/html") -> str:
    PAGES[path] = (kind, body if kind != "text/html" else f"<!doctype html><html lang=en><body>{body}</body></html>")
    return f"{HOST}{path}"


class Service:
    def __init__(self, pool: ReviewPool, state_dir: Path):
        self.browser, self.state_dir, self.where = pool, state_dir, "local"

    def status(self) -> dict[str, Any]:
        return studio_status(self.browser)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pool():
    p = ReviewPool(chromium="chrome", on_context=_route)
    try:
        async with p.page(Target(url=page("/ping", "<h1>ping</h1>")), Screen(width=360)):
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


async def test_since_never_says_all_clear_while_screens_failed(ctx):
    url = page("/nodark", NO_DARK + CLIPPED)
    first = await tools.ui_check(ctx, url=url, widths=[360])
    assert "layout.clipped" in first["counts"]["by_check"] and "1 of 2" in first["verdict"]
    assert 'themes=["light"]' in " ".join(first["coverage"]["problems"])   # the advice is not cut off
    page("/nodark", NO_DARK + GROWN)
    after = await tools.ui_check(ctx, url=url, widths=[360], since=first["run"])
    assert len(after["delta"]["fixed"]) == 1 and "coverage problem" in after["verdict"]
    assert "All clear" not in after["next"] and "read coverage" in after["next"]


async def test_since_says_all_clear_when_the_only_skip_was_asked_for(ctx):
    url = page("/light", NO_DARK + CLIPPED)
    first = await tools.ui_check(ctx, url=url, widths=[360], themes=["light"])
    page("/light", NO_DARK + GROWN)
    after = await tools.ui_check(ctx, url=url, widths=[360], themes=["light"], since=first["run"])
    assert after["next"].startswith("All clear")


async def test_ui_diff_with_widths_compares_only_those_screens_in_the_baselines_themes(ctx):
    url = page("/diff", NO_DARK + "<h1 style='color:#111'>Plans</h1><p>Pick one.</p>")
    run = await tools.ui_check(ctx, url=url, widths=[360, 1280], themes=["light"])
    await tools.ui_baseline(ctx, url=url, run=run["run"], noise=False)
    page("/diff", NO_DARK + "<h1 style='color:#b91c1c'>Plans</h1><p>Pick one.</p>")
    out = await tools.ui_diff(ctx, url=url, widths=[1280])
    assert [s["screen"] for s in out["screens"]] == ["default@1280-light"]
    assert out["verdict"].startswith("1 of 1 screens changed")
    assert any("#b91c1c" in line for line in out["screens"][0]["lines"])


async def test_the_baselines_noise_mask_leaves_a_random_id_out_of_ui_diff(ctx):
    url = page("/noise", NOISY)
    run = await tools.ui_check(ctx, url=url, widths=[1280], themes=["light"])
    kept = await tools.ui_baseline(ctx, url=url, run=run["run"])
    assert "ui_diff leaves them out" in kept["noise"]
    page("/noise", NOISY.replace("color:#111", "color:#b91c1c"))
    out = await tools.ui_diff(ctx, url=url)
    lines = out["screens"][0]["lines"]
    assert any("#b91c1c" in line for line in lines), lines
    assert not any("text changed" in line for line in lines), lines   # the request id is noise
    assert "noise_left_out" in out["used"]


async def test_without_a_mask_the_random_id_is_a_change(ctx):
    url = page("/noise2", NOISY)
    run = await tools.ui_check(ctx, url=url, widths=[1280], themes=["light"])
    kept = await tools.ui_baseline(ctx, url=url, run=run["run"], noise=False)
    assert "noise" not in kept
    out = await tools.ui_diff(ctx, url=url)
    assert any("text changed" in line for line in out["screens"][0]["lines"])


async def test_a_blank_page_quotes_its_own_console_error(pool):
    url = page("/broken", "<div id=root></div><script>const u = undefined; root.textContent = u.name;</script>")
    with pytest.raises(BlankPage) as err:
        async with pool.page(Target(url=url), Screen(width=360)):
            pass
    assert "reading 'name'" in err.value.message and "page text" in err.value.message


async def test_a_page_that_needs_a_service_worker_renders(ctx):
    page("/sw.js", "self.addEventListener('fetch', () => {});", kind="text/javascript")
    url = page("/mocked", DARK + SW_PAGE)
    out = await tools.ui_check(ctx, url=url, widths=[360], themes=["light"])
    assert out["verdict"].startswith("Clean"), out
