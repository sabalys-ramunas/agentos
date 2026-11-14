"""Approval cards: what an approval will do, as the owner should see it (docs/API.md, Needs you).

The node writes a card when it creates an action approval, from the content the approval binds
to, never from model text:

- `post`: a connector run or a flow run that publishes. The exact text it will post, the images it
  will attach (files in the lane's inbox, found by the sha256 the approval binds, fetched by the panel
  through `GET /api/approvals/{id}/files/{param}`), and where: "as @biza.coffee on Instagram, through
  biz-a/instagram". Text params shown verbatim in the content can be edited by the approver
  (`editable`); hidden and file params cannot, and neither can a param its schema marks
  `"readOnly": true` (a profile edit's value comes from the engagement program, which keeps the
  value it asked for as the account's state, so the card approves exactly that value or denies it).
- `publish`: a publish the commit gate caught (AUD-03, agentos.node.commitgate): built from the
  request the page was about to send, normalized (agentos.node.publish): the text, title, link,
  target, media (by hash and size when the upload was seen), visibility, every other field it sends,
  the effect rule that matched it (and whether that rule was checked on a real account), the
  account and lane it publishes as (or a warning that the node cannot see the signed-in account),
  who the body acts as when the gate resolved it (`acting`, `identity_line` "Posts as biz-a (Page)"
  for a chat confirmation, `identity_warning` for an identity agentos does not know),
  characters that do not show or turn text around, names with letters outside ASCII, and the
  digest the approval binds. It cannot be edited.
- `form`: a browser_act click or Enter that submits a form. The fields the form will send and how
  each differs from what the page loaded with (agentos.node.fields: never a password, code or hidden
  value). A raw browser_act call cannot be edited; the card says so.

Vault values never leave in a card, a preview or a summary: not to the panel, not in a push, not to
the controller (ApprovalStore.scrub, and the channel's payload scrubs again). A text field that holds
one (the username the vault typed, a password pasted into a text box) keeps its label and its
`changed` flag, its value reads [redacted], and the row says `vault: true` with a note: the owner sees
which field holds a saved value and whether it changed, never the value. The approval still binds
the real value (the content hash, and browser_act's re-check before the click).

`build_card(tool, lane, content, runtime=...)` returns None for anything else (the card is then the
summary and preview, as before). It never raises: a card is a view, never a reason to refuse.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from agentos.policy.redact import redact_text

log = logging.getLogger("agentos.policy")

_FLOW_KEYS = {"flow", "version", "lane", "params"}
_CONNECTOR_KEYS = {"operation", "contract", "surface", "acts_as", "params"}
_FILE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HIDDEN = re.compile(r"^hidden:[0-9a-f]{8,64}$")
TEXT_PARAMS = ("caption", "text", "body", "message", "content", "comment", "reply", "title", "description")
MAX_TEXT = 5000
IMAGE_MIME = re.compile(r"^image/(png|jpeg|gif|webp)$")
PUBLISH_EDIT_NOTE = ("This is the request the page was about to send, so it cannot be edited here: deny it and "
                     "tell the agent what to change.")
PUBLISH_FIELDS_SHOWN = 40
FORM_EDIT_NOTE = ("A browser_act call types into the page itself, so it cannot be edited here: deny it and tell "
                  "the agent what to change.")


def site_label(site: str | None) -> str:
    return (site or "").replace("_", " ").replace("-", " ").title() or "the site"


def _service(runtime: Any, name: str) -> Any:
    get = getattr(runtime, "get", None)
    try:
        return get(name) if callable(get) else None
    except Exception:  # noqa: BLE001
        return None


def _flow(runtime: Any, flow_id: str, version: str | None = None) -> Any:
    flows = _service(runtime, "flows")
    if flows is None:
        return None
    try:
        return flows.get(flow_id, version) if version else flows.get(flow_id)
    except Exception:  # noqa: BLE001 - a flow that is gone: the card falls back on the content
        return None


def _props(fv: Any) -> dict[str, Any]:
    try:
        return dict(fv.definition.params.get("properties", {})) if fv is not None else {}
    except Exception:  # noqa: BLE001
        return {}


def _inbox(runtime: Any, lane: str) -> Any:
    files = _service(runtime, "files")
    if files is not None and callable(getattr(files, "lane", None)):
        return files.lane(lane)
    config = _service(runtime, "config")
    if config is None:
        return None
    from agentos.node.files import FilesConfig, LaneFiles

    return LaneFiles(config.lane_dir(lane) / "files", FilesConfig.from_node(config))


def find_file(runtime: Any, lane: str, value: str) -> Any:
    """The complete inbox file a param names: by the sha256 a connector approval binds, or by the
    file id or name a flow param holds. None when there is none."""
    box = _inbox(runtime, lane)
    if box is None:
        return None
    try:
        items = [f for f in box.list("inbox") if str(getattr(f.status, "value", f.status)) == "complete"]
    except Exception:  # noqa: BLE001
        return None
    if _FILE_DIGEST.match(value):
        want = value.removeprefix("sha256:")
        return next((f for f in items if f.sha256 == want), None)
    return next((f for f in items if value in (f.id, f.name)), None)


def _file_row(runtime: Any, lane: str, name: str, value: str) -> dict[str, Any]:
    row: dict[str, Any] = {"name": name, "file": True, "match": value}
    found = find_file(runtime, lane, value)
    if found is not None:
        row.update({"file_name": redact_text(found.name, max_len=120), "size": found.size, "mime": found.mime,
                    "image": bool(found.mime and IMAGE_MIME.match(found.mime))})
    else:
        row["missing"] = True
    return row


def _editable(name: str, value: Any, prop: dict[str, Any] | None) -> bool:
    if not isinstance(value, str) or len(value) > MAX_TEXT or redact_text(value, max_len=0) != value:
        return False
    if prop is None:
        return True
    if prop.get("readOnly") is True:      # the caller's value is the one approved (a profile edit's)
        return False
    return prop.get("type") in (None, "string") and not prop.get("enum") and not prop.get("format") \
        and not prop.get("const")


def _uploads(fv: Any) -> set[str]:
    """Params a flow's upload steps send as files (agentos.runner.flows FlowService.upload_params)."""
    try:
        from agentos.runner.models import iter_steps

        return {step.param for _, step in iter_steps(fv.definition.steps)
                if getattr(step, "type", None) == "upload" and isinstance(getattr(step, "param", None), str)}
    except Exception:  # noqa: BLE001
        return set()


