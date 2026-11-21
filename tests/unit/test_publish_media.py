"""Media a publish names, bound by the file the node saw uploaded (openajar.node.publish, AUD-03,
docs/design/egress-guard.md section 8.2): Reddit's S3 uploads through its upload lease, LinkedIn's
media registrations (the urn its answer names binds the file seen at the upload URL it names), and
stickers and GIFs of a site's own catalog, bound by id with a plain label.

None of these shapes was checked on a real account: Reddit's lease and LinkedIn's registration
answers are the clients' and APIs' public shapes, with made-up values. The first real post with
an image on each site should be checked against them (observe mode records the names)."""

from __future__ import annotations

import asyncio
import json
import secrets
from urllib.parse import parse_qs, urlencode

from test_commit_gate_guard import Gate
from test_egress import COMMITTER, FakeRequest, FakeRoute, make_guard
from test_publish_normalize import facebook_comment, linkedin, linkedin_post, meta_form, rich

from openajar.node import egress, publish
from openajar.policy.cards import _media_rows

PHOTO = b"\x89PNG\r\n\x1a\n the photo's bytes"
S3 = "https://reddit-uploaded-media.s3-accelerate.amazonaws.com/"
GQL = "https://www.reddit.com/svc/shreddit/graphql"


def rule(rule_id: str) -> egress.EffectRule:
    return next(r for r in egress.EFFECT_RULES if r.id == rule_id)


def multipart(fields: dict[str, str], file: bytes, boundary: str = "----r") -> tuple[dict, bytes]:
    body = b"".join(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                    for k, v in fields.items())
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"hills.png\"\r\n"
             "Content-Type: image/png\r\n\r\n").encode() + file + f"\r\n--{boundary}--\r\n".encode()
    return {"content-type": f"multipart/form-data; boundary={boundary}"}, body


def capture(rule_id: str, url: str, headers: dict, body: bytes, uploads=()) -> publish.Capture:
    found = egress.classify(url, "POST", headers, body=body)
    assert found is not None and found.id == rule_id
    return publish.capture(found, url, "POST", headers, body, uploads=list(uploads))


# ---- Reddit ----------------------------------------------------------------------------------

LEASE = {"data": {"createMediaUploadLease": {"ok": True, "errors": None, "mediaId": "kz7r2q9xw1ab", "uploadLease": {
    "uploadLeaseUrl": "https://reddit-uploaded-media.s3-accelerate.amazonaws.com",
    "uploadLeaseHeaders": [{"header": "key", "value": "rte_images/kz7r2q9xw1ab"},
                           {"header": "Content-Type", "value": "image/png"},
                           {"header": "x-amz-signature", "value": secrets.token_hex(32)}]}}}}
OLD_ASSET = {"args": {"action": "//reddit-uploaded-media.s3-accelerate.amazonaws.com", "fields": [
    {"name": "key", "value": "rte_images/p0q9w8e7r6t5"}, {"name": "policy", "value": secrets.token_hex(20)}]},
    "asset": {"asset_id": "p0q9w8e7r6t5", "processing_state": "incomplete", "payload": {"filepath": "hills.png"}}}


def s3_upload(key: str, file: bytes = PHOTO) -> publish.Upload:
    headers, body = multipart({"key": key, "x-amz-credential": "AKIA/2026", "policy": secrets.token_hex(8),
                               "x-amz-signature": secrets.token_hex(16)}, file)
    found = egress.classify(S3, "POST", headers, body=body)
    assert found is not None and found.id == "reddit.draft.upload"
    up = publish.upload_of(found.id, S3, headers, body)
    assert up is not None and up.size == len(file)
    return up


def reddit_image_post(media_id: str, text: str = "Hills after the rain.") -> tuple[str, dict, bytes]:
    doc = json.loads(rich(text))
    doc["document"].append({"e": "img", "id": media_id, "c": "a caption"})
    payload = {"operation": "CreatePost", "variables": {"input": {
        "title": "Hills", "content": {"richText": json.dumps(doc)}, "subredditName": "test",
        "recaptchaToken": secrets.token_urlsafe(20), "correlationId": secrets.token_hex(8)}},
        "csrf_token": secrets.token_hex(8)}
    return GQL, {"content-type": "application/json"}, json.dumps(payload).encode()


