"""The pilot's loop with a scripted decider and scripted tools: no model, no lane, no network."""

from __future__ import annotations

from typing import Any

import pytest

from agentos.brains.decisions import DecisionRejected
from agentos.core.models import ActionKind, Principal
from agentos.pilot import Limits, Pilot, Request, State, Status, Value
from agentos.pilot.deciders import BLOCKED, BUDGET, UNAVAILABLE, BrainRescue, DeciderFailed
from agentos.pilot.hands import ToolFailed
from agentos.pilot.page import lean, offered, parse_page
from agentos.pilot.questions import build_questions, noul
from agentos.pilot.testing import FakeTools, ScriptedDecider, ScriptedRescue, ScriptedWriter, pick, step

FEED = """signal: ok
http://127.0.0.1:8000/feed · Feed · 6 refs
- main:
  - article "@mira tomatoes":
    - paragraph: First tomatoes of the year
    - button "Like 3" [pressed] [ref=e1] [cursor=pointer]
  - article "@tomas café":
    - button "Like 1" [ref=e2] [cursor=pointer]
    - textbox "Write a comment" [ref=e3]
    - button "Send" [ref=e6] [cursor=pointer]
  - link "Settings" [ref=e5] [cursor=pointer]:
    - /url: /settings
"""
LIKED = FEED.replace('button "Like 1" [ref=e2]', 'button "Like 2" [pressed] [ref=e2]')
OFFSITE = FEED + """  - link "Settings" [ref=e7] [cursor=pointer]:
    - /url: http://settings.socia1-lanebench.test/account
  - button "Save" [disabled] [ref=e9] [cursor=pointer]
"""
DIALOG = FEED + """  - dialog "Cookies":
    - button "Reject" [ref=e20] [cursor=pointer]
"""
CAPTCHA = FEED.replace("signal: ok", "signal: captcha")
TYPED = FEED.replace('textbox "Write a comment" [ref=e3]', 'textbox "Write a comment" [ref=e3]: Nice work')
LOGIN = """signal: ok
http://127.0.0.1:8000/login · Sign in · 3 refs
- main:
  - form "Sign in":
    - textbox "Username" [ref=e1]
    - textbox "Password" [ref=e2]
    - button "Sign in" [ref=e3] [cursor=pointer]
"""
SIGNED_IN = LOGIN.replace("/login", "/home").replace('form "Sign in"', 'form "Welcome"')


def request(goal: str = "Like the tomas post", **kw: Any) -> Request:
    kw.setdefault("sites", ("127.0.0.1",))
    kw.setdefault("seed", "t")
    return Request(lane="w/l", goal=goal, **kw)


async def nap(_: float) -> None:
    return None


def pilot(tools: FakeTools, decider: ScriptedDecider, **kw: Any) -> Pilot:
    return Pilot(tools, decider, sleep=nap, **kw)


COMMENT = Value(key="v1", label="the text 'Nice work'", text="Nice work")
PASSWORD = Value(key="v2", label="the account password (hidden)", text="hunter2hunter2", secret=True)
VAULT = Value(key="v1", label="the password (hidden, the node fills it)", vault="site-login", secret=True)


# ---- what is offered ------------------------------------------------------------------------------

def test_off_site_links_and_disabled_controls_are_never_offered() -> None:
    refs = {e.ref for e in offered(parse_page(OFFSITE), ["127.0.0.1"], set())}
    assert "e5" in refs and "e7" not in refs and "e9" not in refs


def test_only_an_open_dialog_is_on_offer_and_a_flipped_toggle_is_not() -> None:
    assert [e.ref for e in offered(parse_page(DIALOG), [], set())] == ["e20"]
    page = parse_page(FEED)
    like = page.by_ref("e2")
    assert "e2" not in {e.ref for e in offered(parse_page(LIKED), [], {like.key})}  # its count changed


def test_the_write_operation_exists_only_when_a_run_may_write() -> None:
    page = parse_page(FEED)
    elements = offered(page, [], set())
    assert "WRITE" not in build_questions("g", elements, pending=[], files=[], seed="s")["operation"]["criteria"]
    asked = build_questions("g", elements, pending=[], files=[], seed="s", writing=True)
    assert "WRITE" in asked["operation"]["criteria"] and "write_field" in asked


def test_a_missing_yes_or_no_is_a_failed_call_not_a_no() -> None:
    with pytest.raises(RuntimeError):
        noul({}, "done")


# ---- the ordinary loop ------------------------------------------------------------------------------

