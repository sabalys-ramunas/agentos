"""Learning a profile field (docs/design/learn-a-site.md 1, 2, 7, 8): a recorded edit becomes a
strictly linted connector (agentos.profiles.learn), the node installs it as `learned` (never the
owner's vouch), and only the node's replays mark it verified, for learned operations alone."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.core.errors import InvalidRequest, PolicyDenied
from agentos.core.models import RunStatus
from agentos.profiles.learn import LearnRefused, flow_ids, generate
from agentos.runner.connectors.compile import ConnectorInvalid, compile_connector, compile_path, proof_hash
from agentos.runner.connectors.marks import BY_REPLAY, MarkLedger, mark_replay
from agentos.runner.connectors.service import ConnectorService
from agentos.runner.connectors.verify import mark_installed
from agentos.runner.fakes import OWNER, El, FakeLanes, FakeNotifier, FakeSite, Harness
from agentos.runner.models import is_owner

REPO = Path(__file__).resolve().parents[2]
REDDIT = REPO / "connectors" / "reddit"
LINKEDIN = REPO / "connectors" / "linkedin"
LANE = "biz-a/instagram"
PROFILE = "https://www.instagram.com/biza.coffee/"
TPL = "https://www.instagram.com/${acts_as_id}/"
EDIT = "https://www.instagram.com/accounts/edit/"
SAVE_URL = "https://www.instagram.com/api/v1/web/accounts/edit/"
BIO = {"role": "textbox", "name": "Bio"}
SHOWN = {"css": "header section span._ap3a", "within": {"role": "main"}}
TRACE = {"method": "POST", "host": "www.instagram.com", "path": "/api/v1/web/accounts/edit/"}
T0 = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def _events(text: str = "Coffee in Vilnius", *, extra: list | None = None) -> list[dict]:
    """What record_act pushes for an Instagram bio edit: open the edit page, fill Bio, press Submit."""
    return [
        {"action": "navigate", "url": PROFILE},
        {"action": "click", "locator": {"role": "link", "name": "Edit profile"}},
        {"action": "navigate", "url": EDIT},
        {"action": "fill", "text": text, "locator": BIO, "state_before": {"value": "old"},
         "state_after": {"value": text}},
        *(extra or []),
        {"action": "click", "locator": {"role": "button", "name": "Submit"}},
        {"action": "click", "locator": {"role": "button", "name": "Close"}},   # a toast, after the save
    ]


def _rec(tmp_path, events: list[dict]) -> list[dict]:
    """The events as the Recorder keeps them, each act a second after the one before."""
    h = Harness(tmp_path / "rec")
    rec = h.flows.recorder
    rec.start(LANE)
    for i, ev in enumerate(events):
        rec.observe(LANE, {"at": (T0 + timedelta(seconds=i)).isoformat(), **ev})
    return rec.stop(LANE)["events"]


def _sent_by(events: list[dict], name: str, base: dict) -> dict:
    """The traced save with the moment the request left: while the act on `name` ran (half a
    second before it ended); after everything when no act has that name."""
    ends = [datetime.fromisoformat(e["at"]) for e in events if (e.get("locator") or {}).get("name") == name]
    at = ends[0] - timedelta(seconds=0.5) if ends else T0 + timedelta(hours=1)
    return {**base, "act_at": at.isoformat()}


def _learn(tmp_path, events: list[dict], *, save: str = "Submit", commit: dict | None = None, **kw) -> dict:
    recorded = _rec(tmp_path, events)
    return generate(events=recorded, commit=_sent_by(recorded, save, commit or TRACE), **kw)


def _doc(tmp_path, events=None, *, save: str = "Submit", **kw):
    args = {"site": "instagram", "field": "bio", "readback_locator": SHOWN, "profile_url": PROFILE,
            "limit": 150, "account_id": "biza.coffee", "value": "Coffee in Vilnius", "old_value": "old"}
    commit = kw.pop("commit", TRACE)
    return _learn(tmp_path, events or _events(), save=save, commit=commit, **{**args, **kw})


def _compiled(doc):
    return compile_connector(doc, commit_gate=True, learned=True)


# ---------------------------------------------------------------- generate


def test_an_instagram_bio_edit_compiles_strictly_with_the_commit_gate(tmp_path):
    doc = _doc(tmp_path)
    assert doc["connector"] == "instagram-profile" and doc["site"] == "instagram"
    assert set(doc["operations"]) == {"bio", "bio_read"}
    cc = _compiled(doc)
    assert not [i for i in cc.issues if i.level == "error"]
    assert {op.flow_id for op in cc.operations.values()} == set(flow_ids("instagram", "bio"))
    assert str(cc.operations["bio"].effect) == "commit" and str(cc.operations["bio_read"].effect) == "read"
    meta = cc.operations["bio"].definition["connector"]
    assert meta["commit"]["strict"] is True and meta["commit"]["step"] == "save"
    assert re.match(meta["commit"]["request"]["url"], SAVE_URL)
    assert meta["commit"]["request"]["match"] == {"form:biography": "^${value}$"}   # the run's own value, in its field
    assert not re.match(meta["commit"]["request"]["url"], SAVE_URL + "x")
    assert meta["pace"] == {"min_gap_minutes": 60, "per_day": 2, "per_week": 5, "active_hours": True}
    assert "captcha" in meta["signals"]["hold"]


def test_the_typed_param_is_value_and_only_the_save_may_publish(tmp_path):
    op = _doc(tmp_path)["operations"]["bio"]
    assert list(op["input"]["properties"]) == ["value"]
    value = op["input"]["properties"]["value"]
    assert value["maxLength"] == 150 and value["show"] is True and value["readOnly"] is True
    assert re.fullmatch(value["pattern"], "Coffee\nin Vilnius") and not re.fullmatch(value["pattern"], "a\x07b")
    blob = json.dumps(op)
    assert "${bio}" not in blob and '"param": "bio"' not in blob
    steps = op["steps"]
    types = [s["type"] for s in steps]
    assert types == ["navigate", "assert_identity", "wait_for", "fill", "click", "pause", "navigate", "readback"]
    assert steps[0] == {"type": "navigate", "url": EDIT}
    assert "Edit profile" not in blob   # a recorded click other than the save is never replayed
    save = types.index("click")
    assert steps[save]["label"] == "save" and steps[save]["writes"] is True and "publishes" not in steps[save]
    assert types.index("assert_identity") < save
    assert all(s.get("publishes") is False for s in steps[:save] if s["type"] in ("click", "fill", "type", "press"))
    fill = steps[types.index("fill")]
    assert fill["param"] == "value" and fill["locator"]["names"] == {"en": "Bio"}
    assert fill["locator"]["within"] == {"role": "main"}   # name-only on the commit path: anchored
    assert not any("Close" in json.dumps(s) for s in steps)  # what came after the save is not replayed
    assert steps[-2] == {"type": "navigate", "url": TPL}
    assert steps[-1]["type"] == "readback" and steps[-1]["condition"] == {"kind": "text", "equals": "${value}"}
    assert op["commit"]["preflight"][0] == {"kind": "value_equals", "locator": fill["locator"], "param": "value"}
    assert len(op["commit"]["preflight"]) == 1   # edited on another page than it shows: no text_differs
    assert op["discard"] == [{"type": "press", "key": "Escape", "publishes": False}]


def test_the_read_operation_opens_the_profile_checks_identity_and_returns_the_value(tmp_path):
    op = _doc(tmp_path)["operations"]["bio_read"]
    assert op["effect"] == "read" and "commit" not in op
    assert [s["type"] for s in op["steps"]] == ["navigate", "assert_identity", "wait_for", "extract"]
    assert op["steps"][0]["url"] == TPL and op["steps"][3]["items"] == SHOWN
    assert op["steps"][3]["fields"]["value"]["max_chars"] == 150 == op["output"]["properties"]["value"]["maxLength"]
    assert op["readback"]["live"]["outputs"] == {"value": "current.items.0.value"}


def test_an_edit_on_the_profile_page_itself_also_checks_the_value_differs(tmp_path):
    profile = "https://www.linkedin.com/in/biz-a/"
    events = [{"action": "navigate", "url": profile},
              {"action": "click", "locator": {"role": "button", "name": "Edit intro"}},
              {"action": "fill", "text": "Coffee", "locator": {"role": "textbox", "name": "Headline"}},
              {"action": "click", "locator": {"role": "button", "name": "Save"}}]
    doc = _learn(tmp_path, events, save="Save", site="linkedin", field="headline", profile_url=profile,
                   readback_locator={"css": ".text-body-medium", "within": {"role": "main"}}, limit=220,
                   account_id="biz-a", value="Coffee", old_value="Tea",
                   commit={"method": "POST", "url": "https://www.linkedin.com/flagship-web/rsc-action/actions/server-request",
                           "op": "com.linkedin.sdui.requests.profile.updateHeadline"})
    op = doc["operations"]["headline"]
    assert [c["kind"] for c in op["commit"]["preflight"]] == ["value_equals", "text_differs"]
    assert op["commit"]["request"]["op"] == r"^com\.linkedin\.sdui\.requests\.profile\.updateHeadline$"
    assert op["acting_as"]["source"] == "url"
    assert [s.get("url") for s in op["steps"][:3]] == ["https://www.linkedin.com/in/me/", None,
                                                       "https://www.linkedin.com/in/${acts_as_id}/"]
    assert op["steps"][3]["type"] == "wait_for" and "Edit intro" not in json.dumps(op)
    save = next(s for s in op["steps"] if s.get("label") == "save")
    assert save["locator"]["within"] == {"role": "dialog"}
    assert save["readback"]["condition"] == {"kind": "absent"}
    _compiled(doc)


def test_name_is_never_learned(tmp_path):
    with pytest.raises(LearnRefused, match="rate-locks"):
        _doc(tmp_path, field="name")


@pytest.mark.parametrize("kw, why", [
    ({"commit": {**TRACE, "host": "evil.example"}}, "not instagram.com"),
    ({"commit": {**TRACE, "method": "GET"}}, "POST, PUT or PATCH"),
    ({"profile_url": "https://evil.example/biza/"}, "profile URL"),
    ({"site": "tiktok"}, "does not learn"),
    ({"field": "bio_read"}, "not a field name"),
])
def test_what_does_not_fit_is_refused(tmp_path, kw, why):
    with pytest.raises(LearnRefused, match=why):
        _doc(tmp_path, **kw)


def test_a_recording_that_typed_two_fields_or_saved_nothing_is_refused(tmp_path):
    two = _events(extra=[{"action": "fill", "text": "x.example", "locator": {"role": "textbox", "name": "Website"}}])
    with pytest.raises(LearnRefused, match="2 fields"):
        _doc(tmp_path, two)
    unsaved = _events()[:4]
    with pytest.raises(LearnRefused, match="nothing saved it"):
        _doc(tmp_path / "b", unsaved)
    with pytest.raises(LearnRefused, match="cannot be learned"):
        _doc(tmp_path, draft=({"steps": [{"type": "navigate", "url": EDIT}]}, [], "an input was dropped"))


# ---------------------------------------------------------------- install_learned


def test_install_learned_saves_both_operations_as_learned_and_not_the_owners(tmp_path):
    h = Harness(tmp_path)
    svc = ConnectorService(h.flows)
    ids = svc.install_learned(_doc(tmp_path))
    assert ids == sorted(flow_ids("instagram", "bio"))
    for fid in ids:
        fv = h.flows.get(fid)
        assert fv.saved_by["kind"] == "learned" and not fv.owner_saved and not is_owner(fv.saved_by)
    assert not is_owner({"kind": "learned", "id": "x", "installed_by": "owner", "owner": "owner"})
    rows = {r["operation"]: r for r in svc.describe()}
    assert rows["instagram-profile.bio"]["kind"] == "learned"


def test_a_relearn_replaces_the_learned_version(tmp_path):
    h = Harness(tmp_path)
    svc = ConnectorService(h.flows)
    svc.install_learned(_doc(tmp_path))
    first = h.flows.get("connector.instagram-profile.bio")
    again = _doc(tmp_path / "2", _events("Another bio"), value="Another bio",
                 readback_locator={"css": "header section span", "within": {"role": "main"}})
    svc.install_learned(again)
    head = h.flows.get("connector.instagram-profile.bio")
    assert head.version != first.version and head.definition.connector["version"] == "1.1.0"
    assert svc.installed_version("instagram-profile") == "1.1.0"


def test_install_learned_refuses_what_is_not_one_learned_field(tmp_path):
    h = Harness(tmp_path)
    svc = ConnectorService(h.flows)
    doc = _doc(tmp_path)
    with pytest.raises(PolicyDenied, match="-profile"):
        svc.install_learned({**doc, "connector": "instagram"})
    ops = dict(doc["operations"])
    ops["bio_more"] = ops["bio_read"]
    with pytest.raises(PolicyDenied, match="one commit operation"):
        svc.install_learned({**doc, "operations": ops})
    name_doc = json.loads(json.dumps(doc).replace('"bio_read"', '"name_read"'))
    name_doc["operations"] = {"name": name_doc["operations"]["bio"], "name_read": name_doc["operations"]["name_read"]}
    with pytest.raises(PolicyDenied, match="never learned"):
        svc.install_learned(name_doc)
    with pytest.raises(ConnectorInvalid):  # a catalog install cannot use a learned key
        svc.install(compile_connector(doc, commit_gate=True), by=OWNER)
    assert svc.installed() == []


def test_install_learned_refuses_a_tool_name_another_connector_serves(tmp_path):
    h = Harness(tmp_path)
    svc = ConnectorService(h.flows)
    doc = json.loads((LINKEDIN / "connector.json").read_text())
    doc["operations"] = {"profile_bio": doc["operations"]["profile_headline"]}   # tool linkedin_profile_bio
    svc.install(compile_connector(doc, commit_gate=True), by=OWNER)
    profile = "https://www.linkedin.com/in/biz-a/"
    events = [{"action": "navigate", "url": profile},
              {"action": "fill", "text": "hi there", "locator": {"role": "textbox", "name": "Summary"}},
              {"action": "click", "locator": {"role": "button", "name": "Save"}}]
    learned = _learn(tmp_path, events, save="Save", site="linkedin", field="bio", limit=2600,
                       profile_url=profile, account_id="biz-a", readback_locator=SHOWN, value="hi there",
                       commit={"method": "POST", "url": "https://www.linkedin.com/flagship-web/rsc-action/actions/x"})
    with pytest.raises(InvalidRequest, match="linkedin_profile_bio"):
        svc.install_learned(learned)


def test_remove_learned_deletes_the_pair_and_its_marks(tmp_path):
    h = Harness(tmp_path)
    svc = ConnectorService(h.flows)
    ids = svc.install_learned(_doc(tmp_path))
    ledger = MarkLedger(tmp_path / "home")
    mark_replay(ledger, ids[0], proof_hash(h.flows.get(ids[0]).definition.canonical()), {"run": "r1"})
    with pytest.raises(InvalidRequest, match="no learned"):
        svc.remove_learned("instagram", "website")
    assert svc.remove_learned("instagram", "bio", ledger=ledger) == ids
    assert svc.installed() == [] and ledger.all() == {}


# ---------------------------------------------------------------- replay marks


FID = "connector.instagram-profile.bio"


def test_a_replay_mark_verifies_a_learned_operation_until_its_steps_change(tmp_path):
    h = Harness(tmp_path)
    svc = ConnectorService(h.flows)
    svc.install_learned(_doc(tmp_path))
    home = tmp_path / "home"
    fv = h.flows.get(FID)
    assert MarkLedger(home).state(FID, fv.definition.canonical()) is None
    mark = mark_replay(home, FID, proof_hash(fv.definition.canonical()),
                       {"rehearsal_run": "r1", "read_run": "r2", "version": "1.0.0"})
    assert mark["by"] == BY_REPLAY and "replay" in mark["text"]
    state = MarkLedger(home).state(FID, fv.definition.canonical())
    assert state.by == BY_REPLAY and not state.stale and state.reason is None
    # a relearn with other steps: the mark names the old implementation, so it is stale
    svc.install_learned(_doc(tmp_path / "2", readback_locator={"css": "header span", "within": {"role": "main"}}))
    relearned = MarkLedger(home).state(FID, h.flows.get(FID).definition.canonical())
    assert relearned.stale and relearned.reason == "verified_stale"


def test_a_replay_mark_never_counts_for_a_catalog_operation(tmp_path):
    h = Harness(tmp_path)
    ConnectorService(h.flows).install(compile_path(REDDIT), by=OWNER)
    home = tmp_path / "home"
    fid = "connector.reddit.comment"
    fv = h.flows.get(fid)
    proof = proof_hash(fv.definition.canonical())
    with pytest.raises(ConnectorInvalid, match="learned operation only"):
        mark_replay(home, fid, proof, {"run": "r1"})
    assert MarkLedger(home).get(fid) is None
    # the owner's mark verifies it
    ledger = MarkLedger(home)
    mark_installed(ledger, "reddit.comment", fv.definition.canonical(), domain="reddit.com", note="canary",
                   day=date(2026, 11, 12))
    state = ledger.state(fid, fv.definition.canonical())
    assert state.by == "owner" and not state.stale


def test_mark_replay_wants_a_proof_hash_and_small_json_evidence(tmp_path):
    fid = "connector.instagram-profile.bio"
    with pytest.raises(ConnectorInvalid, match="proof hash"):
        mark_replay(tmp_path, fid, "sha256:abc", {})
    with pytest.raises(ConnectorInvalid, match="at most"):
        mark_replay(tmp_path, fid, "sha256:" + "0" * 64, {"x": "y" * 5000})
    with pytest.raises(ConnectorInvalid, match="JSON"):
        mark_replay(tmp_path, fid, "sha256:" + "0" * 64, {"x": object()})


def test_connector_list_shows_a_learned_connector_and_whether_replays_verified_it(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentos.runner.cli import _service
    from agentos.runner.connectors.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path / "home"))
    flows = _service().flows
    ids = ConnectorService(flows).install_learned(_doc(tmp_path))
    out = CliRunner().invoke(app, ["list"])
    assert out.exit_code == 0, out.output
    assert "instagram-profile.bio " in out.output and "learned on this node" in out.output
    assert "not verified" in out.output
    mark_replay(tmp_path / "home", ids[0], proof_hash(flows.get(ids[0]).definition.canonical()), {"run": "r1"})
    out = CliRunner().invoke(app, ["list", "--json"])
    rows = {r["operation"]: r for r in json.loads(out.output)}
    assert rows["instagram-profile.bio"]["verified_by"] == "replay"
    assert rows["instagram-profile.bio_read"]["verified"] == ""
    assert "verified by replay" in CliRunner().invoke(app, ["list"]).output


def test_a_relearn_clears_the_replay_marks_even_when_nothing_changed(tmp_path):
    h = Harness(tmp_path)
    svc = ConnectorService(h.flows)
    ledger = MarkLedger(tmp_path / "home")
    ids = svc.install_learned(_doc(tmp_path), ledger=ledger)
    for fid in ids:
        mark_replay(ledger, fid, proof_hash(h.flows.get(fid).definition.canonical()), {"run": "r1"})
    owner_key = "instagram-profile.bio_read"
    ledger.put(owner_key, proof=proof_hash(h.flows.get(ids[1]).definition.canonical()),
               text="2026-11-12 instagram.com canary: read by hand")
    svc.install_learned(_doc(tmp_path), ledger=ledger)   # the same steps, learned again
    assert ledger.get(ids[0]) is None                       # a relearn is always verified again
    assert ledger.get(owner_key)["by"] == "owner"           # an owner's mark is the owner's to revoke


def test_the_default_ledger_is_the_one_beside_the_runner_database(tmp_path):
    h = Harness(tmp_path)
    svc = ConnectorService(h.flows)
    ids = svc.install_learned(_doc(tmp_path))
    home = h.flows.store.path.parent
    mark_replay(home, ids[0], proof_hash(h.flows.get(ids[0]).definition.canonical()), {"run": "r1"})
    svc.install_learned(_doc(tmp_path))
    assert MarkLedger(home).get(ids[0]) is None


# ---------------------------------------------------------------- one connector, every account


@pytest.mark.parametrize("profile, account, why", [
    ("https://www.instagram.com/someone.else/", "biza.coffee", "does not carry the account id"),
    ("https://www.facebook.com/biza.coffee", "100012345678", "does not carry the account id"),
    ("https://www.reddit.com/user/biz_a/", "t2_abc123", "does not carry the account id"),
])
def test_a_profile_url_without_the_account_id_is_refused(tmp_path, profile, account, why):
    site = profile.split(".")[1]
    with pytest.raises(LearnRefused, match=why):
        _doc(tmp_path, site=site, profile_url=profile, account_id=account,
             commit={**TRACE, "host": f"www.{site}.com"})


def test_the_profile_and_recorded_pages_name_the_account_by_its_binding(tmp_path):
    events = [{"action": "navigate", "url": "https://www.facebook.com/profile.php?id=100012345678&sk=about"},
              {"action": "click", "locator": {"role": "button", "name": "Edit bio"}},
              {"action": "fill", "text": "Coffee in Vilnius", "locator": {"role": "textbox", "name": "Bio"}},
              {"action": "click", "locator": {"role": "button", "name": "Save"}}]
    doc = _learn(tmp_path, events, save="Save", site="facebook", field="bio", limit=101,
                   profile_url="https://www.facebook.com/profile.php?id=100012345678", account_id="100012345678",
                   readback_locator=SHOWN, value="Coffee in Vilnius",
                   commit={"method": "POST", "url": "https://www.facebook.com/api/graphql/", "op": "ProfileBioSave"})
    blob = json.dumps(doc)
    assert "100012345678" not in blob
    urls = [s["url"] for s in doc["operations"]["bio"]["steps"] if s["type"] == "navigate"]
    assert urls == ["https://www.facebook.com/profile.php?id=${acts_as_id}&sk=about",
                    "https://www.facebook.com/profile.php?id=${acts_as_id}"]
    _compiled(doc)


def instagram_site(me: str, bio: str = "old") -> FakeSite:
    """Instagram as the learned bio assumes it: the navigation's Profile link names the signed-in
    account, /accounts/edit/ edits it, and its own /<username>/ page shows the bio."""
    site = FakeSite()
    site.identity = {"site": "instagram", "user_id": "4200" + str(len(me)), "source": "cookie ds_user_id"}
    site.state = {"bio": {me: bio}, "saves": 0}

    def nav() -> El:
        return El("navigation").add(El("link", "Profile", attrs={"href": f"/{me}/"}))

    def save(s, el, d):
        s.state["saves"] += 1
        s.state["bio"][me] = d.values.get("bio", s.state["bio"][me])

    def edit(s, d):
        return El("document").add(nav(), El("main").add(
            El("textbox", "Bio", key="bio", attrs={"value": s.state["bio"][me]}),
            El("button", "Submit", on_click=save)))

    def profile(s, d):
        user = d.url.rstrip("/").rsplit("/", 1)[-1]
        if user not in s.state["bio"]:   # someone else's profile: the navigation is still the account's own
            return El("document").add(nav(), El("main").add(El("generic", "", text="theirs", css=(".bio",))))
        return El("document").add(nav(), El("main").add(El("link", "Edit profile", attrs={"href": EDIT}),
                                                        El("generic", "", text=s.state["bio"][user], css=(".bio",))))

    site.route(EDIT, edit)
    site.route("https://www.instagram.com/*", profile)
    return site


SYNC = {"principal": {"kind": "flow", "id": "", "owner": "owner"}, "source": "profiles"}   # agentos.profiles.sync


class _Learner:
    """The profile learner's verdict on the learned bio (agentos.profiles.learner.state)."""

    def __init__(self, verified: bool = True, learned_on: str | None = None):
        self.verified, self.learned_on = verified, learned_on

    def state(self, site, field):
        commit, read = flow_ids(site, field)
        return {"site": site, "field": field, "commit": commit, "read": read, "read_verified": True,
                "verified": self.verified, "learned_on": self.learned_on}


