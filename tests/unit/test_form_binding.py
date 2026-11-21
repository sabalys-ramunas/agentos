"""A form approval binds what the click sends: browser_act reads the form again inside its own lane
session, right before the click, and refuses (form_changed) when the page changed what it sends
after the read the approval bound (AUD-10). A re-render that keeps the values goes through."""

from __future__ import annotations

import copy

import pytest
from test_handback import LANE, Ctx, FormPage, SnapInteraction, field
from test_lanes import make

from openajar.core.errors import FormChanged, RefNotFound
from openajar.core.models import ActionKind, ActionResult, LaneSpec, Signal
from openajar.core.runtime import Runtime
from openajar.node import fields as fields_mod
from openajar.node import human
from openajar.node import tools as node_tools
from openajar.node.tools.form_check import _form_change, form_sent

FORM = {"action": "https://shop.test/account", "method": "post", "fields": [
    field("input|text|street|0", "text", "Street", "s2", value="22 New Street", was="1 Old Road", dirty=True),
    field("input|text|city|0", "text", "City", "c1", value="Vilnius", was="Vilnius"),
    field("input|password|pw|0", "secret", "Password", "p1"),
    field("input|checkbox|news|0", "check", "Newsletter", "off", role="checkbox", checked=False),
]}


class Interaction(SnapInteraction):
    def __init__(self):
        self.acts: list[tuple[str, ActionKind]] = []

    async def act(self, page, ref, action, text=None):
        self.acts.append((ref, action))
        return ActionResult(ok=True, action=action, ref=ref, url=page.url, signal=Signal.OK)


class ApprovingCtx(Ctx):
    """Approves at once, and keeps what the approval bound."""

    def __init__(self, runtime):
        super().__init__(runtime)
        self.bound: list[dict] = []

    async def authorize(self, effect, *, lane_id=None, summary=None, content=None, **kw):
        self.bound.append(copy.deepcopy(content))


class Target:
    """The submit button. Each read of its form gets the next answer; the last one repeats."""

    def __init__(self, *forms):
        self.forms, self.reads = list(forms), 0

    async def evaluate(self, js, arg):
        assert js == fields_mod.FORM_DIFF_JS
        form = self.forms[min(self.reads, len(self.forms) - 1)]
        self.reads += 1
        return copy.deepcopy(form)


@pytest.fixture
async def lane(tmp_path):
    mgr, engine, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id=LANE))
    page = FormPage()
    async with mgr.using(LANE) as p:
        assert p is not None
    engine.sessions[-1]._page = page
    inter = Interaction()
    runtime = Runtime(interaction=inter)
    runtime.register("lanes", mgr)
    yield page, runtime, inter
    await mgr.shutdown()


def changed(form, label, **update):
    out = copy.deepcopy(form)
    for row in out["fields"]:
        if row["label"] == label:
            row.update(update)
    return out


async def click(page, runtime, *forms):
    target = Target(*forms)
    page.locator = lambda selector: target
    ctx = ApprovingCtx(runtime)
    result = await node_tools.browser_act(ctx, on=LANE, ref="e9", action=ActionKind.CLICK)
    return ctx, target, result


async def test_a_field_changed_between_the_approval_and_the_click_is_refused(lane):
    page, runtime, inter = lane
    with pytest.raises(FormChanged) as err:
        await click(page, runtime, FORM, changed(FORM, "Street", value="1 Attacker Way", digest="s3"))
    assert inter.acts == []  # nothing was clicked
    assert err.value.code == "form_changed" and '"Street"' in err.value.message
    assert "Attacker" not in err.value.message and "New Street" not in err.value.message  # labels, never values


@pytest.mark.parametrize("now, why", [
    (changed(FORM, "Newsletter", checked=True, digest="on"), "fields changed"),
    ({**FORM, "action": "https://evil.test/collect"}, "somewhere else"),
    ({**FORM, "fields": [*FORM["fields"], field("input|text|bcc|0", "text", "Bcc", "x", value="a@evil.test")]},
     '"Bcc"'),
    (None, "could not be read again"),
])
async def test_what_the_form_sends_or_where_it_goes_changing_is_refused(lane, now, why):
    page, runtime, inter = lane
    with pytest.raises(FormChanged) as err:
        await click(page, runtime, FORM, now)
    assert why in err.value.message and inter.acts == []


