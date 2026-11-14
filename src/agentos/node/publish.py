"""What a publish request will publish, read from the request itself (AUD-03, the commit gate,
docs/design/egress-guard.md section 8).

`capture(rule, url, method, headers, body)` decodes the body of a request that matched a commit
rule (form-encoded, JSON, multipart, and the JSON a form field carries, such as Meta's
`variables` or Reddit's `richText`) into a normalized record:

- `fields`: every leaf the request sends, flattened to dotted paths (`variables.input.message.text`,
  `attachments[0].photo.id`), minus the volatile ones, named exactly (never by a part of their
  name): CSRF and captcha tokens, client and correlation ids, Meta's per-request parameters, a
  rule's logging and tracking blocks, and the operation's own name (VOLATILE_NAMES, and each
  rule's `RULE_VOLATILE` paths). A scheduled time, an audience or a target is never one. What is left is
  what two identical clicks send alike, so the record binds everything the page sends, not only
  what the card shows: an injected link attachment changes the digest.
- what a person reads on the card, picked out of the fields per rule (DISPLAY): the text, a
  title, a link, the target (a subreddit, the post a comment answers, a media id), the account
  the body acts as (Meta's `actor_id`), the visibility, and media references. A media reference
  is bound by the sha256 and size of the upload that made it when the guard saw that upload
  (`uploads`), else by its id.

The digest (`Capture.digest`) is agentos.policy.approvals.content_digest over `content(lane)`,
the dict the gate passes to authorize: so an approval's `content_digest`, a standing approval's
`contents` and the gate compare the same thing. The record holds the raw text: the vault's scrub
runs on the card, the summary and the preview, never on what the digest binds.

A field this module cannot tell is volatile makes the digest change between two clicks; the gate
then refuses with publish_changed after an approval (fail closed). The canary's recorded shapes
(a canary run of 2026-11-04) are the tests for every verified rule.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from agentos.node import acting
from agentos.policy.approvals import content_digest

PARSE_MAX = 2 * 1024 * 1024   # a publish body (its text parts) larger than this is refused, not bound by a prefix
MAX_FIELDS = 5000             # more leaves than this: refused (the digest never binds a part of a body)
MAX_DEPTH = 32                # nested deeper than this: refused

# Volatile fields are named exactly, never by a part of their name: a field not listed here stays
# in the digest and on the card (a scheduled time, an audience, a target are content). Each one is
# here because a recorded shape (tests/unit/test_publish_normalize.py, the canary's field names in
# a canary run of 2026-11-04) shows it changes from one click to the next, or because it only names
# the operation, which the rule already names. A volatile field missing here makes two clicks
# differ: the gate then asks again (publish_changed), which fails closed.
#
# Names volatile wherever they sit (CSRF and captcha tokens, client and correlation ids):
VOLATILE_NAMES = frozenset({
    "client_mutation_id", "idempotence_token", "csrf_token", "__csrf", "fb_dtsg", "fb_dtsg_ag", "jazoest", "lsd",
    "recaptchaToken", "correlationId", "trackingId", "clientRequestId",
})
# Paths at the top of the body that name the operation (the rule already names it)
_OPERATION = ("operation", "operationName", "query", "queryId", "doc_id", "extensions")
# The request parameters Meta's web clients (Facebook, Instagram) add to every form: per-request
# counters and hashes, the build they were served, and the operation's name. The account (`av`,
# `__user`) is kept.
_META = ("__req", "__hs", "__s", "__hsi", "__dyn", "__csr", "__spin_t", "__rev", "__spin_r", "__spin_b", "__ccg",
         "fb_api_req_friendly_name", "fb_api_caller_class", "server_timestamps")
# Per rule, exact dotted paths (`[]` any index); a path drops what is under it too.
RULE_VOLATILE: dict[str, tuple[str, ...]] = {
    "facebook.post": (*_META, "variables.input.source", "variables.input.composer_source_surface",
                      "variables.input.composer_type", "variables.input.navigation_data",
                      "variables.input.event_share_metadata", "variables.input.logging", "variables.input.tracking"),
    "facebook.comment": (*_META, "variables.input.feedback_source", "variables.input.vod_video_timestamp",
                         "variables.input.is_tracking_encrypted", "variables.input.tracking",
                         "variables.input.session_id", "variables.input.attribution_id_v2", "variables.feedLocation",
                         "variables.feedbackSource", "variables.focusCommentID", "variables.scale",
                         "variables.useDefaultActor", "variables.UFI2CommentsProvider_commentsKey"),
    "instagram.comment": (*_META, "variables.root_connections"),
    "instagram.post": _META,
    "instagram.comment.rest": _META,
    "linkedin.post": ("requestId",),
    "linkedin.comment": ("requestId",),
    # the profile edits (to capture): requestId names the operation, as for a post
    **{f"linkedin.profile.{f}": ("requestId",) for f in ("name", "headline", "about", "photo", "cover")},
}
# fields that carry JSON in a string (a form's GraphQL variables, Reddit's rich text)
# (a rich text string is kept whole: the digest binds it raw, the card shows its text)
JSON_FIELDS = frozenset({"variables", "input", "children_metadata", "content_json"})

# What a person reads, per rule: dotted paths (`[]` any index), the first that is present wins.
# Rules not listed use GENERIC, which looks the keys up by name.
DISPLAY: dict[str, dict[str, tuple[str, ...]]] = {
    "instagram.comment": {"text": ("variables.data.comment_text",), "target": ("variables.data.media_id",),
                          "reply_to": ("variables.data.replied_to_comment_id",)},
    "instagram.comment.rest": {"text": ("comment_text",), "reply_to": ("replied_to_comment_id",)},
    "instagram.post": {"text": ("caption",), "media": ("upload_id", "children_metadata[].upload_id"),
                       "visibility": ("audience",)},
    "facebook.post": {"text": ("variables.input.message.text",), "as": ("variables.input.actor_id",),
                      "media": ("variables.input.attachments[].photo.id", "variables.input.attachments[].video.id"),
                      "link": ("variables.input.attachments[].link.share_scrape_data",),
                      "visibility": ("variables.input.audience.privacy.base_state", "variables.input.audience.to_id",
                                     "variables.input.audience.privacy.allow")},
    "facebook.comment": {"text": ("variables.input.message.text",), "as": ("variables.input.actor_id",),
                         "target": ("variables.input.feedback_id",),
                         "media": ("variables.input.attachments[].media.id",)},
    "reddit.post": {"title": ("variables.input.title",),
                    "text": ("variables.input.content.richText", "variables.input.content.markdown",
                             "variables.input.content.text"),
                    "link": ("variables.input.content.url", "variables.input.url"),
                    "target": ("variables.input.subredditName",),
                    "flags": ("variables.input.isNsfw", "variables.input.isSpoiler")},
    "reddit.comment": {"text": ("content",)},
    "reddit.post.oldreddit": {"title": ("title",), "text": ("text",), "link": ("url",), "target": ("sr",)},
    "reddit.comment.oldreddit": {"text": ("text",), "target": ("thing_id", "parent")},
}
# Rules whose body shape is not recorded yet (LinkedIn's profile edits, to capture): what a person
# reads is looked up by field name, as GENERIC does, with the names such an edit is likely to use.
# Every other field (a name's first and last parts among them) is still on the card and in the
# digest (`fields`).
NAMED: dict[str, dict[str, tuple[str, ...]]] = {
    "linkedin.profile.headline": {"text": ("headline", "localizedheadline")},
    "linkedin.profile.about": {"text": ("summary", "about", "localizedsummary")},
}
GENERIC = {
    "text": ("text", "commentary", "comment_text", "message", "body", "caption", "content", "markdown"),
    "title": ("title", "headline"),
    "link": ("url", "link", "href"),
    "target": ("subredditname", "sr", "thing_id", "feedback_id", "media_id", "threadurn", "posturn",
               "target_id", "to_id", "group_id", "page_id", "parent"),
    "as": ("actor_id", "author", "actorurn"),
    "visibility": ("visibility", "visibletoconnectionsonly", "privacy", "audience"),
}
# where a path holds the thing a comment answers (the URL names it, not the body)
PATH_TARGETS: dict[str, str] = {
    "reddit.comment": r"/svc/shreddit/(t[13]_[a-z0-9]+)/create-comment",
    "instagram.comment.rest": r"/api/v1/web/comments/(\d+)/add/",
}
MEDIA_URN = re.compile(r"^urn:li:(digitalmediaAsset|image|video|document):[A-Za-z0-9_-]{4,}$")
# per site: field names (the last path segment, lower case, `[n]` dropped) that name an uploaded file
# by id though no DISPLAY path lists them: Reddit's media ids (from its web client's public
# behaviour, not checked on a real account)
MEDIA_NAMES: dict[str, frozenset[str]] = {"reddit": frozenset({"mediaid", "mediaids", "assetid", "asset_id"})}
# rich text elements that embed media (Reddit's richtext JSON: {"e": "img", "id": <media id>})
RICH_MEDIA = frozenset({"img", "video", "gif"})
# Media a site serves from its own catalog: a sticker, a GIF from the site's picker (Giphy, Tenor).
# Never a file a user uploads, so nobody's file could hide behind it: it is bound by its id and
# shown with a plain label instead of refused as unseen media. Told by the field that names it
# (`attachments[0].sticker.id`, `sticker_id`, `gif_id`) or by the type its object gives it
# (`{"e": "gif", "id": "giphy|..."}`, `__typename: "Sticker"`); an id an upload the node saw
# made is bound by that file all the same.
CATALOG_FIELD = re.compile(r"(?i)(^|[._\[\]])(sticker|gif|giphy|tenor|animated_?image)s?([._\[\]]|_?id$|$)")
CATALOG_TYPE = re.compile(r"(?i)^(sticker|gif|giphy|tenor|animated_?image)$")
CATALOG_TYPE_KEYS = ("e", "type", "__typename", "media_type", "kind")
CATALOG_LABELS = {"sticker": "a sticker from {site}'s own catalog, not a file", "gif": "a GIF from {site}'s own "
                  "picker, not a file"}
SITE_NAMES = {"facebook": "Facebook", "instagram": "Instagram", "reddit": "Reddit", "linkedin": "LinkedIn",
              "medium": "Medium"}


class Unbindable(ValueError):
    """A publish body the node cannot bind whole (too large, too deep, too many fields, a format it
    cannot read): the gate refuses it rather than bind a part of it."""


@dataclass
class Upload:
    """An upload the guard saw leave (a draft.upload rule): the hash and size of the file it sent,
    and the ids the site answered with, so a publish that names one is bound to the file. `sha256`
    None: the guard saw the upload but could not hash it (too large, or its body unreadable), so a
    publish that names it is refused, never bound to an earlier file that used the same id."""
    rule: str
    sha256: str | None
    size: int
    ids: set[str] = field(default_factory=set)
    # what a registration can name the upload by before it happens: its URL (host and path) and an
    # S3 form's `key` (upload_handles); a registration naming one of them adds its ids (Registration)
    handles: set[str] = field(default_factory=set)

    def names(self) -> set[str]:
        """Every handle a registration may match: the upload's own, and each id it has."""
        return self.handles | {"id:" + i for i in self.ids}


