"""The `site_change` trigger on the fake portal: a Site API read on a schedule starts a routine with
what changed, and a site that breaks never looks like every row went."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from siteapi_http_support import API, PORTAL, SURFACE, definition, portal
from workflow_support import OWNER, make_node, read_flow

from openajar.core.errors import InvalidRequest, PolicyDenied
from openajar.core.models import ControlState, Effect, Signal
from openajar.freeze.service import FreezeService
from openajar.node.channel.reports import workflows_catalog
from openajar.policy.approval_binding import _TARGET_RE
from openajar.runner import readvalues
from openajar.runner.fakes import El
from openajar.runner.siteapis.cache import SiteCache, cache_key
from openajar.runner.siteapis.http_store import HttpStore
from openajar.runner.siteapis.models import local_saved_by
from openajar.runner.workflows import engine, sitechange
from openajar.runner.workflows.mail import MAIL_OPERATION
from openajar.runner.workflows.service import WorkflowInvalid
from openajar.runner.workflows.sitechange import a_drop, diff, fitted, held, rows_of

OWNER_P = {"kind": "human", "id": "owner", "owner": "owner"}
READ = f"connector.{API}.open_orders"
WATCH = {"kind": "site_change", "operation": f"{READ}@{SURFACE}", "params": {}, "list": "orders", "key": "number",
         "every_minutes": 15, "label": "when an order appears"}
NOTE = {"id": "tell", "kind": "notify", "label": "tell you", "message": "New: {{each.number}} for {{each.total}}"}
LOOP = {"id": "each", "kind": "loop", "label": "for each new order", "over": "{{trigger.added}}", "body": [NOTE]}
ROWS = [("A-1", "10"), ("A-2", "20"), ("A-3", "30"), ("A-4", "40")]


def doc(**endpoint):
    """The portal's API with the changes a test asks for on `open_orders`. No `expect` unless it says so."""
    d = definition()
    d["surfaces"] = [SURFACE]
    ep = d["endpoints"]["open_orders"]
    ep.pop("expect")
    ep.update(endpoint)
    return d


def activate(node, definition_, bump=False):
    apis = node.runner.site_apis
    apis.save(definition_, by=OWNER_P, bump=bump)
    apis.activate(API, by=OWNER_P)


def make(tmp_path, *, api=None, rows=None):
    node = make_node(tmp_path, site=portal(list(ROWS if rows is None else rows)))
    activate(node, api or doc())
    return node


@pytest.fixture
def node(tmp_path):
    return make(tmp_path)


def routine(*steps, trigger=None, name="Watch orders"):
    return {"name": name, "workspace": "harbor", "trigger": {**WATCH, **(trigger or {})}, "steps": list(steps or [LOOP])}


def on(node, *steps, trigger=None, actor=OWNER):
    wid = node.wf.save(routine(*steps, trigger=trigger), actor)["id"]
    if actor.person:
        node.wf.enable(wid, OWNER)
    return wid


async def tick(node, minutes=15):
    node.h.clock.set(node.h.clock.now() + timedelta(minutes=minutes))
    await node.settle()


def reads(node):
    return node.site.state["reads"]


def runs(node, wid):
    return node.wf.runs(wid)


def told(view):
    return [i for i in view["steps"] if i["step"] == "tell"]


def problems_of(node, *steps, trigger=None):
    with pytest.raises(WorkflowInvalid) as e:
        node.wf.save(routine(*steps, trigger=trigger), OWNER)
    return {p["code"] for p in e.value.problems}


def watch(node, wid):
    return node.wf.public(wid)["watch"]


def rows_held(node, wid):
    return node.wf.store.watch_rows(wid)


# ---- the whole way through ----

async def test_a_baseline_then_a_new_row_a_failure_a_recovery_and_a_re_sort(node):
    wid = on(node)
    await node.settle()
    assert reads(node) == 1 and runs(node, wid) == []  # the first read is the baseline
    assert watch(node, wid)["state"] == "watching" and watch(node, wid)["rows"] == 4

    node.site.state["rows"].append(("A-5", "50"))
    await tick(node)
    [view] = runs(node, wid)
    assert view["cause"] == "The site's answer changed" and view["by"] == "trigger"
    trigger = view["steps"][0]
    assert [r["number"] for r in trigger["output"]["added"]] == ["A-5"]
    assert trigger["output"]["count_added"] == 1 and trigger["output"]["truncated"] is False
    assert trigger["untrusted"] == ["added", "removed", "changed"]
    assert node.step(view, "tell")["input"]["message"] == "New: A-5 for 50"

    await tick(node)
    assert len(runs(node, wid)) == 1  # nothing else changed

    node.site.signal = Signal.CAPTCHA  # the site breaks: this holds the lane, as for any read
    await tick(node)
    node.site.signal = Signal.OK
    assert len(runs(node, wid)) == 1 and watch(node, wid)["state"] == "failing"
    assert rows_held(node, wid).keys() == {"A-1", "A-2", "A-3", "A-4", "A-5"}
    assert node.wf.store.watch(wid)["failure"]["code"].startswith("signal_")

    await tick(node)  # the same rows on recovery: there is nothing stale to fire
    assert len(runs(node, wid)) == 1 and watch(node, wid)["state"] == "watching"

    node.site.state["rows"].reverse()
    await tick(node)
    assert len(runs(node, wid)) == 1  # a re-sorted table starts nothing


async def test_a_row_that_arrived_during_a_failure_starts_once_on_recovery(node):
    wid = on(node)
    await node.settle()
    node.site.signal = Signal.CAPTCHA
    await tick(node)
    node.site.signal = Signal.OK
    node.site.state["rows"].append(("A-5", "50"))
    await tick(node)
    await tick(node)
    [view] = runs(node, wid)
    assert [r["number"] for r in view["steps"][0]["output"]["added"]] == ["A-5"]


# ---- a check is an ordinary read ----

async def test_a_check_is_a_run_of_the_endpoint_under_the_grant_that_saved_it(node):
    wid = on(node)
    await node.settle()
    s = node.runner.scheduler.store.get_schedule(node.wf.store.state(wid).schedule_id)
    run = node.runner.get(s.state["last_run_id"])
    assert s.kind == "every" and s.flow_version is None and s.surface == SURFACE
    assert run.flow_id == READ and run.surface == SURFACE and run.schedule_id == s.id and run.cost.get("ms") is not None
    assert watch(node, wid)["last_run"] == run.id


class Lanes:
    """Just the lane manager's control state: `held` is how the runner sees a person holding a lane."""

    def __init__(self) -> None:
        self.held = False

    def get(self, lane):
        return SimpleNamespace(state="ready", control=self.control(lane))

    def control(self, lane):
        return SimpleNamespace(state=ControlState.PAUSED if self.held else ControlState.AGENT, reason="needs_human:captcha",
                               hold_until=None)


async def test_a_held_lane_starts_nothing_says_so_and_its_one_check_waits(node):
    lanes = Lanes()
    node.h.runtime.register("lanes", lanes)
    wid = on(node)
    await node.settle()
    lanes.held = True
    node.site.state["rows"].append(("A-5", "50"))
    first = reads(node)
    await tick(node)
    before = len(node.runner.store.list_runs(flow_id=READ, limit=100))
    await tick(node)
    await tick(node)
    assert len(node.runner.store.list_runs(flow_id=READ, limit=100)) == before  # a check that overlaps is skipped
    assert reads(node) == first and runs(node, wid) == []
    assert watch(node, wid)["state"] == "waiting" and "held" in watch(node, wid)["words"]
    s = node.runner.scheduler.store.get_schedule(node.wf.store.state(wid).schedule_id)
    assert s.state["skipped_total"] >= 1 and s.state["last_skip"]["reason"] == "previous run still active"
    lanes.held = False
    await tick(node)
    assert reads(node) == first + 1 and len(runs(node, wid)) == 1