def _params(runtime: Any, lane: str, params: dict[str, Any], fv: Any, *, files_by_digest: bool) -> list[dict]:
    props = _props(fv)
    uploads = _uploads(fv) if fv is not None else set()
    rows = []
    for name, value in sorted(params.items()):
        prop = props.get(name) if isinstance(props.get(name), dict) else None
        # a file is never edited on a card: one an upload step sends is a file whatever its schema says (QA-2 #14)
        is_file = (prop or {}).get("format") == "agentos-file" or name in uploads or (
            files_by_digest and isinstance(value, str) and bool(_FILE_DIGEST.match(value)))
        if isinstance(value, str) and _HIDDEN.match(value):
            rows.append({"name": name, "hidden": True})
        elif is_file and isinstance(value, str):
            rows.append(_file_row(runtime, lane, name, value))
        else:
            row = {"name": name, "value": redact_text(value, max_len=MAX_TEXT) if isinstance(value, str) else value}
            if prop is not None or fv is None:
                row["editable"] = _editable(name, value, prop)
            rows.append(row)
    return rows


def _text(rows: list[dict]) -> dict[str, Any] | None:
    """The param that is the post's text: a well-known name first, else the longest shown string."""
    texts = [r for r in rows if isinstance(r.get("value"), str)]
    for want in TEXT_PARAMS:
        for r in texts:
            if r["name"] == want:
                return {"param": r["name"], "value": r["value"]}
    if texts:
        r = max(texts, key=lambda r: len(r["value"]))
        return {"param": r["name"], "value": r["value"]}
    return None


