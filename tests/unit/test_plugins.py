"""The plugin registry: typed manifests, checked requirements, capability on every job, state that
fails closed, and the line that only a person turns a plugin on."""

from __future__ import annotations

import ast
import json
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos.brains.models import BrainSpec
from agentos.cli import app
from agentos.core import catalog
from agentos.core.config import NodeConfig
from agentos.core.errors import InvalidRequest, NotFound, ServiceUnavailable
from agentos.core.models import Effect, LaneKind
from agentos.core.runtime import Runtime
from agentos.plugins import admin
from agentos.plugins import cli as plugins_cli
from agentos.plugins.entries import ANDROID_WORKER, BROWSER_WORKER, PLUGINS
from agentos.plugins.errors import (
    ChangeNotRecorded,
    OnlyAPerson,
    PluginDisabled,
    RequirementsUnmet,
    StateBroken,
)
from agentos.plugins.models import Change, Job, Plugin, Skill, Stage
from agentos.plugins.requirements import (
    GB,
    LANE_KIND_MODULES,
    DevicePresent,
    LaneKindSupported,
    MemoryAtLeast,
    NodeFacts,
    ServicePresent,
)
from agentos.plugins.service import PluginService, brain_skills
from agentos.plugins.store import FILE_NAME, SCHEMA_VERSION, PluginStore
from agentos.plugins.tools import tool_specs

SRC = Path(__file__).resolve().parents[2] / "src" / "agentos"

JEV = BrainSpec.template("jev", "metered", "typesafe", credential="api_key")
CHAT = BrainSpec.template("chat", "metered", "openai-compatible", base_url="https://api.example.com/v1",
                          model="m", price_in_per_mtok=1.0, price_out_per_mtok=1.0)
CLAUDE = BrainSpec.template("claude", "subscription", "anthropic")


class Brains:
    def __init__(self, *specs):
        self.specs = {spec.name: spec for spec in specs}

    def get_spec(self, name):
        if name not in self.specs:
            raise NotFound(f"no brain named {name}")
        return self.specs[name]


def facts_for(runtime, *, installed=(), devices=(), memory=32 * GB):
    return NodeFacts(runtime=runtime, installed=lambda name: name in installed,
                     exists=lambda path: path in devices, memory=lambda: memory)


def installed_all(name: str) -> bool:
    return True


@pytest.fixture
def runtime(tmp_path):
    return Runtime(config=NodeConfig(home=tmp_path), brains=Brains(JEV, CHAT, CLAUDE))


@pytest.fixture
def service(runtime, tmp_path):
    """A node where the pilot exists, so browser-worker can be turned on."""
    return PluginService(runtime, home=tmp_path, facts=NodeFacts(runtime=runtime, installed=installed_all))


@pytest.fixture(autouse=True)
def at_the_nodes_own_shell(monkeypatch):
    """Whatever ran these tests, they play a person at the node's shell unless they say otherwise."""
    monkeypatch.delenv(admin.LANE_ENV, raising=False)
    monkeypatch.delenv(admin.BRAIN_TASK_ENV, raising=False)


def assign_all(service):
    admin.assign(service, "browser-worker", "decide", "jev")
    admin.assign(service, "browser-worker", "write", "chat")
    admin.assign(service, "browser-worker", "rescue", "claude")


# ---- manifests ----

def test_every_plugin_ships_at_its_honest_stage():
    assert [(p.id, p.stage) for p in PLUGINS] == [
        ("browser-worker", Stage.WORKS), ("android-worker", Stage.DECLARED), ("desktop-worker", Stage.LATER),
        ("text-review", Stage.WORKS)]


def test_a_plugin_cli_must_be_first_party_too():
    with pytest.raises(ValueError, match="outside this repo"):
        Plugin(id="x", name="X", description="d", stage=Stage.WORKS, lane_kinds=(), cli="someone_else.cli")


def test_a_manifest_cannot_name_code_outside_the_repo():
    with pytest.raises(ValueError, match="outside this repo"):
        Plugin(id="x", name="X", description="d", stage=Stage.WORKS, lane_kinds=(), tools=("someone_else.tools",))


def test_a_job_must_say_what_its_model_must_do():
    with pytest.raises(ValueError, match="has to be able to do"):
        Job("decide", "d", ())


def test_every_lane_kind_says_what_provides_it():
    assert set(LANE_KIND_MODULES) == {kind.value for kind in LaneKind}


def test_the_package_keeps_the_feature_contract():
    """Only agentos.core and itself: a plugin names other features by string, never by import."""
    for path in (SRC / "plugins").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if name.startswith("agentos"):
                    assert name.startswith(("agentos.core", "agentos.plugins")), f"{path.name} imports {name}"


# ---- requirements ----

def test_a_service_passes_on_the_runtime_or_in_the_build_and_says_which(runtime):
    need = ServicePresent("pilot", "agentos.pilot.service")
    missing = need.check(facts_for(runtime))
    assert not missing.ok and "not part of this build yet" in missing.reason
    built = need.check(facts_for(runtime, installed=("agentos.pilot.service",)))
    assert built.ok and "part of this build" in built.reason
    runtime.register("pilot", object())
    live = need.check(facts_for(runtime))
    assert live.ok and "running" in live.reason


def test_a_loaded_plugin_needs_the_service_really_running(runtime):
    facts = NodeFacts(runtime=runtime, running=True, installed=installed_all)
    check = ServicePresent("pilot", "agentos.pilot.service").check(facts)
    assert not check.ok and "not running" in check.reason


def test_the_real_build_has_the_pilot_and_lanes_are_found_without_importing_them(runtime):
    facts = NodeFacts(runtime=runtime)
    assert ServicePresent("pilot", "agentos.pilot.service").check(facts).ok
    assert LaneKindSupported("browser").check(facts).ok
    android = LaneKindSupported("android").check(facts)
    assert not android.ok and "agentos.android" in android.reason


