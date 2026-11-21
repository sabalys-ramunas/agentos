"""The review browser's parts that need no browser: input checks, studio_status, the service on the
node and the dev node, the error catalog entries, and that it has no path to lanes or the vault."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from openajar.core import catalog
from openajar.core.errors import InvalidRequest
from openajar.core.http_errors import status_of
from openajar.core.runtime import Runtime
from openajar.node.daemon import FEATURE_SERVICES
from openajar.studio.browser import ReviewPool, engines, service, studio_status
from openajar.studio.browser.pool import check_target, check_url
from openajar.studio.contracts import Screen, Step, Target, ThemeSwitch

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "openajar" / "studio" / "browser"


@pytest.mark.parametrize("url", ["http://localhost:3000/", "https://example.com/a?b=1", "http://127.0.0.1:5173",
                                 "http://[::1]:8080/"])
def test_http_and_https_urls_are_allowed_including_localhost(url):
    assert check_url(url) == url


@pytest.mark.parametrize("url", ["file:///etc/passwd", "chrome://settings", "javascript:alert(1)", "about:blank",
                                 "data:text/html,hi", "localhost:3000", "http://"])
def test_other_schemes_are_refused(url):
    with pytest.raises(InvalidRequest, match="http or https"):
        check_url(url)


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data/", "http://metadata.google.internal/",
                                 "http://[fe80::1]/"])
def test_cloud_metadata_addresses_are_refused(url):
    with pytest.raises(InvalidRequest, match="metadata"):
        check_url(url)


def test_a_state_without_steps_or_a_step_without_its_selector_is_refused_before_opening():
    t = Target(url="http://localhost:3000/", states={"menu": [Step(do="click")], "go": [Step(do="goto", value="file:///x")]})
    with pytest.raises(InvalidRequest, match="state 'nope' has no steps.*defined: go, menu"):
        check_target(t, Screen(width=360, state="nope"))
    with pytest.raises(InvalidRequest, match="step 1 \\(click\\) needs a selector"):
        check_target(t, Screen(width=360, state="menu"))
    with pytest.raises(InvalidRequest, match="http or https"):
        check_target(t, Screen(width=360, state="go"))
    assert check_target(t, Screen(width=360)) == []


@pytest.mark.parametrize("switch", [ThemeSwitch(how="attr", dark="dark"), ThemeSwitch(how="storage", name="theme"),
                                    ThemeSwitch(how="attr", name="data-theme")])
def test_a_theme_switch_that_cannot_work_is_refused(switch):
    with pytest.raises(InvalidRequest, match="theme_switch"):
        check_target(Target(url="http://localhost/", theme_switch=switch), Screen(width=360, theme="dark"))


def test_an_unknown_chromium_flavour_is_refused():
    with pytest.raises(InvalidRequest):
        ReviewPool(chromium="edge")  # type: ignore[arg-type]


def test_status_is_a_plain_dict_of_engines_with_install_commands(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(engines, "chrome_path", lambda: None)
    status = studio_status(ReviewPool(), where="local")
    assert status["where"] == "local" and status["default_engine"] == "chromium"
    for name in ("chromium", "firefox", "webkit"):
        assert status["engines"][name]["present"] is False
        assert f"-m playwright install {name}" in status["engines"][name]["install"]
    assert status["fonts"]["pinned"] is False and "not" not in str(status["fonts"]["set"])
    assert status["limits"]["max_pages"] == 4 and status["open_pages"] == 0
    assert "Chromium, the default engine, is missing" in status["next"]
    assert len(status["missing"]) >= 3


def test_status_finds_bundled_builds_and_chrome(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    expected = engines._browsers_json()
    for name in ("chromium-headless-shell", "webkit"):
        (tmp_path / f"{name.replace('-', '_')}-{expected[name]['revision']}").mkdir()
    monkeypatch.setattr(engines, "chrome_path", lambda: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    found = studio_status()["engines"]
    assert found["chromium"] == {"present": True, "flavor": "bundled",
                                 "version": expected["chromium-headless-shell"]["browserVersion"]}
    assert found["webkit"]["present"] is True and found["firefox"]["present"] is False
    assert engines.detect("chrome")["chromium"]["flavor"] == "chrome"


def test_the_studio_service_is_built_on_a_node_and_the_dev_node():
    assert "openajar.studio.browser.service" in FEATURE_SERVICES
    runtime = Runtime()
    built = service.build(runtime)
    assert runtime.studio is built and isinstance(built.browser, ReviewPool)
    assert built.where == "node" and built.status()["where"] == "node"
    assert built.state_dir == Path.home() / "studio"  # no config: the agentos user's home


def test_the_state_folder_is_under_the_node_home_or_the_laptop_home(tmp_path, monkeypatch):
    from openajar.core.config import NodeConfig

    monkeypatch.setenv("HOME", str(tmp_path / "laptop"))  # never this computer's own home folders

    built = service.build(Runtime(config=NodeConfig(name="n", home=tmp_path)))
    assert built.state_dir == tmp_path / "studio" and built.status()["state_dir"] == str(tmp_path / "studio")
    assert ReviewPool().state_dir == Path.home() / ".openajar" / "studio"
    assert ReviewPool().fonts_pinned is False


def test_on_the_dev_node_the_service_says_local(tmp_path, monkeypatch):
    from openajar.dev.lanes import DevLaneManager

    monkeypatch.setenv("HOME", str(tmp_path))  # never this computer's own home folders

    runtime = Runtime(lanes=object.__new__(DevLaneManager))
    built = service.build(runtime)
    assert built.where == "local" and built.state_dir == Path.home() / ".openajar" / "studio"


async def test_stopping_the_service_before_any_page_starts_nothing():
    built = service.build(Runtime())
    await built.stop()
    assert built.browser.running() == {}


def test_importing_the_browser_does_not_import_playwright():
    code = ("import sys, openajar.studio.browser, openajar.studio.browser.service; "
            "print('playwright' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_the_review_browser_has_no_path_to_lanes_the_vault_or_profiles():
    """Decision 1: it is not a lane. Nothing in the package imports the lane manager, the vault,
    lane profiles or the node's lane tools, nor asks the runtime for them."""
    banned = ("openajar.node", "openajar.vault", "openajar.profiles", "openajar.dev", "camoufox")
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(banned), (path.name, node.module)
            elif isinstance(node, ast.Import):
                assert not any(alias.name.startswith(banned) for alias in node.names), path.name
            elif isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("require", "get"):
                args = [a.value for a in node.args if isinstance(a, ast.Constant)]
                assert not any(a in ("vault", "flow_driver", "typist", "files") for a in args), (path.name, args)


