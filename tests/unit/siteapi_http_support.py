"""A node for the /v1/sites tests: the real policy, freeze, runner, panel and MCP apps over real HTTP,
with a fake lane manager and the runner's fake driver on a fake portal. No browser, no real site."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any, Self

import httpx
import uvicorn
from fakes import ControlFakeLaneManager, FakeInteraction
from test_vault_support import make_vault, private_home

from openajar.api import ApiSettings, build_api
from openajar.api.auth import PanelStore
from openajar.core.config import NodeConfig
from openajar.core.models import Effect, LaneSpec, Principal
from openajar.node.daemon import build_node_app, build_services, node_runtime
from openajar.runner.fakes import El, FakeDriver, FakeSite, driver_factory
from openajar.runner.siteapis.models import SAMPLE, install_for_test, web_surface

TOKEN = "node-token-for-the-site-api-tests"
SERVICES = ["openajar.api.events", "openajar.policy.service", "openajar.freeze.service", "openajar.vault.service",
            "openajar.runner.service"]
API = "harbor-supplier"
SURFACE = "harbor/supplier"
OTHER = "harbor/second"
PORTAL = "https://portal.northsupply.example"
OWNER = Principal(kind="human", id="owner")
ROWS = [("A-1042", "1,204.00"), ("A-1043", "88.10")]
READ_PATH = f"/v1/sites/{API}/open_orders"
COMMIT_PATH = f"/v1/sites/{API}/send_note"


def portal(rows: list[tuple[str, str]] | None = None) -> FakeSite:
    site = FakeSite()
    site.state.update(rows=list(ROWS if rows is None else rows), reads=0, notes=[])

    def orders(s: FakeSite, d: FakeDriver) -> El:
        s.state["reads"] += 1
        body = [El("row", children=[El("cell", text=n), El("cell", text="x"), El("cell", text="y"), El("cell", text=t),
                                    El("link", "open", attrs={"href": f"{PORTAL}/orders/{n}"})])
                for n, t in s.state["rows"]]
        return El("main", children=[El("table", "Orders", children=body)])

    def compose(s: FakeSite, d: FakeDriver) -> El:
        def send(st: FakeSite, el: El, dr: FakeDriver) -> None:
            st.state["notes"].append(dr.values.get("note"))

        return El("document", children=[El("main", children=[El("textbox", "Note", key="note"),
                                                             El("button", "Send", key="send", on_click=send)])])

    def sent(s: FakeSite, d: FakeDriver) -> El:
        rows = [El("row", children=[El("cell", text=n)]) for n in s.state["notes"]]
        return El("document", children=[El("main", children=rows)])

    for status in ("open", "late"):
        site.route(f"{PORTAL}/orders?status={status}", orders)
    site.route(f"{PORTAL}/compose", compose)
    site.route(f"{PORTAL}/sent", sent)
    return site


def definition(**endpoints: Any) -> dict[str, Any]:
    """The contract's sample, on two surfaces, with a commit and the extras a test asks for."""
    doc = copy.deepcopy(SAMPLE)
    doc["surfaces"] = [SURFACE, OTHER]
    doc["endpoints"]["open_orders"]["pace"] = {"per_hour": 1000}
    main = {"role": "main"}
    doc["endpoints"]["send_note"] = {
        "title": "Send a note", "effect": "commit", "pace": {"per_day": 1000, "min_gap_minutes": 1},
        "input": {"type": "object", "required": ["note"], "properties": {"note": {"type": "string", "show": True}}},
        "commit": {"step": "send", "preflight": [
            {"kind": "value_equals", "locator": {"role": "textbox", "within": main}, "param": "note"}]},
        "outputs": {"sent": "sent"},
        "steps": [{"type": "navigate", "url": f"{PORTAL}/compose"},
                  {"type": "fill", "locator": {"role": "textbox", "within": main}, "param": "note", "publishes": False},
                  {"type": "click", "label": "send", "writes": True, "locator": {"role": "button", "within": main}},
                  {"type": "readback", "url": f"{PORTAL}/sent", "locator": {"role": "row", "within": main, "nth": 0},
                   "condition": {"kind": "exists"}},
                  {"type": "extract", "save_as": "sent", "max_items": 10, "items": {"role": "row", "within": main},
                   "fields": {"note": {"locator": {"role": "cell"}, "read": "text", "max_chars": 200}}}],
    }
    doc["endpoints"].update(copy.deepcopy(endpoints))
    return doc