async def test_a_click_then_two_agreeing_answers_end_verified() -> None:
    tools = FakeTools(FEED, LIKED, results={"browser_act": {"ok": True, "changed": True, "detail": "pressed"}})
    decider = ScriptedDecider(step("CLICK", click_target="e2"), step("DONE", done=0.95))
    result = await pilot(tools, decider).run(request())
    assert result.status is Status.DONE_VERIFIED and result.effects == 1
    assert tools.named("browser_act") == [{"on": "w/l", "ref": "e2", "action": ActionKind.CLICK}]
    assert result.readbacks[0]["passed"] is True and result.readbacks[0]["how"] == "the target's own state"


async def test_a_fresh_run_looks_first_and_a_new_page_gets_its_look() -> None:
    settings = FEED.replace("/feed", "/settings")
    tools = FakeTools(FEED, settings, settings, results={"browser_act": {"ok": True}})
    await pilot(tools, ScriptedDecider(step("CLICK", click_target="e5"), step("DONE", done=0.95))).run(request())
    assert tools.looks == 2


async def test_the_page_signal_beats_the_model() -> None:
    decider = ScriptedDecider(step("DONE", done=0.99))
    result = await pilot(FakeTools(CAPTCHA), decider).run(request())
    assert result.status is Status.HELD and result.reason == "signal_captcha" and decider.requests == []


async def test_a_challenge_after_an_action_ends_the_run_before_the_next_question() -> None:
    tools = FakeTools(FEED, CAPTCHA, results={"browser_act": {"ok": True, "changed": True}})
    decider = ScriptedDecider(step("CLICK", click_target="e2"))
    result = await pilot(tools, decider).run(request())
    assert result.status is Status.HELD and len(decider.requests) == 1


async def test_a_signed_out_lane_is_a_persons_unless_the_run_holds_a_login() -> None:
    out = FEED.replace("signal: ok", "signal: logged_out")
    decider = ScriptedDecider(step("DONE", done=0.95))
    result = await pilot(FakeTools(out), decider).run(request())
    assert result.status is Status.HELD and result.reason == "signal_logged_out" and decider.requests == []
    await pilot(FakeTools(out), decider).run(request(values=(VAULT,), login=True))
    assert len(decider.requests) == 1


async def test_needs_owner_holds_the_run() -> None:
    result = await pilot(FakeTools(FEED), ScriptedDecider(step("CLICK", owner=0.9, click_target="e2"))).run(request())
    assert result.status is Status.HELD and result.reason == "needs_owner"


async def test_an_answer_that_was_not_offered_runs_nothing() -> None:
    tools = FakeTools(OFFSITE)
    result = await pilot(tools, ScriptedDecider(step("CLICK", click_target="e7"))).run(request())
    assert result.status is Status.REFUSED and tools.named("browser_act") == []


async def test_scrolling_is_the_node_tool_and_never_a_browser_act() -> None:
    tools = FakeTools(FEED, LIKED)
    await pilot(tools, ScriptedDecider(step("SCROLL"), step("DONE", done=0.95))).run(request())
    assert tools.named("browser_scroll") == [{"on": "w/l"}] and tools.named("browser_act") == []


async def test_a_shaky_wait_turns_the_wheel_and_nothing_helpful_on_the_page_looks_further() -> None:
    tools = FakeTools(FEED, LIKED)
    decider = ScriptedDecider(step("WAIT", 0.3), step("CLICK", present=0.1, click_target="e2"), step("DONE", done=0.9))
    await pilot(tools, decider).run(request())
    assert len(tools.named("browser_scroll")) == 2 and tools.named("browser_act") == []


async def test_an_unchanged_page_stops_the_run_and_a_refusal_says_so() -> None:
    limits = Limits(no_progress=2)
    tools = FakeTools(FEED, results={"browser_act": ToolFailed("egress_refused", "off the list")})
    result = await pilot(tools, ScriptedDecider(step("CLICK", click_target="e2"))).run(request(limits=limits))
    assert result.status is Status.REFUSED and result.reason == "no_progress" and "egress_refused" in result.message
    quiet = await pilot(FakeTools(FEED), ScriptedDecider(step("SCROLL"))).run(request(limits=limits))
    assert quiet.status is Status.GAVE_UP and quiet.reason == "no_progress"


async def test_budgets_end_the_run() -> None:
    steps = await pilot(FakeTools(FEED, LIKED, FEED, LIKED), ScriptedDecider(step("SCROLL"))).run(
        request(limits=Limits(max_steps=2)))
    assert steps.status is Status.GAVE_UP and steps.reason == "step_budget" and steps.steps == 2
    ticks = iter(range(0, 10_000, 200))
    timed = await Pilot(FakeTools(FEED, LIKED), ScriptedDecider(step("SCROLL")), clock=lambda: next(ticks), sleep=nap).run(
        request(limits=Limits(wall_s=300)))
    assert timed.status is Status.GAVE_UP and timed.reason == "time_budget"
    money = await pilot(FakeTools(FEED, LIKED), ScriptedDecider(step("SCROLL"), usd=1.0)).run(
        request(limits=Limits(max_usd=0.5)))
    assert money.status is Status.GAVE_UP and money.reason == "budget"


