"""SiteApiService, runtime service "site_apis": drafts, compile, activate, pause, remove.

    draft -> proposed (an agent asks) -> active -> paused

Who may do what is decided here, on `by` (a Principal or {kind, id, owner}):

    an agent    saves its own drafts, tries a read, proposes. Its input steps count as commit and its
                text fields stay untrusted. It never activates, edits a live API, or touches a
                draft a person wrote.
    a person    saves and edits drafts, tries a read or a draft (a paused API too), pauses.
    the owner   activates, which is what widens or re-points an API: the live flows only ever
                change here. An agent's input steps need `vouch`, a person's explicit say.

An activated API is its `connector.<api>.<endpoint>` flows, saved through local_saved_by. A try has
no route of its own: it compiles the draft and stages that version (FlowService.stage), so the run is
an ordinary connector run with a pinned version, counted against pace, while no tool, schedule or
list can see it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from agentos.core.errors import AgentosError, InvalidRequest, NotFound, PolicyDenied, ServiceUnavailable
from agentos.core.models import LANE_ID_RE, ActsAs, Effect, Principal, RunStatus, Surface
from agentos.core.signals import QUIET
from agentos.core.tools import TOOL_NAME_RE
from agentos.node.sites import site as known_site
from agentos.runner.connectors.compile import CompiledConnector, ConnectorInvalid
from agentos.runner.connectors.lint import Issue, param_problems
from agentos.runner.connectors.models import BINDING_PARAMS, WEB_SITE, Binding, version_tuple
from agentos.runner.connectors.steps import is_input, step_locators, walk
from agentos.runner.connectors.surfaces import ConnectorBroken, Target
from agentos.runner.connectors.tools import tool_name
from agentos.runner.models import (
    CONNECTOR_PREFIX,
    SUCCEEDED,
    TRY_SOURCE,
    FlowVersion,
    answered_in_person,
    is_owner,
)
from agentos.runner.recorder import _sensitive
from agentos.runner.siteapis import discover as discovery
from agentos.runner.siteapis.models import (
    DEFAULT_PACE,
    READS,
    Endpoint,
    SiteApi,
    State,
    compile_api,
    extracts,
    labelled,
    local_saved_by,
    parse,
    shown_effect,
    site_of,
)
from agentos.runner.siteapis.store import Draft, DraftStore
from agentos.runner.util import Clock, clip_text, host_in

log = logging.getLogger("agentos.runner")

SERVICE_NAME = "site_apis"
ACTIVATE, LEAVE, REVIEW = "Activate", "Leave it", "I will review it"
TRY_WAIT_S = 60.0
JSON_SETTLE_S = 0.5  # after the outline, for what the page's own code is still fetching
CARD_CHARS = 1900
DESCRIBED = {"click": "click", "press": "press a key", "type": "type", "fill": "fill in", "select": "pick an option",
             "wait_for": "wait for", "scroll_to": "scroll to", "pause": "pause", "readback": "read back",
             "branch": "branch", "for_each": "repeat for each item", "assert_identity": "check who is signed in"}
By = Principal | Mapping[str, Any] | None


class ApiPaused(ConnectorBroken):
    """A person paused the API: its runs wait, and it answers `paused`, until a person activates it."""
    code = "paused"


def site_words(key: str | None) -> str:
    """A site as a person reads it: "Instagram", never its key ("instagram") or a surface's name."""
    found = known_site(key)
    return found.name if found is not None else (key or "another site").replace("_", " ").replace("-", " ").title()


def _who(by: By) -> dict[str, Any]:
    p = by.model_dump() if isinstance(by, Principal) else dict(by or {})
    return {"kind": p.get("kind") or "agent", "id": p.get("id"), "owner": p.get("owner", "owner")}


def is_person(by: By) -> bool:
    return _who(by)["kind"] == "human" and bool(_who(by)["id"])


def digest_of(definition: Mapping[str, Any]) -> str:
    blob = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def bumped(version: str) -> str:
    major, minor, patch = version_tuple(version)
    return f"{major}.{minor}.{patch + 1}"


def _without_sensitive(steps: list[dict[str, Any]], where: str, dropped: list[str]) -> list[dict[str, Any]]:
    out = []
    for i, step in enumerate(steps):
        if step.get("type") in ("type", "fill") and _sensitive({"locator": step.get("locator")}):
            dropped.append(f"{where}.{i}: typing into a sensitive field; use the vault at login instead")
            continue
        for key in ("then", "else", "steps"):
            if isinstance(step.get(key), list):
                step = {**step, key: _without_sensitive(step[key], f"{where}.{i}.{key}", dropped)}
        out.append(step)
    return out


def cleaned(doc: dict[str, Any], *, person: bool) -> tuple[dict[str, Any], list[str]]:
    """A definition as a draft may hold it: a step that types into a sensitive field goes, as the
    recorder drops it, and what an agent wrote keeps every page text field untrusted, so no later
    vouch for its input steps unmarks one."""
    doc = json.loads(json.dumps(doc))
    dropped: list[str] = []
    for name, ep in doc["endpoints"].items():
        ep["steps"] = _without_sensitive(ep["steps"], f"{name}.steps", dropped)
        for _, step in walk(ep["steps"]):
            if not person and step.get("type") in READS:
                step["fields"] = {k: labelled(v, person=False) for k, v in step["fields"].items()}
    return doc, dropped


# ---- a card: everything that runs, and nothing an agent wrote to be read ----


