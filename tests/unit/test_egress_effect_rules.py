"""Effect rules against the request shapes the throwaway accounts really sent on 2026-11-04
(observe mode, a canary run of 2026-11-04). Bodies here carry the real field names with made-up
values."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

from agentos.node import egress
from agentos.node.egress import _RULE_ID_RE, EFFECT_KINDS, EFFECT_RULES, classify, gql_verdict, request_op

JSON = {"Content-Type": "application/json"}
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
REDDIT = "https://www.reddit.com/svc/shreddit/graphql"
IG = "https://www.instagram.com/api/graphql"


def reddit(op: str, **variables: object) -> bytes:
    return json.dumps({"operation": op, "variables": {"input": variables}, "csrf_token": "x"}).encode()


def meta_form(op: str, variables: dict[str, object], *, header: bool = True) -> tuple[dict[str, str], bytes]:
    """Instagram and Facebook web: a form with fb_api_req_friendly_name and JSON `variables`,
    and the same name in x-fb-friendly-name."""
    body = urlencode({"av": "1", "fb_dtsg": "t", "fb_api_caller_class": "RelayModern",
                      "fb_api_req_friendly_name": op, "variables": json.dumps(variables), "doc_id": "1"}).encode()
    headers = {**FORM, "X-FB-Friendly-Name": op} if header else dict(FORM)
    return headers, body


def test_rule_ids_name_their_kind_and_verified_rules_say_when():
    for rule in EFFECT_RULES:
        assert _RULE_ID_RE.match(rule.id), rule.id
        assert rule.id.startswith(rule.site + ".")
        if rule.kind == "read":
            assert rule.effect == "read", rule.id
        elif rule.kind in ("post", "comment", "profile"):
            assert rule.effect == "commit", rule.id   # a profile edit changes the public identity
        else:
            assert rule.effect == "write", rule.id  # like, follow, join and draft never need commit
        if rule.verified:
            assert rule.verified.startswith("2026-"), rule.id
    kinds = {r.kind for r in EFFECT_RULES if r.verified}
    # every kind has at least one rule checked on a real account, but profile: nobody has captured
    # LinkedIn's profile edits yet, so its rules ship "to capture" and the engine holds them
    assert EFFECT_KINDS - {"profile"} <= kinds
    assert EFFECT_KINDS == {"post", "comment", "like", "follow", "join", "profile"}
    profile = [r for r in EFFECT_RULES if r.kind == "profile"]
    assert {r.id for r in profile} == {f"linkedin.profile.{f}" for f in ("name", "headline", "about", "photo", "cover")}
    assert all(not r.verified and r.note.startswith("TO CAPTURE") for r in profile)


@pytest.mark.parametrize(("op", "variables", "rule"), [
    ("CreatePost", {"title": "t", "content": {"richText": "{}"}, "subredditName": "test", "recaptchaToken": "r"},
     "reddit.post"),
    ("UpdatePostVoteState", {"postId": "t3_1", "voteState": "UP"}, "reddit.like.post"),
    ("UpdateCommentVoteState", {"commentId": "t1_1", "voteState": "UP"}, "reddit.like.comment"),
    ("UpdateSubredditSubscriptions", {"inputs": [{"subredditId": "t5_1", "subscribeState": "SUBSCRIBED"}]},
     "reddit.join"),
])
def test_reddit_actions_are_known_by_the_operation_in_the_body(op, variables, rule):
    found = classify(REDDIT, "POST", JSON, body=reddit(op, **variables))
    assert found is not None and found.id == rule and found.verified


def test_reddit_reads_and_unknown_operations_are_not_effects():
    """Not an effect for the grant's ceiling; the commit gate reads the catch-all fail closed
    (gql_verdict, test_commit_gate_classify)."""
    for op in ("ExposeVariant", "CreateCaptchaToken", "AchievementUnlockedNotifications"):
        assert classify(REDDIT, "POST", JSON, body=reddit(op)).id == "reddit.read.graphql"
    assert classify(REDDIT, "POST", JSON, body=b"not json").effect == "read"
    assert classify(REDDIT, "POST", JSON).effect == "read"  # no body: an op rule never matches
    # the old client's endpoints keep their (unverified) rules
    assert classify("https://old.reddit.com/api/submit", "POST").id == "reddit.post.oldreddit"


@pytest.mark.parametrize(("op", "rule"), [
    ("usePolarisLikeMediaXIGLikeMutation", "instagram.like"),
    ("usePolarisFollowMutation", "instagram.follow"),
    ("PolarisPostCommentInputRevampedMutation", "instagram.comment"),
    ("PolarisFeedRootPaginationCachedQuery_subscribe", "instagram.read.graphql"),
    ("PolarisUserHoverCardContentV2Query", "instagram.read.graphql"),
])
def test_instagram_mutations_are_known_by_name_from_the_header_or_the_form(op, rule):
    for header in (True, False):
        headers, body = meta_form(op, {"input": {"media_id": "1"}}, header=header)
        for url in (IG, "https://www.instagram.com/graphql/query"):
            found = classify(url, "POST", headers, body=body)
            assert found is not None and found.id == rule, (op, header, url)


def test_a_comment_or_a_like_is_no_longer_taken_for_a_read():
    """The gap observe mode found: before, every Instagram GraphQL call was a read."""
    headers, body = meta_form("PolarisPostCommentInputRevampedMutation", {"data": {"comment_text": "hi"}})
    assert classify(IG, "POST", headers, body=body).effect == "commit"
    headers, body = meta_form("usePolarisLikeMediaXIGLikeMutation", {"input": {"media_id": "1"}})
    assert classify(IG, "POST", headers, body=body).effect == "write"


def test_the_body_is_read_only_when_a_rule_needs_its_operation():
    calls = []

    def body():
        calls.append(1)
        return reddit("CreatePost")

    assert classify("https://www.example.com/svc/shreddit/graphql", "POST", JSON, body=body) is None
    assert classify("https://www.reddit.com/api/comment", "POST", JSON, body=body).id == "reddit.comment.oldreddit"
    assert calls == []
    assert classify(REDDIT, "POST", JSON, body=body).id == "reddit.post"
    assert calls == [1]  # once, however many rules ask


def test_request_op_reads_names_never_values():
    assert request_op({"x-fb-friendly-name": "CometUFIFeedbackReactMutation"}, None) == "CometUFIFeedbackReactMutation"
    assert request_op(JSON, b'{"operationName": "Publish", "variables": {}}') == "Publish"
    assert request_op(JSON, b'[{"operation": "CreatePost"}]') == "CreatePost"
    assert request_op(JSON, b'{"operation": "not a name; drop table"}') is None
    assert request_op(FORM, urlencode({"fb_api_req_friendly_name": "X"}).encode()) == "X"
    assert request_op(FORM, b"a=1&b=2") is None
    assert request_op({"content-type": "multipart/form-data"}, b"--x\r\n") is None
    assert request_op(JSON, b"{" + b" " * (300 * 1024) + b"}") is None  # an upload is not parsed


@pytest.mark.parametrize(("op", "rule"), [
    ("CometUFIFeedbackReactMutation", "facebook.like"),
    ("CometUserFollowMutation", "facebook.follow"),
    ("useCometUFICreateCommentMutation", "facebook.comment"),
    ("ComposerStoryCreateMutation", "facebook.post"),
    ("CometUFILiveTypingBroadcastMutation_StartMutation", "facebook.read.graphql"),
    ("CometNewsFeedPaginationQuery", "facebook.read.graphql"),
])
def test_facebook_actions_are_known_by_name(op, rule):
    headers, body = meta_form(op, {"input": {"feedback_id": "1"}})
    found = classify("https://www.facebook.com/api/graphql/", "POST", headers, body=body)
    assert found is not None and found.id == rule


def test_a_dotted_request_id_names_the_operation_and_a_uuid_does_not():
    """LinkedIn's web client posts every action to one URL; requestId names the action."""
    body = json.dumps({"requestId": "com.linkedin.sdui.requests.feed.react", "serverRequest": {}}).encode()
    assert request_op(JSON, body) == "com.linkedin.sdui.requests.feed.react"
    for rid in ("6f1c2d3e-aaaa-bbbb-cccc-1234567890ab", "plain", "a.b", "hello world.x.y"):
        assert request_op(JSON, json.dumps({"requestId": rid}).encode()) is None