def _instagram(tmp_path, me: str, doc: dict, bio: str = "old", learner: _Learner | None = None) -> Harness:
    lane = f"biz-{me[:4]}/instagram"
    h = Harness(tmp_path, site=instagram_site(me, bio), services={
        "lanes": FakeLanes([lane]), "notifier": FakeNotifier(), "profiles": SimpleNamespace(learner=learner or _Learner())})
    h.driver.after_hooks.append(lambda op, target, d: d._render() if op in ("type", "fill") else None)
    ConnectorService(h.flows).install_learned(doc)
    h.runner.surfaces.put({"id": lane, "site": "instagram", "lane": lane, "account_language": "en",
                           "acts_as": {"kind": "account", "user_id": me}}, by=OWNER)
    h.lane = lane
    return h


@pytest.mark.parametrize("me", ["biza.coffee", "bizb_shop"])
async def test_one_learned_connector_edits_and_reads_each_accounts_own_profile(tmp_path, me):
    doc = _doc(tmp_path, readback_locator={"css": ".bio", "within": {"role": "main"}})   # learned on biza.coffee
    h = _instagram(tmp_path / me, me, doc)
    run = await h.run("connector.instagram-profile.bio", {"value": "Fresh roast daily"}, lane=h.lane, **SYNC)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert h.site.state["bio"][me] == "Fresh roast daily" and h.site.state["saves"] == 1
    assert run.output["data"] == {"proof_url": f"https://www.instagram.com/{me}/"}
    read = await h.run("connector.instagram-profile.bio_read", {}, lane=h.lane, **SYNC)
    assert read.status == RunStatus.SUCCEEDED_VERIFIED, read.error
    assert read.output["data"] == {"value": "Fresh roast daily"}


