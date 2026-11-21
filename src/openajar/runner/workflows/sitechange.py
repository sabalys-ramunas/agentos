"""`site_change`: what changed in a Site API's answer since the last good read (docs/WORKFLOWS.md).

A check is an ordinary read: an `every` schedule runs the endpoint on its surface under the grant of
whoever saved the workflow, so pace, holds, a freeze, health and Ask me first are the run's own, and
its cost is a run like any other. It goes past the HTTP cache, since nothing here asks it, and a good
read fills the cache as any run does (`SiteCache.record`).

`SiteChanges.tick` then looks at each finished check once. It runs on the scheduler's tick, not on the
run's end, and it keys the event by the check's run: a restart between the event and the new baseline
looks at the same run again, finds the same event, and moves on. So nothing is lost or sent twice.

    a first read, or a read of another shape   is the baseline, and starts nothing
    a read that failed in any way              starts nothing, and keeps the baseline
    a read that would remove half the rows     waits for a second read in a row: what both say went is gone
    anything else that differs                 is one event, then the new baseline

The baseline holds the declared fields of each row and no more. A private field is a keyed digest, so
a change in one is seen but its value is never held, and a long text is clipped with a digest of the
whole, so a change past the clip is seen too. Nothing here reads it but the diff. What a person sees
of a watch is `view`: states, counts, times and the node's own words, never a row.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any

from openajar.core.errors import NotFound, OpenajarError, PolicyDenied
from openajar.core.models import Effect, RunStatus
from openajar.runner.records import SUCCEEDED, Run
from openajar.runner.siteapis.cache import below_expected, canonical, http_of
from openajar.runner.workflows.engine import HIDDEN
from openajar.runner.workflows.models import Trigger

log = logging.getLogger("openajar.runner")

LISTS = ("added", "removed", "changed")
EVENT_KIND = "watch.site_changed"
CAUSE = "The site's answer changed"
MAX_ROWS = 2000            # rows a baseline holds; an extract reads at most 500 at a time
MAX_BYTES = 2 * 1024 * 1024
MAX_HELD_CHARS = 300       # of one value; past this the baseline keeps a clip and a digest of the whole
MAX_EVENT_ROWS = 100       # of each list
MAX_EVENT_CHARS = 18_000   # an event's content stops at 20,000, and a cut would break the rows
EMPTY = {"added": [], "removed": [], "changed": [], "count": 0, "count_added": 0, "count_removed": 0, "count_changed": 0,
         "truncated": False}
KEPT = "nothing started and the rows it knew stay"
FAILURES = {
    "empty_result": f"The last read found fewer rows than this API expects, so {KEPT}.",
    "output_contract_violation": f"The site changed shape, so {KEPT}.",
    "connector_contract_broken": f"The site changed shape, so {KEPT}.",
    "no_list": f"The last read gave no list to compare, so {KEPT}.",
    "no_keys": f"No row of the last read had its key, so {KEPT}.",
    "too_big": f"The list is too big to watch, so {KEPT}.",
    "private_key": f"The field that tells these rows apart is private now, so {KEPT}. Choose another key.",
    "no_event": f"The change could not be handed on, so {KEPT}. The next check looks again.",
}
FAILED = f"The last read did not work, so {KEPT}."
STOPS = {
    "pace_limited": "Waiting: this read is over its pace, so this check was skipped.",
    "paused": "Stopped: a person paused this API, so nothing is read. It carries on when it is activated.",
    "connector_broken": "Waiting: this read is broken here, so nothing is read until a repaired version is active.",
    "denied": "Stopped: the access behind this watch does not cover the read any more. A person needs to look at it.",
    "frozen": "Stopped: a freeze covers this read, so nothing is read until it ends.",
    "not_found": "Stopped: this API is not on this computer any more.",
    "not_a_read": "Stopped: this endpoint does more than read now, and a check must never act on the site. It carries on "
                  "when a version that only reads is active.",
}
STOPPED = "Stopped: the read could not start. A person needs to look at it."


class NotARead(PolicyDenied):
    code = "not_a_read"


class BadRead(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class Read:
    rows: dict[str, dict[str, Any]]
    skipped: int
    cut: bool  # it may not hold every row, so it calls nothing removed
    fingerprint: str


def blob(value: Any) -> bytes:
    """As bytes, whatever a page held: a lone surrogate reads as text, and has no UTF-8 of its own."""
    return canonical(value).encode(errors="replace")


def plain_key(value: Any) -> str | None:
    """A row's key as text, or None when it has none it can be told apart by."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    return str(value).strip() or None