def test_a_reddit_comment_is_a_form_to_its_own_url():
    found = classify("https://www.reddit.com/svc/shreddit/t3_1demo02/create-comment", "POST", FORM,
                     body=b"content=x&mode=richText&richTextMedia=&recaptchaToken=r&csrf_token=c")
    assert found is not None and found.id == "reddit.comment" and found.effect == "commit"
    # typing a comment asks Reddit's automations about the draft: not a publish
    assert classify(REDDIT, "POST", JSON, body=reddit("EvaluateCommentAutomationsByPostId")).effect == "read"


def test_a_facebook_photo_upload_is_a_draft():
    found = classify("https://upload.facebook.com/ajax/react_composer/attachments/photo/upload", "POST",
                     {"Content-Type": "multipart/form-data; boundary=x"})
    assert found is not None and found.id == "facebook.draft.upload" and found.effect == "write"


@pytest.mark.parametrize(("request_id", "rule"), [
    ("com.linkedin.sdui.requests.sharing.CreatePost", "linkedin.post"),
    ("com.linkedin.sdui.comments.createComment", "linkedin.comment"),
    ("com.linkedin.sdui.reactions.create", "linkedin.like"),
    ("com.linkedin.sdui.requests.mynetwork.addaUpdateFollowState", "linkedin.follow"),
    ("com.linkedin.sdui.requests.comments.courtesyReminder", "linkedin.read.rsc"),
    ("com.linkedin.sdui.feed.update.comments.fetchComment", "linkedin.read.rsc"),
])
def test_linkedin_actions_are_known_by_their_request_id(request_id, rule):
    body = json.dumps({"requestId": request_id, "serverRequest": {"requestId": request_id}, "states": []}).encode()
    found = classify("https://www.linkedin.com/flagship-web/rsc-action/actions/server-request", "POST", JSON, body=body)
    assert found is not None and found.id == rule