async def test_a_freeze_of_the_lane_reads_nothing_and_says_so(node, tmp_path):
    freeze = FreezeService(node.h.runtime, home=tmp_path / "freeze")
    node.h.runtime.register("freeze", freeze)
    wid = on(node)
    await node.settle()
    await freeze.freeze("lane", SURFACE, reason="a person said stop", by="owner")
    first = reads(node)
    node.site.state["rows"].append(("A-5", "50"))
    await tick(node)
    assert reads(node) == first and runs(node, wid) == []
    assert watch(node, wid)["state"] == "stopped" and "freeze" in watch(node, wid)["words"]
    schedule = node.wf.store.state(wid).schedule_id
    node.runner.scheduler.set_enabled(schedule, True)  # even with its schedule back, a freeze stops the read
    await tick(node)
    assert reads(node) == first and "freeze" in watch(node, wid)["words"]
    await freeze.unfreeze("lane", SURFACE, by="owner")
    node.wf.enable(wid, OWNER)
    await tick(node)
    assert runs(node, wid) == [] and "resumes" in watch(node, wid)["words"]  # a freeze's pause is the owner's to lift
    node.events.resume_route(node.wf.store.state(wid).route_id, principal=OWNER_P)
    await tick(node)
    assert len(runs(node, wid)) == 1 and watch(node, wid)["state"] == "watching"


async def test_a_freeze_of_the_lane_behind_a_surface_of_another_name_stops_the_read(tmp_path):
    node = make_node(tmp_path, site=portal(list(ROWS)))
    node.runner.surfaces.put({"id": SURFACE, "site": "web", "lane": "harbor/portal", "acts_as": {"kind": "lane"}}, by=OWNER_P)
    activate(node, doc())
    freeze = FreezeService(node.h.runtime, home=tmp_path / "freeze")
    node.h.runtime.register("freeze", freeze)
    wid = on(node)
    await node.settle()
    await freeze.freeze("lane", "harbor/portal", reason="a person said stop", by="owner")
    schedule = node.wf.store.state(wid).schedule_id
    node.runner.scheduler.set_enabled(schedule, True)
    first = reads(node)
    await tick(node)
    assert reads(node) == first
    assert node.runner.scheduler.store.get_schedule(schedule).state["last_error"]["code"] == "frozen"


async def test_a_read_goes_by_ask_me_first_as_any_read_does(node):
    wid = on(node)
    node.svc.ask_first.set("flow", READ, True, by="owner", person=True)
    await node.settle()
    s = node.runner.scheduler.store.get_schedule(node.wf.store.state(wid).schedule_id)
    run = node.runner.get(s.state["last_run_id"])
    plain = node.runner.submit(flow_id=READ, surface=SURFACE, params={})[0]
    await node.settle()
    assert run.status == node.runner.get(plain.id).status  # the table says a read runs, and it does


async def test_a_watch_is_never_served_from_the_cache_and_fills_it(tmp_path):
    node = make(tmp_path, api=doc(cache_s=900))
    cache = SiteCache(HttpStore(tmp_path / "http"), node.h.flows, clock=node.h.clock)
    node.runner.on_finished.append(cache.record)
    wid = on(node)
    await node.settle()
    run = node.runner.get(watch(node, wid)["last_run"])
    fv = node.h.flows.get(READ)
    key = cache_key(fv.id, fv.version, SURFACE, run.lane, run.params)
    assert [r["number"] for r in cache.fresh(key, 900).value["data"]["orders"]][:1] == ["A-1"]
    node.site.state["rows"].append(("A-5", "50"))
    await tick(node, minutes=15)  # the cached answer is still inside cache_s, and the read goes to the site anyway
    assert reads(node) == 2 and len(runs(node, wid)) == 1
    assert len(cache.fresh(key, 900).value["data"]["orders"]) == 5


async def test_a_node_back_after_a_day_off_checks_once(node):
    wid = on(node)
    await node.settle()
    node.site.state["rows"].append(("A-5", "50"))
    first = reads(node)
    await tick(node, minutes=1440)
    assert reads(node) == first + 1 and len(runs(node, wid)) == 1


async def test_a_pace_refusal_starts_nothing_and_says_so(tmp_path):
    node = make(tmp_path, api=doc(pace={"per_hour": 6}))
    wid = on(node, trigger={"every_minutes": 20})
    await node.settle()
    runner = node.runner
    for _ in range(6):
        runner.store.insert_run(runner.get(watch(node, wid)["last_run"]).model_copy(update={
            "id": f"run_{_:012x}", "idempotency_key": None, "created_at": node.h.clock.now(), "started_at": node.h.clock.now()}))
    node.site.state["rows"].append(("A-5", "50"))
    first = reads(node)
    await tick(node, minutes=20)
    assert reads(node) == first and runs(node, wid) == []
    assert watch(node, wid)["state"] == "waiting" and "pace" in watch(node, wid)["words"]
    assert rows_held(node, wid).keys() == {r[0] for r in ROWS}


async def test_a_schema_violation_starts_nothing(tmp_path):
    node = make(tmp_path)
    wid = on(node)
    await node.settle()

    def bad(s, d):
        cells = [El("cell", text="A-9"), El("cell", text="x"), El("cell", text="y"), El("cell", text="9"),
                 El("link", "open", attrs={"href": "https://elsewhere.example/9"})]
        return El("main", children=[El("table", "Orders", children=[El("row", children=cells)])])

    node.site.route(f"{PORTAL}/orders?status=open", bad)
    await tick(node)
    assert runs(node, wid) == [] and watch(node, wid)["state"] == "failing"
    assert rows_held(node, wid).keys() == {r[0] for r in ROWS}


async def test_a_broken_endpoint_starts_nothing_and_its_schedule_waits(node):
    wid = on(node)
    await node.settle()
    node.site.routes.clear()  # the page is gone: the table's locator no longer resolves
    await tick(node)
    await tick(node)
    assert runs(node, wid) == []
    assert rows_held(node, wid).keys() == {r[0] for r in ROWS}
    assert node.runner.health.broken("harbor-supplier.open_orders", SURFACE) is not None
    assert watch(node, wid)["state"] == "waiting" and "broken" in watch(node, wid)["words"]


async def test_a_broken_endpoint_reads_nothing_until_a_repaired_version_and_that_read_is_compared(node):
    wid = on(node)
    await node.settle()
    routes = dict(node.site.routes)
    node.site.routes.clear()
    await tick(node)
    node.site.routes.update(routes)  # the site is back, but nothing looks until a person repairs the endpoint
    node.site.state["rows"].append(("A-5", "50"))
    first = reads(node)
    await tick(node)
    assert reads(node) == first and runs(node, wid) == []
    api = doc()
    api["version"] = "1.1.0"
    api["endpoints"]["open_orders"]["steps"][1]["max_items"] = 90
    activate(node, api)
    await tick(node)
    [view] = runs(node, wid)
    assert [r["number"] for r in view["steps"][0]["output"]["added"]] == ["A-5"]
    assert watch(node, wid)["state"] == "watching"


async def test_an_empty_result_under_expect_starts_nothing_and_is_not_a_removal(tmp_path):
    node = make(tmp_path, api=doc(expect={"min_items": {"orders": 1}}))
    wid = on(node)
    await node.settle()
    node.site.state["rows"] = []
    await tick(node)
    await tick(node)
    await tick(node)
    assert runs(node, wid) == [] and watch(node, wid)["state"] == "failing"
    assert "fewer rows" in watch(node, wid)["words"]
    assert rows_held(node, wid).keys() == {r[0] for r in ROWS}


# ---- a drop needs a second read ----

async def test_zero_rows_with_no_expect_needs_two_reads_in_a_row(node):
    wid = on(node)
    await node.settle()
    node.site.state["rows"] = []
    await tick(node)
    assert runs(node, wid) == [] and watch(node, wid)["state"] == "confirming"
    assert len(rows_held(node, wid)) == 4
    await tick(node)
    [view] = runs(node, wid)
    assert view["steps"][0]["output"]["count_removed"] == 4 and rows_held(node, wid) == {}


