"""The learn window of a lane's egress guard (LaneGuard, docs/design/learn-a-site.md section 4): a
mixin, like GuardPrevention and GuardRecords. agentos.node.egress.learn_window says what it checks
and why; agentos.node.egress.learn reaches it from the runtime."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from agentos.node.egress import rules as _rules
from agentos.node.egress.constants import OBSERVE_EVERY_S
from agentos.node.egress.helpers import _path, _post_data, _short
from agentos.node.egress.learn_window import (
    LEARN_TTL_S,
    LearnWindow,
    carries_exactly,
    check_open,
    endpoint_matches,
    form_data_of,
    others_changed,
    path_under,
)
from agentos.node.egress.rules import EffectRule
from agentos.node.egress.shapes import request_op, request_shape
from agentos.node.egress.sites import host_of, norm_path
from agentos.node.egress.verdicts import CommitMeter, Driver, PublishGate, Refusal

log = logging.getLogger("agentos.node.egress")

FORM_DATA_KEPT = 8  # the latest form_data answers kept (in memory only, never traced)
ASKS_FIRST = ("this browser asks a person first (Ask me first is on), so a lesson's save is not let through "
              "without them and nothing was sent. Tell the human: they turn Ask me first off for the lesson, "
              "or change the field by hand and mark it done")


class GuardLearn:
    """The learn window's part of LaneGuard (the state is set in LaneGuard.__init__)."""

    lane_id: str
    clock: Callable[[], float]
    driver: Driver | None
    trace: Any
    rules: tuple[EffectRule, ...]
    _context: Any
    _routines: int
    _observed: dict[tuple[Any, ...], float]
    _learn: LearnWindow | None
    _learn_done: dict[str, dict[str, Any]]
    _form_data: dict[tuple[str, str], dict[str, Any]]
    _act_seq: int
    _act_at: float | None
    _act_grant: str | None
    _act_call: str | None

    # ---- opening, reading, closing ---------------------------------------------------------

    def open_learn(self, *, field: str, value: str, path_prefixes: list[str] | tuple[str, ...], grant_id: str,
                   site: str, ttl_s: int = LEARN_TTL_S, pinned: Callable[[], Any] | None = None,
                   excluded: Callable[[], str | None] | None = None,
                   identity: Callable[[str], Awaitable[dict[str, str] | None]] | None = None) -> str:
        """Open the lane's learn window and return its id. It lets exactly ONE request go past the
        commit gate with no card, and only when every one of these holds (_learn_problem):

        - an act of `grant_id`'s driver sent it, by a press (the act's gate is submit_like and
          active); no flow holds the lane; a person is not in control. Any act of another driver,
          a person taking the lane or a flow taking it closes the window;
        - it is the field's reviewed endpoint (agentos.node.sites.PROFILE_EDIT_ENDPOINTS: method,
          exact host, whole path, operation name), classified `<site>.unknown`,
          `<site>.profile.<field>` or `<site>.profile.learned_<field>` (or the learned rule of a
          field saved by the very same endpoint), and no operation in it is anything else;
        - the endpoint's `body_key` holds exactly `value`, once; every other field of the body equals
          the form's current values the page loaded (form_data); since the window opened the agent
          entered exactly one text, `value`, and uploaded nothing (learn_window's docstring);
        - the page that sent it is on the site, under one of `path_prefixes` (the site's reviewed
          edit pages, with the pinned account's id where they name one);
        - the browser is signed in to the lane's pinned account (`pinned()`; None is unpinned);
        - "Ask me first" is off for the lane: when it is on, the save is refused (ask_first) and the
          window closes, since a lesson's save never asks for a card;
        - the act's meter takes it: the save is one commit on the grant and the lane, charged like any
          publish (a limit or the lane's pace refuses it, lane_paced or limit_reached).

        After that one request, after `ttl_s`, or on a breach above, the window is closed; opening
        another closes this one. Called by node code only (agentos.node.egress.learn), never by a
        tool argument. ValueError for arguments it cannot open with."""
        from agentos.node.sites import profile_edit_endpoint

        prefixes = check_open(field, value, path_prefixes, ttl_s)
        if not isinstance(grant_id, str) or not grant_id:
            raise ValueError("a learn window is bound to the grant of the agent that learns: name it")
        endpoint = profile_edit_endpoint(site, field) if isinstance(site, str) else None
        if endpoint is None:
            raise ValueError(f"{field} on {site} has no reviewed edit endpoint "
                             "(agentos.node.sites.PROFILE_EDIT_ENDPOINTS), so it cannot be learned")
        self._learn = LearnWindow(id="lw_" + secrets.token_hex(8), field=field, value=value, path_prefixes=prefixes,
                                  until=self.clock() + float(ttl_s), site=site, grant_id=grant_id, endpoint=endpoint,
                                  pinned=pinned or (lambda: None), excluded=excluded, identity=identity)
        log.info("%s: a learn window for %s opened (%ss)", self.lane_id, field, ttl_s)
        return self._learn.id

    def learn_result(self, window_id: str) -> dict[str, Any] | None:
        """What the window released, or None while nothing was: {host, path, method, op, url,
        page_url, at} and the act that sent it: `act_seq` (the guard's count of agent acts, 1 for
        the first act since the browser launched), `act_at` (when that act began, ISO UTC) and
        `act_call` (the content hash of that tool call's arguments, as the commit gate keys it;
        None when the act's gate has none)."""
        found = self._learn_done.get(window_id)
        return dict(found) if found is not None else None

    def learn_window(self, window_id: str) -> LearnWindow | None:
        """The window while it is open (not used, not expired), else None."""
        w = self._learn
        if w is None or w.id != window_id:
            return None
        if w.used or self.clock() >= w.until:
            self._learn = None
            return None
        return w

    def close_learn(self, window_id: str) -> None:
        """Close the window (it releases nothing more) and forget what it released."""
        if self._learn is not None and self._learn.id == window_id:
            self._learn = None
        self._learn_done.pop(window_id, None)

    def _learn_close(self, why: str) -> None:
        w = self._learn
        if w is None:
            return
        self._learn = None
        w.note_refused(why)
        log.info("%s: the learn window for %s closed: %s", self.lane_id, w.field, why)
        if self.trace is not None:
            with contextlib.suppress(Exception):
                self.trace.write({"kind": "learn_closed", "lane": self.lane_id, "window": w.id, "field": w.field,
                                  "why": why})

    # ---- what the guard tells it -------------------------------------------------------------

    def _learn_act(self, driver: Driver | None, agent: bool, gate: Any) -> None:
        """An act begins (acting): count it, and close the window when it is not the window's driver's."""
        if agent and gate is not None:
            self._act_seq += 1
            self._act_at = time.time()
            self._act_grant = driver.grant_id if driver is not None else None
            key = getattr(gate, "key", None)
            self._act_call = key[2] if isinstance(key, tuple) and len(key) == 3 and isinstance(key[2], str) else None
        w = self._learn
        if w is not None and not (agent and driver is not None and driver.grant_id == w.grant_id):
            self._learn_close("another driver acted on the browser")

    def _learn_driver(self, driver: Driver | None) -> None:
        """drive(): a person, or an agent of another grant, now drives the lane."""
        w = self._learn
        if w is not None and not (driver is not None and not driver.human and driver.grant_id == w.grant_id):
            self._learn_close("another driver took the browser")

    def _learn_typed(self, text: str | None, *, key: bool = False) -> None:
        """The agent entered text: the window allows exactly one text, the value, and no single keys."""
        w = self._learn
        if w is None or not text:
            return
        if key:
            self._learn_close("a key was typed one character at a time")
            return
        w.typed.append(text)
        if w.typed != [w.value]:
            self._learn_close("the agent entered other text than the value, or more than one text")

    def _learn_upload(self) -> None:
        if self._learn is not None:
            self._learn_close("something was uploaded")

    def _learn_finished(self, request: Any) -> None:
        """A GET finished: when it is a reviewed endpoint's form_data, keep the form's current values."""
        from agentos.node.sites import PROFILE_EDIT_ENDPOINTS

        try:
            if request.method.upper() != "GET":
                return
            url = request.url
        except Exception:  # noqa: BLE001
            return
        host, path = host_of(url) or "", norm_path(url)
        for fields in PROFILE_EDIT_ENDPOINTS.values():
            for ep in fields.values():
                if ep.form_data is not None and ep.form_data[0] == host and re.fullmatch(ep.form_data[1], path):
                    self._spawn(self._read_form_data(request, ep.form_data))  # type: ignore[attr-defined]
                    return

    async def _read_form_data(self, request: Any, source: tuple[str, str, str]) -> None:
        try:
            response = await asyncio.wait_for(request.response(), 30)
            if response is None:
                return
            text = await asyncio.wait_for(response.text(), 10)
        except Exception:  # noqa: BLE001 - not read: nothing is released without it
            return
        found = form_data_of(text, source[2])
        if found is None:
            return
        self._form_data.pop((source[0], source[1]), None)
        self._form_data[(source[0], source[1])] = found
        while len(self._form_data) > FORM_DATA_KEPT:
            self._form_data.pop(next(iter(self._form_data)))

    # ---- a learned operation's save --------------------------------------------------------------

    def _strict_form_problem(self, commit: Any, url: str, method: str, headers: dict[str, str],
                             body: bytes | None) -> str | None:
        """A strict (learned) run's request to a reviewed profile save endpoint: like the learn
        window, it may change only its own field. The field is the endpoint's `body_key` the run's
        commit request pins (`match`); every other field of the form must equal what the page
        loaded (form_data). None when it does, or when the request is no reviewed endpoint."""
        from agentos.node.sites import PROFILE_EDIT_ENDPOINTS

        host, path = host_of(url) or "", norm_path(url)
        op = request_op(headers, body)
        hits = [ep for fields in PROFILE_EDIT_ENDPOINTS.values() for ep in fields.values()
                if endpoint_matches(ep, host, path, method, op)]
        if not hits:
            return None
        pinned = {key for key, _ in getattr(commit, "match", ())}
        ep = next((e for e in hits if e.body_key in pinned), None)
        if ep is None:
            return (f"{commit.flow} saves a profile form without naming which field it sets; it was learned "
                    "before saves were pinned to their field, so learn it again")
        baseline = self._form_data.get((ep.form_data[0], ep.form_data[1])) if ep.form_data else None
        changed = others_changed(body, ep.body_key, baseline, ep.fields)
        if changed is None:
            return (f"{commit.flow} saves a profile form whose current values the page did not load, so what "
                    "else it changes cannot be told")
        if changed:
            return f"{commit.flow} saves its field and also changes {', '.join(changed[:5])}"
        return None

    # ---- the release ---------------------------------------------------------------------------

    async def _learn_release(self, gate: PublishGate, rule: EffectRule, request: Any, url: str, method: str,
                             headers: dict[str, str], *, meter: CommitMeter | None = None) -> bool | Refusal:
        """Does the open learn window let this request go (see open_learn)? True uses the window up
        (and charged `meter` one commit); False: the window does not apply, the gate holds it as
        usual; a Refusal: the window's own request, aborted with it (Ask me first is on, or a limit
        or the lane's pace refused the commit). Never raises: a check that cannot be made refuses."""
        w = self._learn
        if w is None:
            return False
        page_url = ""
        try:
            why = self._learn_problem(w, gate, rule, request, url, method, headers)
            if why is None:
                page_url = str(request.frame.page.url)
                why = await self._learn_account(w, rule.site)
        except Exception:  # fail closed: a check that cannot be made refuses
            log.warning("%s: the learn window could not check a request; it is not released", self.lane_id,
                        exc_info=True)
            why = "the request could not be checked"
        if why is None and (self._learn is not w or w.used or self.clock() >= w.until):
            why = "the window closed while the account was read"
        if why is not None:
            self._learn_refused(w, rule, url, method, why)
            return False
        if self._asks_first():  # type: ignore[attr-defined]
            self._learn_refused(w, rule, url, method, "Ask me first is on for the browser")
            self._learn_close("Ask me first is on for the browser")
            return Refusal("ask_first", ASKS_FIRST)
        over = self._charge(rule, sent=False, meter=meter)  # type: ignore[attr-defined]
        if over is not None:  # within the limits it is debited now, like any publish of an act
            self._learn_refused(w, rule, url, method, over.reason)
            return over
        w.used = True  # one request only: claimed before anything else can await
        self._learn = None
        body = _post_data(request)
        shape = request_shape(headers, body)
        host, path = host_of(url) or "", norm_path(url)
        bare = urlsplit(url)._replace(query="", fragment="").geturl()
        page = urlsplit(page_url)._replace(query="", fragment="").geturl()
        act_at = datetime.fromtimestamp(self._act_at, UTC).isoformat(timespec="milliseconds") if self._act_at else None
        result = {"host": host, "path": path, "method": method.upper(), "op": shape.get("op"), "url": bare,
                  "page_url": page, "at": datetime.now(UTC).isoformat(timespec="seconds"),
                  "act_seq": self._act_seq, "act_at": act_at, "act_call": self._act_call}
        self._learn_done = {**dict(list(self._learn_done.items())[-7:]), w.id: result}
        log.info("%s: the learn window released %s %s%s for %s", self.lane_id, method.upper(), host, _short(path),
                 w.field)
        if self.trace is not None:
            self.trace.write({"kind": "learn_commit", "lane": self.lane_id, "window": w.id, "field": w.field,
                              "host": host, "path": path, "method": method.upper(), "rule": rule.id,
                              "page_path": _path(page_url), "shape": shape, "act_seq": self._act_seq,
                              "driver": self.driver.public() if self.driver else None})
        return True

    def _learn_refused(self, w: LearnWindow, rule: EffectRule, url: str, method: str, why: str) -> None:
        w.note_refused(why)
        key = ("learn", w.id, host_of(url) or "?", _path(url), why)
        now = self.clock()
        if self.trace is not None and now - self._observed.get(key, -1e9) >= OBSERVE_EVERY_S:
            self._observed[key] = now
            with contextlib.suppress(Exception):
                self.trace.write({"kind": "learn_refused", "lane": self.lane_id, "window": w.id, "field": w.field,
                                  "host": host_of(url) or "?", "path": _path(url), "method": method.upper(),
                                  "rule": rule.id, "why": why})
        log.info("%s: the learn window did not release %s %s%s: %s", self.lane_id, method.upper(),
                 host_of(url) or "?", _short(_path(url)), why)

    def _rule_fits(self, w: LearnWindow, rule: EffectRule) -> bool:
        """`<site>.unknown`, `<site>.profile.<field>`, `<site>.profile.learned_<field>`, or the
        learned rule of another field the very same reviewed endpoint saves (one form, several fields)."""
        from agentos.node.sites import PROFILE_EDIT_ENDPOINTS

        if rule.site != w.site:
            return False
        if rule.id in (f"{w.site}.{_rules.UNKNOWN}", f"{w.site}.profile.{w.field}", f"{w.site}.profile.learned_{w.field}"):
            return True
        m = re.fullmatch(re.escape(w.site) + r"\.profile\.learned_([a-z_]{1,30})", rule.id)
        other = PROFILE_EDIT_ENDPOINTS.get(w.site, {}).get(m.group(1)) if m else None
        ep = w.endpoint
        return other is not None and (other.method, other.host, other.path, other.op) == (ep.method, ep.host,
                                                                                          ep.path, ep.op)

    def _learn_problem(self, w: LearnWindow, gate: PublishGate, rule: EffectRule, request: Any, url: str,
                       method: str, headers: dict[str, str]) -> str | None:
        """Why the window may not release this request (every check but the account's), or None. A
        request to the field's endpoint that changes anything but the field closes the window."""
        from agentos.node import sites as node_sites
        from agentos.node.egress.classify import effects_of

        if w.used or self.clock() >= w.until:
            self._learn = None
            return "the window is closed"
        d = self.driver
        if d is None or d.human or self._human():  # type: ignore[attr-defined]
            return "no agent drives the browser"
        if d.grant_id != w.grant_id or self._act_grant != w.grant_id:
            return "another grant than the one the window was opened for drives the browser"
        if self._routines or self._flow_owns(request):  # type: ignore[attr-defined]
            return "a flow or routine holds the browser"
        if not (getattr(gate, "submit_like", False) and getattr(gate, "active", False)):
            return "it was not sent by a press of the agent's act"
        if w.excluded is not None:
            stop = w.excluded()
            if stop:
                return stop
        host, path = host_of(url) or "", norm_path(url)
        body = _post_data(request)
        if not endpoint_matches(w.endpoint, host, path, method, request_op(headers, body)):
            return "it is not the field's reviewed edit endpoint"
        if not self._rule_fits(w, rule):
            return f"it is classified {rule.id}, not the {w.field} edit"
        others = [r.id for r in effects_of(url, method, headers, body=body, rules=self.rules)
                  if not self._rule_fits(w, r)]
        if others:
            return f"it also carries {', '.join(others[:3])}"
        page_url = str(request.frame.page.url)
        site = node_sites.site(w.site)
        page_host = host_of(page_url) or ""
        if site is None or not any(node_sites.host_is(page_host, h) for h in site.hosts):
            return "the page that sent it is not on the site"
        if not path_under(page_url, w.path_prefixes):
            return "the page that sent it is not one of the site's profile edit pages"
        # the endpoint's own request from here on: what it changes decides, and a breach closes the window
        if w.typed != [w.value]:
            self._learn_close("the save came before the agent entered exactly the value")
            return "the agent did not enter exactly the value, once"
        if not carries_exactly(body, w.endpoint.body_key, w.value):
            self._learn_close("the save does not carry exactly the value in the field")
            return f"its {w.endpoint.body_key} is not exactly the value"
        baseline = self._form_data.get((w.endpoint.form_data[0], w.endpoint.form_data[1])) \
            if w.endpoint.form_data else None
        changed = others_changed(body, w.endpoint.body_key, baseline, w.endpoint.fields)
        if changed is None:
            return "the form's current values were not seen (the edit page loads them), so what else it changes " \
                   "cannot be told"
        if changed:
            self._learn_close("the save changes other fields too")
            return f"it also changes {', '.join(changed[:5])}"
        return None

    async def _learn_account(self, w: LearnWindow, site: str) -> str | None:
        """None when the browser is signed in to the lane's pinned account on `site`, else why not."""
        from agentos.node import sites as node_sites

        pinned = w.pinned()
        if pinned is None or not getattr(pinned, "user_id", None):
            return "the browser pins no account (unpinned)"
        if node_sites.site_key(pinned.site) != site:
            return f"the browser's pinned account is on {pinned.site}, not {site}"
        if w.identity is not None:
            seen = await w.identity(site)
        elif self._context is not None and callable(getattr(self._context, "cookies", None)):
            seen = await node_sites.account_identity(self._context, site)
        else:
            seen = None
        verdict = node_sites.account_matches(pinned, seen)
        if verdict is None:
            return "the signed-in account cannot be read, so it cannot be told from another"
        if verdict is False:
            return "the browser is signed in to another account than the browser's pinned one (wrong_account)"
        return None