def test_a_learned_operation_is_never_served_as_an_agent_tool(tmp_path):
    from agentos.runner.connectors.tools import operation_specs

    h = _instagram(tmp_path, "biza.coffee", _doc(tmp_path))
    names = {spec.name for spec in operation_specs(h.runtime)}
    assert not {"instagram_profile_bio", "instagram_profile_bio_read"} & names


async def test_connectors_list_names_no_tool_for_a_learned_operation(tmp_path):
    from agentos.runner.connectors.tools import connectors_list
    from agentos.runner.fakes import FakeCtx

    h = _instagram(tmp_path, "biza.coffee", _doc(tmp_path))
    rows = (await connectors_list(FakeCtx(h.runtime)))["operations"]
    assert rows and all(r["tool"] is None for r in rows if r["operation"].startswith("instagram-profile."))


@pytest.mark.parametrize("who", [
    {"principal": {"kind": "agent", "id": "agent-1", "owner": "owner"}, "source": "tool"},
    {"principal": OWNER, "source": "tool"},
    {"principal": {"kind": "flow", "id": "", "owner": "owner"}, "source": "tool"},
    {"principal": {"kind": "agent", "id": "agent-1", "owner": "owner"}, "source": "profiles"},
])
@pytest.mark.parametrize("fid", list(flow_ids("instagram", "bio")))
def test_only_the_profile_sync_runs_a_learned_operation(tmp_path, who, fid):
    h = _instagram(tmp_path, "biza.coffee", _doc(tmp_path))
    params = {"value": "anything"} if not fid.endswith("_read") else {}
    with pytest.raises(PolicyDenied, match="only the profile sync"):
        h.submit(fid, params, surface=h.lane, **who)
    assert h.site.state["saves"] == 0


