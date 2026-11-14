"""What the commit gate's digest binds (AUD-03, agentos.node.publish): the whole content, never a
prefix; what cannot be bound whole is refused; every value of a repeated key; media by the file
the guard saw uploaded last under its id, and never by id alone. The review's probes 1, 2 and 6."""

from __future__ import annotations

import json

import pytest

from agentos.core.errors import ApprovalPending, PublishUnbound
from agentos.node import commitgate, publish
from agentos.node.egress import EFFECT_RULES, Refusal
from agentos.policy.approvals import content_digest

RULES = {r.id: r for r in EFFECT_RULES}
JSON = {"content-type": "application/json"}
FORM = {"content-type": "application/x-www-form-urlencoded"}
REDDIT = "https://www.reddit.com/svc/shreddit/graphql"
COMMENT = "https://www.reddit.com/svc/shreddit/t3_abc/create-comment"
CONFIGURE = "https://i.instagram.com/api/v1/media/configure/"


def reddit(title: str, markdown: str, extra: dict | None = None) -> bytes:
    inp = {"subredditName": "lithuania", "title": title, "content": {"markdown": markdown}, **(extra or {})}
    return json.dumps({"operationName": "CreatePost", "variables": {"input": inp}}).encode()


def digest(rule: str, url: str, headers: dict, body: bytes, uploads=()) -> tuple[str, publish.Capture]:
    cap = publish.capture(RULES[rule], url, "POST", headers, body, uploads=uploads)
    return cap.digest("lane-a"), cap


def test_text_past_twenty_thousand_characters_is_bound():
    head = "A" * 20000
    a, cap = digest("reddit.post", REDDIT, JSON, reddit("t", head + "BENIGN_TAIL"))
    b, _ = digest("reddit.post", REDDIT, JSON, reddit("t", head + "EVIL_TAIL_THAT_NOBODY_READ"))
    assert a != b and cap.record["text"].endswith("BENIGN_TAIL")


@pytest.mark.parametrize("extra_leaves", [380, 399, 400, 1000])
def test_a_body_with_hundreds_of_fields_binds_every_one(extra_leaves):
    many = {f"a{i:04d}": f"x{i}" for i in range(extra_leaves)}
    a, cap = digest("reddit.post", REDDIT, JSON, reddit("TITLE", "hi", many | {"zz_link": "https://benign.example"}))
    b, _ = digest("reddit.post", REDDIT, JSON, reddit("TITLE", "hi", many | {"zz_link": "https://evil.example/x"}))
    c, _ = digest("reddit.post", REDDIT, JSON, reddit("TITLE", "hi", many | {"subredditName": "other"}))
    assert len({a, b, c}) == 3 and cap.record["title"] == "TITLE" and cap.record["target"] == "lithuania"


def test_every_value_of_a_repeated_form_key_is_bound_in_order():
    a, cap = digest("reddit.comment", COMMENT, FORM, b"content=APPROVED_TEXT&content=SENT_TEXT")
    b, _ = digest("reddit.comment", COMMENT, FORM, b"content=SENT_TEXT")
    c, _ = digest("reddit.comment", COMMENT, FORM, b"content=SENT_TEXT&content=APPROVED_TEXT")
    assert len({a, b, c}) == 3
    assert cap.record["text"] == "APPROVED_TEXT" and cap.record["fields"]["content[1]"] == "SENT_TEXT"
    body = (b'--b\r\nContent-Disposition: form-data; name="caption"\r\n\r\none\r\n'
            b'--b\r\nContent-Disposition: form-data; name="caption"\r\n\r\ntwo\r\n--b--\r\n')
    fields, _ = publish.decode({"content-type": "multipart/form-data; boundary=b"}, body)
    assert fields == {"caption": ["one", "two"]}


class Ctx:
    """Approves the digests in `approved`; anything else waits for a person."""
    grant = principal = runtime = trace = None

    def __init__(self, approved: set[str] = frozenset()):
        self.approved = set(approved)
        self.asked: list[str] = []

    async def authorize(self, effect, *, content=None, **kw):
        d = content_digest(content)
        if d in self.approved:
            return
        self.asked.append(d)
        raise ApprovalPending("waiting for the owner", approval_id=f"ap_{len(self.asked)}")


class Request:
    def __init__(self, url: str, headers: dict, body: bytes):
        self.url, self.method, self.headers, self.post_data_buffer = url, "POST", headers, body


def gate(ctx: Ctx, uploads=()) -> commitgate.ActGate:
    g = commitgate.ActGate(ctx, "lane-a", call={"ref": "e1", "action": "click", "text": None}, submit_like=True)
    g.uploads = list(uploads)
    return g


async def test_media_the_node_never_saw_uploaded_is_refused_not_bound_by_id():
    ctx = Ctx()
    g = gate(ctx)
    body = b"upload_id=1730700000123&caption=Quiet+hills"
    refusal = await g.hold(RULES["instagram.post"], Request(CONFIGURE, FORM, body), CONFIGURE, "POST")
    assert isinstance(refusal, Refusal) and "did not see uploaded" in refusal.reason
    assert isinstance(g.outcome, PublishUnbound) and ctx.asked == []  # no approval could bind it


