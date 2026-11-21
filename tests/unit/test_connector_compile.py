"""Compiling connectors into runner flows: the shape, the two hashes, pending capabilities
and the strict lint."""

import copy

import pytest
from connector_samples import SITES, commit_op, connector, read_op, write_dir

from openajar.core.models import Effect
from openajar.runner.connectors.compile import (
    ConnectorInvalid,
    compile_connector,
    compile_path,
    contract_hash,
)
from openajar.runner.connectors.models import Connector
from openajar.runner.connectors.steps import PENDING_MARKER, runner_capabilities
from openajar.runner.models import FlowDefinition


def _compile(doc, **kw):
    return compile_connector(doc, sites=SITES, **kw)


def _errors(doc, **kw):
    with pytest.raises(ConnectorInvalid) as e:
        _compile(doc, **kw)
    return [f"{i.where}: {i.message}" for i in e.value.issues if i.level == "error"]


def _one(errors, needle):
    assert any(needle in e for e in errors), errors


# ---------------------------------------------------------------- the compiled flow


def test_a_read_operation_compiles_into_a_flow_the_runner_accepts():
    cc = _compile(connector())
    op = cc.operations["profile_summary"]
    assert op.flow_id == "connector.social.profile_summary"
    assert op.tool_name == "social_profile_summary" and op.dotted == "social.profile_summary"
    assert op.pending == () and op.installable
    flow = FlowDefinition.model_validate(op.definition)
    assert flow.lane == "*/*" and flow.lane_is_pattern
    assert flow.domain == "social.example" and flow.effect == Effect.READ
    assert flow.expose_as_tool is False
    assert "connector" in flow.tags and "connector:social" in flow.tags
    params = flow.params
    assert params["required"] == ["acts_as_id", "account_language"]
    assert params["properties"]["acts_as_id"]["x-agentos-binding"] is True
    assert params["properties"]["account_language"]["enum"] == ["en", "lt"]
    meta = flow.connector
    assert meta["version"] == "1.0.0" and meta["contract"] == op.contract
    assert meta["outputs"] == {"post_count": "posts.count", "posts_label": "status.text",
                               "profile_url": "account.url"}
    assert meta["acting_as"] == {"kind": "account"} and meta["rehearsal"] == "full"
    # the implementation hash is the flow version the runner will compute
    assert op.implementation == flow.content_hash()
    assert cc.index() == {"connector": "social", "site": "social", "version": "1.0.0", "operations": {
        "profile_summary": {"effect": "read", "contract": op.contract, "implementation": op.implementation}}}


BRIEF_B = frozenset({"locator:attr", "locator:names", "step:assert_identity", "step:preflight", "step:extract",
                     "step:for_each"})


def test_the_runner_supports_every_connector_extension():
    assert BRIEF_B <= runner_capabilities()


def test_a_commit_operation_compiles_into_a_flow_the_runner_runs():
    cc = _compile(connector({"post_photo": commit_op()}))
    op = cc.operations["post_photo"]
    assert op.pending == () and op.installable
    flow = op.flow
    assert flow.content_hash() == op.implementation
    assert [s.type for s in flow.steps] == ["navigate", "assert_identity", "click", "upload", "fill", "preflight",
                                            "click", "readback"]
    ident = op.definition["steps"][1]
    assert ident == {"type": "assert_identity", "label": "acting as", "acting_as": "account", "source": "cookie",
                     "expect": "${acts_as_id}", "timeout_ms": 10_000}
    assert flow.steps[1].expect == "${acts_as_id}" and flow.steps[1].source == "cookie"
    pre = flow.steps[5]
    assert pre.label == "preflight" and [c.kind for c in pre.checks] == ["value_equals", "count", "acting_as"]
    assert pre.checks[0].param == "caption" and pre.checks[2].expect == "${acts_as_id}"
    assert flow.steps[6].label == "share" and flow.steps[6].locator.names == {"en": "Share", "lt": "Bendrinti"}
    assert flow.steps[3].locator.attr.equals == "file"
    assert not any(PENDING_MARKER in s for s in op.definition["steps"])
    meta = flow.connector
    assert meta["commit"] == {"step": "share", "request": commit_op()["commit"]["request"]}
    assert meta["readback"]["public"] == "cookieless" and meta["rehearsal"] == "until_commit"
    assert meta["pace"]["per_day"] == 3 and meta["signals"]["benign"] == ["save_login_info"]
    assert meta["discard"] == [{"type": "press", "key": "Escape", "publishes": False}]
    assert [s.type for s in flow.discard_steps()] == ["press"]


