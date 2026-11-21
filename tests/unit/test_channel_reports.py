"""What a node reports over the channel: `run` events, `node { inventory }`, `{ system }`,
`{ programs }`, `{ services }`, `{ doctor }`, and how often."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from channel_fake_controller import FAST, NODE_ID, FakeController, node_pubkey, node_runtime, start, stop

from openajar.core import logs
from openajar.node.channel import reports
from openajar.node.daemon import build_services
from openajar.ops.doctor import CheckResult, Report

FLOW = {"id": "biz-a.social.look", "lane": "biz-a/social",
        "params": {"type": "object", "properties": {"caption": {"type": "string", "default": "hi",
                                                                 "description": "anything at all"}}},
        "steps": [{"type": "navigate", "url": "https://example.com/"}]}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class SecretEntry:
    def public(self):
        return {"id": "vlt_1", "name": "biz-a-login", "kind": "password", "domain": "example.com",
                "lanes": ["biz-a/social"], "created_at": "2026-11-01T00:00:00+00:00", "last_used": None,
                "secret": "hunter2-hunter2", "username": "someone@example.com"}


class FakeVault:
    async def list_entries(self):
        return [SecretEntry()]

    def scrub(self, text, lane_id=None):
        return text.replace("hunter2-hunter2", "[redacted]")


@pytest.fixture
async def node(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake, lanes=("biz-a/social",))
        build_services(runtime, ["openajar.runner.service"])
        runtime.register("vault", FakeVault())
        runtime.get("channel").timings = replace(FAST, reports=0.1, report_min=0.5, doctor_after=600.0)
        runtime.flows.save(FLOW)
        await start(runtime)
        peer = await fake.peer()
        await asyncio.wait_for(runtime.channel.welcomed.wait(), 15)  # the fake sees the peer before the node is in
        yield runtime, peer
        await stop(runtime)


def node_report(kind):
    return lambda e: e.body.get("kind") == "node" and kind in e.body["payload"]


async def test_a_run_is_reported_on_every_status_change_without_its_params(node):
    runtime, peer = node
    run, _ = runtime.runner.submit(flow_id="biz-a.social.look", params={"caption": "secret plan"},
                                   principal={"kind": "agent", "id": "claude", "thread": "t1"}, grant_id="owner")
    event = await peer.recv("event", where=lambda e: e.body.get("kind") == "run" and e.body["payload"]["id"] == run.id)
    payload = event.body["payload"]
    assert payload["lane"] == "biz-a/social" and payload["flow"] == "biz-a.social.look"
    assert payload["status"] == "queued" and payload["effect"] == "read" and payload["principal"] == "claude"
    assert payload["started_at"] == payload["created_at"] and payload["finished_at"] is None
    assert payload["cost"] == {"tokens": 0, "usd": 0.0}
    assert "secret plan" not in str(payload) and "params" not in payload
    assert isinstance(event.seq, int)  # outboxed: a run event needs an ack like any other


def test_a_runs_proof_names_its_file_stream_and_never_a_path_or_query(tmp_path):
    shot = tmp_path / "runner" / "shots" / "run_1" / "final.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"png")
    described = {"id": "run_1", "lane": "biz-a/social", "status": "succeeded_verified", "summary": "done",
                 "principal": {"kind": "agent", "id": "claude"}, "created_at": "2026-11-04T10:00:00+00:00",
                 "started_at": "2026-11-04T10:00:01+00:00", "finished_at": "2026-11-04T10:00:09+00:00",
                 "cost": {"tokens": 12, "usd": 0.01, "ms": 5},
                 "proof": {"url": "https://example.com/p/1?session=abc#x", "screenshot": "runner/shots/run_1/final.png",
                           "readbacks": [{"passed": True}]},
                 "error": {"code": "x", "message": "token=sk-abcdefghijklmnopqrstuvwxyz0123456789"},
                 "params": {"caption": "secret"}}
    payload = reports.run_payload(described, effect="commit", home=tmp_path)
    assert payload["proof"] == {"url": "https://example.com/p/1?...", "id": None,
                                "screenshot": "runs/run_1/proof.png", "readbacks": 1}
    assert str(tmp_path) not in str(payload) and "sk-abcdef" not in str(payload) and "secret" not in str(payload)
    assert payload["started_at"] == "2026-11-04T10:00:01+00:00" and payload["cost"] == {"tokens": 12, "usd": 0.01}
    shot.unlink()
    assert reports.run_payload(described, effect=None, home=tmp_path)["proof"]["screenshot"] is None
    assert "effect" not in reports.run_payload(described, effect=None, home=tmp_path)


async def test_the_inventory_lists_flows_schedules_and_vault_names_only(node):
    runtime, peer = node
    first = await peer.recv("event", where=node_report("inventory"))
    inventory = first.body["payload"]["inventory"]
    flow = next(f for f in inventory["flows"] if f["id"] == "biz-a.social.look")
    assert flow["lane"] == "biz-a/social" and flow["effect"] == "read" and isinstance(flow["version"], str)
    assert flow["params"] == {"type": "object", "required": [], "properties": {"caption": {"type": "string"}}}
    assert inventory["vault"] == [{"id": "vlt_1", "name": "biz-a-login", "kind": "password", "domain": "example.com",
                                   "lanes": ["biz-a/social"], "created_at": "2026-11-01T00:00:00+00:00",
                                   "last_used": None}]
    assert "hunter2" not in str(first.body) and "someone@" not in str(first.body)
    assert inventory["schedules"] == []
    # a new schedule is a change: it goes out once the rate limit allows
    runtime.runner.scheduler.add(flow="biz-a.social.look", cron="0 9 * * *", principal={},
                                 params={"api_key": "sk-live-abcdefghijklmnop"})
    second = await peer.recv("event", where=node_report("inventory"), timeout=5)
    schedule = second.body["payload"]["inventory"]["schedules"][0]
    assert schedule["flow"] == "biz-a.social.look" and schedule["cron"] == "0 9 * * *"
    assert schedule["next_run"] and schedule["params"] == {"api_key": {"type": "string"}}  # names and types only
    assert "sk-live" not in str(second.body)


async def test_system_programs_and_services_go_out_after_welcome(node):
    _, peer = node
    system = (await peer.recv("event", where=node_report("system"))).body["payload"]["system"]
    assert system["protocol"] == 1 and system["cores"] and system["version"]
    programs = (await peer.recv("event", where=node_report("programs"))).body["payload"]["programs"]
    assert any(p["name"] == "openajar" and p["scope"] == "node" for p in programs)
    services = (await peer.recv("event", where=node_report("services"))).body["payload"]["services"]
    assert services[0]["name"] == "agentosd" and services[0]["state"] == "running"


async def test_the_doctor_report_runs_offline_and_masks_addresses(node, monkeypatch):
    runtime, peer = node
    seen = {}

    def fake_run(ctx, only=None):
        seen["network"] = ctx.network
        return Report(node="node-t", home=str(ctx.home), generated_at="2026-11-04T10:00:00+00:00", results=[
            CheckResult("disk", "Disk", "pass", "41 GB free"),
            CheckResult("proxy.res1", "Proxy res1", "fail", "exit IP 203.0.113.7 differs from 198.51.100.2",
                        "openajar proxy add res1 host:port:user"),
            CheckResult("vault", "Vault", "warn", "the key hunter2-hunter2 is readable"),
            CheckResult("backups", "Backups", "skip", "no backup folder"),
        ])

    monkeypatch.setattr("openajar.ops.doctor.run", fake_run)
    monkeypatch.setattr("openajar.core.logs._scrubbers", [FakeVault().scrub])
    channel = runtime.get("channel")
    channel._doctor = await asyncio.to_thread(reports.doctor, channel.config, logs.scrub)
    channel._report_wake.set()
    event = await peer.recv("event", where=node_report("doctor"))
    checks = event.body["payload"]["doctor"]
    assert seen["network"] is False
    assert [c["status"] for c in checks] == ["ok", "fail", "warn"]  # a skipped check is left out
    assert checks[1]["detail"] == "exit IP [address] differs from [address]"
    assert "hunter2" not in str(checks) and checks[0]["at"] == "2026-11-04T10:00:00+00:00"


def test_reports_are_rate_limited_and_the_latest_value_waits():
    clock = Clock()
    throttle = reports.Throttle(60, clock)
    assert throttle.offer("services", [1]) == [1]
    assert throttle.offer("services", [1]) is None  # unchanged
    assert throttle.offer("services", [2]) is None  # changed, but inside the window
    assert throttle.offer("services", [3]) is None
    assert throttle.due() == {}
    clock.now += 61
    assert throttle.due() == {"services": [3]}  # the latest one, once
    assert throttle.due() == {}
    # changed back to what was sent: nothing waits any more
    clock.now += 61
    assert throttle.offer("services", [4]) == [4]
    assert throttle.offer("services", [5]) is None
    assert throttle.offer("services", [4]) is None and throttle.due() == {}
    # a welcome resends every report once, even unchanged and sent moments ago (a new controller
    # session may have lost it), and replaces what waited
    assert throttle.offer("services", [6]) is None
    throttle.force()
    assert throttle.offer("services", [4]) == [4] and throttle.due() == {}
    assert throttle.offer("services", [4]) is None  # once
    clock.now += 61
    throttle.force()
    assert throttle.offer("services", [4]) == [4]
    assert throttle.offer("other", {"a": 1}) == {"a": 1}  # kinds are limited apart


async def test_each_welcome_resends_every_report_once(node):
    runtime, peer = node
    await peer.recv("event", where=node_report("services"))
    channel = runtime.get("channel")
    for _ in range(3):
        await channel.send_reports()  # no welcome: unchanged, nothing goes
    channel._throttle.force()
    await channel.send_reports()
    await channel.send_reports()
    await asyncio.sleep(0.2)
    services = [e for e in peer.seen if e.type == "event" and node_report("services")(e)]
    assert len(services) == 2


def test_addresses_are_masked_in_run_text_before_it_is_cut(tmp_path):
    described = {"id": "run_1", "status": "failed", "summary": "a" * 995 + " 203.0.113.7 then more",
                 "error": {"code": "proxy", "message": "exit 2a02:6b8::2:242 is not 198.51.100.2"}}
    payload = reports.run_payload(described, effect=None, home=tmp_path)
    assert "203.0" not in payload["summary"] and "203." not in payload["summary"][-20:]
    assert payload["error"]["message"] == "exit [address] is not [address]"


def test_doctor_text_is_masked_before_it_is_cut(monkeypatch, tmp_path):
    def fake_run(ctx, only=None):
        return Report(node="n", home=str(ctx.home), generated_at="2026-11-04T10:00:00+00:00", results=[
            CheckResult("proxy.res1", "Proxy res1", "fail", "x" * 293 + " 203.0.113.7")])

    monkeypatch.setattr("openajar.ops.doctor.run", fake_run)
    checks = reports.doctor(SimpleNamespace(home=tmp_path))
    assert "203" not in checks[0]["detail"]


def test_an_oversize_inventory_is_trimmed_with_counts_not_dropped():
    flow = {"id": "f", "title": "t" * 600}
    big = {"flows": [{**flow, "id": f"f{i}"} for i in range(reports.MAX_ITEMS)],
           "vault": [{"id": f"v{i}", "name": "n"} for i in range(10)]}
    assert reports.report_size(big) > reports.MAX_REPORT_BYTES
    fitted, cut = reports.fit("inventory", big)
    assert cut and reports.report_size(fitted) <= reports.MAX_REPORT_BYTES
    assert fitted["truncated"] is True and fitted["counts"] == {"flows": reports.MAX_ITEMS, "vault": 10}
    assert fitted["flows"][0]["id"] == "f0" and fitted["vault"] == big["vault"]
    assert reports.fit("inventory", big) == (fitted, True)  # the same cut each time
    assert reports.fit("services", [1, 2]) == ([1, 2], False)


async def test_an_oversize_report_goes_out_trimmed(node, monkeypatch):
    runtime, peer = node
    await peer.recv("event", where=node_report("inventory"))
    monkeypatch.setattr(reports, "MAX_REPORT_BYTES", 2000)
    for i in range(40):
        runtime.flows.save({**FLOW, "id": f"biz-a.social.look{i}"})
    channel = runtime.get("channel")
    channel._throttle.force()
    await channel.send_reports()
    event = await peer.recv("event", where=lambda e: node_report("inventory")(e)
                            and e.body["payload"]["inventory"].get("truncated"), timeout=5)
    inventory = event.body["payload"]["inventory"]
    assert inventory["counts"]["flows"] == 41 and len(inventory["flows"]) < 41


def test_programs_and_services_come_from_the_env_lanes():
    lane = SimpleNamespace(id="dev/api", kind=SimpleNamespace(value="service"))
    shell = SimpleNamespace(id="dev/box", kind=SimpleNamespace(value="terminal"))
    status = SimpleNamespace(state="backoff", health="unhealthy", restarts=3, started_at=None, last_exit=1)
    envs = SimpleNamespace(
        list=lambda: [SimpleNamespace(lane=lane), SimpleNamespace(lane=shell)],
        store=SimpleNamespace(manifest=lambda lane_id: {"apt": {"jq": "1.7"}, "pip": {"httpx": "0.28.1"}}),
        images=lambda: [SimpleNamespace(manifest={"apt": {"curl": "8.5"}}, content_hash="sha256:" + "a" * 64,
                                        built_at=None)],
        service_status=lambda lane_id: status)
    runtime = SimpleNamespace(get=lambda name: envs if name == "envs" else None)
    listed = reports.programs(runtime)
    jq = next(p for p in listed if p["name"] == "jq")
    assert jq == {"name": "jq", "version": "1.7", "source": "apt", "scope": "lane", "lanes": ["dev/api", "dev/box"]}
    curl = next(p for p in listed if p["name"] == "curl")
    assert curl["scope"] == "base_image" and curl["pinned_hash"] == "sha256:" + "a" * 64
    running = reports.services(runtime)
    assert [s["name"] for s in running] == ["agentosd", "api"]
    assert running[1] == {"name": "api", "lane": "dev/api", "state": "restarting", "health": "unhealthy",
                          "restarts": 3, "started_at": None, "last_exit": 1, "ports": []}
