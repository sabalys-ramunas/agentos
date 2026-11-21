"""The commit gate reads what a publish request will publish (openajar.node.publish, AUD-03): one
recorded request shape per verified commit rule (a canary run of 2026-11-04: the field names the
throwaway accounts' real publishes sent; values are made up), sent twice the way a site sends one
post twice (new nonces, tokens, timestamps and client ids each time). The digest must be the same
for both and change when the text does; the card's parts come out of the right fields."""

from __future__ import annotations

import itertools
import json
import secrets
from urllib.parse import urlencode

import pytest

from openajar.node import egress, publish

_n = itertools.count(1)


def rule(rule_id: str) -> egress.EffectRule:
    return next(r for r in egress.EFFECT_RULES if r.id == rule_id)


def meta_form(friendly: str, variables: dict, **extra: str) -> tuple[dict, bytes]:
    """A Meta web client's GraphQL form: the request parameters change on every call."""
    n = next(_n)
    form = {"av": "61596476060059", "__user": "61596476060059", "__a": "1", "__req": f"{n:x}",
            "__hs": f"20031.HYP:comet_pkg.2.1...{n}", "dpr": "1", "__ccg": "GOOD", "__rev": "1017935553",
            "__s": secrets.token_hex(4), "__hsi": str(7433000000000000000 + n), "__dyn": secrets.token_hex(20),
            "__csr": secrets.token_hex(20), "__comet_req": "15", "fb_dtsg": secrets.token_hex(12),
            "jazoest": str(25000 + n), "lsd": secrets.token_hex(6), "__spin_r": "1017935553", "__spin_b": "trunk",
            "__spin_t": str(1730700000 + n), "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": friendly, "variables": json.dumps(variables), "server_timestamps": "true",
            "doc_id": "9123456789012345", **extra}
    headers = {"content-type": "application/x-www-form-urlencoded", "x-fb-friendly-name": friendly}
    return headers, urlencode(form).encode()


def rich(text: str) -> str:
    return json.dumps({"document": [{"e": "par", "c": [{"e": "text", "t": line}]} for line in text.split("\n")]})


# ---- one recorded shape per verified commit rule ----------------------------------------------

def reddit_post(title="Plain test post from a new account", body="Just checking that posting works."):
    payload = {"operation": "CreatePost", "variables": {"input": {
        "title": title, "content": {"richText": rich(body)}, "subredditName": "test", "isNsfw": False,
        "isSpoiler": False, "isCommercialCommunication": False, "targetLanguage": "",
        "recaptchaToken": secrets.token_urlsafe(40), "correlationId": secrets.token_hex(16)}},
        "csrf_token": secrets.token_hex(16)}
    return ("https://www.reddit.com/svc/shreddit/graphql", {"content-type": "application/json"},
            json.dumps(payload).encode())


def reddit_comment(text="Labai atmosferiškos nuotraukos, ypač ant juostos."):
    form = {"content": rich(text), "mode": "richText", "richTextMedia": "[]",
            "recaptchaToken": secrets.token_urlsafe(40), "csrf_token": secrets.token_hex(16)}
    return ("https://www.reddit.com/svc/shreddit/t3_1demo02/create-comment",
            {"content-type": "application/x-www-form-urlencoded"}, urlencode(form).encode())


def facebook_post(text="Green hills on a calm morning.", photo="122093066475549202", audience="FRIENDS"):
    n = next(_n)
    variables = {"input": {
        "composer_entry_point": "inline_composer", "composer_source_surface": "newsfeed", "composer_type": "feed",
        "idempotence_token": f"{secrets.token_hex(16)}_FEED", "source": "WWW",
        "attachments": [{"photo": {"id": photo}}],
        "audience": {"privacy": {"allow": [], "base_state": audience, "deny": [], "tag_expansion_state": "UNSPECIFIED"}},
        "message": {"ranges": [], "text": text}, "with_tags_ids": None, "inline_activities": [],
        "text_format_preset_id": "0", "logging": {"composer_session_id": secrets.token_hex(16)},
        "navigation_data": {"attribution_id_v2": f"CometHomeRoot.react,comet.home,tap_tabbar,{1730700000 + n},1"},
        "tracking": [None], "event_share_metadata": {"surface": "newsfeed"}, "actor_id": "61596476060059",
        "client_mutation_id": str(n)},
        "feedLocation": "NEWSFEED", "feedbackSource": 0, "focusCommentID": None, "scale": 1,
        "privacySelectorRenderLocation": "COMET_STREAM", "renderLocation": "homepage_stream",
        "useDefaultActor": False}
    headers, body = meta_form("ComposerStoryCreateMutation", variables)
    return "https://www.facebook.com/api/graphql/", headers, body