async def test_a_person_taking_the_lane_ends_the_run_at_once() -> None:
    tools = FakeTools(FEED, results={"browser_act": ToolFailed("human_in_control", "a person has it")})
    result = await pilot(tools, ScriptedDecider(step("CLICK", click_target="e2"))).run(request())
    assert result.status is Status.HELD and result.reason == "human_in_control" and len(tools.named("browser_act")) == 1


async def test_a_cancel_lands_between_steps() -> None:
    class Stopped(Exception):
        pass

    def stop() -> None:
        raise Stopped

    with pytest.raises(Stopped):
        await pilot(FakeTools(FEED), ScriptedDecider(step("SCROLL")), checkpoint=stop).run(request())


# ---- honest ends ----------------------------------------------------------------------------------

async def test_answers_that_disagree_are_never_a_bare_done() -> None:
    both_ways = [(step("DONE", done=0.2), "likely_done"), (step("SCROLL", done=0.9), "done_answers_disagree")]
    for answer, why in both_ways:
        tools = FakeTools(FEED, LIKED, results={"browser_act": {"ok": True, "changed": True}})
        decider = ScriptedDecider(step("CLICK", click_target="e2"), answer)
        result = await pilot(tools, decider).run(request())
        assert result.status is Status.DONE_UNVERIFIED and result.reason == why


async def test_done_with_nothing_done_or_values_left_is_unverified() -> None:
    idle = await pilot(FakeTools(FEED), ScriptedDecider(step("DONE", done=0.95))).run(request())
    assert idle.status is Status.DONE_UNVERIFIED and idle.reason == "nothing_read_back"
    left = await pilot(FakeTools(FEED), ScriptedDecider(step("DONE", done=0.95))).run(request(values=(COMMENT,)))
    assert left.status is Status.DONE_UNVERIFIED and left.reason == "values_not_entered"


async def test_a_last_action_nothing_could_read_back_is_unverified() -> None:
    tools = FakeTools(FEED, results={"browser_act": {"ok": True}})
    decider = ScriptedDecider(step("CLICK", click_target="e2"), step("DONE", done=0.95))
    result = await pilot(tools, decider).run(request())
    assert result.status is Status.DONE_UNVERIFIED and result.reason == "last_action_not_seen"


# ---- values, secrets and files ----------------------------------------------------------------------

async def test_a_value_counts_as_entered_only_when_its_action_worked() -> None:
    tools = FakeTools(FEED, FEED, TYPED, results={"browser_act": [ToolFailed("ref_not_found", "gone"), {"ok": True}]})
    answer = step("TYPE", type_value="v1", field_for_v1="e3")
    decider = ScriptedDecider(answer, answer, step("DONE", done=0.95))
    result = await pilot(tools, decider).run(request(values=(COMMENT,)))
    assert len(tools.named("browser_act")) == 2  # all a typed value leaves to read is a page that changed
    assert result.status is Status.DONE_UNVERIFIED and result.reason == "only_the_page_changed"
    assert "TYPE" in decider.requests[1]["questions"]["operation"]["criteria"]  # the first try did not count
    assert "TYPE" not in decider.requests[2]["questions"]["operation"]["criteria"]


async def test_a_button_in_a_form_with_values_waiting_types_first() -> None:
    tools = FakeTools(FEED, TYPED)
    answer = step("CLICK", click_target="e6", type_value="v1", field_for_v1="e3")
    await pilot(tools, ScriptedDecider(answer, step("DONE", done=0.95))).run(request(values=(COMMENT,)))
    assert tools.named("browser_act")[0]["action"] is ActionKind.FILL


async def test_a_secret_never_reaches_a_model_and_must_not_send_always_goes_with_it() -> None:
    goal = "Sign in with password hunter2hunter2"
    tools = FakeTools(LOGIN, SIGNED_IN)
    decider = ScriptedDecider(step("TYPE", type_value="v2", field_for_v2="e2"), step("DONE", done=0.95))
    await pilot(tools, decider).run(request(goal, values=(PASSWORD,)))
    assert tools.named("browser_act")[0]["text"] == "hunter2hunter2"
    for sent in decider.requests:
        assert "hunter2hunter2" not in str(sent["state"]) + str(sent["questions"])
        assert sent["must_not_send"] == ["hunter2hunter2"]