async def test_a_rerender_that_keeps_every_value_is_allowed(lane):
    page, runtime, inter = lane
    rerendered = copy.deepcopy(FORM)
    rerendered["fields"].reverse()  # the page rebuilt the form: same fields, new order
    rerendered["fields"][0]["key"] = "input|checkbox|news|1"  # a new node for the same field
    ctx, target, result = await click(page, runtime, FORM, rerendered)
    assert inter.acts == [("e9", ActionKind.CLICK)] and result["ok"] is True
    assert target.reads == 2 and ctx.bound[0]["form"]["action"] == "https://shop.test/account"


async def test_what_the_page_loaded_with_may_change_when_what_is_sent_does_not(lane):
    page, runtime, inter = lane
    reset = changed(FORM, "Street", was="22 New Street", dirty=False)  # the page reset its defaults
    await click(page, runtime, FORM, reset)
    assert inter.acts == [("e9", ActionKind.CLICK)]


async def test_a_click_that_submits_no_form_is_not_read_twice(lane):
    page, runtime, inter = lane
    _ctx, target, _result = await click(page, runtime, None)
    assert inter.acts == [("e9", ActionKind.CLICK)] and target.reads == 1


def test_form_sent_ignores_what_was_loaded_and_the_order_of_fields():
    shown = fields_mod.form_diff(FORM)
    again = fields_mod.form_diff({**FORM, "fields": list(reversed(FORM["fields"]))})
    assert form_sent(shown) == form_sent(again) and _form_change(shown, again) is None
    assert all("was" not in row and "changed" not in row for row in form_sent(shown)["fields"])


# ---- what the binding compares: the whole raw value, through a keyed digest ----

def with_raw(form, **raws):
    """FORM with each field's whole raw value (FORM_DIFF_JS's `raw`), by label."""
    out = copy.deepcopy(form)
    out["raw"] = '["https://shop.test/account","post","",""]'
    for row in out["fields"]:
        row["raw"] = raws.get(row["label"], f'["{row.get("value", "")}"]')
    return out


LONG = "x" * 200
SENT = with_raw(FORM, Street=f'["22 New Street {LONG}"]', Password='["hunter2-old"]')


@pytest.mark.parametrize("label, now", [
    ("Street", f'["22 New Street {LONG}!"]'),         # past the 120 characters the card shows
    ("Password", '["hunter2-new"]'),                   # a password field, by its value, not its presence
])
async def test_a_value_the_card_cannot_show_changing_is_refused(lane, label, now):
    page, runtime, inter = lane
    after = copy.deepcopy(SENT)
    next(r for r in after["fields"] if r["label"] == label)["raw"] = now
    with pytest.raises(FormChanged) as err:
        await click(page, runtime, SENT, after)
    assert f'"{label}"' in err.value.message and inter.acts == []


async def test_a_card_number_swapped_for_one_that_masks_the_same_is_refused(lane):
    page, runtime, inter = lane
    card = lambda number: with_raw({**FORM, "fields": [
        field("input|text|cardno|0", "text", "Card", "c", value=number, dirty=True)]}, Card=f'["{number}"]')
    shown = fields_mod.form_diff(card("4111 1111 1111 1111"))
    assert shown["fields"][0]["value"] == fields_mod.form_diff(card("5555 5555 5555 4444"))["fields"][0]["value"]
    with pytest.raises(FormChanged):
        await click(page, runtime, card("4111 1111 1111 1111"), card("5555 5555 5555 4444"))
    assert inter.acts == []


async def test_a_hidden_field_or_the_forms_query_changing_is_refused(lane):
    page, runtime, inter = lane
    hidden = {**FORM, "fields": [*FORM["fields"], field("input|hidden|to|0", "hidden", "to", "h")]}
    with pytest.raises(FormChanged):
        await click(page, runtime, with_raw(hidden, to='["acct-1"]'), with_raw(hidden, to='["acct-2"]'))
    query = with_raw(FORM)
    query["raw"] = '["https://shop.test/account?to=evil","post","",""]'
    with pytest.raises(FormChanged, match="somewhere else"):
        await click(page, runtime, with_raw(FORM), query)
    assert inter.acts == []


def test_the_binding_digest_is_keyed_and_the_raw_value_never_kept():
    shown = fields_mod.form_diff(SENT)
    pw = next(r for r in shown["fields"] if r["label"] == "Password")
    assert pw["value"] is None and pw["bind"] and "raw" not in pw
    assert "hunter2" not in repr(shown) and LONG not in repr(shown)
    import hashlib
    assert pw["bind"] not in hashlib.sha256(b'["hunter2-old"]').hexdigest()  # keyed: not guessable off the node


