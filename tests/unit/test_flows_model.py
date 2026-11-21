"""Flow documents: validation, immutable versions, params, lint, catalog, tool names."""

import pytest

from openajar.core.errors import InvalidRequest, NotFound
from openajar.core.tools import TOOL_NAME_RE
from openajar.runner.fakes import Harness, post_flow
from openajar.runner.flows import lint, tool_name_for
from openajar.runner.models import FlowDefinition, iter_steps, step_at


def _flow(**kw):
    base = {"id": "biz-a.site.read", "lane": "biz-a/site", "steps": [{"type": "navigate", "url": "https://x.test/"}]}
    base.update(kw)
    return base


def test_versions_are_content_hashes_and_immutable(tmp_path):
    h = Harness(tmp_path)
    v1, created = h.flows.save(post_flow())
    assert created and v1.version.startswith("sha256:")
    again, created = h.flows.save(post_flow())
    assert not created and again.version == v1.version

    changed = post_flow(title="Post a photo, v2")
    v2, created = h.flows.save(changed)
    assert created and v2.version != v1.version
    assert h.flows.get(v1.id).version == v2.version          # head moved
    assert h.flows.get(v1.id, v1.version).definition.title == "Post a photo"  # old one still there
    assert [v.version for v in h.flows.versions(v1.id)] == [v1.version, v2.version]

    _, created = h.flows.save(post_flow())                 # same content as v1: re-point the head
    assert not created and h.flows.get(v1.id).version == v1.version
    with pytest.raises(NotFound):
        h.flows.get("biz-a.nothing.here")


@pytest.mark.parametrize("locator", [
    {"ref": "e12"},
    {"role": "button", "name": "Like", "ref": "e3"},
    {"css": "[ref=e5]"},
    {"css": "aria-ref=e5"},
])
def test_snapshot_refs_are_rejected(tmp_path, locator):
    h = Harness(tmp_path)
    flow = _flow(steps=[{"type": "click", "locator": locator}])
    with pytest.raises(InvalidRequest, match="snapshot refs"):
        h.flows.save(flow)