def test_an_older_runner_marks_what_it_lacks_as_pending():
    cc = _compile(connector({"post_photo": commit_op()}), supported=runner_capabilities() - BRIEF_B)
    op = cc.operations["post_photo"]
    assert op.pending == ("locator:attr", "locator:names", "step:assert_identity", "step:preflight")
    assert not op.installable and op.flow is None
    steps = op.definition["steps"]
    assert steps[1] == {"type": "assert_identity", "label": "acting as", "acting_as": "account", "source": "cookie",
                        "expect": "${acts_as_id}", PENDING_MARKER: ["step:assert_identity"]}
    assert steps[5]["label"] == "preflight" and steps[5][PENDING_MARKER] == ["step:preflight"]
    assert steps[5]["checks"][2] == {"kind": "acting_as", "acting_as": "account", "source": "cookie",
                                     "expect": "${acts_as_id}"}
    assert steps[6][PENDING_MARKER] == ["locator:names"] and steps[3][PENDING_MARKER] == ["locator:attr"]
    assert PENDING_MARKER not in steps[2]


def test_pending_markers_follow_what_the_runner_supports():
    read = read_op()
    read["steps"][1]["locator"] = {"role": "heading", "attr": {"name": "data-account", "equals": "${acts_as_id}"},
                                   "within": {"role": "main"}}
    older = runner_capabilities() - {"locator:attr"}
    pending = _compile(connector({"profile_summary": read}), supported=older).operations["profile_summary"]
    assert pending.pending == ("locator:attr",)
    assert pending.definition["steps"][1][PENDING_MARKER] == ["locator:attr"]
    # with the capability present there is no marker, and the runner validates the step itself
    op = _compile(connector({"profile_summary": read})).operations["profile_summary"]
    assert op.pending == () and op.flow.steps[1].locator.attr.equals == "${acts_as_id}"
    read["steps"][1]["locator"]["attr"] = {"name": "data-account", "equals": "${nobody}"}
    _one(_errors(connector({"profile_summary": read})), "${nobody} is not a declared param")


def test_extract_and_for_each_compile_as_read_steps():
    read = read_op()
    read["steps"] += [
        {"type": "extract", "save_as": "items", "items": {"role": "article", "within": {"role": "main"}},
         "max_items": 20, "fields": {
             "id": {"attr": "data-id", "pattern": "^[0-9]{1,6}$"},
             "body": {"read": "own_text", "max_chars": 500, "untrusted": True}}},
        {"type": "for_each", "over": "items", "as": "item", "max_items": 5, "steps": [
            {"type": "readback", "url": "https://social.example/p/${vars.item.id}",
             "locator": {"role": "article", "within": {"role": "main"}, "nth": 0},
             "condition": {"kind": "exists"}}]},
    ]
    op = _compile(connector({"profile_summary": read})).operations["profile_summary"]
    assert op.pending == () and op.effect == Effect.READ and op.installable
    assert [s.type for s in op.flow.steps][-2:] == ["extract", "for_each"]
    older = runner_capabilities() - BRIEF_B
    op = _compile(connector({"profile_summary": read}), supported=older).operations["profile_summary"]
    assert op.pending == ("step:extract", "step:for_each")


