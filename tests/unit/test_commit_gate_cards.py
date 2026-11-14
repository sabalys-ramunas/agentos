"""The publish card says what it does not show (AUD-03, agentos.policy.cards.publish_card): every
link in full, a vault value or a secret shape marked (it is still sent), characters and fields past
the display limit counted (still bound), each operation of a batch, media bound by id only, an
unrecognised request by its shape, and the account the browser is signed into now (AUD-12)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agentos.core.models import AccountRef
from agentos.node import publish
from agentos.node.cardfacts import NodeCardFacts
from agentos.node.egress import EFFECT_RULES, unknown_rule
from agentos.policy.cards import REDACTED_SPAN, VAULT_SPAN, publish_card

RULES = {r.id: r for r in EFFECT_RULES}
JSON = {"content-type": "application/json"}
REDDIT = "https://www.reddit.com/svc/shreddit/graphql"
LONG_LINK = "https://evil.example/" + "x" * 900 + "?ref=end"


class Runtime:
    def __init__(self, account: AccountRef | None = None, secret: str | None = None):
        self.account, self.secret = account, secret

    def get(self, name):
        if name == "vault" and self.secret:
            return SimpleNamespace(scrub=lambda text: text.replace(self.secret, "[vault]"))
        if name == "lanes":
            return SimpleNamespace(get=lambda lane: SimpleNamespace(spec=SimpleNamespace(account=self.account)))
        if name == "card_facts":  # what a node runtime always carries (agentos.node.cardfacts)
            return NodeCardFacts()
        return None


def card_of(body: bytes, rule: str = "reddit.post", runtime=None, record_extra: dict | None = None, rule_obj=None):
    cap = publish.capture(rule_obj or RULES[rule], REDDIT, "POST", JSON, body)
    cap.record.update(record_extra or {})
    return publish_card(cap.content("test/reddit"), runtime or Runtime())


def reddit(title: str, rich: object | None = None, markdown: str | None = None, **extra) -> bytes:
    content = {"richText": json.dumps(rich)} if rich is not None else {"markdown": markdown}
    return json.dumps({"operation": "CreatePost",
                       "variables": {"input": {"subredditName": "test", "title": title, "content": content,
                                               **extra}}}).encode()


def test_every_link_is_shown_in_full_though_the_rich_text_is_cut():
    rich = {"document": [{"e": "par", "c": [{"e": "text", "t": "see "}, {"e": "link", "t": "here", "u": LONG_LINK}]}]}
    card = card_of(reddit("t", rich=rich))
    assert LONG_LINK in card["links"]
    [raw] = [f for f in card["fields"] if f["name"].endswith("richText")]
    assert raw["not_shown_chars"] > 0 and "the approval binds all of them" in raw["note"]


def test_a_vault_value_and_a_secret_shape_are_marked_not_silently_hidden():
    """The review's probe 15: the card showed scrubbed text with no sign that it differs from what is sent."""
    text = "Join our raffle, code hunter2-secret. Use token ghp_abcdefghijklmnopqrstuvwxyz0123456789."
    card = card_of(reddit("t", markdown=text), runtime=Runtime(secret="hunter2-secret"))
    assert card["text"]["vault"] is True and VAULT_SPAN in card["text"]["note"] and REDACTED_SPAN in card["text"]["note"]
    assert "hunter2-secret" not in json.dumps(card)


def test_a_batch_lists_each_operation():
    body = json.dumps([{"operationName": "CreatePost", "variables": {"input": {"title": "A", "content": {"markdown": "SHOWN_A"}}}},
                       {"operationName": "CreatePost", "variables": {"input": {"title": "B", "content": {"markdown": "HIDDEN_B"}}}}]).encode()
    card = card_of(body)
    assert [(o["op"], o["title"]["value"], o["text"]["value"]) for o in card["operations"]] == [
        ("CreatePost", "A", "SHOWN_A"), ("CreatePost", "B", "HIDDEN_B")]


def test_fields_past_the_limit_are_counted_and_media_by_id_says_so():
    card = card_of(reddit("t", markdown="x", **{f"f{i:03d}": i for i in range(60)}),
                   record_extra={"media": [{"ref": "123", "seen": False}]})
    assert card["fields_hidden"] > 0 and "the approval binds them too" in card["fields_note"]
    assert card["media"] == [{"ref": "123", "seen": False, "note": "media not seen by the node: id only"}]


def test_an_unrecognised_request_is_named_by_its_shape():
    rule = unknown_rule(EFFECT_RULES, "www.reddit.com", "https://www.reddit.com/submit-form", "POST")
    cap = publish.capture(rule, "https://www.reddit.com/submit-form", "POST", JSON,
                          json.dumps({"title": "x", "body": "y"}).encode())
    card = publish_card(cap.content("test/reddit"), Runtime())
    assert card["headline"] == "A request agentos does not recognise, to Reddit: POST /submit-form"
    assert card["shape"].startswith("POST /submit-form with ") and card["kind"] == "unknown"
    # in plain words: what is known, and that denying is safe (finding 27 of 2026-11-10)
    assert "cannot tell whether it posts" in card["explain"] and "Denying it is safe" in card["explain"]
    assert card["deny_safe"] is True and "typed" not in card["explain"] and "\u2014" not in json.dumps(card)
    cap.record["carries_typed"] = "title"
    typed = publish_card(cap.content("test/reddit"), Runtime())
    assert "It carries text the agent typed as its title" in typed["explain"]