def facebook_comment(text="Thanks for the roundup.", feedback="ZmVlZGJhY2s6MTIzNDU2Nzg5"):
    n = next(_n)
    variables = {"input": {
        "attachments": None, "feedback_id": feedback, "message": {"ranges": [], "text": text},
        "actor_id": "61596476060059", "client_mutation_id": str(n), "feedback_source": "OBJECT",
        "idempotence_token": f"client:{secrets.token_hex(8)}", "session_id": secrets.token_hex(8),
        "vod_video_timestamp": None, "is_tracking_encrypted": True, "tracking": [secrets.token_hex(12)],
        "attribution_id_v2": f"CometSinglePostDialogRoot.react,comet.post.single_dialog,via_cold_start,{1730700000 + n}"},
        "feedLocation": "DEDICATED_COMMENTING_SURFACE", "feedbackSource": 110, "focusCommentID": None, "scale": 1,
        "useDefaultActor": False, "UFI2CommentsProvider_commentsKey": "CometSinglePostDialogRoute"}
    headers, body = meta_form("useCometUFICreateCommentMutation", variables)
    return "https://www.facebook.com/api/graphql/", headers, body


def instagram_comment(text="Puikus pasirinkimas kelionei", media="3456789012345678901"):
    variables = {"connections": [f"client:root:__PolarisPostComments__xdt_api__v1__media__{media}__comments__"],
                 "data": {"comment_text": text, "media_id": media},
                 "root_connections": [f"client:root:{secrets.token_hex(6)}"]}
    headers, body = meta_form("PolarisPostCommentInputRevampedMutation", variables)
    return "https://www.instagram.com/graphql/query", headers, body


def linkedin(request_id: str, payload: dict):
    """LinkedIn's server-driven client: JSON to one URL, the action named in requestId. The
    canary recorded the operation names, not every field name: these fields are the shape its
    server requests take (states, requested arguments, a tracking id)."""
    body = {"requestId": request_id, "serverRequest": {
        "$type": "proto.sdui.actions.requests.RequestedArguments",
        "requestedArguments": {"$type": "proto.sdui.actions.requests.RequestedArguments", "payload": payload}},
        "states": [], "trackingId": secrets.token_urlsafe(16), "clientRequestId": secrets.token_hex(8)}
    return ("https://www.linkedin.com/flagship-web/rsc-action/actions/server-request",
            {"content-type": "application/json"}, json.dumps(body).encode())


def linkedin_post(text="Evening light over the hills. A quiet end to a busy day.",
                  media="urn:li:digitalmediaAsset:D4D22AQHk1x2y3z4"):
    return linkedin("com.linkedin.sdui.requests.sharing.CreatePost",
                    {"commentary": text, "visibility": "ANYONE", "media": [{"mediaUrn": media}]})


def linkedin_comment(text="Going from 5 m to 113 m outdoors is impressive.",
                     thread="urn:li:activity:7392000000000000000"):
    return linkedin("com.linkedin.sdui.comments.createComment", {"commentary": text, "threadUrn": thread})


SHAPES = {
    "reddit.post": (reddit_post, {"body": "Another body"}),
    "reddit.comment": (reddit_comment, {"text": "Kitas tekstas"}),
    "facebook.post": (facebook_post, {"text": "Brown hills"}),
    "facebook.comment": (facebook_comment, {"text": "Thanks, but no."}),
    "instagram.comment": (instagram_comment, {"text": "Kitas komentaras"}),
    "linkedin.post": (linkedin_post, {"text": "Morning light"}),
    "linkedin.comment": (linkedin_comment, {"text": "Impressive range."}),
}


def cap(rule_id: str, shape) -> publish.Capture:
    url, headers, body = shape
    found = egress.classify(url, "POST", headers, body=body)
    assert found is not None and found.id == rule_id  # the recorded shape still matches its rule
    return publish.capture(found, url, "POST", headers, body)


def test_every_verified_commit_rule_has_a_recorded_shape_here():
    verified = {r.id for r in egress.EFFECT_RULES if r.effect == "commit" and r.verified}
    assert verified == set(SHAPES)