async def test_a_form_that_cannot_be_read_for_the_approval_is_refused_not_run_unbound(lane):
    page, runtime, inter = lane

    class Broken:
        async def evaluate(self, js, arg):
            raise RuntimeError("Execution context was destroyed")

    page.locator = lambda selector: Broken()
    ctx = ApprovingCtx(runtime)
    with pytest.raises(FormChanged, match="could not be read for the approval"):
        await node_tools.browser_act(ctx, on=LANE, ref="e9", action=ActionKind.CLICK)
    assert ctx.bound == [] and inter.acts == []  # never asked for, never clicked


async def test_the_form_is_checked_again_right_before_the_press(lane, monkeypatch):
    """After the first re-check, the click still scrolls, pauses and glides (up to 1.4 s and more):
    a change made then is caught by the check human.click runs just before the mouse goes down."""
    page, runtime, _inter = lane
    pressed = []

    class Pressing(Interaction):
        async def act(self, page, ref, action, text=None):
            await human.click(page, None)
            return await super().act(page, ref, action, text)

    async def nothing(*a, **k):
        return None

    async def press(_page):
        pressed.append(1)

    for name in ("scroll_into_view", "_pause", "_glide_onto"):
        monkeypatch.setattr(human, name, nothing)
    monkeypatch.setattr(human, "_press_release", press)
    runtime.register("interaction", Pressing())
    late = changed(SENT, "Street", raw='["1 Attacker Way"]')
    with pytest.raises(FormChanged):
        await click(page, runtime, SENT, SENT, late)  # approval read, first check, at the press
    assert pressed == []
    _ctx, target, _ = await click(page, runtime, SENT)  # unchanged: the press happens
    assert pressed == [1] and target.reads == 3


class GoneTarget:
    """A ref from an older snapshot: its form cannot be read, and its locator matches `matches` elements."""

    def __init__(self, matches):
        self.matches = matches

    async def evaluate(self, js, arg):
        raise TimeoutError("waiting for locator('aria-ref=e551')")

    async def count(self):
        return self.matches


@pytest.mark.parametrize("matches, error", [(0, RefNotFound), (1, FormChanged)])
async def test_a_stale_ref_says_ref_not_found_not_form_changed(lane, matches, error):
    """2026-11-10 re-test: a ref whose element left the page said form_changed; it must say take a new snapshot."""
    page, runtime, inter = lane
    page.locator = lambda selector: GoneTarget(matches)
    with pytest.raises(error) as err:
        await node_tools.browser_act(ApprovingCtx(runtime), on=LANE, ref="e551", action=ActionKind.CLICK)
    assert inter.acts == []  # nothing was clicked either way
    if error is RefNotFound:
        assert err.value.code == "ref_not_found" and "new snapshot" in err.value.message


def test_the_field_a_select_or_a_typed_newline_writes_is_the_act_not_a_change():
    """A select presses arrow keys and then Enter, and a typed newline comes after its text: each press is
    checked, and the field the act writes has its new value by then. That one field is left out, by the
    name it is sent under; any other field the page changes still stops the press."""
    shown = fields_mod.form_diff({**FORM, "own": "city"})
    picked = fields_mod.form_diff({**changed(FORM, "City", value="Kaunas"), "own": "city"})
    assert shown["own"] == "city"
    assert _form_change(shown, picked, "city") is None
    assert _form_change(shown, picked) == 'fields changed: "City"'
    other = fields_mod.form_diff({**changed(FORM, "Street", value="9 Other Way"), "own": "city"})
    assert _form_change(shown, other, "city") == 'fields changed: "Street"'
    assert "const own = (ownField && ownField.form === form && ownField.name) || ''" in fields_mod.FORM_DIFF_JS


@pytest.mark.parametrize(("action", "skips"), [(ActionKind.SELECT, True), (ActionKind.FILL, True),
                                               (ActionKind.TYPE, True), (ActionKind.CLICK, False),
                                               (ActionKind.PRESS, False)])
async def test_only_acts_that_write_their_own_field_leave_it_out(action, skips):
    class Page:
        def locator(self, _selector):
            return self

        async def evaluate(self, _js, _arg):
            return {**changed(FORM, "City", value="Kaunas"), "own": "city"}

    approved = fields_mod.form_diff({**FORM, "own": "city"})
    if skips:
        await node_tools.check_form_unchanged(Page(), "e9", action, approved, "Poland" if action is ActionKind.SELECT else "x\n")
    else:
        with pytest.raises(FormChanged):
            await node_tools.check_form_unchanged(Page(), "e9", action, approved, "Enter")
