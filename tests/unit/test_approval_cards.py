"""Approval cards that show the diff (agentos.policy.cards): the post as it will appear, inline edit
bound to the edited content and used by the run, "always allow this shape", and the form a
browser_act submit sends."""

import hashlib
import json

import pytest
from connector_samples import POST_PARAMS as PARAMS
from test_connector_approvals import LANE, PHOTO, _harness, _no_pace, _policy, _post

from agentos.api.bridge import ApprovalsAdapter
from agentos.core.config import NodeConfig
from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Principal, RunStatus
from agentos.core.runtime import Runtime
from agentos.node.files import NodeFiles
from agentos.policy import cards

OWNER = Principal(kind="owner", id="owner")


async def _asked(tmp_path):
    h, _inbox = _harness(tmp_path, ops={"post_photo": _no_pace()})
    svc, tok = _policy(h, tmp_path)
    h.runtime.register("approvals", svc.approvals)
    h.runtime.register("traces", svc.traces)

    async def post(caption=PARAMS["caption"]):
        return await _post(h)(svc.context_for_token(tok, tool="social_post_photo"), on=LANE, caption=caption,
                              image="photo.jpg", wait_seconds=0)

    first = await post()
    assert first["status"] == "waiting_approval"
    return h, svc, post, first


async def test_a_connector_post_card_shows_the_text_the_image_and_where(tmp_path):
    _h, svc, _, first = await _asked(tmp_path)
    ap = svc.approvals.get(first["approval_id"])
    card = ap.card
    assert card["type"] == "post" and card["operation"] == "social.post_photo"
    assert card["text"] == {"param": "caption", "value": PARAMS["caption"]}
    assert card["line"] == "as biz-a on Social, through biz-a/social"
    assert card["surface"] == LANE and card["through"] == LANE and card["editable"] == ["caption"]
    image = next(r for r in card["params"] if r["name"] == "image")
    assert image["file"] and image["match"] == "sha256:" + hashlib.sha256(PHOTO).hexdigest()
    public = ApprovalsAdapter(svc.approvals).get(ap.id)
    assert public["card"]["type"] == "post" and public["edit"] is None
    assert ap.content is not None and "content" not in public  # kept for the edit, never sent out
    assert "—" not in json.dumps(public)


async def test_approve_with_an_edit_binds_the_edited_caption_and_the_run_posts_it(tmp_path):
    h, svc, _, first = await _asked(tmp_path)
    before = svc.approvals.get(first["approval_id"])
    with pytest.raises(InvalidRequest, match="only caption"):
        svc.approvals.decide(before.id, "approve", by="owner", edit={"image": "other.jpg"})
    with pytest.raises(InvalidRequest, match="only an approval"):
        svc.approvals.decide(before.id, "deny", by="owner", edit={"caption": "x"})
    ap = ApprovalsAdapter(svc.approvals).decide(before.id, decision="approve", principal=OWNER,
                                                edit={"caption": "Autumn at the lake, edited"})
    assert ap["state"] == "approved" and ap["edit"]["params"] == ["caption"] and ap["edit"]["by"] == "owner:owner"
    assert "approved with an edit by owner:owner: caption" in ap["summary"]
    assert ap["card"]["text"] == {"param": "caption", "value": "Autumn at the lake, edited", "edited": True}
    stored = svc.approvals.get(before.id)
    assert stored.content_digest != before.content_digest and stored.content_hash == before.content_hash
    # the agent's retry of the old content never consumes an edited approval
    assert svc.approvals.consume_approved(before.content_hash, before.grant_id) is None

    assert await h.runner.on_approval(before.id, "approved") == 1
    await h.runner.run_until_idle()
    run = h.runner.get(first["id"])
    assert run.status == RunStatus.SUCCEEDED_VERIFIED and run.params["caption"] == "Autumn at the lake, edited"
    assert [p["caption"] for p in h.site.state["posts"]] == ["Autumn at the lake, edited"]
    edits = svc.traces.query(kind="approval_edit", limit=5)[0]
    assert edits and "approved with an edit by owner:owner: caption" in edits[0].summary