def test_discard_steps_are_validated_as_real_steps():
    op = commit_op()
    op["discard"] = [{"type": "press", "key": "Escape", "publishes": False},
                     {"type": "click", "locator": {"role": "button", "names": {"en": "Discard"}}, "publishes": False}]
    _one(_errors(connector({"post_photo": op})), "names has no entry for lt")
    op["discard"][1]["locator"]["names"]["lt"] = "Atmesti"
    flow = _compile(connector({"post_photo": op})).operations["post_photo"].flow
    assert [s.type for s in flow.discard_steps()] == ["press", "click"]
    bad = copy.deepcopy(flow.canonical())
    bad["connector"]["discard"][1].pop("publishes")
    with pytest.raises(ValueError, match="closes the composer without saving"):
        FlowDefinition.model_validate(bad)
    bad["connector"]["discard"] = [{"type": "navigate", "url": "https://social.example/${ghost}"}]
    with pytest.raises(ValueError, match=r"\$\{ghost\} is not a declared param"):
        FlowDefinition.model_validate(bad)


def test_effect_mismatch_is_a_compile_error():
    read = read_op()
    read["steps"].append({"type": "click", "locator": {"role": "button", "name": "Follow"}})
    _one(_errors(connector({"profile_summary": read})), "declared read, but step 4 (click) is commit")
    read["steps"][-1]["publishes"] = False  # still above read: an input step is at least draft
    _one(_errors(connector({"profile_summary": read})), "declared read, but step 4 (click) is draft")


def test_a_draft_operation_vouched_by_the_catalog_is_draft():
    op = read_op()
    op["effect"] = "draft"
    op["steps"].append({"type": "click", "locator": {"role": "button", "name": "New post",
                                                     "within": {"role": "main"}}, "publishes": False})
    cc = _compile(connector({"profile_summary": op}))
    assert cc.operations["profile_summary"].effect == Effect.DRAFT


# ---------------------------------------------------------------- the two hashes


def _hashes(doc):
    op = next(iter(_compile(doc).operations.values()))
    return op.contract, op.implementation


def test_contract_hash_survives_a_locator_only_repair():
    before = _hashes(connector())
    repaired = connector()
    loc = repaired["operations"]["profile_summary"]["steps"][2]["locator"]
    loc["within"] = {"role": "region", "within": {"role": "main"}}
    after = _hashes(repaired)
    assert after[0] == before[0]
    assert after[1] != before[1]


def test_contract_hash_ignores_descriptions_but_not_schemas():
    base = _hashes(connector())
    worded = connector()
    worded["operations"]["profile_summary"]["output"]["properties"]["post_count"]["description"] = "how many"
    worded["operations"]["profile_summary"]["title"] = "Profile at a glance"
    assert _hashes(worded)[0] == base[0]

    for edit in (
        lambda o: o["output"]["properties"]["posts_label"].update(pattern="^[0-9]{1,5} posts$"),
        lambda o: o["input"]["properties"].update(since={"type": "string", "format": "date-time"}),
        lambda o: o.update(effect="draft"),
        lambda o: o["readback"].update(public="cookieless"),
    ):
        changed = connector()
        edit(changed["operations"]["profile_summary"])
        if changed["operations"]["profile_summary"]["effect"] == "draft":
            changed["operations"]["profile_summary"]["steps"].append(
                {"type": "click", "locator": {"role": "button", "within": {"role": "main"}}, "publishes": False})
        assert _hashes(changed)[0] != base[0]


def test_contract_hash_changes_with_the_commit_request_and_acting_as():
    doc = connector({"post_photo": commit_op()})
    c = Connector.model_validate(doc)
    base = contract_hash("social", "post_photo", c.operations["post_photo"])
    req = copy.deepcopy(doc)
    req["operations"]["post_photo"]["commit"]["request"]["url"] = "^https://social\\.example/api/v2/posts$"
    c2 = Connector.model_validate(req)
    assert contract_hash("social", "post_photo", c2.operations["post_photo"]) != base
    pre = copy.deepcopy(doc)  # a preflight change is implementation, not contract
    pre["operations"]["post_photo"]["commit"]["preflight"].pop(1)
    c3 = Connector.model_validate(pre)
    assert contract_hash("social", "post_photo", c3.operations["post_photo"]) == base
    assert contract_hash("other", "post_photo", c.operations["post_photo"]) != base


