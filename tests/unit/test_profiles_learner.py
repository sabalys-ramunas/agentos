"""Learning a site (agentos.profiles.learner) and syncing with what was learned (agentos.profiles.sync).

The node parts are fakes: the recorder, the runner, the lanes and surfaces, the egress learn
window. learn.generate and install_learned are tested on their own (test_profiles_learn.py); here
they are stand-ins, and what is checked is the order of things and who decides: the node's own
evidence, the model-free replay, the marks, and the sync's outcomes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from agentos.core.errors import InvalidRequest
from agentos.core.runtime import Runtime
from agentos.node.learned_rules import LearnedRules
from agentos.profiles import learner as learner_mod
from agentos.profiles.service import build, value_hash

OWNER = {"kind": "human", "id": "owner", "owner": "owner"}
AGENT = {"kind": "agent", "id": "agent-1"}
IG = "acme/instagram"
IG2 = "acme/instagram-shop"
GRANT = "gr_agent1"
PROFILE = {"profile": "acme", "workspace": "acme", "kind": "business", "display_name": "Acme Coffee",
           "bio": {"short": "Loud grinders. Honest coffee."}, "website": "https://acme.example",
           "accounts": [{"surface": IG}, {"surface": IG2}, {"surface": "acme/x"}]}


class Recorder:
    def __init__(self):
        self.on: set[str] = set()

    def active(self, lane):
        return lane in self.on

    def start(self, lane, principal):
        self.on.add(lane)
        return {"lane": lane}

    def stop(self, lane):
        self.on.discard(lane)
        return {"events": [{"action": "fill"}], "started_at": None}

    def build_checked(self, events, **kw):
        return ({"steps": []}, [], None)


class Flows:
    def __init__(self):
        self.recorder = Recorder()
        self.installed: dict[str, NS] = {}

    def get(self, fid):
        if fid not in self.installed:
            raise KeyError(fid)
        return self.installed[fid]


class Runner:
    def __init__(self, outputs):
        self.outputs = outputs           # flow_id -> callable(params) -> (status, output, error)
        self.runs: list[NS] = []

    def existing_run(self, key, **kw):
        return next((r for r in self.runs if r.key == key), None)

    def submit(self, *, flow_id, params, surface, principal, source, idempotency_key, authorized, **kw):
        status, output, error = self.outputs[flow_id](params)
        run = NS(id=f"run_{len(self.runs) + 1}", key=idempotency_key, flow_id=flow_id, params=params, status=status,
                 output=output, error=error, principal=principal, authorized=authorized, surface=surface)
        self.runs.append(run)
        return run, True

    async def wait(self, run_id, timeout, **kw):
        return next(r for r in self.runs if r.id == run_id)


class Surfaces:
    def __init__(self):
        self.made: dict[str, dict] = {}

    def find(self, sid):
        return NS(lane=self.made[sid]["lane"]) if sid in self.made else None

    def put(self, data, *, by):
        assert by == OWNER
        self.made[data["id"]] = data
        return data, True


class Lanes:
    def __init__(self, user_id="51234567", handle="@acme.coffee"):
        self.user_id = user_id
        self.handle = handle

    def get(self, lane):
        account = NS(site="instagram", user_id=self.user_id, handle=self.handle) if self.user_id else None
        return NS(spec=NS(account=account))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    window = {"shape": {"method": "POST", "host": "www.instagram.com", "path": "/api/v1/web/accounts/edit/",
                        "op": None, "page_url": "https://www.instagram.com/accounts/edit/"}}
    opened: list[dict] = []
    monkeypatch.setattr("agentos.node.egress.learn.open_learn",
                        lambda runtime, lane, **kw: opened.append({"lane": lane, **kw}) or "w1")
    monkeypatch.setattr("agentos.node.egress.learn.learn_result", lambda runtime, lane, wid: window["shape"])
    monkeypatch.setattr("agentos.node.egress.learn.close_learn", lambda runtime, lane, wid: None)
    flows = Flows()
    value = {"text": "Loud grinders. Honest coffee."}

    def read(params):
        return "done", {"data": {"value": value["text"]}}, None

    def commit(params):
        return "done", {"data": {"proof_url": "https://www.instagram.com/acme.coffee/"}}, None

    runner = Runner({"connector.instagram-profile.bio_read": read, "connector.instagram-profile.bio": commit})
    runtime = Runtime(flows=flows, runner=runner, surfaces=Surfaces(), lanes=Lanes())
    svc = build(runtime, home=tmp_path)
    svc.save(PROFILE, by=OWNER, as_owner=True)

    generated: dict = {}

    def fake_generate(**kw):
        generated.update(kw)
        return {"connector": "instagram-profile", "kw": kw}

    def fake_install(doc):
        for fid in ("connector.instagram-profile.bio", "connector.instagram-profile.bio_read"):
            flows.installed[fid] = NS(saved_by={"kind": "learned"},
                                      definition=NS(canonical=lambda fid=fid: {"id": fid, "steps": [fid]}))
        return list(flows.installed)

    monkeypatch.setattr("agentos.profiles.learn.generate", fake_generate)
    monkeypatch.setattr(learner_mod.ProfileLearner, "_connectors", lambda self: NS(install_learned=fake_install))
    return NS(svc=svc, runtime=runtime, flows=flows, runner=runner, window=window, opened=opened, value=value,
              home=tmp_path, generated=generated)


def learn(rig, field="bio"):
    rig.svc.learner.start(IG, field, by=AGENT, grant_id=GRANT)
    return asyncio.run(rig.svc.learner.finish(IG, field, readback_locator={"role": "heading", "name": "Bio"},
                                              profile_url="https://www.instagram.com/acme.coffee/", by=AGENT))


def test_start_opens_the_window_for_exactly_the_value_and_makes_the_surface(rig):
    out = rig.svc.learner.start(IG, "bio", by=AGENT, grant_id=GRANT)
    assert out["value"] == "Loud grinders. Honest coffee." and out["edit_paths"] == ["/accounts/edit/"]
    [w] = rig.opened
    assert w["lane"] == IG and w["value"] == "Loud grinders. Honest coffee." and w["field"] == "bio"
    # the guard resolves the reviewed edit paths with the pinned account itself
    assert w["path_prefixes"] is None and w["site"] == "instagram" and w["grant_id"] == GRANT
    assert IG not in rig.runtime.get("surfaces").made        # made at finish, from the profile URL
    assert rig.flows.recorder.active(IG)
    status = {a["surface"]: a for a in rig.svc.status("acme/acme")["accounts"]}
    assert status[IG]["fields"]["bio"]["state"] == "learning"


@pytest.mark.parametrize("field, words", [("name", "lock a name"), ("phone", "no value"), ("photo", "photos")])
def test_some_fields_are_never_learned(rig, field, words):
    with pytest.raises(InvalidRequest, match=words):
        rig.svc.learner.start(IG, field, by=AGENT, grant_id=GRANT)


def test_a_lane_with_no_pinned_account_is_refused(rig):
    rig.runtime.get("lanes").user_id = None
    with pytest.raises(InvalidRequest, match="no pinned instagram account"):
        rig.svc.learner.start(IG, "bio", by=AGENT, grant_id=GRANT)


def test_a_site_agentos_cannot_learn_is_refused(rig):
    with pytest.raises(InvalidRequest, match="cannot learn x"):
        rig.svc.learner.start("acme/x", "bio", by=AGENT, grant_id=GRANT)


def test_finish_installs_replays_with_no_model_and_marks_verified(rig):
    out = learn(rig)
    assert out["learned"] is True and out["verified"] is False and out["proving_on"] == IG
    # Instagram acts as the username in the profile URL, not the numeric id in the login cookie
    assert rig.runtime.get("surfaces").made[IG]["acts_as"] == {"kind": "account", "user_id": "acme.coffee"}
    assert rig.generated["account_id"] == "acme.coffee" and rig.generated["value"] == "Loud grinders. Honest coffee."
    assert rig.generated["events"] == [{"action": "fill"}]          # the generator works from the raw events
    # the page the guard saw send the save, and the reviewed edit pages it must be under
    assert rig.generated["page_url"] == "https://www.instagram.com/accounts/edit/"
    assert rig.generated["edit_paths"] == ["/accounts/edit/"]
    [replay] = rig.runner.runs
    assert replay.flow_id == "connector.instagram-profile.bio_read" and replay.authorized is False
    assert replay.principal == {"kind": "flow", "id": "", "owner": "owner"}
    rule = LearnedRules(rig.home).get("instagram.profile.learned_bio")
    assert not rule["verified"] and rule["path"] == "/api/v1/web/accounts/edit/"   # the save is proved later
    st = rig.svc.learner.state("instagram", "bio")
    assert st["read_verified"] is True and st["verified"] is False and st["learned_on"] == IG
    fields = {a["surface"]: a for a in rig.svc.status("acme/acme")["accounts"]}[IG]["fields"]
    assert fields["bio"]["state"] == "in_sync" and fields["bio"]["learned"] is True
    assert fields["website"]["state"] == "manual" and fields["website"]["learnable"] is True
    assert {a["surface"]: a for a in rig.svc.status("acme/acme")["accounts"]}["acme/x"]["fields"]["bio"][
        "learnable"] is False                                           # no reviewed save request on x


def test_no_save_seen_means_nothing_is_installed(rig):
    rig.window["shape"] = None
    out = learn(rig)
    assert out["learned"] is False and "save was not seen" in out["problem"]
    assert rig.flows.installed == {} and rig.runner.runs == []
    fields = {a["surface"]: a for a in rig.svc.status("acme/acme")["accounts"]}[IG]["fields"]
    assert fields["bio"]["state"] == "learn_failed"


def test_a_replay_that_reads_another_value_stays_unverified(rig):
    rig.value["text"] = "something else"
    out = learn(rig)
    assert out["verified"] is False and "another value" in out["problem"]
    assert rig.svc.learner.state("instagram", "bio")["read_verified"] is False
    assert not LearnedRules(rig.home).get("instagram.profile.learned_bio")["verified"]


def test_finish_without_start_is_refused(rig):
    with pytest.raises(InvalidRequest, match="profile_learn_start first"):
        asyncio.run(rig.svc.learner.finish(IG, "bio", readback_locator={}, profile_url="https://x", by=AGENT))


# ---------------------------------------------------------------- sync

def test_a_learned_save_is_proved_on_its_own_account_before_any_other(rig):
    learn(rig)
    status = {a["surface"]: a for a in rig.svc.status("acme/acme")["accounts"]}
    assert status[IG2]["fields"]["bio"]["state"] == "proving_elsewhere"
    assert asyncio.run(rig.svc.sync.run_once()) == []                # learning saved it; the other waits
    rig.svc.update("acme/acme", {"bio": {"short": "New bio."}}, by=OWNER, as_owner=True)
    [first] = asyncio.run(rig.svc.sync.run_once())
    assert first["surface"] == IG and first["proved"] is True
    assert rig.svc.learner.state("instagram", "bio")["verified"] is True
    assert LearnedRules(rig.home).get("instagram.profile.learned_bio")["verified"]
    [second] = asyncio.run(rig.svc.sync.run_once())                  # now the site's other accounts
    assert second["surface"] == IG2 and second["status"] == "in_sync" and "proved" not in second


def test_sync_changes_a_learned_field_when_the_profile_changes_with_no_card(rig):
    learn(rig)
    rig.svc.update("acme/acme", {"bio": {"short": "New bio."}}, by=OWNER, as_owner=True)
    done = asyncio.run(rig.svc.sync.run_once())[0]
    assert done["status"] == "in_sync" and done["field"] == "bio"
    commit = rig.runner.runs[-1]
    assert commit.flow_id == "connector.instagram-profile.bio" and commit.params == {"value": "New bio."}
    assert commit.authorized is False and commit.principal["kind"] == "flow"
    row = rig.svc.store.sync(IG)["bio"]
    assert row["status"] == "in_sync" and row["value_hash"] == value_hash("New bio.")
    asyncio.run(rig.svc.sync.run_once())                             # the other account, now proved
    assert asyncio.run(rig.svc.sync.run_once()) == []                # nothing due twice


def test_an_agents_draft_is_not_synced_until_the_owner_approves_it(rig):
    learn(rig)
    v = rig.svc.update("acme/acme", {"bio": {"short": "Agent bio."}}, by=AGENT, as_owner=False)["version"]
    assert asyncio.run(rig.svc.sync.run_once()) == []
    rig.svc.decide("acme/acme", v, approve=True, by=OWNER, in_person=True)
    done = asyncio.run(rig.svc.sync.run_once())[0]
    assert done["status"] == "in_sync"


@pytest.mark.parametrize("code, status", [("signal_captcha", "held"), ("preflight_mismatch", "relearn"),
                                          ("timeout", "failed")])
def test_a_failed_sync_holds_relearns_or_retries(rig, code, status):
    learn(rig)
    rig.runner.outputs["connector.instagram-profile.bio"] = lambda p: ("failed", None, {"code": code})
    rig.svc.update("acme/acme", {"bio": {"short": "New bio."}}, by=OWNER, as_owner=True)
    [done] = asyncio.run(rig.svc.sync.run_once())                    # only the learning account: not proved
    assert done["status"] == status and done["surface"] == IG
    fields = {a["surface"]: a for a in rig.svc.status("acme/acme")["accounts"]}[IG]["fields"]
    assert fields["bio"]["state"] == status
    again = asyncio.run(rig.svc.sync.run_once())
    assert (again == []) is (status == "relearn")                      # a relearn waits for an agent


def test_relearns_are_capped_per_week(rig):
    learn(rig)
    rig.runner.outputs["connector.instagram-profile.bio"] = lambda p: ("failed", None, {"code": "preflight_mismatch"})
    for n in range(4):
        rig.svc.update("acme/acme", {"bio": {"short": f"Bio {n}."}}, by=OWNER, as_owner=True)
        asyncio.run(rig.svc.sync.run_once())
    assert len(rig.svc.store.sync(IG)["bio"]["relearns"]) == 2


def test_a_surface_bound_to_another_id_is_not_overwritten_(rig):
    surfaces = rig.runtime.get("surfaces")
    surfaces.made[IG] = {"id": IG, "lane": IG, "acts_as": None}
    surfaces.find = lambda sid: NS(lane=IG, acts_as=NS(user_id="51234567", id=None)) if sid == IG else None
    out = learn(rig)
    assert out["learned"] is False and "acts as 51234567" in out["problem"]
    assert rig.flows.installed == {}


# ---------------------------------------------------------------- what the adversarial review found

def test_a_lesson_needs_a_grant_and_belongs_to_whoever_started_it(rig):
    with pytest.raises(InvalidRequest, match="bound to the grant"):
        rig.svc.learner.start(IG, "bio", by=AGENT, grant_id=None)
    rig.svc.learner.start(IG, "bio", by=AGENT, grant_id=GRANT)
    with pytest.raises(InvalidRequest, match="already running"):
        rig.svc.learner.start(IG, "bio", by=AGENT, grant_id=GRANT)
    with pytest.raises(InvalidRequest, match="started by another agent"):
        asyncio.run(rig.svc.learner.finish(IG, "bio", readback_locator={}, profile_url="https://x",
                                           by={"kind": "agent", "id": "intruder"}, grant_id="gr_other"))


def test_someone_elses_recording_is_never_stopped(rig):
    rig.flows.recorder.on.add(IG)
    with pytest.raises(InvalidRequest, match="someone is recording"):
        rig.svc.learner.start(IG, "bio", by=AGENT, grant_id=GRANT)
    assert rig.flows.recorder.active(IG)


def test_a_known_field_is_taught_again_only_when_the_site_changed_or_by_the_owner(rig):
    learn(rig)
    with pytest.raises(InvalidRequest, match="already knows how"):
        rig.svc.learner.start(IG, "bio", by=AGENT, grant_id=GRANT)
    rig.svc.store.set_sync(IG, "bio", "relearn", value_hash=None)
    rig.svc.learner.start(IG, "bio", by=AGENT, grant_id=GRANT)


def test_lessons_are_capped_per_lane_a_day(rig):
    for _ in range(learner_mod.LESSONS_PER_LANE_PER_DAY):
        rig.svc.store.add_lesson(site="instagram", field="website", surface=IG, lane=IG, by="a", grant_id=GRANT)
    with pytest.raises(InvalidRequest, match="lessons in the last day"):
        rig.svc.learner.start(IG, "bio", by=AGENT, grant_id=GRANT)


def test_the_account_comes_from_the_owners_handle_not_the_agents_url(rig):
    rig.svc.learner.start(IG, "bio", by=AGENT, grant_id=GRANT)
    out = asyncio.run(rig.svc.learner.finish(IG, "bio", readback_locator={}, by=AGENT, grant_id=GRANT,
                                             profile_url="https://www.instagram.com/someone.else/"))
    assert out["learned"] is False and "@acme.coffee" in out["problem"]
    assert IG not in rig.runtime.get("surfaces").made


def test_no_handle_no_lesson_on_a_username_site(rig):
    rig.runtime.get("lanes").handle = None
    out = learn(rig)
    assert out["learned"] is False and "no handle" in out["problem"]