CODES = ("studio_blank_page", "studio_http_error", "studio_page_not_loaded", "studio_state_failed",
         "studio_theme_not_applied", "studio_timeout")


@pytest.mark.parametrize("code", CODES)
def test_each_failure_has_a_catalog_entry_and_a_status(code):
    entry = catalog.entry(code)
    assert entry is not None and entry.agent and entry.fix
    assert status_of(code) in (409, 502, 504)
    assert "—" not in entry.meaning + entry.agent + entry.fix


def test_no_em_dash_in_the_package():
    for path in PACKAGE.glob("*.py"):
        assert "—" not in path.read_text(), path.name


class _Emitter:
    def __init__(self):
        self.handlers = {}

    def on(self, event, fn):
        self.handlers[event] = fn


class _Req:
    def __init__(self, kind):
        self.resource_type = kind


def test_the_network_wait_leaves_out_event_streams_and_websockets():
    from types import SimpleNamespace

    from openajar.studio.browser.pool import _Network, long_lived

    page = _Emitter()
    net = _Network(page)
    on = page.handlers
    source, ws, sse_fetch, xhr = _Req("eventsource"), _Req("websocket"), _Req("fetch"), _Req("xhr")
    for r in (source, ws, sse_fetch, xhr):
        on["request"](r)
    assert net.inflight == {sse_fetch, xhr} and net.streams == 2
    # a fetch that answers text/event-stream streams for as long as the page is open
    on["response"](SimpleNamespace(headers={"content-type": "text/event-stream; charset=utf-8"}, request=sse_fetch))
    on["response"](SimpleNamespace(headers={"content-type": "application/json"}, request=xhr))
    assert net.inflight == {xhr} and net.streams == 3
    on["requestfinished"](xhr)
    assert net.inflight == set()
    assert long_lived("text/event-stream") and not long_lived("text/html") and not long_lived(None)


def test_under_a_scratch_home_the_accounts_own_browser_builds_are_found(monkeypatch, tmp_path):
    account = tmp_path / "account"
    scratch = tmp_path / "scratch"
    cache = engines._cache_under(account)
    (cache / "firefox-1490").mkdir(parents=True)
    scratch.mkdir()
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(scratch))
    monkeypatch.setattr(engines, "_account_home", lambda: account)
    assert engines.browsers_dir() == cache
    assert [p.name for p in engines._bundled_dirs("firefox")] == ["firefox-1490"]
    engines.point_playwright_at_builds()
    import os

    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(cache)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
    engines._cache_under(scratch).mkdir(parents=True)          # the scratch HOME has its own: it wins
    assert engines.browsers_dir() == engines._cache_under(scratch)