async def test_far_fewer_rows_come_back_and_nothing_starts(node):
    wid = on(node)
    await node.settle()
    rows = list(node.site.state["rows"])
    node.site.state["rows"] = rows[:1]
    await tick(node)
    node.site.state["rows"] = rows
    await tick(node)
    assert runs(node, wid) == [] and watch(node, wid)["state"] == "watching"


async def test_a_failed_read_between_two_drops_means_they_were_not_in_a_row(node):
    wid = on(node)
    await node.settle()
    node.site.state["rows"] = []
    await tick(node)
    node.site.signal = Signal.CAPTCHA
    await tick(node)
    node.site.signal = Signal.OK
    await tick(node)
    assert runs(node, wid) == [] and watch(node, wid)["state"] == "confirming"


async def test_only_a_row_both_reads_said_went_is_removed(node):
    wid = on(node)
    await node.settle()
    node.site.state["rows"] = [ROWS[3]]  # A-1, A-2 and A-3 went
    await tick(node)
    assert runs(node, wid) == [] and node.wf.store.watch(wid)["pending"] == ["A-1", "A-2", "A-3"]
    node.site.state["rows"] = [ROWS[0]]  # A-1 is back and A-4 went: only A-2 and A-3 went twice
    await tick(node)
    [view] = runs(node, wid)
    assert [r["number"] for r in view["steps"][0]["output"]["removed"]] == ["A-2", "A-3"]
    assert view["steps"][0]["output"]["added"] == [] and view["steps"][0]["output"]["count_removed"] == 2
    assert rows_held(node, wid).keys() == {"A-1", "A-4"}  # one read is not enough for A-4
    assert node.wf.store.watch(wid)["pending"] is None
    await tick(node)  # A-4 is half of what is left, so it waits for a second read of its own
    assert len(runs(node, wid)) == 1 and node.wf.store.watch(wid)["pending"] == ["A-4"]
    await tick(node)
    assert [r["number"] for r in runs(node, wid)[0]["steps"][0]["output"]["removed"]] == ["A-4"]
    assert rows_held(node, wid).keys() == {"A-1"}


async def test_two_reads_that_lost_different_rows_remove_nothing(node):
    wid = on(node)
    await node.settle()
    node.site.state["rows"] = ROWS[2:]
    await tick(node)
    node.site.state["rows"] = ROWS[:2]
    await tick(node)
    assert runs(node, wid) == [] and rows_held(node, wid).keys() == {n for n, _ in ROWS}


async def test_what_a_drop_holds_stays_a_subset_of_the_rows_known_and_off_every_view(node):
    wid = on(node)
    await node.settle()
    node.site.state["rows"] = []
    await tick(node)
    assert set(node.wf.store.watch(wid)["pending"]) <= set(rows_held(node, wid))
    assert "A-1" not in json.dumps(node.wf.public(wid), default=str)
    assert "A-1" not in json.dumps(workflows_catalog(node.h.runtime), default=str)


async def test_one_row_of_many_going_starts_at_once(node):
    wid = on(node)
    await node.settle()
    node.site.state["rows"] = node.site.state["rows"][1:]
    await tick(node)
    [view] = runs(node, wid)
    assert [r["number"] for r in view["steps"][0]["output"]["removed"]] == ["A-1"]


async def test_a_read_cut_at_max_items_says_nothing_of_the_rows_past_the_cut(tmp_path):
    api = doc()
    api["endpoints"]["open_orders"]["steps"][1]["max_items"] = 3
    node = make(tmp_path, api=api, rows=ROWS[:3])
    wid = on(node)
    await node.settle()
    node.site.state["rows"] = [("A-0", "5"), *ROWS[:3]]  # A-3 falls past the cut: it did not go
    await tick(node)
    [view] = runs(node, wid)
    assert [r["number"] for r in view["steps"][0]["output"]["added"]] == ["A-0"]
    assert view["steps"][0]["output"]["removed"] == []
    assert set(rows_held(node, wid)) == {"A-0", "A-1", "A-2", "A-3"}


async def test_an_over_long_value_is_not_a_cut_so_a_row_that_went_is_gone(node):
    node.site.state["rows"][0] = ("A-1", "x" * 200)  # past the field's max_chars: clipped, and the list is whole
    wid = on(node)
    await node.settle()
    node.site.state["rows"].pop(1)
    await tick(node)
    [view] = runs(node, wid)
    assert node.runner.get(watch(node, wid)["last_run"]).output["truncated"] is True
    assert [r["number"] for r in view["steps"][0]["output"]["removed"]] == ["A-2"]
    assert "A-2" not in rows_held(node, wid)


async def test_a_list_trimmed_to_fit_the_output_cap_says_nothing_of_the_rows_it_lost(node, monkeypatch):
    wid = on(node)
    await node.settle()
    monkeypatch.setattr(readvalues, "OUTPUT_CAP", 340)  # room for three of the four rows
    node.site.state["rows"].append(("A-5", "50"))
    await tick(node)
    run = node.runner.get(watch(node, wid)["last_run"])
    assert len(run.output["data"]["orders"]) < 5
    view = runs(node, wid)[0] if runs(node, wid) else None
    assert view is None or view["steps"][0]["output"]["removed"] == []
    assert {"A-1", "A-2", "A-3", "A-4"} <= set(rows_held(node, wid))


async def test_another_list_cut_at_its_max_items_says_nothing_of_this_one(tmp_path):
    api = doc()
    ep = api["endpoints"]["open_orders"]
    ep["steps"].append({**copy.deepcopy(ep["steps"][1]), "save_as": "first", "max_items": 1})
    ep["outputs"]["first"] = "first"
    ep["output"]["properties"]["first"] = copy.deepcopy(ep["output"]["properties"]["orders"])
    node = make(tmp_path, api=api)
    wid = on(node)
    await node.settle()
    node.site.state["rows"].pop(1)
    await tick(node)
    [view] = runs(node, wid)
    assert node.runner.get(watch(node, wid)["last_run"]).output["truncated"] is True
    assert [r["number"] for r in view["steps"][0]["output"]["removed"]] == ["A-2"]


# ---- keys and what counts as changed ----

def test_a_row_with_no_key_is_left_out_and_counted():
    rows, skipped, _ = rows_of([{"n": "a"}, {"n": None}, {"n": "  "}, {}, "not a row", {"n": True}], "n", ["n"], set(), "k")
    assert set(rows) == {"a"} and skipped == 5


def test_a_duplicate_is_skipped_but_not_missing():
    rows, skipped, missing = rows_of([{"n": "a"}, {"n": "a"}, {"n": None}], "n", ["n"], set(), "k")
    assert set(rows) == {"a"} and skipped == 2 and missing == 1


async def test_rows_that_lost_their_key_are_not_removed_and_the_known_rows_stay(tmp_path):
    rows = [(f"A-{i}", str(i)) for i in range(10)]
    node = make(tmp_path, rows=rows)
    wid = on(node)
    await node.settle()
    node.site.state["rows"] = [("", total) if i < 4 else (n, total) for i, (n, total) in enumerate(rows)]  # 4 of 10: under the half
    await tick(node)
    assert runs(node, wid) == [] and watch(node, wid)["skipped"] == 4
    assert rows_held(node, wid).keys() == {n for n, _ in rows}
    node.site.state["rows"] = rows  # they have their keys again: nothing was ever new
    await tick(node)
    assert runs(node, wid) == []


async def test_a_row_with_no_key_holds_back_removals_and_a_new_row_still_starts(tmp_path):
    node = make(tmp_path)
    wid = on(node)
    await node.settle()
    node.site.state["rows"] = [("", "1"), *ROWS[1:], ("A-5", "50")]
    await tick(node)
    [view] = runs(node, wid)
    out = view["steps"][0]["output"]
    assert out["removed"] == [] and [r["number"] for r in out["added"]] == ["A-5"]
    assert "A-1" in rows_held(node, wid)
    node.site.state["rows"] = [*ROWS[1:], ("A-5", "50")]  # every row has its key: now A-1 is gone
    await tick(node)
    assert [r["number"] for r in runs(node, wid)[0]["steps"][0]["output"]["removed"]] == ["A-1"]