def test_an_unproved_learned_save_runs_only_on_the_account_it_was_learned_on(tmp_path):
    h = _instagram(tmp_path, "bizb_shop", _doc(tmp_path),
                   learner=_Learner(verified=False, learned_on="biz-biza/instagram"))
    with pytest.raises(PolicyDenied, match="not proved yet"):
        h.submit("connector.instagram-profile.bio", {"value": "anything"}, surface=h.lane, **SYNC)
    h.runtime.get("profiles").learner.learned_on = h.lane
    assert h.submit("connector.instagram-profile.bio", {"value": "anything"}, surface=h.lane, **SYNC)


async def test_a_queued_learned_save_still_stops_when_it_is_no_longer_allowed(tmp_path):
    h = _instagram(tmp_path, "biza.coffee", _doc(tmp_path), learner=_Learner(verified=False, learned_on="biz-bi/instagram"))
    h.runtime.get("profiles").learner.learned_on = h.lane
    run = h.submit("connector.instagram-profile.bio", {"value": "Fresh roast daily"}, surface=h.lane, **SYNC)
    h.runtime.get("profiles").learner.learned_on = "biz-other/instagram"    # relearned elsewhere meanwhile
    await h.runner.run_until_idle()
    run = h.runner.get(run.id)
    assert run.status == RunStatus.FAILED and run.error["code"] == "denied"
    assert h.site.state["saves"] == 0