def test_the_reddit_upload_lease_and_its_s3_post_are_drafts_on_the_gates_route():
    lease = json.dumps({"operation": "CreateMediaUploadLease", "variables": {"input": {"mimetype": "PNG"}}}).encode()
    assert egress.classify(GQL, "POST", {"content-type": "application/json"}, body=lease).id == "reddit.draft.register"
    assert egress.classify("https://www.reddit.com/api/media/asset.json", "POST").id == "reddit.draft.register_asset"
    assert egress.gate_pattern().match(S3)  # held for the gate like any request to a rule host
    assert "frame:reddit-uploaded-media.s3-accelerate.amazonaws.com" in egress.SUGGESTED_SITES["reddit"]


def test_a_reddit_image_post_binds_the_file_its_lease_named():
    [reg] = publish.registrations(json.dumps(LEASE))
    assert reg.handles == {"key:rte_images/kz7r2q9xw1ab"} and reg.ids == {"kz7r2q9xw1ab"}
    up = s3_upload("rte_images/kz7r2q9xw1ab")
    assert reg.bind(up)
    rec = capture("reddit.post", *reddit_image_post("kz7r2q9xw1ab"), uploads=[up]).record
    assert rec["media"] == [{"sha256": up.sha256, "size": len(PHOTO)}] and publish.unbound_media(rec) == []
    assert rec["text"].startswith("Hills after the rain.")
    # another file under the same media id is other content; no upload at all is refused as unseen
    other = s3_upload("rte_images/kz7r2q9xw1ab", b"another file")
    assert capture("reddit.post", *reddit_image_post("kz7r2q9xw1ab"), uploads=[other]).record["media"] != rec["media"]
    unseen = capture("reddit.post", *reddit_image_post("kz7r2q9xw1ab")).record
    assert publish.unbound_media(unseen) == ["kz7r2q9xw1ab"]


def test_the_older_reddit_asset_lease_binds_too():
    [reg] = publish.registrations(json.dumps(OLD_ASSET))
    assert reg.handles == {"key:rte_images/p0q9w8e7r6t5"} and "p0q9w8e7r6t5" in reg.ids
    up = s3_upload("rte_images/p0q9w8e7r6t5")
    assert "p0q9w8e7r6t5" in up.ids  # the key's name: bound even when the lease's answer was not read
    rec = capture("reddit.post", *reddit_image_post("p0q9w8e7r6t5"), uploads=[up]).record
    assert rec["media"] == [{"sha256": up.sha256, "size": len(PHOTO)}]


def test_a_reddit_comment_with_a_giphy_gif_is_bound_by_id_with_a_label():
    doc = {"document": [{"e": "par", "c": [{"e": "text", "t": "Same."}]},
                        {"e": "gif", "id": "giphy|3o7TKSjRrfIPjeiVyM|downsized"}]}
    form = {"content": json.dumps(doc), "mode": "richText", "richTextMedia": "[]",
            "recaptchaToken": secrets.token_urlsafe(20), "csrf_token": secrets.token_hex(8)}
    url = "https://www.reddit.com/svc/shreddit/t3_1demo02/create-comment"
    rec = capture("reddit.comment", url, {"content-type": "application/x-www-form-urlencoded"},
                  urlencode(form).encode()).record
    [gif] = rec["media"]
    assert gif["catalog"] == "gif" and gif["ref"] == "giphy|3o7TKSjRrfIPjeiVyM|downsized"
    assert "Reddit's own picker" in gif["label"] and publish.unbound_media(rec) == []
    assert rec["fields"]["content"] == form["content"]  # the rich text itself stays bound as sent


async def test_the_guard_binds_a_reddit_upload_through_its_lease(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)

    class Response:
        def __init__(self, text: str) -> None:
            self._text = text

        async def text(self) -> str:
            return self._text

    lease = FakeRequest(GQL, "POST", headers={"content-type": "application/json"},
                        body=json.dumps({"operation": "CreateMediaUploadLease", "variables": {}}).encode())

    async def answer():
        return Response(json.dumps(LEASE))

    lease.response = answer
    headers, body = multipart({"key": "rte_images/kz7r2q9xw1ab", "policy": "p"}, PHOTO)
    upload = FakeRequest(S3, "POST", headers=headers, body=body)

    async def s3_answer():
        return Response("<PostResponse><Key>rte_images/kz7r2q9xw1ab</Key></PostResponse>")

    upload.response = s3_answer
    gate = Gate()
    async with guard.acting(gate=gate):
        for request in (lease, upload):
            route = FakeRoute()
            ctx.emit("request", request)
            await ctx.routes[0][1](route, request)
            assert not route.aborted  # drafts: they go
            for _ in range(5):
                await asyncio.sleep(0)
    assert gate.seen == [] and len(guard.uploads) == 1  # the lease is no upload of its own
    [up] = guard.uploads
    assert "kz7r2q9xw1ab" in up.ids and up.size == len(PHOTO)