def held(value: Any, private: bool, secret: str) -> Any:
    """A value as the baseline keeps it: two are the same exactly when the site's were."""
    long = isinstance(value, str) and len(value) > MAX_HELD_CHARS
    if not private and not long:
        return value
    digest = hmac.new(secret.encode(), blob(value), hashlib.sha256).hexdigest()[:32]
    return {"digest": digest} if private else {"digest": digest, "clip": value[:MAX_HELD_CHARS]}


def shown(value: Any) -> Any:
    """A held value as a step may read it: a private one never comes back."""
    return (value.get("clip", HIDDEN)) if isinstance(value, dict) else value


def rows_of(raw: list[Any], key: str, fields: list[str], private: set[str], secret: str) -> tuple[dict[str, dict[str, Any]], int, int]:
    """(rows by key, how many were left out, how many of those had no key). Of rows with one key the
    one kept is the first by its values, not by its place, so a re-sort changes nothing. That is only
    a duplicate: a row with no key may be one that is known, so it says something about what went."""
    out: dict[str, dict[str, Any]] = {}
    skipped = missing = 0
    for item in raw:
        name = plain_key(item.get(key)) if isinstance(item, dict) else None
        if name is None:
            skipped += 1
            missing += 1
            continue
        row = {f: held(item.get(f), f in private, secret) for f in fields}
        if name in out:
            skipped += 1
            if canonical(row) >= canonical(out[name]):
                continue
        out[name] = row
    return out, skipped, missing


def cut_short(saved: list[dict[str, Any]], length: int) -> bool:
    """This list lost rows: a step found more than it kept (`max_items`), or the output cap trimmed
    what it kept. `truncated` on a run says less than that: one clipped value anywhere sets it too."""
    count = sum(int(s.get("count") or 0) for s in saved)
    return sum(int(s.get("total") or 0) for s in saved) > count or length < count


def diff(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    """(added, removed, changed) keys, sorted. Declared fields only, and a row's place means nothing."""
    return (sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys()),
            sorted(k for k in new.keys() & old.keys() if new[k] != old[k]))


def fits(rows: dict[str, Any]) -> bool:
    return len(rows) <= MAX_ROWS and len(blob(rows)) <= MAX_BYTES


def a_drop(old: dict[str, Any], removed: list[str]) -> bool:
    """Half or more of what was known went at once: a site that broke looks just like this."""
    return bool(removed) and len(removed) * 2 >= len(old)


def row_view(name: str, row: dict[str, Any]) -> dict[str, Any]:
    return {**{f: shown(v) for f, v in row.items()}, "row_key": name}


