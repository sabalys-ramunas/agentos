"""The lane check: one report per lane that answers "is this lane safe to use right now?".

`LaneChecker.check(lane_id)` returns a `LaneReport`: nine checks, each ok, warn, fail or unknown, with
one plain sentence and, when something is wrong, a fix. The lane's level is the worst of them (unknown
never raises it). The same checks back `agentos lane check`, the `lane_check` MCP tool, the panel's
`GET /api/lanes/{ws}/{name}/check` and the periodic health monitor (docs/API.md, "Lane check").

    proxy     the lane's proxy answers, measured from the node through it (never from the lane)
    exit_ip   the exit is allowed for that proxy (the self-check's rule, agentos.node.lanes)
    location  the exit's country and timezone offset match the lane's pinned timezone and locale
    webrtc    WebRTC is off or cannot leave around the proxy (from the effective prefs)
    dns       host names are resolved by the proxy, not the node
    profile   the profile folder, the pin file, the browser version, locks and disk use
    session   still signed in, from the session cookies already in the profile
    account   the account in those cookies is the lane's pinned account (AUD-12)
    hold      a quarantine, a hold, a human in control or a bad last signal, with who can clear it

What a check never does: warm a lane, open a page, ask an account's site anything, take a lock on a
profile file, or write to a profile. Cookies come from the live browser context when the lane is warm
(`context.cookies()`, which the page cannot see), otherwise from a private copy of cookies.sqlite (the
file and its -wal are read with plain reads, copied again until both were stable during the copy, and
only the copy is opened). The report holds names, booleans, counts, IP addresses and times: never a
cookie value, a token or a proxy credential.

Exit IP geography: ipinfo.io (already used for the exit IP) is asked for its JSON answer through the
proxy at most once a day per proxy (GEO_TTL), and the answer is cached in AGENTOS_HOME/cache/exit-geo.json
(ip, country, region, city, timezone). That same request is the proxy probe of that run, so it costs no
extra request. The request carries no cookie and nothing about a lane or an account.

How WebRTC and DNS are judged (measured on Camoufox 158 with Playwright's proxy, tests/node/
test_lanecheck_node.py): Playwright gives Firefox the proxy as a browser-wide channel filter with
TRANSPARENT_PROXY_RESOLVES_HOST, and an HTTP proxy gets the host name in CONNECT, so a proxied lane
resolves no name on the node (dns-prefetch hints and links included). Camoufox's camoufox.cfg sets
media.peerconnection.ice.proxy_only_if_behind_proxy, which stops STUN on pages loaded through the
proxy, but not in an about:blank frame a page creates: that frame sends UDP from the node's own IP.
Only media.peerconnection.ice.proxy_only (or WebRTC off) closes it. Prefs are read in Firefox's
order: camoufox.cfg defaults, the launch prefs (CAMOU_PREFS of the running browser, else what the
engine would set), then the profile's prefs.js and user.js, then camoufox.cfg's locked prefs.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentos.core.config import NodeConfig, load_proxies
from agentos.core.errors import LaneNotFound, NotFound
from agentos.core.models import ControlState, LaneControl, LaneKind, LaneSpec, ProxyConfig, Signal
from agentos.core.signals import STOP
from agentos.ops.layout import connect_ro
from agentos.ops.system import Host, redact
from agentos.stealth import fingerprint as fpmod

log = logging.getLogger("agentos.ops.lanecheck")

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"
LEVELS = (OK, UNKNOWN, WARN, FAIL)          # worst last
_RANK = {level: i for i, level in enumerate(LEVELS)}

CHECK_IDS = ("proxy", "exit_ip", "location", "webrtc", "dns", "profile", "session", "account", "hold")
TITLES = {"proxy": "Proxy", "exit_ip": "Exit IP", "location": "Location", "webrtc": "WebRTC", "dns": "DNS",
          "profile": "Profile", "session": "Signed in", "account": "Account", "hold": "Hold"}

PROXY_TTL = 60.0                 # a proxy probe is reused for this long (an agent may check before each step)
NODE_IP_TTL = 600.0              # the node's own IP is read again after this long
NODE_IP_RETRY = 60.0             # a failed read of the node's own IP is tried again after this long
OFFLINE = "not probed (offline)"
GEO_TTL = 86400.0                # one geo lookup per proxy per day, at most
INSTALLED_TTL = 3600.0           # the installed Camoufox (a subprocess) and its camoufox.cfg
COOKIE_TIMEOUT = 5.0             # reading a warm lane's cookies from its browser
COOKIE_COPY_TRIES = 5
PROFILE_WARN_BYTES = 2 * 1024**3
DISK_FAIL_BYTES = 2 * 1024**3    # the lane manager refuses to warm a lane below this (AUD-23)
DISK_WARN_BYTES = 5 * 1024**3
BAD_SIGNALS = STOP
PIN_META = "agentos:pin"         # agentos.node.engine_camoufox.PIN_META
PIN_VERSION = 2

WEBRTC_PREFS = ("media.peerconnection.enabled", "media.peerconnection.ice.proxy_only",
                "media.peerconnection.ice.proxy_only_if_behind_proxy",
                "media.peerconnection.ice.obfuscate_host_addresses")
DNS_PREFS = ("network.proxy.socks_remote_dns", "network.dns.disablePrefetch", "network.trr.mode")
PROXY_ONLY_FIX = ('add user_pref("media.peerconnection.ice.proxy_only", true); to {path} while the lane is '
                  "cold, so WebRTC never sends UDP around the proxy")

_PREF_RE = re.compile(r'^\s*(defaultPref|pref|lockPref|user_pref)\(\s*"([^"]+)"\s*,\s*(.+?)\s*\)\s*;', re.MULTILINE)


# ---- the report ---------------------------------------------------------------------------

@dataclass
class Check:
    id: str
    level: str
    detail: str
    fix: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return TITLES.get(self.id, self.id)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "level": self.level, "detail": self.detail,
                "fix": self.fix, "data": self.data}


def worst(levels: Iterable[str]) -> str:
    """The lane's level: fail, then warn, else ok. An unknown check never makes a lane unsafe."""
    present = set(levels)
    return FAIL if FAIL in present else WARN if WARN in present else OK