def test_a_learned_save_with_no_profile_learner_does_not_run(tmp_path):
    h = _instagram(tmp_path, "biza.coffee", _doc(tmp_path))
    h.runtime.get("profiles").learner = None
    with pytest.raises(PolicyDenied, match="does not run"):
        h.submit("connector.instagram-profile.bio", {"value": "anything"}, surface=h.lane, **SYNC)


async def test_another_account_signed_in_is_a_wrong_account_not_an_edit(tmp_path):
    doc = _doc(tmp_path, readback_locator={"css": ".bio", "within": {"role": "main"}})
    h = _instagram(tmp_path, "bizb_shop", doc)
    h.runner.surfaces.put({"id": h.lane, "site": "instagram", "lane": h.lane, "account_language": "en",
                           "acts_as": {"kind": "account", "user_id": "biza.coffee"}}, by=OWNER)
    run = await h.run("connector.instagram-profile.bio", {"value": "Fresh roast daily"}, lane=h.lane, **SYNC)
    assert run.status == RunStatus.FAILED and run.error["code"] == "wrong_account", run.error
    assert h.site.state["saves"] == 0


async def test_the_read_returns_a_long_value_whole_up_to_the_fields_limit(tmp_path):
    long = " ".join(f"word{i:04d}" for i in range(290))   # 2609 characters, past the 300 a saved state keeps
    long = long[:2600]
    doc = _doc(tmp_path, readback_locator={"css": ".bio", "within": {"role": "main"}}, limit=2600)
    assert doc["operations"]["bio_read"]["output"]["properties"]["value"]["maxLength"] == 2600
    h = _instagram(tmp_path, "biza.coffee", doc, bio=long)
    read = await h.run("connector.instagram-profile.bio_read", {}, lane=h.lane, **SYNC)
    assert read.status == RunStatus.SUCCEEDED_VERIFIED, read.error
    assert read.output["data"]["value"] == long and len(long) == 2600
    unlimited = _doc(tmp_path, readback_locator=SHOWN, limit=None)["operations"]["bio_read"]
    assert unlimited["steps"][-1]["fields"]["value"]["max_chars"] == 5000


