"""Learned profile rules (agentos.node.learned_rules, docs/design/learn-a-site.md section 3): the
node's store, what it refuses, where the guard orders them, and that nothing an agent reaches can
write them."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

import pytest
from test_egress import COMMITTER, make_guard

from agentos.core.errors import InvalidRequest, NotFound
from agentos.node import egress, learned_rules
from agentos.node.egress import EFFECT_RULES, classify
from agentos.node.learned_rules import LearnedRules

IG_GQL = "https://www.instagram.com/graphql/query"
IG_EDIT = "https://www.instagram.com/api/v1/web/accounts/edit/"
REDDIT_GQL = "https://www.reddit.com/svc/shreddit/graphql"
FORM = {"content-type": "application/x-www-form-urlencoded"}
JSON = {"content-type": "application/json"}
BIO_OP = "PolarisProfileEditBioMutation"


def ig(op: str, **variables) -> bytes:
    return f"fb_api_req_friendly_name={op}&variables={json.dumps(variables)}".encode()


def reddit(op: str, **variables) -> bytes:
    return json.dumps({"operation": op, "variables": {"input": variables}}).encode()


EDIT_PATH = "/api/v1/web/accounts/edit/"


@pytest.fixture
def graphql_endpoints(monkeypatch):
    """Reviewed GraphQL edit endpoints as a future entry would name them (the shipped table has
    Instagram's form only): where the order against the GraphQL catch-alls matters."""
    from agentos.node import sites

    monkeypatch.setitem(sites.PROFILE_EDIT_ENDPOINTS, "instagram", {
        **sites.PROFILE_EDIT_ENDPOINTS["instagram"],
        "pronouns": sites.EditEndpoint("POST", "www.instagram.com", r"/graphql/query", "form:pronouns", op=BIO_OP)})
    monkeypatch.setitem(sites.PROFILE_EDIT_ENDPOINTS, "reddit", {
        "about": sites.EditEndpoint("POST", "www.reddit.com", r"/svc/shreddit/graphql", "json:variables.input.about",
                                    op="UpdateProfileAbout"),
        "post_like": sites.EditEndpoint("POST", "www.reddit.com", r"/svc/shreddit/graphql", "json:x", op="CreatePost"),
        "bare": sites.EditEndpoint("POST", "www.reddit.com", r"/svc/shreddit/graphql", "json:x"),
        "query": sites.EditEndpoint("POST", "www.reddit.com", r"/svc/shreddit/graphql", "json:x",
                                    op="ProfileFeedQuery")})


def test_the_store_adds_reads_verifies_and_removes(tmp_path):
    store = LearnedRules(tmp_path)
    rid = store.add("instagram", "bio", host="www.instagram.com", path=EDIT_PATH, method="post", op=None)
    assert rid == "instagram.profile.learned_bio"
    entry = store.get(rid)
    assert entry is not None and entry["kind"] == "profile" and entry["verified"] == "" and entry["method"] == "POST"
    assert stat.S_IMODE(learned_rules.store_path(tmp_path).stat().st_mode) == 0o600
    [rule] = store.effect_rules()
    assert rule.id == rid and rule.kind == "profile" and rule.effect == "commit" and rule.verified == ""
    store.set_verified(rid, "replay")
    assert store.get(rid)["verified"].endswith(" replay") and store.effect_rules()[0].verified.endswith(" replay")
    # learned again (a relearn): replaced, and no longer verified
    store.add("instagram", "bio", host="www.instagram.com", path=EDIT_PATH, method="POST", op=None)
    assert len(store.list()) == 1 and store.get(rid)["verified"] == ""
    with pytest.raises(NotFound):
        store.set_verified("instagram.profile.learned_name", "replay")
    with pytest.raises(InvalidRequest):
        store.set_verified(rid, "not a name with spaces")
    assert store.remove(rid) is True and store.remove(rid) is False and store.list() == []


@pytest.mark.parametrize(("kw", "why"), [
    ({"site": "example", "field": "bio", "host": "www.example.com", "path": "/edit", "method": "POST", "op": None},
     "not a host the commit gate knows"),
    ({"site": "reddit", "field": "bio", "host": "www.instagram.com", "path": EDIT_PATH, "method": "POST", "op": None},
     "belongs to instagram"),
    ({"site": "instagram", "field": "Bio", "host": "www.instagram.com", "path": EDIT_PATH, "method": "POST",
      "op": None}, "a-z and _"),
    ({"site": "instagram", "field": "a" * 31, "host": "www.instagram.com", "path": EDIT_PATH, "method": "POST",
      "op": None}, "a-z and _"),
    ({"site": "instagram", "field": "bio", "host": "www.instagram.com", "path": EDIT_PATH, "method": "GET",
      "op": None}, "writes"),
    # only the reviewed endpoint that saves the field (sites.PROFILE_EDIT_ENDPOINTS)
    ({"site": "instagram", "field": "name", "host": "www.instagram.com", "path": EDIT_PATH, "method": "POST",
      "op": None}, "no reviewed edit endpoint"),
    ({"site": "linkedin", "field": "location", "host": "www.linkedin.com",
      "path": "/voyager/api/voyagerMessagingDashMessengerMessages", "method": "POST", "op": None},
     "no reviewed edit endpoint"),
    ({"site": "instagram", "field": "bio", "host": "www.instagram.com", "path": "/api/v1/web/accounts/set_private/",
      "method": "POST", "op": None}, "not the reviewed endpoint"),
    ({"site": "instagram", "field": "bio", "host": "i.instagram.com", "path": EDIT_PATH, "method": "POST",
      "op": None}, "not the reviewed endpoint"),
    ({"site": "instagram", "field": "bio", "host": "www.instagram.com", "path": EDIT_PATH, "method": "PUT",
      "op": None}, "not the reviewed endpoint"),
    ({"site": "instagram", "field": "bio", "host": "www.instagram.com", "path": EDIT_PATH, "method": "POST",
      "op": "SomethingElse"}, "not the reviewed endpoint"),
    # a built-in commit, write or draft rule's request is never a profile edit
    ({"site": "instagram", "field": "bio", "host": "www.instagram.com", "path": "/graphql/query", "method": "POST",
      "op": "PolarisPostCommentInputRevampedMutation"}, "instagram.comment"),
    ({"site": "instagram", "field": "bio", "host": "i.instagram.com", "path": "/rupload_igphoto/x", "method": "POST",
      "op": None}, "instagram.draft.upload"),
])
def test_the_store_refuses_what_could_hold_or_free_something_else(tmp_path, kw, why):
    store = LearnedRules(tmp_path)
    site, field = kw.pop("site"), kw.pop("field")
    with pytest.raises(InvalidRequest, match=re.escape(why)):
        store.add(site, field, **kw)
    assert store.list() == []


@pytest.mark.parametrize(("field", "op", "why"), [
    ("post_like", "CreatePost", "reddit.post"),          # even a reviewed entry cannot name a post
    ("bare", None, "names no operation"),                # nor every read of a GraphQL endpoint
    ("query", "ProfileFeedQuery", "known read"),         # nor a known read
])
def test_even_a_reviewed_entry_never_takes_a_post_or_a_read(tmp_path, graphql_endpoints, field, op, why):
    with pytest.raises(InvalidRequest, match=re.escape(why)):
        LearnedRules(tmp_path).add("reddit", field, host="www.reddit.com", path="/svc/shreddit/graphql",
                                   method="POST", op=op)


def test_a_hand_edited_entry_never_becomes_a_rule(tmp_path):
    store = LearnedRules(tmp_path)
    store.add("instagram", "bio", host="www.instagram.com", path=EDIT_PATH, method="POST", op=None)
    path = learned_rules.store_path(tmp_path)
    data = json.loads(path.read_text())
    good = data["rules"][0]
    data["rules"] += [
        {**good, "id": "instagram.post.learned_bio"},                          # another kind
        {**good, "kind": "post"},
        {**good, "field": "name", "id": "instagram.profile.learned_name", "path": "/graphql/query",
         "op": "PolarisPostCommentInputRevampedMutation"},                     # a comment's request
        {**good, "field": "x", "id": "instagram.profile.learned_x", "host": "evil.example"},
        {**good, "field": "y", "id": "instagram.profile.learned_y", "method": "GET"},
        {**good, "field": "name", "id": "instagram.profile.learned_name"},     # no reviewed endpoint
        {**good, "path": "/api/v1/web/accounts/set_private/"},                # not the reviewed one
    ]
    path.write_text(json.dumps(data))
    assert [r.id for r in store.effect_rules()] == ["instagram.profile.learned_bio"]


def test_a_torn_file_falls_back_to_its_last_copy(tmp_path):
    store = LearnedRules(tmp_path)
    store.add("instagram", "bio", host="www.instagram.com", path=EDIT_PATH, method="POST", op=None)
    store.add("instagram", "website", host="www.instagram.com", path=EDIT_PATH, method="POST", op=None)
    learned_rules.store_path(tmp_path).write_text("{torn")
    assert [r["id"] for r in store.list()] == ["instagram.profile.learned_bio"]  # the .bak of the last save


# ---- the order: after every built-in commit, write and draft rule, before the reads -----------

def test_moving_the_built_in_reads_after_the_actions_changes_no_built_in_classification():
    """ordered() puts every built-in read after every non-read rule when learned rules exist: no read
    precedes a non-read rule of its site whose path could overlap, so first-match is unchanged."""
    from agentos.policy import globs

    for i, read in enumerate(EFFECT_RULES):
        if read.effect != "read":
            continue
        for later in EFFECT_RULES[i + 1:]:
            if later.effect == "read" or later.site != read.site:
                continue
            if not (globs.domain_match(read.host, later.host.lstrip("*.")) or
                    globs.domain_match(later.host, read.host.lstrip("*."))):
                continue
            # the read's path regex is anchored at its end; the later rule's literal start never matches it
            literal = re.match(r"^/[A-Za-z0-9/_-]*", later.path).group(0)
            assert not re.match(read.path, literal), (read.id, later.id)


def _guard(tmp_path, entries):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.learned_rules = lambda: entries
    guard.rules = guard._base_rules  # reread now
    return guard, ctx


def test_a_learned_rule_takes_its_request_and_never_a_post_comment_like_or_follow(tmp_path, graphql_endpoints):
    store = LearnedRules(tmp_path)
    store.add("instagram", "pronouns", host="www.instagram.com", path="/graphql/query", method="POST", op=BIO_OP)
    store.add("reddit", "about", host="www.reddit.com", path="/svc/shreddit/graphql", method="POST",
              op="UpdateProfileAbout")
    guard, _ = _guard(tmp_path, tuple(store.list()))
    rules = guard.rules
    # the learned request: today a GraphQL read catch-all's (gated as unknown), now the profile edit
    assert classify(IG_GQL, "POST", FORM, body=ig(BIO_OP)).id == "instagram.read.graphql"
    assert classify(IG_GQL, "POST", FORM, body=ig(BIO_OP), rules=rules).id == "instagram.profile.learned_pronouns"
    assert classify(REDDIT_GQL, "POST", JSON, body=reddit("UpdateProfileAbout"), rules=rules).id \
        == "reddit.profile.learned_about"
    # every built-in action on the same endpoints keeps its own rule
    for url, headers, body, want in (
        (IG_GQL, FORM, ig("PolarisPostCommentInputRevampedMutation", data={"comment_text": "hi"}), "instagram.comment"),
        (IG_GQL, FORM, ig("usePolarisLikeMediaXIGLikeMutation"), "instagram.like"),
        (IG_GQL, FORM, ig("usePolarisFollowMutation"), "instagram.follow"),
        (REDDIT_GQL, JSON, reddit("CreatePost", title="t"), "reddit.post"),
        (REDDIT_GQL, JSON, reddit("UpdateSubredditSubscriptions"), "reddit.join"),
        (REDDIT_GQL, JSON, reddit("UpdatePostVoteState"), "reddit.like.post"),
        (REDDIT_GQL, JSON, reddit("ProfileFeedQuery"), "reddit.read.graphql"),
    ):
        assert classify(url, "POST", headers, body=body, rules=rules).id == want
        assert classify(url, "POST", headers, body=body).id == want
    # the learned rule's commit is still gated like any profile edit
    assert egress.gate_pattern(rules).match(IG_GQL)


def test_with_no_learned_rule_the_order_is_exactly_the_built_in_one(tmp_path):
    guard, _ = _guard(tmp_path, ())
    assert guard.rules == EFFECT_RULES


def test_the_guard_rereads_the_store_when_it_changes(tmp_path):
    store = LearnedRules(tmp_path)
    guard, _, _, clock = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.learned_rules = learned_rules.RuleSource(learned_rules.store_path(tmp_path))
    assert guard.rules == EFFECT_RULES
    store.add("instagram", "bio", host="www.instagram.com", path=EDIT_PATH, method="POST", op=None)
    clock.now += egress.SITES_TTL_S + 1
    assert "instagram.profile.learned_bio" in {r.id for r in guard.rules}
    assert classify(IG_EDIT, "POST", FORM, body=b"biography=x", rules=guard.rules).id == "instagram.profile.learned_bio"
    store.remove("instagram.profile.learned_bio")
    clock.now += egress.SITES_TTL_S + 1
    assert guard.rules == EFFECT_RULES


def test_a_store_that_cannot_be_read_leaves_the_built_in_rules(tmp_path):
    guard, _ = _guard(tmp_path, ())

    def broken():
        raise OSError("gone")

    guard.learned_rules = broken
    guard.rules = guard._base_rules
    assert guard.rules == EFFECT_RULES


# ---- nothing an agent reaches writes them --------------------------------------------------

SRC = Path(__file__).resolve().parents[2] / "src" / "agentos"
# node code that may import the store: the guard and the lane manager read it; the profile learner
# (in-process, from its own trace evidence) and the operations registry write or read it
ALLOWED = {"node/learned_rules.py", "node/egress/guard.py", "node/egress/learn.py", "node/lanes/launch.py",
           "profiles/learn.py",
           # the learner writes a rule only from the request the guard's learn window released (its
           # own trace), and verifies it only after its model-free replay: never from an agent's words
           "profiles/learner.py",
           "engage/operations.py"}


def test_no_tool_api_route_or_cli_reaches_the_store():
    users = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        if re.search(r"\blearned_rules\b|\bLearnedRules\b|learned-rules\.json", text):
            users.add(str(path.relative_to(SRC)))
    assert users <= ALLOWED, users - ALLOWED
    # and the guard only reads it (no add, set_verified or remove)
    guard_src = (SRC / "node" / "egress" / "guard.py").read_text()
    assert not re.search(r"\.(add|set_verified|remove)\(\s*[\"']?\w*learned", guard_src)
    assert "LearnedRules(" not in guard_src
