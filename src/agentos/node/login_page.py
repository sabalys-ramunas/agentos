"""What an assisted login reads from a lane's page, how it presses a button, and the redacted screenshot.

agentos.vault.steps decides what a step is from facts; this module collects them on a real page:

- the typist's fields (agentos.node.typist), and for each the page's own label for it;
- the clickable things (buttons, submit inputs, links, role=button and friends, account items), each
  with a locator `ac1:<frame path>:<index>:<signature>` like the typist's: the index is the element's
  place in the same walk of its document, the signature a hash of its tag, type, role, id, name, link
  host, form and frame origin chain. A locator resolves only to an element at that place with that
  signature, so a press never lands on something the step reader did not see;
- the heading, the message near the form, and a big number shown on its own ("tap 97").

All reads run in Camoufox's isolated world and change nothing on the page.
Text is collected for the owner's form only; nothing here decides by it.

The screenshot is the viewport at CSS scale with every password and one-time-code field painted
over before it leaves this module (the owner sees it; nothing else does, and it is never stored).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import struct
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agentos.core.errors import EngineError
from agentos.node import human
from agentos.node.human import quietly
from agentos.node.typist import _PRELUDE, MAX_FRAME_DEPTH, Locator, PlaywrightTypist, _child_frame, path_text
from agentos.vault.policy import FieldRole, classify_fields
from agentos.vault.steps import Action, PageRead
from agentos.vault.typist import FieldInfo

log = logging.getLogger(__name__)

Page = Any
Frame = Any

ACTION_VERSION = "ac1"
SCREENSHOT_TIMEOUT_MS = 10_000
REDACT_PAD = 4                     # CSS pixels around a hidden field
REDACT_RGBA = (38, 40, 46, 255)
MAX_ACTIONS = 200                  # per frame: a page with thousands of links is not a sign-in step

_CLICKS = """
  const CLICK_ROLES = new Set(['button', 'link', 'option', 'menuitem', 'radio', 'tab']);
  const isClick = (el) => {
    const tag = el.localName;
    if (tag === 'button') return true;
    if (tag === 'input') { const t = String(el.type || '').toLowerCase(); return t === 'submit' || t === 'button' || t === 'image'; }
    if (tag === 'a' && el.hasAttribute('href')) return true;
    if (CLICK_ROLES.has(String(el.getAttribute('role') || '').toLowerCase())) return true;
    return el.hasAttribute('data-identifier') || el.hasAttribute('data-email');
  };
  const clicks = (doc) => {
    const out = [];
    const stack = [doc];
    while (stack.length) {
      const node = stack.pop();
      if (node.nodeType === 1 && node.namespaceURI === HTML_NS && isClick(node)) out.push(node);
      const kids = node.children;
      if (kids) for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i]);
      if (node.nodeType === 1 && node.shadowRoot) stack.push(node.shadowRoot);
    }
    return out;
  };
  const textOf = (el) => String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
  const ashape = (el, forms) => {
    const tag = el.localName;
    const type = (tag === 'button' || tag === 'input') ? String(el.type || '').toLowerCase() : '';
    let host = '';
    if (tag === 'a') { try { host = new URL(el.href).hostname.toLowerCase(); } catch (e) { host = ''; } }
    const owner = (tag === 'button' || tag === 'input') ? el.form : el.closest('form');
    return {tag, type, role: String(el.getAttribute('role') || '').toLowerCase(), id: el.getAttribute('id') || '',
            name: el.getAttribute('name') || '', host, form: owner ? forms.indexOf(owner) : null};
  };