@dataclass
class Registration:
    """A media registration's answer (LinkedIn's registerUpload / initializeUpload, Reddit's upload
    lease): the ids of the media it makes (an asset or image urn, a media id), and how the upload
    that carries the file is named (its upload URL, an S3 key, or an id the request named, such as
    the asset an image registration is for). The file seen at that upload binds those ids."""
    handles: set[str]
    ids: set[str]

    def bind(self, up: Upload) -> bool:
        """Add this registration's ids to `up` when it names that upload. True when it did."""
        if self.ids and self.handles & up.names():
            up.ids |= self.ids
            return True
        return False


@dataclass
class Capture:
    """One publish request, normalized. `record` is what the digest binds (with the rule and the lane)."""
    rule: str
    site: str
    kind: str
    verified: bool
    host: str
    path: str
    record: dict[str, Any]
    # who the body names as acting (agentos.node.acting.read); the gate resolves it into record["acts_as"]
    acting: Any = None

    def content(self, lane: str) -> dict[str, Any]:
        """What the gate passes to authorize: the approval binds exactly this."""
        return {"publish": self.rule, "lane": lane, "record": self.record}

    def digest(self, lane: str) -> str:
        return content_digest(self.content(lane))


# ---- decoding --------------------------------------------------------------------------------

def _maybe_json(key: str, value: Any) -> Any:
    if isinstance(value, str) and key in JSON_FIELDS and value[:1] in "[{":
        with contextlib.suppress(ValueError):
            return json.loads(value)
    return value