def test_compiling_twice_gives_the_same_hashes():
    assert _hashes(connector()) == _hashes(connector())


# ---------------------------------------------------------------- the strict lint


def _commit(**edits):
    op = commit_op()
    for path, value in edits.items():
        cur = op
        *parts, last = path.split("__")
        for p in parts:
            cur = cur[int(p)] if isinstance(cur, list) else cur[p]
        if value is None:
            del cur[int(last) if isinstance(cur, list) else last]
        elif isinstance(cur, list):
            cur[int(last)] = value
        else:
            cur[last] = value
    return connector({"post_photo": op})


@pytest.mark.parametrize(("doc", "needle"), [
    (_commit(commit__preflight=[]), "checks the composer right before the commit"),
    (_commit(steps__5__label="go"), "exactly one top-level step is labelled 'share'"),
    (_commit(steps__2__publishes=None), "only the commit step may publish"),
    (_commit(steps__5__publishes=False), "cannot be \"publishes\": false"),
    (_commit(steps__5__type="fill", steps__5__param="caption"), "click or press"),
    (_commit(steps__1=None), "checks acting as (assert_identity) before the commit"),
    (_commit(steps__4__locator={"role": "textbox", "name": "Caption"}), "need a structural part"),
    (_commit(steps__4__locator={"css": "textarea"}), "need a structural part"),
    (_commit(commit__preflight__0__locator={"role": "textbox", "name": "Caption"}), "need a structural part"),
    (_commit(input__properties__caption={"type": "string", "maxLength": 2200}), "must be shown"),
    (_commit(readback=None), "reads the live object back"),
    (_commit(pace={"per_day": 3}), "sets per_day and min_gap_minutes"),
    (_commit(commit=None), "names its commit step"),
    (_commit(discard=[{"type": "click", "locator": {"role": "button", "within": {"role": "dialog"}}}]),
     "a discard step closes the composer"),
    (_commit(input__properties__image={"type": "string", "format": "agentos-file"}), "media types and max_bytes"),
    (_commit(steps__5__locator={"role": "button", "names": {"en": "Share"}, "within": {"role": "dialog"}}),
     "names has no entry for lt"),
])
def test_strict_lint_commit_rules(doc, needle):
    _one(_errors(doc), needle)


def _read(**edits):
    op = read_op()
    for fn in edits.values():
        fn(op)
    return connector({"profile_summary": op})


