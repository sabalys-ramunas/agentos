"""Form fields as an agent or the owner may see them: one page read, redacted at the source.

Two users, one reader:

- the hand-back note (openajar.node.handback) compares the fields of the page the human was given
  with the page they handed back, and lists which fields changed;
- browser_act's form diff (openajar.node.tools) lists, before a submit, the fields the form will send
  and how each differs from what the page loaded with, so the owner's approval card shows them.

The read is a single `evaluate` in Playwright's utility world (Camoufox's isolated sandbox): it reads
DOM properties and never writes to the page. A field's value leaves the page only when the field is
known safe: never for a password, a one-time code, a card number or security code, a hidden input,
or anything whose name, id or autocomplete says it holds a secret. Every field carries a short
digest of its value made in the page (cyrb53, for telling values apart, not a secret), so a change
is visible without the value. Safe text is clipped, and redact_text scrubs secret shapes from it.

browser_act's read (FORM_DIFF_JS) also takes each field's whole raw value, and the form's whole
target URL, method and encoding, and turns each into a keyed digest at once (bind_digest, a key that
never leaves the process): the approval binds those, so a value past the clipped text, a card number
swapped for another that masks the same, or a hidden or password field changed is a change. The raw
values are never kept or shown.

A known gap: only a native submit (a submit button, an input of type submit or image, Enter in a
text input) is bound. A link, a div with role=button or an input of type button whose script posts
the form submits none that FORM_DIFF_JS can see, so its approval binds the click only.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Any

from openajar.node.human import quietly
from openajar.node.snapshot import find_refs, parse_line
from openajar.policy.redact import redact_text

MAX_FIELDS = 200          # fields read per page
VALUE_CHARS = 120         # a safe text value is clipped to this many characters
LABEL_CHARS = 80
READ_TIMEOUT_S = 3.0

# Shared by both reads: what kind of field an element is, its role and label, and its state.
_FIELD_FNS = r"""
const SECRET_WORDS = /pass|pwd|otp|one.?time|2fa|totp|mfa|\bpin\b|cvv|cvc|csc|ssn|secret|token|api.?key|\bcode\b|security.?code/i;
const SECRET_AUTOCOMPLETE = /one-time-code|current-password|new-password|cc-number|cc-csc|cc-exp/i;
const clip = (s, n) => { s = (s || '').replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n) : s; };
const hash = (str) => {  // cyrb53: tells values apart; never leaves the node
  let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
  for (let i = 0; i < str.length; i++) {
    const ch = str.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761); h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(36);
};
const kindOf = (el) => {
  const tag = el.tagName.toLowerCase();
  if (tag === 'select') return 'choice';
  if (tag === 'textarea') return 'text';
  if (tag !== 'input') return el.isContentEditable ? 'text' : null;
  const t = (el.type || 'text').toLowerCase();
  if (t === 'hidden') return 'hidden';
  if (t === 'password') return 'secret';
  if (t === 'checkbox' || t === 'radio') return 'check';
  if (t === 'file') return 'file';
  if (['button', 'submit', 'reset', 'image'].includes(t)) return null;
  return 'text';
};
const roleOf = (el) => {
  const explicit = el.getAttribute('role');
  if (explicit) return explicit.split(' ')[0];
  const tag = el.tagName.toLowerCase();
  if (tag === 'select') return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
  if (tag !== 'input') return 'textbox';
  const t = (el.type || 'text').toLowerCase();
  return {checkbox: 'checkbox', radio: 'radio', search: 'searchbox', number: 'spinbutton', range: 'slider',
          file: 'button'}[t] || 'textbox';
};
const labelOf = (el) => {
  const aria = el.getAttribute('aria-label');
  if (aria) return clip(aria, 80);
  const by = el.getAttribute('aria-labelledby');
  if (by) {
    const text = by.split(/\s+/).map((id) => document.getElementById(id)?.textContent || '').join(' ');
    if (text.trim()) return clip(text, 80);
  }
  for (const l of el.labels || []) if (l.textContent.trim()) return clip(l.textContent, 80);
  return clip(el.getAttribute('placeholder') || el.getAttribute('title') || el.name || el.id || '', 80);
};
const secretByName = (el) => SECRET_WORDS.test([el.name, el.id, el.getAttribute('autocomplete')].join(' '))
  || SECRET_AUTOCOMPLETE.test(el.getAttribute('autocomplete') || '');
