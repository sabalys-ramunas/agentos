"""Words, colors and icons the dashboard uses. Pure functions, no Textual.

Status words are honest: a run is "verified", "not verified", "partial" or "failed",
never a bare "done". Separators are commas and middle dots, never em dashes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rich.cells import cell_len
from rich.text import Text

from openajar.core.clock import now as now_utc

# ---- palette (matches the Textual theme in openajar.tui.app) ----
OK = "#8fbf7f"
WARN = "#e0b45c"
BAD = "#e07a7a"
INFO = "#7aa6e6"
HUMAN = "#c39be8"
MUTED = "#7d8594"
DIM = "#5a616e"
TEXT = "#d5d9e0"
ACCENT = "#7aa6e6"

SEP = " · "

# ---- lanes ----
KIND_ICON = {"browser": "◎", "terminal": "❯", "service": "⚙", "android": "▣"}
KIND_WORD = {"browser": "browser", "terminal": "terminal", "service": "service", "android": "android"}

LANE_STATE_STYLE = {"cold": DIM, "warming": WARN, "warm": OK, "active": INFO, "quarantined": BAD}

SIGNAL_WORD = {"ok": "ok", "captcha": "captcha", "checkpoint": "security check", "rate_limited": "rate limited",
               "blocked": "blocked",
               "logged_out": "logged out", "unknown": "unknown"}
SIGNAL_STYLE = {"ok": OK, "captcha": WARN, "checkpoint": WARN, "rate_limited": WARN, "blocked": BAD, "logged_out": WARN,
                "unknown": DIM}

HEALTH_STYLE = {"ok": OK, "attention": WARN, "blocked": BAD, "idle": MUTED}

# ---- runs and statuses ----
RUN_STATUS: dict[str, tuple[str, str, str]] = {
    "succeeded_verified": ("verified", OK, "✓"),
    "succeeded_unverified": ("not verified", WARN, "?"),
    "partial": ("partial", WARN, "◐"),
    "failed": ("failed", BAD, "✗"),
    "running": ("running", INFO, "▸"),
    "queued": ("queued", MUTED, "·"),
    "waiting_approval": ("waiting for you", WARN, "!"),
    "cancelled": ("cancelled", MUTED, "-"),
}
TRACE_STATUS: dict[str, tuple[str, str, str]] = {
    "ok": ("ok", MUTED, "·"),
    "error": ("failed", BAD, "✗"),
    "denied": ("denied", BAD, "✗"),
    "approval_pending": ("waiting for you", WARN, "!"),
    "human_in_control": ("human in control", HUMAN, "!"),
    "agent_paused": ("paused", WARN, "!"),
    "waiting": ("waiting", WARN, "…"),
}
WRITE_EFFECTS = {"commit", "pay", "irreversible"}
STATUS_FILTERS = ["verified", "not verified", "partial", "failed", "waiting for you", "running", "ok", "denied"]

BRAIN_STATE = {"ready": ("ready", OK), "cooling_down": ("cooling down", WARN),
               "needs_auth": ("needs login", WARN), "down": ("down", BAD), "busy": ("busy", INFO)}
NODE_STATE = {"up": ("up", OK), "degraded": ("degraded", WARN), "down": ("down", BAD)}

EFFECT_ORDER = ["pay", "irreversible", "commit", "vm", "install_base", "install_overlay", "exec", "secret",
                "brain", "service", "draft", "read"]
EFFECT_HINT = {
    "pay": "spends money",
    "irreversible": "delete or send, cannot be undone",
    "commit": "publish or post on the site",
    "install_base": "changes a shared base image",
    "install_overlay": "adds a program to one lane",
    "exec": "runs commands in a lane",
    "secret": "fills a login from the vault",
    "brain": "spends model capacity",
    "vm": "rents a cloud VM, billed by the hour",
    "service": "runs a long-running service",
    "draft": "private write",
    "read": "read only",
}
EFFECT_STYLE = {"pay": BAD, "irreversible": BAD, "commit": WARN, "install_base": WARN, "vm": WARN}


def norm(value: str | None) -> str:
    return (value or "").strip().lower().replace("-", "_")


def status_info(status: str | None, effect: str | None = None) -> tuple[str, str, str]:
    """(word, color, glyph) for a run or trace status, honest about verification."""
    key = norm(status)
    if key in RUN_STATUS:
        return RUN_STATUS[key]
    if key in TRACE_STATUS:
        word, color, glyph = TRACE_STATUS[key]
        if key == "ok" and norm(effect) in WRITE_EFFECTS:
            return RUN_STATUS["succeeded_unverified"]
        return word, color, glyph
    if key in ("done", "success", "succeeded", "completed", "complete"):
        # nothing proved the effect happened, so say so
        if norm(effect) in WRITE_EFFECTS or not effect:
            return RUN_STATUS["succeeded_unverified"]
        return TRACE_STATUS["ok"]
    if key in ("verified",):
        return RUN_STATUS["succeeded_verified"]
    if not key:
        return ("unknown", DIM, "·")
    return (key.replace("_", " "), MUTED, "·")


def status_text(status: str | None, effect: str | None = None, *, glyph: bool = True) -> Text:
    word, color, mark = status_info(status, effect)
    return Text(f"{mark} {word}" if glyph else word, style=color)


# ---- time ----


def ago(dt: datetime | None, now: datetime | None = None, *, never: str = "never") -> str:
    if dt is None:
        return never
    now = now or now_utc()
    s = (now - dt).total_seconds()
    if s < 0:
        return until(dt, now)
    if s < 10:
        return "just now"
    if s < 60:
        return f"{int(s)}s ago"
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


def until(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return ""
    now = now or now_utc()
    s = (dt - now).total_seconds()
    if s <= 0:
        return "now"
    if s < 60:
        return f"in {int(s)}s"
    if s < 3600:
        return f"in {int(s // 60)}m"
    if s < 86400:
        h, m = divmod(int(s // 60), 60)
        return f"in {h}h {m}m" if m and h < 10 else f"in {h}h"
    return f"in {int(s // 86400)}d"


def zone(name: str | None) -> tzinfo:
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo or UTC


def clock(dt: datetime | None, tz: tzinfo, now: datetime | None = None, *, seconds: bool = False) -> str:
    """14:05 today, otherwise Nov 3 14:05, in the given timezone."""
    if dt is None:
        return ""
    local = dt.astimezone(tz)
    today = (now or now_utc()).astimezone(tz).date()
    hm = local.strftime("%H:%M:%S" if seconds else "%H:%M")
    if local.date() == today:
        return hm
    return f"{local.strftime('%b')} {local.day} {hm}"


def is_today(dt: datetime | None, tz: tzinfo, now: datetime | None = None) -> bool:
    if dt is None:
        return False
    return dt.astimezone(tz).date() == (now or now_utc()).astimezone(tz).date()


def duration(start: datetime | None, end: datetime | None) -> str:
    if not start or not end:
        return ""
    s = int((end - start).total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


# ---- numbers ----
def usd(value: float | None) -> str:
    if value is None:
        return ""
    if value == 0:
        return "$0"
    if value < 0.01:
        return "<$0.01"
    return f"${value:,.2f}"


def tokens(value: int | None) -> str:
    if not value:
        return ""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M tok"
    if value >= 1000:
        return f"{value / 1000:.0f}k tok"
    return f"{value} tok"


def gb(mb: float | None) -> str:
    if mb is None:
        return "?"
    return f"{mb / 1024:.1f}"


def bar(pct: float | None, width: int = 10) -> Text:
    if pct is None:
        return Text("n/a", style=DIM)
    pct = max(0.0, min(100.0, float(pct)))
    filled = round(pct / 100 * width)
    color = OK if pct < 70 else WARN if pct < 90 else BAD
    out = Text("█" * filled, style=color)
    out.append("░" * (width - filled), style=DIM)
    out.append(f" {pct:.0f}%", style=TEXT)
    return out


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ---- text fitting ----
def fit(text: str, width: int) -> str:
    """Cut to `width` cells with an ellipsis; pad to exactly `width`."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text + " " * (width - cell_len(text))
    out, used = [], 0
    for ch in text:
        w = cell_len(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…" + " " * (width - used - 1)


def clip(text: str, width: int) -> str:
    """Cut to `width` cells with an ellipsis, no padding."""
    return fit(text, width).rstrip() if cell_len(text) > width else text


def one_line(text: str | None) -> str:
    return " ".join((text or "").split())


# Block elements of a site's rich-text document (Reddit's richtext JSON: par, h, blockquote, list items):
# each starts a new line when it is read as plain text.
_BLOCKS = {"par", "h", "blockquote", "li", "list", "code", "table", "hr", "img", "gif", "video"}


def readable(text: str | None) -> str:
    """Text as a person reads it. A post a site keeps as a rich-text JSON document
    (`{"document": [{"e": "par", "c": [{"e": "text", "t": "..."}]}]}`) becomes its paragraphs; any other
    text is returned as it is. Never fails: what is not such a document stays as written."""
    raw = text or ""
    stripped = raw.strip()
    if not stripped.startswith(("{", "[")):
        # a document inside a longer line ('text: "{"document": ...}"'): that part is read, the rest kept
        at = raw.find('{"document"')
        if at < 0:
            return raw
        try:
            doc, end = json.JSONDecoder().raw_decode(raw, at)
        except ValueError:
            return raw
        inner = readable(json.dumps(doc))
        head = raw[:at].rstrip().removesuffix('"')
        tail = raw[end:].lstrip().removeprefix('"')
        return "\n".join(part for part in (head.rstrip(), inner, tail.strip()) if part)
    try:
        doc = json.loads(stripped)
    except ValueError:
        return raw
    lines: list[str] = []

    def walk(node: object, line: list[str]) -> None:
        if isinstance(node, str):
            return
        if isinstance(node, list):
            for child in node:
                walk(child, line)
            return
        if not isinstance(node, dict):
            return
        block = node.get("e") in _BLOCKS
        target = [] if block else line
        for key in ("t", "text", "title", "body"):
            if isinstance(node.get(key), str):
                target.append(node[key])
        for key in ("c", "document", "children", "content"):
            if key in node:
                walk(node[key], target)
        if block and target:
            lines.append("".join(target))

    top: list[str] = []
    walk(doc, top)
    if top:
        lines.insert(0, "".join(top))
    out = "\n".join(line for line in lines if line.strip())
    return out or raw