def test_a_lane_kind_agentos_does_not_have_says_so(runtime):
    check = LaneKindSupported("desktop").check(facts_for(runtime))
    assert not check.ok and check.reason == "agentos has no desktop lanes yet"


@pytest.mark.parametrize("memory, ok", [(16 * GB, True), (int(15.6 * GB), True), (int(3.73 * GB), False), (None, False)])
def test_memory_counts_a_16_gb_machine_that_reports_a_little_less(runtime, memory, ok):
    check = MemoryAtLeast(16).check(facts_for(runtime, memory=memory))
    assert check.ok is ok
    assert "16 GB" in check.reason


def test_a_device_may_have_more_than_one_path(runtime):
    binder = DevicePresent("binder", ("/dev/binder", "/dev/binderfs/binder"), "not loaded")
    assert binder.check(facts_for(runtime, devices=("/dev/binderfs/binder",))).reason == "/dev/binderfs/binder is present"
    assert binder.check(facts_for(runtime)).reason == "/dev/binder is missing: not loaded"


def test_the_current_vm_shows_each_real_android_blocker(runtime, tmp_path):
    """mynode per docs/ANDROID.md: no binder, 3.8 GB. Redroid does not need /dev/kvm."""
    service = PluginService(runtime, home=tmp_path, facts=facts_for(runtime, memory=int(3.73 * GB)))
    unmet = service.view("android-worker").unmet
    assert any("Android engine does not exist" in r for r in unmet)
    assert not any("/dev/kvm" in r for r in unmet)  # redroid needs binder and memory, not hardware virtualization
    assert any("/dev/binder is missing" in r for r in unmet)
    assert any("3.7 GB of memory" in r and "16 GB" in r for r in unmet)


# ---- jobs and capability ----

def test_what_a_real_brain_can_do():
    assert brain_skills(JEV) == {Skill.DECISION}
    assert brain_skills(CHAT) == {Skill.TEXT}
    assert brain_skills(CLAUDE) == {Skill.TEXT, Skill.TOOLS}
    assert brain_skills(object()) == frozenset()


@pytest.mark.parametrize("job, brain", [("decide", "chat"), ("decide", "claude"), ("write", "jev"), ("rescue", "jev")])
def test_a_job_refuses_a_brain_without_the_capability(service, job, brain):
    with pytest.raises(InvalidRequest, match=f"{brain} cannot do the {job} job"):
        admin.assign(service, "browser-worker", job, brain)
    assert service.state().assignments == {}


def test_assign_refuses_what_does_not_exist(service):
    with pytest.raises(InvalidRequest, match="no brain named nobody"):
        admin.assign(service, "browser-worker", "decide", "nobody")
    with pytest.raises(InvalidRequest, match="Its jobs: decide, write, rescue"):
        admin.assign(service, "browser-worker", "dance", "jev")
    with pytest.raises(NotFound, match="This build ships browser-worker"):
        admin.assign(service, "nothing", "decide", "jev")


def test_assign_refuses_when_the_brains_cannot_be_read(tmp_path):
    runtime = Runtime(config=NodeConfig(home=tmp_path))
    with pytest.raises(InvalidRequest, match="brains service is not available"):
        admin.assign(PluginService(runtime, home=tmp_path), "browser-worker", "decide", "jev")


def test_a_brain_that_changes_after_assignment_is_caught(service, runtime):
    """Removed, or registered again without the capability: every check sees it, the worker too."""
    assign_all(service)
    admin.enable(service, "browser-worker")
    service.to_load()
    assert service.brain_for("browser-worker", "decide") == "jev"
    runtime.brains.specs["jev"] = CHAT
    assert any("jev cannot do the decide job" in r for r in service.view("browser-worker").unmet)
    assert service.view("browser-worker").status == "blocked"
    with pytest.raises(ServiceUnavailable, match="jev cannot do the decide job"):
        service.brain_for("browser-worker", "decide")
    del runtime.brains.specs["jev"]
    assert any("no brain named jev" in r for r in service.view("browser-worker").unmet)


def test_the_privacy_rules_are_not_copied_here():
    source = "".join(path.read_text() for path in (SRC / "plugins").glob("*.py"))
    assert not {"privacy_for", "LOCAL_ONLY", "local-only", "REDACTED_CLOUD"} & {w for w in source.replace('"', " ").split()}


# ---- state fails closed ----

def write_enabled_row(home, plugin="browser-worker", value=1):
    store = PluginStore(home)
    store.set_enabled(plugin, False)  # makes the file and its tables; it is off already, so it adds no row
    con = sqlite3.connect(store.path)
    con.execute("INSERT OR REPLACE INTO plugins (id, enabled, updated_at) VALUES (?, ?, 'now')", (plugin, value))
    con.commit()
    con.close()


def test_a_missing_file_means_everything_is_off(tmp_path):
    store = PluginStore(tmp_path)
    state = store.read()
    assert state.enabled == frozenset() and state.problem is None
    assert not store.path.exists()  # looking changes nothing


def test_state_survives_and_can_be_read_after_every_writer_is_gone(tmp_path):
    PluginStore(tmp_path).set_enabled("browser-worker", True)
    PluginStore(tmp_path).assign("browser-worker", "decide", "jev")
    state = PluginStore(tmp_path).read()
    assert state.problem is None and state.enabled == {"browser-worker"}
    assert state.brains_of("browser-worker") == {"decide": "jev"}
    assert sqlite3.connect(tmp_path / FILE_NAME).execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_state_is_private_to_its_owner(tmp_path):
    store = PluginStore(tmp_path)
    store.set_enabled("browser-worker", False)
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_written_as_root_it_stays_readable_by_a_daemon_with_its_own_user(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append((Path(path).name, uid, gid)))
    PluginStore(tmp_path).set_enabled("browser-worker", False)
    home = tmp_path.stat()
    assert calls[0] == (FILE_NAME, home.st_uid, home.st_gid)