def _add(fields: dict[str, Any], key: str, value: Any) -> None:
    """A field that repeats keeps every value, in order (a list): the digest binds them all."""
    if key not in fields:
        fields[key] = value
    elif isinstance(fields[key], _Repeated):
        fields[key].append(value)
    else:
        fields[key] = _Repeated([fields[key], value])


class _Repeated(list):
    """The values of a form or multipart key sent more than once."""


def _form(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in parse_qsl(text, keep_blank_values=True):
        _add(out, k, _maybe_json(k, v))
    return out


def _multipart(ctype_header: str, body: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The text parts of a multipart body as fields (a repeated name keeps every value), and its
    file parts as {name, sha256, size}."""
    m = re.search(r'boundary="?([^";]+)"?', ctype_header)
    if not m:
        return {}, []
    boundary = b"--" + m.group(1).encode("latin-1")
    fields: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    for part in body.split(boundary)[1:]:
        if part.startswith(b"--"):
            break
        head, _, data = part.partition(b"\r\n\r\n")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        disp = re.search(rb'(?i)content-disposition:[^\r\n]*', head)
        if disp is None:
            continue
        name = re.search(rb'\bname="([^"]*)"', disp.group(0))
        key = name.group(1).decode("utf-8", "replace") if name else ""
        if re.search(rb'\bfilename="', disp.group(0)):
            files.append({"name": key, "sha256": "sha256:" + hashlib.sha256(data).hexdigest(), "size": len(data)})
        else:
            _add(fields, key, _maybe_json(key, data.decode("utf-8", "replace")))
    return fields, files


def decode(headers: dict[str, str] | None, body: bytes | None) -> tuple[Any, list[dict[str, Any]]]:
    """The body as a value (a dict for forms and JSON objects) and the files a multipart body
    carries (bound by hash and size). Raises Unbindable for text parts larger than PARSE_MAX and
    for a body in no format known here: a person could not read what it sends."""
    low = {k.lower(): v for k, v in (headers or {}).items()}
    ctype = low.get("content-type") or ""
    kind = ctype.split(";")[0].strip().lower()
    data = body or b""
    if not data:
        return {}, []
    if "multipart/" in kind:
        fields, files = _multipart(ctype, data)
        if len(data) - sum(f["size"] for f in files) > PARSE_MAX:
            raise Unbindable(f"its text parts are larger than {PARSE_MAX // (1024 * 1024)} MB")
        return fields, files
    if len(data) > PARSE_MAX:
        raise Unbindable(f"its body is {len(data)} bytes, larger than the {PARSE_MAX // (1024 * 1024)} MB the "
                         "node reads whole")
    text = data.decode("utf-8", "replace")
    if "json" in kind or (not kind and text[:1] in "[{"):
        with contextlib.suppress(ValueError):
            return json.loads(text), []
    if "x-www-form-urlencoded" in kind or (not kind and "=" in text):
        return _form(text), []
    raise Unbindable(f"its body is in a format the node cannot read ({kind or 'no content type'})")


def flatten(value: Any, *, rule: str = "", keep_volatile: bool = False) -> dict[str, Any]:
    """Leaves as {dotted path: value}, the volatile ones left out (VOLATILE_NAMES, RULE_VOLATILE: exact
    names and paths) unless `keep_volatile`."""
    volatile = frozenset((*_OPERATION, *RULE_VOLATILE.get(rule, ())))
    out: dict[str, Any] = {}

    def walk(v: Any, path: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise Unbindable(f"it nests fields deeper than {MAX_DEPTH} levels")
        if len(out) >= MAX_FIELDS:
            raise Unbindable(f"it sends more than {MAX_FIELDS} fields")
        if isinstance(v, dict):
            for key in sorted(v, key=str):
                k = str(key)
                sub = f"{path}.{k}" if path else k
                if not keep_volatile and (k in VOLATILE_NAMES or re.sub(r"\[\d+\]", "[]", sub) in volatile):
                    continue
                walk(_maybe_json(k, v[key]), sub, depth + 1)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                walk(item, f"{path}[{i}]", depth + 1)
        elif v is None or v == "" or v == [] or v == {}:
            return
        else:
            out[path or "body"] = v if isinstance(v, (bool, int, float)) else str(v)  # whole: never a prefix

    walk(value, "", 0)
    return out


# ---- what a person reads --------------------------------------------------------------------

def _path_re(path: str) -> re.Pattern[str]:
    return re.compile("^" + re.escape(path).replace(r"\[\]", r"\[\d+\]") + r"(\[\d+\]|\..*)?$")


def _pick(flat: dict[str, Any], paths: Iterable[str]) -> list[tuple[str, Any]]:
    """The fields under the first of `paths` that is present (a path with `[]` may give several)."""
    for path in paths:
        rx = _path_re(path)
        found = [(k, v) for k, v in flat.items() if rx.match(k)]
        if found:
            return found
    return []


def _generic(flat: dict[str, Any], names: tuple[str, ...]) -> list[tuple[str, Any]]:
    for name in names:
        found = [(k, v) for k, v in flat.items()
                 if re.sub(r"\[\d+\]", "", k).rsplit(".", 1)[-1].lower() == name]
        if found:
            return found[:1]
    return []


def rich_text(value: Any) -> str | None:
    """The plain text of a rich text document (Reddit's richtext JSON: paragraphs of `t` runs)."""
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            value = json.loads(value) if value[:1] in "[{" else value
    if isinstance(value, str):
        return value
    lines: list[str] = []

    def runs(node: Any) -> str:
        if isinstance(node, dict):
            return (node.get("t") if isinstance(node.get("t"), str) else "") + "".join(runs(c) for c in node.get("c") or [])
        if isinstance(node, list):
            return "".join(runs(c) for c in node)
        return ""

    doc = value.get("document") if isinstance(value, dict) else value
    for para in doc if isinstance(doc, list) else [doc]:
        lines.append(runs(para))
    text = "\n".join(lines).strip()
    return text or None


def capture(rule: Any, url: str, method: str, headers: dict[str, str] | None, body: bytes | None, *,
            uploads: Iterable[Upload] = ()) -> Capture:
    """Normalize one request that matched commit rule `rule` (agentos.node.egress.EffectRule)."""
    parts = urlsplit(url)
    value, files = decode(headers, body)
    rid = rule.id
    flat = flatten(value, rule=rid)
    spec = DISPLAY.get(rid, {})

    def pick(key: str) -> list[tuple[str, Any]]:
        if key in spec:
            return _pick(flat, spec[key])
        if rid in NAMED:
            return _generic(flat, NAMED[rid].get(key, ()))
        return _generic(flat, GENERIC.get(key, ()))

    shown: set[str] = set()
    record: dict[str, Any] = {"kind": rule.kind}
    for key in ("title", "text", "link", "target", "as", "visibility", "reply_to"):
        found = pick(key)
        if not found:
            continue
        if key == "text":
            path, raw = found[0]
            text = rich_text(raw) if isinstance(raw, str) and raw[:1] in "[{" else raw
            record["text"] = text
            if text == raw:
                shown.add(path)  # else the raw rich text stays in `fields`: the digest binds it as sent
            continue
        shown.update(k for k, _ in found)
        record[key] = found[0][1] if len(found) == 1 else {k: v for k, v in found}
    target = PATH_TARGETS.get(rid)
    if target:
        m = re.search(target, parts.path)
        if m:
            record["target"] = m.group(1)
    flags = [kv for path in spec.get("flags", ()) for kv in _pick(flat, (path,))]
    if flags:
        shown.update(k for k, _ in flags)
        record["flags"] = {k.rsplit(".", 1)[-1]: v for k, v in flags}
    media = _media(flat, flatten(value, rule=rid, keep_volatile=True), spec, files, uploads, shown, site=rule.site)
    if media:
        record["media"] = media
    if isinstance(value, list):  # a batch: each operation, by name, with its text (the fields bind them all)
        record["operations"] = [_operation(el) for el in value]
    if rule.kind == "unknown":  # no rule reads it: the card names the request itself
        record["request"] = {"method": method.upper(), "path": parts.path or "/"}
    rest = {k: v for k, v in flat.items() if k not in shown}
    if rest:
        record["fields"] = rest
    return Capture(rule=rid, site=rule.site, kind=rule.kind, verified=bool(rule.verified),
                   host=(parts.hostname or "").lower(), path=parts.path or "/", record=record,
                   acting=acting.read(rule.site, flat))


def _operation(element: Any) -> dict[str, Any]:
    """One operation of a batch body as a person reads it: its name, and its text and title by name."""
    out: dict[str, Any] = {}
    if isinstance(element, dict):
        for key in ("operationName", "operation", "fb_api_req_friendly_name", "queryName", "requestId"):
            if isinstance(element.get(key), str):
                out["op"] = element[key]
                break
    flat = flatten(element)
    for key in ("title", "text"):
        found = _generic(flat, GENERIC[key])
        if found:
            raw = found[0][1]
            out[key] = rich_text(raw) if isinstance(raw, str) and raw[:1] in "[{" else raw
    return out


def _media(flat: dict[str, Any], raw: dict[str, Any], spec: dict[str, tuple[str, ...]],
           files: list[dict[str, Any]], uploads: Iterable[Upload], shown: set[str], *,
           site: str = "") -> list[dict[str, Any]]:
    """Media references: files in the body itself, and ids the body names, bound by the hash and
    size of the upload the guard saw make the id (the latest one: a client-chosen id reused for
    another file binds the other file). An id no upload the guard saw made is `{"ref", "seen":
    False}`, and one whose upload it could not hash `{"ref", "seen": "not hashed"}`: the gate
    refuses both (unbound_media). A sticker or a GIF of the site's own catalog (CATALOG_FIELD) is
    `{"ref", "catalog", "label"}`, bound by its id. `raw` keeps volatile names: an Instagram photo
    is named by its upload id."""
    out: list[dict[str, Any]] = [{"sha256": f["sha256"], "size": f["size"]} for f in files]
    found = [(k, v, None) for k, v in (_pick(raw, spec["media"]) if "media" in spec else [])]
    found += [(k, v, None) for k, v in flat.items() if isinstance(v, str) and MEDIA_URN.match(v)]
    names = MEDIA_NAMES.get(site, frozenset())
    found += [(k, v, None) for k, v in raw.items()
              if names and re.sub(r"\[\d+\]", "", k).rsplit(".", 1)[-1].lower() in names]
    found += [(k, v, None) for k, v in raw.items() if CATALOG_FIELD.search(k) and _id_leaf(k)]
    found += _rich_media(flat)
    by_id: dict[str, Upload] = {}
    for up in uploads:
        for i in up.ids:
            by_id[i] = up
    seen: set[tuple[str, str]] = set()
    for path, ref, kind in found:
        if (path, str(ref)) in seen:
            continue
        seen.add((path, str(ref)))
        if kind is None:
            shown.add(path)  # a rich text's media stays in its text, which the fields bind as sent
        up = by_id.get(str(ref))
        if up is None and str(ref).startswith("urn:li:"):  # an asset urn names the id its upload URL carries
            up = by_id.get(str(ref).rsplit(":", 1)[-1])
        catalog = kind if kind in ("sticker", "gif") else _catalog_kind(raw, path)
        if up is None and catalog is not None:
            label = CATALOG_LABELS[catalog].format(site=SITE_NAMES.get(site, site or "the site"))
            out.append({"ref": str(ref), "catalog": catalog, "label": f"{label} (id {ref})"})
        elif up is None:
            out.append({"ref": str(ref), "seen": False})
        elif up.sha256 is None:
            out.append({"ref": str(ref), "size": up.size, "seen": "not hashed"})
        else:
            out.append({"sha256": up.sha256, "size": up.size})
    return out


def _id_leaf(path: str) -> bool:
    """Does this path end in the catalog item's own id (`sticker.id`, `sticker_id`, `gif_id`), not
    in some other id beside it (a tracking id changes per click and is no media)?"""
    last = re.sub(r"\[\d+\]", "", path).rsplit(".", 1)[-1].lower()
    return last in ("id", "fbid") or re.fullmatch(r"(sticker|gif|giphy|tenor|animated_?image)s?_?id", last) is not None


def _catalog_kind(raw: dict[str, Any], path: str) -> str | None:
    """"sticker" or "gif" when the field naming this media, or the type its object gives it,
    says it is one of the site's own catalog; None for anything that may be a user's file."""
    m = CATALOG_FIELD.search(path)
    word = m.group(2).lower() if m else None
    if word is None:
        parent = path.rsplit(".", 1)[0] if "." in path else ""
        for key in CATALOG_TYPE_KEYS:
            value = raw.get(f"{parent}.{key}" if parent else key)
            if isinstance(value, str) and CATALOG_TYPE.match(value):
                word = value.lower()
                break
    if word is None:
        return None
    return "sticker" if word == "sticker" else "gif"


def _rich_media(flat: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """Media a rich text document embeds (Reddit's richtext JSON, in a post's `richText` or a
    comment's `content`): (field path, id, "gif" for a GIF of the site's picker, "rich" for an
    image or video, which must be an upload the node saw)."""
    out: list[tuple[str, str, str | None]] = []
    for path, value in flat.items():
        if not isinstance(value, str) or value[:1] not in "[{" or '"e"' not in value:
            continue
        try:
            doc = json.loads(value)
        except ValueError:
            continue

        def walk(node: Any, depth: int, path: str = path) -> None:
            if depth > MAX_DEPTH or len(out) > 50:
                return
            if isinstance(node, dict):
                if node.get("e") in RICH_MEDIA and isinstance(node.get("id"), (str, int)):
                    ref = str(node["id"])
                    out.append((path, ref, "gif" if node["e"] == "gif" and not _uploaded_gif(ref) else "rich"))
                for child in node.values():
                    walk(child, depth + 1)
            elif isinstance(node, list):
                for child in node:
                    walk(child, depth + 1)

        walk(doc, 0)
    return out


def _uploaded_gif(ref: str) -> bool:
    """Reddit names a GIF of its picker `giphy|<id>|<size>`; any other id is an upload."""
    return not ref.lower().startswith(("giphy|", "tenor|"))


def unbound_media(record: dict[str, Any]) -> list[str]:
    """The media ids a record names that are bound by id only (no upload the guard saw and
    hashed), a sticker or GIF of the site's own catalog aside (bound by its id: it is no file)."""
    return [str(m.get("ref")) for m in record.get("media") or []
            if isinstance(m, dict) and "sha256" not in m and not m.get("catalog")]


# ---- uploads --------------------------------------------------------------------------------

_ID_KEY = re.compile(r"(?i)(^|_)(id|fbid|photoid|mediaid|upload_?id|asset|urn|value|handle)$")


def upload_of(rule_id: str, url: str, headers: dict[str, str] | None, body: bytes | None) -> Upload | None:
    """An upload request (a draft.upload rule) as the hash and size of the file it sends: the
    file part of a multipart body, else the whole body (a PUT of the bytes). Ids in its URL path
    (LinkedIn's asset in `/dms-uploads/<id>`) are noted at once; the response adds the rest."""
    data = body or b""
    low = {k.lower(): v for k, v in (headers or {}).items()}
    if not data:
        return None
    ctype = low.get("content-type") or ""
    if "multipart/" in ctype.lower():
        fields, files = _multipart(ctype, data)
        if not files:
            return None
        biggest = max(files, key=lambda f: f["size"])
        up = Upload(rule_id, biggest["sha256"], biggest["size"])
        for key in ("upload_id", "uploadId", "waterfall_id"):
            if isinstance(fields.get(key), str):
                up.ids.add(fields[key])
        s3_key = fields.get("key")
        if isinstance(s3_key, str) and s3_key:  # an S3 form post (Reddit's media): the key its lease gave
            up.handles.add("key:" + s3_key)
            name = s3_key.rsplit("/", 1)[-1]
            for i in {name, name.split(".", 1)[0]}:
                if len(i) >= 6:
                    up.ids.add(i)
    else:
        up = Upload(rule_id, "sha256:" + hashlib.sha256(data).hexdigest(), len(data))
    up.ids |= upload_ids(url, headers)
    up.handles |= upload_handles(url)
    return up


def upload_handles(url: str) -> set[str]:
    """How a registration can name an upload by its URL: `url:<host><path>` (the query is left
    out: it carries signatures). Not for a URL whose path is `/` (an S3 bucket takes every upload
    there, so it names none)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return set()
    host, path = (parts.hostname or "").lower().rstrip("."), parts.path or "/"
    return {f"url:{host}{path}"} if host and path != "/" else set()


def upload_ids(url: str, headers: dict[str, str] | None) -> set[str]:
    """Ids an upload names before the site answers: segments of its URL path (LinkedIn's asset,
    Instagram's `fb_uploader_<id>` and the id after its last `_`), and Instagram's
    `x-instagram-rupload-params` header (`upload_id`, a client-chosen id)."""
    ids: set[str] = set()
    for seg in urlsplit(url).path.split("/"):
        if len(seg) >= 8 and re.fullmatch(r"[A-Za-z0-9_:-]+", seg) and any(c.isdigit() for c in seg):
            ids.add(seg)
            tail = seg.rsplit("_", 1)[-1]
            if tail != seg and len(tail) >= 6 and tail.isdigit():
                ids.add(tail)
    low = {k.lower(): v for k, v in (headers or {}).items()}
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        params = json.loads(low.get("x-instagram-rupload-params") or "{}")
        for key in ("upload_id", "uploadId"):
            if isinstance(params.get(key), (str, int)):
                ids.add(str(params[key]))
    return ids


_UPLOAD_URL_KEY = re.compile(r"(?i)^(upload_?(lease_?)?url|single_?upload_?url|action)$")
_REG_ID_KEY = re.compile(r"(?i)^(media_?id|asset_?id|asset|image|video|document)$")
_DMS_URL = re.compile(r"https://www\.linkedin\.com/dms-uploads/[^\s\"'\\<>]+")
_URN_TEXT = re.compile(r"urn:li:(?:digitalmediaAsset|image|video|document):[A-Za-z0-9_-]{4,}")


def registrations(text: str, *, named: Iterable[str] = ()) -> list[Registration]:
    """What a media registration's answer says (LinkedIn's registerUpload and initializeUpload,
    its web client's registerMediaUpload and RegisterImageMedia, Reddit's upload lease): each
    media it makes (urns, media ids) with how the upload of its file is named (an upload URL, an
    S3 form `key`). `named`: ids the registration request itself named (the asset an image
    registration is for), which name the upload that already carries the file, when it names
    exactly one and the answer one other id. Pairs are taken per object of the answer: the
    smallest object that holds both a way to name an upload and ids, and only when it holds
    exactly one of each (an ambiguous object binds nothing, and no larger object pairs its parts).
    An answer that is not JSON (a server component stream) is read line by line, and as a last
    resort by its upload URLs and urns, only when it names exactly one of each (else nothing
    binds, and a publish naming those urns is refused as unseen). Unverified on a real account:
    the shapes are the APIs' public ones."""
    named_handles = {"id:" + n for i in named if i for n in {str(i), str(i).rsplit(":", 1)[-1]}}
    values = _json_values(text)
    out: list[Registration] = []
    for value in values:
        _pairs(value, out, 0)
    if not values:
        urls = {h for u in _DMS_URL.findall(text) for h in upload_handles(u.replace("\\u0026", "&"))}
        urns = set(_URN_TEXT.findall(text))
        if len(urns) == 1 and len(urls) == 1:
            out.append(Registration(urls, urns))
    named_ids = {str(i) for i in named if i}
    if len(named_ids) == 1:  # the one id this registration answers with is the upload's the request named
        ids = (set().union(*(r.ids for r in out)) if out else _all_ids(values, text)) - named_ids
        if len(ids) == 1:
            out.append(Registration(named_handles, ids))
    return out


def _json_values(text: str) -> list[Any]:
    text = text.strip().removeprefix("for (;;);")
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    found = []
    for line in text.splitlines()[:200]:  # a server component stream: `0:{...}` lines
        body = re.sub(r"^[0-9a-zA-Z]{1,8}:", "", line.strip(), count=1)
        if body[:1] in "[{":
            with contextlib.suppress(ValueError):
                found.append(json.loads(body))
    return found


def _pairs(node: Any, out: list[Registration], depth: int) -> tuple[set[str], set[str], bool]:
    """(handles, ids, paired) of a subtree; pairs the smallest objects that hold both."""
    handles: set[str] = set()
    ids: set[str] = set()
    paired = False
    if depth > 16:
        return handles, ids, paired
    if isinstance(node, dict):
        if str(node.get("header") or node.get("name") or "").lower() == "key" and isinstance(node.get("value"), str):
            handles.add("key:" + node["value"])  # an S3 form field the upload will send: {"name": "key", "value": ...}
        for key, value in node.items():
            if isinstance(value, str):
                if _UPLOAD_URL_KEY.match(str(key)) and value.startswith(("http", "//")):
                    handles |= upload_handles(value if value.startswith("http") else "https:" + value)
                elif MEDIA_URN.match(value) or (_REG_ID_KEY.match(str(key)) and 4 <= len(value) <= 200
                                                and not value.startswith(("http", "//"))):
                    ids.add(value)
            elif isinstance(value, (dict, list)):
                h, i, p = _pairs(value, out, depth + 1)
                paired |= p
                if not p:
                    handles |= h
                    ids |= i
    elif isinstance(node, list):
        for value in node[:50]:
            h, i, p = _pairs(value, out, depth + 1)
            paired |= p
            if not p:
                handles |= h
                ids |= i
    if handles and ids and not paired:
        # the smallest object that holds both: it pairs only when it holds exactly one of each; two
        # uploads and two urns (or one upload and two urns) cannot say which file is which, so they
        # bind nothing (a publish naming them is refused as unseen), and no larger object pairs them
        if len(handles) == 1 and len(ids) == 1:
            out.append(Registration(handles, ids))
        return set(), set(), True
    return handles, ids, paired


def _all_ids(values: list[Any], text: str) -> set[str]:
    out: set[str] = set(_URN_TEXT.findall(text))
    for value in values:
        _, ids, _ = _pairs(value, [], 0)
        out |= ids
    return out


def named_media(body: bytes | None) -> set[str]:
    """The media urns a registration request names (an image registration names its asset)."""
    if not body or len(body) > PARSE_MAX:
        return set()
    return set(_URN_TEXT.findall(body.decode("utf-8", "replace")))


def response_ids(text: str) -> set[str]:
    """Ids an upload's response names (Meta's `for (;;);{"payload":{"photoID":...}}`, LinkedIn's urns)."""
    text = text.strip()
    text = text.removeprefix("for (;;);")
    try:
        value = json.loads(text)
    except ValueError:
        return set()
    out: set[str] = set()

    def walk(v: Any, key: str, depth: int) -> None:
        if depth > 8 or len(out) > 50:
            return
        if isinstance(v, dict):
            for k, item in v.items():
                walk(item, str(k), depth + 1)
        elif isinstance(v, list):
            for item in v[:20]:
                walk(item, key, depth + 1)
        elif isinstance(v, (str, int)) and not isinstance(v, bool) and _ID_KEY.search(key):
            s = str(v)
            if 4 <= len(s) <= 200:
                out.add(s)

    walk(value, "", 0)
    return out