async def test_a_duplicate_does_not_hold_back_a_removal(node):
    wid = on(node)
    await node.settle()
    node.site.state["rows"] = [ROWS[0], ROWS[0], *ROWS[2:]]
    await tick(node)
    [view] = runs(node, wid)
    assert [r["number"] for r in view["steps"][0]["output"]["removed"]] == ["A-2"]


def test_two_rows_with_one_key_keep_the_same_row_whatever_the_order():
    one = [{"n": "a", "t": "2"}, {"n": "a", "t": "1"}]
    assert rows_of(one, "n", ["n", "t"], set(), "k") == rows_of(one[::-1], "n", ["n", "t"], set(), "k")
    assert rows_of(one, "n", ["n", "t"], set(), "k")[1] == 1


def test_a_number_key_and_a_duplicate_do_not_crash_the_diff():
    rows, *_ = rows_of([{"n": 7, "t": 1}, {"n": 7.5, "t": 2}], "n", ["n", "t"], set(), "k")
    assert diff({}, rows) == (["7", "7.5"], [], [])


async def test_a_read_where_no_row_has_its_key_is_a_failed_read(node):
    wid = on(node)
    await node.settle()

    def blank(s, d):
        cells = [El("cell", text=""), El("cell", text="x"), El("cell", text="y"), El("cell", text="9"),
                 El("link", "open", attrs={"href": f"{PORTAL}/orders/9"})]
        return El("main", children=[El("table", "Orders", children=[El("row", children=cells)])])

    node.site.route(f"{PORTAL}/orders?status=open", blank)
    await tick(node)
    await tick(node)
    assert runs(node, wid) == [] and "had its key" in watch(node, wid)["words"]


async def test_a_changed_row_says_which_field_changed_and_what_it_was(node):
    wid = on(node, {"id": "each", "kind": "loop", "over": "{{trigger.changed}}", "body": [
        {"id": "tell", "kind": "notify", "message": "{{each.number}}: {{each.was_total}} to {{each.total}} ({{each.what_changed}})"}]})
    await node.settle()
    node.site.state["rows"][1] = ("A-2", "25")
    await tick(node)
    [view] = runs(node, wid)
    [row] = view["steps"][0]["output"]["changed"]
    assert row["what_changed"] == ["total"] and row["was_total"] == "20" and row["total"] == "25"
    assert "was_url" not in row and row["row_key"] == "A-2"
    assert node.step(view, "tell")["input"]["message"] == "A-2: 20 to 25 (['total'])"


def test_changed_reads_declared_fields_only_and_never_a_rows_place():
    fields = ["n", "t"]
    old, *_ = rows_of([{"n": "a", "t": "1"}, {"n": "b", "t": "2"}], "n", fields, set(), "k")
    new, *_ = rows_of([{"n": "b", "t": "2", "extra": "x"}, {"n": "a", "t": "1", "extra": "y"}], "n", fields, set(), "k")
    assert diff(old, new) == ([], [], [])


def test_half_the_rows_going_is_a_drop_and_fewer_are_not():
    known = {str(i): {} for i in range(4)}
    assert a_drop(known, ["1", "2"]) and a_drop({"a": {}}, ["a"])
    assert not a_drop(known, ["1"]) and not a_drop(known, []) and not a_drop({}, [])


# ---- at save ----

def test_a_commit_endpoint_is_refused(node):
    assert problems_of(node, trigger={"operation": f"connector.{API}.send_note@{SURFACE}"}) == {"not_a_read"}


def test_a_draft_endpoint_is_refused_with_a_problem_for_a_person(tmp_path):
    api = doc()
    ep = api["endpoints"]["look"] = copy.deepcopy(api["endpoints"]["open_orders"])
    ep.update(effect="draft", cache_s=0, title="Look")
    ep["steps"].insert(1, {"type": "fill", "locator": {"role": "textbox"}, "param": "status", "publishes": False})
    node = make(tmp_path, api=api)
    with pytest.raises(WorkflowInvalid) as e:
        node.wf.save(routine(trigger={"operation": f"connector.{API}.look@{SURFACE}"}), OWNER)
    [p] = e.value.problems
    assert p["target"] == "trigger" and p["code"] == "not_a_read" and "only a read" in p["words"]


def test_a_flow_that_is_not_an_endpoint_is_refused(node):
    node.h.save({"id": "harbor.social.read", "lane": "harbor/social", "effect": "read",
                 "steps": [{"type": "navigate", "url": f"{PORTAL}/"}]}, by=OWNER_P)
    assert problems_of(node, trigger={"operation": "harbor.social.read"}) == {"not_a_site_api"}


def test_an_operation_that_is_not_there_is_refused(node):
    assert problems_of(node, trigger={"operation": f"connector.{API}.nope@{SURFACE}"}) == {"unknown_operation"}


@pytest.mark.parametrize("change", [{"list": "nope"}, {"key": "nope"}, {"list": "count"}, {"params": {"status": "gone"}}])
def test_the_list_key_and_params_are_checked_against_the_endpoint(node, change):
    assert problems_of(node, trigger=change) == {"bad_trigger"}


@pytest.mark.parametrize("change", [{"key": "{{trigger.key}}"}, {"list": "${list}"}, {"params": {"status": "{{trigger.x}}"}},
                                    {"operation": "{{trigger.x}}"}])
def test_the_triggers_own_fields_are_literal(node, change):
    assert "must_be_literal" in problems_of(node, trigger=change)


@pytest.mark.parametrize("missing", ["operation", "list", "key", "every_minutes"])
def test_a_watch_may_be_saved_unfinished_and_not_turned_on(node, missing):
    saved = node.wf.save(routine({"id": "tell", "kind": "notify", "message": "It changed."},
                                 trigger={missing: None}), OWNER)
    assert saved["missing"]
    with pytest.raises(WorkflowInvalid):
        node.wf.enable(saved["id"], OWNER)


@pytest.mark.parametrize(("every", "good"), [(4, False), (5, True), (0, False), (20000, False)])
def test_every_minutes_has_a_floor_and_a_ceiling(node, every, good):
    trigger = {"every_minutes": every}
    if good:
        node.wf.save(routine(trigger=trigger), OWNER)
    else:
        assert problems_of(node, trigger=trigger) & {"too_often", "bad_trigger"}


def test_a_watch_is_not_faster_than_the_answer_is_kept(tmp_path):
    node = make(tmp_path, api=doc(cache_s=1800))
    assert problems_of(node, trigger={"every_minutes": 15}) == {"too_often"}
    node.wf.save(routine(trigger={"every_minutes": 30}), OWNER)


@pytest.mark.parametrize(("pace", "every", "good"), [
    ({"per_hour": 6}, 15, False), ({"per_hour": 6}, 20, True), ({"per_hour": 60}, 5, True),
    ({"per_day": 10}, 240, False), ({"per_day": 10}, 288, True), ({"min_gap_minutes": 30}, 15, False)])
def test_a_watch_alone_uses_at_most_half_of_the_pace(tmp_path, pace, every, good):
    node = make(tmp_path, api=doc(pace=pace))
    if good:
        node.wf.save(routine(trigger={"every_minutes": every}), OWNER)
    else:
        assert problems_of(node, trigger={"every_minutes": every}) == {"too_often"}


def test_a_private_field_cannot_be_the_key(tmp_path):
    api = doc()
    api["endpoints"]["open_orders"]["steps"][1]["fields"]["number"]["private"] = True
    node = make(tmp_path, api=api)
    assert problems_of(node) == {"bad_trigger"}


def test_a_watch_outside_the_workspace_is_refused(node):
    data = routine()
    data["workspace"] = "biz-a"
    with pytest.raises(WorkflowInvalid) as e:
        node.wf.save(data, OWNER)
    assert "outside_workspace" in {p["code"] for p in e.value.problems}