@pytest.mark.parametrize("break_it", [
    lambda path: path.write_bytes(b"this is not a database, it is a note to myself" * 100),
    lambda path: path.write_bytes(path.read_bytes()[:150] + b"\0" * 50),
    lambda path: _sql(path, "DROP TABLE plugins"),
    lambda path: _sql(path, "DROP TABLE schema_version"),
    lambda path: _sql(path, "DROP TABLE assignments"),
    lambda path: _sql(path, "DELETE FROM schema_version"),
    lambda path: _sql(path, f"UPDATE schema_version SET version = {SCHEMA_VERSION + 1}"),
    lambda path: _sql(path, "UPDATE plugins SET enabled = 2"),
    lambda path: _sql(path, "UPDATE plugins SET enabled = 'true'"),
    lambda path: _sql(path, "UPDATE plugins SET enabled = 1.5"),
    lambda path: _sql(path, "UPDATE assignments SET brain = ''"),
], ids=["garbage", "truncated", "no-plugins", "no-version", "no-assignments", "version-gone", "newer", "two", "text",
        "float", "empty-brain"])
def test_a_broken_file_turns_nothing_on(service, tmp_path, break_it):
    assign_all(service)
    admin.enable(service, "browser-worker")
    path = tmp_path / FILE_NAME
    break_it(path)
    state = service.state()
    assert state.problem and state.enabled == frozenset()
    assert service.to_load() == []
    view = service.view("browser-worker")
    assert view.enabled is False and view.status != "running"
    with pytest.raises(PluginDisabled, match="could not be read"):
        service.require("browser-worker")


def _sql(path, statement):
    con = sqlite3.connect(path)
    con.execute(statement)
    con.commit()
    con.close()


def test_an_empty_file_reads_as_never_written(tmp_path):
    (tmp_path / FILE_NAME).write_bytes(b"")
    assert PluginStore(tmp_path).read().enabled == frozenset()
    PluginStore(tmp_path).set_enabled("browser-worker", True)
    assert PluginStore(tmp_path).read().enabled == {"browser-worker"}


def test_a_broken_file_is_left_for_a_person_and_never_replaced(service, tmp_path):
    path = tmp_path / FILE_NAME
    path.write_bytes(b"x" * 4096)
    with pytest.raises(StateBroken, match="move it aside"):
        admin.disable(service, "browser-worker")
    with pytest.raises(RequirementsUnmet, match="could not be read"):
        admin.enable(service, "browser-worker")
    assert path.read_bytes() == b"x" * 4096


def test_rows_for_a_plugin_this_build_does_not_have_load_nothing(service, tmp_path):
    PluginStore(tmp_path).set_enabled("someone-elses-plugin", True)
    assert service.to_load() == []
    assert [view.id for view in service.views()] == [p.id for p in PLUGINS]


# ---- enabling ----

def test_off_by_default(service):
    assert {view.status for view in service.views()} == {"unavailable"}
    assert service.to_load() == []


def test_enable_refuses_with_every_unmet_reason_and_changes_nothing(runtime, tmp_path):
    service = PluginService(runtime, home=tmp_path, facts=facts_for(runtime))
    with pytest.raises(RequirementsUnmet) as refused:
        admin.enable(service, "browser-worker")
    message = refused.value.message
    assert message.startswith("browser-worker cannot be turned on. Not met:")
    for reason in service.view("browser-worker").unmet:
        assert f"  - {reason}" in message
    assert "pilot service is not part of this build yet" in message
    assert "no brain is assigned to the decide job, which needs a decision brain" in message
    assert not (tmp_path / FILE_NAME).exists()


def test_enable_works_once_every_requirement_is_met(service):
    assert service.view("browser-worker").status == "unavailable"
    assign_all(service)
    assert service.view("browser-worker").status == "off"
    assert admin.enable(service, "browser-worker").status == "enabled"
    assert [plugin.id for plugin in service.to_load()] == ["browser-worker"]
    assert service.view("browser-worker").status == "blocked"  # the pilot is not really on this fake runtime
    service.runtime.register("pilot", object())
    assert service.view("browser-worker").status == "running"


def test_android_can_never_be_enabled_even_on_perfect_hardware(runtime, tmp_path):
    facts = NodeFacts(runtime=runtime, installed=installed_all, exists=lambda path: True, memory=lambda: 64 * GB)
    service = PluginService(runtime, home=tmp_path, facts=facts)
    for job, brain in (("decide", "jev"), ("write", "chat"), ("rescue", "claude")):
        admin.assign(service, "android-worker", job, brain)
    with pytest.raises(RequirementsUnmet, match="the Android engine does not exist yet"):
        admin.enable(service, "android-worker")
    assert ANDROID_WORKER.services == () and ANDROID_WORKER.tools == ()


@pytest.mark.parametrize("plugin", ["android-worker", "desktop-worker"])
def test_a_hand_written_row_cannot_start_a_worker_that_does_not_exist(service, tmp_path, plugin):
    write_enabled_row(tmp_path, plugin)
    assert service.to_load() == []
    assert service.view(plugin).status == "blocked"


def test_the_daemon_checks_again_at_every_start(service, runtime):
    assign_all(service)
    admin.enable(service, "browser-worker")
    del runtime.brains.specs["chat"]  # gone since the person enabled it
    assert service.to_load() == []
    assert service.view("browser-worker").status == "blocked"