async def test_a_body_the_node_cannot_bind_whole_is_refused():
    ctx = Ctx()
    g = gate(ctx)
    big = reddit("t", "x" * (publish.PARSE_MAX + 1))
    refusal = await g.hold(RULES["reddit.post"], Request(REDDIT, JSON, big), REDDIT, "POST")
    assert isinstance(refusal, Refusal) and isinstance(g.outcome, PublishUnbound) and ctx.asked == []


async def test_a_client_chosen_upload_id_reused_for_another_file_after_approval_is_caught():
    photo_a = publish.Upload("instagram.draft.upload", "sha256:" + "a" * 64, 100, {"1730700000123"})
    body = b"upload_id=1730700000123&caption=Quiet+hills"
    approved, _ = digest("instagram.post", CONFIGURE, FORM, body, uploads=[photo_a])
    ctx = Ctx({approved})
    assert await gate(ctx, [photo_a]).hold(RULES["instagram.post"], Request(CONFIGURE, FORM, body), CONFIGURE,
                                           "POST") is None
    # the page uploads another file under the same id, then publishes: the digest is the other file's
    photo_b = publish.Upload("instagram.draft.upload", "sha256:" + "b" * 64, 100, {"1730700000123"})
    g = gate(ctx, [photo_a, photo_b])
    assert isinstance(await g.hold(RULES["instagram.post"], Request(CONFIGURE, FORM, body), CONFIGURE, "POST"),
                      Refusal)
    assert isinstance(g.outcome, ApprovalPending)
    # and one the guard saw but could not hash is refused, never bound to the first file
    unhashed = publish.Upload("instagram.draft.upload", None, 90 * 1024 * 1024, {"1730700000123"})
    g = gate(ctx, [photo_a, unhashed])
    await g.hold(RULES["instagram.post"], Request(CONFIGURE, FORM, body), CONFIGURE, "POST")
    assert isinstance(g.outcome, PublishUnbound)


def test_an_instagram_upload_names_its_client_chosen_id():
    ids = publish.upload_ids("https://i.instagram.com/rupload_igphoto/fb_uploader_1730700000123",
                             {"X-Instagram-Rupload-Params": json.dumps({"upload_id": "1730700000123"})})
    assert {"fb_uploader_1730700000123", "1730700000123"} <= ids


async def test_a_publish_sent_then_a_second_held_reports_both():
    """The review's probe 9: the first post left under its approval, the second waits. The act says
    so (not "nothing was sent"), and the approval of the one that left is not retired."""
    first, _ = digest("reddit.post", REDDIT, JSON, reddit("A", "first post"))
    ctx = Ctx({first})
    g = gate(ctx)
    assert await g.hold(RULES["reddit.post"], Request(REDDIT, JSON, reddit("A", "first post")), REDDIT, "POST") is None
    held = await g.hold(RULES["reddit.post"], Request(REDDIT, JSON, reddit("B", "second post")), REDDIT, "POST")
    third = await g.hold(RULES["reddit.post"], Request(REDDIT, JSON, reddit("A", "first post")), REDDIT, "POST")
    assert isinstance(held, Refusal) and isinstance(third, Refusal)  # once one is held, later ones are too
    with pytest.raises(ApprovalPending) as e:
        g.raise_outcome()
    assert e.value.approval_id == "ap_1" and "1 publish of this click already left" in str(e.value)
    assert '"A"' in str(e.value) and "A later one did not leave" in str(e.value)


async def test_a_page_that_submits_twice_posts_once():
    """The duplicate-post scenario: one click, and the page sends the same post twice (a double
    submit, a retry after an error). The first leaves under its approval, the copy is aborted, and
    the act reports what was sent; nothing waits."""
    commitgate.ASKED.clear()
    a, _ = digest("reddit.post", REDDIT, JSON, reddit("A", "first post"))
    ctx = Ctx({a})
    g = gate(ctx)
    assert await g.hold(RULES["reddit.post"], Request(REDDIT, JSON, reddit("A", "first post")), REDDIT, "POST") is None
    dup = await g.hold(RULES["reddit.post"], Request(REDDIT, JSON, reddit("A", "first post")), REDDIT, "POST")
    assert isinstance(dup, Refusal) and "already sent it once" in dup.reason
    g.raise_outcome()
    assert g.sent == [a] and ctx.asked == []


async def test_the_signed_in_account_is_bound_with_the_content():
    """Approved while the browser was signed in as one account, sent as another: other content."""
    ctx = Ctx()
    asked = []
    for user in ("t2_me", "t2_other"):
        g = gate(ctx)

        async def identity(site, user=user):
            return {"site": site, "user_id": user, "source": "cookie reddit_session"}

        g.identity = identity
        await g.hold(RULES["reddit.post"], Request(REDDIT, JSON, reddit("A", "post")), REDDIT, "POST")
        asked.append(ctx.asked[-1])
    assert asked[0] != asked[1]
