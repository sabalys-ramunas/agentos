"""An approval's preview in words, for the app (types.ts `Approval.preview`): never the raw record.

The node writes a card for most approvals (openajar.policy.cards): a post of a routine or a connector, a publish
the commit gate caught, or a form a click submits. The app reads `preview.kind` and `preview.data`, so this
module turns each card into them: a post's own text, or a
list of plain fields with what to check first. A preview with no card is shown as it is, unless it is a record
(JSON): then it is told as fields too, or said in one plain line, and never shown raw (finding 24 of the
2026-11-10 full test). The words about an unknown request are the card's own (`headline`, Builder G's text).

A module of this package with no route of its own: it only has `routes(api)`, so the package can list it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from starlette.routing import BaseRoute

from openajar.api.app import Api

# What a publish is, by the kind of the commit rule that caught it (openajar.node.egress.rules EFFECT_KINDS).
KIND_WORDS = {"post": "A post", "comment": "A comment", "like": "A like", "follow": "A follow",
              "join": "Joining a group", "profile": "A change to the account's profile"}
ASK = {"post": "Post this on {site}?", "comment": "Post this comment on {site}?", "like": "Like this on {site}?",
       "follow": "Follow this account on {site}?", "join": "Join this group on {site}?",
       "profile": "Change the profile on {site}?"}
YES = {"post": "Post it", "comment": "Post the comment", "like": "Like it", "follow": "Follow",
       "join": "Join", "profile": "Change it"}
NO = {"post": "Don't post", "comment": "Don't post", "like": "Don't like", "follow": "Don't follow",
      "join": "Don't join", "profile": "Don't change"}
VISIBILITY = {"EVERYONE": "Everyone", "PUBLIC": "Everyone", "ANYONE": "Anyone", "FRIENDS": "Friends",
              "FRIENDS_OF_FRIENDS": "Friends of friends", "SELF": "Only the account", "ONLY_ME": "Only the account",
              "CONNECTIONS": "Connections only", "CONNECTIONS_ONLY": "Connections only"}
WARNINGS = ("account_warning", "identity_warning", "chars_warning", "lookalike_warning")
MAX_FIELDS = 12
MAX_VALUE = 500
UNREADABLE = "openajar cannot show the details of this request in words. Ask the agent what it is for before you say yes."


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def plain(value: Any) -> str:
    """A value as words: text as it is, yes or no, a number, a list joined, and a group named by its size.
    Never a record."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value[:MAX_VALUE]
    if isinstance(value, dict):
        if "value" in value:  # a value as a card shows it (cards._Shown): its text, and what it does not show
            note = value.get("note")
            shown = plain(value.get("value"))
            return f"{shown} ({note})" if note and shown else (note or shown)
        return f"a group of {len(value)} values" if value else ""
    if isinstance(value, (list, tuple)):
        parts = [plain(item) for item in value]
        return ", ".join(part for part in parts if part)[:MAX_VALUE]
    return ""


def _field(label: str, value: Any, before: Any = None) -> dict[str, Any] | None:
    text = plain(value)
    if not text:
        return None
    out: dict[str, Any] = {"label": label, "value": text}
    was = plain(before)
    if was and was != text:
        out["before"] = was
    return out


def _warnings(card: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"label": "Check first", "value": str(card[key])} for key in WARNINGS if card.get(key)]


def _publish(card: dict[str, Any], summary: str) -> dict[str, Any]:
    site = str(card.get("site_label") or "the site")
    kind = str(card.get("kind") or "")
    if kind == "unknown" or card.get("headline"):
        fields = [_field("What openajar knows", card.get("headline") or summary),
                  _field("Where", _cap(str(card.get("line") or "")))] + _warnings(card)
        return {"kind": "form", "data": {
            "form": f"A request to {site} that openajar does not recognise",
            "ask": f"Let this request go to {site}?", "yes": "Let it go", "no": "Don't send it",
            "fields": [f for f in fields if f]}}
    text = card.get("text") if isinstance(card.get("text"), dict) else {}
    words = text.get("value") if isinstance(text.get("value"), str) else None
    warnings = _warnings(card)
    if kind in ("post", "comment") and words and not warnings and not card.get("title") and not card.get("link"):
        return {"kind": "post", "text": words}  # drawn as the post it will be
    others = len(card.get("fields") or []) + int(card.get("fields_hidden") or 0)
    visibility = card.get("visibility")
    if isinstance(visibility, str):
        visibility = VISIBILITY.get(visibility.upper(), visibility)
    media = card.get("media") or []
    fields = [
        _field("What it is", KIND_WORDS.get(kind, "A publish")),
        _field("Title", card.get("title")), _field("Text", text), _field("Link", card.get("link")),
        _field("Goes to", card.get("target")), _field("In reply to", card.get("reply_to")),
        _field("Where", _cap(str(card.get("line") or ""))), _field("Who can see it", visibility),
        _field("Pictures or videos", len(media) if media else None),
        _field("Also sends", f"{others} other values. Saying yes covers all of them." if others else None),
        *warnings,
    ]
    return {"kind": "form", "data": {
        "form": f"{KIND_WORDS.get(kind, 'A publish')} on {site}",
        "ask": ASK.get(kind, "Send this to {site}?").format(site=site),
        "yes": YES.get(kind, "Send it"), "no": NO.get(kind, "Don't send it"),
        "fields": [f for f in fields if f]}}