def test_the_signed_in_account_is_shown_and_a_mismatch_warned():
    pinned = AccountRef(site="reddit", handle="u/MiraKestrelDemo", user_id="t2_examplecat0")
    same = card_of(reddit("t", markdown="x"), runtime=Runtime(pinned),
                   record_extra={"session_account": {"site": "reddit", "user_id": "t2_examplecat0"}})
    assert same["line"].startswith("as u/MiraKestrelDemo on Reddit") and "account_warning" not in same
    other = card_of(reddit("t", markdown="x"), runtime=Runtime(pinned),
                    record_extra={"session_account": {"site": "reddit", "user_id": "t2_other"}})
    assert other["line"].startswith("as t2_other on Reddit") and "t2_other" in other["account_warning"]
    unpinned = card_of(reddit("t", markdown="x"), record_extra={"session_account": {"site": "reddit", "user_id": "t2_x"}})
    assert unpinned["line"].startswith("as t2_x on Reddit") and unpinned["signed_in_as"]["user_id"] == "t2_x"


def linkedin_body(text: str = "Five lessons") -> bytes:
    return json.dumps({"requestId": "com.linkedin.sdui.requests.sharing.CreatePost", "serverRequest": {
        "requestedArguments": {"payload": {"commentary": text, "visibility": "ANYONE"}}}}).encode()


def test_a_site_that_cannot_report_its_account_says_so_and_never_shows_the_lane_account_as_verified():
    """LinkedIn has no identity cookie (agentos.node.sites): the card said "as <lane handle>" with no caveat."""
    pinned = AccountRef(site="linkedin", handle="Arlo Fenwick", user_id="juozas-k")
    card = card_of(linkedin_body(), rule="linkedin.post", runtime=Runtime(pinned))
    assert card["account_status"] == "unknown" and card["acts_as"] is None and card["signed_in_as"] is None
    assert card["account_warning"].startswith("The node cannot see which account this browser is signed into on "
                                              "Linkedin, so it cannot confirm this publishes as Arlo Fenwick.")
    assert not card["line"].startswith("as ")


def test_a_site_whose_account_could_not_be_read_says_so():
    pinned = AccountRef(site="reddit", handle="u/name", user_id="t2_abc")
    card = card_of(reddit("t", markdown="x"), runtime=Runtime(pinned))
    assert card["account_status"] == "unknown" and card["acts_as"] is None
    assert "could not read which account this browser is signed into on Reddit" in card["account_warning"]
    assert "u/name" in card["account_warning"]
    bare = card_of(reddit("t", markdown="x"))
    assert "no expected account is set for this lane" in bare["account_warning"]


def test_a_lane_pinned_by_handle_only_compares_the_handle():
    pinned = AccountRef(site="wordpress", handle="@Fernhill", user_id=None)
    same = card_of(reddit("t", markdown="x"), runtime=Runtime(pinned),
                   record_extra={"session_account": {"site": "wordpress", "user_id": "fernhill"}})
    assert same["account_status"] == "match" and "account_warning" not in same and same["acts_as"] == "@Fernhill"
    other = card_of(reddit("t", markdown="x"), runtime=Runtime(pinned),
                    record_extra={"session_account": {"site": "wordpress", "user_id": "someone-else"}})
    assert other["account_status"] == "wrong_account" and other["acts_as"] == "someone-else"
    assert "someone-else" in other["account_warning"] and "@Fernhill" in other["account_warning"]


def test_a_batch_operation_keeps_its_counts_and_vault_marks():
    long = "y" * 700
    body = json.dumps([{"operationName": "CreatePost", "variables": {"input": {"title": "A", "content": {
        "markdown": "code hunter2-secret " + long}}}}]).encode()
    [op] = card_of(body, runtime=Runtime(secret="hunter2-secret"))["operations"]
    assert op["op"] == "CreatePost" and op["title"] == {"value": "A"}
    assert op["text"]["vault"] is True and op["text"]["not_shown_chars"] > 0 and VAULT_SPAN in op["text"]["note"]
    assert "hunter2-secret" not in json.dumps(op)


def test_a_text_that_is_not_a_string_is_shown_as_json():
    card = card_of(json.dumps({"operation": "CreatePost", "variables": {"input": {
        "subredditName": "t", "title": "t", "content": {"text": 12345}}}}).encode())
    assert card["text"]["value"] == "12345"


def test_invisible_and_direction_characters_are_warned():
    card = card_of(reddit("t", markdown="pay here https://good.example/\u202eexe.txt and\u200b more"))
    assert "U+202E" in card["chars"] and "U+200B" in card["chars"]
    assert "2 invisible or direction-changing characters" in card["chars_warning"]
    assert "chars_warning" not in card_of(reddit("t", markdown="plain"))


def test_non_ascii_names_and_hosts_are_shown_with_their_script_mix():
    card = card_of(json.dumps({"operation": "CreatePost", "variables": {"input": {
        "subredditName": "lithu\u0430nia", "title": "t",
        "content": {"markdown": "see https://p\u0430ypal.example/x"}}}}).encode())
    [target, host] = card["lookalikes"]
    assert target == {"field": "target", "value": "lithu\u0430nia", "ascii": "lithu[U+0430]nia",
                      "scripts": ["CYRILLIC", "LATIN"]}
    assert host["field"] == "link host" and host["ascii"] == "xn--pypal-4ve.example"
    assert "look like" in card["lookalike_warning"]
    assert "lookalikes" not in card_of(reddit("t", markdown="https://plain.example"))


def test_a_link_inside_rich_text_json_is_listed_whole_not_cut_at_an_escape():
    rich = {"document": [{"e": "par", "c": [{"e": "link", "t": "x", "u": "https://b\u0430d.example/p"}]}]}
    card = card_of(reddit("t", rich=rich))
    assert card["links"] == ["https://b\u0430d.example/p"]
