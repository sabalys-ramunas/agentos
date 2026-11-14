"""The exit IP self-check and quarantine of the lane manager (agentos.node.lanes, "Exit IP" and
"Holds" there; AUD-14).

The self-check reads the exit IP inside the lane's own browser when it warms (and on demand,
probe_exit_ip). A lane without a proxy must leave from the node's own IP, a proxied lane never may,
a proxy whose host is an IP address must exit from that address, and a gateway proxy's first exit is
pinned. A mismatch quarantines the lane: a persisted record plus a hold, so nothing runs there until
a human hands it back. The node's own IP comes from direct_exit_ip_probe, read straight from the
daemon. A test patches `_proxy_ips` and `SELF_CHECK_JS` here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import urllib.request

from agentos.core.errors import InvalidRequest, LaneQuarantined
from agentos.core.models import ControlState, LaneControl, LaneInfo, LaneState, ProxyConfig
from agentos.node.browser_output import browser_error
from agentos.node.interfaces import BrowserSession
from agentos.node.lanes.base import (
    IDLE_HANDBACK_BY,
    NODE_BY,
    QUARANTINE_HOLD,
    NodeIpProbe,
    _Lane,
    _who,
    clean_reason,
)
from agentos.node.store import LaneRecord, QuarantineRecord

log = logging.getLogger("agentos.lanes")

SELF_CHECK_URL = "https://ipinfo.io/ip"
SELF_CHECK_JS = """
async (url) => {
  const r = await fetch(url, {cache: 'no-store', credentials: 'omit'});
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return (await r.text()).trim();
}
"""
QUARANTINE_USE_WAIT = 10.0         # quarantine() lets a tool call in flight finish its step
PROBE_USE_WAIT = 2.0               # probe_exit_ip() skips a lane busy for longer than this
NODE_IP_TTL = 600.0                # the node's own IP is read again after this long
NODE_IP_RETRY = 60.0               # ... or after this long when it could not be read


def direct_exit_ip_probe(url: str = SELF_CHECK_URL, *, timeout: float = 10.0) -> NodeIpProbe:
    """Reads the node's own public IP straight from the daemon (no proxy, not even one set in
    the environment), for the self-check of lanes with and without a proxy."""

    def fetch() -> str:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(url, headers={"User-Agent": "agentos-self-check"})
        with opener.open(request, timeout=timeout) as response:
            return response.read(64).decode("ascii", "replace").strip()

    async def probe() -> str | None:
        return await asyncio.to_thread(fetch)

    return probe


class QuarantineMixin:
    """The self-check and quarantine of the lane manager (agentos.node.lanes.LaneManagerImpl)."""

    async def _self_check(self, lane_id: str, lane: _Lane,
                          proxy: ProxyConfig | None) -> tuple[str, QuarantineRecord | None] | None:
        """Read the exit IP from inside the lane. Returns (reason, record) to quarantine (a
        record makes it persist until a human clears it), or None."""
        assert lane.session is not None
        try:
            ip = await asyncio.wait_for(self._fetch_exit_ip(lane.session), self.self_check_timeout)
            ip = str(ipaddress.ip_address(str(ip).strip()))
        except Exception as e:  # noqa: BLE001
            if proxy:
                why = browser_error(e, lane_id)
                return f"self-check could not read the exit IP through proxy {proxy.label}: {why}", None
            log.warning("%s: self-check could not read the exit IP (no proxy, so not fatal): %s", lane_id,
                        browser_error(e, lane_id))
            return None
        lane.exit_ip = ip
        self.store.soon(self.store.set_exit_ip, lane_id, ip)  # informative: the last exit seen
        return await self._judge_exit(lane_id, proxy, ip)

    async def _judge_exit(self, lane_id: str, proxy: ProxyConfig | None,
                          ip: str) -> tuple[str, QuarantineRecord | None] | None:
        node_ip = await self._read_node_ip()
        if node_ip and not _same_family(ip, node_ip):
            # The daemon and the browser read over different IP versions: nothing to compare.
            log.info("%s: exit IP %s and the node's IP %s are different IP versions", lane_id, ip, node_ip)
            node_ip_known = False
        else:
            node_ip_known = bool(node_ip)
        if proxy is None:
            if node_ip_known and ip != node_ip:
                reason = (f"exit IP {ip} is not this node's own IP {node_ip}, but the lane has no proxy. Check "
                          "the profile's own proxy settings, then open it again")
                return reason, None
            log.info("%s: exit IP %s (no proxy)", lane_id, ip)
            return None
        label = proxy.label
        if not proxy.check_exit:
            log.info("%s: exit IP %s through %s (an environment's tunnel: not compared)", lane_id, ip, label)
            return None
        if node_ip and ip == node_ip:
            reason = f"proxy {label} is not in use: the lane left from the node's own IP {ip}"
            return reason, self._record(reason, "exit_ip_is_node", label, ip)
        hosts = await _proxy_ips(proxy.host)
        pin = self.store.proxy_exit(label)
        if _is_ip(proxy.host):
            # A static proxy exits from its own address, always: no pin can make another one valid.
            if ip in hosts:
                if pin is None or pin.exit_ip != ip:
                    await self.store.run(self.store.pin_proxy_exit, label, ip, when=self.now())  # durable
                else:
                    self.store.soon(self.store.touch_proxy_exit, label, self.now())
                log.info("%s: exit IP %s matches proxy %s", lane_id, ip, label)
                return None
            if pin is not None:  # it worked before, so something changed: a human looks
                reason = (f"proxy {label} now exits from {ip}, not from its own address {proxy.host} as before. A "
                          f"human checks the proxy; the lane opens again once it exits from {proxy.host}")
                return reason, self._record(reason, "exit_ip_mismatch", None, ip)
            return f"exit IP {ip} does not match proxy {label} ({proxy.host})", None
        # A gateway (a host name) may exit anywhere: pin the first exit, then hold to it.
        if pin is not None:
            if pin.exit_ip == ip:
                self.store.soon(self.store.touch_proxy_exit, label, self.now())
                log.info("%s: exit IP %s matches the pinned exit of proxy %s", lane_id, ip, label)
                return None
            reason = (f"proxy {label} used to exit from {pin.exit_ip} and now exits from {ip}, so the account "
                      "would be seen from a new address. A human checks the proxy and hands the lane back to "
                      "accept the new address")
            return reason, self._record(reason, "exit_ip_changed", label, ip)
        if not node_ip_known and self.node_ip_probe is not None:
            # never pin an exit that might be the node's own address
            reason = (f"could not read this node's own IP, so the first exit {ip} of gateway proxy {label} "
                      "cannot be checked against it; open the lane again shortly")
            return reason, None
        held = await self.store.run(self.store.pin_proxy_exit_if_absent, label, ip, by="self-check (gateway)",
                                    when=self.now())
        if held.exit_ip != ip:  # another lane on this gateway pinned its first exit meanwhile
            reason = (f"proxy {label} is pinned to exit from {held.exit_ip} and now exits from {ip}, so the "
                      "account would be seen from a new address. A human checks the proxy and hands the lane "
                      "back to accept the new address")
            return reason, self._record(reason, "exit_ip_changed", label, ip)
        self._event(lane_id, "exit_ip_pinned", {"proxy": label, "exit_ip": ip, "host": proxy.host})
        log.info("%s: proxy %s is a gateway (%s); its first exit IP %s is pinned", lane_id, label, proxy.host, ip)
        return None

    def _record(self, reason: str, kind: str, label: str | None, ip: str | None) -> QuarantineRecord:
        return QuarantineRecord(reason=reason, at=self.now(), by=NODE_BY, kind=kind, proxy_label=label, exit_ip=ip)

    async def _read_node_ip(self) -> str | None:
        """The node's own public IP from node_ip_probe (cached), or None when unknown."""
        if self.node_ip_probe is None:
            return None
        now = self.clock()
        if self._node_ip_at is not None and now - self._node_ip_at < (NODE_IP_TTL if self._node_ip else NODE_IP_RETRY):
            return self._node_ip
        try:
            raw = await asyncio.wait_for(self.node_ip_probe(), self.self_check_timeout)
            ip = str(ipaddress.ip_address(str(raw).strip())) if raw else None
        except Exception as e:  # noqa: BLE001
            log.info("could not read the node's own IP: %s", str(e).strip() or type(e).__name__)
            ip = None
        self._node_ip, self._node_ip_at = ip, now
        return ip

    async def _fetch_exit_ip(self, session: BrowserSession) -> str:
        page = await session.page()
        if getattr(page, "url", "about:blank") in ("about:blank", ""):
            return await page.evaluate(SELF_CHECK_JS, self.self_check_url)
        # Never fetch from a site's origin (its CSP may block it): use a blank scratch tab.
        probe = await page.context.new_page()
        try:
            return await probe.evaluate(SELF_CHECK_JS, self.self_check_url)
        finally:
            await probe.close()

    async def probe_exit_ip(self, lane_id: str) -> str | None:
        """The self-check on demand, for a warm lane: reads its exit IP in its own browser and
        quarantines it on the same rules as at open. Returns the IP, or None when the lane is
        not warm, is busy (a tool call or a flow run), has a live view open, the IP could not
        be read, or the proxy it should leave through cannot be told (so nothing was judged).

        With a live view open it does not run: the read needs a scratch page, which Camoufox
        opens as a second window over the whole screen until the fetch through the proxy is
        done, so the person watching sees the screen go blank (and loses keyboard focus mid
        sign-in). The health monitor checks the proxy's exit from the daemon instead then."""
        rec = self.store.require(lane_id)
        lane = self._lanes.get(lane_id)
        if lane is None or lane.state not in (LaneState.WARM, LaneState.ACTIVE) or lane.session is None:
            return None
        if lane.watchers:
            log.debug("%s: exit IP probe skipped: a live view is open", lane_id)
            return None
        used = await self._enter_system(lane_id, lane, PROBE_USE_WAIT)
        if used is None:
            return None
        try:
            async with lane.op:
                session = lane.session
                if session is None or lane.state != LaneState.WARM:
                    return None
                try:
                    raw = await asyncio.wait_for(self._fetch_exit_ip(session), self.self_check_timeout)
                    ip = str(ipaddress.ip_address(str(raw).strip()))
                except Exception as e:  # noqa: BLE001 - a dead proxy fails requests; it cannot leak
                    log.info("%s: exit IP probe failed: %s", lane_id, browser_error(e, lane_id))
                    return None
                lane.exit_ip = ip
                self.store.soon(self.store.set_exit_ip, lane_id, ip)  # informative: the last exit seen
                try:
                    proxy = self._proxy_for(rec.spec)
                except InvalidRequest as e:
                    # Which proxy it should leave through cannot be told (not configured here, or not its
                    # device's), so the exit was not judged: never vouch for it (the health monitor's
                    # judges_exit_ip takes a returned IP as accepted, and checks the proxy itself on None).
                    log.warning("%s: exit IP %s was not judged: %s", lane_id, ip, e.message)
                    return None
                verdict = await self._judge_exit(lane_id, proxy, ip)
                if verdict:
                    reason, record = verdict
                    log.warning("%s: quarantined by the exit IP probe: %s", lane_id, reason)
                    try:
                        if record is not None:  # persisted first: a cancelled close cannot lose it
                            await self._quarantine_record(lane_id, record)
                    finally:  # and closed even when it could not be saved (it is refused in memory then)
                        await self._teardown(lane_id, lane, LaneState.QUARANTINED)
                        lane.quarantine_reason = reason
                return ip
        finally:
            self._leave(lane_id, lane, used)
            await self._notify()

    # ---- quarantine -------------------------------------------------------------

    def _refuse_quarantined(self, rec: LaneRecord) -> None:
        lane_id = rec.spec.id
        quarantine = rec.quarantine or self._quarantining.get(lane_id) or self._unsaved_quarantines.get(lane_id)
        if quarantine is not None:
            raise LaneQuarantined(
                f"lane {rec.spec.id} is quarantined ({quarantine.reason}). Nothing runs there until a human "
                f"fixes the cause and hands it back (agentos lane handback {rec.spec.id}); tell your user.")

    async def quarantine(self, lane_id: str, reason: str, *, by: str = NODE_BY, kind: str = "manual",
                         proxy_label: str | None = None, exit_ip: str | None = None) -> LaneInfo:
        """QUARANTINED until a human hands the lane back: agents are held off at once, a tool
        call in flight gets QUARANTINE_USE_WAIT to finish its step, then the browser closes.
        Persisted, so it survives a restart."""
        self.store.require(lane_id)
        text = clean_reason(reason) or "quarantined by the node"
        record = QuarantineRecord(reason=text, at=self.now(), by=_who(by), kind=kind, proxy_label=proxy_label,
                                  exit_ip=exit_ip)
        try:
            await self._quarantine_record(lane_id, record)
        except BaseException:
            # Not saved, or cancelled: it holds in memory (refused and held), and the browser still closes.
            await self._close_quarantined(lane_id, text)
            raise
        await self._close_quarantined(lane_id, text)
        log.warning("%s: quarantined (%s): %s", lane_id, _who(by), text)
        return self.get(lane_id)

    async def _close_quarantined(self, lane_id: str, text: str) -> None:
        lane = self._lanes.get(lane_id)
        if lane is not None:
            used = await self._enter_system(lane_id, lane, QUARANTINE_USE_WAIT)
            if used is None:
                log.warning("%s: a tool call is still using it; quarantining it anyway", lane_id)
            try:
                async with lane.op:  # an open in progress finishes (and sees the quarantine) first
                    if lane.state != LaneState.COLD or lane.session is not None or lane.viewer is not None:
                        await self._teardown(lane_id, lane, LaneState.QUARANTINED)
                    lane.quarantine_reason = text
            finally:
                if used is not None:
                    self._leave(lane_id, lane, used)
            await self._notify()

    async def _quarantine_record(self, lane_id: str, record: QuarantineRecord) -> None:
        """Persist a quarantine and hold agents off the lane (the gate closes before anything else).
        The write is queued first, ahead of the event and the hold's own write, and is done
        before this returns; the hold closes the gate in memory without waiting for it. A write
        that fails raises here, and the lane stays refused in memory (_unsaved_quarantines) until a
        human clears it: a quarantine never lifts itself because it could not be saved."""
        self._quarantining[lane_id] = record  # refused at once, by agents and humans, until it lands
        saved = self.store.submit(self.store.set_quarantine, lane_id, record)

        def landed(done: asyncio.Future) -> None:
            if self._quarantining.get(lane_id) is record:
                del self._quarantining[lane_id]
            if not done.cancelled() and done.exception() is not None:
                self._unsaved_quarantines[lane_id] = record

        saved.add_done_callback(landed)
        try:
            self._event(lane_id, "quarantined", record.model_dump(mode="json"))
            await self.hold(lane_id, reason=f"{QUARANTINE_HOLD} {record.reason}", by=record.by)
        finally:
            await saved

    async def clear_quarantine(self, lane_id: str, *, by: str) -> LaneInfo:
        """A human clears a quarantine, and the quarantine's own hold with it (hand_back does
        this too, and hands control back). Another hold, such as needs_human:checkpoint, stays
        until a human hands the lane back. Clearing an exit_ip_changed quarantine accepts the
        new exit IP for its proxy label."""
        return await self._clear_quarantine(lane_id, by=by, release_hold=True)

    async def _clear_quarantine(self, lane_id: str, *, by: str, release_hold: bool) -> LaneInfo:
        await self.store.flush()  # a quarantine still waiting for a lock lands first, then is cleared
        rec = self.store.require(lane_id)
        unsaved = self._unsaved_quarantines.pop(lane_id, None)  # one whose write failed: a human clears it here
        if unsaved is not None:
            self._event(lane_id, "quarantine_cleared", {"by": _who(by), "reason": unsaved.reason,
                                                        "kind": unsaved.kind, "saved": False})
        lane = self._lanes.get(lane_id)
        if lane is not None and lane.state == LaneState.QUARANTINED:
            lane.state, lane.quarantine_reason = LaneState.COLD, None
        q = rec.quarantine
        if q is None:
            if release_hold:  # a hold whose record never landed (its write failed) goes too
                await self._release_quarantine_hold(lane_id, None, by=by)
            return self.get(lane_id)
        await self.store.run(self.store.set_quarantine, lane_id, None)
        # only for the proxy the lane still uses: after a move, the old proxy's new exit stays unaccepted
        if q.kind == "exit_ip_changed" and q.proxy_label and q.exit_ip and q.proxy_label == rec.spec.proxy_label:
            await self.store.run(self.store.pin_proxy_exit, q.proxy_label, q.exit_ip, by=f"accepted by {_who(by)}",
                                 when=self.now())
        self._event(lane_id, "quarantine_cleared", {"by": _who(by), "reason": q.reason, "kind": q.kind})
        log.info("%s: quarantine cleared by %s", lane_id, _who(by))
        if release_hold:
            await self._release_quarantine_hold(lane_id, q, by=by)
        return self.get(lane_id)

    async def _release_quarantine_hold(self, lane_id: str, record: QuarantineRecord | None, *, by: str) -> None:
        """Lift the hold a quarantine put on the lane (its reason starts with QUARANTINE_HOLD).
        Any other hold (a challenge, an egress block) stays until a human hands the lane back, and
        so does a person's own stop the quarantine landed on (it stays PAUSED, without the reason)."""
        old = self._control(lane_id)
        if not (old.reason or "").startswith(QUARANTINE_HOLD):
            return
        by = _who(by)
        quarantined_by = {NODE_BY, IDLE_HANDBACK_BY} | ({_who(record.by)} if record is not None else set())
        if old.state == ControlState.PAUSED and old.by in quarantined_by:
            new = LaneControl(state=ControlState.AGENT, by=by, since=self.now(), last_human_input=old.last_human_input)
            self._denied_at.pop(lane_id, None)
        else:
            new = old.model_copy(update={"reason": None, "hold_until": None})
        self._event(lane_id, "hold_cleared", {"by": by, "reason": old.reason})
        await self._apply(lane_id, new, why=f"quarantine cleared by {by}")


def _same_family(a: str, b: str) -> bool:
    try:
        return ipaddress.ip_address(a).version == ipaddress.ip_address(b).version
    except ValueError:
        return False


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def _proxy_ips(host: str) -> set[str]:
    try:
        return {str(ipaddress.ip_address(host))}
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return {host}
    return {str(ipaddress.ip_address(info[4][0])) for info in infos} | {host}
