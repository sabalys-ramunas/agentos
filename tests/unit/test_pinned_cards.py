"""Pins publish_card's whole card for a table of publishes (agentos.policy.cards): the account
check (pinned by id, by handle, unpinned, unreadable), who it acts as (self, surface, unknown),
invisible characters, lookalike names, an unrecognised request, fields past the limit, media."""

from __future__ import annotations

from types import SimpleNamespace

from pinned import check, outcome

from agentos.core.models import AccountRef
from agentos.node.cardfacts import NodeCardFacts
from agentos.policy.cards import publish_card

LANE = "test/reddit"


class Runtime:
    def __init__(self, account: AccountRef | None = None, secret: str | None = None, gone: bool = False):
        self.account, self.secret, self.gone = account, secret, gone

    def get(self, name):
        if name == "vault" and self.secret:
            return SimpleNamespace(scrub=lambda text: text.replace(self.secret, "[vault]"))
        if name == "lanes":
            def get(lane):
                if self.gone:
                    raise KeyError(lane)
                return SimpleNamespace(spec=SimpleNamespace(account=self.account))
            return SimpleNamespace(get=get)
        if name == "card_facts":  # what a node runtime always carries (agentos.node.cardfacts)
            return NodeCardFacts()
        return None


class NoServices:
    """No lanes and no vault: only the facts every node runtime carries (agentos.node.cardfacts)."""

    def get(self, name):
        return NodeCardFacts() if name == "card_facts" else None


NO_SERVICES = NoServices()
BY_ID = Runtime(AccountRef(site="reddit", handle="@Me", user_id="u1"))
BY_HANDLE = Runtime(AccountRef(site="reddit", handle="@Me"))
UNPINNED = Runtime(None)
LIVE_U1 = {"site": "reddit", "user_id": "u1"}
LIVE_U2 = {"site": "reddit", "user_id": "u2"}
LIVE_ME = {"site": "reddit", "user_id": "me"}
LIVE_OTHER_SITE = {"site": "facebook", "user_id": "me"}

SELF = {"status": "self", "id": "u1", "label": "Me", "field": "author"}
SURFACE = {"status": "surface", "id": "p9", "label": "biz-a (Page)", "surface": "biz-a/page", "through": "personal/fb",
           "kind": "page", "verified": True}
STRANGER = {"status": "unrecognized", "id": "x77", "field": "actor_id", "why": "no surface names it.", "short": "s"}
STRANGER_NO_FIELD = {"status": "unrecognized", "id": "x77"}
STRANGER_NO_ID = {"status": "unrecognized"}


def content(record: dict, rule: str = "reddit.post", lane: str = LANE) -> dict:
    return {"publish": rule, "lane": lane, "record": {"kind": "post", **record}}


FIELDS = {f"f{i:03d}": i for i in range(45)}
CASES = {
    "minimal no runtime": (content({"text": "hello"}), NO_SERVICES),
    "unknown rule id and site": (content({"text": "hi"}, rule="mystery.thing"), NO_SERVICES),
    "rule with no dot": (content({"text": "hi"}, rule="mystery"), NO_SERVICES),
    "text not a string": (content({"text": {"a": 1}}), NO_SERVICES),
    "no text title link": (content({"title": "T", "link": "https://example.com/a"}), NO_SERVICES),
    "match by user id": (content({"text": "hi", "session_account": LIVE_U1}), BY_ID),
    "wrong account by user id": (content({"text": "hi", "session_account": LIVE_U2}), BY_ID),
    "match by handle": (content({"text": "hi", "session_account": LIVE_ME}), BY_HANDLE),
    "wrong account by handle": (content({"text": "hi", "session_account": LIVE_U2}), BY_HANDLE),
    "handle on another site": (content({"text": "hi", "session_account": LIVE_OTHER_SITE}), BY_HANDLE),
    "unpinned and seen": (content({"text": "hi", "session_account": LIVE_U2}), UNPINNED),
    "unpinned and not seen": (content({"text": "hi"}), UNPINNED),
    "pinned not seen": (content({"text": "hi"}), BY_ID),
    "pinned not seen unknown site": (content({"text": "hi"}, rule="mystery.x"), BY_ID),
    "session account not a dict": (content({"text": "hi", "session_account": "u1"}), BY_ID),
    "lane gone": (content({"text": "hi"}), Runtime(gone=True)),
    "acts as self": (content({"text": "hi", "acts_as": SELF, "session_account": LIVE_U1}), BY_ID),
    "acts as surface": (content({"text": "hi", "acts_as": SURFACE, "session_account": LIVE_U1}), BY_ID),
    "acts as stranger": (content({"text": "hi", "acts_as": STRANGER}), BY_ID),
    "acts as stranger no field": (content({"text": "hi", "acts_as": STRANGER_NO_FIELD}), BY_ID),
    "acts as stranger no id": (content({"text": "hi", "acts_as": STRANGER_NO_ID}), BY_ID),
    "acts as not a dict": (content({"text": "hi", "acts_as": "self"}), BY_ID),
    "one invisible char": (content({"text": "a\u200bb"}), NO_SERVICES),
    "several invisible chars": (content({"text": "a\u200bb\u202ec\u200b", "title": "\ufeffT"}), NO_SERVICES),
    "lookalike target and as": (content({"text": "hi", "target": "p\u0430ypal", "as": "\u0430dmin"}), NO_SERVICES),
    "lookalike link host": (content({"text": "see https://ex\u0430mple.com/x and https://plain.example/y."}), NO_SERVICES),
    "lookalike surface label": (content({"text": "hi", "acts_as": {**SURFACE, "label": "b\u0456z"}}), BY_ID),
    "fields past the limit": (content({"text": "x", "fields": FIELDS}), NO_SERVICES),
    "operations and media": (content({"operations": [{"op": "CreatePost", "title": "A", "text": "B"}, "junk"],
                                      "media": [{"ref": "1", "seen": False}, {"ref": "2"}, {"ref": "3", "sha256": "s"},
                                                {"catalog": True, "label": "a GIF"}, {"catalog": True}, "junk"]}),
                              NO_SERVICES),
    "reply visibility flags": (content({"text": "hi", "target": "r/test", "reply_to": "t3_x", "visibility": "public",
                                        "flags": ["nsfw"]}), NO_SERVICES),
    "vault value in text": (content({"text": "code hunter2-secret here"}), Runtime(secret="hunter2-secret")),
    "unknown request typed text": (content({"kind": "unknown", "fields": {"b": 1, "a": 2}, "carries_typed": "text",
                                            "request": {"method": "PUT", "path": "/x"}}), NO_SERVICES),
    "unknown request typed title": (content({"kind": "unknown", "carries_typed": "title"}), NO_SERVICES),
    "unknown request typed field": (content({"kind": "unknown", "fields": {"q": "v"}, "carries_typed": "body.q",
                                             "request": "not a dict"}), NO_SERVICES),
    "unknown request typed empty": (content({"kind": "unknown", "carries_typed": ""}), NO_SERVICES),
    "unknown request nothing typed": (content({"kind": "unknown", "fields": {f"k{i:02d}": i for i in range(25)}}),
                                      NO_SERVICES),
}


def test_publish_card_is_pinned():
    check("publish_card", {name: outcome(lambda c=c, r=r: publish_card(c, r)) for name, (c, r) in CASES.items()})
