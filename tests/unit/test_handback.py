"""The hand-back note (agentos.node.handback): what changed while a human had a lane.

Fake pages answer the field read the way the page script does (agentos.node.fields); the real page
script runs on Camoufox in tests/node/test_handback_node.py.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from test_lanes import make

from agentos.core.models import Principal, SnapshotResult
from agentos.core.runtime import Runtime
from agentos.node import fields as fields_mod
from agentos.node.control_tools import tool_specs as control_specs
from agentos.node.handback import STALE_REFS, attach_note
from agentos.node.lanes import LaneSpec
from agentos.node.tools import with_handback

LANE = "personal/shop"
OUTLINE = """- textbox "Display name" [ref=e3]
- textbox "Password" [ref=e4]
- checkbox "Subscribe" [ref=e5]
- textbox "Bio" [ref=e6]"""


def field(key, kind, label, digest, *, role="textbox", value=None, dirty=False, **extra):
    out = {"key": key, "kind": kind, "role": role, "label": label, "name": label.lower(), "digest": digest,
           "dirty": dirty, "disabled": False, "in_form": True}
    if value is not None:
        out["value"] = value
    out.update(extra)
    return out


class FormPage:
    """A page whose fields the test changes by hand, the way a human would."""

    def __init__(self):
        self.url = "http://shop.test/account"
        self.doc = "1730700000000.5"
        self.title_text = "Account"
        self.fields = {
            "name": field("input|text|name|0", "text", "Display name", "a1", value="Ana"),
            "pw": field("input|password|pw|0", "secret", "Password", "p1"),
            "sub": field("input|checkbox|sub|0", "check", "Subscribe", "off", role="checkbox", checked=False),
            "bio": field("textarea||bio|0", "text", "Bio", "b1", value=""),
            "csrf": field("input|hidden|csrf|0", "hidden", "csrf", "h1"),
        }

    async def evaluate(self, js, arg=None):
        if js == fields_mod.PAGE_FIELDS_JS:
            return {"doc": self.doc, "fields": list(self.fields.values())}
        return "203.0.113.7"  # the lane manager's self-check

    async def title(self):
        return self.title_text


class SnapInteraction:
    async def snapshot(self, page):
        return SnapshotResult(url=page.url, title=page.title_text, text=OUTLINE, ref_count=4, signal="ok")


class Ctx:
    def __init__(self, runtime, thread="T1"):
        self.runtime = runtime
        self.principal = Principal(id="claude-code", thread=thread)
        self.labels: list[str] = []

    async def authorize(self, *a, **kw):
        pass

    def trace(self, *a, **kw):
        pass

    def taint(self, label, source=None):
        self.labels.append(source)


@pytest.fixture
async def env(tmp_path):
    mgr, engine, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id=LANE))
    page = FormPage()
    async with mgr.using(LANE) as p:  # warm it, then swap in the form page
        assert p is not None
    engine.sessions[-1]._page = page
    mgr.watch(SnapInteraction())
    runtime = Runtime()
    runtime.register("lanes", mgr)
    yield mgr, page, runtime
    await mgr.shutdown()


async def human_session(mgr, page, note=None):
    await mgr.take_control(LANE, by="owner")
    await asyncio.sleep(0)  # the capture runs in the background
    await asyncio.sleep(0.01)
    page.fields["name"].update(digest="a2", value="Ana Lopez")
    page.fields["pw"].update(digest="p2")
    page.fields["sub"].update(digest="on", checked=True)
    page.fields["csrf"].update(digest="h2")
    page.url = "http://shop.test/account#saved"
    page.title_text = "Account saved"
    await mgr.hand_back(LANE, by="owner", note=note)


async def test_the_note_lists_what_the_human_changed_and_never_a_secret(env):
    mgr, page, _runtime = env
    await human_session(mgr, page, note="Fixed the name; the password is hunter2")
    note = await mgr.handback.claim(LANE, "agent-a")
    assert note["url"] == {"before": "http://shop.test/account", "after": "http://shop.test/account#saved",
                           "changed": True}
    assert note["title"] == {"before": "Account", "after": "Account saved"}
    assert note["by"] == "owner" and "handed back by owner" in note["what"]
    assert note["refs"] == STALE_REFS and "stale" in note["refs"]
    by_label = {f["label"]: f for f in note["fields"]}
    assert set(by_label) == {"Display name", "Password", "Subscribe"}  # the hidden csrf field is left out
    assert by_label["Display name"] == {"ref": "e3", "role": "textbox", "label": "Display name",
                                        "value": "Ana Lopez", "typed_by_human": True, "was": "Ana"}
    assert by_label["Password"]["value"] is None and "never shown" in by_label["Password"]["hidden"]
    assert by_label["Subscribe"] == {"ref": "e5", "role": "checkbox", "label": "Subscribe", "checked": True}
    assert note["human_note"].startswith("Fixed the name")
    text = json.dumps(note)
    assert "p2" not in text and "h2" not in text and "—" not in text
    # once per agent session; another session still gets it
    assert await mgr.handback.claim(LANE, "agent-a") is None
    assert (await mgr.handback.claim(LANE, "agent-b"))["url"]["changed"]


async def test_wait_control_carries_the_note_once(env):
    mgr, page, runtime = env
    specs = {s.name: s for s in control_specs(runtime)}
    await human_session(mgr, page)
    ctx = Ctx(runtime)
    out = await specs["browser_wait_handback"].handler(ctx, on=LANE, wait_s=1)
    assert out["agent_may_act"] and out["handback"]["fields"]
    assert "new browser_snapshot" in out["next"] and ctx.labels == ["handback"]
    again = await specs["browser_wait_handback"].handler(ctx, on=LANE, wait_s=1)
    assert "handback" not in again


async def test_the_first_browser_result_carries_it_and_a_failed_call_gives_it_back(env):
    mgr, page, runtime = env
    await human_session(mgr, page)
    calls = []

    async def failing(ctx, *, on):
        calls.append(on)
        raise RuntimeError("engine")

    async def outline(ctx, *, on):
        return '{"on": "x"}\n\nsnapshot: full'

    with pytest.raises(RuntimeError):
        await with_handback(failing)(Ctx(runtime), on=LANE)
    text = await with_handback(outline)(Ctx(runtime), on=LANE)
    head = json.loads(text.split("\n", 1)[0])
    assert head["on"] == "x" and head["handback"]["url"]["changed"]
    assert text.endswith("snapshot: full")
    assert await with_handback(outline)(Ctx(runtime), on=LANE) == '{"on": "x"}\n\nsnapshot: full'


async def test_no_note_while_the_human_still_has_it_and_a_new_take_over_replaces_it(env):
    mgr, _page, _runtime = env
    await mgr.take_control(LANE, by="owner")
    assert not mgr.handback.pending(LANE)
    await mgr.hand_back(LANE, by="owner")
    assert mgr.handback.pending(LANE)
    await mgr.take_control(LANE, by="owner")
    assert not mgr.handback.pending(LANE)  # the next hand back writes a fresh one


async def test_a_cold_lane_gets_a_short_note(tmp_path):
    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id=LANE))
    await mgr.stop_agent(LANE, by="owner")
    await mgr.hand_back(LANE, by="owner", note="Go ahead")
    note = await mgr.handback.claim(LANE, "a")
    assert "no page" in note["page"] and note["human_note"] == "Go ahead"
    assert "agents were resumed by owner" in note["what"] and note["what"].startswith("Agents were held off")
    await mgr.shutdown()


async def test_idle_hand_back_says_so(env):
    mgr, _page, _runtime = env
    await mgr.take_control(LANE, by="owner")
    await mgr.hand_back(LANE, by="idle")
    note = await mgr.handback.claim(LANE, "a")
    assert "without human input" in note["what"]


def test_attach_note_shapes():
    note = {"what": "x"}
    assert attach_note({"ok": True}, note) == {"ok": True, "handback": note}
    bare = attach_note("snapshot: full\nlane: a/b", note)
    assert json.loads(bare.split("\n", 1)[0]) == {"handback": note} and bare.endswith("lane: a/b")


def test_the_field_reader_keeps_secret_values_out():
    rows = fields_mod.parse_fields([
        field("a", "secret", "Code", "x", value="123456"),
        field("b", "hidden", "t", "y", value="tok"),
        field("c", "text", "Card", "z", value="4111 1111 1111 1111"),
        field("d", "text", "Long", "w", value="x" * 500),
    ])
    assert rows[0].value is None and rows[1].value is None
    assert "4111 1111 1111 1111" not in (rows[2].value or "")
    assert len(rows[3].value) <= fields_mod.VALUE_CHARS + 3


async def test_browser_act_binds_the_form_a_submit_sends(env):
    from agentos.core.models import ActionKind
    from agentos.node.tools import form_before_submit

    mgr, page, runtime = env
    asked = {"form": {"action": "shop.test/account", "method": "post", "fields": [
        field("input|text|street|0", "text", "Street", "s2", value="22 New Street", was="1 Old Road", dirty=True),
        field("input|password|pw|0", "secret", "Password", "p2", value="hunter2", dirty=True)]}}

    class Target:
        async def evaluate(self, js, arg):
            assert js == fields_mod.FORM_DIFF_JS and arg[0] == "click"
            return asked["form"]

    page.locator = lambda selector: Target()
    ctx = Ctx(runtime)
    form, ungated = await form_before_submit(ctx, LANE, "e9", ActionKind.CLICK, None)
    assert form["action"] == "shop.test/account" and form["method"] == "post"
    assert ungated is not None  # a POST to a host no effect rule names: Ask me first holds it in browser_act
    street, pw = form["fields"]
    assert street == {"label": "Street", "role": "textbox", "name": "street", "changed": True,
                      "value": "22 New Street", "was": "1 Old Road"}
    assert pw["value"] is None and "hunter2" not in json.dumps(form)
    assert await form_before_submit(ctx, LANE, "e9", ActionKind.TYPE, "x") == (None, None)
    assert await form_before_submit(ctx, LANE, "e9", ActionKind.PRESS, "Tab") == (None, None)
    await mgr.take_control(LANE, by="owner")
    assert await form_before_submit(ctx, LANE, "e9", ActionKind.CLICK, None) == (None, None)  # a human has it