@pytest.mark.parametrize("rule_id", sorted(SHAPES))
def test_two_identical_clicks_have_one_digest_and_other_text_another(rule_id):
    make, change = SHAPES[rule_id]
    a, b = cap(rule_id, make()), cap(rule_id, make())
    assert a.record == b.record and a.digest("test/x") == b.digest("test/x")
    assert a.digest("test/x") != a.digest("test/y")  # bound to the lane it publishes through
    other = cap(rule_id, make(**change))
    assert other.digest("test/x") != a.digest("test/x")


@pytest.mark.parametrize("rule_id", sorted(SHAPES))
def test_no_volatile_value_reaches_the_record(rule_id):
    record = json.dumps(cap(rule_id, SHAPES[rule_id][0]()).record)
    for word in ("csrf", "recaptcha", "fb_dtsg", "jazoest", "idempotence", "client_mutation", "tracking",
                 "attribution", "session_id", "doc_id", "__req", "correlation", "clientRequestId", "requestId"):
        assert word not in record, word


def test_reddit_post_card_parts():
    rec = cap("reddit.post", reddit_post(body="Line one\nLine two")).record
    assert rec["title"] == "Plain test post from a new account" and rec["target"] == "test"
    assert rec["text"] == "Line one\nLine two" and rec["kind"] == "post"
    assert rec["flags"] == {"isNsfw": False, "isSpoiler": False}
    # the rich text is bound as sent, not only its plain text: bold or a link inside changes the digest
    assert any(k.endswith("content.richText") for k in rec["fields"])


def test_reddit_comment_names_the_post_from_its_url():
    rec = cap("reddit.comment", reddit_comment()).record
    assert rec["target"] == "t3_1demo02" and rec["text"].startswith("Labai") and rec["kind"] == "comment"


def test_facebook_post_card_parts():
    rec = cap("facebook.post", facebook_post()).record
    assert rec["text"] == "Green hills on a calm morning." and rec["as"] == "61596476060059"
    assert rec["media"] == [{"ref": "122093066475549202", "seen": False}]
    assert rec["visibility"] == "FRIENDS"
    public = cap("facebook.post", facebook_post(audience="EVERYONE")).record
    assert public["visibility"] == "EVERYONE"


def test_facebook_post_with_another_photo_or_a_link_attachment_is_other_content():
    base = cap("facebook.post", facebook_post()).digest("test/facebook")
    assert cap("facebook.post", facebook_post(photo="999")).digest("test/facebook") != base
    url, headers, body = facebook_post()
    form = dict(__import__("urllib.parse").parse.parse_qsl(body.decode()))
    variables = json.loads(form["variables"])
    variables["input"]["attachments"].append({"link": {"share_scrape_data": '{"share_params":{"urls":["https://evil.example"]}}'}})
    form["variables"] = json.dumps(variables)
    injected = cap("facebook.post", (url, headers, urlencode(form).encode()))
    assert injected.digest("test/facebook") != base and "evil.example" in json.dumps(injected.record)


def test_facebook_and_instagram_comments_name_what_they_answer():
    fb = cap("facebook.comment", facebook_comment()).record
    assert fb["target"] == "ZmVlZGJhY2s6MTIzNDU2Nzg5" and fb["text"] == "Thanks for the roundup."
    ig = cap("instagram.comment", instagram_comment()).record
    assert ig["target"] == "3456789012345678901" and ig["text"] == "Puikus pasirinkimas kelionei"


def test_linkedin_post_binds_its_media_and_visibility():
    rec = cap("linkedin.post", linkedin_post()).record
    assert rec["text"].startswith("Evening light") and rec["visibility"] == "ANYONE"
    assert rec["media"] == [{"ref": "urn:li:digitalmediaAsset:D4D22AQHk1x2y3z4", "seen": False}]
    other = cap("linkedin.post", linkedin_post(media="urn:li:digitalmediaAsset:OTHER0000"))
    assert other.digest("x/y") != cap("linkedin.post", linkedin_post()).digest("x/y")


def test_a_media_reference_binds_the_upload_the_guard_saw():
    boundary = "----x"
    photo = b"\xff\xd8\xff\xe0 the photo bytes"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"profile_id\"\r\n\r\n61596476060059\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"upload_id\"\r\n\r\njsc_c_{secrets.token_hex(3)}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"source\"; filename=\"blob\"\r\n"
            "Content-Type: image/jpeg\r\n\r\n").encode() + photo + f"\r\n--{boundary}--\r\n".encode()
    up = publish.upload_of("facebook.draft.upload", "https://upload.facebook.com/ajax/react_composer/attachments/photo/upload",
                           {"content-type": f"multipart/form-data; boundary={boundary}"}, body)
    assert up is not None and up.size == len(photo)
    up.ids |= publish.response_ids('for (;;);{"payload":{"photoID":"122093066475549202","imageSrc":"x"}}')
    url, headers, req = facebook_post()
    rec = publish.capture(rule("facebook.post"), url, "POST", headers, req, uploads=[up]).record
    assert rec["media"] == [{"sha256": up.sha256, "size": len(photo)}]