# ---- LinkedIn ---------------------------------------------------------------------------------

ASSET = "urn:li:digitalmediaAsset:C4D22AQHk1x2y3z4"
IMAGE = "urn:li:image:D4E10AQFq9w8e7r6"
DMS = "https://www.linkedin.com/dms-uploads/sp/v2/C4D22AQHk1x2y3z4/uploaded-image/0"


def dms_put(url: str = DMS + "?ca=vector_feedshare&cn=uploads&v=beta&ut=2xyz") -> publish.Upload:
    found = egress.classify(url, "PUT", {"content-type": "image/jpeg"})
    assert found is not None and found.id == "linkedin.draft.upload"
    up = publish.upload_of(found.id, url, {"content-type": "image/jpeg"}, PHOTO)
    assert up is not None
    return up


def test_linkedin_registration_answers_name_the_urn_and_the_upload_url():
    """registerUpload (the API), initializeUpload (the Images API) and the older client's
    voyager answer: the urn binds the file seen at the upload URL it names."""
    shapes = [
        {"value": {"uploadMechanism": {"com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest": {
            "uploadUrl": DMS + "?ca=vector_feedshare&cn=uploads", "headers": {}}}, "asset": ASSET,
            "mediaArtifact": f"urn:li:digitalmediaMediaArtifact:({ASSET},urn:li:digitalmediaMediaArtifactClass:x)"}},
        {"value": {"uploadUrlExpiresAt": 1793826536758, "uploadUrl": DMS + "?ca=x", "image": ASSET}},
        {"data": {"value": {"urn": ASSET, "singleUploadUrl": DMS + "?sync=0", "type": "SINGLE"}}},
    ]
    for shape in shapes:
        regs = publish.registrations(json.dumps(shape))
        up = dms_put()
        up.ids.clear()  # bound through the answer alone, not the asset id in the URL
        assert any(r.bind(up) for r in regs), shape
        assert ASSET in up.ids


def test_a_linkedin_photo_post_binds_its_file_through_the_image_registration():
    """The canary saw registerMediaUpload, the PUT, then RegisterImageMedia; the post may name the
    image urn, which no PUT names. The image registration names the asset it is for, so its answer's
    urn binds the file the PUT of that asset carried."""
    up = dms_put()
    # registerMediaUpload's answer as a server component stream line
    stream = '0:["$","div",null,{}]\n1:' + json.dumps({"uploadUrl": DMS + "?ca=x", "urn": ASSET}) + "\n"
    for reg in publish.registrations(stream):
        reg.bind(up)
    assert ASSET in up.ids
    named = publish.named_media(json.dumps({"requestId": "x.RegisterImageMedia", "asset": ASSET}).encode())
    regs = publish.registrations(json.dumps({"value": {"image": IMAGE, "status": "AVAILABLE"}}), named=named)
    assert any(r.bind(up) for r in regs) and IMAGE in up.ids
    url, headers, body = linkedin_post(media=IMAGE)
    rec = capture("linkedin.post", url, headers, body, uploads=[up]).record
    assert rec["media"] == [{"sha256": up.sha256, "size": len(PHOTO)}]
    # the asset urn alone binds by the id its upload URL carries
    rec = capture("linkedin.post", *linkedin_post(media=ASSET), uploads=[dms_put()]).record
    assert publish.unbound_media(rec) == []


def test_an_answer_the_node_cannot_pair_binds_nothing():
    """Fail closed: a stream naming two urns and an upload URL cannot say which is the file's."""
    text = f"<html>{DMS}?a=1 {ASSET} urn:li:image:D4E10AQOTHER000</html>"
    assert publish.registrations(text) == []
    up = dms_put()
    up.ids.clear()
    rec = capture("linkedin.post", *linkedin_post(media=IMAGE), uploads=[up]).record
    assert publish.unbound_media(rec) == [IMAGE]