def _form_value(row: dict[str, Any]) -> tuple[Any, Any]:
    if row.get("hidden"):
        return row["hidden"], None
    if "checked" in row:
        return ("Ticked" if row.get("checked") else "Not ticked"), (
            None if "was_checked" not in row else "Ticked" if row.get("was_checked") else "Not ticked")
    if row.get("files"):
        return row["files"], None
    return row.get("selected") or row.get("value"), row.get("was") if row.get("changed") else None


def _form(card: dict[str, Any], site: str) -> dict[str, Any]:
    fields, changed = [], []
    for row in card.get("fields") or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or row.get("name") or "A field")
        value, before = _form_value(row)
        shown = _field(label, value, before) or {"label": label, "value": "(empty)"}
        fields.append(shown)
        if row.get("changed"):
            changed.append(label)
    return {"kind": "form", "data": {"form": f"A form on {site}", "fields": fields, "changed": changed}}


def _record_fields(record: Any) -> list[dict[str, Any]]:
    """A record with no card, as fields: its plain values by name, the first few."""
    if not isinstance(record, dict):
        return []
    out = []
    for key, value in record.items():
        row = _field(_cap(re.sub(r"[_-]+", " ", str(key)).strip()), value)
        if row is not None:
            out.append(row)
        if len(out) == MAX_FIELDS:
            break
    return out


def looks_like_record(text: str) -> bool:
    """A preview that is a JSON record (or the start of one, cut short), not words."""
    head = text.lstrip()
    return head[:1] in ("{", "[") and ('":' in head or head[:2] in ("{}", "[]", "[{", "[\""))