def test_instagram_photo_publish_binds_its_upload_and_caption():
    def configure(caption: str) -> tuple[str, dict, bytes]:
        form = {"caption": caption, "upload_id": "1730700000123", "source_type": "library",
                "disable_comments": "0", "jazoest": secrets.token_hex(3)}
        return ("https://www.instagram.com/api/v1/media/configure/",
                {"content-type": "application/x-www-form-urlencoded"}, urlencode(form).encode())

    a, b = cap("instagram.post", configure("Quiet hills")), cap("instagram.post", configure("Quiet hills"))
    assert a.record["media"] == [{"ref": "1730700000123", "seen": False}] and a.record["text"] == "Quiet hills"
    assert a.digest("x/y") == b.digest("x/y") and not a.verified


def test_bodies_that_cannot_be_bound_whole_are_refused():
    """Never a prefix: a body larger than PARSE_MAX, in a format the node cannot read, with more
    than MAX_FIELDS leaves or nested deeper than MAX_DEPTH is refused (the gate says why)."""
    with pytest.raises(publish.Unbindable, match="larger than"):
        publish.decode({"content-type": "application/json"}, b"x" * (publish.PARSE_MAX + 1))
    with pytest.raises(publish.Unbindable, match="format the node cannot read"):
        publish.decode({"content-type": "application/octet-stream"}, b"\x00\x01binary")
    with pytest.raises(publish.Unbindable, match="more than"):
        publish.flatten({f"k{i}": i for i in range(publish.MAX_FIELDS + 1)})
    deep: dict = {"text": "x"}
    for _ in range(publish.MAX_DEPTH + 1):
        deep = {"a": deep}
    with pytest.raises(publish.Unbindable, match="deeper"):
        publish.flatten(deep)
    # a multipart body's file parts are hashed, whatever their size: only its text parts count
    big = b"--b\r\nContent-Disposition: form-data; name=\"f\"; filename=\"v.mp4\"\r\n\r\n" + b"v" * (
        publish.PARSE_MAX + 10) + b"\r\n--b--\r\n"
    fields, files = publish.decode({"content-type": "multipart/form-data; boundary=b"}, big)
    assert fields == {} and files[0]["size"] == publish.PARSE_MAX + 10


@pytest.mark.parametrize("key", ["scheduled_publish_time", "publish_time", "audience_tracking_mode",
                                 "logging_target", "session_id_hint", "target_ts", "visibility_uuid"])
def test_a_field_is_volatile_only_by_its_exact_path_never_by_a_part_of_its_name(key):
    """The review's probe: `variables.input.scheduled_publish_time` was dropped as volatile (it has
    `_time` in it), so a post scheduled for another time had the same digest and no card row."""
    def post(value: str):
        url, headers, body = facebook_post()
        form = dict(__import__("urllib.parse").parse.parse_qsl(body.decode()))
        variables = json.loads(form["variables"])
        variables["input"][key] = value
        form["variables"] = json.dumps(variables)
        return cap("facebook.post", (url, headers, urlencode(form).encode()))

    a, b = post("1767225600"), post("1767312000")
    assert a.digest("test/facebook") != b.digest("test/facebook")
    assert a.record["fields"][f"variables.input.{key}"] == "1767225600"


def test_the_volatile_keys_are_exact_paths_and_names():
    assert all(isinstance(p, str) and "*" not in p and "(" not in p for ps in publish.RULE_VOLATILE.values() for p in ps)
    assert all(isinstance(n, str) for n in publish.VOLATILE_NAMES)
    kept = publish.flatten({"variables": {"input": {"scheduled_publish_time": 1, "audience": {"tracking": 2},
                                                    "client_mutation_id": "3", "csrf_token_hint": 4}}},
                           rule="facebook.post")
    assert kept == {"variables.input.scheduled_publish_time": 1, "variables.input.audience.tracking": 2,
                    "variables.input.csrf_token_hint": 4}