def test_two_uploads_and_two_urns_never_cross_bind():
    """The review's probe: an answer naming two upload URLs and two urns in one object (or in
    siblings no smaller object pairs) bound both urns to both uploads, and a post naming the first
    photo's urn was bound to the second file. Pairs come only from an object holding exactly one
    of each; anything else binds nothing, and the post is refused as unseen."""
    other_dms = "https://www.linkedin.com/dms-uploads/sp/v2/C4D22AQOTHER0001/uploaded-image/0"
    other_image = "urn:li:image:D4E10AQOTHER0002"
    paired = {"elements": [{"uploadUrl": DMS + "?ca=x", "image": IMAGE},
                           {"uploadUrl": other_dms + "?ca=x", "image": other_image}]}
    regs = publish.registrations(json.dumps(paired))
    assert sorted((sorted(r.handles), sorted(r.ids)) for r in regs) == sorted(
        [([publish.upload_handles(DMS).pop()], [IMAGE]), ([publish.upload_handles(other_dms).pop()], [other_image])])
    for ambiguous in ({"urls": [{"uploadUrl": DMS + "?ca=x"}, {"uploadUrl": other_dms + "?ca=x"}],
                       "media": [{"image": IMAGE}, {"image": other_image}]},
                      {"value": {"uploadUrl": DMS + "?ca=x", "image": IMAGE, "asset": other_image}}):
        assert publish.registrations(json.dumps(ambiguous)) == [], ambiguous
        first, second = dms_put(), dms_put(other_dms + "?ca=x")
        first.ids.clear()
        second.ids.clear()
        second.sha256 = "0" * 64  # another file
        for reg in publish.registrations(json.dumps(ambiguous)):
            reg.bind(first)
            reg.bind(second)
        rec = capture("linkedin.post", *linkedin_post(media=IMAGE), uploads=[first, second]).record
        assert publish.unbound_media(rec) == [IMAGE]
    # a stream with two upload URLs and one urn cannot say which upload carries it either
    assert publish.registrations(f"<p>{DMS}?a=1 {other_dms}?a=1 {IMAGE}</p>") == []
    # an image registration naming its asset binds only the one other id its answer gives
    named = {ASSET}
    assert publish.registrations(json.dumps({"value": {"image": IMAGE, "also": other_image}}), named=named) == []


async def test_the_guard_binds_a_registration_answered_before_the_put(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)

    class Response:
        async def text(self) -> str:
            return json.dumps({"value": {"uploadUrl": DMS + "?ca=x", "image": IMAGE}})

    url, headers, body = linkedin("com.linkedin.sdui.requests.sharing.registerMediaUpload", {"mediaType": "IMAGE"})
    register = FakeRequest(url, "POST", headers=headers, body=body)

    async def answer():
        return Response()

    register.response = answer
    ctx.emit("request", register)
    for _ in range(5):
        await asyncio.sleep(0)
    assert list(guard.uploads) == [] and len(guard.registrations) == 1
    ctx.emit("request", FakeRequest(DMS + "?ca=x", "PUT", headers={"content-type": "image/jpeg"}, body=PHOTO))
    [up] = guard.uploads
    assert IMAGE in up.ids and up.size == len(PHOTO)


# ---- stickers and GIFs ------------------------------------------------------------------------

def facebook_comment_with(attachment: dict) -> tuple[str, dict, bytes]:
    url, _, body = facebook_comment()
    variables = json.loads(parse_qs(body.decode(), keep_blank_values=True)["variables"][0])
    variables["input"]["attachments"] = [attachment]
    headers, body = meta_form("useCometUFICreateCommentMutation", variables)
    return url, headers, body


def test_a_facebook_sticker_or_gif_in_a_comment_is_bound_by_id_with_a_label():
    for attachment, kind in (({"sticker": {"id": "369239263222822"}}, "sticker"),
                             ({"media": {"id": "1234567890123", "__typename": "Sticker"}}, "sticker"),
                             ({"gif": {"id": "xT9IgG50Fb7Mi0prBC"}}, "gif"),
                             ({"media": {"gif_id": "xT9IgG50Fb7Mi0prBC"}}, "gif")):
        rec = capture("facebook.comment", *facebook_comment_with(attachment)).record
        [m] = rec["media"]
        assert m["catalog"] == kind and "Facebook's own" in m["label"] and publish.unbound_media(rec) == [], attachment
        [row] = _media_rows(rec["media"])
        assert row["note"] == m["label"] and "id " in row["note"]
    one = capture("facebook.comment", *facebook_comment_with({"sticker": {"id": "369239263222822"}}))
    other = capture("facebook.comment", *facebook_comment_with({"sticker": {"id": "126361874215276"}}))
    assert one.digest("x/y") != other.digest("x/y")  # the id is bound: another sticker is other content


def test_only_the_catalog_items_own_id_is_bound():
    """Another id beside it (a tracking id, which changes per click) is not taken for the sticker."""
    a = capture("facebook.comment", *facebook_comment_with({"sticker": {"id": "369239263222822",
                                                                        "tracking_id": secrets.token_hex(6)}}))
    assert [m["ref"] for m in a.record["media"]] == ["369239263222822"]