"""

# Per frame: labels of the typist's controls, the clickables, and (top frame) the page's own words.
_READ_JS = """(top) => {""" + _PRELUDE + _CLICKS + """
  const w = walk(document);
  const byId = (id) => { const el = id ? document.getElementById(id) : null; return el ? textOf(el) : ''; };
  const labelOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria;
    if (el.labels && el.labels.length) { const t = textOf(el.labels[0]); if (t) return t; }
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = by.split(/\\s+/).map(byId).join(' ').trim(); if (t) return t; }
    return el.getAttribute('placeholder') || el.getAttribute('title') || '';
  };
  const labels = w.controls.map((el) => String(labelOf(el)).slice(0, 200));
  const actions = clicks(document).slice(0, %MAX%).map((el, index) => {
    const s = ashape(el, w.forms);
    const label = el.getAttribute('aria-label') || (s.tag === 'input' ? (el.value || el.getAttribute('alt') || '') : '')
      || textOf(el) || el.getAttribute('title') || '';
    s.index = index;
    s.label = String(label).slice(0, 200);
    s.hint = [s.id, s.name, el.getAttribute('class'), el.getAttribute('data-provider'), el.getAttribute('data-testid')]
      .filter(Boolean).join(' ').slice(0, 200);
    s.identifier = el.hasAttribute('data-identifier') || el.hasAttribute('data-email');
    s.visible = shown(el);
    s.disabled = el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true';
    return s;
  });
  const frames = w.frames.map((el, index) => ({index, visible: shown(el)}));
  const out = {origin: originOf(document), labels, actions, frames};
  if (!top) return out;
  // The page's own words, near the form: the closest form, dialog or main around the first shown field
  // or button, else the body. Only for the owner to read.
  const firstShown = w.controls.find((el) => shown(el)) || clicks(document).find((el) => shown(el));
  const box = (firstShown && firstShown.closest('form, [role=dialog], dialog, main')) || document.body || document.documentElement;
  const heading = [...box.querySelectorAll('h1, [role=heading][aria-level="1"]'), ...document.querySelectorAll('h1'),
                   ...box.querySelectorAll('h2, [role=heading]')].find((el) => shown(el) && textOf(el));
  out.heading = heading ? textOf(heading).slice(0, 200) : '';
  const skip = (el) => el.closest('button, a, label, select, option, script, style, noscript, template, [role=button], [role=link]');
  const parts = [];
  let size = 0;
  const all = box.querySelectorAll('*');
  for (let i = 0; i < all.length && size < 900; i++) {
    const el = all[i];
    if (el === heading || skip(el) || !shown(el)) continue;
    let own = '';
    for (const n of el.childNodes) if (n.nodeType === 3) own += n.nodeValue;
    own = own.replace(/\\s+/g, ' ').trim();
    if (own.length < 2) continue;
    const alert = el.matches('[role=alert], [aria-live], [role=status]');
    if (own.length < 12 && !alert && !/^\\d{1,3}$/.test(own)) continue;
    if (!parts.includes(own)) { parts.push(own); size += own.length + 1; }
  }
  out.message = parts.join(' ').slice(0, 900);
  let number = null;
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length || !shown(el)) continue;
    const t = String(el.textContent || '').trim();
    if (!/^\\d{1,3}$/.test(t)) continue;
    const size = parseFloat(el.ownerDocument.defaultView.getComputedStyle(el).fontSize) || 0;
    if (size >= 24) { number = t; break; }
  }
  out.number = number;
  return out;
}""".replace("%MAX%", str(MAX_ACTIONS))

_ACTION_AT_JS = """(i) => {""" + _PRELUDE + _CLICKS + """ return clicks(document)[i] || null; }"""
_FRAME_AT_JS = """(i) => {""" + _PRELUDE + """ return walk(document).frames[i] || null; }"""
_ACTION_SHAPE_JS = """(el) => {""" + _PRELUDE + _CLICKS + """
  const s = ashape(el, walk(el.ownerDocument).forms);
  s.connected = el.isConnected;
  s.visible = shown(el);
  s.origin = originOf(el.ownerDocument);
  s.index = clicks(el.ownerDocument).indexOf(el);
  return s;
}"""

_ACTION_RE = re.compile(r"^ac1:(top|\d{1,6}(?:\.\d{1,6}){0,7}):(\d{1,6}):([0-9a-f]{32})$")


def action_signature(shape: Mapping[str, Any], frame_origins: Sequence[str]) -> str:
    keys = ("tag", "type", "role", "id", "name", "host", "form")
    raw = json.dumps([[shape.get(k) for k in keys], list(frame_origins)], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def action_locator(frames: tuple[int, ...], index: int, shape: Mapping[str, Any], origins: Sequence[str]) -> str:
    return f"{ACTION_VERSION}:{path_text(frames)}:{index}:{action_signature(shape, origins)}"


def parse_action(raw: str) -> tuple[tuple[int, ...], int, str]:
    m = _ACTION_RE.match(raw or "")
    if not m:
        raise ValueError("not an action locator")
    path = () if m.group(1) == "top" else tuple(int(x) for x in m.group(1).split("."))
    return path, int(m.group(2)), m.group(3)


@dataclass
class _Frame:
    path: tuple[int, ...]
    origins: tuple[str, ...]
    data: dict[str, Any]


async def _scan(frame: Frame, path: tuple[int, ...], chain: tuple[str, ...], out: list[_Frame]) -> None:
    data = await frame.evaluate(_READ_JS, not path)
    origins = (*chain, str(data.get("origin")))
    out.append(_Frame(path, origins, data))
    if len(path) >= MAX_FRAME_DEPTH:
        return
    for item in data.get("frames") or ():
        try:
            child = await _child_frame(frame, int(item["index"]))
            if child is not None:
                await _scan(child, (*path, int(item["index"])), origins, out)
        except Exception as e:  # noqa: BLE001 - a frame that navigates or detaches has nothing to read
            log.debug("login page: skipped a frame: %s", type(e).__name__)


async def read_extras(page: Page) -> tuple[list[Action], dict[tuple[tuple[int, ...], int], str], dict[str, Any]]:
    """The page's clickables, its labels by (frame path, control index), and the top frame's words."""
    frames: list[_Frame] = []
    try:
        await _scan(page.main_frame, (), (), frames)
    except Exception as e:  # the main document navigated mid-read: the caller reads again after settling
        raise EngineError(f"could not read the page: {type(e).__name__}") from e
    actions: list[Action] = []
    labels: dict[tuple[tuple[int, ...], int], str] = {}
    for fr in frames:
        for i, text in enumerate(fr.data.get("labels") or ()):
            if text:
                labels[(fr.path, i)] = str(text)
        for a in fr.data.get("actions") or ():
            actions.append(Action(
                locator=action_locator(fr.path, int(a["index"]), a, fr.origins), frame_origins=fr.origins,
                tag=str(a.get("tag") or ""), type=str(a.get("type") or ""), role=str(a.get("role") or ""),
                form=f"{path_text(fr.path)}/form{a['form']}" if isinstance(a.get("form"), int) else None,
                label=str(a.get("label") or ""),
                href_host=str(a.get("host") or ""), hint=str(a.get("hint") or ""),
                identifier=bool(a.get("identifier")), visible=bool(a.get("visible")),
                disabled=bool(a.get("disabled"))))
    top = frames[0].data if frames else {}
    return actions, labels, {"heading": top.get("heading") or "", "message": top.get("message") or "",
                             "number": top.get("number")}


def field_labels(fields: Sequence[FieldInfo], labels: Mapping[tuple[tuple[int, ...], int], str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in fields:
        try:
            loc = Locator.parse(f.locator)
        except ValueError:
            continue
        text = labels.get((loc.frames, loc.index))
        if text:
            out[f.locator] = text
    return out


async def read_page(page: Page, typist: PlaywrightTypist, *, signal: Any, signal_detail: str | None,
                    signed_in: bool, approve: bool) -> PageRead:
    fields = await typist.fields()
    actions, labels, words = await read_extras(page)
    return PageRead(url=page.url, fields=tuple(fields), actions=tuple(actions), labels=field_labels(fields, labels),
                    heading=words["heading"], message=words["message"], number=words["number"], signal=signal,
                    signal_detail=signal_detail, signed_in=signed_in, approve=approve)


# ---- pressing a button ----------------------------------------------------------------------------

async def resolve_action(page: Page, locator: str) -> Any | None:
    """The element a locator names, if it is still there with the same signature, else None."""
    try:
        path, index, sig = parse_action(locator)
    except ValueError:
        return None
    frame = page.main_frame
    handles = []
    try:
        for j in path:
            found = await quietly(frame.evaluate_handle(_FRAME_AT_JS, j), None)
            el = found.as_element() if found is not None else None
            if el is None:
                return None
            handles.append(el)
            frame = await quietly(el.content_frame(), None)
            if frame is None:
                return None
        handle = await quietly(frame.evaluate_handle(_ACTION_AT_JS, index), None)
        element = handle.as_element() if handle is not None else None
        if element is None:
            return None
        shape = await quietly(element.evaluate(_ACTION_SHAPE_JS), None)
        if not isinstance(shape, dict) or not shape.get("connected") or shape.get("index") != index:
            await quietly(element.dispose())
            return None
        origins = []
        for h in handles:
            origin = await quietly(h.evaluate(f"(e) => {{ {_PRELUDE} return originOf(e.ownerDocument); }}"), None)
            origins.append(str(origin))
        origins.append(str(shape.get("origin")))
        if action_signature(shape, origins) != sig or not shape.get("visible"):
            await quietly(element.dispose())
            return None
        return element
    finally:
        for h in handles:
            await quietly(h.dispose())


async def press_action(page: Page, locator: str) -> bool:
    """Click the button with human input. False when it is no longer there as the step saw it."""
    element = await resolve_action(page, locator)
    if element is None:
        return False
    try:
        await human.click(page, element)
        return True
    finally:
        await quietly(element.dispose())


# ---- the screenshot -------------------------------------------------------------------------------

def hidden_fields(fields: Sequence[FieldInfo]) -> list[FieldInfo]:
    """Password and one-time-code fields: painted over in every screenshot."""
    roles = classify_fields(fields)
    return [f for f in fields if (f.type or "").lower() == "password" or roles.get(f.locator) is FieldRole.OTP
            or "one-time-code" in f.autocomplete_tokens]


async def screenshot(page: Page, typist: PlaywrightTypist, fields: Sequence[FieldInfo]) -> bytes | None:
    """The viewport with password and code fields painted over, or None (no picture rather than an
    unredacted one)."""
    hide = hidden_fields(fields)
    try:
        png = await asyncio.wait_for(page.screenshot(type="png", scale="css", caret="initial",
                                                     timeout=SCREENSHOT_TIMEOUT_MS), SCREENSHOT_TIMEOUT_MS / 1000 + 2)
    except Exception as e:  # noqa: BLE001 - a dialog, a navigation: the form still works without a picture
        log.info("login page: no screenshot: %s", type(e).__name__)
        return None
    boxes = []
    for f in hide:
        b = await typist.box(f)
        if b is None:
            continue
        boxes.append(b)
    try:
        return redact_png(png, boxes)
    except Exception as e:  # noqa: BLE001 - never an unredacted picture
        log.warning("login page: screenshot not redacted (%s), so none is shown", type(e).__name__)
        return None


def _chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    out, i = [], 8
    while i + 8 <= len(data):
        (n,) = struct.unpack(">I", data[i:i + 4])
        out.append((data[i + 4:i + 8], data[i + 8:i + 8 + n]))
        i += 12 + n
    return out


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> Any:
    import numpy as np

    stride = width * bpp
    rows = np.frombuffer(raw, dtype=np.uint8)[: height * (stride + 1)].reshape(height, stride + 1)
    out = np.zeros((height, stride), dtype=np.uint8)
    prev = np.zeros(stride, dtype=np.uint8)
    for y in range(height):
        kind, line = int(rows[y, 0]), rows[y, 1:]
        if kind == 0:
            cur = line.copy()
        elif kind == 1:      # Sub: a running sum per channel, mod 256 (what Firefox writes)
            cur = np.cumsum(line.reshape(width, bpp), axis=0, dtype=np.uint8).reshape(stride)
        elif kind == 2:      # Up
            cur = line + prev
        elif kind in (3, 4):  # Average, Paeth: byte by byte
            cur = bytearray(line.tobytes())
            up = prev.tobytes()
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                b = up[i]
                if kind == 3:
                    cur[i] = (cur[i] + ((a + b) >> 1)) & 0xFF
                else:
                    c = up[i - bpp] if i >= bpp else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                    cur[i] = (cur[i] + pred) & 0xFF
            cur = np.frombuffer(bytes(cur), dtype=np.uint8)
        else:
            raise ValueError("bad PNG filter")
        out[y] = cur
        prev = cur
    return out


def redact_png(data: bytes, boxes: Sequence[Mapping[str, float]], *, pad: int = REDACT_PAD) -> bytes:
    """Paint each box (CSS pixels, the screenshot's own scale) over an 8-bit RGB or RGBA PNG."""
    import numpy as np

    chunks = _chunks(data)
    head = next(body for kind, body in chunks if kind == b"IHDR")
    width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", head)
    if depth != 8 or color not in (2, 6) or interlace != 0:
        raise ValueError("unsupported PNG")
    if not boxes:
        return data
    bpp = 4 if color == 6 else 3
    pixels = _unfilter(zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT")),
                       width, height, bpp).reshape(height, width, bpp)
    fill = np.array(REDACT_RGBA[:bpp], dtype=np.uint8)
    for b in boxes:
        x0 = max(0, int(b["x"]) - pad)
        y0 = max(0, int(b["y"]) - pad)
        x1 = min(width, int(b["x"] + b["width"]) + pad + 1)
        y1 = min(height, int(b["y"] + b["height"]) + pad + 1)
        if x1 > x0 and y1 > y0:
            pixels[y0:y1, x0:x1] = fill
    flat = pixels.reshape(height, width * bpp)
    sub = flat.copy()
    sub[:, bpp:] = flat[:, bpp:] - flat[:, :-bpp]   # re-encode with Sub, as it came
    body = np.concatenate([np.ones((height, 1), dtype=np.uint8), sub], axis=1).tobytes()
    out = [b"\x89PNG\r\n\x1a\n", _chunk(b"IHDR", head)]
    out.append(_chunk(b"IDAT", zlib.compress(body, 6)))
    out.append(_chunk(b"IEND", b""))
    return b"".join(out)


__all__ = ["action_locator", "hidden_fields", "parse_action", "press_action", "read_page", "redact_png",
           "resolve_action", "screenshot"]