def test_a_linkedin_image_upload_is_a_draft():
    found = classify("https://www.linkedin.com/dms-uploads/sp/sync/v2/D4D/uploaded-image/B4D/0", "PUT",
                     {"Content-Type": "image/jpeg"})
    assert found is not None and found.id == "linkedin.draft.upload"


# ---- a connector run's own commit request (FlowCommit, LaneGuard.scope_flow) -------------------

LI_RSC = "https://www.linkedin.com/flagship-web/rsc-action/actions/server-request"


def linkedin(op: str) -> bytes:
    return json.dumps({"requestId": op, "serverRequest": {"requestId": op}}).encode()


def _declared(site: str, op: str) -> egress.FlowCommit:
    from agentos.runner.connectors.compile import compile_path

    root = Path(__file__).resolve().parents[2] / "connectors" / site
    request = compile_path(root).operations[op].definition["connector"]["commit"]
    return egress.FlowCommit.of(f"connector.{site}.{op}", (request or {}).get("request"))


def _problem(commit: egress.FlowCommit, url: str, headers: dict[str, str], body: bytes) -> str | None:
    return commit.problem(classify(url, "POST", headers, body=body), url, "POST", headers, lambda: body)


def test_a_like_run_may_send_its_create_and_never_the_delete_the_rule_also_takes():
    react = _declared("linkedin", "react")
    assert _problem(react, LI_RSC, JSON, linkedin("com.linkedin.sdui.reactions.create")) is None
    for undo in ("com.linkedin.sdui.reactions.delete", "com.linkedin.sdui.reactions.update"):
        assert classify(LI_RSC, "POST", JSON, body=linkedin(undo)).id == "linkedin.like"   # the rule takes both
        why = _problem(react, LI_RSC, JSON, linkedin(undo))
        assert why is not None and "not the operation it declares" in why
    # another commit on the same URL (a comment) is not the like's either
    assert _problem(react, LI_RSC, JSON, linkedin("com.linkedin.sdui.comments.createComment")) is not None
    # reads and unknown requests are not the scope's business (the listener and the gate judge them)
    assert _problem(react, LI_RSC, JSON, linkedin("com.linkedin.sdui.hovercard.fetchHovercard")) is None


