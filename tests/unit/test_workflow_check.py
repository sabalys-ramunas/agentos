"""A definition's rules, on the definition alone: what untrusted data may fill and may not decide."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from openajar.core.models import Effect
from openajar.runner.models import SendsSpec
from openajar.runner.workflows.check import Field, Operation, check
from openajar.runner.workflows.models import WorkflowDefinition


def op(id_, *, effect=Effect.READ, produces=(), sends=None, targets=(), params=None, lane="harbor/shop", required=()):
    return Operation(id=id_, flow_id=id_, on=None, name=id_, lane=lane, effect=effect, params=params or {},
                     produces=list(produces), sends=sends, targets=frozenset(targets), required=frozenset(required))


OPS = {
    "harbor.shop.stock": op("harbor.shop.stock", produces=[Field("left", "Stock left", "number"),
                                                           Field("orders", "Orders", "list"),
                                                           Field("orders_email", "Email"),
                                                           Field("orders_note", "Their note", "text", True),
                                                           Field("note", "A note", "text", True),
                                                           Field("page_items", "Items", "list", True)]),
    "harbor.shop.post": op("harbor.shop.post", effect=Effect.COMMIT, targets={"url"},
                           params={"caption": {}, "url": {}}),
    "mail.send@harbor/mail": op("mail.send@harbor/mail", effect=Effect.COMMIT, sends=SendsSpec(),
                                params={"subject": {}, "body": {}}, lane="harbor/mail"),
}


class Resolver:
    def operation(self, text):
        return OPS.get(text)

    def list_workspace(self, list_id):
        return {"list.harbor.people": "harbor", "list.other": "other"}.get(list_id)

    def mailbox(self, mailbox_id):
        return mailbox_id == "harbor/mail"


def definition(*steps, trigger=None):
    return WorkflowDefinition.model_validate({"name": "A job", "workspace": "harbor",
                                              "trigger": trigger or {"kind": "webhook"}, "steps": list(steps)})


def problems(*steps, trigger=None):
    return [p.code for p in check(definition(*steps, trigger=trigger), Resolver())[0]]


def action(id_="a", operation="harbor.shop.post", **rest):
    return {"id": id_, "kind": "action", "label": id_, "operation": operation, **rest}


STOCK = action("stock", "harbor.shop.stock")
SEND = {"list": "list.harbor.people"}


def test_untrusted_data_fills_an_input_and_a_message_and_a_question():
    assert problems(action(inputs={"caption": "New order from {{trigger.name}}: {{trigger.body}}"}),
                    {"id": "n", "kind": "notify", "message": "{{trigger.body}}"},
                    {"id": "q", "kind": "ask", "question": "Reply to {{trigger.name}}?"}) == []


@pytest.mark.parametrize("left", ["{{trigger.body}}", "{{trigger.amount}}", "{{stock.note}}"])
def test_untrusted_data_cannot_choose_a_branch(left):
    assert "untrusted_decides" in problems(STOCK, {"id": "b", "kind": "branch", "when": {"left": left, "op": "is", "right": "1"}})


def test_every_email_field_is_untrusted_even_the_sender():
    mail = {"kind": "email", "lane": "harbor/mail"}
    for key in ("from", "subject", "body"):
        assert problems({"id": "b", "kind": "branch", "when": {"left": f"{{{{trigger.{key}}}}}"}}, trigger=mail) == ["untrusted_decides"]


def test_a_trusted_read_decides_even_when_an_untrusted_value_went_in():
    assert problems(action("stock", "harbor.shop.stock", inputs={}),
                    {"id": "b", "kind": "branch", "when": {"left": "{{stock.left}}", "op": "more_than", "right": "0"},
                     "then": [action(inputs={"caption": "{{trigger.note}}"})]}) == []


def test_untrusted_data_cannot_choose_what_a_loop_goes_through():
    assert problems({"id": "l", "kind": "loop", "over": "{{trigger.items}}", "body": []}) == ["untrusted_decides"]
    assert "untrusted_decides" in problems(STOCK, {"id": "l", "kind": "loop", "over": "{{stock.page_items}}", "body": []})


def test_untrusted_data_cannot_name_a_target():
    assert problems(action(inputs={"url": "https://harbor.example/{{trigger.path}}"})) == ["untrusted_decides"]
    assert problems(STOCK, action(inputs={"url": "https://harbor.example/{{stock.left}}"})) == []


def test_untrusted_data_cannot_choose_who_a_message_goes_to():
    assert problems(action("s", "mail.send@harbor/mail", send={**SEND, "to": "{{trigger.email}}"})) == ["untrusted_decides"]
    loop = {"id": "each", "kind": "loop", "over": "{{stock.orders}}", "body": [
        action("s", "mail.send@harbor/mail", send={**SEND, "to": "{{each.note}}"})]}
    assert problems(STOCK, loop) == ["untrusted_decides"]
    loop["body"][0]["send"]["to"] = "{{each.email}}"
    assert problems(STOCK, loop) == []


@pytest.mark.parametrize("step", [
    action(operation="{{trigger.op}}"),
    action("s", "mail.send@harbor/mail", send={"list": "{{trigger.list}}"}),
    {"id": "b", "kind": "branch", "when": {"left": "{{stock.left}}", "op": "is", "right": "{{trigger.x}}"}},
])
def test_what_a_step_does_and_the_list_and_what_a_branch_compares_with_are_written_out(step):
    assert "must_be_literal" in problems(STOCK, step)


def test_the_trigger_itself_is_written_out():
    assert "must_be_literal" in problems(trigger={"kind": "page_change", "lane": "harbor/shop", "url": "{{trigger.url}}"})


def test_a_reference_looks_back_only():
    assert problems(action(inputs={"caption": "{{later.left}}"}), action("later", "harbor.shop.stock")) == ["bad_reference"]
    arms = {"id": "b", "kind": "branch", "when": {"left": "{{stock.left}}"}, "then": [action("inner", "harbor.shop.stock")]}
    assert problems(STOCK, arms, action(inputs={"caption": "{{inner.left}}"})) == ["bad_reference"]


def test_a_send_needs_an_owners_list_in_this_workspace_and_only_a_sender_takes_one():
    assert problems(action("s", "mail.send@harbor/mail", send={"list": "list.scraped"})) == ["unknown_list"]
    assert problems(action("s", "mail.send@harbor/mail", send={"list": "list.other"})) == ["outside_workspace"]
    assert problems(action(send=SEND)) == ["send_only_for_senders"]
    assert problems(action("s", "mail.send@harbor/mail", send=SEND, inputs={"to": "x@example.com"})) == ["to_is_in_send"]


def test_a_definition_cannot_carry_a_claim_about_itself():
    for extra in ({"ask_first": False}, {"enabled": True}, {"grant": "owner"}, {"untrusted": []}):
        with pytest.raises(ValidationError):
            definition(action(**extra))
        with pytest.raises(ValidationError):
            WorkflowDefinition.model_validate({"name": "x", "trigger": {"kind": "manual"}, **extra})


def test_unfinished_is_not_a_problem_but_stops_it_being_turned_on():
    d = WorkflowDefinition.model_validate({"name": "New", "trigger": {"kind": "schedule"},
                                           "steps": [{"id": "a", "kind": "action", "label": "do it"}]})
    bad, missing = check(d, Resolver())
    assert bad == [] and [m.words for m in missing] == ["Choose a business.", "Finish how it starts.", 'Finish "do it".']


PHOTO = op("harbor.shop.photo", effect=Effect.COMMIT, params={"caption": {"type": "string"}, "image": {"type": "string", "title": "picture"}},
           required=("caption", "image"))
OPS["harbor.shop.photo"] = PHOTO


def test_a_param_the_operation_needs_is_named_when_it_is_left_empty():
    empty = action("post", "harbor.shop.photo", label="post it", inputs={"caption": "  "})
    bad, missing = check(definition(empty), Resolver())
    assert bad == [] and [(m.target, m.words) for m in missing] == [("post", 'Fill in caption and picture for "post it".')]
    assert check(definition(action("post", "harbor.shop.photo", label="post it", inputs={"caption": "Hi", "image": "a.jpg"})), Resolver()) == ([], [])


def test_a_step_that_posts_before_the_step_that_asks_is_unfinished_and_says_where_it_goes():
    ask = {"id": "read", "kind": "ask", "label": "ask you to read it", "question": "Is this ready?"}
    post = action("post", "harbor.shop.post", label="post it", inputs={"caption": "Hi"})
    bad, missing = check(definition(post, ask), Resolver())
    assert bad == [] and [(m.target, m.code, m.words) for m in missing] == [
        ("post", "acts_before_asking", '"post it" would run before anyone is asked. Move it after "ask you to read it".')]
    assert check(definition(ask, post), Resolver())[1] == []
    assert check(definition(post), Resolver())[1] == []  # a routine with no question is not this rule's business


def test_the_version_is_the_content_and_ignores_what_the_node_filled():
    a = definition(STOCK, trigger={"kind": "webhook"})
    b = definition(STOCK, trigger={"kind": "webhook", "hook": {"token": "x"}})
    assert a.content_hash() == b.content_hash() != definition(trigger={"kind": "webhook"}).content_hash()


def test_shape_limits():
    too_many = [{"id": f"s{i}", "kind": "notify", "message": "x"} for i in range(101)]
    assert problems(*too_many) == ["too_many_steps"]
    deep = {"id": "l1", "kind": "loop", "over": "{{stock.orders}}", "body": [
        {"id": "l2", "kind": "loop", "over": "{{l1.value}}", "body": [
            {"id": "l3", "kind": "loop", "over": "{{l2.value}}", "body": []}]}]}
    assert "too_deep" in problems(STOCK, deep)
    assert problems({"id": "w", "kind": "wait", "amount": 91, "unit": "days"}) == ["wait_too_long"]
    assert problems({"id": "a", "kind": "notify", "message": "x"}, {"id": "a", "kind": "notify", "message": "y"}) == ["duplicate_step"]