def _post(rows: list[dict], **head: Any) -> dict[str, Any]:
    images = [{k: r.get(k) for k in ("name", "file_name", "size", "mime")} for r in rows if r.get("image")]
    return {"type": "post", **head, "text": _text(rows), "images": images, "params": rows,
            "editable": [r["name"] for r in rows if r.get("editable")]}


def connector_card(runtime: Any, content: dict[str, Any]) -> dict[str, Any]:
    operation = str(content["operation"])
    site = operation.split(".", 1)[0]
    surface_id = str(content["surface"])
    fv = _flow(runtime, f"connector.{operation}")
    surfaces = _service(runtime, "surfaces")
    surface = None
    if surfaces is not None and callable(getattr(surfaces, "find", None)):
        try:
            surface = surfaces.find(surface_id)
        except Exception:  # noqa: BLE001
            surface = None
    acts = content.get("acts_as") or {}
    who = (getattr(surface.acts_as, "name", None) or getattr(surface.acts_as, "asset_id", None)) if surface else None
    who = str(who or acts.get("id") or surface_id)
    lane = surface.lane if surface is not None else surface_id
    title = getattr(getattr(fv, "definition", None), "title", None) or operation
    rows = _params(runtime, lane, dict(content.get("params") or {}), fv, files_by_digest=True)
    return _post(rows, operation=operation, title=title, site=site, site_label=site_label(site), surface=surface_id,
                 acts_as=who, acts_as_kind=acts.get("kind"), through=lane,
                 line=f"as {who} on {site_label(site)}, through {lane}")


def flow_card(runtime: Any, content: dict[str, Any]) -> dict[str, Any]:
    flow_id, version, lane = str(content["flow"]), str(content["version"]), str(content["lane"])
    fv = _flow(runtime, flow_id, version)
    site = lane.split("/", 1)[-1]
    title = getattr(getattr(fv, "definition", None), "title", None) or flow_id
    rows = _params(runtime, lane, dict(content.get("params") or {}), fv, files_by_digest=False)
    return _post(rows, flow=flow_id, version=version, title=title, site=site, site_label=site_label(site),
                 surface=lane, acts_as=None, through=lane, line=f"on {site_label(site)}, through {lane}")


VAULT_NOTE = "holds a value saved in the vault, so it is not shown here"


def _vault_row(row: dict[str, Any], scrub: Any) -> dict[str, Any]:
    """The row with any vault value (a username, a password reused as text) replaced, marked so the
    owner still sees which field holds one and whether it changed."""
    out = dict(row)
    for key in ("value", "was"):
        if isinstance(row.get(key), str):
            out[key] = scrub(row[key])
    if isinstance(row.get("selected"), list):
        out["selected"] = [scrub(v) if isinstance(v, str) else v for v in row["selected"]]
    if out != row:
        out.update({"vault": True, "hidden": VAULT_NOTE})
    return out


def form_card(content: dict[str, Any], runtime: Any = None) -> dict[str, Any] | None:
    form = content.get("form")
    if not isinstance(form, dict):
        return None
    # `bind` is the keyed digest browser_act compares (agentos.node.fields.bind_digest): not for a person
    fields = [{k: v for k, v in f.items() if k != "bind"} for f in form.get("fields") or [] if isinstance(f, dict)]
    if runtime is not None:
        from agentos.vault.scrub import runtime_scrub

        scrub = runtime_scrub(runtime)
        fields = [_vault_row(f, scrub) for f in fields]
    return {"type": "form", "action": form.get("action"), "method": form.get("method"), "fields": fields,
            "changed": sum(1 for f in fields if f.get("changed")), "editable": [], "edit_note": FORM_EDIT_NOTE}


def _lane_account(runtime: Any, lane: str) -> Any:
    """The lane's expected account (an AccountRef), or None."""
    lanes = _service(runtime, "lanes")
    try:
        return lanes.get(lane).spec.account if lanes is not None else None
    except Exception:  # noqa: BLE001 - a lane that is gone: the card names the lane only
        return None


