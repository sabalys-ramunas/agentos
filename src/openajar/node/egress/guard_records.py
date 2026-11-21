"""The egress guard's records: blocks, notices, trace rows, rule hits, uploads and observed requests."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from openajar.core import globs
from openajar.node import publish
from openajar.node.egress import constants as _constants
from openajar.node.egress.constants import (
    ASK_FIRST_TELL_S,
    DOCUMENT_KINDS,
    HIT_EVERY_S,
    LOG_EVERY_S,
    NOTICE_BURST,
    NOTICE_EVERY_S,
    OBSERVE_EVERY_S,
    TRACE_EVERY_S,
    UNRULED_EVERY_S,
    UPLOAD_HASH_MAX,
)
from openajar.node.egress.helpers import _path, _post_data, _short
from openajar.node.egress.rules import _REGISTER_RULE, EffectRule
from openajar.node.egress.shapes import request_shape
from openajar.node.egress.sites import allow_entry, host_of, in_sites
from openajar.node.egress.suggested import IDENTITY_SITES
from openajar.node.egress.verdicts import Block, Refusal

log = logging.getLogger("openajar.node.egress")


class GuardRecords:
    """LaneGuard's records, a mixin: what a block, a notice, an upload or an observed request leaves."""

    # ---- records -------------------------------------------------------------------------

    def _note_human(self, url: str, method: str, kind: str, *, frame: bool = False) -> None:
        """A document a human loaded from a host off the lane's list: let through, and noted in the
        lane's egress trace (rate-limited) so the owner can add the host for agents later."""
        host = host_of(url)
        if not host or in_sites(self.sites, host, frame=frame):
            return
        if kind in DOCUMENT_KINDS and self.on_notice is not None:  # the live view says it and offers Allow
            self._seq += 1
            self._notice(Block(seq=self._seq, host=host, rule="noted", kind=kind, method=method.upper(),
                               path=_path(url), prevented=False, frame=frame,
                               reason=f"{host} is not on this browser's site list: you loaded it, an agent could not"))
        key = ("noted", host, kind)
        now = self.clock()
        last = self._limits.setdefault(key, [-1e18, -1e18, 0])
        last[2] += 1
        if now - last[1] < TRACE_EVERY_S:
            return
        last[1] = now
        if self.trace is not None:
            self.trace.write({"kind": "noted", "lane": self.lane_id, "host": host, "path": _path(url),
                              "method": method.upper(), "nav": kind, "repeats": last[2] - 1,
                              "why": "a human had control: let through, not on the browser's site list"})
        last[2] = 0

    def _record(self, url: str, method: str, refusal: Refusal, *, kind: str, prevented: bool,
                rule: EffectRule | None = None, frame: bool = False) -> Block:
        self._seq += 1
        host = host_of(url) or "?"
        block = Block(seq=self._seq, host=host, rule=refusal.rule, reason=refusal.reason, kind=kind,
                      method=method.upper(), path=_path(url), prevented=prevented,
                      driver=self.driver.public() if self.driver else None,
                      effect_rule=rule.id if rule is not None else None, frame=frame)
        if kind in DOCUMENT_KINDS and self.on_notice is not None and self._human():
            self._notice(block)
        self._notes.append(block)
        self.stats["blocked"] += 1
        if prevented:
            self.stats["prevented"] += 1
        key = (refusal.rule, host, kind)
        now = self.clock()
        last = self._limits.setdefault(key, [-1e18, -1e18, 0])
        last[2] += 1
        row = {"kind": "blocked", "lane": self.lane_id, "rule": block.rule, "host": host, "path": block.path,
               "method": block.method, "nav": kind, "prevented": prevented, "driver": block.driver,
               "effect_rule": block.effect_rule}
        if now - last[1] >= TRACE_EVERY_S:
            last[1] = now
            if self.trace is not None:
                self.trace.write({**row, "repeats": last[2] - 1})
        if now - last[0] >= LOG_EVERY_S:
            repeats = last[2] - 1
            last[0], last[2] = now, 0
            log.warning("%s: egress refused (%s, %s): %s%s", self.lane_id, block.rule, kind, block.reason,
                        f" (and {repeats} more since the last line)" if repeats else "")
            self._event("egress_blocked", {"rule": block.rule, "host": host, "kind": kind, "method": block.method,
                                           "prevented": prevented, "repeats": repeats, "driver": block.driver,
                                           "effect_rule": block.effect_rule})
        if block.rule == "ask_first":
            self._tell_person(block)
        return block

    def _tell_person(self, block: Block) -> None:
        """A publish `ask_first` refused, to Needs you: one card a lane every ASK_FIRST_TELL_S."""
        now = self.clock()
        if self.on_ask_first is None or (self._told_at is not None and now - self._told_at < ASK_FIRST_TELL_S):
            return
        self._told_at = now
        try:
            self.on_ask_first(block)
        except Exception:
            log.warning("%s: could not tell a person about a refused publish", self.lane_id, exc_info=True)

    def _notice(self, block: Block) -> None:
        """Tell the human who has control, in the live view, what was refused and how to allow it.
        One notice per (rule, host, kind) every NOTICE_EVERY_S, with a count of what was folded in,
        and at most NOTICE_BURST a lane in any NOTICE_WINDOW_S. Host and path only: never a query."""
        now = self.clock()
        key = (block.rule, block.host, block.kind)
        last = self._notice_keys.get(key)
        if last is not None and now - last[0] < NOTICE_EVERY_S:
            last[1] += 1
            return
        while self._notice_times and now - self._notice_times[0] >= _constants.NOTICE_WINDOW_S:
            self._notice_times.popleft()
        if len(self._notice_times) >= NOTICE_BURST:
            if last is not None:
                last[1] += 1
            return
        repeats = int(last[1]) if last is not None else 0
        self._notice_keys[key] = [now, 0]
        self._notice_times.append(now)
        entry = allow_entry(block.host, block.frame)
        notice = {"seq": block.seq, "at": datetime.fromtimestamp(block.at, UTC).isoformat(timespec="seconds"),
                  "host": block.host, "path": _short(block.path), "kind": block.kind, "frame": block.frame,
                  "rule": block.rule, "reason": block.reason, "repeats": repeats, "allow": entry,
                  "command": f"openajar lane sites {self.lane_id} add {entry}" if entry else None}
        try:
            self.on_notice(notice)
        except Exception:
            log.warning("%s: could not send a block notice to the live view", self.lane_id, exc_info=True)

    @staticmethod
    def _body(request: Any) -> tuple[bytes | None, dict[str, str]]:
        try:
            body = request.post_data_buffer
        except Exception:  # noqa: BLE001
            body = None
        try:
            headers = dict(request.headers or {})
        except Exception:  # noqa: BLE001
            headers = {}
        return body, headers

    def _lane_host(self, host: str) -> bool:
        """Is `host` one of the lane's own sites (its list, else a host an effect rule names)?"""
        if not host:
            return False
        if self.sites:
            return in_sites(self.sites, host, frame=True)
        return any(globs.domain_match(r.host, host) for r in self.rules)

    def _unmatched(self, request: Any, url: str, method: str) -> None:
        """A non-GET to the lane's own site that no rule matches, sent while an agent's press ran:
        it may be a publish no rule knows. It goes (unknown requests are never blocked: many sites
        read through POST) and is traced by its shape, so a rule can be added (`unmatched_publish`,
        names only, one row per shape a second)."""
        body, headers = self._body(request)
        shape = request_shape(headers, body)
        host, path = host_of(url) or "?", _path(url)
        key = ("unmatched", host, path, str(shape.get("op") or ""), hash(tuple(shape.get("fields") or ())))
        now = self.clock()
        if now - self._observed.get(key, -1e9) < OBSERVE_EVERY_S:
            return
        self._observed[key] = now
        self.stats["unmatched_publishes"] += 1
        log.info("%s: an agent's press sent %s %s%s, which no effect rule matches", self.lane_id, method.upper(),
                 host, _short(path))
        if self.trace is not None:
            self.trace.write({"kind": "unmatched_publish", "lane": self.lane_id, "host": host, "path": path,
                              "method": method.upper(), "size": len(body) if body else 0, "shape": shape,
                              "driver": self.driver.public() if self.driver else None})

    def _unruled(self, host: str) -> bool:
        """Is `host` one of the lane's own sites (a plain entry of its list, not a sign-in or captcha
        host) that no effect rule names? A publish there is not gated: the commit gate's route covers
        only the hosts the rules name (docs/design/egress-guard.md 8.12)."""
        sites = self.sites
        if not host or not sites or not in_sites(sites, host):
            return False
        if in_sites(IDENTITY_SITES, host):
            return False
        return not any(globs.domain_match(r.host, host) for r in self.rules)

    def _unruled_request(self, request: Any, url: str, method: str, *, flow: bool) -> None:
        """A non-GET to a host of the lane's own site list that no effect rule names, sent while no
        person had control (an agent between presses, a flow run, a page's own timer). It goes, as it
        always did (many sites read through POST), and is traced by its shape (`unruled_request`,
        names only, one row per shape a minute), so the owner sees what the site sends that no rule
        reads (openajar.node.rulehealth)."""
        body, headers = self._body(request)
        shape = request_shape(headers, body)
        host, path = host_of(url) or "?", _path(url)
        key = ("unruled", host, path, method.upper(), str(shape.get("op") or ""),
               hash(tuple(shape.get("fields") or ())))
        now = self.clock()
        if now - self._observed.get(key, -1e9) < UNRULED_EVERY_S:
            return
        if len(self._observed) > 2000:
            self._observed.clear()
        self._observed[key] = now
        self.stats["unruled_requests"] += 1
        if self.trace is not None:
            self.trace.write({"kind": "unruled_request", "lane": self.lane_id, "host": host, "path": path,
                              "method": method.upper(), "size": len(body) if body else 0, "shape": shape,
                              "flow": flow, "driver": self.driver.public() if self.driver else None})

    def _hit(self, rule: EffectRule, url: str, method: str) -> None:
        """A write rule matched a request (`rule_hit`: the rule, host, path and method, one row per rule
        a second), so rule health can say when each rule last matched (openajar.node.rulehealth).
        Commit rules leave `observed_commit` or `blocked` rows already."""
        now = self.clock()
        key = ("hit", rule.id, "", "", "", 0)
        if now - self._observed.get(key, -1e9) < HIT_EVERY_S:
            return
        if len(self._observed) > 2000:
            self._observed.clear()
        self._observed[key] = now
        if self.trace is not None:
            self.trace.write({"kind": "rule_hit", "lane": self.lane_id, "rule": rule.id, "effect": rule.effect,
                              "host": host_of(url) or "?", "path": _path(url), "method": method.upper()})

    def _upload(self, request: Any, url: str, headers: dict[str, str], rule: EffectRule) -> None:
        """An upload (a draft rule): its file's hash and size, and later the ids the site answers
        with, so a publish that names the upload binds the file (openajar.node.publish). A media
        registration (`<site>.draft.register...`) carries no file: its answer names the media it
        makes and the upload that will carry the file (publish.registrations)."""
        body = _post_data(request)
        if _REGISTER_RULE.match(rule.id):
            self._spawn(self._registration(request, publish.named_media(body)))
            return
        up = None
        if body and len(body) <= UPLOAD_HASH_MAX:
            try:
                up = publish.upload_of(rule.id, url, headers, body)
            except Exception:
                log.debug("%s: could not read an upload", self.lane_id, exc_info=True)
        if up is None:  # seen, not hashed: a publish naming its ids is refused, never bound to an older file
            ids = publish.upload_ids(url, headers)
            if not ids:
                return
            up = publish.Upload(rule.id, None, len(body or b""), ids)
        for reg in self.registrations:  # registered before it was sent (its URL or S3 key)
            reg.bind(up)
        self.uploads.append(up)
        self.stats["uploads"] += 1
        self._spawn(self._upload_ids(request, up))

    async def _registration(self, request: Any, named: set[str]) -> None:
        try:
            response = await asyncio.wait_for(request.response(), 120)
            if response is None:
                return
            text = await asyncio.wait_for(response.text(), 10)
        except Exception:  # noqa: BLE001 - no answer: nothing binds through it (a publish naming its media is refused)
            return
        if len(text) > publish.PARSE_MAX:
            return
        for reg in publish.registrations(text, named=named):
            self.registrations.append(reg)
            for up in self.uploads:  # an upload already sent (an image registered for an asset it carries)
                reg.bind(up)

    async def _upload_ids(self, request: Any, up: publish.Upload) -> None:
        try:
            response = await asyncio.wait_for(request.response(), 120)
            if response is None:
                return
            text = await asyncio.wait_for(response.text(), 10)
        except Exception:  # noqa: BLE001 - no answer: the upload binds by the ids in its URL
            return
        up.ids |= publish.response_ids(text)
        for reg in self.registrations:  # a registration that names an id the site gave it just now
            reg.bind(up)

    def _observed_commit(self, request: Any, url: str, method: str, rule: EffectRule) -> None:
        body, headers = self._body(request)
        self.stats["observed_commits"] += 1
        if self._human() and self.on_human_commit is not None:
            try:  # a person's own post: on the pace ledger (no check), so an agent's next one keeps the gap
                self.on_human_commit()
            except Exception:
                log.warning("%s: could not record a person's publish on the lane's pace", self.lane_id, exc_info=True)
        if self.trace is not None:
            row = {"kind": "observed_commit", "lane": self.lane_id, "host": host_of(url) or "?",
                   "path": _path(url), "method": method.upper(), "size": len(body) if body else 0,
                   "effect_rule": rule.id, "verified_rule": bool(rule.verified),
                   "driver": self.driver.public() if self.driver else None}
            if self.observing():
                row["shape"] = request_shape(headers, body)
            self.trace.write(row)

    def _observed_request(self, request: Any, url: str, method: str, rule: EffectRule | None) -> None:
        """While the owner observes the lane: a non-GET request by its shape (request_shape)."""
        if self.trace is None:
            return
        body, headers = self._body(request)
        shape = request_shape(headers, body)
        host, path = host_of(url) or "?", _path(url)
        key = (host, path, method.upper(), str(shape.get("op") or ""), hash(tuple(shape.get("fields") or ())))
        now = self.clock()
        if now - self._observed.get(key, -1e9) < OBSERVE_EVERY_S:
            return
        self._observed[key] = now
        if len(self._observed) > 2000:
            self._observed.clear()
        self.stats["observed_requests"] += 1
        self.trace.write({"kind": "observed_request", "lane": self.lane_id, "host": host, "path": path,
                          "method": method.upper(), "size": len(body) if body else 0, "shape": shape,
                          "effect_rule": rule.id if rule else None, "effect": rule.effect if rule else None,
                          "driver": self.driver.public() if self.driver else None})