async def test_a_vault_value_goes_through_the_vault_by_name() -> None:
    tools = FakeTools(LOGIN, SIGNED_IN, results={"vault_fill": {"ok": True, "field": "password", "needs": None}})
    decider = ScriptedDecider(step("TYPE", type_value="v1", field_for_v1="e2"), step("DONE", done=0.95))
    result = await pilot(tools, decider).run(request("Sign in", values=(VAULT,)))
    assert tools.named("vault_fill") == [{"on": "w/l", "field": "e2", "entry": "site-login"}]
    assert tools.named("browser_act") == [] and result.status is Status.DONE_UNVERIFIED
    assert "site-login" not in str(decider.requests)  # the model reads a label, not the entry


async def test_a_vault_fill_that_needs_a_person_stops_the_run() -> None:
    tools = FakeTools(LOGIN, results={"vault_fill": {"ok": False, "needs": "human"}})
    result = await pilot(tools, ScriptedDecider(step("TYPE", type_value="v1", field_for_v1="e2"))).run(
        request("Sign in", values=(VAULT,)))
    assert result.status is Status.NEEDS_HELP and result.reason == "vault_fill_failed"


async def test_a_refused_request_is_a_refusal_with_nothing_done() -> None:
    tools = FakeTools(FEED)
    decider = ScriptedDecider(DeciderFailed(BLOCKED, "the body holds must_not_send[0], so nothing was sent"))
    result = await pilot(tools, decider).run(request())
    assert result.status is Status.REFUSED and result.reason == "secret_in_request" and result.effects == 0


async def test_no_decider_and_no_budget_are_honest_ends() -> None:
    gone = await pilot(FakeTools(FEED), ScriptedDecider(DeciderFailed(UNAVAILABLE, "down"))).run(request())
    assert gone.status is Status.GAVE_UP and gone.reason == "decider_unavailable" and "brain" in gone.needs
    spent = await pilot(FakeTools(FEED), ScriptedDecider(DeciderFailed(BUDGET, "budget: spent"))).run(request())
    assert spent.status is Status.GAVE_UP and spent.reason == "budget"


async def test_a_file_is_attached_once_and_a_missing_one_is_refused_before_anything_runs() -> None:
    inbox = {"inbox": [{"id": "in_1", "name": "a.png"}]}
    tools = FakeTools(FEED, LIKED, results={"files_list": inbox})
    decider = ScriptedDecider(step("UPLOAD", upload_target="e2", click_target="e2"), step("DONE", done=0.95))
    result = await pilot(tools, decider).run(request(files=("a.png",)))
    assert tools.named("browser_upload") == [{"on": "w/l", "ref": "e2", "file_id": "in_1"}]
    assert result.status is Status.DONE_UNVERIFIED and "UPLOAD" not in decider.requests[1]["questions"]["operation"]["criteria"]
    missing = await pilot(FakeTools(FEED, results={"files_list": inbox}), ScriptedDecider(step("DONE"))).run(
        request(files=("b.png",)))
    assert missing.status is Status.REFUSED and missing.reason == "file_not_in_inbox"


# ---- going up a level -------------------------------------------------------------------------------

async def test_without_a_rescue_an_unsure_pick_ends_saying_what_it_needs() -> None:
    tools = FakeTools(FEED)
    result = await pilot(tools, ScriptedDecider(step("CLICK", click_target=pick("e2", 0.4)))).run(request())
    assert result.status is Status.NEEDS_HELP and result.reason == "low_confidence_target"
    assert "rescue model" in result.needs and tools.named("browser_act") == []
    assert [e.outcome for e in result.escalations] == ["no_rescue"]


async def test_a_rescue_picks_from_the_same_options_and_its_cost_is_recorded() -> None:
    tools = FakeTools(FEED, LIKED, results={"browser_act": {"ok": True, "changed": True}})
    decider = ScriptedDecider(step("CLICK", click_target=pick("e2", 0.4)), step("DONE", done=0.95))
    rescue = ScriptedRescue("e6")
    result = await pilot(tools, decider, rescue=rescue).run(request())
    assert tools.named("browser_act")[0]["ref"] == "e6" and result.status is Status.DONE_VERIFIED
    asked, sent = rescue.asked[0], decider.requests[0]
    assert asked["options"] == sent["questions"]["click_target"]["criteria"] and asked["state"] == sent["state"]
    assert asked["question"] == sent["questions"]["click_target"]["instructions"]
    [record] = result.escalations
    assert (record.reason, record.outcome, record.to) == ("low_confidence_target", "picked", "haiku")
    assert record.picked == {"click_target": "e6"} and record.usd == 0.002 and record.tokens == 300
    assert result.spend["rescue"].usd == 0.002 and result.public()["spend"]["rescue"]["calls"] == 1


