"""Pins a member grant's ceiling and specials from its roles and options
(openajar.policy.members.member_specials), every role alone and combined, each option valid and not."""

from __future__ import annotations

import itertools

from pinned import check, outcome, plain

from openajar.policy.members import MEMBER_ROLES, member_specials

OPTIONS = {
    "none": {},
    "unknown option": {"colour": "red"},
    "lanes": {"lanes": ["biz-c/insta", "biz-c/*"]},
    "lanes empty": {"lanes": []},
    "lanes not a list": {"lanes": "biz-c/*"},
    "lane outside": {"lanes": ["biz-a/x"]},
    "lane pattern bad": {"lanes": ["no slash here!"]},
    "max_usd": {"max_usd": 50},
    "max_usd float": {"max_usd": 12.5},
    "max_usd zero": {"max_usd": 0},
    "max_usd negative": {"max_usd": -1},
    "max_usd bool": {"max_usd": True},
    "max_usd text": {"max_usd": "5"},
    "max_usd inf": {"max_usd": float("inf")},
    "max_session_s": {"max_session_s": 900},
    "max_session_s zero": {"max_session_s": 0},
    "max_session_s float": {"max_session_s": 9.5},
    "max_session_s bool": {"max_session_s": True},
    "watch": {"watch": True},
    "watch not bool": {"watch": "yes"},
    "effects": {"effects": ["commit", "draft"]},
    "effects empty": {"effects": []},
    "effects not a list": {"effects": "commit"},
    "effects unknown": {"effects": ["fly"]},
    "everything": {"lanes": ["biz-c/*"], "max_usd": 20, "max_session_s": 600, "watch": True,
                   "effects": ["pay", "commit"]},
}
ROLE_SETS = [[r] for r in MEMBER_ROLES] + [list(c) for c in itertools.combinations(MEMBER_ROLES, 2)] + [
    [], ["superuser"], ["viewer", "superuser"], list(MEMBER_ROLES)]


def test_member_specials_is_pinned():
    cases = {}
    for roles in ROLE_SETS:
        for name, options in OPTIONS.items():
            cases[f"{'+'.join(roles) or 'no role'} / {name}"] = outcome(
                lambda roles=roles, options=options: plain(member_specials(roles, options, "biz-c")))
    check("member_specials", cases)