def test_a_check_that_raises_loads_nothing(service, monkeypatch):
    assign_all(service)
    admin.enable(service, "browser-worker")
    monkeypatch.setattr(service, "evaluate", lambda *a, **k: 1 / 0)
    assert service.to_load() == []


# ---- what an agent can do ----

class Ctx:
    def __init__(self):
        self.effects = []

    async def authorize(self, effect, **kwargs):
        self.effects.append(effect)


def plugins_list_of(runtime):
    (spec,) = tool_specs(runtime)
    return spec


def test_the_only_tool_reads():
    specs = tool_specs(Runtime())
    assert [(spec.name, spec.effect) for spec in specs] == [("plugins_list", Effect.READ)]


def test_no_served_tool_can_change_a_plugin():
    result = CliRunner().invoke(app, ["tools", "--json"])
    names = [row["name"] for row in json.loads(result.stdout)]
    assert [name for name in names if "plugin" in name] == ["plugins_list"]


def test_only_the_cli_and_the_signed_in_route_reach_the_writes():
    importers = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            direct = isinstance(node, ast.ImportFrom) and node.module == "agentos.plugins.admin"
            parent = (isinstance(node, ast.ImportFrom) and node.module == "agentos.plugins"
                      and any(alias.name == "admin" for alias in node.names))
            if direct or parent:
                importers.append(str(path.relative_to(SRC)))
    assert sorted(importers) == ["api/routes_worker.py", "api/web/workers.py", "plugins/cli.py"]


def test_the_service_on_the_runtime_has_no_way_to_write():
    assert not {"enable", "disable", "assign", "unassign", "set_enabled", "set_lane"} & set(dir(PluginService))


async def test_an_agent_sees_every_worker_through_the_policy(service, runtime):
    runtime.register("plugins", service)
    ctx = Ctx()
    out = await plugins_list_of(runtime).handler(ctx)
    assert [p["id"] for p in out["plugins"]] == [p.id for p in PLUGINS]
    assert ctx.effects == [Effect.READ]
    json.dumps(out)


async def test_an_agent_asking_for_a_disabled_worker_is_told_a_person_must_enable_it(service, runtime):
    runtime.register("plugins", service)
    with pytest.raises(PluginDisabled) as refused:
        await plugins_list_of(runtime).handler(Ctx(), plugin="browser-worker")
    assert "Only a person can turn it on" in refused.value.message
    assert "agentos plugins enable browser-worker" in refused.value.message
    assert refused.value.code == "plugin_disabled"
    assert "person" in catalog.entry("plugin_disabled").meaning


async def test_an_agent_gets_an_enabled_worker_only_once_it_has_loaded(service, runtime):
    runtime.register("plugins", service)
    tool = plugins_list_of(runtime)
    assign_all(service)
    admin.enable(service, "browser-worker")
    with pytest.raises(ServiceUnavailable, match="next starts"):
        await tool.handler(Ctx(), plugin="browser-worker")
    service.to_load()
    runtime.register("pilot", object())
    assert (await tool.handler(Ctx(), plugin="browser-worker"))["status"] == "running"
    with pytest.raises(NotFound):
        await tool.handler(Ctx(), plugin="nothing")


def test_disable_counts_at_once(service):
    assign_all(service)
    admin.enable(service, "browser-worker")
    service.to_load()
    assert service.require("browser-worker") is BROWSER_WORKER
    admin.disable(service, "browser-worker")
    with pytest.raises(PluginDisabled):
        service.require("browser-worker")


# ---- the CLI ----

@pytest.fixture
def cli(service, runtime, monkeypatch):
    runtime.register("plugins", service)
    monkeypatch.setattr(plugins_cli, "runtime_factory", lambda: runtime)
    return CliRunner()