async def test_must_not_send_goes_to_a_rescue_too() -> None:
    rescue = ScriptedRescue("e2")
    decider = ScriptedDecider(step("TYPE", type_value="v2", field_for_v2=pick("e2", 0.4)), step("DONE", done=0.95))
    await pilot(FakeTools(LOGIN, SIGNED_IN), decider, rescue=rescue).run(request("Sign in", values=(PASSWORD,)))
    assert rescue.asked[0]["must_not_send"] == ["hunter2hunter2"]


async def test_an_unsure_operation_goes_up_alone_and_its_target_keeps_its_own_confidence() -> None:
    tools = FakeTools(FEED, LIKED, results={"browser_act": {"ok": True, "changed": True}})
    decider = ScriptedDecider(step("CLICK", 0.3, click_target="e2"), step("DONE", done=0.95))
    rescue = ScriptedRescue("CLICK")
    result = await pilot(tools, decider, rescue=rescue).run(request())
    assert len(rescue.asked) == 1 and tools.named("browser_act")[0]["ref"] == "e2"
    assert result.escalations[0].reason == "low_confidence_operation"


async def test_an_unsure_operation_and_target_are_two_questions() -> None:
    tools = FakeTools(FEED, LIKED, results={"browser_act": {"ok": True, "changed": True}})
    decider = ScriptedDecider(step("CLICK", 0.3, click_target=pick("e2", 0.3)), step("DONE", done=0.95))
    rescue = ScriptedRescue("CLICK", "e2")
    result = await pilot(tools, decider, rescue=rescue).run(request())
    assert [e.reason for e in result.escalations] == ["low_confidence_operation", "low_confidence_target"]
    assert result.status is Status.DONE_VERIFIED


async def test_a_rescue_passes_the_same_gates_a_decider_does() -> None:
    tools = FakeTools(FEED, TYPED)
    decider = ScriptedDecider(step("CLICK", 0.3, click_target="e6", type_value="v1", field_for_v1="e3"),
                              step("DONE", done=0.95))
    await pilot(tools, decider, rescue=ScriptedRescue("CLICK")).run(request(values=(COMMENT,)))
    assert tools.named("browser_act")[0]["action"] is ActionKind.FILL  # the send button still waits for the text


async def test_a_rescue_cannot_invent_an_action() -> None:
    for answer in ("https://evil.test", "#selector", "e7"):  # e7 is the off-site link: never offered
        tools = FakeTools(OFFSITE)
        result = await pilot(tools, ScriptedDecider(step("CLICK", click_target=pick("e2", 0.4))),
                             rescue=ScriptedRescue(answer)).run(request())
        assert result.status is Status.NEEDS_HELP and result.reason == "rescue_failed"
        assert tools.named("browser_act") == [] and result.escalations[0].outcome == "failed"


async def test_a_rescue_can_decline_or_stay_unsure_and_nothing_runs() -> None:
    for answer, reason, outcome in ((None, "rescue_abstained", "abstained"),
                                    (("e2", 0.3), "low_confidence_target", "unsure")):
        tools = FakeTools(FEED)
        result = await pilot(tools, ScriptedDecider(step("CLICK", click_target=pick("e2", 0.4))),
                             rescue=ScriptedRescue(answer)).run(request())
        assert result.status is Status.NEEDS_HELP and result.reason == reason
        assert "person" in result.needs and tools.named("browser_act") == []
        assert result.escalations[0].outcome == outcome


async def test_a_rescue_that_fails_or_is_refused_ends_the_run_honestly() -> None:
    low = step("CLICK", click_target=pick("e2", 0.4))
    down = await pilot(FakeTools(FEED), ScriptedDecider(low), rescue=ScriptedRescue(
        DeciderFailed(UNAVAILABLE, "claude-haiku did not answer"))).run(request())
    assert down.status is Status.NEEDS_HELP and down.reason == "rescue_failed"
    assert down.escalations[0].outcome == "failed"
    blocked = await pilot(FakeTools(FEED), ScriptedDecider(low), rescue=ScriptedRescue(
        DeciderFailed(BLOCKED, "a pick held something that must not leave the node"))).run(request())
    assert blocked.status is Status.REFUSED and blocked.reason == "secret_in_request"


async def test_a_rescue_that_says_done_is_held_to_the_same_rules() -> None:
    low = step("CLICK", 0.3, click_target="e2")
    done = await pilot(FakeTools(FEED), ScriptedDecider(low), rescue=ScriptedRescue("DONE")).run(request())
    assert done.status is Status.DONE_UNVERIFIED and done.reason == "likely_done"  # the done question said no


async def test_a_rescue_cannot_pick_hold_any_more_than_a_decider_can() -> None:
    low = step("CLICK", 0.3, click_target="e2")
    result = await pilot(FakeTools(FEED), ScriptedDecider(low), rescue=ScriptedRescue("HOLD")).run(request())
    assert result.status is Status.NEEDS_HELP and result.reason == "rescue_failed"


