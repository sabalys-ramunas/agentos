"""The lane check's pure rules: given what was measured, judge the exit IP, the location, WebRTC,
DNS and holds. No I/O here, so each rule is tested on its own. Also how Firefox prefs files are
read (strip_comments, parse_prefs) and layered (effective_prefs).
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentos.core.models import ControlState, LaneControl, ProxyConfig, Signal
from agentos.core.signals import STOP
from agentos.stealth.lanefacts import LaneFacts
from agentos.stealth.lanereport import FAIL, OK, UNKNOWN, WARN, Check, as_datetime, stamp

BAD_SIGNALS = STOP


WEBRTC_PREFS = ("media.peerconnection.enabled", "media.peerconnection.ice.proxy_only",
                "media.peerconnection.ice.proxy_only_if_behind_proxy",
                "media.peerconnection.ice.obfuscate_host_addresses")


DNS_PREFS = ("network.proxy.socks_remote_dns", "network.dns.disablePrefetch", "network.trr.mode")


PROXY_ONLY_FIX = ('add user_pref("media.peerconnection.ice.proxy_only", true); to {path} while the lane is '
                  "cold, so WebRTC never sends UDP around the proxy")


_PREF_RE = re.compile(r'^\s*(defaultPref|pref|lockPref|user_pref)\(\s*"([^"]+)"\s*,\s*(.+?)\s*\)\s*;', re.MULTILINE)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def judge_exit(proxy: ProxyConfig, ip: str, *, host_ips: set[str], pinned: str | None,
               node_ip: str | None, node_ip_known: bool | None = None) -> Check:
    """The lane self-check's rule (agentos.node.lanes.quarantine._judge_exit) without its side effects: an IP
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
        since = stamp(q.get("at"))
        return Check("hold", FAIL, f"Quarantined{' since ' + since if since else ''}: {q.get('reason')}.",
                     f"a human fixes the cause, then hands the lane back ({handback}, or the panel)",
                     {"kind": "quarantine", "since": since, "by": q.get("by"), "reason": q.get("reason"),
                      "quarantine_kind": q.get("kind"), "cleared_by": "human"})
    control = facts.control or LaneControl()
    since = stamp(control.since)
    if control.state == ControlState.PAUSED and control.reason:
        until = as_datetime(control.hold_until)
        data = {"kind": "hold", "since": since, "by": control.by, "reason": control.reason,
                "until": stamp(until)}
        if until is not None and until > now:
            return Check("hold", WARN, f"Held until {stamp(until)}: {control.reason}. It clears itself.",
                         f"wait until {stamp(until)}, or a human hands it back sooner ({handback})",
                         {**data, "cleared_by": "time"})
        if until is not None:
            return Check("hold", WARN, f"A timed hold ended at {stamp(until)} ({control.reason}); agentosd lifts "
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
            at = stamp(sig.get("at"))
            where = f" on {sig['detail']}" if sig.get("detail") else ""
            return Check("hold", WARN, f"The last page showed {value.value}{where}{' at ' + at if at else ''}, "
                         "and nothing holds the lane.", f"a human looks at the lane before agents go on "
                         f"(agentos lane view {lane})", {"kind": "signal", "signal": value.value, "since": at})
    return Check("hold", OK, "No hold, no quarantine; agents have control.", None, {"kind": None})