@pytest.mark.parametrize(("doc", "needle"), [
    (_read(a=lambda o: o["input"].pop("additionalProperties")), "cannot pass unknown params"),
    (_read(a=lambda o: o["output"].pop("additionalProperties")), "returns declared fields only"),
    (_read(a=lambda o: o["output"]["properties"]["posts_label"].pop("pattern")), "cannot carry prose"),
    (_read(a=lambda o: o["output"]["properties"]["profile_url"].pop("x-agentos-host")), "names its host"),
    (_read(a=lambda o: o["output"]["properties"]["profile_url"].update({"x-agentos-host": "evil.example"})),
     "names its host"),
    (_read(a=lambda o: o["readback"]["live"]["outputs"].pop("post_count")), "required output 'post_count'"),
    (_read(a=lambda o: o["readback"]["live"]["outputs"].update(extra="posts.count")), "not declared in the output"),
    (_read(a=lambda o: o["readback"]["live"]["outputs"].update(post_count="nothing.count")), "not saved by any step"),
    (_read(a=lambda o: o.update(readback=None)), "maps none"),
    (_read(a=lambda o: o["steps"].__setitem__(0, {"type": "navigate", "url": "${target}"})), "https with a literal"),
    (_read(a=lambda o: o["steps"].__setitem__(0, {"type": "navigate", "url": "https://${acts_as_id}.social.example/"})),
     "no param decides a host"),
    (_read(a=lambda o: o["steps"].__setitem__(0, {"type": "navigate", "url": "https://evil.example/u/x"})),
     "outside the connector's domain"),
    (_read(a=lambda o: o["steps"].__setitem__(0, {"type": "navigate", "url": "http://social.example/"})),
     "https with a literal host"),
    (_read(a=lambda o: o["steps"][2]["locator"].update(nth=0, within=None)), "nth without an anchor"),
    (_read(a=lambda o: o["steps"].append({"type": "for_each", "over": "nothing", "as": "item", "max_items": 3,
                                          "steps": [{"type": "pause", "min_ms": 0, "max_ms": 0}]})),
     "not saved by an earlier step"),
    (_read(a=lambda o: o["steps"].append({"type": "for_each", "over": "posts", "as": "item", "max_items": 3,
                                          "steps": [{"type": "scroll_to", "locator": {"role": "main"}},
                                                    {"type": "click", "publishes": False,
                                                     "locator": {"role": "button", "within": {"role": "main"}}}]})),
     "no input steps inside for_each"),
    (_read(a=lambda o: o["steps"].append({"type": "extract", "save_as": "items", "max_items": 3,
                                          "items": {"role": "article", "within": {"role": "main"}},
                                          "fields": {"body": {"read": "text"}}})), "a pattern, a format or max_chars"),
    (_read(a=lambda o: o["steps"].append({"type": "extract", "save_as": "items", "max_items": 3,
                                          "items": {"role": "article", "within": {"role": "main"}},
                                          "fields": {"link": {"attr": "href", "format": "uri",
                                                              "host": "evil.example"}}})), "outside social.example"),
    (_read(a=lambda o: o["steps"][1]["locator"].update(attr={"name": "data-x", "equals": "${nope}"}, name=None)),
     "${nope} is not a declared param"),
    (_read(a=lambda o: o["steps"].append({"type": "wait_for", "locator": {"role": "region", "name": "${missing}"}})),
     "is not a declared param"),
    (connector(site="nowhere", connector="nowhere"), "not a site openajar knows"),
    (connector(domain="elsewhere.example"), "not one of the social hosts"),
])
def test_strict_lint_read_rules(doc, needle):
    _one(_errors(doc), needle)


def test_missing_commit_request_is_a_warning_until_the_commit_gate():
    cc = _compile(_commit(commit__request=None))
    assert any("commit gate request block" in w.message for w in cc.warnings)
    _one(_errors(_commit(commit__request=None), commit_gate=True), "commit gate request block")


def test_fixtures_are_required_per_operation_and_language(tmp_path):
    d = write_dir(tmp_path, connector(), fixtures=False)
    with pytest.raises(ConnectorInvalid) as e:
        compile_path(d, sites=SITES)
    msgs = [i.message for i in e.value.issues if i.level == "error"]
    assert any("no fixture for account language en" in m for m in msgs)
    assert any("no fixture for account language lt" in m for m in msgs)
    d = write_dir(tmp_path / "ok", connector())
    cc = compile_path(d, sites=SITES)
    assert cc.root == d and not any("fixtures" in w.where for w in cc.warnings)


def test_compile_reports_every_error_at_once():
    op = read_op()
    op["input"].pop("additionalProperties")
    op["output"].pop("additionalProperties")
    errors = _errors(connector({"profile_summary": op}))
    assert len(errors) >= 2


def test_load_errors_are_clear(tmp_path):
    with pytest.raises(ConnectorInvalid, match="not found"):
        compile_path(tmp_path, sites=SITES)
    (tmp_path / "connector.json").write_text("{nope")
    with pytest.raises(ConnectorInvalid, match="not valid JSON"):
        compile_path(tmp_path, sites=SITES)


def test_the_default_site_table_is_openajar_node_sites():
    doc = connector(connector="reddit", site="reddit", domain="reddit.com")
    doc["operations"]["profile_summary"]["steps"][0]["url"] = "https://www.reddit.com/user/${acts_as_id}"
    out = doc["operations"]["profile_summary"]["output"]["properties"]["profile_url"]
    out["x-agentos-host"] = "reddit.com"
    cc = compile_connector(doc)
    assert cc.operations["profile_summary"].flow_id == "connector.reddit.profile_summary"