async def test_escalations_are_limited_per_run() -> None:
    tools = FakeTools(FEED, LIKED, results={"browser_act": {"ok": True, "changed": False}})
    result = await pilot(tools, ScriptedDecider(step("CLICK", click_target=pick("e2", 0.4))),
                         rescue=ScriptedRescue("e2")).run(request(limits=Limits(max_escalations=2)))
    assert result.status is Status.NEEDS_HELP and result.reason == "too_many_escalations"
    assert len(result.escalations) == 2


async def test_a_shaky_scroll_never_goes_up_a_level() -> None:
    rescue = ScriptedRescue("e1")
    await pilot(FakeTools(FEED, LIKED), ScriptedDecider(step("SCROLL", 0.2), step("DONE", done=0.95)),
                rescue=rescue).run(request())
    assert rescue.asked == []


async def test_a_brain_rescue_asks_for_the_right_first_and_reads_the_typed_reply() -> None:
    calls: list[str] = []

    class Brains:
        async def pick(self, principal: Any, **kw: Any) -> dict[str, Any]:
            calls.append("pick")
            assert kw["must_not_send"] == ["s3cret"] and kw["brain"] == "claude-haiku"
            return {"brain": "claude-haiku", "choice": "e2", "confidence": 0.9, "tokens": 40, "latency_ms": 3000}

    async def authorize(text: str) -> None:
        calls.append("authorize")

    rescue = BrainRescue(Brains(), Principal(id="a"), workspace="w", brain="claude-haiku", authorize=authorize)
    got = await rescue.pick("q", {"e2": "x"}, {}, must_not_send=["s3cret"])
    assert calls == ["authorize", "pick"] and (got.choice, got.tokens, got.model) == ("e2", 40, "claude-haiku")


async def test_a_brain_rescue_that_refuses_the_reply_is_no_answer() -> None:
    class Brains:
        async def pick(self, principal: Any, **kw: Any) -> dict[str, Any]:
            raise DecisionRejected("the model chose something that was not offered")

    async def authorize(text: str) -> None:
        return None

    rescue = BrainRescue(Brains(), Principal(id="a"), workspace="w", brain="b", authorize=authorize)
    with pytest.raises(DeciderFailed) as err:
        await rescue.pick("q", {"e2": "x"}, {}, must_not_send=[])
    assert err.value.kind == UNAVAILABLE


# ---- writing -----------------------------------------------------------------------------------------

async def test_a_step_that_needs_words_asks_the_writer_and_types_what_it_wrote() -> None:
    tools = FakeTools(FEED, TYPED)
    writer = ScriptedWriter("Lovely tomatoes")
    decider = ScriptedDecider(step("WRITE", write_field="e3"), step("DONE", done=0.95))
    result = await pilot(tools, decider, writer=writer).run(request("Reply to tomas", may_write=True))
    assert tools.named("browser_act")[0]["text"] == "Lovely tomatoes" and result.spend["write"].usd == 0.001
    assert "WRITE" not in decider.requests[1]["questions"]["operation"]["criteria"]  # once per run
    assert writer.asked[0]["goal"] == "Reply to tomas"


async def test_without_a_writer_a_step_that_needs_words_says_so() -> None:
    tools = FakeTools(FEED)
    result = await pilot(tools, ScriptedDecider(step("WRITE", write_field="e3"))).run(request(may_write=True))
    assert result.status is Status.NEEDS_HELP and result.reason == "needs_text" and "values" in result.needs
    assert tools.named("browser_act") == []


# ---- reading -----------------------------------------------------------------------------------------

async def test_a_reading_run_picks_a_line_and_acts_on_nothing() -> None:
    tools = FakeTools(FEED)
    result = await pilot(tools, ScriptedDecider({"line": pick("l1")})).run(request("Read it", read="first"))
    assert result.answer == {"first": "First tomatoes of the year"} and result.status is Status.DONE_UNVERIFIED
    assert tools.named("browser_act") == [] and result.public()["untrusted_fields"][0] == "answer"


async def test_a_shaky_read_goes_up_a_level() -> None:
    tools = FakeTools(FEED)
    result = await pilot(tools, ScriptedDecider({"line": pick("l1", 0.3)}), rescue=ScriptedRescue("l1")).run(
        request("Read it", read="first"))
    assert result.answer == {"first": "First tomatoes of the year"} and result.escalations[0].outcome == "picked"
    alone = await pilot(FakeTools(FEED), ScriptedDecider({"line": pick("l1", 0.3)})).run(request("Read it", read="first"))
    assert alone.status is Status.NEEDS_HELP and not alone.answer