# ---- what a row may do ----

@pytest.mark.parametrize("step", [
    {"id": "b", "kind": "branch", "when": {"left": "{{trigger.added}}", "op": "not_empty"}},
    {"id": "e", "kind": "loop", "over": "{{trigger.added}}", "body": [
        {"id": "b", "kind": "branch", "when": {"left": "{{e.number}}", "op": "is", "right": "A-1"}}]},
])
def test_a_rows_text_never_decides_at_save(node, step):
    assert problems_of(node, step) == {"untrusted_decides"}


def test_only_the_triggers_own_rows_may_be_looped_over(node):
    read = {"id": "look", "kind": "action", "label": "look", "operation": f"{READ}@{SURFACE}"}
    each = {"id": "e", "kind": "loop", "over": "{{look.orders}}", "body": [{"id": "t", "kind": "notify", "message": "One."}]}
    assert problems_of(node, read, each) == {"untrusted_decides"}  # a read's own list is still the site's


def test_a_field_the_endpoint_does_not_give_is_refused_in_a_loop(node):
    bad = {"id": "e", "kind": "loop", "over": "{{trigger.added}}", "body": [
        {"id": "t", "kind": "notify", "message": "{{e.nope}}"}]}
    assert problems_of(node, bad) == {"bad_reference"}


def test_the_nodes_own_counts_may_decide(node):
    node.wf.save(routine({"id": "b", "kind": "branch", "when": {"left": "{{trigger.count_added}}", "op": "more_than", "right": "1"},
                          "then": [{"id": "t", "kind": "notify", "message": "Many."}]}), OWNER)


async def test_an_agents_values_named_like_rows_are_still_only_text_from_outside(node):
    wid = node.wf.save(routine(), OWNER)["id"]
    agent = node.agent(ceiling=Effect.COMMIT, workspaces=("harbor",))
    view = await node.run(wid, agent, values={"added": [{"number": "fake", "total": "0"}]})
    assert view["status"] == "failed" and "from outside" in view["result"]


async def test_a_person_may_run_it_by_hand_and_nothing_changed(node):
    wid = node.wf.save(routine(), OWNER)["id"]
    assert (await node.run(wid))["status"] == "succeeded_unverified"


def commit_each(**rest):
    return {"id": "each", "kind": "loop", "over": "{{trigger.added}}", "body": [
        {"id": "note", "kind": "action", "label": "send a note", "operation": f"connector.{API}.send_note@{SURFACE}",
         "inputs": {"note": "{{each.number}}"}, **rest}]}


async def test_an_action_filled_from_a_row_asks_first_and_says_why(node):
    wid = on(node, commit_each())
    await node.settle()
    node.site.state["rows"].append(("A-5", "50"))
    await tick(node)
    [view] = runs(node, wid)
    assert view["status"] == "waiting_approval" and node.site.state["notes"] == []
    step = node.step(view, "note")
    child = node.runner.get(step["run_id"])
    assert step["untrusted"] == ["note"] and child.trigger["origins"][0]["kind"] == "web_page"
    assert child.trigger["origins"][0]["host"] == "portal.northsupply.example"


async def test_a_commit_in_a_loop_over_rows_counts_against_the_daily_limit(node):
    wid = on(node, commit_each())
    assert node.wf.public(wid)["send_limits"][0]["counted"] is True
    await node.settle()
    node.wf.sends.set_limit(SURFACE, 1, person=True, by="owner")
    node.site.state["rows"] += [("A-5", "50"), ("A-6", "60")]
    await tick(node)
    [view] = runs(node, wid)
    assert node.wf.store.sent_on(SURFACE, node.wf.sends.day()) == 1
    assert view["status"] != "failed"


async def test_a_commit_fed_the_whole_list_is_counted_and_asks_first(node):
    step = {"id": "note", "kind": "action", "label": "send", "operation": f"connector.{API}.send_note@{SURFACE}",
            "inputs": {"note": "New: {{trigger.added}}"}}
    wid = on(node, step)
    assert node.wf.public(wid)["send_limits"][0]["counted"] is True
    await node.settle()
    node.site.state["rows"].append(("A-5", "50"))
    await tick(node)
    [view] = runs(node, wid)
    assert view["status"] == "waiting_approval" and node.step(view, "note")["untrusted"] == ["note"]


# ---- what a site's rows can make happen ----

NOTE_LITERAL = {"id": "note", "kind": "action", "label": "send a note", "operation": f"connector.{API}.send_note@{SURFACE}",
                "inputs": {"note": "Look at the portal"}}
HARBOR_MAIL, HARBOR_LIST = "harbor/mail", "list.harbor.customers"


def rows_loop(*body, over="{{trigger.added}}", id_="each"):
    return {"id": id_, "kind": "loop", "label": "for each new order", "over": over, "body": list(body)}


def new_rows(node, count=1):
    node.site.state["rows"] += [(f"N-{len(node.site.state['rows'])}-{i}", "1") for i in range(count)]


def mail_step(node):
    node.wf.mail.add({"id": HARBOR_MAIL, "address": "hello@harbor.example", "imap_host": "imap.invalid",
                      "smtp_host": "smtp.invalid", "username": "hello", "secret_ref": "harbor.mail"}, person=True)
    node.wf.sends.save_list(HARBOR_LIST, workspace="harbor", name="Customers",
                            people=[{"email": "ana@example.com", "name": "Ana"}, {"email": "bo@example.com", "name": "Bo"}],
                            person=True, by="owner")
    return {"id": "mail", "kind": "action", "label": "tell the list", "operation": f"{MAIL_OPERATION}@{HARBOR_MAIL}",
            "inputs": {"subject": "News", "body": "We have news"}, "send": {"list": HARBOR_LIST}}


async def test_a_commit_with_literal_inputs_in_a_loop_over_rows_asks_first(node):
    wid = on(node, rows_loop(NOTE_LITERAL))
    await node.settle()
    new_rows(node, 3)
    await tick(node)
    [view] = runs(node, wid)
    assert view["status"] == "waiting_approval" and node.site.state["notes"] == []
    step = node.step(view, "note")
    assert "site decides how many times" in step["why"]
    approval = node.svc.approvals.get(step["approval_id"])
    assert "a list of rows at portal.northsupply.example" in approval.reason


async def test_a_send_with_literal_inputs_in_a_loop_over_rows_asks_first_and_counts(node):
    wid = on(node, rows_loop(mail_step(node)))
    await node.settle()
    new_rows(node)
    await tick(node)
    [view] = runs(node, wid)
    assert view["status"] == "waiting_approval" and node.mail.to == []
    step = node.step(view, "mail")
    assert "site decides how many times" in step["why"]
    node.svc.approvals.decide(step["approval_id"], "approve", by="owner")
    await node.settle()
    assert sorted(node.mail.to) == ["ana@example.com", "bo@example.com"]
    assert node.wf.store.sent_on(HARBOR_MAIL, node.wf.sends.day()) == 2


@pytest.mark.parametrize("wrap", [
    lambda step: {"id": "if", "kind": "branch", "label": "if any", "when": {"left": "{{trigger.count_added}}", "op": "more_than", "right": "0"},
                  "then": [step]},
    lambda step: rows_loop(step, over="{{trigger.changed}}", id_="inner"),
], ids=["a branch", "a loop"])
async def test_a_commit_nested_deeper_in_a_loop_over_rows_asks_first(node, wrap):
    wid = on(node, rows_loop(wrap(NOTE_LITERAL)))
    await node.settle()
    new_rows(node)
    node.site.state["rows"][0] = ("A-1", "99")
    await tick(node)
    [view] = runs(node, wid)
    assert view["status"] == "waiting_approval" and node.site.state["notes"] == []