def lenient_json(text: str) -> Any:
    """A JSON record, or as much of one as a preview cut short still holds: the node keeps the first 2000
    characters of an approval's content, and a push with a long log can be longer. The text is cut back to
    the last whole value and its open brackets closed. None when nothing whole is left."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    cuts: list[tuple[int, str]] = []
    stack: list[str] = []
    in_string = escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            stack.append("]" if ch == "[" else "}")
        elif ch in "]}":
            if stack:
                stack.pop()
            cuts.append((i + 1, "".join(reversed(stack))))
        elif ch == ",":
            cuts.append((i, "".join(reversed(stack))))
    for end, closing in reversed(cuts[-200:]):
        try:
            return json.loads(text[:end] + closing)
        except ValueError:
            continue
    return None


PUSH_LOG_LINES = 20
TRUNCATED = re.compile(r"(\s*\[preview truncated: [^\]]*\]|\.\.\.\[truncated \d+ chars\])\s*$")


def _push(record: dict[str, Any]) -> dict[str, Any]:
    """A git push (openajar.git.gateway): the remote and, per ref, its kind, force, protected and commits."""
    refs = []
    for ref in record.get("refs") or []:
        if not isinstance(ref, dict) or not isinstance(ref.get("ref"), str):
            continue
        name = ref["ref"].removeprefix("refs/heads/").removeprefix("refs/tags/")
        log = [str(line)[:200] for line in (ref.get("log") or []) if isinstance(line, str)][:PUSH_LOG_LINES]
        kind = ref.get("kind") if ref.get("kind") in ("create", "update", "delete") else "update"
        commits = ref.get("commits")
        refs.append({"ref": ref["ref"][:200], "name": name[:200], "kind": kind, "force": ref.get("force") is True,
                     "protected": ref.get("protected") is True,
                     "commits": commits if isinstance(commits, int) and not isinstance(commits, bool) else None,
                     "log": log})
    return {"kind": "push", "data": {"remote": str(record.get("git_push"))[:200], "push_refs": refs}}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _vm(record: dict[str, Any]) -> dict[str, Any]:
    """A disposable cloud machine (openajar.vms): its size, time limit and price, as the node planned them."""
    data: dict[str, Any] = {"computer": str(record.get("create"))[:200], "server_type": str(record.get("server_type"))[:60],
                            "vm_profile": str(record.get("profile") or "")[:60] or None,
                            "price_hourly_eur": _number(record.get("price_hourly_eur")),
                            "max_cost_eur": _number(record.get("max_eur"))}
    ttl = _number(record.get("ttl_minutes"))
    if ttl is not None:
        data["ttl_minutes"] = int(ttl)
    return {"kind": "vm", "data": {k: v for k, v in data.items() if v is not None}}


LANE_SETTINGS = (("network", "Network"), ("allow_hosts", "Can reach"), ("egress_allowlist", "Can reach"),
                 ("base_image", "Starts from"), ("image", "Starts from"), ("filesystem", "Files"),
                 ("memory_mb", "Memory (MB)"), ("cpus", "Processors"), ("disk_mb", "Disk (MB)"))


def _lane(record: dict[str, Any], lane: str | None) -> dict[str, Any]:
    """A new terminal lane (terminal_create), or a command in one (terminal_exec, terminal_job_start)."""
    data: dict[str, Any] = {"computer": (str(record["on"]) if isinstance(record.get("on"), str) else lane or "")[:200]}
    if isinstance(record.get("preset"), str):
        data["lane_preset"] = record["preset"][:60]
        overrides = record.get("overrides") if isinstance(record.get("overrides"), dict) else {}
        fields = [_field(label, overrides.get(key)) for key, label in LANE_SETTINGS if key in overrides]
        data["fields"] = [f for f in fields if f]
    if isinstance(record.get("cmd"), str):
        data["command"] = record["cmd"][:MAX_VALUE * 4]
        if isinstance(record.get("cwd"), str):
            data["cwd"] = record["cwd"][:300]
        timeout = _number(record.get("timeout_s"))
        if timeout is not None:
            data["timeout_s"] = timeout
        sync = record.get("sync") if isinstance(record.get("sync"), dict) else None
        if sync:
            data["sync_dest"] = str(sync.get("dest") or "")[:300] or None
            # the content names the manifest's chunks, not its files: the count is the summary's, so null here
            data["synced_files"] = sync.get("files") if isinstance(sync.get("files"), int) else None
    return {"kind": "lane", "data": data}


def kind_of_record(item: dict[str, Any], record: Any) -> dict[str, Any] | None:
    """The kinds told from what an approval binds: a git push, a VM, a new lane or a command in one."""
    if not isinstance(record, dict):
        return None
    if isinstance(record.get("git_push"), str) and isinstance(record.get("refs"), list):
        return _push(record)
    if item.get("effect") == "vm" and isinstance(record.get("create"), str) and "server_type" in record:
        return _vm(record)
    if item.get("effect") == "exec" and (isinstance(record.get("cmd"), str)
                                         or (isinstance(record.get("on"), str) and "preset" in record)):
        return _lane(record, item.get("lane"))
    return None


def preview_of(item: dict[str, Any], site: str) -> dict[str, Any]:
    """`Approval.preview` without the image: the post's text, or the kind and its fields."""
    card = item.get("card") if isinstance(item.get("card"), dict) else None
    summary = str(item.get("summary") or "")
    if card is not None:
        kind = card.get("type")
        if kind == "publish":
            return _publish(card, summary)
        if kind == "form":
            return _form(card, site)
        if kind == "post":
            text = card.get("text") if isinstance(card.get("text"), dict) else {}
            if isinstance(text.get("value"), str) and text["value"]:
                return {"kind": "post", "text": text["value"]}
    preview = item.get("preview") if isinstance(item.get("preview"), dict) else {}
    text = preview.get("text") if isinstance(preview.get("text"), str) else None
    if not text or not looks_like_record(text):
        return {"text": text} if text else {}
    try:
        record = json.loads(text)
    except ValueError:
        record = None
    # a push, a VM or a lane is told from its fields, which a preview cut short still holds; anything else
    # cut short stays unreadable rather than half shown
    told = kind_of_record(item, record if record is not None else lenient_json(TRUNCATED.sub("", text)))
    if told is not None:
        return told
    if isinstance(record, dict) and isinstance(record.get("record"), dict):
        record = record["record"]  # a publish record kept with no card: what it sends, not where it came from
    fields = _record_fields(record)
    return {"kind": "form", "data": {"form": summary or "The details of this request",
                                     "fields": fields or [{"label": "Details", "value": UNREADABLE}]}}


def routes(api: Api) -> list[BaseRoute]:
    """No route of its own: the decision routes use the words above."""
    del api
    return []