def test_a_photo_a_comment_names_by_id_is_still_refused_as_unseen():
    rec = capture("facebook.comment", *facebook_comment_with({"media": {"id": "122093066475549202"}})).record
    assert rec["media"] == [{"ref": "122093066475549202", "seen": False}]
    assert publish.unbound_media(rec) == ["122093066475549202"]
    # the same with a photo's type: not the site's catalog
    rec = capture("facebook.comment", *facebook_comment_with(
        {"media": {"id": "122093066475549202", "__typename": "Photo"}})).record
    assert publish.unbound_media(rec) == ["122093066475549202"]


# ---- LinkedIn profile photo and cover (brief B2): the same registration path as a post's photo

PROFILE_DMS = "https://www.linkedin.com/dms-uploads/sp/v2/D4D03AQPROFILE0001/profile-displayphoto/0"
PROFILE_IMAGE = "urn:li:image:D4D03AQPROFILE0001"


def test_a_profile_photo_binds_its_own_file_and_never_a_posts():
    """A post's photo and then a profile photo go up on one lane: each registration pairs its own
    upload URL with its own urn, so the profile edit binds the profile file, the post binds the
    post's, and neither urn reaches the other file. Shapes are guesses (to capture)."""
    post_up = dms_put()
    profile_file = b"\xff\xd8\xff the profile photo's bytes"
    profile_url = PROFILE_DMS + "?ca=vector_profile"
    found = egress.classify(profile_url, "PUT", {"content-type": "image/jpeg"})
    assert found is not None and found.id == "linkedin.draft.upload"
    profile_up = publish.upload_of(found.id, profile_url, {"content-type": "image/jpeg"}, profile_file)
    assert profile_up is not None
    post_up.ids.clear()
    profile_up.ids.clear()           # bound through the answers alone, not the ids in the URLs
    answers = [json.dumps({"value": {"uploadUrl": DMS + "?ca=x", "image": IMAGE}}),
               json.dumps({"value": {"uploadUrl": profile_url, "image": PROFILE_IMAGE}})]
    for text in answers:
        for reg in publish.registrations(text):
            reg.bind(post_up)
            reg.bind(profile_up)
    assert post_up.ids == {IMAGE} and profile_up.ids == {PROFILE_IMAGE}
    url, headers, body = linkedin("com.linkedin.sdui.requests.profile.updateProfilePhoto",
                                  {"profilePicture": {"displayImageUrn": PROFILE_IMAGE}})
    rec = capture("linkedin.profile.photo", url, headers, body, uploads=[post_up, profile_up]).record
    assert rec["kind"] == "profile"
    assert rec["media"] == [{"sha256": profile_up.sha256, "size": len(profile_file)}]
    rec = capture("linkedin.post", *linkedin_post(media=IMAGE), uploads=[post_up, profile_up]).record
    assert rec["media"] == [{"sha256": post_up.sha256, "size": len(PHOTO)}]
    # a cover that names the post's image, when only the profile photo's upload was seen, binds to no file:
    # the urn was registered for another upload, so the cover's media is unbound (refused as unseen)
    url, headers, body = linkedin("com.linkedin.sdui.requests.profile.updateBackgroundPhoto",
                                  {"backgroundImage": {"displayImageUrn": IMAGE}})
    rec = capture("linkedin.profile.cover", url, headers, body, uploads=[profile_up]).record
    assert publish.unbound_media(rec) == [IMAGE]          # not the profile file: refused as unseen


def test_a_profile_text_edit_shows_its_value_and_binds_every_field():
    url, headers, body = linkedin("com.linkedin.sdui.requests.profile.updateHeadline",
                                  {"headline": "Analyst of engines", "locale": "en_US"})
    rec = capture("linkedin.profile.headline", url, headers, body).record
    assert rec["kind"] == "profile" and rec["text"] == "Analyst of engines"
    assert "requestId" not in json.dumps(rec)      # the operation's own name is not content
    other = capture("linkedin.profile.headline", *linkedin("com.linkedin.sdui.requests.profile.updateHeadline",
                                                          {"headline": "Analyst of engines", "locale": "lt_LT"}))
    assert other.digest("t/li") != capture("linkedin.profile.headline", url, headers, body).digest("t/li")
    url, headers, body = linkedin("com.linkedin.sdui.requests.profile.updateAbout", {"summary": "Notes."})
    assert capture("linkedin.profile.about", url, headers, body).record["text"] == "Notes."