# ---- a commit ----------------------------------------------------------------------------------------

PENDING = {"status": "approval_pending", "approval_id": "ap_1", "summary": 'Publish a comment: "Nice work"'}


async def test_a_commit_waits_for_a_person_and_the_same_click_goes_after() -> None:
    tools = FakeTools(FEED, FEED.replace("[ref=e6]", "[ref=e16]"), LIKED,
                      results={"browser_act": [PENDING, {"ok": True, "changed": True}]})
    decider = ScriptedDecider(step("CLICK", click_target="e6"), step("DONE", done=0.95))
    run = pilot(tools, decider)
    state = State()
    first = await run.run(request(), state)
    assert first.status is Status.WAITING_APPROVAL and first.approval_id == "ap_1" and "Nice work" in first.approval_summary
    assert first.effects == 0 and state.held is not None
    second = await run.run(request(), state)
    assert tools.named("browser_act")[1]["ref"] == "e16"  # the same button, found again by what it is
    assert second.status is Status.DONE_VERIFIED and len(decider.requests) == 2 and tools.looks == 1


async def test_a_commit_whose_button_went_away_sends_nothing() -> None:
    tools = FakeTools(FEED, FEED.replace('button "Send" [ref=e6] [cursor=pointer]', 'button "Other" [ref=e6]'),
                      results={"browser_act": PENDING})
    run = pilot(tools, ScriptedDecider(step("CLICK", click_target="e6")))
    state = State()
    await run.run(request(), state)
    second = await run.run(request(), state)
    assert second.status is Status.GAVE_UP and second.reason == "approved_target_gone"
    assert len(tools.named("browser_act")) == 1


async def test_a_written_commit_retries_the_same_words() -> None:
    tools = FakeTools(FEED, FEED, TYPED, results={"browser_act": [PENDING, {"ok": True}]})
    writer = ScriptedWriter("Once only")
    run = pilot(tools, ScriptedDecider(step("WRITE", write_field="e3"), step("DONE", done=0.95)), writer=writer)
    state = State()
    await run.run(request(may_write=True), state)
    await run.run(request(may_write=True), state)
    assert [a["text"] for a in tools.named("browser_act")] == ["Once only", "Once only"] and len(writer.asked) == 1


class CostlyRescue:
    """A rescue that answers badly and still bills, as `pick` does for an unusable reply."""
    name = "metered"

    def __init__(self) -> None:
        self.budgets: list[float | None] = []

    async def pick(self, question: str, options: dict[str, str], state: Any, *, must_not_send: Any,
                   max_usd: float | None = None) -> Any:
        from agentos.pilot.deciders import UNAVAILABLE, DeciderFailed

        self.budgets.append(max_usd)
        raise DeciderFailed(UNAVAILABLE, "no usable answer", usd=0.03, tokens=900)


def test_a_rescue_gets_what_is_left_of_the_budget_and_a_bad_answer_still_counts():
    from agentos.pilot import deciders
    from agentos.pilot.deciders import DeciderFailed

    failed = DeciderFailed(deciders.UNAVAILABLE, "x", usd=0.03, tokens=900)
    assert (failed.usd, failed.tokens) == (0.03, 900)
    assert deciders._failed(type("E", (deciders.AgentosError,), {"cost_usd": 0.02, "tokens": 5})("x")).usd == 0.02


# ---- what the decision lab measured ---------------------------------------------------------------

FORMS = """signal: ok
http://127.0.0.1:8000/compose · New post · 5 refs
- main:
  - complementary:
    - button "Publish" [ref=e1] [cursor=pointer]
  - form:
    - heading "New post" [level=1]
    - textbox "Post text" [ref=e2]
    - button "Publish" [ref=e3] [cursor=pointer]
"""
ANSWERED = FORMS.replace('[ref=e2]', '[ref=e2]: Open late')


def choice(key: str, chance: float, second: float = 0.0) -> dict[str, Any]:
    return {"type": "choice", "choice": key, "confidence": 0.1, "probabilities": {key: chance, "other": second}}


def test_hold_is_not_an_operation() -> None:
    questions = build_questions("Do it", offered(parse_page(FEED), [], set()), pending=[], files=[], seed="s")
    assert "HOLD" not in questions["operation"]["criteria"]


async def test_only_the_owner_question_stops_for_a_person() -> None:
    result = await pilot(FakeTools(FEED), ScriptedDecider(step("CLICK", owner=0.9, click_target="e2"))).run(request())
    assert result.status is Status.HELD and result.reason == "needs_owner"