def _said(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _locator_words(loc: Mapping[str, Any]) -> str:
    rest = {k: v for k, v in loc.items() if k != "label" and v is not None}
    words = [str(rest.pop("role", "element"))]
    if "name" in rest:
        words.append(repr(rest.pop("name")))
    if "css" in rest:
        words.append(f"css {rest.pop('css')}")
    if "nth" in rest:
        nth = rest.pop("nth")
        words.append(f"number {nth + 1}" if isinstance(nth, int) and not isinstance(nth, bool) else f"#{nth}")
    within = rest.pop("within", None)
    words += [f"{k} {_said(v)}" for k, v in rest.items()]
    return " ".join(words + ([f"in {_locator_words(within)}"] if within else []))


def _field_words(name: str, field: Mapping[str, Any]) -> str:
    """A field in words (QA 2026-11-10, tester B 10: `max_chars 40 read "text" untrusted true` read as
    code). The keys it knows get words; any other is still shown as written, so nothing is hidden."""
    rest = {k: v for k, v in field.items() if v is not None}
    loc = rest.pop("locator", None)
    words = [f"{name}:"]
    read = rest.pop("read", None)
    if read is not None:
        words.append(("the text" if read == "text" else f"the {read}") + (" of" if loc else ""))
    if loc:
        words.append(_locator_words(loc))
    if "capture" in rest:
        words.append(f"the part matching {rest.pop('capture')}")
    if "pattern" in rest:
        words.append(f"which must match {rest.pop('pattern')}")
    if isinstance(rest.get("max_chars"), int):
        words.append(f"up to {rest.pop('max_chars')} characters")
    if rest.get("untrusted") is True:
        rest.pop("untrusted")
        words.append("treated as untrusted")
    return " ".join(words + [f"{k} {_said(v)}" for k, v in rest.items()])


_PACE_WORDS = {"per_hour": "{} runs an hour", "per_day": "{} a day", "per_week": "{} a week",
               "min_gap_minutes": "{} minutes apart at least", "max_items": "{} items a run"}


def _pace_words(pace: Mapping[str, Any]) -> str:
    """`{"per_hour": 60}` as "at most 60 runs an hour"; a key without words is shown as written."""
    rest = dict(pace)
    words = [_PACE_WORDS[k].format(rest.pop(k)) for k in list(_PACE_WORDS) if rest.get(k) is not None]
    hours = rest.pop("active_hours", None)
    if hours is True:
        words.append("only in the account's active hours")
    elif hours is False:
        words.append("at any hour")
    words += [f"{k} {_said(v)}" for k, v in rest.items()]
    return "at most " + ", ".join(words) if words else "no limit of its own"


def _step_words(step: Mapping[str, Any]) -> str:
    """A step as the node says it. What has no words of its own is shown as written, so that a field
    this has never heard of still reaches the card."""
    rest = {k: v for k, v in step.items() if k not in ("type", "label") and v is not None}
    kind = str(step.get("type"))
    if kind == "navigate":
        words = [f"open {rest.pop('url', None)}"]
    elif kind == "extract":
        fields = rest.pop("fields", None) or {}
        words = [f"read up to {rest.pop('max_items', None)} records of {_locator_words(rest.pop('items', None) or {})}",
                 f"as {rest.pop('save_as', None)}", f"({', '.join(_field_words(k, v) for k, v in fields.items())})"]
        pick = rest.pop("pick", None)
        if pick == "outermost":
            words.append("keeping only the outermost matches")
        elif pick == "card":
            words.append("each match standing for the card around it")
        elif pick is not None:
            words.append(f"pick {_said(pick)}")
        if rest.get("drop_empty") is True:
            rest.pop("drop_empty")
            words.append("skipping empty records")
    elif kind == "read_json":
        fields = rest.pop("fields", None) or {}
        where = f"{rest.pop('items', None)} of the JSON the page itself fetches from {rest.pop('url', None)}"
        words = [f"read up to {rest.pop('max_items', None)} records at {where}", f"as {rest.pop('save_as', None)}",
                 f"({', '.join(_field_words(k, v) for k, v in fields.items())})"]
    else:
        words = [DESCRIBED.get(kind, kind)]
        if isinstance(rest.get("locator"), dict):
            words.append(_locator_words(rest.pop("locator")))
    for key in ("then", "else", "steps"):
        if isinstance(rest.get(key), list):
            words.append(f"{key} ({_steps_words(rest.pop(key))})")
    return " ".join(words + [f"{k} {_said(v)}" for k, v in rest.items()])


def _steps_words(steps: list[dict[str, Any]]) -> str:
    return "; ".join(_step_words(s) for s in steps)


def _names(loc: Mapping[str, Any] | None) -> list[str]:
    """The accessible names in a locator and its anchors: what a page or an agent chose to say."""
    out: list[str] = []
    while isinstance(loc, Mapping):
        out += [str(loc["name"])] if loc.get("name") is not None else []
        out += [str(v) for v in (loc.get("names") or {}).values()]
        loc = loc.get("within")
    return out


def _prose(ep: Endpoint) -> bool:
    """Would showing what this endpoint runs put words on a card that are not a plain label?"""
    locators = [loc for steps in (ep.steps, ep.discard) for _, step in walk(steps) for _, loc in step_locators(step)]
    locators += [c.locator for c in (ep.commit.preflight if ep.commit else []) if c.locator]
    return not all(discovery.plain_label(name) for loc in locators for name in _names(loc))


def input_endpoints(api: SiteApi) -> list[str]:
    return sorted(name for name, ep in api.endpoints.items() if any(is_input(s) for _, s in walk(ep.steps)))


def _same_site(page: str | None) -> Callable[[str], bool]:
    """https URLs of the page's own registrable domain: an API often sits on a sibling host."""
    from agentos.vault.domains import registrable_domain

    host = urlsplit(page or "").hostname or ""
    site = registrable_domain(host) or host

    def wanted(url: str) -> bool:
        try:
            parts = urlsplit(url)
            return parts.scheme == "https" and host_in(parts.hostname or "", site)
        except ValueError:
            return False

    return wanted


@dataclass(slots=True)
class TryResult:
    """What a try reports (site_api_try, POST /api/site-apis/{api}/try). `problem` is set only when the
    run ended without a result: what a caller would have been told (`problems.from_run`), as a dict."""
    run: Any
    problems: list[str]
    output: dict[str, Any] | None
    effect: str
    problem: dict[str, Any] | None = None

    def ms(self) -> int | None:
        started, finished = getattr(self.run, "started_at", None), getattr(self.run, "finished_at", None)
        return int((finished - started).total_seconds() * 1000) if started and finished else None

    def public(self) -> dict[str, Any]:
        out = self.output or {}
        return {"run": self.run.id, "status": str(self.run.status), "effect": self.effect, "data": out.get("data"),
                "untrusted_fields": out.get("untrusted_fields", []), "private_fields": out.get("private_fields", []),
                "truncated": bool(out.get("truncated")), "problems": self.problems, "ms": self.ms(),
                "problem": self.problem}


def _field_kind(spec: Mapping[str, Any]) -> str:
    """A field's kind from what the definition says of it: a link, a date, a count or text."""
    if spec.get("format") == "uri":
        return "link"
    if spec.get("format") == "date-time":
        return "date"
    return "count" if spec.get("type") in ("number", "integer") else "text"


def _fields_of(ep: Endpoint) -> list[dict[str, str]]:
    """The declared fields an endpoint returns, by name, as the steps name them."""
    seen: dict[str, Mapping[str, Any]] = {}
    for step in extracts(ep.steps).values():
        for name, spec in (step.get("fields") or {}).items():
            seen.setdefault(name, spec)
    return [{"key": name, "kind": _field_kind(spec)} for name, spec in sorted(seen.items())]


def _inputs_of(ep: Endpoint) -> list[dict[str, Any]]:
    """The params a caller may give, from the input schema the endpoint declares."""
    required = set(ep.input.get("required") or [])
    out: list[dict[str, Any]] = []
    for key, spec in sorted((ep.input.get("properties") or {}).items()):
        enum = spec.get("enum")
        kind = "number" if spec.get("type") in ("number", "integer") else "date" if spec.get("format") in ("date", "date-time") else "text"
        item: dict[str, Any] = {"key": key, "label": str(spec.get("title") or key.replace("_", " ")),
                                "type": "choice" if enum else kind, "required": key in required}
        if enum:
            item["options"] = [{"value": str(value), "label": str(value)} for value in enum]
        if "default" in spec:
            item["default"] = str(spec["default"])
        if spec.get("description"):
            item["hint"] = str(spec["description"])
        out.append(item)
    return out


# how bad a surface's health is, for the one line a whole API shows
_WORSE = {"broken": 4, "held": 3, "degraded": 2, "healthy": 1, "unknown": 0}


class SiteApiService:
    def __init__(self, runner: Any, *, runtime: Any = None, clock: Clock | None = None,
                 sites: Mapping[str, tuple[str, ...]] | None = None):
        self.runner, self.runtime, self.sites = runner, runtime, sites
        self.flows = runner.flows
        self.clock = clock or runner.clock
        self.drafts = DraftStore(runner.store)
        if runner.scheduler is not None:
            runner.scheduler.tick_hooks.append(self.tick)

    def _svc(self, name: str) -> Any:
        return self.runtime.get(name) if self.runtime is not None else None

    # ---- reading ----
    def state_of(self, api: str) -> str | None:
        d = self.drafts.get(api)
        return d.state if d is not None else None

    def _draft(self, api: str) -> Draft:
        d = self.drafts.get(api)
        if d is None:
            raise NotFound(f"no site API {api!r} on this node")
        return d

    def _flows_of(self, api: str) -> list[FlowVersion]:
        return [fv for fv in self.flows.list(f"{CONNECTOR_PREFIX}{api}") if fv.definition.connector]

    def row(self, d: Draft) -> dict[str, Any]:
        """The node's own account of an API, what `show`, the list and the panel read."""
        api = parse(d.definition)
        site = site_of(api.domain, self.sites)
        return {
            "api": d.api, "state": d.state, "title": api.title, "domain": api.domain, "version": api.version,
            "site": site or WEB_SITE, "acts_as": "account" if site else "lane", "surfaces": list(api.surfaces),
            "by_person": d.person, "author": d.author.get("kind"), "digest": d.digest,
            "edited": bool(d.installed and d.installed.get("digest") != d.digest),
            "installed": d.installed, "proposal": self._proposal_of(d), "updated_at": d.updated_at.isoformat(),
            "endpoints": {name: {"title": ep.title, "description": ep.description,
                                 "effect": shown_effect(ep, person=d.person), "declared_effect": ep.effect,
                                 "cache_s": ep.http_cache_s, "pace_per_hour": (ep.pace.per_hour if ep.pace and ep.pace.per_hour else DEFAULT_PACE["per_hour"]),
                                 "expect_min": next(iter(ep.expect.min_items.values()), None),
                                 "fields": _fields_of(ep), "input": _inputs_of(ep),
                                 "tool": f"{d.api}_{name}".replace("-", "_")} for name, ep in api.endpoints.items()},
            "health": self.health_of(d),
        }

    def _proposal_of(self, d: Draft) -> dict[str, Any] | None:
        """The card an agent's proposal made: who asked and when, and what each endpoint will do, in the node's words."""
        if d.proposal is None:
            return None
        by = at = None
        approvals = self._svc("approvals")
        try:
            asked = approvals.get(d.proposal.get("approval_id")) if approvals is not None else None
        except AgentosError:
            asked = None
        if asked is not None:
            by = (asked.requester or {}).get("id")
            at = datetime.fromtimestamp(asked.created_at, UTC).isoformat()
        steps = [f"{name}: {_steps_words(ep.steps)}" for name, ep in parse(d.definition).endpoints.items()]
        return {**d.proposal, "by": by, "at": at, "steps": steps}

    def apis(self, allowed: Callable[[str], bool] | None = None) -> list[dict[str, Any]]:
        rows = [self.row(d) for d in self.drafts.list()]
        if allowed is None:
            return rows
        return [r for r in rows if not r["surfaces"] or any(allowed(s) for s in r["surfaces"])]

    def get(self, api: str) -> dict[str, Any]:
        d = self._draft(api)
        return {**self.row(d), "definition": d.definition}

    # ---- drafts ----
    def _compile(self, api: SiteApi | Mapping[str, Any], *, person: bool, languages: list[str] | None = None) -> CompiledConnector:
        return compile_api(api, person=person, sites=self.sites, languages=languages)

    def _key_free(self, api: str) -> None:
        """Not a key the catalog installed: a site API never replaces a reviewed connector."""
        for fv in self._flows_of(api):
            if (fv.saved_by or {}).get("kind") != "local":
                raise PolicyDenied(f"{api} is an installed connector here; pick another key")

    def save(self, definition: Mapping[str, Any], *, by: By, bump: bool = False) -> dict[str, Any]:
        """Save a draft. It never changes what is live: only `activate` does. `bump`: raise the patch
        version when the content changed and the version did not (the CLI and the panel do)."""
        who, person = _who(by), is_person(by)
        api = parse(definition)
        self._key_free(api.api)
        doc, dropped = cleaned(api.public(), person=person)
        api = parse(doc)
        self._check_params(api)
        old = self.drafts.get(api.api)
        if not person and old is not None and (old.person or old.state in (State.ACTIVE, State.PAUSED)):
            raise PolicyDenied(f"a person looks after {api.api}: an agent cannot change it. Propose under another key.")
        changed = old is not None and digest_of({**doc, "version": old.definition["version"]}) != digest_of(old.definition)
        if old is not None and changed and version_tuple(api.version) <= version_tuple(old.definition["version"]):
            if not bump:
                raise InvalidRequest(f"{api.api} changed: give it a version above {old.definition['version']}")
            doc["version"] = bumped(old.definition["version"])
        now = self.clock.now()
        same = old is not None and digest_of(doc) == old.digest
        state = old.state if old is not None and old.state in (State.ACTIVE, State.PAUSED) else State.DRAFT
        if same and old is not None and old.state == State.PROPOSED:
            state = State.PROPOSED
        d = Draft(api=api.api, state=state, definition=doc, digest=digest_of(doc), person=person or (same and old is not None and old.person),
                  author=who, proposal=old.proposal if same and old is not None else None,
                  installed=old.installed if old is not None else None,
                  created_at=old.created_at if old is not None else now, updated_at=now)
        self.drafts.put(d)
        return {**self.row(d), "problems": self._problems_of(api, person=d.person), **({"dropped": dropped} if dropped else {})}

    @staticmethod
    def _check_params(api: SiteApi) -> None:
        """Where a param can reach a page is refused here, not only when a draft compiles, because
        it is what a draft is for: other problems a person can fix after saving."""
        for name, ep in api.endpoints.items():
            for steps in (ep.steps, ep.discard):
                for _, step in walk(steps):
                    fields = step.get("fields")
                    if fields is not None and not isinstance(fields, dict):  # refused in words, before anything reads it
                        raise ConnectorInvalid(f"{api.api}: endpoints.{name}: a step's fields name each field and what it reads, "
                                               "so they are a map, not a list.")
        issues = [Issue("error", f"endpoints.{name}.{where}", why) for name, ep in api.endpoints.items()
                  for where, why in param_problems(ep)]
        if issues:
            raise ConnectorInvalid(f"{api.api}: {issues[0].where}: {issues[0].message}", issues)

    def _problems_of(self, api: SiteApi, *, person: bool) -> list[str]:
        """What stops this draft compiling, said at once, so a person sees it before a try."""
        try:
            self._compile(api, person=person)
        except ConnectorInvalid as e:
            return [str(i) for i in e.issues if i.level == "error"][:12] or [e.message]
        return []

    def from_recording(self, recording: Mapping[str, Any], *, api: str, endpoint: str, by: By, surfaces: list[str] | None = None,
                       candidate: Mapping[str, Any] | None = None, title: str | None = None, vouch: bool = False) -> dict[str, Any]:
        """A draft flow from flow_record_stop as one endpoint of `api`. Typed values and field names the
        recorder kept as examples stay out: the definition reaches an OpenAPI document. `candidate`: a
        discover candidate on the page the recording ends at, read as the endpoint's output. `vouch`: a
        person says the recorded input steps publish nothing (a search box, a date filter)."""
        steps = [dict(s) for s in recording.get("steps") or []]
        if vouch and is_person(by):
            for s in steps:
                if is_input(s):
                    s["publishes"] = False
        first = next((s.get("url") for s in steps if s.get("type") == "navigate"), None)
        host = urlsplit(first).hostname if first else None
        if not host:
            raise InvalidRequest("the recording has no page to start from: it needs a navigate step")
        params = recording.get("params") or {}
        props = {k: {key: v for key, v in (p or {}).items() if key in ("type", "enum", "minimum", "maximum", "pattern")}
                 for k, p in (params.get("properties") or {}).items()}
        for p in props.values():
            if p.get("type") == "string" and not p.get("enum") and not p.get("pattern"):
                p["maxLength"] = 200
        ep: dict[str, Any] = {"title": title or endpoint.replace("_", " ").capitalize(), "steps": steps,  # (see save)
                              "input": {"type": "object", "properties": props, "required": list(params.get("required") or [])},
                              "effect": Effect.DRAFT if any(is_input(s) for s in steps) else Effect.READ}
        if candidate is not None:
            step = discovery.extract_step(dict(candidate))
            ep["steps"] = [*steps, step]
            ep["outputs"] = {step["save_as"]: step["save_as"]}
            ep["expect"] = {"min_items": {step["save_as"]: 1}}
        old = self.drafts.get(api)
        doc: dict[str, Any] = dict(old.definition) if old is not None else {"api": api, "domain": host, "version": "1.0.0",
                                                                            "surfaces": []}
        doc["endpoints"] = {**doc.get("endpoints", {}), endpoint: ep}
        if surfaces:
            doc["surfaces"] = list(surfaces)
        return self.save(doc, by=by, bump=True)

    # ---- discovering ----
    async def discover(self, on: str, url: str | None = None) -> dict[str, Any]:
        """Candidates from the page in the lane behind `on`. A challenge stops it at once: the outline
        is not read, and the lane's own holds apply. With a `url` the page is loaded here, so what its
        own code fetches as JSON from its own site is listed too, by URL path and shape only."""
        lane = self.lane_of(on)
        target = self._page(url) if url else None
        async with self.runner.lane_session(lane) as driver:
            read = getattr(driver, "outline", None)
            if not callable(read):
                raise ServiceUnavailable("this node's flow driver cannot read an outline")
            listen = getattr(driver, "watch_json", None)
            watch = listen(_same_site(target), keep=discovery.MAX_JSON, only_json=True) if target and callable(listen) else None
            try:
                if target:
                    await driver.navigate(target)
                signal = await driver.signal()
                if signal not in QUIET:
                    return {"on": on, "signal": str(signal), "candidates": [],
                            "note": "The lane stopped on a challenge. Nothing was read; a person takes it from here."}
                here = await driver.current_url()
                text = await read()
                if watch is None:
                    json_found = []
                else:
                    await self.clock.sleep(JSON_SETTLE_S)
                    json_found = discovery.json_candidates(watch.responses)
            finally:
                if watch is not None:
                    watch.close()
        return {"on": on, "signal": str(signal), **discovery.discover(text, here), **({"json": json_found} if json_found else {})}

    @staticmethod
    def _page(url: str) -> str:
        """A web page on https, by the rule every caller's navigation goes by: never a file on the node."""
        from agentos.node.tools import normalize_url

        target = normalize_url(url)
        if not target.startswith("https://"):
            raise InvalidRequest("a site API reads a page on https")
        return target

    def lane_of(self, on: str) -> str:
        surface = self.runner.surfaces.find(on) if self.runner.surfaces is not None else None
        return surface.lane if surface is not None else on

    # ---- trying ----
    def _trial_target(self, fv: FlowVersion, on: str, params: Mapping[str, Any], site: str) -> Target:
        """The lane and params of a try. A surface that exists binds as usual, less the API's list of
        surfaces (it may not be live). A web API on a lane nothing names yet runs on the lane's own
        session; a site agentos knows needs its surface, because that pins the account."""
        surfaces = self.runner.surfaces
        surface = surfaces.find(on)
        if surface is None:
            if site != WEB_SITE:
                raise InvalidRequest(f"{on} is not a surface here: a site agentos knows pins its account, so bind one first")
            if not LANE_ID_RE.match(on):
                raise InvalidRequest(f"{on!r} is not a surface or a lane, like 'harbor/supplier'")
            surface = Surface(id=on, site=WEB_SITE, lane=on, acts_as=ActsAs(kind="lane"))
        if surface.site != site:
            raise InvalidRequest(f"{on} is on {site_words(surface.site)}, and this API is for {site_words(site)}.")
        given = sorted(set(params or {}) & set(BINDING_PARAMS))
        if given:
            raise PolicyDenied(f"{', '.join(given)} come from the surface's binding, never from the caller")
        binding = Binding.from_surface(surface)
        full = self.flows.validate_params(fv, {**dict(params or {}), **binding.params()})
        assert binding.lane is not None
        return Target(surface=surface, binding=binding, lane=binding.lane, params=full)

    def prepare_try(self, api_key: str, endpoint: str, params: Mapping[str, Any] | None, *, by: By,
                    on: str | None = None) -> tuple[FlowVersion, Target, str]:
        """(the staged version, where it runs, its effect), or a refusal: a try never runs a commit
        step, a draft endpoint is a person's to try, and so is a paused API, to test a fix."""
        d = self._draft(api_key)
        if d.state == State.PAUSED and not is_person(by):
            raise ApiPaused(f"a person paused {api_key}; an agent's try waits until they activate it")
        api = parse(d.definition)
        ep = api.endpoints.get(endpoint)
        if ep is None:
            raise NotFound(f"{api_key} has no endpoint {endpoint!r}")
        person = is_person(by)
        effect = shown_effect(ep, person=d.person)
        if effect == Effect.COMMIT:
            why = "" if d.person else ": a person must vouch for its input steps first"
            raise PolicyDenied(f"{api_key}.{endpoint} counts as commit, and a try never runs a commit step{why}")
        if effect == Effect.DRAFT and not person:
            raise PolicyDenied(f"{api_key}.{endpoint} has input steps: only a person tries a draft endpoint")
        target = on or next(iter(api.surfaces), None)
        if not target:
            raise InvalidRequest(f"{api_key} names no surface: pass on, or add one to its surfaces")
        surface = self.runner.surfaces.find(target)
        compiled = self._compile(api, person=d.person, languages=[surface.account_language] if surface else None)
        op = compiled.operations[endpoint]
        if op.flow is None:
            raise InvalidRequest(f"this node cannot run {api_key}.{endpoint} yet: it needs {', '.join(op.pending)}")
        who = _who(by)
        fv = self.flows.stage(op.flow, by=local_saved_by(api_key, {"id": who["id"], "owner": who["owner"]}))
        if fv.effect == Effect.COMMIT:
            raise PolicyDenied(f"{api_key}.{endpoint} counts as commit, and a try never runs a commit step")
        return fv, self._trial_target(fv, target, params or {}, str((fv.definition.connector or {}).get("site"))), effect

    async def run_try(self, fv: FlowVersion, target: Target, effect: str, *, by: By, grant_id: str | None = None,
                      wait_s: float = TRY_WAIT_S) -> TryResult:
        """One run on the lane. It counts against pace as any run of this endpoint does."""
        run, _ = self.runner.submit(flow_id=fv.id, version=fv.version, params=target.caller_params, surface=target.surface.id,
                                    target=target, principal=_who(by), source=TRY_SOURCE, authorized=True, grant_id=grant_id)
        run = await self.runner.wait(run.id, min(float(wait_s), TRY_WAIT_S))
        problem = None
        if run.terminal and run.status not in SUCCEEDED:
            from agentos.runner.siteapis.problems import from_run  # problems imports this module
            problem = from_run(run, now=self.clock.now()).body()
        return TryResult(run=run, problems=self._problems(fv, run), output=run.output, effect=effect, problem=problem)

    async def try_endpoint(self, api: str, endpoint: str, params: Mapping[str, Any] | None = None, *, by: By,
                           on: str | None = None, grant_id: str | None = None, wait_s: float = TRY_WAIT_S) -> dict[str, Any]:
        fv, target, effect = self.prepare_try(api, endpoint, params, by=by, on=on)
        return (await self.run_try(fv, target, effect, by=by, grant_id=grant_id, wait_s=wait_s)).public()

    async def read_now(self, api_key: str, *, by: By, grant_id: str | None = None) -> dict[str, Any]:
        """Starts a read of each endpoint that needs no input, on its surface, and answers at once. The runs
        finish on the lane and show in its history and health; each counts against pace, as any read does."""
        d = self._draft(api_key)
        api = parse(d.definition)
        started = []
        for name, ep in api.endpoints.items():
            if shown_effect(ep, person=is_person(by)) != Effect.READ or set(ep.input.get("required") or []):
                continue
            await self.try_endpoint(api_key, name, {}, by=by, grant_id=grant_id, wait_s=0)
            started.append(name)
        if not started:
            raise InvalidRequest(f"{api_key} has no endpoint that reads without input, so there is nothing to read now")
        return {**self.row(d), "started": started}

    @staticmethod
    def _problems(fv: FlowVersion, run: Any) -> list[str]:
        problems: list[str] = []
        if run.status not in (RunStatus.SUCCEEDED_VERIFIED, RunStatus.SUCCEEDED_UNVERIFIED):
            err = run.error or {}
            problems.append(clip_text(str(err.get("code") or run.status), 80) + ": " + clip_text(str(err.get("message") or ""), 240)
                            if run.terminal else "still running: read it with run_status")
        data = (run.output or {}).get("data") or {}
        http = (fv.definition.connector or {}).get("http") or {}
        for name, least in ((http.get("expect") or {}).get("min_items") or {}).items():
            have = data.get(name)
            if run.output is not None and isinstance(have, list) and len(have) < least:
                problems.append(f"{name}: {len(have)} rows, and this endpoint expects at least {least}")
        return problems

    # ---- proposing ----
    def _card(self, d: Draft) -> str:
        """Everything that will run, written from the definition in the node's words: Activate
        activates exactly this, so nothing is cut and a draft that cannot be shown is refused. The
        agent's title and description stay off, and so does a name that is not a plain label."""
        api = parse(d.definition)
        site = site_of(api.domain, self.sites)
        how = f"as the {site} account" if site else "through the lane session"
        where = ", ".join(api.surfaces) or "no surface yet"
        parts = [f"An agent proposes a site API: {api.api} {api.version}, on {api.domain} ({how}), on {where}."]
        for name, ep in api.endpoints.items():
            if _prose(ep):
                raise InvalidRequest(f"{api.api}.{name}: a locator's name is not a plain label, so it cannot go on a card. "
                                     "Find the element by its role and position, or ask a person to save this draft")
            fields = sorted({f for s in extracts(ep.steps).values() for f in s.get("fields") or {}})
            params = [f"{k} {', '.join(map(str, p['enum']))}" if p.get("enum") else k
                      for k, p in sorted(ep.input.get("properties", {}).items())]
            pace = ep.pace.model_dump(exclude_none=True) if ep.pace else DEFAULT_PACE
            parts.append(f"{name} ({shown_effect(ep, person=d.person)}): {_steps_words(ep.steps)}. "
                         f"Returns {', '.join(fields) or 'nothing'}" + (f". Takes {'; '.join(params)}" if params else "")
                         + f". Keeps an answer for {ep.http_cache_s} seconds. Runs {_pace_words(pace)}."
                         + (f" Hands back {', '.join(k if k == v else f'{k} (from {v})' for k, v in ep.outputs.items())}."
                            if ep.outputs else "")
                         + (" Expects at least " + ", ".join(f"{n} {k}" for k, n in ep.expect.min_items.items())
                            + " (fewer means the site changed)." if ep.expect.min_items else "")
                         + (f" Discard: {_steps_words(ep.discard)}." if ep.discard else "")
                         + (f" Commit {_said(ep.commit.model_dump(exclude_none=True))}." if ep.commit else "")
                         + (f" not_to_people {_said(ep.not_to_people)}." if ep.not_to_people is not None else ""))
        needs = input_endpoints(api)
        if needs and not d.person:
            parts.append(f"{', '.join(needs)} has input steps an agent wrote, so it counts as commit until you vouch: "
                         f"agentos site-api activate {api.api} --vouch")
        text = " ".join(parts)
        if len(text) > CARD_CHARS:
            raise InvalidRequest(f"{api.api} is too long to show on one card, and a person activates only what they can "
                                 "read. Split it up: propose a few endpoints at a time, under another key")
        return text

    def propose(self, api_key: str, *, by: By, grant_id: str | None = None) -> dict[str, Any]:
        """A card in Needs you, for a person to activate exactly this content."""
        approvals = self._svc("approvals")
        if approvals is None:
            raise ServiceUnavailable("asking a person needs the policy service on this node")
        d = self._draft(api_key)
        if d.state in (State.ACTIVE, State.PAUSED) and d.digest == (d.installed or {}).get("digest"):
            raise InvalidRequest(f"{api_key} is already {d.state}, with this content")
        api = parse(d.definition)
        if not api.surfaces:
            raise InvalidRequest(f"{api_key} names no surface: a person needs to know where it runs")
        self._compile(api, person=True)  # refused here, not on the person's card
        options = [REVIEW, LEAVE] if input_endpoints(api) and not d.person else [ACTIVATE, LEAVE]
        who = _who(by)
        requester = Principal(kind=who["kind"], id=who["id"] or "agent", owner=who["owner"])
        self.runner.check_pending(approvals, grant_id)
        ap = approvals.create_question(question=self._card(d), options=options, requester=requester, grant_id=grant_id,
                                       grant_path=self.runner._grant_path(grant_id) if grant_id else None, workspace=None)
        d.proposal = {"approval_id": ap.id, "digest": d.digest}
        if d.state == State.DRAFT:
            d.state = State.PROPOSED
        d.updated_at = self.clock.now()
        self.drafts.put(d)
        return {"approval_id": ap.id, "status": "pending", "state": d.state, "digest": d.digest}

    def check_proposals(self) -> int:
        """A person answered a card: Activate activates exactly the content on the card."""
        approvals = self._svc("approvals")
        if approvals is None:
            return 0
        done = 0
        for d in self.drafts.list():
            if not d.proposal:
                continue
            try:
                ap = approvals.get(d.proposal["approval_id"])
            except NotFound:
                ap = None
            if ap is not None and ap.state == "pending":
                continue
            proposal, d.proposal = d.proposal, None
            d.state = State.DRAFT if d.state == State.PROPOSED else d.state
            self.drafts.put(d)
            done += 1
            if ap is None or (ap.answer or "") != ACTIVATE or proposal["digest"] != d.digest:
                continue
            if not answered_in_person(ap):
                self.runner._notify(d.api, "Only the owner activates a site API. It stays a draft.", data={"site_api": d.api})
                continue
            try:
                self.activate(d.api, by={"kind": "human", "id": "owner", "owner": "owner"}, digest=proposal["digest"])
            except (ConnectorInvalid, InvalidRequest, PolicyDenied, NotFound) as e:
                self.runner._notify(d.api, f"It could not be activated: {e.message}", data={"site_api": d.api})
        return done

    def tick(self, *_: Any) -> None:
        self.check_proposals()

    # ---- activating ----
    def _bind(self, api: SiteApi, by: dict[str, Any]) -> list[str]:
        """The account languages of the API's surfaces. A web surface that does not exist yet is made
        here, on the lane of the same name; a site agentos knows is bound by hand, for its account."""
        surfaces = self.runner.surfaces
        site = site_of(api.domain, self.sites)
        languages: set[str] = set()
        for sid in api.surfaces:
            s = surfaces.find(sid)
            if s is None:
                if site:
                    raise InvalidRequest(f"The {site} account for {sid} is not set up on this computer yet, so it cannot be "
                                         "turned on. Set up that account, then try again.")
                s, _ = surfaces.put({"id": sid, "site": WEB_SITE, "lane": sid, "acts_as": {"kind": "lane"}}, by=by)
            if s.site != (site or WEB_SITE):
                raise InvalidRequest(f"{sid} is on {site_words(s.site)}, and this API is for {site_words(site or WEB_SITE)}.")
            languages.add(s.account_language)
        return sorted(languages)

    def activate(self, api_key: str, *, by: By, vouch: bool = False, digest: str | None = None) -> dict[str, Any]:
        """The owner makes this content the live API, or wakes a paused one. `vouch`: they have read
        an agent's input steps and vouch for them. `digest`: only if the content is still this."""
        who = _who(by)
        if not is_owner(who):
            raise PolicyDenied("only the owner activates a site API (the panel or the CLI)")
        d = self._draft(api_key)
        if digest is not None and digest != d.digest:
            raise PolicyDenied(f"{api_key} changed since it was proposed; read it again")
        self._key_free(api_key)
        api = parse(d.definition)
        if not api.surfaces:
            raise InvalidRequest(f"{api_key} names no surface: add one before activating it")
        needs = input_endpoints(api)
        if needs and not d.person and not vouch:
            raise PolicyDenied(f"an agent wrote the input steps of {', '.join(needs)}: they count as commit. Read them and "
                               "vouch for them (--vouch), or save the draft yourself")
        person = d.person or vouch
        languages = self._bind(api, who)
        compiled = self._compile(api, person=person, languages=languages)
        self._check_upgrade(d, api, compiled)
        self._refuse_if_watched(compiled)
        gone = sorted(set((d.installed or {}).get("endpoints") or {}) - set(api.endpoints))
        for name in gone:
            self._refuse_if_used(api_key, name)
        by_local = local_saved_by(api_key, who)
        saved = {}
        for name, op in sorted(compiled.operations.items()):
            assert op.flow is not None
            saved[name] = self.flows.save(op.flow, by=by_local)[0].version
        for name in gone:
            self.flows.delete(f"{CONNECTOR_PREFIX}{api_key}.{name}")
        d.state, d.person, d.proposal = State.ACTIVE, person, None
        d.installed = {"version": api.version, "digest": d.digest, "endpoints": saved}
        d.updated_at = self.clock.now()
        self.drafts.put(d)
        return self.row(d)

    def _check_upgrade(self, d: Draft, api: SiteApi, compiled: CompiledConnector) -> None:
        taken = self._tools_of_others(api.api)
        for name, op in compiled.operations.items():
            if op.flow is None:
                raise InvalidRequest(f"this node cannot run {api.api}.{name} yet: it needs {', '.join(op.pending)}")
            if not TOOL_NAME_RE.match(op.tool_name):
                raise InvalidRequest(f"{op.tool_name} is too long to be a tool: shorten the key or the endpoint")
            if op.tool_name in taken:
                raise InvalidRequest(f"{op.tool_name} is already the tool of {taken[op.tool_name]}: a hyphen in a key "
                                     "reads as an underscore. Change the key or the endpoint")
        have = d.installed
        if have and have["digest"] != d.digest and version_tuple(api.version) <= version_tuple(have["version"]):
            raise PolicyDenied(f"{api.api} {have['version']} is live: a changed API gets a higher version")

    def _refuse_if_watched(self, compiled: CompiledConnector) -> None:
        """A watch that is on goes on checking what goes live here. It must stay a read, because a check
        must never act on the site, and the key it tells rows apart by must stay shown."""
        workflows = self._svc("workflows") or getattr(self.runner, "workflows", None)
        if workflows is None:
            return
        problems = []
        for name, op in sorted(compiled.operations.items()):
            assert op.flow is not None
            effect = op.flow.effective_effect(owner_saved=True)[0]  # only the owner gets here
            private = ((op.flow.connector or {}).get("http") or {}).get("private_fields") or []
            for routine, t in workflows.watches_on(op.flow_id):
                if effect != Effect.READ:
                    problems.append(f'"{routine}" checks {name}, and this version makes it a {effect.value} endpoint, so '
                                    "each check would act on the site. Turn the routine off or have it watch something "
                                    "else first.")
                elif f"{t.list}[].{t.key}" in private:
                    problems.append(f'"{routine}" tells the rows of {name} apart by {t.key}, and this version makes that '
                                    "field private, but a key is shown in what changed. Choose another key first.")
        if problems:
            raise InvalidRequest(" ".join(problems))

    def _tools_of_others(self, api_key: str) -> dict[str, str]:
        """Tool name -> operation of every connector installed here but this API: two with one name
        would drop one tool, the catalog's as easily as anyone's."""
        return {tool_name(fv): fv.id.removeprefix(CONNECTOR_PREFIX) for fv in self.flows.list(CONNECTOR_PREFIX.rstrip("."))
                if fv.definition.connector and fv.definition.connector.get("connector") != api_key}

    # ---- pausing and removing ----
    def _drop_cache(self, api: str) -> None:
        http = self._svc("site_http")
        if http is not None:
            http.api_dropped(api)

    def pause(self, api_key: str, *, by: By) -> dict[str, Any]:
        if not is_person(by):
            raise PolicyDenied("only a person pauses a site API")
        d = self._draft(api_key)
        if d.state not in (State.ACTIVE, State.PAUSED):
            raise InvalidRequest(f"{api_key} is {d.state}, not active")
        d.state, d.updated_at = State.PAUSED, self.clock.now()
        self.drafts.put(d)
        self._drop_cache(api_key)
        return self.row(d)

    def resume(self, api_key: str, *, by: By) -> dict[str, Any]:
        """Wakes a paused API with the content that was live when it paused. Waking is activating, so only
        the owner does it, and content changed since the pause is activated as new, never resumed."""
        d = self._draft(api_key)
        if d.state != State.PAUSED:
            raise InvalidRequest(f"{api_key} is {d.state}, not paused")
        if d.digest != (d.installed or {}).get("digest"):
            raise InvalidRequest(f"{api_key} changed since it was paused, so it is not resumed as it was. "
                                 "Activate the new content instead")
        return self.activate(api_key, by=by, digest=d.digest)

    def decline(self, api_key: str, *, by: By) -> dict[str, Any]:
        """A person says no to an agent's proposal. Its card is closed. A draft nobody has turned on is
        dropped; one that is live keeps its content and loses the proposal."""
        if not is_person(by):
            raise PolicyDenied("only a person declines a proposal")
        d = self._draft(api_key)
        if d.proposal is None:
            raise InvalidRequest(f"nobody has proposed {api_key}")
        self._close_card(d.proposal.get("approval_id"), by)
        if d.installed is None:
            self.drafts.delete(api_key)
            self._drop_cache(api_key)
            return {"declined": api_key, "removed": True}
        d.proposal = None
        if d.state == State.PROPOSED:
            d.state = State.ACTIVE
        d.updated_at = self.clock.now()
        self.drafts.put(d)
        return {**self.row(d), "removed": False}

    def _close_card(self, approval_id: str | None, by: By) -> None:
        approvals = self._svc("approvals")
        if approvals is None or not approval_id:
            return
        try:
            if approvals.get(approval_id).state == "pending":
                approvals.decide(approval_id, "deny", by=_who(by)["id"] or "owner", note="Not turned on.")
        except AgentosError:
            log.info("the card for %s was closed already", approval_id)

    def accept_empty(self, api_key: str, *, by: By) -> dict[str, Any]:
        """The owner says an empty page is the right answer: every minimum an endpoint expects drops to zero,
        and that content is activated. Changing what an endpoint promises is a widening, so only the owner."""
        if not is_owner(_who(by)):
            raise PolicyDenied("only the owner says an empty page is right")
        d = self._draft(api_key)
        if d.state == State.PAUSED:
            raise InvalidRequest(f"{api_key} is paused: resume it, then say an empty page is right")
        doc = json.loads(json.dumps(d.definition))
        changed = []
        for name, ep in (doc.get("endpoints") or {}).items():
            expect = (ep.get("expect") or {}).get("min_items") or {}
            if any(expect.values()):
                ep["expect"]["min_items"] = {key: 0 for key in expect}
                changed.append(name)
        if not changed:
            raise InvalidRequest(f"{api_key} expects no minimum number of rows, so there is nothing to accept")
        self.save(doc, by=by, bump=True)
        return self.activate(api_key, by=by)

    def health_of(self, d: Draft) -> dict[str, dict[str, Any]]:
        """Per endpoint, the worst health any surface reports on the node: what its last runs said."""
        health = getattr(self.runner, "health", None)
        if health is None:
            return {}
        names = set(parse(d.definition).endpoints)
        out: dict[str, dict[str, Any]] = {}
        for row in health.list():
            api, _, name = str(row.get("operation") or "").partition(".")
            if api != d.api or name not in names:
                continue
            if name not in out or _WORSE.get(row.get("state"), 0) > _WORSE.get(out[name].get("state"), 0):
                out[name] = row
        return out

    def _users(self, api_key: str, endpoint: str | None = None) -> list[str]:
        """What still starts this API: schedules, and workflows by any version they might run."""
        prefix = f"{CONNECTOR_PREFIX}{api_key}."
        wanted = f"{prefix}{endpoint}" if endpoint else prefix
        used = [f"schedule {s.id}" for s in self.runner.store.list_schedules()
                if (s.flow_id or "").startswith(wanted) and (endpoint is None or s.flow_id == wanted)]
        workflows = self._svc("workflows") or getattr(self.runner, "workflows", None)
        if workflows is not None:
            for state in workflows.store.states():
                for version in {state.head, state.live} - {None}:
                    wv = workflows.store.version(state.id, version)
                    if wv is not None and wanted in json.dumps(wv.definition.canonical()):
                        used.append(f"workflow {state.id}")
                        break
        return used

    def _refuse_if_used(self, api_key: str, endpoint: str | None = None) -> None:
        used = self._users(api_key, endpoint)
        if used:
            raise InvalidRequest(f"{', '.join(used)} use{'s' if len(used) == 1 else ''} {api_key}; remove {'it' if len(used) == 1 else 'them'} first")

    def remove(self, api_key: str, *, by: By) -> dict[str, Any]:
        """Its flows lose their route to running; its runs stay in history."""
        if not is_person(by):
            raise PolicyDenied("only a person removes a site API")
        d = self._draft(api_key)
        self._refuse_if_used(api_key)
        flows = sorted({fv.id for fv in self._flows_of(api_key)} |
                       {f"{CONNECTOR_PREFIX}{api_key}.{name}" for name in d.definition.get("endpoints") or {}})  # and what a try staged
        for flow_id in flows:
            self.flows.delete(flow_id)
            self.runner.store.delete_health(flow_id.removeprefix(CONNECTOR_PREFIX))
        self.drafts.delete(api_key)
        self._drop_cache(api_key)
        return {"removed": api_key, "state": d.state, "flows": flows}


def build(runner: Any, runtime: Any, *, sites: Mapping[str, tuple[str, ...]] | None = None) -> SiteApiService:
    service = SiteApiService(runner, runtime=runtime, sites=sites)
    runner.site_apis = service
    if runtime is not None:
        runtime.register(SERVICE_NAME, service)
    return service


