"""Profiles: one person or business written once, its accounts brought in line from it.

The model (validation, privacy, voice), the mapping to each site's fields, the service (versions,
who approves, one profile per account, photos, sync status), and the tools."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from openajar.core.errors import InvalidRequest, NotFound, PolicyDenied
from openajar.core.models import Effect, Principal
from openajar.core.runtime import Runtime
from openajar.profiles import mapping, tools
from openajar.profiles.errors import OwnerOnly, ProfileInvalid
from openajar.profiles.models import Attachment, Profile
from openajar.profiles.service import ProfilesService, account_changes, build, merge, parse_profile

LI = "biz-a/linkedin"
OWNER = {"kind": "human", "id": "owner", "owner": "owner"}
AGENT = {"kind": "agent", "id": "agent-1"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64

BASE = {
    "profile": "ada", "workspace": "biz-a", "kind": "person", "display_name": "Ada Lovelace",
    "name": {"first": "Ada", "last": "Lovelace"}, "headline": "Analyst of engines",
    "bio": {"short": "Engines and notes.", "long": "Notes on the analytical engine."},
    "phone": "+370 600 00000", "email": "ada@example.com", "birth_date": "1815-12-10",
    "website": "https://ada.example",
    "voice": {"personality": "Dry and exact.", "tone": "short", "dont": ["politics"], "emoji": "none",
              "examples": [{"context": "Is it a calculator?", "reply": "It weaves algebra."}]},
    "accounts": [{"surface": LI}, {"surface": "biz-a/instagram"}],
}


def data(**changes):
    return merge(BASE, changes)


# ---------------------------------------------------------------- the model

def test_a_profile_validates_and_normalizes():
    p = parse_profile(data())
    assert p.phone == "+37060000000"
    assert [a.site for a in p.accounts] == ["linkedin", "instagram"]
    for bad, words in [
        (data(phone="060000000"), "international number"),
        (data(headline="x" * 221), "at most 220"),
        (data(website="ada.example"), "https://"),
        (data(headline="hi\u200bthere"), "invisible"),
        (data(kind="business"), "first and last name"),
        (data(accounts=[{"surface": "other/linkedin"}]), "another workspace"),
        (data(accounts=[{"surface": LI}, {"surface": LI}]), "listed twice"),
        (data(photos={"avatar": "in_3f9a2c71"}), "profile store keeps"),
    ]:
        with pytest.raises(ProfileInvalid, match=words):
            parse_profile(bad)


def test_nothing_is_hidden_from_agents():
    view = parse_profile(data()).canonical()
    assert view["phone"] == "+37060000000" and view["email"] == "ada@example.com"


def test_the_voice_becomes_plain_instructions():
    text = parse_profile(data()).voice_prompt()
    assert text.startswith("Write as Ada Lovelace.")
    assert "Personality: Dry and exact." in text and "Never: politics" in text and "Emoji: none." in text
    assert "Reply: It weaves algebra." in text
    assert parse_profile(data(voice=None)).voice_prompt() is None


# ---------------------------------------------------------------- the mapping

def test_each_site_takes_its_own_fields_with_overrides_and_privacy():
    p = parse_profile(data(accounts=[{"surface": LI}, {"surface": "biz-a/instagram", "overrides": {"bio": "IG only"}},
                                     {"surface": "biz-a/x"}]))
    li, ig, x = (mapping.resolve(p, a) for a in p.accounts)
    assert li.manual == ["name", "headline", "about", "website", "phone"]
    assert li.values["name"] == {"first": "Ada", "last": "Lovelace"} and li.values["about"].startswith("Notes")
    assert li.values["phone"] == "+37060000000"
    assert ig.values == {"name": "Ada Lovelace", "bio": "IG only", "website": "https://ada.example",
                         "phone": "+37060000000", "email": "ada@example.com"}
    assert ig.skipped == {}
    assert x.values["bio"] == "Engines and notes." and not x.problems


def test_a_value_a_site_does_not_take_is_a_problem_never_cut_short():
    p = parse_profile(data(bio={"short": "x" * 200}, accounts=[{"surface": "biz-a/instagram"}, {"surface": "biz-a/x"}]))
    ig, x = (mapping.resolve(p, a) for a in p.accounts)
    assert "bio" not in ig.values and "takes at most 150" in ig.problems[0]
    assert "bio" not in x.values and "at most 160" in x.problems[0]
    unknown = mapping.resolve(p, Attachment(surface="biz-a/instagram", overrides={"headline": "x"}))
    assert "no profile field 'headline'" in unknown.problems[0]


def test_a_business_page_name_changes_by_hand():
    p = parse_profile(data(kind="business", name=None, birth_date=None, display_name="Acme Ltd"))
    li = mapping.resolve(p, p.accounts[0])
    assert li.skipped["name"] == "people only"
    assert set(li.manual) == {"headline", "about", "website", "phone"}


# ---------------------------------------------------------------- the service

def test_the_owner_saves_live_and_an_agent_saves_a_draft(tmp_path):
    svc = ProfilesService(home=tmp_path)
    out = svc.save(data(), by=OWNER, as_owner=True)
    assert out["state"] == "approved" and out["version"] == 1
    assert out["changes"][LI] == ["name", "headline", "about", "website", "phone"]
    assert "website changes on 2 accounts" in out["summary"]
    agent = svc.update("biz-a/ada", {"headline": "Poet of numbers"}, by=AGENT, as_owner=False)
    assert agent["state"] == "draft" and agent["version"] == 2 and agent["changes"] == {LI: ["headline"]}
    assert svc.for_surface(LI).definition.headline == "Analyst of engines"       # nothing changed yet
    assert svc.get("biz-a/ada")["draft_waiting"] == 2
    with pytest.raises(OwnerOnly):
        svc.decide("biz-a/ada", 2, approve=True, by=AGENT, in_person=True)
    svc.decide("biz-a/ada", 2, approve=True, by=OWNER, in_person=True)
    assert svc.for_surface(LI).definition.headline == "Poet of numbers"
    assert [v.state.value for v in svc.store.versions("biz-a", "ada")] == ["superseded", "approved"]
    # the same profile again is the same version; a rejected one may come back as a new draft
    assert svc.update("biz-a/ada", {"headline": "Poet of numbers"}, by=AGENT, as_owner=False)["created"] is False
    v3 = svc.update("biz-a/ada", {"headline": "Third"}, by=AGENT, as_owner=False)["version"]
    svc.decide("biz-a/ada", v3, approve=False, by=OWNER, in_person=True)
    assert svc.update("biz-a/ada", {"headline": "Third"}, by=AGENT, as_owner=False)["version"] == v3 + 1


def test_changes_merge_and_name_the_fields_per_account():
    old = parse_profile(data())
    new = parse_profile(merge(old.canonical(), {"bio": {"short": "New short"}, "website": None}))
    assert new.bio.long == old.bio.long
    changes = account_changes(old, new)
    assert changes["biz-a/instagram"] == ["bio", "website (no longer set; the account keeps what it shows)"]
    assert changes[LI] == ["website (no longer set; the account keeps what it shows)"]


def test_an_account_belongs_to_one_profile_and_too_long_values_refuse_the_save(tmp_path):
    svc = ProfilesService(home=tmp_path)
    svc.save(data(), by=OWNER, as_owner=True)
    with pytest.raises(ProfileInvalid, match="attached to profile biz-a/ada"):
        svc.save(data(profile="other", accounts=[{"surface": LI}]), by=OWNER, as_owner=True)
    with pytest.raises(ProfileInvalid, match="instagram takes at most 150"):
        svc.update("biz-a/ada", {"bio": {"short": "x" * 151}}, by=OWNER, as_owner=True)
    ok = svc.update("biz-a/ada", {"bio": {"short": "x" * 151}, "accounts": [
        {"surface": LI}, {"surface": "biz-a/instagram", "overrides": {"bio": "short"}}]}, by=OWNER, as_owner=True)
    assert ok["state"] == "approved"
    svc.detach("biz-a/ada", LI, by=OWNER, as_owner=True)
    assert svc.for_surface(LI) is None
    assert svc.attach("biz-a/ada", LI, by=OWNER, overrides={"headline": "Here"}, as_owner=True)["state"] == "approved"
    assert svc.resolved(LI).values["headline"] == "Here"
    with pytest.raises(InvalidRequest, match="workspace/profile"):
        svc.get("ada")


def test_photos_are_kept_once_and_must_exist(tmp_path):
    svc = ProfilesService(home=tmp_path)
    a = svc.put_photo("biz-a", "me.png", PNG)
    assert svc.put_photo("biz-a", "again.png", PNG)["photo"] == a["photo"]
    assert len(list(svc.store.photos_dir.iterdir())) == 1
    path, _meta = svc.store.photo(a["photo"])
    assert path.read_bytes() == PNG and oct(path.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(InvalidRequest, match="JPEG, PNG"):
        svc.put_photo("biz-a", "x.txt", b"hello")
    with pytest.raises(ProfileInvalid, match="no stored photo"):
        svc.save(data(photos={"avatar": "sha256:" + "b" * 64}), by=OWNER, as_owner=True)
    assert svc.save(data(photos={"avatar": a["photo"]}), by=OWNER, as_owner=True)["state"] == "approved"


def test_manual_fields_read_done_by_hand_until_the_value_changes(tmp_path):
    svc = ProfilesService(home=tmp_path)
    svc.save(data(), by=OWNER, as_owner=True)
    ig = next(a for a in svc.status("biz-a/ada")["accounts"] if a["surface"] == "biz-a/instagram")
    assert {f: v["state"] for f, v in ig["fields"].items()} == {"name": "manual", "bio": "manual", "website": "manual",
                                                       "phone": "manual", "email": "manual"}
    with pytest.raises(InvalidRequest, match="not a manual field"):
        svc.mark_synced(LI, ["bio"], by=AGENT)  # LinkedIn has no bio field
    svc.mark_synced("biz-a/instagram", ["bio"], by=AGENT)
    ig = next(a for a in svc.status("biz-a/ada")["accounts"] if a["surface"] == "biz-a/instagram")
    assert ig["fields"]["bio"]["state"] == "done_by_hand"
    svc.update("biz-a/ada", {"bio": {"short": "Changed."}}, by=OWNER, as_owner=True)
    ig = next(a for a in svc.status("biz-a/ada")["accounts"] if a["surface"] == "biz-a/instagram")
    assert ig["fields"]["bio"]["state"] == "manual"


# ---------------------------------------------------------------- the tools

class Ctx:
    def __init__(self, runtime, principal, *, lanes="biz-a/", in_person=False):
        self.runtime, self.principal, self.lanes, self.in_person = runtime, principal, lanes, in_person
        self.seen: list[tuple[str, str | None]] = []

    async def authorize(self, effect, *, lane_id=None, summary=None, content=None, **kw):
        self.seen.append((effect.value, lane_id))
        return NS(route="auto", via=None)

    def can_access_lane(self, lane_id):
        return lane_id.startswith(self.lanes)


def call(fn, ctx, **kw):
    return asyncio.run(fn(ctx, **kw))


def test_the_tools_save_drafts_for_agents(tmp_path):
    runtime = Runtime()
    build(runtime, home=tmp_path)
    owner = Ctx(runtime, Principal(kind="human", id="owner"), in_person=True)
    token = Ctx(runtime, Principal(kind="human", id="owner"))              # the node's token: not in person
    agent = Ctx(runtime, Principal(kind="agent", id="agent-1"))
    out = call(tools.profile_save, owner, profile=json.dumps(data()))
    assert out["state"] == "approved"
    assert call(tools.profile_update, token, profile="biz-a/ada", changes="headline: From a token")["state"] == "draft"
    got = call(tools.profile_get, agent, surface=LI)
    assert got["definition"]["phone"] == "+37060000000" and got["voice_prompt"].startswith("Write as Ada")
    assert call(tools.profile_get, owner, profile="biz-a/ada")["definition"]["phone"] == "+37060000000"
    assert [p["profile"] for p in call(tools.profile_list, agent)["profiles"]] == ["biz-a/ada"]
    outsider = Ctx(runtime, Principal(kind="agent", id="other"), lanes="biz-b/")
    assert call(tools.profile_list, outsider)["profiles"] == []
    with pytest.raises(NotFound):
        call(tools.profile_get, outsider, profile="biz-a/ada")
    narrow = Ctx(runtime, Principal(kind="agent", id="narrow"), lanes="biz-a/linkedin")
    with pytest.raises(PolicyDenied, match="whole workspace"):
        call(tools.profile_update, narrow, profile="biz-a/ada", changes="headline: x")
    status = call(tools.profile_sync_status, narrow, profile="biz-a/ada")
    assert [a["surface"] for a in status["accounts"]] == [LI]
    assert call(tools.profile_mark_synced, agent, surface="biz-a/instagram", fields=["bio"])["marked"] == ["bio"]
    assert ("draft", "biz-a/instagram") in agent.seen
    assert {s.name for s in tools.tool_specs(None)} >= {"profile_get", "profile_update", "profile_sync_status"}
    assert all(s.effect in (Effect.READ, Effect.DRAFT) for s in tools.tool_specs(None))


def test_the_profile_has_the_fields_accounts_read():
    assert Profile.model_fields.keys() >= {"voice", "phone", "photos", "accounts"}


# ---------------------------------------------------------------- what the review found

def test_an_owners_edit_never_carries_an_agents_waiting_draft_live(tmp_path):
    svc = ProfilesService(home=tmp_path)
    svc.save(data(), by=OWNER, as_owner=True)
    svc.update("biz-a/ada", {"phone": "+37069999999", "voice": {"personality": "Loud."}}, by=AGENT, as_owner=False)
    out = svc.update("biz-a/ada", {"headline": "New headline"}, by=OWNER, as_owner=True)
    live = svc.for_surface(LI).definition
    assert out["state"] == "approved" and live.headline == "New headline"
    assert live.phone == "+37060000000" and live.voice.personality == "Dry and exact."


def test_only_the_owner_in_person_decides_and_only_a_named_version(tmp_path):
    svc = ProfilesService(home=tmp_path)
    svc.save(data(), by=OWNER, as_owner=True)
    v = svc.update("biz-a/ada", {"headline": "x"}, by=AGENT, as_owner=False)["version"]
    with pytest.raises(OwnerOnly):
        svc.decide("biz-a/ada", v, approve=True, by=OWNER, in_person=False)      # the node's token
    assert svc.for_surface(LI).definition.headline == "Analyst of engines"


@pytest.mark.parametrize("account, words", [
    ({"surface": LI, "overrides": {"headline": "two\nlines"}}, "takes one line"),
    ({"surface": LI, "map": {"headline": "links"}}, "takes text"),
    ({"surface": LI, "map": {"about": "voice"}}, "takes text"),
    ({"surface": LI, "map": {"photo": "headline"}}, "takes a photo"),
    ({"surface": "biz-a/instagram", "map": {"bio": "name"}}, "not a first and last name"),
])
def test_a_value_a_site_would_refuse_refuses_the_save(tmp_path, account, words):
    svc = ProfilesService(home=tmp_path)
    with pytest.raises(ProfileInvalid, match=words):
        svc.save(data(accounts=[account]), by=OWNER, as_owner=True)


def test_a_member_in_person_saves_drafts(tmp_path):
    runtime = Runtime()
    build(runtime, home=tmp_path)
    call(tools.profile_save, Ctx(runtime, Principal(kind="human", id="owner"), in_person=True),
         profile=json.dumps(data()))
    member = Ctx(runtime, Principal(kind="human", id="m_1", owner="m_1"), in_person=True)
    member.grant = NS(owner_token=False)
    assert call(tools.profile_update, member, profile="biz-a/ada", changes="headline: x")["state"] == "draft"


def test_every_site_says_where_its_login_lives():
    rows = {r["site"]: r for r in mapping.sites()}
    assert rows["linkedin"]["domain"] == "linkedin.com" and rows["x"]["domain"] == "x.com"
    assert {"name": "bio", "source": "bio.short", "limit": 150, "people_only": False} in \
        rows["instagram"]["fields"]
    assert "blog" not in rows


def test_the_sections_import_on_their_own():
    """profiles.sections and profiles.models build on profiles.fields, never on each other in a circle."""
    import subprocess
    import sys

    for module in ("openajar.profiles.sections", "openajar.profiles.models", "openajar.profiles.fields"):
        done = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True, check=False)
        assert done.returncode == 0, done.stderr