async def test_a_loop_over_a_list_of_the_owners_inside_a_loop_over_rows_asks_first(node, monkeypatch):
    node.h.save({**read_flow("harbor.lookup", [{"key": "codes", "type": "list", "untrusted": False}]), "lane": SURFACE}, by=OWNER_P)
    monkeypatch.setattr(engine.Walk, "_output", lambda self, child, op: ({"codes": ["a", "b"]}, set()))
    look = {"id": "look", "kind": "action", "label": "look it up", "operation": "harbor.lookup"}
    wid = on(node, look, rows_loop(rows_loop(NOTE_LITERAL, over="{{look.codes}}", id_="inner")))
    await node.settle()
    new_rows(node)
    await tick(node)
    [view] = runs(node, wid)
    assert view["status"] == "waiting_approval" and node.site.state["notes"] == []


async def test_a_read_in_a_loop_over_rows_runs_on_its_own(node):
    read = {"id": "read", "kind": "action", "label": "read the orders", "operation": f"{READ}@{SURFACE}"}
    wid = on(node, rows_loop(read))
    await node.settle()
    new_rows(node)
    await tick(node)
    [view] = runs(node, wid)
    assert view["status"].startswith("succeeded")
    step = node.step(view, "read")
    assert "why" not in step and "origins" not in node.runner.get(step["run_id"]).trigger


async def test_a_commit_after_the_loop_is_not_asked_about_for_the_rows(node):
    wid = on(node, rows_loop(NOTE), NOTE_LITERAL)
    await node.settle()
    new_rows(node, 2)
    await tick(node)
    [view] = runs(node, wid)
    assert view["status"].startswith("succeeded") and node.site.state["notes"] == ["Look at the portal"]
    assert "why" not in node.step(view, "note")


async def test_a_standing_approval_of_what_it_sends_covers_a_commit_in_a_loop_over_rows(node):
    wid = on(node, rows_loop(NOTE_LITERAL))
    await node.settle()
    new_rows(node)
    await tick(node)
    [view] = runs(node, wid)
    asked = node.svc.approvals.get(node.step(view, "note")["approval_id"])
    node.svc.approvals.add_standing(effects=["commit"], lanes=[SURFACE], targets=[f"tool:{asked.tool}"],
                                    contents=[asked.content_digest], by="owner", until=node.svc.approvals.clock() + 3600)
    node.svc.approvals.decide(asked.id, "approve", by="owner")
    await node.settle()
    new_rows(node)
    await tick(node)
    assert [v["status"].startswith("succeeded") for v in runs(node, wid)] == [True, True]
    assert node.site.state["notes"] == ["Look at the portal"] * 2


async def test_a_run_acts_for_at_most_so_many_rows_and_stops_there_saying_so(node, monkeypatch):
    monkeypatch.setattr(engine, "MAX_ROW_ACTS", 2)
    wid = on(node, rows_loop({**NOTE_LITERAL, "inputs": {"note": "{{each.number}}"}}))
    await node.settle()
    new_rows(node, 3)
    await tick(node)
    for _ in range(4):
        pending = node.step(runs(node, wid)[0], "note").get("approval_id")
        if pending:
            node.svc.approvals.decide(pending, "approve", by="owner")
        await tick(node, 2)
    [view] = runs(node, wid)
    assert view["status"] == "partial" and len(node.site.state["notes"]) == 2
    assert node.runner.get(view["id"]).error["code"] == "too_many_rows" and "2 times" in view["result"]


# ---- bounds ----

def test_an_event_carries_a_capped_number_of_rows_and_says_so():
    rows = [{"row_key": str(i)} for i in range(sitechange.MAX_EVENT_ROWS + 5)]
    kept, cut = fitted({"added": rows, "removed": [], "changed": []})
    assert len(kept["added"]) == sitechange.MAX_EVENT_ROWS and cut


def test_an_event_fits_what_an_event_holds_and_shares_it_between_the_lists():
    big = [{"row_key": str(i), "text": "x" * 300} for i in range(90)]
    kept, cut = fitted({"added": big, "removed": big, "changed": big})
    assert cut and len(json.dumps(kept)) < 20_000 and len(kept["added"]) == len(kept["removed"]) == len(kept["changed"])


async def test_a_big_change_starts_one_run_with_truncated_true_and_true_counts(node, monkeypatch):
    monkeypatch.setattr(sitechange, "MAX_EVENT_ROWS", 2)
    wid = on(node)
    await node.settle()
    node.site.state["rows"] += [(f"B-{i}", "1") for i in range(5)]
    await tick(node)
    [view] = runs(node, wid)
    out = view["steps"][0]["output"]
    assert len(out["added"]) == 2 and out["count_added"] == 5 and out["truncated"] is True
    await tick(node)
    assert len(runs(node, wid)) == 1  # what stayed out is not sent later


async def test_a_list_past_the_cap_is_a_failed_read_and_says_so(node, monkeypatch):
    wid = on(node)
    await node.settle()
    monkeypatch.setattr(sitechange, "MAX_ROWS", 3)
    await tick(node)
    assert runs(node, wid) == [] and "too big" in watch(node, wid)["words"]
    assert len(rows_held(node, wid)) == 4


async def test_a_cut_list_that_outgrows_the_baseline_is_a_failed_read_and_no_old_row_comes_back_as_new(tmp_path, monkeypatch):
    api = doc()
    api["endpoints"]["open_orders"]["steps"][1]["max_items"] = 3
    node = make(tmp_path, api=api, rows=ROWS[:3])
    wid = on(node)
    await node.settle()
    monkeypatch.setattr(sitechange, "MAX_ROWS", 4)
    node.site.state["rows"] = [("A-0", "5"), *ROWS[:3]]
    await tick(node)
    assert len(runs(node, wid)) == 1 and set(rows_held(node, wid)) == {"A-0", "A-1", "A-2", "A-3"}
    node.site.state["rows"] = [("A-00", "5"), ("A-0", "5"), *ROWS[:3]]  # with what it knows, that is five
    await tick(node)
    assert len(runs(node, wid)) == 1 and "too big" in watch(node, wid)["words"]
    assert set(rows_held(node, wid)) == {"A-0", "A-1", "A-2", "A-3"}
    node.site.state["rows"] = list(ROWS[:3])  # the old rows are at the top again, and they are not new
    await tick(node)
    assert runs(node, wid)[0]["steps"][0]["output"]["added"] == []


# ---- the baseline ----

def test_a_private_value_is_held_as_a_digest_and_a_long_one_as_a_clip_and_a_digest():
    assert "secret" not in json.dumps(held("secret", True, "k"))
    assert held("secret", True, "k") == held("secret", True, "k") != held("other", True, "k")
    assert held("secret", True, "k") != held("secret", True, "another key")
    long = held("x" * 1000, False, "k")
    assert len(long["clip"]) == sitechange.MAX_HELD_CHARS and long != held("x" * 999 + "y", False, "k")


async def test_a_private_field_is_seen_to_change_and_its_value_is_never_kept_or_shown(tmp_path):
    api = doc()
    api["endpoints"]["open_orders"]["steps"][1]["fields"]["total"]["private"] = True
    node = make(tmp_path, api=api)
    wid = on(node, {"id": "e", "kind": "loop", "over": "{{trigger.changed}}", "body": [
        {"id": "tell", "kind": "notify", "message": "{{e.number}}: {{e.what_changed}}"}]})
    await node.settle()
    held_rows = json.dumps(rows_held(node, wid))
    assert all(f'"{total}"' not in held_rows for _, total in ROWS)
    node.site.state["rows"][0] = ("A-1", "secret-99")
    node.site.state["rows"].append(("A-5", "secret-55"))
    await tick(node)
    [view] = runs(node, wid)
    out = view["steps"][0]["output"]
    assert out["changed"][0]["what_changed"] == ["total"]
    assert out["changed"][0]["total"] == "(hidden)" and out["changed"][0]["was_total"] == "(hidden)"
    assert out["added"][0]["total"] == "(hidden)"
    assert "secret-" not in json.dumps(view)
    event = node.events.store.list_events(limit=5)[0]
    assert "secret-" not in event.content.text