async def test_an_edit_that_no_longer_matches_fails_the_run(tmp_path):
    h, svc, _, first = await _asked(tmp_path)
    svc.approvals.decide(first["approval_id"], "approve", by="owner", edit={"caption": "Edited"})
    run = h.runner.get(first["id"])
    run.params = {**run.params, "caption": "tampered"}  # what the run would post is not what was approved
    h.runner.store.save_run(run)
    h.runner.surfaces.digests = lambda fv, target: {"image": "sha256:" + "0" * 64}
    assert await h.runner.on_approval(first["approval_id"], "approved") == 1
    failed = h.runner.get(first["id"])
    assert failed.status == RunStatus.FAILED and failed.error["code"] == "edit_mismatch"
    assert h.site.state["posts"] == []


async def test_always_allow_this_shape_covers_other_captions_on_that_surface_only(tmp_path):
    _h, svc, post, first = await _asked(tmp_path)
    svc.approvals.decide(first["approval_id"], "approve", by="owner")
    adapter = ApprovalsAdapter(svc.approvals)
    with pytest.raises(InvalidRequest, match=r"is for biz-a/social, not biz-b/social"):
        adapter.create_standing(lane="biz-b/social", effect="commit", ttl_seconds=86_400, principal=OWNER,
                                from_approvals=[first["approval_id"]], shape=True)
    sa = adapter.create_standing(lane=LANE, effect="commit", ttl_seconds=86_400, principal=OWNER,
                                 from_approvals=[first["approval_id"]], shape=True)
    assert sa["shape"] and sa["targets"][0].startswith("connector:social.post_photo@")
    assert (await post("A different caption"))["status"] == "queued"
    with pytest.raises(InvalidRequest, match="names flow versions or connector operations"):
        svc.approvals.add_standing(effects=["commit"], lanes=[LANE], until=svc.clock() + 60, by="owner",
                                   targets=["tool:browser_act"], shape=True)
    with pytest.raises(InvalidRequest, match="cannot cover pay"):
        svc.approvals.add_standing(effects=["pay"], lanes=[LANE], until=svc.clock() + 60, by="owner",
                                   targets=sa["targets"], shape=True)


def test_a_form_card_lists_the_fields_and_cannot_be_edited():
    content = {"ref": "e9", "action": "click", "text": None, "form": {
        "action": "shop.test/checkout", "method": "post",
        "fields": [{"label": "Street", "role": "textbox", "name": "street", "changed": True,
                    "value": "22 New Street", "was": "1 Old Road"},
                   {"label": "Password", "role": "textbox", "name": "pw", "changed": True, "value": None,
                    "hidden": "a password or code field: its value is never shown"}]}}
    card = cards.build_card("browser_act", "biz-a/shop", content)
    assert card["type"] == "form" and card["changed"] == 2 and card["editable"] == []
    assert "cannot be edited" in card["edit_note"]
    assert cards.build_card("browser_act", "biz-a/shop", {"ref": "e1", "action": "click", "text": None}) is None
    with pytest.raises(InvalidRequest, match="cannot be edited"):
        cards.edited_content(content, card, {"text": "x"})


def test_a_card_image_is_read_only_while_its_bytes_match(tmp_path):
    config = NodeConfig(home=tmp_path)
    config.lane_dir("biz-a/social").mkdir(parents=True)
    files = NodeFiles(config)
    runtime = Runtime()
    runtime.register("files", files)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    stored = files.lane("biz-a/social").put_bytes("photo.png", png)
    digest = "sha256:" + hashlib.sha256(png).hexdigest()
    content = {"operation": "social.post_photo", "contract": "c1", "surface": "biz-a/social",
               "acts_as": {"kind": "account", "id": "biz-a"}, "params": {"caption": "Hi", "image": digest}}
    card = cards.build_card("social_post_photo", "biz-a/social", content, runtime=runtime)
    row = next(r for r in card["params"] if r["name"] == "image")
    assert row["image"] and row["file_name"] == "photo.png" and card["images"][0]["name"] == "image"
    data, mime = cards.read_image(runtime, card, "image")
    assert data == png and mime == "image/png"
    with pytest.raises(NotFound):
        cards.read_image(runtime, card, "caption")
    bad = {**card, "params": [{**row, "match": "sha256:" + "1" * 64}]}
    with pytest.raises(NotFound):
        cards.read_image(runtime, bad, "image")
    assert stored.id.startswith("in_")