# ---------------------------------------------------------------- locators that name the value


@pytest.mark.parametrize("loc", [
    {"role": "paragraph", "name": "Coffee in Vilnius", "within": {"role": "main"}},
    {"role": "generic", "name": "old bio text", "within": {"role": "main"}},
    {"css": "span[title='Coffee in Vilnius']", "within": {"role": "main"}},
    {"role": "paragraph", "within": {"role": "region", "name": "coffee IN vilnius, about"}},
])
def test_a_readback_locator_naming_the_value_is_refused(tmp_path, loc):
    with pytest.raises(LearnRefused, match="readback locator names"):
        _doc(tmp_path, readback_locator=loc, old_value="old bio text")


def test_a_commit_path_locator_naming_the_value_is_refused(tmp_path):
    events = _events()
    events[3] = {**events[3], "locator": {"role": "textbox", "name": "Bio: old bio text"}}
    with pytest.raises(LearnRefused, match="finds its element by"):
        _doc(tmp_path, events, old_value="old bio text")
    events = _events()
    events[4] = {**events[4], "locator": {"role": "button", "name": "Save Coffee in Vilnius"}}
    with pytest.raises(LearnRefused, match="finds its element by"):
        _doc(tmp_path / "b", events, save="Save Coffee in Vilnius")
    assert _doc(tmp_path / "c", _events("Bi"), value="Bi")   # too short to tell from a label ("Bio"): not refused