async def test_the_baseline_goes_with_the_workflow_and_stays_off_every_view(node):
    wid = on(node)
    await node.settle()
    node.site.state["rows"].append(("A-5", "50"))
    await tick(node)
    assert rows_held(node, wid) is not None
    public = json.dumps(node.wf.public(wid), default=str)
    assert '"rows": 5' in public and "A-1" not in public and "1,204" not in public
    assert "A-1" not in json.dumps(workflows_catalog(node.h.runtime), default=str)
    node.wf.delete(wid, OWNER)
    assert node.wf.store.watch(wid) is None and rows_held(node, wid) is None


async def test_turning_it_off_forgets_what_it_read_so_the_time_off_starts_nothing(node):
    wid = on(node)
    await node.settle()
    node.wf.disable(wid, OWNER)
    assert rows_held(node, wid) is None
    node.site.state["rows"].append(("A-5", "50"))
    node.wf.enable(wid, OWNER)
    await tick(node)
    await tick(node)
    assert runs(node, wid) == [] and len(rows_held(node, wid)) == 5


async def test_a_check_from_before_it_was_turned_off_is_not_the_baseline(node):
    wid = on(node)
    await node.settle()
    node.wf.disable(wid, OWNER)
    node.site.state["rows"].append(("A-5", "50"))
    node.wf.enable(wid, OWNER)
    await node.settle()  # back on inside the interval: nothing is due yet, and the old check stays old
    assert rows_held(node, wid) is None and watch(node, wid)["state"] == "starting"
    await tick(node)
    await tick(node)
    assert runs(node, wid) == [] and len(rows_held(node, wid)) == 5


def test_the_watch_shown_is_the_version_that_runs(node):
    plain = {"name": "Plain", "workspace": "harbor", "trigger": {"kind": "manual"},
             "steps": [{"id": "t", "kind": "notify", "message": "It ran."}]}
    agent = node.agent(workspaces=("harbor",))
    wid = node.wf.save(plain, OWNER)["id"]
    node.wf.enable(wid, OWNER)
    node.wf.save({**routine(), "name": "Plain", "id": wid}, agent)  # not live until a person looks
    assert "watch" not in node.wf.public(wid)


async def test_a_new_key_starts_over_from_the_next_read(node):
    wid = on(node)
    await node.settle()
    node.wf.save(routine(trigger={"key": "total"}), OWNER)
    await tick(node)
    await tick(node)
    assert runs(node, wid) == [] and set(rows_held(node, wid)) == {r[1] for r in ROWS}


# ---- who turns it on ----

def test_only_a_person_turns_it_on(node):
    agent = node.agent(workspaces=("harbor",))
    wid = node.wf.save(routine(), agent)["id"]
    with pytest.raises(PolicyDenied, match="only a person"):
        node.wf.enable(wid, agent)
    assert node.wf.public(wid)["enabled"] is False
    assert node.wf.propose(wid, agent)["status"] == "pending"


def test_an_agents_watch_stays_inside_its_grant_at_save(node):
    outside = node.agent(workspaces=("biz-a",))
    with pytest.raises(PolicyDenied):
        node.wf.save(routine(), outside)


async def test_an_agents_watch_stops_when_its_grant_does(node):
    agent = node.agent(workspaces=("harbor",))
    wid = node.wf.save(routine(), agent)["id"]
    node.wf.enable(wid, OWNER)
    await node.settle()
    assert reads(node) == 1
    node.svc.revoke(agent.grant_id, by="owner")
    await tick(node)
    assert reads(node) == 1 and runs(node, wid) == []
    assert watch(node, wid)["state"] == "stopped"


async def test_an_agents_new_version_does_not_change_what_a_person_turned_on(node):
    agent = node.agent(workspaces=("harbor",))
    wid = node.wf.save(routine(), agent)["id"]
    node.wf.enable(wid, OWNER)
    await node.settle()
    node.wf.save(routine(trigger={"key": "total"}), agent)
    node.site.state["rows"].append(("A-5", "50"))
    await tick(node)
    [view] = runs(node, wid)
    assert [r["number"] for r in view["steps"][0]["output"]["added"]] == ["A-5"]


# ---- a paused or removed API, and a new version ----

async def test_a_paused_api_stops_the_watch_and_says_why(node):
    wid = on(node)
    await node.settle()
    node.runner.site_apis.pause(API, by=OWNER_P)
    first = reads(node)
    await tick(node)
    assert reads(node) == first and runs(node, wid) == []
    state = watch(node, wid)
    assert state["state"] == "stopped" and state["words"].startswith(f"Stopped: a person paused {API}")
    node.runner.site_apis.activate(API, by=OWNER_P)
    node.site.state["rows"].append(("A-5", "50"))
    await tick(node)
    assert watch(node, wid)["state"] == "watching" and len(runs(node, wid)) == 1


async def test_removing_an_api_a_watch_uses_is_refused_until_the_workflow_goes(node):
    wid = on(node)
    await node.settle()
    with pytest.raises(InvalidRequest, match=wid):
        node.runner.site_apis.remove(API, by=OWNER_P)
    node.wf.disable(wid, OWNER)
    with pytest.raises(InvalidRequest, match=wid):  # off is not gone: it can be turned on again
        node.runner.site_apis.remove(API, by=OWNER_P)
    node.wf.delete(wid, OWNER)
    assert node.runner.site_apis.remove(API, by=OWNER_P)["removed"] == API


async def test_dropping_an_endpoint_a_watch_uses_is_refused(node):
    on(node)
    api = doc()
    del api["endpoints"]["open_orders"]
    api["version"] = "1.1.0"
    node.runner.site_apis.save(api, by=OWNER_P)
    with pytest.raises(InvalidRequest, match="use"):
        node.runner.site_apis.activate(API, by=OWNER_P)


async def test_a_flow_gone_from_under_a_watch_says_so(node):
    wid = on(node)
    await node.settle()
    node.h.flows.delete(READ)
    await tick(node)
    assert watch(node, wid)["state"] == "stopped" and "not on this computer" in watch(node, wid)["words"]


async def test_a_new_version_with_the_same_rows_keeps_the_baseline(node):
    wid = on(node)
    await node.settle()
    api = doc(title="Open orders, renamed")
    api["version"] = "1.1.0"
    activate(node, api)
    node.site.state["rows"].append(("A-5", "50"))
    await tick(node)
    [view] = runs(node, wid)
    assert [r["number"] for r in view["steps"][0]["output"]["added"]] == ["A-5"]


async def test_a_changed_output_schema_resets_the_baseline_and_fires_nothing(node):
    wid = on(node)
    await node.settle()
    api = doc()
    api["version"] = "1.1.0"
    api["endpoints"]["open_orders"]["steps"][1]["fields"]["note"] = {"locator": {"role": "cell", "nth": 1}, "read": "text",
                                                                     "max_chars": 40}
    activate(node, api)
    await tick(node)
    assert runs(node, wid) == []
    assert set(next(iter(rows_held(node, wid).values()))) == {"number", "total", "url", "note"}
    assert "started over" in watch(node, wid)["words"]
    node.site.state["rows"].append(("A-5", "50"))
    await tick(node)
    assert len(runs(node, wid)) == 1


async def test_a_check_of_a_version_that_is_not_active_any_more_says_nothing(node):
    wid = on(node)
    await node.settle()
    node.h.clock.set(node.h.clock.now() + timedelta(minutes=15))
    await node.runner.scheduler.tick()
    await node.runner.run_until_idle()  # the check read 1.0.0, and nothing has looked at it yet
    api = doc()
    api["version"] = "1.1.0"
    api["endpoints"]["open_orders"]["steps"][1]["fields"]["note"] = {"locator": {"role": "cell", "nth": 1}, "read": "text",
                                                                     "max_chars": 40}
    activate(node, api)
    node.site.state["rows"].append(("A-5", "50"))
    run = node.runner.get(watch(node, wid)["last_run"])
    assert run.flow_version != node.h.flows.get(READ).version
    await node.settle()
    assert runs(node, wid) == [] and watch(node, wid)["state"] == "watching"
    assert set(next(iter(rows_held(node, wid).values()))) == {"number", "total", "url"}  # still the old baseline