const stateOf = (el, kind) => {
  const tag = el.tagName.toLowerCase();
  if (kind === 'check') return {now: el.checked ? 'on' : 'off', was: el.defaultChecked ? 'on' : 'off', checked: el.checked};
  if (kind === 'file') {
    const names = Array.from(el.files || [], (f) => clip(f.name, 80));
    return {now: names.join('\n'), was: '', files: names};
  }
  if (tag === 'select') {
    const opts = Array.from(el.options || []);
    const now = opts.filter((o) => o.selected).map((o) => clip(o.label || o.textContent, 80));
    const was = opts.filter((o) => o.defaultSelected).map((o) => clip(o.label || o.textContent, 80));
    if (!was.length && opts.length && !el.multiple) was.push(clip(opts[0].label || opts[0].textContent, 80));
    return {now: now.join('\n'), was: was.join('\n'), selected: now};
  }
  if (el.isContentEditable && tag !== 'input' && tag !== 'textarea') return {now: el.textContent || '', was: null};
  return {now: el.value || '', was: el.defaultValue || ''};
};
// What the field sends, whole and unmasked, for binding an approval to it: read only by
// FORM_DIFF_JS, turned into a keyed digest on the node at once (parse_fields) and never kept.
const rawOf = (el, kind, st) => {
  if (kind === 'check') return JSON.stringify([el.checked, el.value || '']);
  if (kind === 'file') return JSON.stringify(Array.from(el.files || [], (f) => [f.name, f.size, f.lastModified]));
  if (el.tagName.toLowerCase() === 'select')
    return JSON.stringify(Array.from(el.options || []).filter((o) => o.selected).map((o) => [o.value, o.label]));
  return JSON.stringify([st.now]);
};
const describe = (el, i, withRaw) => {
  let kind = kindOf(el);
  if (!kind) return null;
  if (kind === 'text' && secretByName(el)) kind = 'secret';
  const st = stateOf(el, kind);
  const tag = el.tagName.toLowerCase();
  const out = {
    key: [tag, (el.type || '').toLowerCase(), el.name || el.id || '', i].join('|'),
    kind, role: roleOf(el), label: labelOf(el), name: clip(el.name || '', 80),
    digest: hash(st.now), dirty: st.was !== null && st.now !== st.was,
    disabled: !!el.disabled, in_form: !!el.form,
  };
  if (kind === 'text') { out.value = clip(st.now, 400); if (st.was !== null) out.was = clip(st.was, 400); }
  if (kind === 'choice') { out.selected = st.selected; out.was = st.was; }
  if (kind === 'check') { out.checked = st.checked; out.was_checked = st.was === 'on'; }
  if (kind === 'file') out.files = st.files;
  if (withRaw) out.raw = rawOf(el, kind, st);
  return out;
};
const controls = (root) => Array.from(
  root.querySelectorAll('input, textarea, select, [contenteditable=""], [contenteditable="true"]'));
