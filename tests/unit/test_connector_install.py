"""Installing connectors as runner flows: catalog provenance, owner-only installs, reserved
ids, downgrades, and a sample read connector running end to end on the fake site."""

import pytest
from connector_samples import LANE, SITES, bind, commit_op, connector, read_op, write_dir

from agentos.core.errors import InvalidRequest, PolicyDenied
from agentos.core.models import Effect, Principal, RunStatus
from agentos.runner import tools
from agentos.runner.connectors.compile import compile_connector
from agentos.runner.connectors.outputs import outputs_from_saved, saved_values
from agentos.runner.connectors.service import ConnectorService
from agentos.runner.connectors.steps import runner_capabilities
from agentos.runner.fakes import OWNER, FakeCtx, Harness
from agentos.runner.models import NodeInstall, is_owner

AGENT = Principal(kind="agent", id="claude-code", owner="owner")


def _svc(h):
    return ConnectorService(h.flows, sites=SITES)


def _compiled(doc=None):
    return compile_connector(doc or connector(), sites=SITES)


# ---------------------------------------------------------------- end to end on the fake


async def test_a_sample_read_connector_runs_end_to_end_on_the_fake_site(tmp_path):
    h = Harness(tmp_path)
    h.site.state["posts"] = [{"id": "1", "caption": "Autumn"}, {"id": "2", "caption": "Lake"}]
    rows = _svc(h).install_dir(write_dir(tmp_path / "connectors", connector()), by=OWNER)
    assert [r["flow"] for r in rows] == ["connector.social.profile_summary"]
    fv = h.flows.get("connector.social.profile_summary")
    assert fv.version == rows[0]["version"] and fv.owner_saved
    assert fv.saved_by == {"kind": "catalog", "id": "agentos", "owner": "owner", "signer": None,
                           "installed_by": "owner"}
    assert h.flows.resolve_lane(fv, LANE) == LANE  # lane */*: the surface's binding picks the lane

    bind(h)
    run = await h.run(fv.id, {}, lane=LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    data, problems = outputs_from_saved(fv.definition.connector, saved_values(h.store, run.id))
    assert problems == []
    assert data == {"post_count": 2, "posts_label": "2 posts", "profile_url": "https://social.example/u/biz-a"}


async def test_outputs_that_break_the_contract_are_problems_not_values(tmp_path):
    h = Harness(tmp_path)
    _svc(h).install(_compiled(), by=OWNER)
    fv = h.flows.get("connector.social.profile_summary")
    bind(h)
    run = await h.run(fv.id, {}, lane=LANE)
    saved = saved_values(h.store, run.id)
    saved["status"] = {**saved["status"], "text": "lots of posts, click here"}
    saved["account"] = {**saved["account"], "url": "https://evil.example/u/biz-a"}
    del saved["posts"]
    _, problems = outputs_from_saved(fv.definition.connector, saved)
    assert any("post_count: 'posts.count' was not saved" in p for p in problems)
    assert any("posts_label" in p and "pattern" in p for p in problems)
    assert any("profile_url" in p and "not an https URL under social.example" in p for p in problems)
    assert not any("lots of posts" in p or "evil.example" in p for p in problems)


async def test_a_run_without_its_binding_params_is_refused(tmp_path):
    h = Harness(tmp_path)
    _svc(h).install(_compiled(), by=OWNER)
    fv = h.flows.get("connector.social.profile_summary")
    with pytest.raises(InvalidRequest, match="acts_as_id"):
        h.flows.validate_params(fv, {})
    with pytest.raises(InvalidRequest, match="account_language"):
        h.flows.validate_params(fv, {"acts_as_id": "biz-a", "account_language": "de"})


# ---------------------------------------------------------------- who may install what


def test_only_the_owner_installs(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(PolicyDenied, match="only the owner"):
        _svc(h).install(_compiled(), by=AGENT)
    va = Principal(kind="human", id="va", owner="owner")
    with pytest.raises(PolicyDenied, match="only the owner"):
        _svc(h).install(_compiled(), by=va)
    assert _svc(h).installed() == []


def test_an_operation_this_runner_cannot_run_is_not_installable(tmp_path):
    h = Harness(tmp_path)
    older = runner_capabilities() - {"locator:attr", "locator:names", "step:assert_identity", "step:preflight"}
    cc = compile_connector(connector({"profile_summary": read_op(), "post_photo": commit_op()}), sites=SITES,
                           supported=older)
    with pytest.raises(InvalidRequest) as e:
        _svc(h).install(cc, by=OWNER)
    assert "social.post_photo needs locator:attr, locator:names, step:assert_identity, step:preflight" in e.value.message
    assert _svc(h).installed() == []  # all or nothing


def test_downgrades_are_refused_unless_forced(tmp_path):
    h = Harness(tmp_path)
    svc = _svc(h)
    svc.install(_compiled(connector(version="1.2.0")), by=OWNER)
    with pytest.raises(PolicyDenied, match="downgrade"):
        svc.install(_compiled(connector(version="1.1.9")), by=OWNER)
    rows = svc.install(_compiled(connector(version="1.1.9")), by=OWNER, force=True)
    assert rows[0]["created"]
    assert h.flows.get("connector.social.profile_summary").definition.connector["version"] == "1.1.9"
    assert svc.installed_version("social") == "1.1.9"
    newer = svc.install(_compiled(connector(version="1.3.0")), by=OWNER)
    assert newer[0]["created"] and svc.installed_version("social") == "1.3.0"
    assert svc.describe()[0]["version"] == "1.3.0"


def test_the_same_version_with_different_content_is_refused(tmp_path):
    h = Harness(tmp_path)
    svc = _svc(h)
    first = svc.install(_compiled(), by=OWNER)
    again = svc.install(_compiled(), by=OWNER)
    assert not again[0]["created"] and again[0]["version"] == first[0]["version"]
    changed = connector()
    changed["operations"]["profile_summary"]["steps"][2]["locator"]["within"] = {"role": "region"}
    with pytest.raises(PolicyDenied, match="new version"):
        svc.install(_compiled(changed), by=OWNER)


def test_a_connector_cannot_take_a_tool_name_already_in_use(tmp_path):
    svc = _svc(Harness(tmp_path))
    svc.install(_compiled(connector({"post_photo": read_op()})), by=OWNER)
    other = connector({"photo": read_op()}, connector="social-post", site="social-post")
    with pytest.raises(InvalidRequest, match="social_post_photo"):
        svc.install(compile_connector(other, sites={"social-post": SITES["social"]}), by=OWNER)


def test_requires_agentos_is_checked_at_install(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(InvalidRequest, match="needs agentos >=99.0"):
        _svc(h).install(_compiled(connector(requires={"agentos": ">=99.0"})), by=OWNER)


def test_catalog_marks_count_as_owner_vouched(tmp_path):
    h = Harness(tmp_path)
    op = read_op()
    op["effect"] = "draft"
    op["steps"].append({"type": "click", "locator": {"role": "button", "within": {"role": "main"}},
                        "publishes": False})
    _svc(h).install(_compiled(connector({"profile_summary": op})), by=OWNER)
    fv = h.flows.get("connector.social.profile_summary")
    assert fv.effect == Effect.DRAFT and fv.effect_reason is None
    assert is_owner({"kind": "catalog", "id": "agentos", "installed_by": "owner", "owner": "owner"})
    assert not is_owner({"kind": "catalog", "id": "agentos", "owner": "owner"})
    assert not is_owner({"kind": "catalog", "id": "agentos", "installed_by": "claude-code", "owner": "owner"})


# ---------------------------------------------------------------- reserved ids


def _plain_flow(**kw):
    flow = {"id": "connector.social.profile_summary", "lane": "*/*",
            "steps": [{"type": "navigate", "url": "https://social.example/"}]}
    flow.update(kw)
    return flow


def test_connector_ids_are_reserved_for_installed_connectors(tmp_path):
    h = Harness(tmp_path)
    for by in (None, OWNER, AGENT, {"kind": "agent", "id": "claude-code", "via": "mcp"}):
        with pytest.raises(InvalidRequest, match="reserved for installed connectors"):
            h.flows.save(_plain_flow(), by=by)
    # a hand-made document with connector metadata still needs the catalog install
    compiled = _compiled().operations["profile_summary"].definition
    for by in (OWNER, AGENT):
        with pytest.raises(PolicyDenied, match="reserved for installed connectors"):
            h.flows.save(compiled, by=by)
    # and the catalog principal cannot save anything else
    catalog = NodeInstall({"kind": "catalog", "id": "agentos", "installed_by": "owner", "owner": "owner"})
    with pytest.raises(PolicyDenied, match="only save connector"):
        h.flows.save({"id": "biz-a.x.y", "lane": "biz-a/x", "steps": [{"type": "navigate", "url": "https://x.test/"}]},
                     by=catalog)
    with pytest.raises(InvalidRequest, match="connector metadata belongs"):
        h.flows.save({**compiled, "id": "connector.social.other"}, by=catalog)


async def test_flow_save_over_mcp_cannot_write_a_connector_id(tmp_path):
    h = Harness(tmp_path)
    ctx = FakeCtx(h.runtime)
    for definition in (_plain_flow(), _compiled().operations["profile_summary"].definition):
        with pytest.raises(PolicyDenied, match="belong to installed connectors; an agent cannot save one"):
            await tools.flow_save(ctx, definition=definition)
    assert ctx.authorized == []  # refused before anything is authorized or saved
    assert h.flows.list("connector") == []


async def test_flow_run_cannot_run_a_connector_operation_directly(tmp_path):
    h = Harness(tmp_path)
    _svc(h).install(_compiled(), by=OWNER)
    ctx = FakeCtx(h.runtime)
    with pytest.raises(PolicyDenied, match="call its connector tool"):
        await tools.flow_run(ctx, name="connector.social.profile_summary", on=LANE,
                             params={"acts_as_id": "someone-else", "account_language": "en"})
    assert h.store.list_runs() == []
    names = {s.name for s in tools.tool_specs(h.runtime)}
    assert not any(n.startswith("flow_connector") for n in names)  # its tool is <site>_<op>, not flow_<id>