async def test_a_new_version_that_drops_the_key_is_a_failed_read_not_a_fire(node):
    wid = on(node)
    await node.settle()
    api = doc()
    api["version"] = "1.1.0"
    del api["endpoints"]["open_orders"]["steps"][1]["fields"]["number"]
    del api["endpoints"]["open_orders"]["output"]["properties"]["orders"]["items"]["properties"]["number"]
    activate(node, api)
    await tick(node)
    assert runs(node, wid) == [] and watch(node, wid)["state"] == "failing"
    assert rows_held(node, wid).keys() == {r[0] for r in ROWS}


# ---- a watched endpoint stays a read ----

def clicking(version="1.1.0"):
    """A new version whose read also clicks on the site. It has to say so: a connector is never lower than its steps."""
    api = doc(effect="draft", cache_s=0)
    api["version"] = version
    api["endpoints"]["open_orders"]["steps"].insert(1, {"type": "click", "label": "open", "publishes": False,
                                                        "locator": {"role": "link", "name": "open", "nth": 0, "within": {"role": "main"}}})
    return api


def committing():
    """A new version of the same endpoint that is a commit."""
    api = definition()
    api["surfaces"], api["version"] = [SURFACE], "1.1.0"
    api["endpoints"]["open_orders"] = {**api["endpoints"]["send_note"], "outputs": {"orders": "sent"}}
    return api


def behind_the_check(node, wid, api):
    """A version that goes live though a watch is on: the state of the routine says off for just that long."""
    state = node.wf.store.state(wid)
    state.enabled = False
    node.wf.store.put_state(state)
    activate(node, api)
    state.enabled = True
    node.wf.store.put_state(state)


@pytest.mark.parametrize("api", [clicking(), committing()], ids=["a draft", "a commit"])
def test_activating_a_version_that_stops_a_watched_read_being_one_is_refused_and_names_the_routine(node, api):
    on(node)
    node.runner.site_apis.save(api, by=OWNER_P)
    with pytest.raises(InvalidRequest, match="Watch orders") as e:
        node.runner.site_apis.activate(API, by=OWNER_P)
    assert "open_orders" in e.value.message and "act on the site" in e.value.message
    assert node.h.flows.get(READ).effect == Effect.READ


def test_a_watch_that_is_off_does_not_hold_an_activation_and_cannot_be_turned_on_over_it(node):
    wid = on(node)
    node.wf.disable(wid, OWNER)
    activate(node, clicking())
    with pytest.raises(WorkflowInvalid) as e:
        node.wf.enable(wid, OWNER)
    assert {p["code"] for p in e.value.problems} == {"not_a_read"}


def test_a_new_version_that_keeps_the_read_a_read_goes_live(node):
    on(node)
    api = doc(title="Open orders, renamed")
    api["version"] = "1.1.0"
    activate(node, api)
    assert node.h.flows.get(READ).definition.title == "Open orders, renamed"


async def test_a_watched_endpoint_that_is_no_read_any_more_is_not_fired_and_the_routine_says_why(node):
    wid = on(node)
    await node.settle()
    behind_the_check(node, wid, clicking())
    assert node.h.flows.get(READ).effect == Effect.DRAFT
    first = reads(node)
    before = len(node.runner.store.list_runs(flow_id=READ, limit=100))
    await tick(node)
    await tick(node)
    assert reads(node) == first and len(node.runner.store.list_runs(flow_id=READ, limit=100)) == before
    state = watch(node, wid)
    assert state["state"] == "stopped" and "act on the site" in state["words"]
    assert runs(node, wid) == []
    api = doc()
    api["version"] = "1.2.0"
    activate(node, api)
    node.site.state["rows"].append(("A-5", "50"))
    await tick(node)
    assert watch(node, wid)["state"] == "watching" and len(runs(node, wid)) == 1


async def test_a_watched_endpoint_that_gained_pay_is_not_fired(node):
    wid = on(node)
    await node.settle()
    head = node.h.flows.get(READ)
    node.h.flows.save(head.definition.model_copy(update={"effect": Effect.PAY}), by=local_saved_by(API, OWNER_P))
    assert node.h.flows.get(READ).effect == Effect.PAY
    first = reads(node)
    await tick(node)
    assert reads(node) == first and watch(node, wid)["state"] == "stopped"


async def test_a_schedule_that_is_no_watch_fires_whatever_its_flow_does(node):
    wid = on(node)
    await node.settle()
    node.wf.delete(wid, OWNER)
    s, _ = node.runner.scheduler.add(flow=f"connector.{API}.send_note", on=SURFACE, params={"note": "hi"}, every={"every_s": 3600},
                                     principal=OWNER_P)
    await tick(node, 61)
    assert node.site.state["notes"] == ["hi"] and "last_error" not in node.runner.scheduler.store.get_schedule(s.id).state


# ---- a watched key stays shown ----

def private_field(field="number"):
    api = doc()
    api["version"] = "1.1.0"
    api["endpoints"]["open_orders"]["steps"][1]["fields"][field]["private"] = True
    return api


def test_activating_a_version_that_makes_a_watched_key_private_is_refused_and_names_the_routine(node):
    on(node)
    node.runner.site_apis.save(private_field(), by=OWNER_P)
    with pytest.raises(InvalidRequest, match="Watch orders") as e:
        node.runner.site_apis.activate(API, by=OWNER_P)
    assert "number" in e.value.message and "private" in e.value.message


def test_another_field_of_a_watched_list_may_go_private(node):
    on(node)
    activate(node, private_field("total"))


async def test_a_key_gone_private_is_a_failed_read_and_its_values_go_nowhere(node):
    wid = on(node)
    await node.settle()
    behind_the_check(node, wid, private_field())
    node.site.state["rows"].append(("A-5", "50"))
    await tick(node)
    await tick(node)
    assert runs(node, wid) == [] and "A-5" not in json.dumps(rows_held(node, wid))
    assert node.events.store.list_events(limit=5) == []
    assert watch(node, wid)["state"] == "failing" and "private" in watch(node, wid)["words"]


# ---- a restart ----

async def test_a_restart_between_the_event_and_the_baseline_starts_one_run(node, monkeypatch):
    wid = on(node)
    await node.settle()
    node.site.state["rows"].append(("A-5", "50"))
    real = node.wf.store.put_watch
    monkeypatch.setattr(node.wf.store, "put_watch", lambda *a, **k: (_ for _ in ()).throw(OSError("the node stopped")))
    await tick(node)
    monkeypatch.setattr(node.wf.store, "put_watch", real)
    await node.settle()
    await tick(node, minutes=1)
    assert len(runs(node, wid)) == 1 and "A-5" in rows_held(node, wid)


# ---- what a page can hold, and what leaves with an export ----

def test_a_row_with_a_lone_surrogate_does_not_stop_the_check():
    rows, *_ = rows_of([{"n": "a\ud800", "t": "b\ud800" * 400}], "n", ["n", "t"], {"t"}, "k")
    assert sitechange.fits(rows) and json.dumps(rows)
    assert fitted({"added": [{"row_key": "a\ud800"}], "removed": [], "changed": []})[0]["added"]


async def test_an_export_leaves_out_what_only_reads_for_a_workflow(node):
    from openajar.workspaces.transfer.export import _collect_schedules

    wid = on(node)
    notes = []
    src = SimpleNamespace(scheduler=node.runner.scheduler, flows=node.h.flows)
    assert _collect_schedules(src, "harbor", [], set(), notes) == []
    assert node.wf.store.state(wid).schedule_id in notes[0]


def test_a_site_api_with_a_hyphen_can_be_a_standing_target():
    assert _TARGET_RE.match("connector:harbor-supplier.open_orders@sha256:" + "a" * 64)
