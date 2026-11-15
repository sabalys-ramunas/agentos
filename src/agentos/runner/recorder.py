"""Recorder: turn the actions someone took in a lane into a draft flow.

Action events are structured, one per executed browser action, and carry a structural
locator resolved at action time (never the snapshot ref the agent used):

    {"action": "click" | "type" | "fill" | "select" | "press" | "scroll" | "upload" | "navigate",
     "url": "...", "locator": {"role": "button", "name": "Like", "within": {...}},
     "text": "...", "option": "...", "key": "Enter", "ok": true, "sensitive": false,
     "state_before": {"attributes": {"aria-pressed": "false"}, "value": ...},
     "state_after":  {"attributes": {"aria-pressed": "true"}, "value": ...}}

The interaction layer pushes events with `observe(lane, event)` while a recording is
active. Typed text becomes a param (named from the field's accessible name), with an
inline readback that the field holds it. A click whose target's own state toggled
(aria-pressed, aria-checked, ...) becomes a writing step with a readback of that state;
when pressing renames the control ("Like" becomes "Remove Like"), the readback looks for it
under its new name in the same anchor. Typing into a sensitive field (a password, a code)
is never stored, not even while the recording runs. Typed text becomes a URL param only
where it is a whole path segment or query value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import unquote, unquote_plus, urlsplit, urlunsplit

from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.locator import Locator
from agentos.core.models import Effect, parse_lane_id
from agentos.runner.store import RunnerStore
from agentos.runner.util import Clock, clip_text

STATE_ATTRS = ("aria-pressed", "aria-checked", "aria-selected", "aria-expanded", "aria-current")
MAX_EVENTS = 2000
_SENSITIVE_RE = re.compile(r"pass(word)?|code|otp|secret|pin\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d[\d.,]*[KkMm]?")
_REGEX_SPECIALS = set("\\^$.|?*+()[]{}/-")  # read the same by Python and the browser


def _sensitive(ev: dict[str, Any]) -> bool:
    loc = ev.get("locator") or {}
    return bool(ev.get("sensitive")) or bool(_SENSITIVE_RE.search(str(loc.get("name") or loc.get("label") or "")))


def _norm(text: Any) -> str:
    return " ".join(str(text).split())


def _name_matches(loc: dict[str, Any], name: str) -> bool:
    want, match = loc.get("name"), loc.get("match", "exact")
    if want is None:
        return True
    if match == "regex":
        try:
            return re.search(want, name) is not None
        except re.error:
            return False
    return _norm(want) in _norm(name) if match == "contains" else _norm(want) == _norm(name)


def _name_like(name: str, match: str) -> tuple[str, str]:
    """(name, match) for a renamed control: count tolerant when the recorded name was."""
    name = _norm(name)
    if match == "regex" and _NUMBER_RE.search(name):
        esc = ["".join("\\" + c if c in _REGEX_SPECIALS else c for c in piece) for piece in _NUMBER_RE.split(name)]
        return "^" + r"\d[\d.,]*[KkMm]?".join(esc) + "$", "regex"
    return name, "exact"


def renamed_after(loc: dict[str, Any] | None, before: dict[str, Any], after: dict[str, Any]) -> str | None:
    """The control's new accessible name when acting renamed it ("Like" -> "Remove Like") and
    the recorded locator no longer matches it; else None."""
    if not loc or not loc.get("role") or loc.get("name") is None:
        return None
    ba = (before.get("attributes") or {}).get("aria-label")
    aa = (after.get("attributes") or {}).get("aria-label")
    if aa is not None:
        new = aa if aa != ba else None
    elif ba is None and before.get("text") is not None and after.get("text") is not None:
        new = after["text"] if _norm(before["text"]) != _norm(after["text"]) else None
    else:
        new = None
    if not new or not _norm(new) or _name_matches(loc, new):
        return None
    return _norm(new)


def _url_with_params(url: str, typed: dict[str, str]) -> str:
    """Typed text becomes ${param} only where it is a whole path segment or query value."""
    if not typed:
        return url
    parts = urlsplit(url)

    def sub(raw: str, decode) -> str | None:
        value = decode(raw)
        return "${" + typed[value] + "}" if value and value in typed else None

    path = "/".join(sub(seg, unquote) or seg for seg in parts.path.split("/"))
    query = []
    for pair in parts.query.split("&") if parts.query else []:
        key, eq, value = pair.partition("=")
        query.append(f"{key}={sub(value, unquote_plus) or value}" if eq else pair)
    return urlunsplit((parts.scheme, parts.netloc, path, "&".join(query), parts.fragment))


def _param_name(locator: dict[str, Any] | None, taken: set[str], fallback: str = "text") -> str:
    raw = (locator or {}).get("name") or (locator or {}).get("label") or fallback
    words = re.findall(r"[a-z0-9]+", raw.lower())
    stop = {"a", "an", "the", "your", "write", "enter", "add", "type", "here", "please"}
    words = [w for w in words if w not in stop][:3] or [fallback]
    name = "_".join(words)[:32].strip("_") or fallback
    if name[0].isdigit():
        name = f"p_{name}"
    if name in ("on", "vars", "trigger", "version", "ctx"):
        name = f"{name}_text"
    base, n = name, 2
    while name in taken:
        name = f"{base}_{n}"
        n += 1
    return name


INPUT_ACTIONS = ("type", "fill", "select", "upload")  # what an action that sends carries
SEND_ACTIONS = ("click", "press")


class Recorder:
    def __init__(self, store: RunnerStore, clock: Clock | None = None):
        self.store = store
        self.clock = clock or Clock()

    def active(self, lane: str) -> bool:
        return self.store.get_recording(lane) is not None

    def start(self, lane: str, principal: dict[str, Any] | None = None) -> dict[str, Any]:
        parse_lane_id(lane)
        now = self.clock.now()
        self.store.put_recording(lane, now, principal or {}, [])
        return {"lane": lane, "recording": True, "started_at": now.isoformat()}

    def observe(self, lane: str, event: dict[str, Any]) -> bool:
        """Append an action event if a recording is active on the lane. Returns True if kept."""
        rec = self.store.get_recording(lane)
        if rec is None:
            return False
        events = rec["events"]
        if len(events) >= MAX_EVENTS:
            return False
        ev = dict(event)
        if _sensitive(ev):
            # never stored, not even until the recording stops; the draft drops the step
            ev["sensitive"] = True
            for key in ("text", "option"):
                ev.pop(key, None)
            for key in ("state_before", "state_after"):
                if isinstance(ev.get(key), dict):
                    ev[key] = {k: v for k, v in ev[key].items() if k not in ("value", "text")}
        ev.setdefault("at", self.clock.now().isoformat())
        events.append(ev)
        self.store.put_recording(lane, rec["started_at"], rec["principal"], events)
        return True

    def stop(self, lane: str, *, pulled: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        rec = self.store.get_recording(lane)
        if rec is None:
            raise NotFound(f"no recording is active on {lane}; start one with flow_record_start")
        self.store.delete_recording(lane)
        events = rec["events"] or list(pulled or [])
        return {"lane": lane, "started_at": rec["started_at"], "principal": rec["principal"], "events": events}

    # ------------------------------------------------------------ events -> draft

    def build(self, events: list[dict[str, Any]], *, lane: str, flow_id: str | None = None,
              effect: Effect | str | None = None, title: str | None = None,
              started_at: datetime | None = None) -> tuple[dict[str, Any], list[str]]:
        """Return (draft definition, notes). The draft still needs review: set the effect and
        add a readback for the final action when the recorder could not derive one."""
        draft, notes, _ = self.build_checked(events, lane=lane, flow_id=flow_id, effect=effect, title=title,
                                             started_at=started_at)
        return draft, notes

    def build_checked(self, events: list[dict[str, Any]], *, lane: str, flow_id: str | None = None,
                      effect: Effect | str | None = None, title: str | None = None,
                      started_at: datetime | None = None) -> tuple[dict[str, Any], list[str], str | None]:
        """build, plus why the draft must not be saved (None when it may): every action the recording
        saw was dropped, so the flow would only navigate, or an input it saw before an action that sends
        (a click or a key press) was dropped, so the replay would send without it (QA-2 #15)."""
        ws, name = parse_lane_id(lane)
        stamp = (started_at or self.clock.now()).strftime("%Y%m%d%H%M")
        flow_id = flow_id or f"{ws}.{name}.draft-{stamp}"
        d = _Draft()
        for ev in events:
            if ev.get("ok") is not False:
                d.add(ev)
        if not d.steps:
            raise InvalidRequest("nothing was recorded: no actions with structural locators reached the recorder")
        eff = Effect(effect) if effect else (Effect.DRAFT if d.acted else Effect.READ)
        if not effect and d.acted:
            d.notes.append("recorded with effect 'draft', but every input step counts as commit until the owner "
                           "marks the steps that publish nothing with \"publishes\": false")
        draft = {
            "id": flow_id, "title": title or f"Recorded on {lane}", "lane": lane, "effect": str(eff),
            # what an agent reads in the typed tool; review advice for the author goes in the lint warnings
            "description": "Replays steps recorded in the browser.",
            "params": {"type": "object", "properties": d.props, "required": d.required, "additionalProperties": False},
            "steps": d.steps,
        }
        return draft, d.notes, d.problem()


@dataclass
class _Draft:
    """What build_checked collects while it reads a recording, event by event."""
    steps: list[dict[str, Any]] = field(default_factory=list)
    props: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    typed: dict[str, str] = field(default_factory=dict)  # typed value -> param name
    acted: bool = False
    last_nav_index: int | None = None
    dropped: list[str] = field(default_factory=list)       # actions the draft left out
    lost_inputs: list[str] = field(default_factory=list)   # inputs among them (typing, a choice, a file)
    sent_without: list[str] = field(default_factory=list)  # those lost before an action that sends was recorded

    def drop(self, action: Any, note: str) -> None:
        self.notes.append(note)
        self.dropped.append(str(action))
        if action in INPUT_ACTIONS:
            self.lost_inputs.append(str(action))

    def add(self, ev: dict[str, Any]) -> None:
        action = ev.get("action")
        loc_data = ev.get("locator")
        if action == "navigate":
            self._navigate(ev)
            return
        if action in ("snapshot", "screenshot", "hover", "read"):
            return
        if action not in ("click", "type", "fill", "select", "press", "scroll", "upload"):
            self.drop(action, f"dropped an unsupported action: {action!r}")
            return
        if not loc_data and action != "press":
            self.drop(action, f"dropped a {action} with no structural locator (only refs were recorded)")
            return
        try:
            loc = Locator.model_validate(loc_data).model_dump(exclude_none=True) if loc_data else None
        except ValueError as e:
            self.drop(action, f"dropped a {action}: {clip_text(str(e), 120)}")
            return
        if loc is not None and self.last_nav_index is not None and self.last_nav_index == len(self.steps) - 1:
            self.steps.append({"type": "wait_for", "locator": loc})
        self.acted = True
        if action in SEND_ACTIONS and self.lost_inputs:
            self.sent_without = list(self.lost_inputs)
        self._act(ev, action, loc)

    def _navigate(self, ev: dict[str, Any]) -> None:
        url = str(ev.get("url") or "")
        if not url:
            return
        step = {"type": "navigate", "url": _url_with_params(url, self.typed)}
        if self.steps and self.steps[-1]["type"] == "navigate":
            self.steps[-1] = step
        else:
            self.steps.append(step)
        self.last_nav_index = len(self.steps) - 1

    def _act(self, ev: dict[str, Any], action: str, loc: dict[str, Any] | None) -> None:
        if action == "click":
            self.steps.append(_click_step(ev, loc))
        elif action in ("type", "fill"):
            self._typing(ev, action, loc)
        elif action == "select":
            self.steps.append({"type": "select", "locator": loc, "option": str(ev.get("option") or ev.get("text") or "")})
        elif action == "press":
            step = {"type": "press", "key": str(ev.get("key") or ev.get("text") or "Enter")}
            if loc:
                step["locator"] = loc
            self.steps.append(step)
        elif action == "scroll":
            self.steps.append({"type": "scroll_to", "locator": loc})
        elif action == "upload":
            pname = _param_name({"name": "file"}, set(self.props), "file")
            self.props[pname] = {"type": "string", "description": "File to upload: a path on the node or a files ref"}
            self.required.append(pname)
            self.steps.append({"type": "upload", "locator": loc, "param": pname})

    def _typing(self, ev: dict[str, Any], action: str, loc: Any) -> None:
        text = str(ev.get("text") or "")
        if _sensitive(ev):
            self.drop(action, "dropped typing into a sensitive field; use login(on) or the vault instead")
            return
        pname = self.typed.get(text) or _param_name(loc, set(self.props))
        if pname not in self.props:
            self.props[pname] = {"type": "string", "description": f"Typed into {Locator(**loc).describe()}",
                                 "examples": [clip_text(text, 80)]}
            self.required.append(pname)
        self.typed[text] = pname
        after = ev.get("state_after") or {}
        cond_kind = "value" if after.get("value") is not None or "value" not in after else "text"
        cond = {"kind": cond_kind, ("equals" if cond_kind == "value" else "contains"): "${" + pname + "}"}
        self.steps.append({"type": "fill" if action == "fill" else "type", "locator": loc, "param": pname,
                           "readback": {"condition": cond}})

    def problem(self) -> str | None:
        """Why the draft must not be saved, or None."""
        sent_without = self.sent_without
        actions = [st for st in self.steps if st["type"] not in ("navigate", "wait_for")]
        if self.dropped and not actions:
            return (f"every action the recording saw ({len(self.dropped)}) was dropped, so the flow would only open "
                    "pages and nothing was saved. Record again, acting on elements the snapshot names "
                    "(see the notes)")
        if sent_without:
            return (f"{len(sent_without)} input{'s' if len(sent_without) != 1 else ''} before a click or key "
                    f"press ({', '.join(sent_without)}) {'were' if len(sent_without) != 1 else 'was'} dropped, "
                    "so the flow would send without them and nothing was saved. Record again (see the notes), "
                    "or save it with flow_save once the steps are whole")
        return None


def _click_step(ev: dict[str, Any], loc: Any) -> dict[str, Any]:
    """A click, with a readback when the control's state attribute changed (pressed, checked)."""
    step: dict[str, Any] = {"type": "click", "locator": loc}
    before = (ev.get("state_before") or {}).get("attributes") or {}
    after = (ev.get("state_after") or {}).get("attributes") or {}
    changed = [a for a in STATE_ATTRS if a in after and before.get(a) != after.get(a)]
    if changed:
        attr = changed[0]
        step["writes"] = True
        step["readback"] = {"condition": {"kind": "attribute", "name": attr, "equals": after[attr]}}
        step["label"] = f"set {attr} to {after[attr]}"
        new_name = renamed_after(loc, ev.get("state_before") or {}, ev.get("state_after") or {})
        if new_name:  # pressed, the control is found by its new name in the same anchor
            name, match = _name_like(new_name, loc.get("match", "exact"))
            renamed = {k: v for k, v in loc.items() if k not in ("nth", "name", "match")}
            step["readback"]["locator"] = {**renamed, "name": name,
                                           **({"match": match} if match != "exact" else {})}
    return step