PUBLISH_SHORT = 500        # characters of a field shown on a publish card
VAULT_SPAN = "a vault value is here; it will be sent"
REDACTED_SPAN = "part of this looks like a secret or a code and is hidden here; it will be sent as it is"
URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")


class _Shown:
    """Turns a bound value into what the card shows, saying what it does not show: a vault value
    or a secret shape hidden (it is still sent), and characters past the display limit (still bound)."""

    def __init__(self, runtime: Any):
        self.scrub = None
        if runtime is not None:
            from agentos.vault.scrub import runtime_scrub

            self.scrub = runtime_scrub(runtime)

    def __call__(self, value: Any, limit: int = PUBLISH_SHORT) -> dict[str, Any]:
        if not isinstance(value, str):
            return {"value": value}
        shown = redact_text(value, max_len=0)
        out: dict[str, Any] = {}
        notes = []
        if shown != value:
            notes.append(REDACTED_SPAN)
        if self.scrub is not None:
            scrubbed = self.scrub(shown)
            if scrubbed != shown:
                out["vault"] = True
                notes.append(VAULT_SPAN)
                shown = scrubbed
        if len(shown) > limit:
            out["not_shown_chars"] = len(shown) - limit
            notes.append(f"{len(shown) - limit} more characters are not shown here; the approval binds all of them")
            shown = shown[:limit]
        out["value"] = shown
        if notes:
            out["note"] = "; ".join(notes)
        return out


def _links(record: dict[str, Any]) -> list[str]:
    """Every link URL the publish sends, in full (in its text, link, fields and rich text)."""
    found: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str) and v[:1] in "[{":  # rich text sent as JSON: its links, unescaped
            try:
                return walk(json.loads(v))
            except ValueError:
                pass
        if isinstance(v, str):
            for url in URL_RE.findall(v):
                url = url.rstrip(".,;:)]}")
                if url not in found:
                    found.append(url)
        elif isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    walk(record)
    return found