def search_endpoint(**input_props: Any) -> dict[str, Any]:
    """A read with the input the test names, on the same page as open_orders."""
    ep = copy.deepcopy(SAMPLE["endpoints"]["open_orders"])
    ep["steps"][0]["url"] = f"{PORTAL}/orders?status=open"
    ep["input"] = {"type": "object", "properties": input_props}
    return {**ep, "title": "Search", "pace": {"per_hour": 1000}}


async def poll(client: httpx.AsyncClient, path: str, *, tries: int = 200) -> httpx.Response:
    """Until the run stops answering 202."""
    for _ in range(tries):
        r = await client.get(path)
        if r.status_code != 202:
            return r
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} still pending: {r.text}")


class Node:
    def __init__(self, tmp_path: Path, *, site: FakeSite | None = None, doc: dict[str, Any] | None = None,
                 lanes: tuple[str, ...] = (SURFACE, OTHER), install: bool = True, flow_driver: Any = None):
        self.tmp = tmp_path
        home = private_home(tmp_path)
        make_vault(home)
        self.config = NodeConfig(home=home, mcp_token=TOKEN, mcp_port=7400)
        self.lanes = ControlFakeLaneManager(self.config)
        self.site = site or portal()
        self.driver = FakeDriver(self.site)
        self.runtime = node_runtime(self.config, self.lanes, FakeInteraction(),
                                    flow_driver=flow_driver or driver_factory(self.driver))
        build_services(self.runtime, SERVICES)
        self.store = PanelStore.for_home(home)
        self.policy = self.runtime.policy
        self.runner = self.runtime.get("runner")
        self.flows = self.runtime.get("flows")
        self.fvs = install_for_test(self.flows, doc or definition()) if install else []
        self.api_app = build_api(self.runtime, ApiSettings(lane_pump_seconds=0, schedule_pump_seconds=0,
                                                           brain_pump_seconds=0, panel_dir=tmp_path / "none"))
        self.app = build_node_app(self.config, self.runtime, api_app=self.api_app, panel=True)
        self.site_http = self.runtime.get("site_http")
        self.keys = self.site_http.keys
        self.cache = self.site_http.cache
        self.surface_ids = lanes
        self.url = ""

    async def __aenter__(self) -> Self:
        for lane in self.surface_ids:
            await self.lanes.create(LaneSpec(id=lane))
            if self.fvs:
                self.runner.surfaces.put(web_surface(lane), by=OWNER)
        await self.runner.start()
        config = uvicorn.Config(self.app, host="127.0.0.1", port=0, lifespan="on", log_config=None, access_log=False)
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://localhost:{port}"
        return self

    async def __aexit__(self, *_: object) -> None:
        self.server.should_exit = True
        await self.server_task
        await self.runner.stop(timeout=5)
        self.policy.close()

    def client(self, key: str | None = None, **kw: Any) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return httpx.AsyncClient(base_url=self.url, headers=headers, timeout=70, **kw)

    def key(self, **kw: Any) -> str:
        kw.setdefault("label", "n8n")
        kw.setdefault("apis", [API])
        return self.keys.add(by=OWNER, **kw)[1]

    def key_row(self, **kw: Any) -> tuple[Any, str]:
        kw.setdefault("label", "n8n")
        kw.setdefault("apis", [API])
        return self.keys.add(by=OWNER, **kw)

    async def session(self, client: httpx.AsyncClient) -> dict[str, str]:
        self.store.add_code("one-time-code-for-tests")
        r = await client.post("/api/session", json={"code": "one-time-code-for-tests"})
        assert r.status_code == 200, r.text
        return {"X-Agentos-CSRF": r.json()["csrf"]}

    def grant(self, **kw: Any) -> str:
        kw.setdefault("subject", {"id": "worker"})
        kw.setdefault("workspaces", ["harbor"])
        kw.setdefault("ceiling", Effect.COMMIT)
        return self.policy.issue(**kw)[1]