@dataclass
class LaneReport:
    lane: str
    state: str
    at: datetime
    checks: list[Check]

    @property
    def level(self) -> str:
        return worst(c.level for c in self.checks)

    @property
    def safe(self) -> bool:
        return self.level != FAIL

    def failing(self) -> list[str]:
        return [c.id for c in self.checks if c.level == FAIL]

    def summary(self) -> str:
        bad = [c for c in self.checks if c.level == FAIL] or [c for c in self.checks if c.level == WARN]
        if bad:
            return " ".join(c.detail for c in bad)
        unknown = [c.title for c in self.checks if c.level == UNKNOWN]
        if unknown:
            return (f"No known problems: nothing failed, but {len(unknown)} of {len(self.checks)} checks could not "
                    f"run ({', '.join(unknown)}).")
        return "No known problems: every check passed."

    def to_dict(self) -> dict[str, Any]:
        counts = {level: 0 for level in LEVELS}
        for c in self.checks:
            counts[c.level] += 1
        return {"lane": self.lane, "level": self.level, "safe": self.safe, "state": self.state,
                "at": self.at.isoformat(timespec="seconds"), "summary": self.summary(), "counts": counts,
                "checks": [c.to_dict() for c in self.checks]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LaneReport:
        checks = [Check(c["id"], c["level"], c["detail"], c.get("fix"), dict(c.get("data") or {}))
                  for c in data.get("checks", [])]
        return cls(str(data["lane"]), str(data.get("state", "unknown")), _when(data.get("at")) or datetime.now(UTC),
                   checks)

    def brief(self) -> dict[str, Any]:
        """What a list shows: the worst level per lane and which checks fail or warn."""
        return {"lane": self.lane, "level": self.level, "safe": self.safe, "state": self.state,
                "at": self.at.isoformat(timespec="seconds"), "failing": self.failing(),
                "warning": [c.id for c in self.checks if c.level == WARN]}


# ---- facts about one lane -----------------------------------------------------------------

@dataclass
class LaneFacts:
    spec: LaneSpec
    state: str = "cold"
    control: LaneControl | None = None
    quarantine: dict[str, Any] | None = None    # reason, at, by, kind
    signal: dict[str, Any] | None = None        # signal, at, detail
    exit_ip: str | None = None                  # what the lane's own self-check saw last
    context: Any = None                         # the live browser context of a warm lane


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            return _when(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def _stamp(value: Any) -> str | None:
    when = _when(value)
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if when else None


def _fresh(at: float, now: float, ttl: float) -> bool:
    """A cached value stamped `at` is still good at `now`. A stamp in the future (the clock stepped
    back, or a bad clock wrote it) is not fresh."""
    return 0 <= now - at < ttl


def _value(x: Any) -> Any:
    return getattr(x, "value", x)


class ManagerFacts:
    """Facts from the running lane manager (inside agentosd)."""

    def __init__(self, lanes: Any):
        self.lanes = lanes

    def lane_ids(self) -> list[str]:
        return [info.spec.id for info in self.lanes.list()]

    def facts(self, lane_id: str) -> LaneFacts:
        info = self.lanes.get(lane_id)
        state = str(_value(info.state))
        quarantine = None
        store = getattr(self.lanes, "store", None)
        rec = None
        if store is not None:
            try:
                rec = store.get(lane_id)
            except Exception:  # noqa: BLE001 - the LaneInfo below still says it is quarantined
                rec = None
        if rec is not None and rec.quarantine is not None:
            q = rec.quarantine
            quarantine = {"reason": q.reason, "at": q.at, "by": q.by, "kind": q.kind}
        elif state == "quarantined":
            quarantine = {"reason": info.quarantine_reason or "failed self-check", "at": None, "by": "node",
                          "kind": "self_check"}
        signal = None
        if info.signal is not None:
            signal = {"signal": str(_value(info.signal)), "at": info.signal_at,
                      "detail": rec.signal.detail if rec is not None and rec.signal is not None else None}
        context = None
        if state in ("warm", "active"):
            lane = getattr(self.lanes, "_lanes", {}).get(lane_id)  # read only: the warm browser's context
            session = getattr(lane, "session", None)
            context = getattr(session, "context", None)
        return LaneFacts(spec=info.spec, state=state, control=info.control, quarantine=quarantine, signal=signal,
                         exit_ip=info.exit_ip, context=context)

    def pinned_exit(self, label: str) -> str | None:
        store = getattr(self.lanes, "store", None)
        pin = store.proxy_exit(label) if store is not None and hasattr(store, "proxy_exit") else None
        return pin.exit_ip if pin is not None else None


class StoreFacts:
    """Facts read straight from agentos.db, read-only (the daemon is not running). Every lane is cold."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def _rows(self, where: str = "", args: tuple = ()) -> list[tuple]:
        if not self.db_path.exists():
            return []
        conn = connect_ro(self.db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(lanes)")}
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            q = "l.quarantine" if "quarantine" in columns else "NULL"
            s = "l.signal" if "signal" in columns else "NULL"
            control = "c.control" if "lane_control" in tables else "NULL"
            join = " LEFT JOIN lane_control c ON c.id = l.id" if "lane_control" in tables else ""
            sql = f"SELECT l.id, l.spec, l.last_used, l.exit_ip, {control}, {q}, {s} FROM lanes l{join}{where}"
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def lane_ids(self) -> list[str]:
        return sorted(row[0] for row in self._rows())

    def facts(self, lane_id: str) -> LaneFacts:
        from agentos.node.store import LaneStore

        rows = self._rows(" WHERE l.id = ?", (lane_id,))
        if not rows:
            raise LaneNotFound(f"no lane {lane_id}")
        rec = LaneStore._record(rows[0])
        quarantine = None
        if rec.quarantine is not None:
            q = rec.quarantine
            quarantine = {"reason": q.reason, "at": q.at, "by": q.by, "kind": q.kind}
        signal = None
        if rec.signal is not None:
            signal = {"signal": rec.signal.signal.value, "at": rec.signal.at, "detail": rec.signal.detail}
        return LaneFacts(spec=rec.spec, state="quarantined" if quarantine else "cold", control=rec.control,
                         quarantine=quarantine, signal=signal, exit_ip=rec.exit_ip)

    def pinned_exit(self, label: str) -> str | None:
        """The pinned exit, or None when there is no pin. A read that fails raises (sqlite3.Error): an
        unreadable pin is not "no pin"."""
        if not self.db_path.exists():
            return None
        conn = connect_ro(self.db_path)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "proxy_exits" not in tables:
                return None
            row = conn.execute("SELECT exit_ip FROM proxy_exits WHERE label = ?", (label,)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None


# ---- pure rules -----------------------------------------------------------------------------

def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def judge_exit(proxy: ProxyConfig, ip: str, *, host_ips: set[str], pinned: str | None,
               node_ip: str | None, node_ip_known: bool | None = None) -> Check:
    """The lane self-check's rule (agentos.node.lanes._judge_exit) without its side effects: an IP
    literal proxy must exit from its own address; a gateway (a host name) must keep the exit pinned
    when a lane first opened through it; no proxy may exit from the node's own IP. An unpinned gateway
    is not passed while the node's own IP is unknown (`node_ip_known` False): its exit could be the node."""
    node_ip_known = bool(node_ip) if node_ip_known is None else node_ip_known
    label = proxy.label
    data = {"exit_ip": ip, "proxy": label, "host": proxy.host, "pinned": pinned}
    if node_ip and ip == node_ip:
        return Check("exit_ip", FAIL, f"Proxy {label} lets traffic out from this node's own IP {ip}, so the "
                     "account would see the node.", "fix or replace the proxy: agentos proxy add "
                     f"{label} <host>:<port>:<user>", data)
    if _is_ip(proxy.host):
        if ip in host_ips:
            return Check("exit_ip", OK, f"Exits from {ip}, the proxy's own address.", None, data)
        return Check("exit_ip", FAIL, f"Proxy {label} exits from {ip}, not from its own address {proxy.host}; "
                     "opening the lane would quarantine it.", "ask the provider for a static exit, or point the "
                     f"proxy at its exit: agentos proxy add {label} <exit-host>:<port>:<user>", data)
    if pinned is None and not node_ip_known:
        return Check("exit_ip", UNKNOWN, f"Exits from {ip}, but this node's own IP could not be read, so it is "
                     f"not known whether gateway proxy {label} exits from the node itself.", None,
                     {**data, "node_ip_known": False})
    if pinned is None:
        return Check("exit_ip", OK, f"Exits from {ip}; proxy {label} is a gateway, and its first exit is pinned "
                     "when a lane opens through it.", None, data)
    if pinned == ip:
        return Check("exit_ip", OK, f"Exits from {ip}, the address pinned for gateway proxy {label}.", None, data)
    return Check("exit_ip", FAIL, f"Proxy {label} now exits from {ip}, not the pinned {pinned}; the account would "
                 "be seen from a new address, and opening the lane quarantines it.",
                 "check the proxy; to accept the new address, open the lane and hand it back "
                 "(agentos lane handback {lane})", data)


def locale_region(locale: str | None) -> str | None:
    """"en-US" -> "US"; None when the locale names no region ("en")."""
    parts = re.split(r"[-_]", locale or "")
    region = next((p for p in parts[1:] if len(p) == 2 and p.isalpha()), None)
    return region.upper() if region else None


def utc_offset(tz: str | None, at: datetime) -> str | None:
    if not tz:
        return None
    try:
        delta = at.astimezone(ZoneInfo(tz)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError):
        return None
    if delta is None:
        return None
    minutes = int(delta.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def judge_location(geo: Mapping[str, Any] | None, *, pinned_tz: str | None, pinned_country: str | None,
                   locale: str | None, at: datetime, why_unknown: str | None = None) -> Check:
    """The exit's country against the locale's region and the pin's country; its timezone's UTC offset
    (now) against the pinned timezone's. A mismatch is a warning with the exact values."""
    if not geo or not geo.get("country"):
        return Check("location", UNKNOWN, why_unknown or "Where the exit is could not be looked up.", None,
                     {"pinned_timezone": pinned_tz, "locale": locale})
    country = str(geo["country"]).upper()
    tz = geo.get("timezone")
    region = locale_region(locale)
    exit_offset, pinned_offset = utc_offset(tz, at), utc_offset(pinned_tz, at)
    data = {"exit_ip": geo.get("ip"), "exit_country": country, "exit_timezone": tz, "exit_offset": exit_offset,
            "pinned_timezone": pinned_tz, "pinned_offset": pinned_offset, "pinned_country": pinned_country,
            "locale": locale, "looked_up_at": geo.get("at")}
    where = f"{country} ({tz}, {exit_offset})" if tz and exit_offset else country
    problems = []
    if region and region != country:
        problems.append(f"the lane's locale {locale} says {region}")
    moved = False
    if pinned_country and str(pinned_country).upper() != country:
        problems.append(f"the pin was made in {str(pinned_country).upper()}")
        moved = True
    if pinned_offset and exit_offset and pinned_offset != exit_offset:
        problems.append(f"the pinned timezone is {pinned_tz} ({pinned_offset})")
        moved = True
    if problems:
        fix = ("use a proxy in the place the lane was made, or give the account a lane made in the new place "
               "(the pinned timezone never follows the proxy)") if moved else \
              (f"give the lane a locale for {country} (a new lane), or use a proxy in {region}")
        return Check("location", WARN, f"The exit is in {where}, but {', and '.join(problems)}.", fix, data)
    if not pinned_tz:
        return Check("location", OK, f"The exit is in {where}; the timezone is pinned from it on first launch.",
                     None, data)
    if exit_offset is None or pinned_offset is None:
        missing = ("the lookup gave no usable timezone for the exit" if exit_offset is None
                   else f"the pinned timezone {pinned_tz} is not known to this node")
        return Check("location", UNKNOWN, f"The exit is in {where}, but its offset was not compared with the "
                     f"pinned {pinned_tz}: {missing}.", None, data)
    same = "the same timezone" if tz == pinned_tz else f"the same offset as the pinned {pinned_tz}"
    return Check("location", OK, f"The exit is in {where}, {same}.", None, data)


def strip_comments(text: str) -> str:
    """`text` without its /* */ and // comments (Firefox's pref parser skips both), leaving string
    literals alone. A block comment keeps its newlines, so line starts stay where they were."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("\n" * text.count("\n", i, end) or " ")
            i = end
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_prefs(text: str) -> dict[str, dict[str, Any]]:
    """Pref calls in a camoufox.cfg, prefs.js or user.js: {kind: {name: value}} where kind is
    defaultPref, pref, lockPref or user_pref. Calls inside a comment do not count. Values that are not
    JSON literals are skipped."""
    out: dict[str, dict[str, Any]] = {}
    for kind, name, raw in _PREF_RE.findall(strip_comments(text)):
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        out.setdefault(kind, {})[name] = value
    return out


def effective_prefs(*, cfg: Mapping[str, dict[str, Any]] | None, launch: Mapping[str, Any] | None,
                    prefs_js: Mapping[str, Any] | None, user_js: Mapping[str, Any] | None) -> dict[str, Any]:
    """Firefox's order: defaults (camoufox.cfg defaultPref, then the launch prefs, which Camoufox puts on
    the default branch), user values (prefs.js, then user.js, and camoufox.cfg's pref()), locked prefs."""
    cfg = cfg or {}
    out: dict[str, Any] = {}
    for layer in (cfg.get("defaultPref"), launch, prefs_js, user_js, cfg.get("pref"), cfg.get("lockPref")):
        out.update(layer or {})
    return out


def judge_webrtc(prefs: Mapping[str, Any], *, proxied: bool, known: bool, user_js: Path,
                 missing_proxy: str | None = None) -> Check:
    data = {k: prefs.get(k) for k in WEBRTC_PREFS}
    if missing_proxy:
        return Check("webrtc", UNKNOWN, f"The lane's proxy {missing_proxy} is missing, so where WebRTC would "
                     "leave from cannot be judged.", None, data)
    if not proxied:
        return Check("webrtc", OK, "No proxy, so WebRTC shows the node's own IP, the address sites already see.",
                     None, data)
    if prefs.get("media.peerconnection.enabled") is False:
        return Check("webrtc", OK, "WebRTC is off.", None, data)
    if not known:
        return Check("webrtc", UNKNOWN, "The browser's WebRTC settings could not be read (no camoufox.cfg found).",
                     "check that Camoufox is installed: agentos doctor", data)
    if prefs.get("media.peerconnection.ice.obfuscate_host_addresses") is False:
        return Check("webrtc", FAIL, "WebRTC shows the node's real network address in its host candidates.",
                     'remove media.peerconnection.ice.obfuscate_host_addresses from the profile\'s prefs.js '
                     "and user.js while the lane is cold", data)
    if prefs.get("media.peerconnection.ice.proxy_only") is True:
        return Check("webrtc", OK, "WebRTC only connects through the proxy.", None, data)
    if prefs.get("media.peerconnection.ice.proxy_only_if_behind_proxy") is True:
        return Check("webrtc", WARN, "WebRTC stays behind the proxy on normal pages, but a page can make it send "
                     "UDP from this node's own IP through a blank frame.", PROXY_ONLY_FIX.format(path=user_js),
                     data)
    return Check("webrtc", FAIL, "WebRTC can send UDP around the proxy and show this node's own IP.",
                 PROXY_ONLY_FIX.format(path=user_js), data)


def judge_dns(prefs: Mapping[str, Any], *, proxy: ProxyConfig | None, missing_proxy: str | None = None) -> Check:
    data = {k: prefs.get(k) for k in DNS_PREFS}
    if missing_proxy:
        return Check("dns", UNKNOWN, f"The lane's proxy {missing_proxy} is missing, so where host names are "
                     "resolved cannot be judged.", None, data)
    if proxy is None:
        return Check("dns", OK, "No proxy, so names resolve on the node, like all of the lane's traffic.", None,
                     data)
    scheme = proxy.server.split(":", 1)[0].lower()
    data["proxy_scheme"] = scheme
    if scheme in ("http", "https"):
        return Check("dns", OK, "Host names go to the proxy, which resolves them; the node resolves none.", None,
                     data)
    if scheme in ("socks", "socks5"):
        return Check("dns", OK, "Host names go to the SOCKS proxy, which resolves them.", None, data)
    return Check("dns", FAIL, f"A {scheme} proxy cannot take host names, so the node resolves them.",
                 "use an HTTP or SOCKS5 proxy", data)


def judge_hold(facts: LaneFacts, *, now: datetime) -> Check:
    lane = facts.spec.id
    handback = f"agentos lane handback {lane}"
    q = facts.quarantine
    if q is not None:
        since = _stamp(q.get("at"))
        return Check("hold", FAIL, f"Quarantined{' since ' + since if since else ''}: {q.get('reason')}.",
                     f"a human fixes the cause, then hands the lane back ({handback}, or the panel)",
                     {"kind": "quarantine", "since": since, "by": q.get("by"), "reason": q.get("reason"),
                      "quarantine_kind": q.get("kind"), "cleared_by": "human"})
    control = facts.control or LaneControl()
    since = _stamp(control.since)
    if control.state == ControlState.PAUSED and control.reason:
        until = _when(control.hold_until)
        data = {"kind": "hold", "since": since, "by": control.by, "reason": control.reason,
                "until": _stamp(until)}
        if until is not None and until > now:
            return Check("hold", WARN, f"Held until {_stamp(until)}: {control.reason}. It clears itself.",
                         f"wait until {_stamp(until)}, or a human hands it back sooner ({handback})",
                         {**data, "cleared_by": "time"})
        if until is not None:
            return Check("hold", WARN, f"A timed hold ended at {_stamp(until)} ({control.reason}); agentosd lifts "
                         "it on its next round, and agents wait until then.",
                         "wait a minute; if it stays, check that agentosd is running (agentos doctor)",
                         {**data, "cleared_by": "time"})
        return Check("hold", FAIL, f"Held for a human{' since ' + since if since else ''}: {control.reason}.",
                     f"a human looks at the lane (agentos lane view {lane} --control), then hands it back "
                     f"({handback})", {**data, "cleared_by": "human"})
    if control.state == ControlState.PAUSED:
        return Check("hold", FAIL, f"Agents were stopped on this lane{' by ' + control.by if control.by else ''}"
                     f"{' since ' + since if since else ''}.", f"the human hands it back ({handback})",
                     {"kind": "stopped", "since": since, "by": control.by, "cleared_by": "human"})
    if control.state == ControlState.HUMAN:
        return Check("hold", WARN, f"A human has control{' (' + control.by + ')' if control.by else ''}"
                     f"{' since ' + since if since else ''}; agents wait.",
                     f"the human hands it back ({handback}), or it returns after a while without input",
                     {"kind": "human", "since": since, "by": control.by, "cleared_by": "human"})
    sig = facts.signal
    if sig is not None:
        try:
            value = Signal(sig.get("signal"))
        except ValueError:
            value = Signal.UNKNOWN
        if value in BAD_SIGNALS:
            at = _stamp(sig.get("at"))
            where = f" on {sig['detail']}" if sig.get("detail") else ""
            return Check("hold", WARN, f"The last page showed {value.value}{where}{' at ' + at if at else ''}, "
                         "and nothing holds the lane.", f"a human looks at the lane before agents go on "
                         f"(agentos lane view {lane})", {"kind": "signal", "signal": value.value, "since": at})
    return Check("hold", OK, "No hold, no quarantine; agents have control.", None, {"kind": None})


# ---- cookies ----------------------------------------------------------------------------------

def _stat(path: Path) -> tuple[int, int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns, st.st_ino


def _host_matches(cookie_host: str, domains: Iterable[str]) -> bool:
    from agentos.node.sites import host_is

    host = cookie_host.lstrip(".")
    return any(host_is(host, d) for d in domains)


def read_cookie_db(profile: Path, domains: Iterable[str], *, now: float | None = None,
                   scratch: Path | None = None, tries: int = COOKIE_COPY_TRIES) -> list[dict[str, Any]] | None:
    """The unexpired cookies for `domains`, or None when they cannot be read (read_cookies says why)."""
    return read_cookies(profile, domains, now=now, scratch=scratch, tries=tries)[0]


BUSY = "the cookie file could not be copied while the browser wrote it"
DAMAGED = "the cookie file is damaged or is not a cookie database"


def read_cookies(profile: Path, domains: Iterable[str], *, now: float | None = None, scratch: Path | None = None,
                 tries: int = COOKIE_COPY_TRIES) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The unexpired cookies for `domains` (and their subdomains) in a profile's cookies.sqlite, as
    [{name, value, domain, path}], or None when they cannot be read. [] when the profile has no
    cookie file yet.

    The live file is never opened with SQLite (Firefox may hold it and must never meet a lock or a
    -shm of ours): cookies.sqlite and its -wal are copied with plain reads into a private temporary
    folder, again until neither changed during the copy, and only the copy is opened (it replays the
    WAL into the copy). Values stay in this process: callers report names and booleans only."""
    db = Path(profile) / "cookies.sqlite"
    if not db.exists():
        return [], None
    domains = list(domains)
    files = [db, db.with_name(db.name + "-wal")]
    now = time.time() if now is None else now
    why: str | None = BUSY
    for attempt in range(max(1, tries)):
        before = [_stat(f) for f in files]
        try:
            with tempfile.TemporaryDirectory(prefix="agentos-cookies-", dir=scratch) as tmp:
                for f in files:
                    if before[files.index(f)] is not None:
                        shutil.copyfile(f, Path(tmp) / f.name)
                if [_stat(f) for f in files] != before:
                    why = BUSY
                    time.sleep(0.05 * (attempt + 1))
                    continue
                conn = sqlite3.connect(Path(tmp) / db.name, timeout=1.0)
                try:
                    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        why = DAMAGED
                        continue
                    rows = conn.execute("SELECT name, value, host, path, expiry FROM moz_cookies").fetchall()
                finally:
                    conn.close()
        except sqlite3.Error:
            why = DAMAGED      # a stable copy that SQLite cannot read: the file itself is bad
            time.sleep(0.05 * (attempt + 1))
            continue
        except OSError as e:
            why = f"the cookie file could not be read ({type(e).__name__})"
            time.sleep(0.05 * (attempt + 1))
            continue
        out = []
        for name, value, host, path, expiry in rows:
            if not _host_matches(str(host or ""), domains):
                continue
            exp = float(expiry or 0)
            if exp > 1e11:  # Firefox stores milliseconds since version 135, seconds before
                exp /= 1000.0
            if exp and exp < now:
                continue
            out.append({"name": str(name), "value": str(value or ""), "domain": str(host or ""), "path": path,
                        "expires": exp or -1})
        return out, None
    return None, why


# ---- the checker ----------------------------------------------------------------------------

@dataclass
class _Probe:
    at: float
    ok: bool
    exit_ip: str | None = None
    error: str | None = None
    took_ms: int | None = None
    geo: dict[str, Any] | None = None


class LaneChecker:
    """Runs the checks. `lanes` is the running lane manager (inside agentosd); without it the lanes
    are read read-only from agentos.db and every lane counts as cold. `lanes_lookup` finds the lane
    manager when `lanes` is not set (agentosd's runtime, which may register it after the checker is made).
    `write_cache` False keeps the geo cache in memory only (nothing is written anywhere)."""

    def __init__(self, config: NodeConfig, *, lanes: Any = None, lanes_lookup: Callable[[], Any] | None = None,
                 host: Host | None = None,
                 proxies_loader: Callable[[], dict[str, ProxyConfig]] | None = None,
                 engine: Any = None, clock: Callable[[], float] = time.time, network: bool = True,
                 write_cache: bool = True, proxy_timeout: float = 15.0):
        self.config = config
        self.home = Path(config.home)
        self._lanes = lanes
        self.lanes_lookup = lanes_lookup
        self.host = host or Host()
        self.proxies_loader = proxies_loader or (lambda: load_proxies(config))
        self._engine = engine
        self.clock = clock
        self.network = network
        self.write_cache = write_cache
        self.proxy_timeout = proxy_timeout
        self.geo_path = self.home / "cache" / "exit-geo.json"
        self.last: dict[str, LaneReport] = {}
        self._probes: dict[str, _Probe] = {}
        self._probe_locks: dict[str, asyncio.Lock] = {}
        self._node_ip: tuple[float, str | None] | None = None
        self._installed: tuple[float, dict[str, Any]] | None = None
        self._geo: dict[str, dict[str, Any]] | None = None
        self._geo_lock = threading.Lock()  # probes of different proxies run in parallel threads

    @property
    def lanes(self) -> Any:
        if self._lanes is None and self.lanes_lookup is not None:
            return self.lanes_lookup()
        return self._lanes

    @lanes.setter
    def lanes(self, value: Any) -> None:
        self._lanes = value

    @property
    def engine(self) -> Any:
        return self._engine if self._engine is not None else getattr(self.lanes, "engine", None)

    # ---- sources -------------------------------------------------------------------------

    def _facts_source(self) -> ManagerFacts | StoreFacts:
        lanes = self.lanes
        return ManagerFacts(lanes) if lanes is not None else StoreFacts(self.config.db_path)

    def lane_ids(self) -> list[str]:
        return self._facts_source().lane_ids()

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    # ---- the whole report ------------------------------------------------------------------

    async def check(self, lane_id: str) -> LaneReport:
        source = self._facts_source()
        facts = await asyncio.to_thread(source.facts, lane_id)
        report = await self._check(facts, source)
        self.last[lane_id] = report
        return report

    async def check_all(self, lane_ids: Iterable[str] | None = None) -> list[LaneReport]:
        ids = list(lane_ids) if lane_ids is not None else await asyncio.to_thread(self.lane_ids)
        reports = await asyncio.gather(*(self.check(i) for i in ids), return_exceptions=True)
        out = []
        for lane_id, r in zip(ids, reports, strict=True):
            if isinstance(r, LaneNotFound):
                continue
            if isinstance(r, BaseException):
                # The type only: an exception's text can quote a value it read (a cookie, a path).
                log.warning("%s: the lane check failed: %s", lane_id, type(r).__name__)
                r = LaneReport(lane_id, "unknown", self._now(),
                               [Check("profile", WARN, f"The check itself failed ({type(r).__name__}), so nothing "
                                      "about this lane was checked.",
                                      f"run agentos lane check {lane_id} again; if it keeps failing, report it "
                                      "as a bug", {"crashed": True})])
            out.append(r)
        return out

    async def _check(self, facts: LaneFacts, source: Any) -> LaneReport:
        spec = facts.spec
        now = self._now()
        if spec.kind != LaneKind.BROWSER:
            return LaneReport(spec.id, facts.state, now, [
                Check("profile", UNKNOWN, f"A {spec.kind.value} lane has no browser to check.", None, {}),
                judge_hold(facts, now=now)])
        try:
            proxies = self.proxies_loader()
            proxies_error = None
        except (OSError, ValueError) as e:
            proxies, proxies_error = {}, type(e).__name__
        proxy = proxies.get(spec.proxy_label) if spec.proxy_label else None
        checks: list[Check] = []
        probe: _Probe | None = None
        if spec.proxy_label and proxy is None:
            why = (f"secrets/proxies.json cannot be read ({proxies_error})" if proxies_error
                   else f"there is no proxy {spec.proxy_label} on this node")
            checks.append(Check("proxy", FAIL, f"The lane's proxy is missing: {why}.",
                                f"add it again: agentos proxy add {spec.proxy_label} <host>:<port>:<user>",
                                {"proxy": spec.proxy_label}))
            checks.append(Check("exit_ip", UNKNOWN, "No proxy to check the exit of.", None, {}))
            checks.append(Check("location", UNKNOWN, "No proxy to check the exit of.", None, {}))
        elif proxy is None:
            checks += self._no_proxy_checks(spec, facts)
        else:
            probe = await self._probe(proxy)
            checks.append(self._proxy_check(proxy, probe))
            checks.append(await self._exit_check(proxy, probe, source, spec.id))
        lane_dir = self.config.lane_dir(spec.id)
        profile = lane_dir / "profile"
        pin, pin_error = await asyncio.to_thread(_read_pin, lane_dir / "fingerprint.json")
        if proxy is not None:
            checks.append(self._location_check(proxy, probe, pin, spec, now))
        installed = await self._installed_browser()
        prefs, known = await asyncio.to_thread(self._prefs, facts, profile, proxy, pin, installed)
        missing = spec.proxy_label if spec.proxy_label and proxy is None else None
        checks.append(judge_webrtc(prefs, proxied=proxy is not None, known=known, user_js=profile / "user.js",
                                   missing_proxy=missing))
        checks.append(judge_dns(prefs, proxy=proxy, missing_proxy=missing))
        checks.append(await asyncio.to_thread(self._profile_check, facts, lane_dir, pin, pin_error, installed))
        session, account = await self._session_checks(facts, profile)
        checks += [session, account]
        checks.append(judge_hold(facts, now=now))
        order = {cid: i for i, cid in enumerate(CHECK_IDS)}
        checks.sort(key=lambda c: order.get(c.id, len(order)))
        return LaneReport(spec.id, facts.state, now, checks)

    async def check_device(self, name: str) -> LaneReport:
        """A device's network health (AUD-02, with the AUD-14 self-check): its proxy answers, it
        exits where its proxy's pinned exit says, and the exit is where the device's pin says
        (country, timezone and the locale's region). Reads only; never warms a lane."""
        from agentos.ops.layout import device_specs, lane_specs

        device = (await asyncio.to_thread(device_specs, self.home)).get(name)
        if device is None:
            raise NotFound(f"no device named {name}")
        now = self._now()
        source = self._facts_source()
        pin, _err = await asyncio.to_thread(_read_pin, self.config.device_pin_path(name))
        lanes = sorted(i for i, s in (await asyncio.to_thread(lane_specs, self.home)).items() if s.device == name)
        checks: list[Check] = []
        try:
            proxy = self.proxies_loader().get(device.proxy_label) if device.proxy_label else None
        except (OSError, ValueError):
            proxy = None
        if device.proxy_label is None:
            checks.append(Check("proxy", WARN, "The device has no proxy: its lanes leave from the node's own IP.",
                                "a device for accounts needs a static ISP proxy of its own (docs/PROXIES.md)",
                                {"proxy": None}))
        elif proxy is None:
            checks.append(Check("proxy", FAIL, f"The device's proxy {device.proxy_label} is not on this node.",
                                f"add it again: agentos proxy add {device.proxy_label} <host>:<port>:<user>",
                                {"proxy": device.proxy_label}))
        else:
            probe = await self._probe(proxy)
            checks.append(self._proxy_check(proxy, probe))
            checks.append(await self._exit_check(proxy, probe, source, lanes[0] if lanes else name))
            meta = pin.get(PIN_META) if isinstance(pin, dict) and isinstance(pin.get(PIN_META), dict) else {}
            geo_meta = meta.get("geo") if isinstance(meta.get("geo"), dict) else {}
            language = pin.get("locale:language") if isinstance(pin, dict) else None
            region = pin.get("locale:region") if isinstance(pin, dict) else None
            locale = f"{language}-{region}" if language and region else None
            if pin is None:
                checks.append(Check("location", UNKNOWN, "The device is not pinned yet: its first lane's first "
                                    "launch pins it where the proxy exits.", None, {}))
            else:
                geo = probe.geo
                if geo is not None and geo.get("at"):
                    geo = {**geo, "at": datetime.fromtimestamp(float(geo["at"]), UTC).isoformat(timespec="seconds")}
                checks.append(judge_location(geo, pinned_tz=pin.get("timezone"),
                                             pinned_country=geo_meta.get("country"), locale=locale, at=now,
                                             why_unknown=None if probe.ok else
                                             "Cannot tell where the exit is: the proxy did not answer."))
        return LaneReport(f"device:{name}", "pinned" if pin else "unpinned", now, checks)

    # ---- proxy, exit, location ---------------------------------------------------------------

    def _no_proxy_checks(self, spec: LaneSpec, facts: LaneFacts) -> list[Check]:
        from agentos.node.sites import expectation_for

        account_lane = bool(expectation_for(spec).sites)
        level = WARN if account_lane else OK
        detail = ("No proxy: the account sees this node's own IP." if account_lane
                  else "No proxy: the lane exits from the node's own IP.")
        fix = (f"give it a proxy while it is cold: agentos lane set-proxy {spec.id} <label>"
               if account_lane else None)
        return [Check("proxy", level, detail, fix, {"proxy": None}),
                Check("exit_ip", OK, "No proxy, so the lane exits from the node's own IP"
                      + (f" ({facts.exit_ip} last time)." if facts.exit_ip else "."), None,
                      {"exit_ip": facts.exit_ip}),
                Check("location", UNKNOWN, "No proxy: the location is the node's own.", None, {})]

    async def _probe(self, proxy: ProxyConfig) -> _Probe:
        lock = self._probe_locks.setdefault(proxy.label, asyncio.Lock())
        async with lock:
            cached = self._probes.get(proxy.label)
            if cached is not None and _fresh(cached.at, self.clock(), PROXY_TTL):
                return cached
            if not self.network:
                return _Probe(self.clock(), False, error=OFFLINE)
            probe = await asyncio.to_thread(self._probe_now, proxy)
            self._probes[proxy.label] = probe
            return probe

    def _probe_now(self, proxy: ProxyConfig) -> _Probe:
        start = time.monotonic()
        try:
            self.host.tcp_connect(proxy.host, proxy.port, timeout=min(10.0, self.proxy_timeout))
        except OSError as e:
            return _Probe(self.clock(), False, error=f"cannot connect ({redact(str(e), proxy.username, proxy.password)})")
        with self._geo_lock:
            entry = dict(self._geo_cache().get(proxy.label) or {})
        # At most one lookup a day per proxy, so only a checker that can keep the answer asks at all.
        due = self.write_cache and not _fresh(_number(entry.get("tried")), self.clock(), GEO_TTL)
        info: dict[str, Any] | None = None
        try:
            if due and hasattr(self.host, "proxy_exit_info"):
                entry["tried"] = self.clock()
                try:
                    info = self.host.proxy_exit_info(proxy, timeout=self.proxy_timeout)
                except OSError as e:
                    if "407" in str(e):
                        raise
                    log.info("proxy %s: the geo lookup failed (%s); it is tried again tomorrow", proxy.label,
                             redact(str(e), proxy.username, proxy.password))
                if info is not None:   # the new answer replaces the old one whole: no field of an old exit stays
                    entry = {"tried": entry["tried"], "at": self.clock(),
                             **{k: info[k] for k in ("ip", "country", "region", "city", "timezone") if info.get(k)}}
                with self._geo_lock:
                    self._geo_cache()[proxy.label] = entry
                    self._save_geo(self._geo_cache())
            ip = info["ip"] if info else self.host.proxy_exit_ip(proxy, timeout=self.proxy_timeout)
        except OSError as e:
            return _Probe(self.clock(), False, error=redact(str(e), proxy.username, proxy.password))
        took = int((time.monotonic() - start) * 1000)
        geo = dict(entry) if entry.get("ip") == ip and entry.get("country") else None
        return _Probe(self.clock(), True, exit_ip=ip, took_ms=took, geo=geo)

    def _proxy_check(self, proxy: ProxyConfig, probe: _Probe) -> Check:
        data = {"proxy": proxy.label, "host": proxy.host, "port": proxy.port}
        if not probe.ok:
            if probe.error == OFFLINE:
                return Check("proxy", UNKNOWN, f"Proxy {proxy.label} was not probed (offline).", None, data)
            fix = (f"agentos proxy add {proxy.label} {proxy.host}:{proxy.port}:<user> (asks for the password)"
                   if "407" in (probe.error or "") else
                   f"check that the proxy is up and {proxy.host}:{proxy.port} is right (agentos proxy list)")
            return Check("proxy", FAIL, f"Proxy {proxy.label} does not answer: {probe.error}.", fix, data)
        return Check("proxy", OK, f"Proxy {proxy.label} answers ({probe.took_ms} ms).", None,
                     {**data, "took_ms": probe.took_ms})

    async def _node_ip_now(self) -> str | None:
        """The node's own public IP, or None when it cannot be read (a failure is retried after
        NODE_IP_RETRY, like the lane self-check does)."""
        if self._node_ip is not None:
            at, cached = self._node_ip
            if _fresh(at, self.clock(), NODE_IP_TTL if cached else NODE_IP_RETRY):
                return cached
        ip = None
        if self.network:
            try:
                raw = await asyncio.to_thread(self.host.direct_exit_ip, timeout=10.0)
                ip = str(ipaddress.ip_address(str(raw).strip())) if raw else None
            except (OSError, ValueError):
                ip = None
        self._node_ip = (self.clock(), ip)
        return ip

    async def _exit_check(self, proxy: ProxyConfig, probe: _Probe, source: Any, lane_id: str) -> Check:
        if probe.error == OFFLINE:
            return Check("exit_ip", UNKNOWN, "Where the lane exits was not checked: the proxy was not probed "
                         "(offline).", None, {"proxy": proxy.label})
        if not probe.ok or not probe.exit_ip:
            return Check("exit_ip", UNKNOWN, "Cannot tell where the lane exits: the proxy did not answer.", None,
                         {"proxy": proxy.label})
        host_ips = await asyncio.to_thread(self.host.resolve, proxy.host) | {proxy.host}
        try:
            pinned = await asyncio.to_thread(source.pinned_exit, proxy.label)
        except (sqlite3.Error, OSError) as e:
            return Check("exit_ip", UNKNOWN, f"Exits from {probe.exit_ip}, but the exit pinned for proxy "
                         f"{proxy.label} could not be read ({type(e).__name__}), so it is not known whether the "
                         "exit moved.", "run the check again in a moment",
                         {"exit_ip": probe.exit_ip, "proxy": proxy.label, "host": proxy.host, "pinned": None})
        node_ip = await self._node_ip_now()
        check = judge_exit(proxy, probe.exit_ip, host_ips=host_ips, pinned=pinned, node_ip=node_ip,
                           node_ip_known=node_ip is not None)
        if check.fix:
            check.fix = check.fix.replace("{lane}", lane_id)
        return check

    def _location_check(self, proxy: ProxyConfig, probe: _Probe | None, pin: dict | None, spec: LaneSpec,
                        now: datetime) -> Check:
        meta = pin.get(PIN_META) if isinstance(pin, dict) and isinstance(pin.get(PIN_META), dict) else {}
        geo_meta = meta.get("geo") if isinstance(meta.get("geo"), dict) else {}
        pinned_tz = pin.get("timezone") if isinstance(pin, dict) else None
        why = None
        geo = probe.geo if probe is not None else None
        if probe is not None and probe.error == OFFLINE:
            why = "Where the exit is was not checked: the proxy was not probed (offline)."
        elif probe is None or not probe.ok:
            why = "Cannot tell where the exit is: the proxy did not answer."
        elif geo is None:
            with self._geo_lock:
                entry = dict(self._geo_cache().get(proxy.label) or {})
            if not self.write_cache and not entry:
                why = "Where the exit is has not been looked up yet (agentosd does it, once a day per proxy)."
            elif entry.get("ip") and entry.get("ip") != probe.exit_ip:
                why = (f"The exit changed since today's location lookup ({entry.get('ip')} then, {probe.exit_ip} "
                       "now); it is looked up again within a day.")
            else:
                why = "The exit's location lookup failed today; it is tried again within a day."
        if geo is not None and geo.get("at"):
            geo = {**geo, "at": datetime.fromtimestamp(float(geo["at"]), UTC).isoformat(timespec="seconds")}
        return judge_location(geo, pinned_tz=pinned_tz if isinstance(pinned_tz, str) else None,
                              pinned_country=geo_meta.get("country"), locale=spec.locale, at=now, why_unknown=why)

    def _geo_cache(self) -> dict[str, dict[str, Any]]:
        if self._geo is None:
            try:
                data = json.loads(self.geo_path.read_text())
                self._geo = {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._geo = {}
        return self._geo

    def _save_geo(self, cache: dict[str, dict[str, Any]]) -> None:
        self._geo = cache
        if not self.write_cache:
            return
        try:
            self.geo_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.geo_path.with_name(self.geo_path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(cache, f, indent=1, sort_keys=True)
            os.replace(tmp, self.geo_path)
        except OSError as e:
            log.info("could not save %s: %s", self.geo_path, type(e).__name__)

    # ---- browser, prefs, profile -------------------------------------------------------------

    async def _installed_browser(self) -> dict[str, Any]:
        """The installed Camoufox ({path, verstr} or {error}) and its camoufox.cfg prefs, cached."""
        if self._installed is not None and _fresh(self._installed[0], self.clock(), INSTALLED_TTL):
            return self._installed[1]

        def read() -> dict[str, Any]:
            cache = self.home / ".cache"
            info = dict(self.host.camoufox(cache if (cache / "camoufox").exists() else None))
            path = info.get("path")
            cfg_prefs: dict[str, dict[str, Any]] | None = None
            if path:
                try:
                    cfg_prefs = parse_prefs((Path(path) / "camoufox.cfg").read_text(errors="replace"))
                except OSError:
                    cfg_prefs = None
            info["cfg"] = cfg_prefs
            info["major"] = fpmod.browser_major(info.get("verstr"))
            return info

        info = await asyncio.to_thread(read)
        self._installed = (self.clock(), info)
        return info

    def _launch_prefs(self, facts: LaneFacts, profile: Path, proxy: ProxyConfig | None,
                      pin: dict | None) -> tuple[dict[str, Any], str]:
        """The prefs the browser got (or would get) at launch: CAMOU_PREFS of the running browser, else
        the engine's own prefs for this lane plus the pin's, plus WebRTC on (Camoufox's launch default;
        agentos never passes block_webrtc)."""
        for pid in self.host.browser_pids(profile) if facts.state in ("warm", "active") else []:
            raw = self.host.environ(pid) if hasattr(self.host, "environ") else None
            if raw:
                parsed = _camou_prefs(raw)
                if parsed is not None:
                    return parsed, "running browser"
        prefs: dict[str, Any] = {"media.peerconnection.enabled":
                                 not bool(getattr(self.engine, "extra_options", {}).get("block_webrtc"))}
        # Imported by name (read at each call, so a patched value counts): a renamed pref set fails
        # here loudly instead of reading as no prefs, which would misjudge WebRTC and the cache.
        from agentos.node.engine_camoufox import (
            CACHE_PREFS,
            LINUX_HOST_PREFS,
            STEADY_CHROME_PREFS,
            WEBRTC_PROXY_PREFS,
        )

        for engine_prefs in (CACHE_PREFS, STEADY_CHROME_PREFS, LINUX_HOST_PREFS):
            prefs.update(engine_prefs)
        if proxy is not None:  # what the engine sets on every proxied lane, with or without an engine here
            prefs.update(WEBRTC_PROXY_PREFS)
        try:
            guard = getattr(self.engine, "_network_guard_prefs", None)
            if guard is not None and proxy is not None:  # proxied: no PAC file, so no side effect
                prefs.update(guard(proxied=True, pac_dir=profile.parent) or {})
        except Exception as e:  # noqa: BLE001 - an engine that cannot say its guard prefs only means fewer known prefs
            log.debug("engine prefs not read: %s", type(e).__name__)
        meta = pin.get(PIN_META) if isinstance(pin, dict) and isinstance(pin.get(PIN_META), dict) else {}
        if isinstance(meta.get("prefs"), dict):
            prefs.update(meta["prefs"])
        return prefs, "engine"

    def _prefs(self, facts: LaneFacts, profile: Path, proxy: ProxyConfig | None, pin: dict | None,
               installed: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        def user_values(name: str) -> dict[str, Any]:
            try:
                return parse_prefs((profile / name).read_text(errors="replace")).get("user_pref", {})
            except OSError:
                return {}

        launch, _ = self._launch_prefs(facts, profile, proxy, pin)
        cfg = installed.get("cfg")
        prefs = effective_prefs(cfg=cfg, launch=launch, prefs_js=user_values("prefs.js"),
                                user_js=user_values("user.js"))
        return prefs, cfg is not None

    def _profile_check(self, facts: LaneFacts, lane_dir: Path, pin: dict | None, pin_error: str | None,
                       installed: dict[str, Any]) -> Check:
        lane_id = facts.spec.id
        profile = lane_dir / "profile"
        fp = lane_dir / "fingerprint.json"
        data: dict[str, Any] = {"profile": str(profile)}
        problems: list[tuple[str, str, str | None]] = []   # (level, sentence, fix)
        if not profile.is_dir():
            # The path stays in `data`; the sentence a person reads names no path (the console shows it).
            return Check("profile", FAIL, "The profile folder is missing, so the lane has no logins.",
                         "Restore its profile folder from a backup, or set the account up again.", data)
        if not os.access(profile, os.R_OK | os.X_OK):
            return Check("profile", FAIL, "The profile folder cannot be read by agentos.",
                         f"give it back to the service user: chown -R <user> {lane_dir}", data)
        used = any((profile / n).exists() for n in ("prefs.js", "cookies.sqlite", "times.json"))
        version = None
        if pin_error:
            problems.append((FAIL, f"The pin file cannot be read ({pin_error}).",
                             f"restore {fp} from a backup; deleting it makes sites see a new device"))
        elif pin is None:
            if facts.state == "warming":   # the engine writes the pin once the browser has started (settle_pin)
                data["pin_pending"] = True
            elif used:
                problems.append((FAIL, ("The profile was used, but its pin file is gone, so the next launch "
                                 "would show sites a new device."), f"restore {fp} from a backup"))
        else:
            meta = pin.get(PIN_META)
            version = meta.get("version") if isinstance(meta, dict) and isinstance(meta.get("version"), int) else 1
            data["pin_version"] = version
            data["pin_v1_backup"] = (lane_dir / "fingerprint.json.pin-v1.bak").exists()
            if version < PIN_VERSION:
                problems.append((WARN, (f"The pin is version {version}; it is completed on the next launch "
                                 "(the old file is kept as fingerprint.json.pin-v1.bak)."), None))
        pinned = fpmod.pinned_major(pin) if isinstance(pin, dict) else None
        engine = installed.get("major")
        data.update(pinned_firefox=pinned, engine_firefox=engine)
        verdict = fpmod.compare(engine, pinned)
        if verdict == "older":
            problems.append((WARN, f"The pin says Firefox {pinned}, the browser is Firefox {engine}.",
                             f"agentos lane refresh-fingerprint {lane_id} --keep-device"))
        elif verdict == "newer":
            problems.append((FAIL, (f"The pin says Firefox {pinned}, newer than the installed browser "
                             f"(Firefox {engine})."), "update Camoufox (agentos doctor shows the command)"))
        pids = self.host.browser_pids(profile)
        warm = facts.state in ("warm", "active", "warming")
        data["browser_running"] = bool(pids)
        if pids and not warm and self.lanes is not None:
            problems.append((FAIL, "A browser nobody owns has this profile open (left by a crash).",
                             "restart agentosd, which closes it (agentos node restart)"))
        elif pids and self.lanes is None:
            problems.append((WARN, "A browser has this profile open while agentosd is not running.",
                             "start agentosd (it closes browsers it does not own), or close that browser"))
        lock = profile / "lock"
        if not pids and left_by_a_crash(profile):
            data["stale_lock"] = True
            problems.append((WARN, "A lock left by a crashed browser is still in the profile.",
                             f"Firefox clears it on the next launch; if the lane will not open, remove {lock}"))
        size = _tree_size(lane_dir)
        usage = self.host.disk_usage(profile)
        free = usage[2] if usage else None
        data.update(size_bytes=size, disk_free_bytes=free)
        if free is not None and free < DISK_FAIL_BYTES:
            problems.append((FAIL, (f"Only {_size(free)} free on the disk, so the lane will not open and a "
                             "cookie write could log the account out."),
                             "free disk space: agentos backup prune --keep 3, old lanes/*/*/shots"))
        elif free is not None and free < DISK_WARN_BYTES:
            problems.append((WARN, f"Only {_size(free)} free on the disk.", "free disk space soon"))
        if size > PROFILE_WARN_BYTES:
            problems.append((WARN, f"The lane uses {_size(size)} of disk.", f"look for large files under {lane_dir}"))
        if not problems:
            pin_part = (f"pin version {version}, Firefox {pinned}" if version else
                        "the pin is written once the browser has started" if data.get("pin_pending") else
                        "not pinned yet (pinned on first launch)")
            return Check("profile", OK, f"Profile intact: {pin_part}, {_size(size)} used"
                         + (f", {_size(free)} free." if free is not None else "."), None, data)
        level = worst(p[0] for p in problems)
        chosen = [p for p in problems if p[0] == level]
        fixes = [p[2] for p in chosen if p[2]]
        return Check("profile", level, " ".join(p[1] for p in chosen), "; ".join(fixes) or None, data)

    # ---- session and account -----------------------------------------------------------------

    async def _session_checks(self, facts: LaneFacts, profile: Path) -> tuple[Check, Check]:
        from agentos.node import sites

        expect = sites.expectation_for(facts.spec)
        wanted = [s for s in (sites.site(k) for k in sorted(expect.sites)) if s is not None]
        if not wanted:
            note = ("The lane names no site (its account or name), so there is no session to look for."
                    if not expect.sites else f"agentos knows no rules for {', '.join(sorted(expect.sites))}.")
            return (Check("session", UNKNOWN, note, None, {"sites": sorted(expect.sites)}),
                    Check("account", UNKNOWN, "No expected account for this lane.", None, {}))
        domains = sorted({h for s in wanted for h in s.hosts})
        cookies, source, why = await self._cookies(facts, profile, wanted, domains)
        if cookies is None:
            return (Check("session", UNKNOWN, f"The session cookies could not be read ({why}).", None,
                          {"source": source}),
                    Check("account", UNKNOWN, "The session cookies could not be read.", None, {}))
        session_data: dict[str, Any] = {"source": source, "sites": {}}
        signed_out = []
        for s in wanted:
            mine = [c for c in cookies if _host_matches(str(c.get("domain", "")), s.hosts)]
            missing = s.missing_session(mine)
            # The site table's names only (`wordpress_logged_in_*`): a site's other cookie names can carry
            # the account id (`wp-settings-<id>`), and the report never holds one.
            session_data["sites"][s.key] = {"signed_in": missing is None, "missing_cookie": missing,
                                            "cookies_seen": [c.name for c in s.session if c.find(mine) is not None],
                                            "expires_at": session_expiry(s, mine)}
            if missing is not None:
                signed_out.append((s, missing))
        if signed_out:
            names = ", ".join(f"{s.name} (no {m} cookie)" for s, m in signed_out)
            session = Check("session", FAIL, f"Signed out of {names}.",
                            f"a human logs in again through the live view (agentos lane view {facts.spec.id} "
                            "--control)", session_data)
        else:
            session = Check("session", OK, f"Signed in to {', '.join(s.name for s in wanted)} (session cookie "
                            "present).", None, session_data)
        return session, self._account_check(expect, wanted, cookies, signed_out, facts.spec.id)

    async def _cookies(self, facts: LaneFacts, profile: Path, wanted: list[Any],
                       domains: list[str]) -> tuple[list[dict[str, Any]] | None, str, str]:
        if facts.context is not None:
            urls = sorted({f"https://{h}/" for h in domains} | {s.home for s in wanted if s.home})
            try:
                return list(await asyncio.wait_for(facts.context.cookies(urls), COOKIE_TIMEOUT)), "live", ""
            except Exception as e:  # noqa: BLE001 - fall back to the file
                log.info("%s: live cookies not read (%s); reading the file", facts.spec.id, type(e).__name__)
        scratch = self.home / "run"
        found, why = await asyncio.to_thread(read_cookies, profile, domains, now=self.clock(),
                                             scratch=scratch if scratch.is_dir() else None)
        return found, "file", why or BUSY

    def _account_check(self, expect: Any, wanted: list[Any], cookies: list[dict[str, Any]],
                       signed_out: list[Any], lane_id: str) -> Check:
        from agentos.node import sites

        pinned = expect.account
        if pinned is None:
            return Check("account", UNKNOWN, "The lane has no expected account set, so which account is signed "
                         "in is not checked.", "set the lane's expected account (site, handle and user id)",
                         {"pinned": False})
        s = sites.site(pinned.site)
        handle = pinned.handle or pinned.site
        if s is None:
            return Check("account", UNKNOWN, f"agentos knows no rules for {pinned.site}.", None, {"pinned": True})
        if any(o[0].key == s.key for o in signed_out):
            return Check("account", UNKNOWN, f"Not signed in to {s.name}, so the account cannot be checked.", None,
                         {"pinned": True, "site": s.key})
        if s.identity is None:
            return Check("account", UNKNOWN, f"{s.name} keeps no account id in its cookies, so the account is not "
                         "checked.", None, {"pinned": True, "site": s.key, "visible": False})
        mine = [c for c in cookies if _host_matches(str(c.get("domain", "")), s.hosts)]
        try:
            identity = sites.identity_from_cookies(s, mine)
        except Exception as e:  # noqa: BLE001 - an odd cookie value; its text may quote the value, so the type only
            log.info("%s: the %s account id was not read (%s)", lane_id, s.key, type(e).__name__)
            return Check("account", UNKNOWN, f"{s.name}'s account cookie has a form agentos cannot read, so the "
                         "account is not checked.", None, {"pinned": True, "site": s.key, "visible": False})
        result = sites.check_account(expect, identity)
        data = {"pinned": True, "site": s.key, "handle": pinned.handle, "visible": identity is not None,
                "status": result.status if result else None}
        if result is None:
            return Check("account", UNKNOWN, f"{s.name}'s cookies show no account id right now.", None, data)
        if result.status == "match":
            return Check("account", OK, f"Signed in as the expected {s.name} account ({handle}).", None, data)
        if result.status == sites.WRONG_ACCOUNT:
            return Check("account", FAIL, f"Signed in to a different {s.name} account than the expected {handle}.",
                         f"a human signs out and logs in as {handle} (agentos lane view {lane_id} --control)", data)
        return Check("account", WARN, f"The expected {s.name} account ({handle}) has no user id pinned, so a "
                     "wrong account would not be noticed.", "pin the account's user id in the lane's account",
                     data)


# ---- helpers ------------------------------------------------------------------------------------

def session_expiry(s: Any, cookies: Iterable[Mapping[str, Any]]) -> str | None:
    """When the first of a site's session cookies expires (ISO, UTC), or None when none is there or
    they all live for the browser session only. A time, never a value (agentos.keepalive plans by it)."""
    cookies = list(cookies)
    ends = []
    for c in s.session:
        found = c.find(cookies)
        exp = _number(found.get("expires")) if found is not None else 0.0
        if exp > 0:
            ends.append(exp)
    return datetime.fromtimestamp(min(ends), UTC).isoformat(timespec="seconds") if ends else None


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _read_pin(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        return None, type(e).__name__
    if not isinstance(data, dict) or not {k for k in data if k != PIN_META}:
        return None, "no browser settings in it"
    return data, None


def _camou_prefs(environ: bytes) -> dict[str, Any] | None:
    """The CAMOU_PREFS_<n> chunks of a process environment, joined and parsed (nothing else is read)."""
    chunks = sorted((kv for kv in environ.decode(errors="replace").split("\0") if kv.startswith("CAMOU_PREFS_")),
                    key=lambda kv: int(kv.split("=", 1)[0].rsplit("_", 1)[1] or 0))
    if not chunks:
        return None
    try:
        data = json.loads("".join(kv.split("=", 1)[1] for kv in chunks))
    except (ValueError, IndexError):
        return None
    return data if isinstance(data, dict) else None


def left_by_a_crash(profile: Path) -> bool:
    """Firefox leaves its `lock` link on a clean exit too, so that alone says nothing; the session
    recovery file is removed by a clean exit and only a crash leaves it."""
    recovery = profile / "sessionstore-backups" / "recovery.jsonlz4"
    return (profile / "lock").is_symlink() and recovery.exists()


def _tree_size(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def _size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit in ("B", "KiB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def format_report(report: LaneReport, *, color: bool = False) -> str:
    """A report as short lines a phone can show."""
    colors = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", UNKNOWN: "\033[2m"}

    def label(level: str) -> str:
        text = level.upper().ljust(7)
        return f"{colors[level]}{text}\033[0m" if color else text

    lines = [(f"{report.lane}: {label(report.level).strip()} ({"device" if report.lane.startswith("device:") else "lane"} {report.state}, checked "
             f"{report.at.strftime('%H:%M UTC')})")]
    for c in report.checks:
        lines.append(f"  {label(c.level)} {c.title}: {c.detail}")
        if c.fix and c.level in (WARN, FAIL):
            lines.append(f"          fix: {c.fix}")
    return "\n".join(lines)
