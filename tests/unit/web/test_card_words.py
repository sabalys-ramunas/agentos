"""An approval's preview in the app is words, never the record it binds (finding 24 of the 2026-11-10 full test).
Cards are built the way the node builds them (agentos.policy.cards), then told by agentos.api.web.cardwords."""

from __future__ import annotations

import json

from agentos.api.web import cardwords
from agentos.policy import cards

UNKNOWN = {"publish": "facebook.unknown", "lane": "test/facebook",
           "record": {"kind": "unknown", "fields": {"__a": "1", "__aaid": "0", "fb_dtsg": "x"},
                      "request": {"method": "POST", "path": "/static_resources/webworker/rsrc/"}}}


def _has_no_record(preview: dict) -> None:
    said = json.dumps(preview)
    assert '{\\"' not in said and "record" not in said.lower(), said
    for field in (preview.get("data") or {}).get("fields") or []:
        assert not cardwords.looks_like_record(field["value"]), field


def test_an_unknown_request_is_a_short_plain_card_with_the_gates_own_words() -> None:
    card = cards.publish_card(UNKNOWN)
    preview = cardwords.preview_of({"card": card, "summary": "publish on test/facebook"}, "Facebook")
    assert preview["kind"] == "form"
    data = preview["data"]
    assert data["form"] == "A request to Facebook that agentos does not recognise"
    assert data["ask"] == "Let this request go to Facebook?" and data["no"] == "Don't send it"
    assert {"label": "What agentos knows", "value": card["headline"]} in data["fields"]
    assert not any(field["label"] in ("__a", "fb_dtsg") for field in data["fields"])
    _has_no_record(preview)


def test_a_comment_the_gate_caught_is_the_comment_itself() -> None:
    card = cards.publish_card({"publish": "instagram.comment", "lane": "test/instagram",
                               "record": {"kind": "comment", "text": "Lovely light!", "target": "C123"}})
    preview = cardwords.preview_of({"card": card}, "Instagram")
    if preview["kind"] == "post":  # no warning to show: drawn as the comment it will be
        assert preview["text"] == "Lovely light!"
    else:  # a warning (the node cannot see the account) is shown with it, in words
        data = preview["data"]
        assert data["ask"] == "Post this comment on Instagram?"
        assert {"label": "Text", "value": "Lovely light!"} in data["fields"]
        assert any(field["label"] == "Check first" for field in data["fields"])
    _has_no_record(preview)


def test_a_like_says_what_it_is_and_never_a_post() -> None:
    card = cards.publish_card({"publish": "facebook.like", "lane": "test/facebook",
                               "record": {"kind": "like", "target": "post 42", "fields": {"doc_id": "9"}}})
    data = cardwords.preview_of({"card": card}, "Facebook")["data"]
    assert data["ask"] == "Like this on Facebook?" and data["yes"] == "Like it"
    assert {"label": "What it is", "value": "A like"} in data["fields"]
    assert {"label": "Goes to", "value": "post 42"} in data["fields"]
    assert {"label": "Also sends", "value": "1 other values. Saying yes covers all of them."} in data["fields"]


def test_a_form_click_lists_its_fields_with_what_changed() -> None:
    card = cards.form_card({"form": {"action": "https://shop.example/save", "method": "POST", "fields": [
        {"label": "Name", "value": "Aster", "was": "Astor", "changed": True},
        {"label": "Newsletter", "checked": True, "was_checked": False, "changed": True},
        {"label": "Password", "hidden": "a password is never shown"}]}})
    data = cardwords.preview_of({"card": card}, "Shop")["data"]
    assert data["form"] == "A form on Shop"
    assert data["fields"][0] == {"label": "Name", "value": "Aster", "before": "Astor"}
    assert data["fields"][1] == {"label": "Newsletter", "value": "Ticked", "before": "Not ticked"}
    assert data["fields"][2] == {"label": "Password", "value": "a password is never shown"}
    assert data["changed"] == ["Name", "Newsletter"]


def test_a_preview_that_is_a_record_with_no_card_is_never_shown_raw() -> None:
    raw = json.dumps(UNKNOWN, indent=2)
    preview = cardwords.preview_of({"preview": {"text": raw}, "summary": "Post this?"}, "Facebook")
    assert "text" not in preview and preview["kind"] == "form"
    _has_no_record(preview)
    cut = cardwords.preview_of({"preview": {"text": raw[:40] + "\n[preview truncated: 900 more characters are not shown]"}}, "Facebook")
    assert cut["data"]["fields"] == [{"label": "Details", "value": cardwords.UNREADABLE}]


def test_words_stay_words() -> None:
    assert cardwords.preview_of({"preview": {"text": "Morning light at the roastery."}}, "Instagram") == {
        "text": "Morning light at the roastery."}
    assert cardwords.preview_of({}, "Instagram") == {}
    assert cardwords.plain({"a": 1, "b": 2}) == "a group of 2 values"
    assert cardwords.plain([1, "two", True]) == "1, two, yes"


def test_the_approval_the_app_reads_carries_the_words_not_the_record() -> None:
    from agentos.api.web.decisions import Ctx, approval_out

    item = {"id": "ap_1", "state": "pending", "effect": "commit", "lane": "test/facebook", "summary": "publish",
            "preview": {"text": json.dumps(UNKNOWN)}, "card": cards.publish_card(UNKNOWN), "requested_by": {}}
    out = approval_out(item, Ctx({}, set(), {}))
    assert out["preview"]["kind"] == "form" and "text" not in out["preview"]
    _has_no_record(out["preview"])