# ---------------------------------------------------------------- only the field and its save are replayed


SUGGEST = {"action": "click", "locator": {"role": "checkbox", "name": "Show account suggestions on profiles"},
           "state_before": {"attributes": {"aria-checked": "true"}},
           "state_after": {"attributes": {"aria-checked": "false"}}}


def test_a_checkbox_toggled_before_submit_is_refused_not_replayed(tmp_path):
    """The reviewer's probe: the first click after the typing was a checkbox, Submit came next.
    The checkbox rides in the same POST, so replaying it on every sync passes strict scope."""
    with pytest.raises(LearnRefused, match="made a choice before saving.*checkbox"):
        _doc(tmp_path, _events(extra=[SUGGEST]))
    gender = {"action": "select", "option": "Prefer not to say", "locator": {"role": "combobox", "name": "Gender"}}
    with pytest.raises(LearnRefused, match="made a choice before saving"):
        _doc(tmp_path / "b", _events(extra=[gender]))
    pressed = {"action": "click", "locator": {"role": "button", "name": "Private account"},
               "state_before": {"attributes": {"aria-pressed": "false"}},
               "state_after": {"attributes": {"aria-pressed": "true"}}}
    with pytest.raises(LearnRefused, match="made a choice before saving"):
        _doc(tmp_path / "c", [pressed, *_events()])