def test_a_join_run_may_subscribe_and_never_leave():
    join = _declared("reddit", "join")
    sub = reddit("UpdateSubredditSubscriptions", inputs=[{"subredditId": "t5_1", "subscribeState": "SUBSCRIBED"}])
    leave = reddit("UpdateSubredditSubscriptions", inputs=[{"subredditId": "t5_1", "subscribeState": "NONE"}])
    mixed = reddit("UpdateSubredditSubscriptions", inputs=[{"subredditId": "t5_1", "subscribeState": "SUBSCRIBED"},
                                                           {"subredditId": "t5_2", "subscribeState": "NONE"}])
    assert _problem(join, REDDIT, JSON, sub) is None
    for body in (leave, mixed, reddit("UpdateSubredditSubscriptions")):
        why = _problem(join, REDDIT, JSON, body)
        assert why is not None and "subscribeState" in why
    assert _problem(join, REDDIT, JSON, reddit("UpdatePostVoteState", postId="t3_1", voteState="UP")) is not None


def test_a_post_run_may_upload_its_photo_and_a_read_run_writes_nothing():
    post = _declared("linkedin", "post")
    put = "https://www.linkedin.com/dms-uploads/abc"
    assert post.problem(classify(put, "PUT"), put, "PUT", {}, lambda: b"\x89PNG") is None   # a draft: the upload
    assert _problem(post, LI_RSC, JSON, linkedin("com.linkedin.sdui.requests.sharing.CreatePost")) is None
    feed = egress.FlowCommit.of("connector.linkedin.feed", None)
    why = _problem(feed, LI_RSC, JSON, linkedin("com.linkedin.sdui.reactions.create"))
    assert why is not None and "declares no commit request" in why


async def test_the_guard_aborts_a_runs_other_writes_before_they_leave_and_leaves_agents_alone(tmp_path):
    from test_egress import AGENT, FakePage, FakeRequest, FakeRoute, make_guard

    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=AGENT)
    page = FakePage("https://www.linkedin.com/feed/")
    async with guard.flow(page):
        await guard.scope_flow(_declared("linkedin", "react"))
        [(_, handler)] = ctx.routes                       # the rule hosts' route is on for the run
        undo = FakeRequest(LI_RSC, "POST", frame=page.main_frame, headers=JSON,
                           body=linkedin("com.linkedin.sdui.reactions.delete"))
        await handler(route := FakeRoute(), undo)
        ctx.emit("request", undo)
        assert route.aborted
        [block] = guard.blocks_since(0)
        assert block.rule == "flow_commit" and block.prevented and block.effect_rule == "linkedin.like"
        like = FakeRequest(LI_RSC, "POST", frame=page.main_frame, headers=JSON,
                           body=linkedin("com.linkedin.sdui.reactions.create"))
        await handler(route := FakeRoute(), like)
        assert not route.aborted
    assert ctx.routes == [] and guard._flow_commit is None    # gone with the run
    # an agent's own browsing is not scoped: with no run on the lane nothing is routed for it
    async with guard.acting(route=False):
        pass
    assert ctx.routes == []


# ---- the profile edits (brief B2): to capture, so these names are the rules' own guesses

PROFILE_OPS = {
    "name": "com.linkedin.sdui.requests.profile.updateProfileName",
    "headline": "com.linkedin.sdui.requests.profile.updateHeadline",
    "about": "com.linkedin.sdui.requests.profile.updateAbout",
    "photo": "com.linkedin.sdui.requests.profile.updateProfilePhoto",
    "cover": "com.linkedin.sdui.requests.profile.updateBackgroundPhoto",
}


@pytest.mark.parametrize("field", list(PROFILE_OPS))
def test_a_profile_edit_is_a_commit_of_its_own_rule_and_nothing_verifies_it(field):
    rule = classify(LI_RSC, "POST", JSON, body=linkedin(PROFILE_OPS[field]))
    assert rule is not None and rule.id == f"linkedin.profile.{field}" and rule.kind == "profile"
    assert rule.effect == "commit" and rule.verified == ""