async def test_a_form_approval_never_carries_a_vault_value_to_the_panel_a_push_or_the_controller(tmp_path):
    """A username the vault typed into a text field used to reach the preview, the card and the
    controller's approval_request. The card marks the field instead, so the owner sees it changed."""
    from test_policy_support import make_service
    from test_vault_support import PASSWORD, USERNAME, make_vault, private_home

    from agentos.core.errors import ApprovalPending
    from agentos.core.models import Effect
    from agentos.node.channel import bridge
    from agentos.vault.scrub import runtime_scrub

    svc, rt = make_service(tmp_path / "policy")
    vault = make_vault(private_home(tmp_path))
    await vault.add_entry(name="shop", kind="password", domain="shop.example.com", lanes=["biz-a/shop"],
                          username=USERNAME, secret=PASSWORD)
    rt.register("vault", vault)
    content = {"ref": "e9", "action": "click", "text": None, "form": {
        "action": "shop.test/login", "method": "post",
        "fields": [{"label": "Email", "role": "textbox", "name": "email", "changed": True,
                    "value": USERNAME, "was": ""},
                   {"label": "Note", "role": "textbox", "name": "note", "changed": False,
                    "value": "hello", "was": "hello"}]}}
    _g, tok = svc.issue(subject={"id": "agent"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    ctx = svc.context(svc.resolve(tok)[1], tool="browser_act")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/shop", content=content,
                            summary=f"click e9 to log in as {USERNAME}")
    ap = svc.approvals.get(e.value.approval_id)
    public = ap.public()
    payload = bridge.approval_payload(public, runtime_scrub(rt))
    for shown in (json.dumps(public), json.dumps(payload), ap.preview, ap.summary):
        assert USERNAME.lower() not in shown.lower()
    email, note = ap.card["fields"]
    assert email["changed"] and email["vault"] and email["value"] == "[redacted]" and "vault" in email["hidden"]
    assert note["value"] == "hello" and "vault" not in note
    # an approval stored before this scrub is sent clean too
    old = {**public, "preview": {"text": f"value: {USERNAME}"}}
    assert USERNAME not in json.dumps(bridge.approval_payload(old, runtime_scrub(rt)))
    svc.close()


def test_a_read_only_param_is_shown_but_never_editable():
    """A profile edit's value comes from the engagement program: the card approves it as it is."""
    from pathlib import Path
    from types import SimpleNamespace

    from agentos.runner.connectors.compile import compile_path

    root = Path(__file__).resolve().parents[2] / "connectors" / "linkedin"
    flow = compile_path(root).operations["profile_headline"].flow
    fv = SimpleNamespace(definition=flow)
    rows = cards._params(None, LANE, {"headline": "Analyst of engines"}, fv, files_by_digest=False)
    assert rows == [{"name": "headline", "value": "Analyst of engines", "editable": False}]
    assert cards._post(rows)["editable"] == []
    prop = dict(flow.params["properties"]["headline"])
    assert cards._editable("headline", "x", {**prop, "readOnly": False}) is True
    with pytest.raises(InvalidRequest, match="cannot be edited"):
        cards.edited_content({"params": {"headline": "x"}}, cards._post(rows), {"headline": "y"})


def test_a_flow_cards_upload_param_is_a_file_and_the_approval_binds_its_bytes(tmp_path):
    """QA-2 #14: a flow's `image` param (an upload step's, with no `format: agentos-file` in its schema)
    was marked editable on the card, and the approval bound only its name, so another file saved under
    that name would have gone out. It is a file row now, never editable, and the approval binds its sha256."""
    from agentos.runner.fakes import Harness, post_flow

    lane = "biz-a/social"
    config = NodeConfig(home=tmp_path / "node")
    config.lane_dir(lane).mkdir(parents=True)
    files = NodeFiles(config)
    h = Harness(tmp_path / "runner", services={"files": files, "config": config})
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    stored = files.lane(lane).put_bytes("photo.png", png)
    fv = h.save(post_flow())
    params = {"caption": "Autumn at the lake", "image": stored.id}  # what files_put returns
    content = h.flows.approval_content(fv, lane, params)
    assert content["params"]["image"] == "sha256:" + hashlib.sha256(png).hexdigest()
    card = cards.build_card("flow_run", lane, content, runtime=h.runtime)
    image = next(r for r in card["params"] if r["name"] == "image")
    assert image["file"] and image["file_name"] == "photo.png" and not image.get("editable"), image
    assert card["editable"] == ["caption"] and card["images"][0]["name"] == "image"
    with pytest.raises(InvalidRequest, match="only caption"):
        cards.edited_content(content, card, {"image": "other.png"})
    # other bytes under the same name are another approval
    path = files.resolve(lane, stored.id)
    path.write_bytes(png + b"\x01")
    assert h.flows.approval_content(fv, lane, params) != content