def test_step_with_ref_key_is_rejected(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(InvalidRequest, match="snapshot refs"):
        h.flows.save(_flow(steps=[{"type": "click", "ref": "e7"}]))


def test_locators_need_role_or_css_and_valid_roles(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(InvalidRequest, match="role"):
        h.flows.save(_flow(steps=[{"type": "click", "locator": {"name": "Like"}}]))
    with pytest.raises(InvalidRequest, match="ARIA role"):
        h.flows.save(_flow(steps=[{"type": "click", "locator": {"role": "Button!"}}]))


def test_templates_must_reference_declared_params(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(InvalidRequest, match="not a declared param"):
        h.flows.save(_flow(steps=[{"type": "navigate", "url": "https://x.test/${missing}"}]))
    with pytest.raises(InvalidRequest, match="not declared"):
        h.flows.save(_flow(steps=[{"type": "fill", "locator": {"role": "textbox"}, "param": "caption"}]))
    ok = _flow(params={"properties": {"q": {"type": "string"}}},
               steps=[{"type": "navigate", "url": "https://x.test/s?q=${q}"}])
    h.flows.save(ok)


def test_writing_steps_must_declare_a_readback(tmp_path):
    h = Harness(tmp_path)
    click = {"type": "click", "locator": {"role": "button", "name": "Share"}, "writes": True}
    with pytest.raises(InvalidRequest, match="must declare a readback"):
        h.flows.save(_flow(steps=[click]))
    h.flows.save(_flow(steps=[click, {"type": "readback", "locator": {"role": "status"},
                                      "condition": {"kind": "exists"}}]))
    inline = {**click, "readback": {"condition": {"kind": "attribute", "name": "aria-pressed", "equals": "true"}}}
    h.flows.save(_flow(steps=[inline]))


def test_conditions_validate(tmp_path):
    h = Harness(tmp_path)
    bad = [
        {"kind": "attribute", "equals": "x"},                    # no attribute name
        {"kind": "count", "changed_by": 1},                       # no baseline
        {"kind": "text", "equals": "a", "contains": "b"},         # two operators
        {"kind": "exists", "equals": "x"},
    ]
    for cond in bad:
        with pytest.raises(InvalidRequest):
            h.flows.save(_flow(steps=[{"type": "readback", "locator": {"role": "status"}, "condition": cond}]))
    with pytest.raises(InvalidRequest, match="never saved"):
        h.flows.save(_flow(steps=[{"type": "readback", "locator": {"role": "article"},
                                   "condition": {"kind": "count", "changed_by": 1, "baseline": "before"}}]))


def test_effect_lane_and_param_names(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(InvalidRequest, match="effect"):
        h.flows.save(_flow(effect="exec"))
    with pytest.raises(InvalidRequest, match="lane"):
        h.flows.save(_flow(lane="Biz A/site"))
    with pytest.raises(InvalidRequest, match="reserved"):
        h.flows.save(_flow(params={"properties": {"on": {"type": "string"}}}))
    with pytest.raises(InvalidRequest, match="flow id"):
        h.flows.save(_flow(id="nodots"))
    with pytest.raises(InvalidRequest, match="JSON schema"):
        h.flows.save(_flow(params={"type": "object", "properties": {"x": {"type": "strnig"}}}))


def test_lane_patterns_resolve_with_on(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(_flow(id="any.site.read", lane="*/site"))
    with pytest.raises(InvalidRequest, match="pass `on`"):
        h.flows.resolve_lane(fv, None)
    assert h.flows.resolve_lane(fv, "biz-b/site") == "biz-b/site"
    with pytest.raises(InvalidRequest, match="does not match"):
        h.flows.resolve_lane(fv, "biz-b/other")
    fixed = h.save(_flow())
    assert h.flows.resolve_lane(fixed, None) == "biz-a/site"
    with pytest.raises(InvalidRequest, match="bound to lane"):
        h.flows.resolve_lane(fixed, "biz-b/site")


def test_params_are_validated_with_defaults(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    with pytest.raises(InvalidRequest, match="caption"):
        h.flows.validate_params(fv, {"image": "a.jpg"})
    with pytest.raises(InvalidRequest, match="caption"):
        h.flows.validate_params(fv, {"image": "a.jpg", "caption": 42})
    with pytest.raises(InvalidRequest, match="Additional properties"):
        h.flows.validate_params(fv, {"image": "a.jpg", "caption": "x", "extra": 1})
    with pytest.raises(InvalidRequest, match="KB"):
        h.flows.validate_params(fv, {"image": "a.jpg", "caption": "x" * 300_000})
    with_default = h.save(_flow(id="biz-a.site.search", params={"properties": {"q": {"type": "string", "default": "news"}}}))
    assert h.flows.validate_params(with_default, {}) == {"q": "news"}


def test_lint_warns_about_unprovable_flows(tmp_path):
    h = Harness(tmp_path)
    defn = FlowDefinition.model_validate(_flow(effect="commit", steps=[
        {"type": "navigate", "url": "https://x.test/"},
        {"type": "click", "locator": {"css": "#post"}},
        {"type": "click", "locator": {"role": "button", "nth": 2}},
    ]))
    warnings = " | ".join(lint(defn))
    assert "declares no readback" in warnings
    assert "last action" in warnings
    assert "css-only" in warnings
    assert "nth without an anchor" in warnings
    assert lint(FlowDefinition.model_validate(post_flow())) == []
    assert h.flows.save(post_flow())


def test_catalog_is_lazy_by_namespace(tmp_path):
    h = Harness(tmp_path)
    for fid in ("biz-a.instagram.post", "biz-a.instagram.story", "biz-a.reddit.post", "biz-b.blog.publish"):
        ws = fid.split(".")[0]
        h.save(_flow(id=fid, lane=f"{ws}/site"))
    top = h.flows.catalog(None, limit=2)
    assert top["total"] == 4 and top["flows"] == [] and "hint" in top
    assert {n["prefix"]: n["flows"] for n in top["namespaces"]} == {"biz-a": 3, "biz-b": 1}
    biz_a = h.flows.catalog("biz-a")
    assert {n["prefix"] for n in biz_a["namespaces"]} == {"biz-a.instagram", "biz-a.reddit"}
    assert {f["id"] for f in biz_a["flows"]} == {"biz-a.instagram.post", "biz-a.instagram.story", "biz-a.reddit.post"}
    ig = h.flows.catalog("biz-a.instagram")
    assert all(f["tool"].startswith("flow_biz_a_instagram_") for f in ig["flows"])


def test_tool_names_are_mcp_safe():
    assert tool_name_for("biz-a.instagram.post") == "flow_biz_a_instagram_post"
    long_id = "a" * 40 + "." + "b" * 40 + "." + "c" * 40
    name = tool_name_for(long_id)
    assert len(name) <= 64 and TOOL_NAME_RE.match(name)
    assert name != tool_name_for(long_id + "x")


def test_step_paths_cover_branches():
    defn = FlowDefinition.model_validate(_flow(params={"properties": {"skip": {"type": "boolean"}}}, steps=[
        {"type": "navigate", "url": "https://x.test/"},
        {"type": "branch", "if": {"param": "skip", "equals": True},
         "then": [{"type": "pause", "min_ms": 1, "max_ms": 2}],
         "else": [{"type": "click", "locator": {"role": "button", "name": "Go"}},
                  {"type": "branch", "if": {"param": "skip", "is_set": False}, "then": [],
                   "else": [{"type": "press", "key": "Enter"}]}]},
    ]))
    paths = [p for p, _ in iter_steps(defn.steps)]
    assert paths == ["0", "1", "1.then.0", "1.else.0", "1.else.1", "1.else.1.else.0"]
    assert step_at(defn.steps, "1.else.1.else.0").type == "press"
    dumped = defn.canonical()
    assert "if" in dumped["steps"][1] and "else" in dumped["steps"][1]
    assert FlowDefinition.model_validate(dumped).content_hash() == defn.content_hash()