def changed_view(name: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    fields = sorted(f for f in after if before.get(f) != after[f])
    return {**row_view(name, after), "what_changed": fields, **{f"was_{f}": shown(before.get(f)) for f in fields}}


def fitted(lists: dict[str, list[dict[str, Any]]]) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """(what one event carries, whether any row stayed out). One of each kind at a time, so a long
    list of one kind does not push another out."""
    kept: dict[str, list[dict[str, Any]]] = {name: [] for name in LISTS}
    left, cut = MAX_EVENT_CHARS, False
    for rows in zip_longest(*(lists[name][:MAX_EVENT_ROWS] for name in LISTS)):
        for name, row in zip(LISTS, rows, strict=True):
            if row is None:
                continue
            size = len(json.dumps(row)) + 2
            if size > left:
                cut = True
                continue
            kept[name].append(row)
            left -= size
    return kept, cut or any(len(lists[name]) > MAX_EVENT_ROWS for name in LISTS)


def trigger_values(event: Any) -> dict[str, Any]:
    """What a run of this event gives its steps. The rows are the site's, the counts the node's."""
    facts = event.facts
    try:
        rows = json.loads(event.content.text) if event.content is not None else {}
    except ValueError:
        rows = {}
    out: dict[str, Any] = {name: rows.get(name) or [] for name in LISTS}
    counts = {k: int(facts.get(k) or 0) for k in ("count_added", "count_removed", "count_changed")}
    return {**out, **counts, "count": sum(counts.values()), "truncated": bool(facts.get("truncated"))}


def parts_of(operation: str) -> tuple[str, str, str]:
    """(flow id, the API's key, the endpoint) of `connector.<api>.<endpoint>@<surface>`."""
    flow_id = operation.partition("@")[0]
    _, api, endpoint = (flow_id.split(".", 2) + ["", ""])[:3]
    return flow_id, api, endpoint


class SiteChanges:
    def __init__(self, service: Any):
        self.service = service
        self.store = service.store
        self.runner = service.runner
        self.clock = service.clock

    def _secret(self) -> str:
        return self.runner.store.meta("site_change_key", secrets.token_hex(16))

    def _schedule(self, state: Any) -> Any:
        scheduler = self.runner.scheduler
        return scheduler.store.get_schedule(state.schedule_id) if scheduler is not None and state.schedule_id else None

    def forget(self, state: Any) -> None:
        """It was turned off: the next read is a first read, and the check from before does not count."""
        s = self._schedule(state)
        self.store.drop_watch(state.id)
        if s is not None and s.state.get("last_run_id"):
            self.store.put_watch(state.id, {"settled": s.state["last_run_id"]})

    def check_fire(self, schedule: Any) -> None:
        """Right before a check: saving and activating looked at the endpoint, and this is what holds
        if it stopped being a read in any other way. The run then pins this same version."""
        state = next((s for s in self.store.states() if s.schedule_id == schedule.id), None)
        wv = self.store.version(state.id, state.live) if state is not None and state.live else None
        if wv is None or wv.definition.trigger.kind != "site_change":
            return
        op = self.service.operation(wv.definition.trigger.operation or "")
        if op is not None and op.effect != Effect.READ:
            raise NotARead(f"{op.name} is a {op.effect.value} endpoint now, so {wv.definition.name} does not check it")

    def tick(self) -> None:
        for state in self.store.states():
            if not state.enabled or not state.live or not state.schedule_id:
                continue
            wv = self.store.version(state.id, state.live)
            if wv is None or wv.definition.trigger.kind != "site_change":
                continue
            try:
                self.settle(state, wv)
            except Exception:
                log.exception("the check of %s could not be read", state.id)

    # ---- one finished check ----
    def settle(self, state: Any, wv: Any) -> None:
        s = self._schedule(state)
        run_id = s.state.get("last_run_id") if s is not None else None
        doc = self.store.watch(state.id) or {}
        run = self.runner.store.get_run(run_id) if run_id and run_id != doc.get("settled") else None
        if run is None or not run.terminal:
            return
        now = self.clock.now().isoformat()
        doc.update(settled=run.id, checked_at=now, last_run=run.id)
        try:
            read = self._read(run, wv.definition.trigger)
        except BadRead as e:
            self._failed(state, doc, e.code)
            return
        if read is None:  # the run was of a version that is not the active one any more: it says nothing
            self.store.put_watch(state.id, doc)
            return
        old = self.store.watch_rows(state.id)
        if old is None or doc.get("fingerprint") != read.fingerprint:
            self._baseline(state, doc, read, again=old is not None)
            return
        added, gone, changed = diff(old, read.rows)
        removed = [] if read.cut else gone  # the rest is past the cut or lost its key, and a row there looks like one that went
        doc.update(skipped=read.skipped, failure=None, restarted=False)
        pending = doc.get("pending")
        if pending is None and a_drop(old, removed):
            self.store.put_watch(state.id, {**doc, "pending": removed})
            return
        if pending is not None:  # gone only if both reads say so; a row only this one lost has its own two reads to come
            removed = sorted(set(removed) & set(pending))
        doc["pending"] = None
        rows = {**{k: old[k] for k in set(gone) - set(removed)}, **read.rows}  # what is not called gone stays known
        if not fits(rows):  # dropping any to fit would make it look new when it came back
            self._failed(state, doc, "too_big")
            return
        if added or removed or changed:
            try:
                self._emit(state, wv, run, read, old, added, removed, changed)
            except OpenajarError as e:
                log.warning("the change in %s could not be handed on: %s", state.id, e.message)
                self._failed(state, doc, "no_event")
                return
            doc["changed_at"] = now
        self._store(state, doc, rows)

    def _read(self, run: Run, t: Trigger) -> Read | None:
        """What this run read, or None when it says nothing. Anything that is not a good read of the
        list is a BadRead: a site that breaks must never look like every row went."""
        if run.status not in SUCCEEDED:
            raise BadRead(str((run.error or {}).get("code") or "failed"))
        head = self.runner.flows.store.get_flow(run.flow_id or "")
        if head is None:
            raise BadRead("not_found")
        if head.version != run.flow_version:
            return None
        out = run.output or {}
        data = out.get("data") or {}
        raw = data.get(t.list)
        if not isinstance(raw, list):
            raise BadRead("no_list")
        if below_expected(head, data):
            raise BadRead("empty_result")
        meta = head.definition.connector or {}
        schema = ((meta.get("output") or {}).get("properties") or {}).get(t.list) or {}
        props = (schema.get("items") or {}).get("properties") or {}
        fields = sorted(props)
        if t.key not in props:
            raise BadRead("no_keys")
        prefix = f"{t.list}[]."
        private = {p[len(prefix):] for p in http_of(head).get("private_fields") or [] if p.startswith(prefix)}
        if t.key in private:  # a key is shown, and is what the baseline is keyed by: no value of it goes there or on
            raise BadRead("private_key")
        rows, skipped, missing = rows_of(raw, t.key or "", fields, private, self._secret())
        if raw and not rows:
            raise BadRead("no_keys")
        if not fits(rows):
            raise BadRead("too_big")
        who = [run.flow_id, run.surface, run.lane, run.params, t.list, t.key, props, sorted(private)]
        saved = self._saved(run, str((meta.get("outputs") or {}).get(t.list) or "").partition(".")[0])
        cut = cut_short(saved, len(raw)) if saved else bool(out.get("truncated"))
        return Read(rows, skipped, cut or missing > 0, hashlib.sha256(blob(who)).hexdigest()[:32])

    def _saved(self, run: Run, name: str) -> list[dict[str, Any]]:
        """What the run's steps saved as `name`, with its `count` and `total`: one for each pass of a for_each."""
        store = self.runner.store
        docs = {**{r.path: r.result for r in store.steps(run.id)}, **store.step_states(run.id)}
        saved = [((d or {}).get("vars") or {}).get(name) for d in docs.values()]
        return [s for s in saved if isinstance(s, dict)]

    def _failed(self, state: Any, doc: dict[str, Any], code: str) -> None:
        """Nothing starts, and the baseline stays. A failure also ends "two reads in a row"."""
        last = doc.get("failure") or {}
        doc.update(pending=None, failure={"code": code, "count": int(last.get("count") or 0) + 1})
        self.store.put_watch(state.id, doc)

    def _baseline(self, state: Any, doc: dict[str, Any], read: Read, *, again: bool) -> None:
        doc.update(fingerprint=read.fingerprint, pending=None, failure=None, skipped=read.skipped, restarted=again)
        self._store(state, doc, read.rows)

    def _store(self, state: Any, doc: dict[str, Any], rows: dict[str, Any]) -> None:
        doc["rows"] = len(rows)
        self.store.put_watch(state.id, doc, json.dumps(rows))

    def _emit(self, state: Any, wv: Any, run: Run, read: Read, old: dict[str, Any], added: list[str], removed: list[str],
              changed: list[str]) -> None:
        events = self.service._svc("event_queue")
        if events is None:
            raise NotFound("the events layer is not running on this node")
        d, t = wv.definition, wv.definition.trigger
        _, api, endpoint = parts_of(t.operation or "")
        lists = {"added": [row_view(k, read.rows[k]) for k in added], "removed": [row_view(k, old[k]) for k in removed],
                 "changed": [changed_view(k, old[k], read.rows[k]) for k in changed]}
        kept, cut = fitted(lists)
        fv = self.runner.flows.store.get_flow(run.flow_id or "")
        events.emit({
            "source": "watch", "kind": EVENT_KIND, "workspace": d.workspace, "lane": run.surface,
            "dedupe_key": f"site_change:{state.id}:{run.id}",
            "facts": {"workflow": state.id, "api": api, "endpoint": endpoint, "run": run.id,
                      "url": f"https://{fv.definition.domain}/" if fv is not None else "", "count_added": len(added),
                      "count_removed": len(removed), "count_changed": len(changed), "truncated": cut},
            "content": {"text": json.dumps(kept), "origin": f"{api}.{endpoint} on {run.surface}"},
        }, route_id=state.route_id)

    # ---- what a person reads ----
    def view(self, state: Any, wv: Any) -> dict[str, Any]:
        """How the watch stands, in the node's words: this goes on the routine, and leaves the node
        with it, so it holds no row and nothing a site wrote."""
        t = wv.definition.trigger
        doc = self.store.watch(state.id) or {}
        s = self._schedule(state)
        st = s.state if s is not None else {}
        status, words = self._standing(state, t, doc, s)
        return {"state": status, "words": words, "rows": doc.get("rows"), "skipped": doc.get("skipped") or 0,
                "failures": (doc.get("failure") or {}).get("count") or 0, "last_checked_at": doc.get("checked_at"), "last_changed_at": doc.get("changed_at"),
                "last_run": st.get("last_run_id"), "next_at": st.get("next_check_at") if state.enabled else None}

    def _standing(self, state: Any, t: Trigger, doc: dict[str, Any], s: Any) -> tuple[str, str]:
        flow_id, api, endpoint = parts_of(t.operation or "")
        st = s.state if s is not None else {}
        if not state.enabled:
            return "off", "It is off, so nothing is read."
        if s is not None and not s.enabled:  # a freeze stops a schedule, and what it stopped a person starts again
            return "stopped", "Stopped: a freeze or a person stopped its schedule. Turn it on again to carry on."
        events = self.service._svc("event_queue")
        route = events.store.get_route(state.route_id) if events is not None and state.route_id else None
        if route is not None and route.state.paused:
            return "stopped", "Stopped: a freeze or a person paused what this starts. What changed waits until the owner resumes it."
        site_apis = getattr(self.runner, "site_apis", None)
        if self.runner.flows.store.get_flow(flow_id) is None:
            return "stopped", f"Stopped: {api} is not on this computer any more."
        if site_apis is not None and site_apis.state_of(api) == "paused":
            return "stopped", f"Stopped: a person paused {api}, so nothing is read. It carries on when it is activated."
        if st.get("held"):
            return "waiting", f"Waiting: {endpoint} is broken here, so nothing is read until a repaired version is active."
        err = st.get("last_error") or {}
        if err:
            code = str(err.get("code"))
            return ("waiting" if code in ("pace_limited", "connector_broken") else "stopped"), STOPS.get(code, STOPPED)
        pending = self.runner.store.get_run(st["last_run_id"]) if st.get("last_run_id") else None
        if pending is not None and pending.status == RunStatus.WAITING_APPROVAL:
            return "waiting", "Waiting: this read asks a person first."
        if pending is not None and not pending.terminal and (pending.error or {}).get("control"):
            return "waiting", "Waiting: the read is held, and goes on when the lane or the API is free again."
        failure = doc.get("failure") or {}
        if failure:
            code = str(failure.get("code"))
            return "failing", FAILURES.get(code, FAILED)
        if doc.get("pending"):
            return "confirming", "The last read had far fewer rows, so nothing started. The next read decides."
        if doc.get("fingerprint") is None:
            return "starting", "The first read sets what it compares with, and starts nothing."
        if doc.get("restarted"):
            return "watching", "The API changed shape, so it started over from the last read."
        return "watching", "Watching."