def test_a_profile_photos_upload_and_registration_stay_drafts():
    """A registration of the photo's upload is a draft (it names the upload), never the edit."""
    for name in ("com.linkedin.sdui.requests.profile.registerMediaUpload",
                 "com.linkedin.sdui.requests.sharing.registerMediaUpload"):
        assert classify(LI_RSC, "POST", JSON, body=linkedin(name)).id == "linkedin.draft.register"
    put = "https://www.linkedin.com/dms-uploads/sp/v2/D4D22AQPROFILE01/profile-displayphoto/0"
    assert classify(put, "PUT", {"content-type": "image/jpeg"}).id == "linkedin.draft.upload"
    # a name without the edit's verb is no profile edit: it is an unknown write of the catch-all
    rule = classify(LI_RSC, "POST", JSON, body=linkedin("com.linkedin.sdui.requests.profile.ProfilePhotoFrames"))
    assert rule is not None and rule.id == "linkedin.read.rsc"


@pytest.mark.parametrize("name", [
    "editProfileNameForm", "saveDraftHeadlineSuggestions", "updateBackgroundImageUploadPrompt",
    "updateProfileSectionsRead", "editPhotoFrames", "fetchAboutSection", "editHeadline", "updateNameForm",
    "updateProfilePhotoFrames", "saveAboutSuggestions", "updateHeadlineDraft", "updateProfilePhotoUploadPrompt",
])
def test_a_name_around_a_profile_field_is_no_profile_edit(name):
    """Only a verb that writes (update, save) followed by the field and nothing after it: a form, a
    suggestion, a prompt or a read around the field is not taken for the edit (the gate holds it
    as an unknown write instead: linkedin.read.rsc is a strict_ops read)."""
    rule = classify(LI_RSC, "POST", JSON, body=linkedin(f"com.linkedin.sdui.requests.profile.{name}"))
    assert rule is not None and rule.kind != "profile" and rule.id == "linkedin.read.rsc", (name, rule)
    ok, _ = gql_verdict("linkedin", JSON, linkedin(f"com.linkedin.sdui.requests.profile.{name}"))
    assert not ok                                     # and the gate does not take it for a known read


@pytest.mark.parametrize("name,field", [
    ("saveProfileName", "name"), ("updateMemberFullName", "name"), ("saveHeadline", "headline"),
    ("updateProfileSummary", "about"), ("updateAboutSection", "about"), ("saveProfileCoverImage", "cover"),
    ("updateDisplayPicture", "photo"),
])
def test_the_guessed_profile_names_that_write(name, field):
    rule = classify(LI_RSC, "POST", JSON, body=linkedin(f"com.linkedin.sdui.requests.profile.{name}"))
    assert rule is not None and rule.id == f"linkedin.profile.{field}"


@pytest.mark.parametrize("field", list(PROFILE_OPS))
def test_a_profile_run_may_send_only_its_own_edit(field):
    """Flow commit routing (egress-guard 8.6): during profile_<field> the page may send that edit
    and its uploads; any other profile edit, a post, a like or a follow is refused before it leaves."""
    run = _declared("linkedin", f"profile_{field}")
    assert _problem(run, LI_RSC, JSON, linkedin(PROFILE_OPS[field])) is None
    others = [op for f, op in PROFILE_OPS.items() if f != field] + [
        "com.linkedin.sdui.requests.sharing.CreatePost", "com.linkedin.sdui.reactions.create",
        "com.linkedin.sdui.requests.mynetwork.addaUpdateFollowState"]
    for other in others:
        why = _problem(run, LI_RSC, JSON, linkedin(other))
        assert why is not None and "not the operation it declares" in why, other
    put = "https://www.linkedin.com/dms-uploads/sp/v2/D4D22AQPROFILE01/uploaded-image/0"
    assert run.problem(classify(put, "PUT"), put, "PUT", {}, lambda: b"\x89PNG") is None
    register = linkedin("com.linkedin.sdui.requests.profile.registerMediaUpload")
    assert _problem(run, LI_RSC, JSON, register) is None