def _media_rows(media: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for m in media:
        if not isinstance(m, dict):
            continue
        row = dict(m)
        if m.get("catalog"):  # a sticker or GIF of the site's own catalog: no file, bound by its id
            row["note"] = str(m.get("label") or "from the site's own catalog, not a file")
        elif "sha256" not in m:
            row["note"] = ("media not seen by the node: id only" if m.get("seen") is False
                           else "media the node saw uploaded but could not hash: id only")
        rows.append(row)
    return rows


# characters that do not show, or that turn the text around them (a link can read differently from
# what is sent): bidi embeddings, overrides and isolates, marks, and zero-width characters
INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")


def _invisible(record: dict[str, Any]) -> list[str]:
    found = sorted(set(INVISIBLE.findall(json.dumps(record, ensure_ascii=False))))
    return [f"U+{ord(c):04X}" for c in found]


def _scripts(value: str) -> list[str]:
    return sorted({unicodedata.name(c, "UNKNOWN").split()[0] for c in value if c.isalpha()})


def _lookalikes(names: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    """Names (a target, an account, a link's host) with letters outside ASCII, which can look like
    another name: each with its ASCII form (punycode for a host) and the scripts it mixes."""
    out: list[dict[str, Any]] = []
    for name, value in names:
        if not isinstance(value, str) or value.isascii():
            continue
        if name == "link host":
            try:
                plain = value.encode("idna").decode("ascii")
            except UnicodeError:
                plain = value.encode("punycode").decode("ascii")
        else:
            plain = "".join(c if c.isascii() else f"[U+{ord(c):04X}]" for c in value)
        row = {"field": name, "value": value, "ascii": plain, "scripts": _scripts(value)}
        if row not in out:
            out.append(row)
    return out


def _handle(value: Any) -> str:
    """A handle as compared: case and an `@` or `u/` in front do not count."""
    text = str(value or "").strip().casefold()
    for prefix in ("@", "/u/", "u/"):
        text = text.removeprefix(prefix)
    return text


def _same_account(ref: Any, live: dict[str, Any] | None) -> bool | None:
    """True or False when the signed-in account can be compared with the lane's: by user id when
    one is pinned, else by handle. None when it cannot be (nothing pinned or read, another site)."""
    from agentos.node.sites import account_matches, site_key

    if ref is None or live is None:
        return None
    if ref.user_id:
        return account_matches(ref, live)
    if ref.handle and live.get("user_id") and site_key(ref.site) == live.get("site"):
        return _handle(ref.handle) == _handle(live.get("user_id"))
    return None


def _cannot_see(site: str) -> str:
    from agentos.node.sites import site as known_site

    found = known_site(site)
    if found is None or found.identity is None:
        return f"the node cannot see which account this browser is signed into on {site_label(site)}"
    return (f"the node could not read which account this browser is signed into on {site_label(site)} (it may "
            "be signed out, or its cookies could not be read)")


def publish_card(content: dict[str, Any], runtime: Any = None) -> dict[str, Any]:
    """The card of a publish the commit gate caught: what the page was about to send. A value it
    cannot show whole says so (a vault value, a secret shape, characters past the limit: all still
    sent and bound); every link is listed in full; a batch lists each operation the same way; the
    account the browser is signed into now (AUD-12) is shown, with a warning when it is not the
    lane's or when the node cannot see it (`account_status`); invisible and direction-changing
    characters and names outside ASCII are warned."""
    from agentos.node import acting, egress
    from agentos.policy.approvals import content_digest

    rule_id, lane, record = str(content["publish"]), str(content["lane"]), dict(content["record"])
    rule = next((r for r in egress.EFFECT_RULES if r.id == rule_id), None)
    site = rule.site if rule is not None else rule_id.split(".", 1)[0]
    shown = _Shown(runtime)
    # who the body acts as (agentos.node.acting): `lane` is then the surface it posts as, `through` the login
    acts = record.get("acts_as") if isinstance(record.get("acts_as"), dict) else None
    through = str(acts.get("through")) if acts and acts.get("through") else lane
    ref = _lane_account(runtime, through)
    account = {"site": ref.site, "handle": ref.handle, "user_id": ref.user_id} if ref is not None else None
    expected = (account or {}).get("handle") or (account or {}).get("user_id")
    live = record.get("session_account") if isinstance(record.get("session_account"), dict) else None
    match = _same_account(ref, live)
    seen = live.get("user_id") if live is not None else None
    # who it publishes as, only as far as the node can tell: never the lane's account unread
    status = ("match" if match else "wrong_account" if match is False
              else "unpinned" if seen and not expected else "unknown")
    who = expected if status == "match" else seen if status in ("wrong_account", "unpinned") else None
    text = record.get("text")
    if text is not None and not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    fields = record.get("fields") or {}
    rows = sorted(fields.items())
    listed = rows[:PUBLISH_FIELDS_SHOWN]
    hidden = len(rows) - len(listed)
    unknown = record.get("kind") == egress.UNKNOWN
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    card = {
        "type": "publish", "rule": rule_id, "kind": record.get("kind"), "site": site, "site_label": site_label(site),
        "verified_rule": bool(rule is not None and rule.verified),
        "text": {"param": "text", **shown(text, MAX_TEXT)} if isinstance(text, str) else None,
        "title": shown(record.get("title")) if record.get("title") is not None else None,
        "link": shown(record.get("link"), 4000) if record.get("link") is not None else None,
        "links": _links(record),
        "target": record.get("target"), "reply_to": record.get("reply_to"), "body_as": record.get("as"),
        "visibility": record.get("visibility"), "flags": record.get("flags"),
        "media": _media_rows(record.get("media") or []),
        "operations": [{k: (shown(v)["value"] if k == "op" else shown(v)) for k, v in op.items()}
                       for op in record.get("operations") or [] if isinstance(op, dict)],
        "fields": [{"name": k, **shown(v)} for k, v in listed],
        "fields_hidden": hidden,
        "surface": lane, "through": through, "acts_as": who, "account": account, "signed_in_as": live,
        "account_status": status,
        "line": (f"as {who} on {site_label(site)}, through {through}" if who
                 else f"on {site_label(site)}, through {through} (the account it publishes as is not known)"),
        "digest": content_digest(content), "images": [], "params": [], "editable": [],
        "edit_note": PUBLISH_EDIT_NOTE,
    }
    if hidden:
        card["fields_note"] = f"{hidden} more fields are not shown here; the approval binds them too"
    card["identity_line"] = f"Posts as {who}" if who else None  # what a chat confirmation says (from the card)
    if acts is not None:
        card["acting"] = {k: acts.get(k) for k in ("status", "id", "field", "surface", "kind", "label", "verified",
                                                   "why") if acts.get(k) is not None}
        card["identity_line"] = acting.card_line(acts)
        if acts.get("status") == acting.SURFACE:
            card["acts_as"] = acts.get("label")
            card["line"] = f"as {acts.get('label')} on {site_label(site)}, through {through}"
        elif acts.get("status") == acting.UNRECOGNIZED:
            raw = acts.get("id")
            card["acts_as"] = None
            card["line"] = (f"as an identity agentos does not know{f' ({raw})' if raw else ''} on "
                            f"{site_label(site)}, through {through}")
            named = f" ({raw}, from {acts.get('field')})" if raw and acts.get("field") else f" ({raw})" if raw else ""
            why = str(acts.get("why") or "no surface names it").rstrip(".")
            card["identity_warning"] = (
                f"This would post as an identity agentos does not know{named}: {why}. It is never automatic: "
                "approve only if you know who that is, or add it as a surface first.")
    if match is False:
        by = "" if ref.user_id else " (the lane pins a handle only, so the node compared the handle)"
        card["account_warning"] = (f"The browser is signed in to {site_label(site)} as {seen}, not as "
                                   f"the lane's expected account {expected}{by}: this would publish as {seen}.")
    elif status == "unknown":
        cannot = _cannot_see(site) if seen is None else (
            f"the node read the signed-in account {seen} but cannot compare it with the lane's account")
        whom = (f"so it cannot confirm this publishes as {expected}" if expected
                else "and no expected account is set for this lane")
        card["account_warning"] = (f"{cannot[0].upper()}{cannot[1:]}, {whom}. Check the account in the live view "
                                   "before you approve.")
    chars = _invisible(record)
    if chars:
        card["chars"] = chars
        n = len(INVISIBLE.findall(json.dumps(record, ensure_ascii=False)))
        card["chars_warning"] = (f"What it sends holds {n} invisible or direction-changing character"
                                 f"{'s' if n != 1 else ''} ({', '.join(chars)}, marked where they sit): they can "
                                 "make text or a link read differently from what is sent.")
    names = [("target", record.get("target")), ("profile or page", record.get("as")),
             ("posts as", (acts or {}).get("label") if (acts or {}).get("status") == acting.SURFACE else None),
             ("lane's account", expected), ("signed-in account", seen)]
    names += [("link host", urlsplit(u).hostname) for u in card["links"]]
    look = _lookalikes(names)
    if look:
        card["lookalikes"] = look
        card["lookalike_warning"] = ("Some names have letters outside ASCII and may look like another name ("
                                     + "; ".join(f"{x['value']} is {x['ascii']}, {'/'.join(x['scripts'])}" for x in look)
                                     + "). Check each is the one you mean.")
    if unknown:
        names = ", ".join(k for k, _ in rows[:20]) or "no fields"
        method, path = request.get("method", "POST"), request.get("path", "/")
        typed = record.get("carries_typed")
        where = ({"text": " as its text", "title": " as its title"}.get(typed, f" (in the field {typed})")
                 if isinstance(typed, str) and typed else "")
        card["headline"] = f"A request agentos does not recognise, to {site_label(site)}: {method} {path}"
        card["shape"] = f"{method} {path} with {names}"
        card["explain"] = (
            f"agentos has no rule for this request, so it cannot tell whether it posts anything on "
            f"{site_label(site)}. "
            + (f"It carries text the agent typed{where}, and the agent's own click or Enter sent it. "
               if typed is not None else "")
            + "Every field it sends is listed here. Denying it is safe: nothing is sent, and the agent is told. "
              "Approve only if you expected the agent to send this.")
        card["deny_safe"] = True
    return card


def build_card(tool: str | None, lane: str | None, content: Any, *, runtime: Any = None) -> dict[str, Any] | None:
    if not isinstance(content, dict):
        return None
    try:
        if set(content) == _CONNECTOR_KEYS and content.get("surface") == lane:
            return connector_card(runtime, content)
        if set(content) == _FLOW_KEYS and content.get("lane") == lane:
            return flow_card(runtime, content)
        if set(content) == {"publish", "lane", "record"} and content.get("lane") == lane:  # lane: a surface too
            return publish_card(content, runtime)
        if tool == "browser_act":
            return form_card(content, runtime)
    except Exception:
        log.warning("could not build the approval card for %s on %s", tool, lane, exc_info=True)
    return None


def edited_content(content: Any, card: dict[str, Any] | None, edit: dict[str, Any]) -> dict[str, Any]:
    """The content with the owner's edit applied. Only the card's editable params, as text."""
    from agentos.core.errors import InvalidRequest

    if not isinstance(content, dict) or not isinstance(content.get("params"), dict) or not card:
        raise InvalidRequest("this approval cannot be edited: approve it as it is, or deny it")
    allowed = set(card.get("editable") or [])
    if not allowed:
        raise InvalidRequest(card.get("edit_note") or "this approval cannot be edited: approve it as it is, or deny it")
    if not isinstance(edit, dict) or not edit:
        raise InvalidRequest('an edit is {"params": {"caption": "the new text"}}')
    bad = sorted(set(edit) - allowed)
    if bad:
        raise InvalidRequest(f"only {', '.join(sorted(allowed))} can be edited here, not {', '.join(bad)}")
    params = dict(content["params"])
    for name, value in edit.items():
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
            raise InvalidRequest(f"{name} must be text of 1 to {MAX_TEXT} characters")
        params[name] = value
    if params == content["params"]:
        raise InvalidRequest("the edit changes nothing: approve it as it is")
    return {**content, "params": params}


def with_edit(card: dict[str, Any] | None, params: dict[str, Any]) -> dict[str, Any] | None:
    """The card showing the edited params (the text and each param row)."""
    if not card:
        return card
    out = dict(card)
    rows = []
    for row in card.get("params") or []:
        if row.get("name") in params and "value" in row:
            row = {**row, "value": redact_text(str(params[row["name"]]), max_len=MAX_TEXT), "edited": True}
        rows.append(row)
    out["params"] = rows
    text = card.get("text")
    if isinstance(text, dict) and text.get("param") in params:
        out["text"] = {**text, "value": redact_text(str(params[text["param"]]), max_len=MAX_TEXT), "edited": True}
    return out


MAX_IMAGE_BYTES = 20 * 1024 * 1024


def read_image(runtime: Any, card: dict[str, Any] | None, param: str) -> tuple[bytes, str]:
    """(bytes, mime) of the image a card shows for `param`. A connector card's file is read only
    while its bytes still hash to the digest the approval binds."""
    import hashlib

    from agentos.core.errors import NotFound

    row = next((r for r in (card or {}).get("params") or [] if r.get("name") == param and r.get("file")), None)
    lane = (card or {}).get("through")
    if row is None or not row.get("image") or not isinstance(lane, str) or not isinstance(row.get("match"), str):
        raise NotFound("This approval shows no image for that param.")
    found = find_file(runtime, lane, row["match"])
    box = _inbox(runtime, lane)
    if found is None or box is None or not (found.mime and IMAGE_MIME.match(found.mime)):
        raise NotFound("The image is no longer in the lane's inbox.")
    data = box.read(found.id, MAX_IMAGE_BYTES)
    if _FILE_DIGEST.match(row["match"]) and "sha256:" + hashlib.sha256(data).hexdigest() != row["match"]:
        raise NotFound("The image in the inbox changed since the approval was made.")
    return data, found.mime