const keyed = (list) => {
  const seen = {};
  return list.map((el) => {
    const base = [el.tagName.toLowerCase(), (el.type || '').toLowerCase(), el.name || el.id || ''].join('|');
    seen[base] = (seen[base] || 0) + 1;
    return [el, seen[base] - 1];
  });
};
"""

# Every field of the document, for the hand-back note, plus the document's identity.
PAGE_FIELDS_JS = "(max) => {" + _FIELD_FNS + r"""
  const fields = [];
  for (const [el, i] of keyed(controls(document))) {
    if (fields.length >= max) break;
    const d = describe(el, i);
    if (d) fields.push(d);
  }
  return {doc: String(performance.timeOrigin), fields};
}
"""

# The form a click or an Enter would submit, and its fields, for browser_act's form diff. `el` is
# the target; `key` is "click" or "press". null when the action submits no form.
FORM_DIFF_JS = "(el, [action, max]) => {" + _FIELD_FNS + r"""
  let form = null;
  if (action === 'click') {
    const button = el.closest('button, input[type=submit], input[type=image]');
    if (button && button.form && (button.tagName.toLowerCase() !== 'button'
        || !button.getAttribute('type') || button.type === 'submit')) form = button.form;
  } else if (action === 'key') {
    // a key or typed newline the gate counts as a submit (egress.helpers._press_mode): Enter in a
    // field, Space or Enter on a button, a select's Enter. Any field or button inside a form may send
    // it, whatever its type, so the form is read for all of them (a page script can submit on any key)
    const owner = el.closest('button, input, select, textarea');
    form = (owner && owner.form) || el.form || el.closest('form');
  }
  if (!form) return null;
  const fields = [];
  for (const [f, i] of keyed(Array.from(form.elements))) {
    if (fields.length >= max) break;
    const d = describe(f, i, true);
    if (d) fields.push(d);
  }
  let target = '', full = '';
  try {
    const u = new URL(form.action || location.href, location.href);
    target = u.host + u.pathname; full = u.href;
  } catch (e) {}
  const enctype = (form.enctype || '').toLowerCase(), method = (form.method || 'get').toLowerCase();
  // the field the act itself writes (a type, a fill, a select): its new value is the act, not a change
  const ownField = el.closest('input, select, textarea');
  const own = (ownField && ownField.form === form && ownField.name) || '';
  return {action: target, method, fields, own, raw: JSON.stringify([full, method, enctype, form.target || ''])};
}
"""

SECRET_NOTE = "a password or code field: its value is never shown"

# The key of the binding digests: one per process, never written or sent. A digest keyed with it
# says nothing about a value outside the node (a short password cannot be guessed back from it), and
# an approval asked before a restart no longer matches after it (the agent is asked again).
_BIND_KEY = os.urandom(32)


def bind_digest(raw: Any) -> str:
    """A keyed digest of what a field sends, whole and unmasked; "" when the page sent none."""
    if not isinstance(raw, str):
        return ""
    return hmac.new(_BIND_KEY, raw.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class FieldState:
    """One field as the reader saw it. `value` is set only for safe text (clipped and redacted)."""
    key: str
    kind: str                  # text, secret, hidden, check, choice, file
    role: str
    label: str
    name: str
    digest: str
    dirty: bool = False        # differs from what the page loaded with
    disabled: bool = False
    value: str | None = None
    was: str | None = None     # what the page loaded with (safe text, or the selected option)
    checked: bool | None = None
    was_checked: bool | None = None
    selected: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    bind: str = ""             # bind_digest of the whole raw value (browser_act's form read only)


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if len(text) > VALUE_CHARS:
        text = text[:VALUE_CHARS] + "..."
    return redact_text(text, max_len=0)


def parse_fields(raw: Any) -> list[FieldState]:
    """FieldStates from the page's answer. Anything malformed is dropped; a secret or hidden field
    keeps no value whatever the page sent."""
    out: list[FieldState] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        kind = str(item.get("kind") or "")
        if kind not in ("text", "secret", "hidden", "check", "choice", "file"):
            continue
        safe = kind == "text"
        selected = item.get("selected") if kind == "choice" else None
        out.append(FieldState(
            key=item["key"][:300], kind=kind, role=str(item.get("role") or "textbox")[:40],
            label=str(item.get("label") or "")[:LABEL_CHARS], name=str(item.get("name") or "")[:LABEL_CHARS],
            digest=str(item.get("digest") or "")[:40], dirty=bool(item.get("dirty")),
            disabled=bool(item.get("disabled")),
            value=_safe_text(item.get("value")) if safe else None,
            was=(_safe_text(item.get("was")) if safe or kind == "choice" else None),
            checked=bool(item["checked"]) if kind == "check" and "checked" in item else None,
            was_checked=bool(item["was_checked"]) if kind == "check" and "was_checked" in item else None,
            selected=tuple(_safe_text(s) or "" for s in selected) if isinstance(selected, list) else (),
            files=tuple(str(f)[:LABEL_CHARS] for f in item.get("files") or []) if kind == "file" else (),
            bind=bind_digest(item.get("raw")),
        ))
    return out


async def read_page_fields(page: Any) -> tuple[str | None, list[FieldState]]:
    """(the document's identity, its fields), or (None, []) when the page cannot be read."""
    raw = await quietly(page.evaluate(PAGE_FIELDS_JS, MAX_FIELDS), None, timeout=READ_TIMEOUT_S)
    if not isinstance(raw, dict):
        return None, []
    doc = raw.get("doc")
    return (str(doc) if doc is not None else None), parse_fields(raw.get("fields"))


def ref_for(outline: str | None, f: FieldState) -> str | None:
    """The ref of the field in an outline: the one line with its role and label (exact, then case and
    space insensitive). None when there is no outline or the match is not unique."""
    if not outline or not f.label:
        return None
    refs = find_refs(outline, role=f.role, name=f.label)
    if len(refs) == 1:
        return refs[0]
    if refs:
        return None
    folded = " ".join(f.label.split()).casefold()
    loose = []
    for raw in outline.splitlines():
        line = parse_line(raw)
        if line.role == f.role and line.ref and line.name and " ".join(line.name.split()).casefold() == folded:
            loose.append(line.ref)
    return loose[0] if len(loose) == 1 else None


def describe_value(f: FieldState) -> dict[str, Any]:
    """What a field holds, as shown to an agent or the owner: never a secret or hidden value."""
    if f.kind == "secret":
        return {"value": None, "hidden": SECRET_NOTE}
    if f.kind == "hidden":
        return {"value": None, "hidden": "a hidden field: its value is never shown"}
    if f.kind == "check":
        return {"checked": bool(f.checked)}
    if f.kind == "choice":
        return {"selected": list(f.selected)}
    if f.kind == "file":
        return {"files": list(f.files)}
    return {"value": f.value}


def form_diff(raw: Any) -> dict[str, Any] | None:
    """browser_act's form diff from FORM_DIFF_JS: where the form goes and its fields, each with what
    the page loaded with and what will be sent. Unchecked boxes and disabled fields send nothing."""
    if not isinstance(raw, dict):
        return None
    fields = []
    for f in parse_fields(raw.get("fields")):
        row: dict[str, Any] = {"label": f.label or f.name or "(no label)", "role": f.role, "name": f.name,
                               "changed": f.dirty}
        if f.disabled or (f.kind == "check" and not f.checked):
            row["sent"] = False
        if f.kind == "text":
            row.update({"value": f.value, "was": f.was})
        elif f.kind == "choice":
            row.update({"value": "\n".join(f.selected), "was": f.was})
        elif f.kind == "check":
            row.update({"checked": bool(f.checked), "was_checked": bool(f.was_checked)})
        else:
            row.update(describe_value(f))
        if f.bind:
            row["bind"] = f.bind
        fields.append(row)
    action = str(raw.get("action") or "")[:300]
    method = str(raw.get("method") or "get")[:10]
    out = {"action": redact_text(action, max_len=0), "method": method, "fields": fields}
    if isinstance(raw.get("raw"), str):  # the whole target URL (its query too), method, encoding, window
        out["bind"] = bind_digest(raw["raw"])
    if isinstance(raw.get("own"), str) and raw["own"]:  # the name of the field the act writes, if it has one
        out["own"] = raw["own"][:200]
    return out


class FormUnreadable(Exception):
    """The page did not answer the form read (a detached target, a page that hangs, an error)."""


_UNREAD = object()


async def read_form_diff(page: Any, target: Any, action: str) -> dict[str, Any] | None:
    """The form diff for an act on `target` (a Locator) that may submit one, or None when it submits
    no form. `action` is the act's submit mode (openajar.node.egress.helpers._press_mode): "click", or
    "key" for a key or typed newline that can submit ("press" is read as "key").
    Raises FormUnreadable when the page could not be read: then nobody knows what it would send."""
    if action == "press":
        action = "key"
    if action not in ("click", "key"):
        return None
    raw = await quietly(target.evaluate(FORM_DIFF_JS, [action, MAX_FIELDS]), _UNREAD, timeout=READ_TIMEOUT_S)
    if raw is _UNREAD:
        raise FormUnreadable("the form could not be read")
    return form_diff(raw)
