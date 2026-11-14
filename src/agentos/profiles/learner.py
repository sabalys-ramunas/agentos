"""Learning a site: an agent changes one profile field by hand once, and the node keeps how.

    learner = ProfileLearner(profiles_service)
    learner.start("acme/instagram", "bio", by=principal)       # recorder on, learn window open
    ... the agent logs in, opens the site's edit page, fills the field with the value, saves ...
    await learner.finish("acme/instagram", "bio", readback_locator=..., profile_url=..., by=principal)

`start` checks the account is attached to an approved profile, the field is one the site has and
the engine does not edit yet (or one to learn again), and the lane is signed into its pinned
account. It makes sure the account has a surface (who it acts as: the lane's own pinned account),
turns on the flow recorder for the lane and opens the lane guard's learn window
(agentos.node.egress.learn): one request whose body carries exactly the profile's value, from a
page under the site's reviewed edit paths, is let through without a card. The owner's consent is
the login they saved and the profile they saved; nothing else is released.

`finish` takes what the node saw, never what the agent says: the recorded steps, the one request
the window released (its host, path, method and operation name, and the page that sent it, which
must be the page the recording typed on and under the site's reviewed edit pages), and the value read back at
the locator the agent pointed at on the profile page (built by the node from a snapshot ref).
agentos.profiles.learn.generate turns that into two connector operations (`<field>` writes,
`<field>_read` reads), installed as `learned` (ConnectorService.install_learned), with a learned
proof rule (agentos.node.learned_rules). Then the node replays the read operation with no model in
the loop: when it returns exactly the value, the rule is marked verified and both operations get
a replay mark (agentos.runner.connectors.marks.mark_replay), so the sync runner
(agentos.profiles.sync) may use them from then on. Anything less leaves them unverified and says
why.

`name` is never learned: Facebook and Instagram lock a name for weeks after a change. Photos come
later (the recorder does not record uploads yet).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from agentos.core.errors import InvalidRequest, NotFound
from agentos.profiles import mapping

log = logging.getLogger("agentos.profiles")

NOT_LEARNED = ("name", "photo", "cover")
SESSION_TTL_S = 900.0
LESSONS_PER_LANE_PER_DAY = 3        # every start counts, failed ones too
LESSONS_PER_FIELD_PER_WEEK = 3
REPLAY_WAIT_S = 180.0
PRINCIPAL = {"kind": "flow", "id": "", "owner": "owner"}     # the runner gives it the owner's grant
SOURCE = "profiles"
OWNER = {"kind": "human", "id": "owner", "owner": "owner"}


@dataclass
class Session:
    surface: str
    field: str
    site: str
    lane: str
    value: str
    window_id: str
    started_at: float
    by: dict[str, Any] = field(default_factory=dict)
    grant_id: str | None = None
    lesson_id: int | None = None


def learnable_fields() -> list[str]:
    """`site field` pairs agentos may learn: those with a reviewed save request (agentos.node.sites)."""
    from agentos.node.sites import PROFILE_EDIT_ENDPOINTS

    return [f"{site} {field}" for site, fields in sorted(PROFILE_EDIT_ENDPOINTS.items()) for field in sorted(fields)]


def _who(by: Any) -> dict[str, Any]:
    if by is None:
        return {}
    p = by if isinstance(by, dict) else (by.model_dump() if hasattr(by, "model_dump") else dict(vars(by)))
    return {k: p.get(k) for k in ("kind", "id", "owner") if p.get(k) is not None}


def _norm(text: Any) -> str:
    return "\n".join(" ".join(line.split()) for line in str(text or "").strip().splitlines())


class ProfileLearner:
    def __init__(self, service: Any, *, clock: Any = time.time):
        self.svc = service
        self.runtime = service.runtime
        self.clock = clock
        self.sessions: dict[tuple[str, str], Session] = {}

    # ---------------------------------------------------------------- the node's parts
    def _need(self, name: str) -> Any:
        if self.runtime is None or self.runtime.get(name) is None:
            raise InvalidRequest(f"learning a site needs the node's {name} service; run it on the node (agentosd)")
        return self.runtime.get(name)

    def _home(self) -> Any:
        return self.svc.home

    def _connectors(self) -> Any:
        from agentos.runner.connectors.service import ConnectorService

        return ConnectorService(self._need("flows"))

    def _lane_of(self, surface: str) -> str:
        surfaces = self._need("surfaces")
        found = surfaces.find(surface)
        return found.lane if found is not None else surface

    def _pinned(self, lane: str, site: str) -> str:
        info = self._need("lanes").get(lane)
        account = getattr(getattr(info, "spec", None), "account", None)
        if account is None or not account.user_id or mapping.site_of(account.site) != site:
            raise InvalidRequest(f"lane {lane} has no pinned {site} account yet: sign it in once (login, or the "
                                 "panel's Log in), so agentos knows which account it is, then learn again")
        return str(account.user_id)

    def _account_id(self, site: str, lane: str, profile_url: str) -> str:
        """The id a learned operation acts as, from what the node or the owner knows, never from
        the agent alone: the lane's pinned id (the identity cookie's), or on a site whose profile
        pages carry the account's name (Instagram's username) the handle the owner gave the lane's
        account, which the profile URL the agent visited must name too."""
        import re

        from agentos.profiles.learn import SITE_PROFILES

        pinned = self._pinned(lane, site)
        acting = SITE_PROFILES[site].acting_as
        if acting.get("source") not in ("href", "url") or not acting.get("pattern"):
            return pinned
        account = self._need("lanes").get(lane).spec.account
        handle = str(getattr(account, "handle", None) or "").strip().lstrip("@")
        if not handle:
            raise InvalidRequest(f"lane {lane}'s {site} account has no handle: on {site} the learned steps open "
                                 "the account by its username, so the owner sets it on the lane first (the "
                                 "account's handle, like @acme.coffee)")
        m = re.search(acting["pattern"], profile_url)
        if m is None or m.group(1).casefold() != handle.casefold():
            raise InvalidRequest(f"{profile_url} is not the profile page of {site} account @{handle}, the one "
                                 f"lane {lane} is pinned to")
        return handle

    def _ensure_surface(self, surface: str, lane: str, site: str, account_id: str) -> str | None:
        """The account acts as its own login. Made for the owner when missing. Returns a problem
        when one already exists acting as another id (the learned steps would open the wrong page)."""
        surfaces = self._need("surfaces")
        found = surfaces.find(surface)
        if found is None:
            surfaces.put({"id": surface, "site": site, "lane": lane,
                          "acts_as": {"kind": "account", "user_id": account_id}}, by=OWNER)
            return None
        acts = getattr(found, "acts_as", None)
        have = getattr(acts, "user_id", None) or getattr(acts, "id", None)
        if have is not None and str(have).casefold() != account_id.casefold():
            return (f"surface {surface} acts as {have}, but the learned steps act as {account_id} (on {site} that "
                    "is the id in the profile URL). Bind the surface to that id (agentos connector bind) and learn again")
        return None

    # ---------------------------------------------------------------- what may be learned
    def learnable(self, surface: str, field_name: str) -> tuple[Any, str, str]:
        """(resolved, site, value) for a field that may be learned on this account, or raises."""
        from agentos.node.sites import profile_edit_endpoint
        from agentos.profiles.learn import SITE_PROFILES

        if field_name in NOT_LEARNED:
            raise InvalidRequest(f"{field_name} is not learned automatically: "
                                 + ("sites lock a name for weeks after a change, so a person changes it"
                                    if field_name == "name" else "photos come in a later version"))
        r = self.svc.resolved(surface)
        if r is None:
            raise NotFound(f"{surface} is not attached to an approved profile")
        if r.problems:
            raise InvalidRequest(f"{surface}: fix the profile first: {r.problems[0]}")
        if field_name not in r.values:
            raise InvalidRequest(f"{surface} has no value for {field_name} (it has {', '.join(r.values) or 'none'})")
        if r.site not in SITE_PROFILES or profile_edit_endpoint(r.site, field_name) is None:
            raise InvalidRequest(f"agentos cannot learn {r.site} {field_name} yet: only reviewed save requests are "
                                 f"learned ({', '.join(learnable_fields()) or 'none'})")
        value = r.values[field_name]
        if not isinstance(value, str):
            raise InvalidRequest(f"{field_name} is not text; only text fields are learned for now")
        return r, r.site, value

    def state(self, site: str, field_name: str) -> dict[str, Any] | None:
        """The learned operations for a site's field and whether the node verified them, or None."""
        from agentos.node.learned_rules import LearnedRules
        from agentos.profiles.learn import flow_ids
        from agentos.runner.connectors.compile import proof_hash
        from agentos.runner.connectors.marks import MarkLedger, operation_key

        flows = self.runtime.get("flows") if self.runtime is not None else None
        if flows is None:
            return None
        commit_id, read_id = flow_ids(site, field_name)
        ledger = MarkLedger(self._home())
        out: dict[str, Any] = {"site": site, "field": field_name, "commit": commit_id, "read": read_id}
        marked = {}
        for fid in (commit_id, read_id):
            try:
                fv = flows.get(fid)
            except Exception:  # noqa: BLE001 - not installed
                return None
            if (getattr(fv, "saved_by", None) or {}).get("kind") != "learned":
                return None
            mark = ledger.get(operation_key(fid)) or {}
            marked[fid] = mark.get("proof") == proof_hash(fv.definition.canonical())
        rule = LearnedRules(self._home()).get(f"{site}.profile.learned_{field_name}") or {}
        out["read_verified"] = marked[read_id]
        out["verified"] = marked[read_id] and marked[commit_id] and bool(rule.get("verified"))
        learned = [x for x in self.svc.store.lessons(site=site, field=field_name) if x["status"] in ("learned", "proved")]
        out["learned_on"] = learned[-1]["surface"] if learned else None
        return out

    # ---------------------------------------------------------------- learning
    def _may_teach(self, surface: str, site: str, field_name: str, lane: str, *, owner: bool) -> None:
        """Refuses a lesson that is not needed or one too many. A field with learned operations is
        taught again only when a sync saw the site change (`relearn`), its last lesson failed, or
        the owner in person asks; lessons are capped per lane a day and per field a week."""
        from datetime import UTC, datetime, timedelta

        st = self.state(site, field_name)
        row = self.svc.store.sync(surface).get(field_name) or {}
        if st is not None and (st["verified"] or st["read_verified"]) and not owner \
                and row.get("status") not in ("relearn", "learn_failed"):
            raise InvalidRequest(f"agentos already knows how to change {field_name} on {site}; it is taught again "
                                 "only when the site changes (the field then reads relearn), or by the owner")
        now = datetime.now(UTC)
        day = (now - timedelta(days=1)).isoformat(timespec="seconds")
        week = (now - timedelta(days=7)).isoformat(timespec="seconds")
        if len(self.svc.store.lessons(lane=lane, since=day)) >= LESSONS_PER_LANE_PER_DAY:
            raise InvalidRequest(f"lane {lane} had {LESSONS_PER_LANE_PER_DAY} lessons in the last day; try tomorrow")
        if len(self.svc.store.lessons(site=site, field=field_name, since=week)) >= LESSONS_PER_FIELD_PER_WEEK:
            raise InvalidRequest(f"{site} {field_name} was taught {LESSONS_PER_FIELD_PER_WEEK} times this week; "
                                 "the owner looks at it before another lesson")

    def start(self, surface: str, field_name: str, *, by: Any, grant_id: str | None,
              owner: bool = False) -> dict[str, Any]:
        """`grant_id`: the grant of the agent that will drive the lane; only its acts may send the
        one save the window lets through. `owner`: the owner in person (may teach a known field again)."""
        from agentos.node.egress.learn import close_learn, open_learn
        from agentos.node.sites import profile_edit_paths

        if not grant_id:
            raise InvalidRequest("a lesson is bound to the grant of the agent that drives the lane; call it with one")
        _r, site, value = self.learnable(surface, field_name)
        lane = self._lane_of(surface)
        pinned = self._pinned(lane, site)        # signed in to a known account, or refused now
        old = self.sessions.get((surface, field_name))
        if old is not None and self.clock() - old.started_at <= SESSION_TTL_S:
            raise InvalidRequest(f"a lesson for {field_name} on {surface} is already running; finish it first")
        if old is not None:
            close_learn(self.runtime, old.lane, old.window_id)
            self.sessions.pop((surface, field_name), None)
        recorder = self._need("flows").recorder
        if recorder.active(lane):
            raise InvalidRequest(f"someone is recording in lane {lane}; a lesson starts when it is free")
        self._may_teach(surface, site, field_name, lane, owner=owner)
        who = _who(by)
        lesson = self.svc.store.add_lesson(site=site, field=field_name, surface=surface, lane=lane,
                                           by=str(who.get("id") or ""), grant_id=grant_id)
        paths = profile_edit_paths(site, pinned)
        window = open_learn(self.runtime, lane, field=field_name, value=value, path_prefixes=None,
                            ttl_s=int(SESSION_TTL_S), site=site, grant_id=grant_id)
        recorder.start(lane, who or dict(PRINCIPAL))
        self.sessions[(surface, field_name)] = Session(surface, field_name, site, lane, value, window,
                                                       self.clock(), who, grant_id, lesson)
        self.svc.store.set_sync(surface, field_name, "learning", value_hash=None)
        return {"surface": surface, "lane": lane, "site": site, "field": field_name, "value": value,
                "edit_paths": paths,
                "next": (f"In lane {lane}: log in if needed (login), open the {site} page where the profile is "
                         f"edited (it is under {', '.join(paths)}), put exactly this value in the {field_name} "
                         "field (replace what is there), save, then open the account's own profile page, take a "
                         "snapshot, and call profile_learn_finish with the ref of the element that shows the new "
                         f"value and that page's URL. One save is let through; the window closes in "
                         f"{int(SESSION_TTL_S // 60)} minutes.")}

    async def finish(self, surface: str, field_name: str, *, readback_locator: dict[str, Any], profile_url: str,
                     by: Any, grant_id: str | None = None) -> dict[str, Any]:
        from agentos.node.egress.learn import close_learn, learn_result
        from agentos.node.learned_rules import LearnedRules
        from agentos.profiles.learn import generate

        s = self.sessions.get((surface, field_name))
        if s is None or self.clock() - s.started_at > SESSION_TTL_S:
            self.sessions.pop((surface, field_name), None)
            raise InvalidRequest(f"no learning session for {field_name} on {surface} (or it expired): call "
                                 "profile_learn_start first")
        if _who(by).get("id") != s.by.get("id") or (grant_id is not None and grant_id != s.grant_id):
            raise InvalidRequest("this lesson was started by another agent; only it finishes it")
        recorder = self._need("flows").recorder
        try:
            shape = learn_result(self.runtime, s.lane, s.window_id)
            rec = recorder.stop(s.lane) if recorder.active(s.lane) else {"events": [], "started_at": None}
        finally:
            close_learn(self.runtime, s.lane, s.window_id)
            self.sessions.pop((surface, field_name), None)
        if not shape:
            return self._failed(s, "the save was not seen: no request carrying exactly the value left the lane "
                                   "from the site's edit page. Learn again and save the value exactly as given")
        built = recorder.build_checked(rec["events"], lane=s.lane, flow_id=None, effect="commit",
                                       title=f"{s.site} {field_name}", started_at=rec.get("started_at"))
        if built[2] is not None:
            return self._failed(s, f"the recording is not usable: {built[2]}")
        limit = next((f.limit for n, f in mapping.SITES[s.site].items() if n == field_name), None)
        commit = {k: shape[k] for k in ("method", "host", "path", "op", "act_seq", "act_at") if shape.get(k)}
        try:
            from agentos.node.sites import profile_edit_paths

            account_id = self._account_id(s.site, s.lane, profile_url)
            problem = self._ensure_surface(s.surface, s.lane, s.site, account_id)
            if problem:
                return self._failed(s, problem)
            doc = generate(site=s.site, field=field_name, events=rec["events"], draft=built,
                           readback_locator=readback_locator,
                           profile_url=profile_url, commit=commit, limit=limit, account_id=account_id,
                           value=s.value, page_url=shape.get("page_url"),
                           edit_paths=profile_edit_paths(s.site, self._pinned(s.lane, s.site)))
            flow_ids = self._connectors().install_learned(doc)
            rule_id = LearnedRules(self._home()).add(s.site, field_name, host=shape["host"], path=shape["path"],
                                                     method=shape["method"], op=shape.get("op"))
        except Exception as e:  # noqa: BLE001 - every refusal is reported, nothing is half installed silently
            return self._failed(s, f"the learned steps were refused: {getattr(e, 'message', None) or e}")
        out = await self.verify(s.surface, s.site, field_name, s.value, rule_id=rule_id, flow_ids=flow_ids,
                                shape=shape)
        if out.get("learned") and s.lesson_id is not None:
            self.svc.store.set_lesson(s.lesson_id, "learned")
        return out

    async def verify(self, surface: str, site: str, field_name: str, value: str, *, rule_id: str,
                     flow_ids: list[str], shape: dict[str, Any]) -> dict[str, Any]:
        """Replay the read operation with no model: it must return exactly the value the learning
        saved. Then the read operation gets a replay mark. The save is not trusted yet: it is
        proved by its first real run on this same account (`prove`, from the sync runner), and only
        then is it used on the site's other accounts."""
        from agentos.profiles.learn import flow_ids as ids_for
        from agentos.runner.connectors.compile import proof_hash
        from agentos.runner.connectors.marks import mark_replay

        _commit_id, read_id = ids_for(site, field_name)
        runner = self._need("runner")
        run, _created = runner.submit(flow_id=read_id, params={}, surface=surface, principal=dict(PRINCIPAL),
                                      source=SOURCE, idempotency_key=f"profile-learn:{surface}:{field_name}:"
                                      f"{int(self.clock())}", authorized=False)
        run = await runner.wait(run.id, REPLAY_WAIT_S)
        data = ((run.output or {}).get("data") or {}) if str(run.status) in ("done", "succeeded") else {}
        seen = data.get("value")
        s = Session(surface, field_name, site, "", value, "", 0.0)
        if str(run.status) not in ("done", "succeeded"):
            code = (run.error or {}).get("code") or str(run.status)
            return self._failed(s, f"the replay with no model did not finish ({code}); the steps stay unverified")
        if _norm(seen) != _norm(value):
            return self._failed(s, "the replay with no model read back another value than the one saved; the steps "
                                   "stay unverified")
        flows = self._need("flows")
        evidence = {"run": run.id, "request": {k: shape.get(k) for k in ("method", "host", "path", "op")}}
        fv = flows.get(read_id)
        mark_replay(self._home(), read_id, proof_hash(fv.definition.canonical()), evidence)
        from agentos.profiles.service import value_hash

        self.svc.store.set_sync(surface, field_name, "in_sync", value_hash=value_hash(value), run_id=run.id)
        return {"surface": surface, "site": site, "field": field_name, "learned": True, "verified": False,
                "proving_on": surface, "operations": flow_ids, "rule": rule_id,
                "next": (f"Learned. The next time {field_name} changes, agentos saves it on {surface} first; once "
                         f"that save is read back, it changes {field_name} on every {site} account by itself.")}

    def prove(self, surface: str, site: str, field_name: str, run: Any) -> bool:
        """The learned save's first real run, on the account it was learned on, finished with its
        own readback: the save is verified (its replay mark and the rule). Called by the sync
        runner only. Returns whether it marked anything."""
        from agentos.node.learned_rules import LearnedRules
        from agentos.profiles.learn import flow_ids as ids_for
        from agentos.runner.connectors.compile import proof_hash
        from agentos.runner.connectors.marks import mark_replay

        st = self.state(site, field_name)
        if st is None or st["verified"] or not st["read_verified"] or st["learned_on"] != surface:
            return False
        commit_id, _read_id = ids_for(site, field_name)
        fv = self._need("flows").get(commit_id)
        if getattr(run, "flow_id", commit_id) != commit_id:
            return False
        mark_replay(self._home(), commit_id, proof_hash(fv.definition.canonical()), {"run": run.id, "on": surface})
        LearnedRules(self._home()).set_verified(f"{site}.profile.learned_{field_name}", by=f"replay:{run.id}")
        for x in self.svc.store.lessons(site=site, field=field_name):
            if x["status"] == "learned" and x["surface"] == surface:
                self.svc.store.set_lesson(x["id"], "proved")
        return True

    def _failed(self, s: Session, why: str) -> dict[str, Any]:
        self.svc.store.set_sync(s.surface, s.field, "learn_failed", value_hash=None, error=why[:300])
        if s.lesson_id is not None:
            self.svc.store.set_lesson(s.lesson_id, "failed", error=why[:300])
        return {"surface": s.surface, "site": s.site, "field": s.field, "learned": False, "verified": False,
                "problem": why}