def test_the_save_is_the_act_that_sent_the_request_never_the_checkbox(tmp_path):
    events = _events(extra=[SUGGEST])
    with pytest.raises(LearnRefused, match="is not a button"):
        _doc(tmp_path, events, save="Show account suggestions on profiles")
    toggle_button = {"action": "click", "locator": {"role": "button", "name": "Save"},
                     "state_before": {"attributes": {"aria-pressed": "false"}},
                     "state_after": {"attributes": {"aria-pressed": "true"}}}
    with pytest.raises(LearnRefused, match="is not a button"):
        _doc(tmp_path / "b", [*_events()[:4], toggle_button], save="Save")
    link = {"action": "click", "locator": {"role": "link", "name": "Done"}}
    with pytest.raises(LearnRefused, match="is not a button"):
        _doc(tmp_path / "c", [*_events()[:4], link], save="Done")


def test_acts_after_the_save_and_harmless_clicks_before_it_are_not_in_the_steps(tmp_path):
    events = [*_events()[:-1], SUGGEST, {"action": "click", "locator": {"role": "button", "name": "Close"}}]
    op = _doc(tmp_path, events)["operations"]["bio"]
    blob = json.dumps(op["steps"])
    assert "suggestions" not in blob and "Close" not in blob and "Edit profile" not in blob
    save = next(s for s in op["steps"] if s.get("label") == "save")
    assert save["locator"]["names"] == {"en": "Submit"}
    assert [s["type"] for s in op["steps"] if s["type"] in ("click", "press", "select", "fill", "type",
                                                            "scroll_to")] == ["fill", "click"]


def test_the_save_can_be_named_by_its_act_sequence_number(tmp_path):
    events = [{**ev, "act_seq": i} for i, ev in enumerate(_events())]
    recorded = _rec(tmp_path, events)
    args = {"site": "instagram", "field": "bio", "readback_locator": SHOWN, "profile_url": PROFILE, "limit": 150,
            "account_id": "biza.coffee", "value": "Coffee in Vilnius"}
    op = generate(events=recorded, commit={**TRACE, "act_seq": 4}, **args)["operations"]["bio"]
    assert next(s for s in op["steps"] if s.get("label") == "save")["locator"]["names"] == {"en": "Submit"}
    with pytest.raises(LearnRefused, match="is not a button"):
        generate(events=recorded, commit={**TRACE, "act_seq": 1}, **args)   # the Edit profile link
    with pytest.raises(LearnRefused, match="no recorded act sent"):
        generate(events=recorded, commit={**TRACE, "act_seq": 99}, **args)
    with pytest.raises(LearnRefused, match="act_seq or act_at"):
        generate(events=recorded, commit=TRACE, **args)
    # events without numbers (the recorder's today): the guard's act_seq is passed along, act_at decides
    plain = _rec(tmp_path / "plain", _events())
    commit = {**_sent_by(plain, "Submit", TRACE), "act_seq": 31}
    op = generate(events=plain, commit=commit, **args)["operations"]["bio"]
    assert next(s for s in op["steps"] if s.get("label") == "save")["locator"]["names"] == {"en": "Submit"}
    with pytest.raises(LearnRefused, match="recorded events"):
        generate(commit={**TRACE, "act_seq": 4}, **args)


def test_the_edit_page_is_the_page_that_sent_the_save_and_a_reviewed_edit_page(tmp_path):
    doc = _doc(tmp_path, page_url=EDIT, edit_paths=["/accounts/edit/"])
    assert doc["operations"]["bio"]["steps"][0] == {"type": "navigate", "url": EDIT}
    # the recording typed on another page than the one the guard saw send the save
    with pytest.raises(LearnRefused, match="was sent from"):
        _doc(tmp_path, page_url="https://www.instagram.com/accounts/password/change/",
             edit_paths=["/accounts/edit/"])
    # a page outside the site's reviewed edit pages (a DM thread, a settings page)
    events = [{**e, "url": "https://www.instagram.com/direct/t/123/"} if e.get("url") == EDIT else e
              for e in _events()]
    with pytest.raises(LearnRefused, match="reviewed profile edit pages"):
        _doc(tmp_path, events, page_url="https://www.instagram.com/direct/t/123/", edit_paths=["/accounts/edit/"])
    with pytest.raises(LearnRefused, match="reviewed profile edit pages"):
        _doc(tmp_path, events, edit_paths=["/accounts/edit/"])
    with pytest.raises(LearnRefused, match="reviewed profile edit pages"):
        _doc(tmp_path, edit_paths=[])                                       # a site with none
    # with no recorded page, the page that sent the save is the edit page
    no_nav = [e for e in _events() if e.get("action") != "navigate"]
    doc = _doc(tmp_path, no_nav, page_url=EDIT, edit_paths=["/accounts/edit/"])
    assert doc["operations"]["bio"]["steps"][0] == {"type": "navigate", "url": EDIT}


def test_typing_anything_but_exactly_the_value_is_refused(tmp_path):
    with pytest.raises(LearnRefused, match="other than exactly the value"):
        _doc(tmp_path, _events("Coffee in Vilnius "))
    retyped = _events(extra=[{"action": "type", "text": "!", "locator": BIO}])
    with pytest.raises(LearnRefused, match="other than exactly the value"):
        _doc(tmp_path / "b", retyped)