async def test_an_entry_is_judged_by_its_field_and_not_by_the_operation() -> None:
    sure = step("TYPE", 0.3, type_value="v1", field_for_v1=choice("e3", 0.99))
    tools = FakeTools(FEED, TYPED)
    await pilot(tools, ScriptedDecider(sure, step("DONE", done=0.95))).run(request(values=(COMMENT,)))
    assert tools.named("browser_act")[0]["ref"] == "e3"


async def test_an_entry_into_an_unsure_field_stops() -> None:
    unsure = step("TYPE", 0.99, type_value="v1", field_for_v1=choice("e3", 0.6, 0.1))
    tools = FakeTools(FEED)
    result = await pilot(tools, ScriptedDecider(unsure)).run(request(values=(COMMENT,)))
    assert result.status is Status.NEEDS_HELP and result.reason == "low_confidence_target"
    assert tools.named("browser_act") == []


async def test_a_click_still_needs_a_clear_operation() -> None:
    shaky = step("CLICK", 0.4, click_target=choice("e2", 0.99))
    result = await pilot(FakeTools(FEED), ScriptedDecider(shaky)).run(request())
    assert result.reason == "low_confidence_operation"


@pytest.mark.parametrize(("chance", "second"), [(0.69, 0.0), (0.72, 0.25)])
async def test_a_click_needs_a_likely_target_with_a_clear_lead(chance: float, second: float) -> None:
    tools = FakeTools(FEED)
    answers = step("CLICK", 0.99, click_target=choice("e2", chance, second))
    result = await pilot(tools, ScriptedDecider(answers)).run(request())
    assert result.reason == "low_confidence_target"
    assert tools.named("browser_act") == []


async def test_a_gate_reads_the_chance_and_not_the_confidence() -> None:
    answers = step("CLICK", 0.99, click_target=choice("e2", 0.8, 0.1))  # its `confidence` is 0.1
    tools = FakeTools(FEED, LIKED, results={"browser_act": {"ok": True, "changed": True}})
    await pilot(tools, ScriptedDecider(answers, step("DONE", done=0.95))).run(request())
    assert tools.named("browser_act")[0]["ref"] == "e2"


async def test_a_rescue_goes_by_its_own_word_at_the_old_floor() -> None:
    unsure = step("CLICK", 0.99, click_target=choice("e2", 0.6, 0.3))
    tools = FakeTools(FEED, LIKED, results={"browser_act": {"ok": True, "changed": True}})
    rescue = ScriptedRescue(("e2", 0.6))
    await pilot(tools, ScriptedDecider(unsure, step("DONE", done=0.95)), rescue=rescue).run(request())
    assert len(rescue.asked) == 1 and tools.named("browser_act")[0]["ref"] == "e2"


def test_a_box_with_no_name_goes_by_its_heading() -> None:
    page = parse_page(FORMS)
    assert page.by_ref("e3").where == 'form "New post"'
    assert 'in form "New post"' in page.by_ref("e3").label
    assert page.by_ref("e1").where == "complementary"


def test_a_twin_outside_the_form_being_filled_is_not_offered() -> None:
    page = parse_page(ANSWERED)
    assert {e.ref for e in offered(page, [], set())} == {"e1", "e2", "e3"}
    assert {e.ref for e in offered(page, [], set(), 'form "New post"')} == {"e2", "e3"}


async def test_the_form_being_filled_lasts_until_something_is_pressed() -> None:
    entry = step("TYPE", type_value="v1", field_for_v1="e2")
    press = step("CLICK", click_target="e3")
    decider = ScriptedDecider(entry, press, step("DONE", done=0.95))
    value = Value(key="v1", label="the text 'Open late'", text="Open late")
    await pilot(FakeTools(FORMS, ANSWERED, ANSWERED.replace("Open late", "Posted")), decider).run(request(values=(value,)))
    offers = [set(r["questions"]["click_target"]["criteria"]) for r in decider.requests]
    assert offers == [{"e1", "e3"}, {"e3"}, {"e1", "e3"}]


def test_nothing_behind_an_open_dialog_can_scroll_or_wait() -> None:
    questions = build_questions("Do it", offered(parse_page(DIALOG), [], set()), pending=[], files=[], seed="s")
    assert set(questions["operation"]["criteria"]) == {"CLICK", "DONE"}


async def test_a_blocked_page_is_not_scrolled_past_a_dialog() -> None:
    tools = FakeTools(DIALOG, FEED, results={"browser_act": {"ok": True, "changed": False}})
    decider = ScriptedDecider(step("CLICK", present=0.1, click_target="e20"), step("DONE", done=0.95))
    await pilot(tools, decider).run(request())
    assert tools.named("browser_act")[0]["ref"] == "e20"


def test_a_model_reads_no_pointer_shapes() -> None:
    assert "cursor" not in lean(FEED) and 'button "Like 1"' in lean(FEED)