def test_list_shows_status_jobs_and_the_reason(cli, service):
    admin.assign(service, "browser-worker", "decide", "jev")
    result = cli.invoke(app, ["plugins", "list"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["ID", "STATUS", "STAGE", "JOBS", "WHY"]
    assert "decide: jev, write: –, rescue: –" in lines[1]
    assert "the Android engine does not exist yet" in lines[2]
    assert "planned for later" in lines[3]


def test_list_as_json(cli):
    out = json.loads(cli.invoke(app, ["plugins", "list", "--json"]).stdout)
    assert out["problem"] is None
    assert out["plugins"][0]["checks"][0]["label"] == "pilot service"


def test_show_gives_every_requirement_and_job(cli):
    result = cli.invoke(app, ["plugins", "show", "android-worker"])
    assert result.exit_code == 0, result.output
    assert "not met: the Android engine does not exist yet" in result.stdout
    assert "decide: – (needs a decision brain)" in result.stdout
    assert "rescue: – (optional, needs a text brain or a tool brain)" in result.stdout


def test_a_refused_enable_prints_the_reasons_and_the_fix(cli):
    result = cli.invoke(app, ["plugins", "enable", "android-worker"])
    assert result.exit_code == 1
    assert "error: plugin_requirements_unmet: android-worker cannot be turned on. Not met:" in result.stderr
    assert "  - the Android engine does not exist yet" in result.stderr
    assert "fix: " in result.stderr


def test_enable_and_disable_say_when_they_take_effect(cli, service):
    assign_all(service)
    on = cli.invoke(app, ["plugins", "enable", "browser-worker"])
    assert on.exit_code == 0, on.output
    assert "loads the next time agentosd starts" in on.stdout and "systemctl restart agentosd" in on.stdout
    off = cli.invoke(app, ["plugins", "disable", "browser-worker"])
    assert off.exit_code == 0, off.output
    assert "tools refuse calls from now on" in off.stdout and "until agentosd next starts" in off.stdout


def test_assign_through_the_cli(cli, service):
    good = cli.invoke(app, ["plugins", "assign", "browser-worker", "decide", "jev"])
    assert good.exit_code == 0 and service.state().brains_of("browser-worker") == {"decide": "jev"}
    bad = cli.invoke(app, ["plugins", "assign", "browser-worker", "write", "jev"])
    assert bad.exit_code == 1
    assert "jev cannot do the write job, which needs a text brain" in bad.stderr


def test_a_broken_file_is_named_and_the_list_still_works(cli, tmp_path):
    (tmp_path / FILE_NAME).write_bytes(b"x" * 4096)
    result = cli.invoke(app, ["plugins", "list"])
    assert result.exit_code == 0
    assert "Nothing is on until it is repaired" in result.stderr


def test_the_real_cli_reads_real_brains(tmp_path, monkeypatch):
    """No fakes: the composition root's runtime, a real brain store, a real BrainSpec."""
    from agentos.brains.service import build as build_brains

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    build_brains(Runtime(config=NodeConfig(home=tmp_path))).store.put_brain(JEV)
    runner = CliRunner()
    good = runner.invoke(app, ["plugins", "assign", "browser-worker", "decide", "jev"])
    assert good.exit_code == 0, good.output
    bad = runner.invoke(app, ["plugins", "assign", "browser-worker", "write", "jev"])
    assert bad.exit_code == 1 and "needs a text brain" in bad.stderr
    listed = runner.invoke(app, ["plugins", "list"])
    assert "decide: jev" in listed.stdout and "pilot service is not part of this build yet" not in listed.stdout


# ---- optional jobs ----

def test_only_decide_is_required_of_the_browser_worker():
    assert [(job.name, job.optional) for job in BROWSER_WORKER.jobs] == [
        ("decide", False), ("write", True), ("rescue", True)]


@pytest.mark.parametrize("fields", [{"optional": True}, {"without": "it stops"}])
def test_an_optional_job_says_what_happens_without_it_and_only_that_one_does(fields):
    with pytest.raises(ValueError, match="says what happens without it"):
        Job("x", "d", (Skill.TEXT,), **fields)


def test_a_worker_turns_on_with_only_its_required_job(service):
    admin.assign(service, "browser-worker", "decide", "jev")
    assert admin.enable(service, "browser-worker").enabled
    assert service.to_load() == [BROWSER_WORKER]


def test_a_required_job_still_stops_it(service):
    admin.assign(service, "browser-worker", "write", "chat")
    admin.assign(service, "browser-worker", "rescue", "claude")
    with pytest.raises(RequirementsUnmet, match="no brain is assigned to the decide job"):
        admin.enable(service, "browser-worker")


def test_a_brain_given_an_optional_job_must_still_fit(service, runtime):
    assign_all(service)
    runtime.require("brains").specs["chat"] = JEV.model_copy(update={"name": "chat"})
    assert "chat cannot do the write job, which needs a text brain" in service.view("browser-worker").unmet
    with pytest.raises(RequirementsUnmet, match="chat cannot do the write job"):
        admin.enable(service, "browser-worker")
    admin.unassign(service, "browser-worker", "write")
    assert admin.enable(service, "browser-worker").enabled


def test_a_worker_with_an_empty_optional_job_has_no_brain_for_it(service, runtime):
    admin.assign(service, "browser-worker", "decide", "jev")
    admin.enable(service, "browser-worker")
    service.to_load()
    assert service.brain_for("browser-worker", "decide") == "jev"
    with pytest.raises(ServiceUnavailable, match="no brain is assigned to the rescue job"):
        service.brain_for("browser-worker", "rescue")


def test_show_says_what_the_worker_does_without_each_optional_job(cli):
    shown = cli.invoke(app, ["plugins", "show", "browser-worker"]).stdout
    assert "without it: The worker cannot write text" in shown
    assert "without it: When the worker is unsure, it stops for a person." in shown
    assert shown.count("without it:") == 2


def test_show_as_json_says_so_too(cli):
    jobs = json.loads(cli.invoke(app, ["plugins", "show", "browser-worker", "--json"]).stdout)["jobs"]
    assert [job["optional"] for job in jobs] == [False, True, True]
    assert jobs[2]["without"] == "When the worker is unsure, it stops for a person."


def test_unassign_clears_a_job(cli, service):
    assign_all(service)
    result = cli.invoke(app, ["plugins", "unassign", "browser-worker", "rescue"])
    assert result.exit_code == 0, result.output
    assert "claude no longer does the rescue job of browser-worker" in result.stdout
    assert "it stops for a person" in result.stdout
    assert service.state().brains_of("browser-worker") == {"decide": "jev", "write": "chat"}


def test_unassign_of_a_required_job_says_the_worker_will_not_load(cli, service):
    assign_all(service)
    admin.enable(service, "browser-worker")
    result = cli.invoke(app, ["plugins", "unassign", "browser-worker", "decide"])
    assert result.exit_code == 0, result.output
    assert "does not load until a brain does this job" in result.stdout
    assert service.view("browser-worker").status == "blocked"
    assert service.to_load() == []


def test_unassign_of_an_empty_job_says_so_and_records_nothing(cli, service):
    result = cli.invoke(app, ["plugins", "unassign", "browser-worker", "write"])
    assert result.exit_code == 0 and "had no brain" in result.stdout
    assert "  –" in cli.invoke(app, ["plugins", "show", "browser-worker"]).stdout.split("history:")[1]


def test_unassign_refuses_a_job_that_does_not_exist(cli):
    result = cli.invoke(app, ["plugins", "unassign", "browser-worker", "sing"])
    assert result.exit_code == 1 and "Its jobs: decide, write, rescue" in result.stderr


# ---- the audit trail ----

def history(cli, plugin="browser-worker"):
    out = json.loads(cli.invoke(app, ["plugins", "show", plugin, "--json"]).stdout)
    assert out["history_problem"] is None
    return out["history"]


def test_every_kind_of_change_is_in_the_action_log_and_show_reads_it_back(cli, service):
    for args in (["assign", "browser-worker", "decide", "jev"], ["enable", "browser-worker"],
                 ["assign", "browser-worker", "write", "chat"], ["assign", "browser-worker", "write", "claude"],
                 ["unassign", "browser-worker", "write"], ["disable", "browser-worker"]):
        result = cli.invoke(app, ["plugins", *args])
        assert result.exit_code == 0, result.output
    assert [row["summary"] for row in history(cli)] == [
        "browser-worker disabled",
        "write job of browser-worker cleared (it was claude)",
        "write job of browser-worker moved from chat to claude",
        "write job of browser-worker given to chat",
        "browser-worker enabled",
        "decide job of browser-worker given to jev"]


def test_a_record_says_what_when_and_who(cli, monkeypatch):
    monkeypatch.setenv("SUDO_USER", "robin")
    cli.invoke(app, ["plugins", "assign", "browser-worker", "decide", "jev"])
    row = history(cli)[0]
    assert row["principal"] == "cli:robin" and row["principal_kind"] == "human"
    assert row["kind"] == "plugin_change" and row["at"].startswith("20")
    assert row["args"] == {"plugin": "browser-worker", "what": "assigned", "by": "cli:robin", "job": "decide",
                           "before": None, "after": "jev", "lane": None}


def test_show_prints_the_history(cli):
    cli.invoke(app, ["plugins", "assign", "browser-worker", "decide", "jev"])
    shown = cli.invoke(app, ["plugins", "show", "browser-worker"]).stdout.split("history:\n")[1]
    assert "cli:" in shown and "decide job of browser-worker given to jev" in shown


def test_a_record_is_in_the_real_chained_log_of_this_node(cli, tmp_path):
    from agentos.policy.traces import TraceStore

    cli.invoke(app, ["plugins", "assign", "browser-worker", "decide", "jev"])
    store = TraceStore(tmp_path / "traces.db")
    try:
        assert store.verify().status == "ok"
        assert [row.summary for row in store.query(kind="plugin_change")[0]] == [
            "decide job of browser-worker given to jev"]
    finally:
        store.close()


def test_history_is_per_plugin(cli, service):
    cli.invoke(app, ["plugins", "assign", "browser-worker", "decide", "jev"])
    cli.invoke(app, ["plugins", "assign", "android-worker", "decide", "jev"])
    assert [row["args"]["plugin"] for row in history(cli, "android-worker")] == ["android-worker"]


def test_saying_the_same_thing_again_changes_nothing_and_records_nothing(cli, service):
    assign_all(service)
    cli.invoke(app, ["plugins", "assign", "browser-worker", "decide", "jev"])
    cli.invoke(app, ["plugins", "disable", "browser-worker"])
    assert history(cli) == []


class BrokenLog:
    def __init__(self, error=None):
        self.error = error or OSError("disk full")

    def record(self, change):
        raise self.error

    def read(self, plugin, limit):
        raise self.error

    def close(self):
        pass


@pytest.fixture
def no_record(monkeypatch):
    monkeypatch.setattr(plugins_cli, "change_log_factory", lambda runtime: BrokenLog())


@pytest.mark.parametrize("args", [["enable", "browser-worker"], ["assign", "browser-worker", "write", "claude"],
                                  ["unassign", "browser-worker", "write"]])
def test_a_change_that_cannot_be_recorded_does_not_stand(cli, service, no_record, args):
    admin.assign(service, "browser-worker", "decide", "jev")
    admin.assign(service, "browser-worker", "write", "chat")
    before = service.state()
    result = cli.invoke(app, ["plugins", *args])
    assert result.exit_code == 1
    assert "plugin_change_not_recorded" in result.stderr and "so it was undone (OSError)" in result.stderr
    assert service.state() == before


def test_off_always_works_and_says_nothing_recorded_it(cli, service, no_record):
    assign_all(service)
    admin.enable(service, "browser-worker")
    result = cli.invoke(app, ["plugins", "disable", "browser-worker"])
    assert result.exit_code == 1
    assert "browser-worker is off, and the action log has no record of it" in result.stderr
    assert service.state().enabled == frozenset()


def test_a_build_with_no_action_log_changes_nothing(cli, service, monkeypatch):
    monkeypatch.setattr(plugins_cli, "change_log_factory", None)
    result = cli.invoke(app, ["plugins", "assign", "browser-worker", "decide", "jev"])
    assert result.exit_code == 1 and "this build has no action log" in result.stderr
    assert service.state().assignments == {}


def test_show_still_works_when_the_log_cannot_be_read(cli, no_record):
    result = cli.invoke(app, ["plugins", "show", "browser-worker"])
    assert result.exit_code == 0 and "could not be read: OSError" in result.stdout
    assert "status:" in result.stdout


def test_the_real_composition_root_wires_the_action_log():
    from agentos.commands.features import PluginChanges

    assert plugins_cli.change_log_factory is not None
    assert isinstance(plugins_cli.change_log_factory(Runtime(config=NodeConfig(home=Path("/nowhere")))), PluginChanges)


def test_a_row_the_log_will_not_take_is_a_failure(tmp_path):
    from agentos.commands.features import PluginChanges

    log = PluginChanges(tmp_path)
    log._open().flush = lambda: 1
    with pytest.raises(ServiceUnavailable, match="did not take the row"):
        log.record(Change(plugin="p", what="enabled", by="cli:x"))
    log.close()


def test_root_never_makes_the_log_for_the_daemon(tmp_path, monkeypatch):
    from agentos.commands import features as root_cli

    monkeypatch.setattr(root_cli.os, "geteuid", lambda: 0)
    with pytest.raises(ServiceUnavailable, match="start agentosd once"):
        root_cli.PluginChanges(tmp_path).record(Change(plugin="p", what="enabled", by="cli:x"))
    assert not (tmp_path / "traces.db").exists()


def test_listing_never_opens_the_log(cli, tmp_path):
    assert cli.invoke(app, ["plugins", "list"]).exit_code == 0
    assert not (tmp_path / "traces.db").exists()


# ---- only a person, and what that check is ----

INSIDE = [(admin.LANE_ENV, "w/bench", "lane w/bench"), (admin.BRAIN_TASK_ENV, "t_123", "brain task t_123")]


@pytest.mark.parametrize("variable, value, where", INSIDE)
@pytest.mark.parametrize("args", [["enable", "browser-worker"], ["assign", "browser-worker", "decide", "jev"],
                                  ["unassign", "browser-worker", "decide"]])
def test_a_brain_or_a_lane_cannot_turn_on_or_reassign(cli, service, tmp_path, monkeypatch, variable, value, where, args):
    admin.assign(service, "browser-worker", "decide", "jev")
    before = service.state()
    monkeypatch.setenv(variable, value)
    result = cli.invoke(app, ["plugins", *args])
    assert result.exit_code == 1
    assert "plugin_person_only" in result.stderr and f"running inside {where}" in result.stderr
    assert "fix: " in result.stderr
    assert service.state() == before
    assert not (tmp_path / "traces.db").exists()  # refused before anything is opened


@pytest.mark.parametrize("variable, value, where", INSIDE)
def test_turning_off_works_from_anywhere(cli, service, monkeypatch, variable, value, where):
    assign_all(service)
    admin.enable(service, "browser-worker")
    monkeypatch.setenv(variable, value)
    assert cli.invoke(app, ["plugins", "disable", "browser-worker"]).exit_code == 0
    assert service.state().enabled == frozenset()


@pytest.mark.parametrize("variable, value, where", INSIDE)
def test_reading_works_from_anywhere(cli, monkeypatch, variable, value, where):
    monkeypatch.setenv(variable, value)
    assert cli.invoke(app, ["plugins", "list"]).exit_code == 0
    assert cli.invoke(app, ["plugins", "show", "browser-worker"]).exit_code == 0


def test_an_empty_variable_is_no_lane():
    admin.require_person({admin.LANE_ENV: "", admin.BRAIN_TASK_ENV: ""})


def test_the_variables_are_the_ones_the_runners_really_set(tmp_path):
    """The guard names them as strings, because this package may not import either feature."""
    from types import SimpleNamespace

    from agentos.brains import adapters
    from agentos.brains.settings import BrainsSettings
    from agentos.env.lanes import LaneRuntime

    assert admin.BRAIN_TASK_ENV == adapters.BRAIN_TASK_ENV
    env = adapters.base_env(bin_path="/bin/claude", settings=BrainsSettings(), home=tmp_path, user=None, task_id="t_1")
    assert env[admin.BRAIN_TASK_ENV] == "t_1"
    for isolated in (False, True):
        lane = SimpleNamespace(lane=SimpleNamespace(id="w/x", isolated=isolated, profile=SimpleNamespace(network="host")),
                               plan=None, proxy_port=None)
        assert LaneRuntime.base_env(lane)[admin.LANE_ENV] == "w/x"


def test_nothing_a_person_reads_has_an_em_dash(cli):
    outputs = [cli.invoke(app, ["plugins", *args]) for args in (
        ["list"], ["show", "browser-worker"], ["show", "android-worker"], ["show", "desktop-worker"],
        ["enable", "android-worker"], ["disable", "browser-worker"], ["--help"])]
    assert all("—" not in result.output for result in outputs)
    for code in ("plugin_disabled", "plugin_requirements_unmet", "plugin_state_broken"):
        found = catalog.entry(code)
        assert "—" not in found.meaning + found.agent + found.fix


# ---- the worker on one lane ----

LANE = "biz-a/social"


def old_file(tmp_path, *, enabled=True):
    """A plugins.db from before the lane switch: the same version, with no such table."""
    con = sqlite3.connect(tmp_path / "plugins.db")
    con.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    con.execute("INSERT INTO schema_version VALUES (1)")
    con.execute("CREATE TABLE plugins (id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, updated_at TEXT NOT NULL)")
    con.execute("CREATE TABLE assignments (plugin TEXT NOT NULL, job TEXT NOT NULL, brain TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, PRIMARY KEY (plugin, job))")
    con.execute("INSERT INTO plugins VALUES ('browser-worker', ?, 'x')", (int(enabled),))
    con.commit()
    con.close()


def test_a_lane_is_on_until_a_person_turns_it_off(service):
    assert service.lane_stop("browser-worker", LANE) is None
    admin.set_lane(service, LANE, False)
    assert "person turned the browser worker off on biz-a/social" in service.lane_stop("browser-worker", LANE)
    assert service.lane_stop("browser-worker", "biz-a/other") is None
    assert service.view("browser-worker").lanes_off == (LANE,)
    admin.set_lane(service, LANE, True)
    assert service.lane_stop("browser-worker", LANE) is None and service.view("browser-worker").lanes_off == ()


def test_a_file_from_before_the_switch_still_reads_and_the_next_write_adds_it(service, tmp_path):
    old_file(tmp_path)
    state = service.state()
    assert state.problem is None and "browser-worker" in state.enabled and state.off_on("browser-worker") == frozenset()
    admin.set_lane(service, LANE, False)
    state = service.state()
    assert state.problem is None and "browser-worker" in state.enabled and state.off_on("browser-worker") == {LANE}


def test_a_lane_switch_with_a_bad_value_reads_as_everything_off(service, tmp_path):
    admin.set_lane(service, LANE, False)
    con = sqlite3.connect(tmp_path / "plugins.db")
    con.execute("UPDATE lane_switches SET enabled = 7")
    con.commit()
    con.close()
    assert service.state().problem is not None
    assert "could not be read" in service.lane_stop("browser-worker", "biz-a/other")


def test_a_lane_id_that_is_not_one_is_refused(service):
    with pytest.raises(InvalidRequest, match="not a lane id"):
        admin.set_lane(service, "no-slash", False)


@pytest.mark.parametrize("variable", [admin.LANE_ENV, admin.BRAIN_TASK_ENV])
def test_only_a_person_turns_a_lane_on_and_off_works_from_anywhere(service, monkeypatch, variable):
    monkeypatch.setenv(variable, "biz-a/shell")
    admin.set_lane(service, LANE, False)
    assert service.view("browser-worker").lanes_off == (LANE,)
    with pytest.raises(OnlyAPerson) as refused:
        admin.set_lane(service, LANE, True)
    assert refused.value.code == "plugin_person_only"
    assert service.view("browser-worker").lanes_off == (LANE,)


def test_the_cli_shows_sets_and_records_a_lane(cli, service):
    on = cli.invoke(app, ["plugins", "lane", LANE])
    assert on.exit_code == 0 and f"{LANE}: the browser worker is on here" in on.stdout
    off = cli.invoke(app, ["plugins", "lane", LANE, "off"])
    assert off.exit_code == 0, off.output
    assert "is off here" in off.stdout and "browser tools" in off.stdout and "take over" in off.stdout
    assert json.loads(cli.invoke(app, ["plugins", "lane", LANE, "--json"]).stdout) == {
        "lane": LANE, "plugin": "browser-worker", "on": False}
    assert f"off on lanes: {LANE}" in cli.invoke(app, ["plugins", "show", "browser-worker"]).stdout
    assert json.loads(cli.invoke(app, ["plugins", "show", "browser-worker", "--json"]).stdout)["lanes_off"] == [LANE]
    assert cli.invoke(app, ["plugins", "lane", LANE, "on"]).exit_code == 0
    assert [row["summary"] for row in history(cli)] == [f"browser-worker turned on on {LANE}",
                                                        f"browser-worker turned off on {LANE}"]
    assert history(cli)[0]["args"]["lane"] == LANE
    for result in (on, off):
        assert "—" not in result.output


def test_a_lane_switch_that_changes_nothing_writes_nothing(cli):
    assert cli.invoke(app, ["plugins", "lane", LANE, "on"]).exit_code == 0
    assert history(cli) == []


def test_the_cli_refuses_a_word_it_does_not_know(cli, service):
    result = cli.invoke(app, ["plugins", "lane", LANE, "maybe"])
    assert result.exit_code == 1 and service.view("browser-worker").lanes_off == ()


def test_the_cli_says_who_may_turn_a_lane_on(cli, service, monkeypatch):
    assert cli.invoke(app, ["plugins", "lane", LANE, "off"]).exit_code == 0
    monkeypatch.setenv(admin.LANE_ENV, "biz-a/shell")
    result = cli.invoke(app, ["plugins", "lane", LANE, "on"])
    assert result.exit_code == 1 and "plugin_person_only" in result.output
    assert service.view("browser-worker").lanes_off == (LANE,)


def test_off_goes_through_with_no_record_and_says_so_and_on_does_not(service):
    """This is the node's first write as well: the file it leaves must still read."""
    with pytest.raises(ChangeNotRecorded, match="off on biz-a/social, and the action log has no record"):
        admin.set_lane(service, LANE, False, BrokenLog())
    assert service.state().problem is None and service.view("browser-worker").lanes_off == (LANE,)
    with pytest.raises(ChangeNotRecorded):
        admin.set_lane(service, LANE, True, BrokenLog())
    assert service.view("browser-worker").lanes_off == (LANE,)


def test_a_first_change_that_is_undone_leaves_a_file_that_still_reads(service):
    with pytest.raises(ChangeNotRecorded):
        admin.assign(service, "browser-worker", "decide", "jev", BrokenLog())
    assert service.state().problem is None and service.state().assignments == {}


async def test_an_agent_reads_only_the_lanes_it_reaches(service, runtime):
    admin.set_lane(service, LANE, False)
    admin.set_lane(service, "personal/secret", False)
    runtime.register("plugins", service)
    ctx = Ctx()
    ctx.can_access_lane = lambda lane: lane.startswith("biz-a/")
    out = await plugins_list_of(runtime).handler(ctx)
    assert next(p for p in out["plugins"] if p["id"] == "browser-worker")["lanes_off"] == [LANE]
    assert "personal/secret" not in json.dumps(out)


def test_a_missing_worker_tool_names_the_first_step_that_works(service, runtime):
    """QA 2026-11-10, tester B 6: the hint said `enable` first (refused until every job has a brain),
    "restarts it" said nothing, and the name read "Browser worker worker"."""
    from agentos.plugins.service import missing_tool
    runtime.register("plugins", service)
    text = missing_tool(runtime, "lane_do")
    assert "worker worker" not in text and "Browser worker (browser-worker)" in text
    assert "agentos plugins assign browser-worker decide BRAIN" in text and "a decision brain" in text
    assert text.index("plugins assign") < text.index("plugins enable") < text.index("restart agentosd")
    assert "—" not in text
    assign_all(service)
    text = missing_tool(runtime, "lane_do")
    assert "plugins assign" not in text and "agentos plugins enable browser-worker" in text
