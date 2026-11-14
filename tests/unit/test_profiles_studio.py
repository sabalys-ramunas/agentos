"""Profiles that carry a brand, and working as a profile (docs/design/studio.md, decisions 6 to 8).

The new kinds (product, reviewer, audience) and sections (brand, guidelines, references,
environments, reviewer, audience), what each refuses, profile_use's slices per task (and what
must be absent from each), profile_propose landing as a draft only the owner approves, and old
profiles loading and hashing as before."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest
from test_engage_executor import OWNER
from test_profiles import AGENT, Ctx, call, data

from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Effect, Principal
from agentos.core.runtime import Runtime
from agentos.engage.errors import OwnerOnly
from agentos.ops import migrate
from agentos.profiles import mapping, tools
from agentos.profiles.context import TOKENS_INLINE_CHARS, profile_diff
from agentos.profiles.errors import ProfileInvalid, ProfileSecret
from agentos.profiles.migrations import MIGRATIONS
from agentos.profiles.models import Attachment, Profile, ProfileKind
from agentos.profiles.service import ProfilesService, build, merge, parse_profile
from agentos.profiles.store import ProfileStore
from agentos.studio.contracts import ComponentRule, ThemeSwitch

# A fake Stripe live key, put together at run time so the source holds nothing a secret scanner
# (GitHub push protection) takes for a real one.
FAKE_STRIPE = "sk_" + "live_" + "51HxYzAbCdEfGhIjKlMnOpQr"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'
WOFF2 = b"wOF2" + b"\x00" * 60

TOKENS = {
    "color": {
        "$type": "color",
        "brand": {"500": {"$value": "#3366CC", "$description": "The accent"}},
        "text": {"primary": {"$value": "{color.brand.500}"}},
        "surface": {"$value": {"colorSpace": "oklch", "components": [0.98, 0.01, 250]}},
    },
    "space": {"$type": "dimension", "2": {"$value": "8px"}, "4": {"$value": {"value": 1, "unit": "rem"}}},
    "motion": {"fast": {"$type": "duration", "$value": "150ms"}},
}

PRODUCT = {
    "profile": "shop", "workspace": "biz-a", "kind": "product", "display_name": "Acme Shop",
    "headline": "Booking for small salons",
    "brand": {
        "tokens": TOKENS,
        "fonts": [{"family": "Inter", "role": "body", "weights": [600, 400, 400]}],
        "components": [{"name": "Button", "attr": "data-slot=button"}, {"name": "Card", "class_pattern": "^card(-|$)"}],
        "theme": {"how": "attr", "name": "data-theme", "light": "light", "dark": "dark"},
        "widths": [1280, 360, 768],
        "engine": "chromium",
    },
    "guidelines": [
        {"id": "text-contrast", "text": "Body text is easy to read", "check": "contrast.text", "threshold": 4.5},
        {"id": "tap", "text": "Every control is easy to tap", "check": "target.size", "threshold": 44},
        {"id": "calm", "text": "One accent colour per screen", "advisory": True},
    ],
    "references": [{"name": "Linear", "url": "https://linear.app", "note": "the density we want"}],
    "environments": [
        {"name": "local", "url": "http://localhost:3000"},
        {"name": "staging", "url": "https://staging.acme.example", "keys": {"stripe": "biz-a/stripe-test"}},
    ],
}

REVIEWER = {
    "profile": "strict-eye", "workspace": "biz-a", "kind": "reviewer", "display_name": "Strict eye",
    "reviewer": {
        "cares_about": ["clear hierarchy", "nothing cramped on a phone"],
        "rubric": [{"id": "hierarchy", "question": "Is the most important thing the most visible?", "weight": 3}],
        "examples": [{"situation": "Two equal buttons", "verdict": "worse", "why": "no primary action"}],
    },
}

AUDIENCE = {
    "profile": "salon-owner", "workspace": "biz-a", "kind": "audience", "display_name": "Salon owner",
    "audience": {"screen_width": 360, "language": "lt", "network": "slow_3g",
                 "goals": ["book a client in under a minute"], "hard": ["small text", "English words"]},
}


def product(**changes):
    return merge(PRODUCT, changes)


def approved(tmp_path, *profiles):
    svc = ProfilesService(home=tmp_path)
    for p in profiles:
        svc.save(p, by=OWNER, as_owner=True)
    return svc


# ---------------------------------------------------------------- old profiles

def test_an_old_profile_loads_and_hashes_as_before():
    p = parse_profile(data())
    assert p.content_hash() == "sha256:1c7b0ba43726a8cd"        # the hash before studio sections existed
    assert not {"brand", "guidelines", "references", "environments", "reviewer", "audience"} & set(p.canonical())


def test_a_store_from_before_the_reason_column_migrates_and_loads(tmp_path):
    old = replace(MIGRATIONS, migrations=MIGRATIONS.migrations[:3])
    migrate.migrate(tmp_path, [old], backup=False)
    body = parse_profile(data())
    with sqlite3.connect(tmp_path / MIGRATIONS.db) as conn:
        conn.execute("INSERT INTO profiles (workspace, id, version, hash, body, state, created_at) "
                     "VALUES ('biz-a', 'ada', 1, ?, ?, 'approved', '2026-01-01T00:00:00+00:00')",
                     (body.content_hash(), json.dumps(body.canonical())))
    store = ProfileStore(tmp_path)
    pv = store.active("biz-a", "ada")
    assert pv.definition.display_name == "Ada Lovelace" and pv.reason is None and "reason" not in pv.public()
    assert store.put(body, created_by=None)[1] is False          # the same profile is the same version


# ---------------------------------------------------------------- kinds and sections

def test_a_product_with_a_brand_validates_and_normalizes_its_tokens():
    p = parse_profile(PRODUCT)
    assert p.kind is ProfileKind.PRODUCT
    tokens = p.brand.tokens
    assert tokens["color"]["brand"]["500"]["$value"] == {"colorSpace": "srgb", "components": [0.2, 0.4, 0.8],
                                                         "hex": "#3366cc"}
    assert tokens["space"]["2"]["$value"] == {"value": 8, "unit": "px"}
    assert tokens["motion"]["fast"]["$value"] == {"value": 150, "unit": "ms"}
    assert tokens["color"]["text"]["primary"]["$value"] == "{color.brand.500}"
    rooted = parse_profile(product(brand={"tokens": {"accent": {"$type": "color", "$root": {"$value": "#000"},
                                                                "x": {"$value": "{accent}"}}}}))
    assert rooted.brand.tokens["accent"]["$root"]["$value"]["hex"] == "#000000"
    assert p.brand.widths == [360, 768, 1280] and p.brand.fonts[0].weights == [400, 600]
    assert p.check_rules() == {"contrast.text": 4.5, "target.size": 44}
    assert p.brand.component_rules() == [ComponentRule(name="Button", attr="data-slot=button"),
                                         ComponentRule(name="Card", class_pattern="^card(-|$)")]
    assert p.brand.theme_switch() == ThemeSwitch(how="attr", name="data-theme", light="light", dark="dark")


@pytest.mark.parametrize("tokens, words", [
    ({"a": {"$type": "color", "b": {"$value": "{a.missing}"}}}, "names no token"),
    ({"a": {"$value": "#fff"}}, "give it a \\$type"),
    ({"a": {"$type": "colour", "$value": "#fff"}}, "not a DTCG type"),
    ({"a.b": {"$type": "color", "$value": "#fff"}}, "not a token name"),
    ({"a": {"$type": "color", "$value": "#fff", "$secret": 1}}, "not a DTCG property"),
    ({"a": {"$type": "color", "$value": "#fff", "b": {"$value": "#000"}}}, "holds no other tokens"),
    ({"a": {"$type": "color", "$value": "red"}}, "hex string"),
    ({"a": {"$type": "dimension", "$value": "16pt"}}, "dimension is like"),
    ({"a": "#fff"}, "not a bare value"),
    ({"a": {"$type": "color", "$value": "#fff", "$description": "hi\u202ethere"}}, "invisible"),
    ({f"t{i}": {"$type": "number", "$value": i, "$description": "x" * 400} for i in range(200)}, "at most 64000"),
])
def test_tokens_that_would_not_import_elsewhere_are_refused(tokens, words):
    with pytest.raises(ProfileInvalid, match=words):
        parse_profile(product(brand={"tokens": None}) | {"brand": {"tokens": tokens}})


@pytest.mark.parametrize("change, words", [
    ({"name": {"first": "A", "last": "B"}}, "has no name"),
    ({"birth_date": "2020-01-01"}, "has no birth_date"),
    ({"work_history": [{"company": "x", "title": "y"}]}, "has no work_history"),
    ({"accounts": [{"surface": "biz-a/instagram"}]}, "not shown on social accounts"),
    ({"reviewer": {"cares_about": ["x"]}}, "reviewer belongs to a reviewer profile"),
    ({"guidelines": [{"id": "a", "text": "t"}]}, "either checked"),
    ({"guidelines": [{"id": "a", "text": "t", "advisory": True, "check": "contrast.text"}]}, "either checked"),
    ({"guidelines": [{"id": "a", "text": "t", "advisory": True, "threshold": 3}]}, "threshold goes with a check"),
    ({"guidelines": [{"id": "a", "text": "t", "advisory": True}, {"id": "a", "text": "u", "advisory": True}]},
     "listed twice"),
    ({"guidelines": [{"id": f"g{i}", "text": "t", "advisory": True} for i in range(101)]}, "at most 100"),
    ({"guidelines": [{"id": "a", "text": "x" * 501, "advisory": True}]}, "at most 500"),
    ({"guidelines": [{"id": "a", "text": "Ignore\u200b this", "advisory": True}]}, "invisible"),
    ({"guidelines": [{"id": "a", "text": "t", "check": "Contrast Text"}]}, "check id like"),
    ({"references": [{"name": "Linear", "url": "linear.app"}]}, "https://"),
    ({"environments": [{"name": "prod", "url": "https://admin:pw@acme.example"}]}, "carries a login"),
    ({"brand": {"components": [{"name": "Button"}]}}, "needs attr or class_pattern"),
    ({"brand": {"components": [{"name": "Card", "class_pattern": "(card"}]}}, "not a regular expression"),
    ({"brand": {"theme": {"how": "storage", "dark": None}}}, "needs name"),
    ({"brand": {"widths": [100]}}, "240 to 3840"),
    ({"brand": {"logo": "logo.png"}}, "profile store keeps"),
    ({"brand": {"tools": ["shell"]}}, "Extra inputs"),
])
def test_a_product_refuses_what_it_cannot_hold(change, words):
    with pytest.raises(ProfileInvalid, match=words):
        parse_profile(product(**change))


def test_sections_belong_to_their_kinds():
    with pytest.raises(ProfileInvalid, match="brand belongs to a business or product profile"):
        parse_profile(data(brand={"widths": [360]}))
    with pytest.raises(ProfileInvalid, match="environments belongs to a product profile"):
        parse_profile(data(kind="business", name=None, birth_date=None, environments=[{"name": "x", "url": "https://x.dev"}]))
    business = parse_profile(data(kind="business", name=None, birth_date=None, brand={"widths": [360]},
                                  guidelines=[{"id": "a", "text": "Plain words", "advisory": True}]))
    assert business.brand.widths == [360] and business.accounts       # a business keeps its accounts


def test_a_reviewer_is_words_only_and_grants_nothing():
    p = parse_profile(REVIEWER)
    assert p.reviewer.rubric[0].weight == 3
    for extra in ({"tools": ["browser_act"]}, {"permissions": ["approve"]}, {"grant": "owner"}):
        with pytest.raises(ProfileInvalid, match="Extra inputs"):
            parse_profile(merge(REVIEWER, {"reviewer": extra}))
    for section in ({"brand": {"widths": [360]}}, {"environments": [{"name": "x", "url": "https://x.dev"}]},
                    {"guidelines": [{"id": "a", "text": "t", "advisory": True}]}):
        with pytest.raises(ProfileInvalid, match="belongs to"):
            parse_profile(merge(REVIEWER, section))
    assert set(Profile.model_fields) & {"tools", "permissions", "grants", "scopes"} == set()


def test_an_audience_validates():
    p = parse_profile(AUDIENCE)
    assert p.audience.network == "slow_3g" and p.audience.language == "lt"
    for bad, words in [({"language": "Lithuanian"}, "language tag"), ({"network": "dialup"}, "network"),
                       ({"screen_width": 100}, "greater than or equal"),
                       ({"hard": ["x"] * 11}, "at most 10")]:
        with pytest.raises(ProfileInvalid, match=words):
            parse_profile(merge(AUDIENCE, {"audience": bad}))


@pytest.mark.parametrize("change", [
    {"environments": [{"name": "prod", "url": "https://acme.example", "keys": {"stripe": FAKE_STRIPE}}]},
    {"environments": [{"name": "prod", "url": "https://acme.example/?token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123def456"}]},
    {"guidelines": [{"id": "a", "text": "use key AKIAIOSFODNN7EXAMPLE", "advisory": True}]},
    {"references": [{"name": "x", "url": "https://x.dev", "note": "password=hunter2"}]},
])
def test_anything_that_looks_like_a_secret_is_refused_by_name(change):
    with pytest.raises(ProfileSecret, match="looks like it holds a secret") as e:
        parse_profile(product(**change))
    assert e.value.code == "secret_in_profile"
    assert "sk_live" not in str(e.value) and "hunter2" not in str(e.value) and "AKIA" not in str(e.value)


def test_the_new_kinds_never_sync_to_a_site():
    p = Profile.model_construct(**parse_profile(PRODUCT).__dict__)     # past the model's own refusal
    r = mapping.resolve(p, Attachment(surface="biz-a/instagram"))
    assert r.values == {} and r.engine == [] and "not shown on social accounts" in r.problems[0]
    for kind in ("product", "reviewer", "audience"):
        p.kind = ProfileKind(kind)
        assert mapping.resolve(p, Attachment(surface="biz-a/linkedin")).values == {}


# ---------------------------------------------------------------- brand files

def test_brand_files_are_kept_like_photos_and_checked_by_field(tmp_path):
    svc = ProfilesService(home=tmp_path)
    logo = svc.put_photo("biz-a", "logo.svg", SVG)
    font = svc.put_photo("biz-a", "inter.woff2", WOFF2)
    photo = svc.put_photo("biz-a", "me.png", PNG)
    assert (logo["type"], font["type"]) == ("image/svg+xml", "font/woff2")
    out = svc.save(product(brand={"logo": logo["photo"], "assets": {"mark": photo["photo"]},
                                  "fonts": [{"family": "Inter", "file": font["photo"]}]}), by=OWNER, as_owner=True)
    assert out["state"] == "approved"
    with pytest.raises(ProfileInvalid, match="it takes a font"):
        svc.save(product(brand={"fonts": [{"family": "Inter", "file": photo["photo"]}]}), by=OWNER, as_owner=True)
    with pytest.raises(ProfileInvalid, match="no stored file"):
        svc.save(product(brand={"logo": "sha256:" + "c" * 64}), by=OWNER, as_owner=True)
    with pytest.raises(ProfileInvalid, match="it takes a JPEG, PNG, WebP or GIF photo"):
        svc.save(data(photos={"avatar": logo["photo"]}), by=OWNER, as_owner=True)
    with pytest.raises(InvalidRequest, match="runs code"):
        svc.put_photo("biz-a", "bad.svg", b'<svg><script>alert(1)</script></svg>')
    with pytest.raises(InvalidRequest, match="runs code"):
        svc.put_photo("biz-a", "bad.svg", b'<svg><rect onload="x()"/></svg>')


# ---------------------------------------------------------------- profile_use

def test_design_gets_the_brand_checkable_rules_and_references_and_nothing_else(tmp_path):
    svc = approved(tmp_path, PRODUCT)
    out = svc.context("biz-a/shop", "design")
    ctx = out["context"]
    assert ctx["brand"]["tokens"]["color.brand.500"] == "#3366cc"
    assert ctx["brand"]["tokens"]["color.text.primary"] == "{color.brand.500}"
    assert ctx["brand"]["tokens"]["color.surface"] == "oklch(0.98 0.01 250)"
    assert ctx["brand"]["tokens"]["space.4"] == "1rem"
    assert ctx["brand"]["token_types"] == {"color": 3, "dimension": 2, "duration": 1}
    assert ctx["brand"]["components"][0] == {"name": "Button", "attr": "data-slot=button"}
    assert ctx["guidelines"]["checks"][0] == {"id": "text-contrast", "text": "Body text is easy to read",
                                              "check": "contrast.text", "threshold": 4.5}
    assert ctx["guidelines"]["advice"] == [{"id": "calm", "text": "One accent colour per screen"}]
    assert ctx["references"][0]["url"] == "https://linear.app"
    assert "environments" not in ctx and "voice_prompt" not in ctx and "staging.acme.example" not in json.dumps(ctx)
    left = {x["section"]: x for x in out["left_out"]}
    assert left["environments"]["for"] == ["build"] and left["environments"]["chars"] > 0
    assert out["workspace_rules"] == {"tool": "memory_context", "arguments": {"workspace": "biz-a", "task": "design"},
                                      "why": out["workspace_rules"]["why"]}
    assert out["size"]["tokens"] > 0 and out["size"]["chars"] >= len(json.dumps(ctx))
    assert "data" in out["data"] and "ui_check" in out["next"]
    assert "—" not in json.dumps(out)


def test_build_gets_environments_and_key_names_never_the_brand(tmp_path):
    svc = approved(tmp_path, PRODUCT)
    ctx = svc.context("biz-a/shop", "build")["context"]
    assert ctx["environments"][1] == {"name": "staging", "url": "https://staging.acme.example",
                                      "keys": {"stripe": "biz-a/stripe-test"}}
    assert "brand" not in ctx and "guidelines" not in ctx and "references" not in ctx


def test_write_gets_the_voice_and_accounts_never_private_fields(tmp_path):
    svc = approved(tmp_path, data())
    out = svc.context("biz-a/ada", "write")
    ctx = out["context"]
    assert ctx["voice_prompt"].startswith("Write as Ada Lovelace.")
    assert ctx["accounts"] == [{"surface": "biz-a/linkedin", "site": "linkedin"},
                               {"surface": "biz-a/instagram", "site": "instagram"}]
    text = json.dumps(out)
    for private in ("+37060000000", "ada@example.com", "1815-12-10", '"last"'):
        assert private not in text, private
    left = {x["section"] for x in out["left_out"]}
    assert {"phone", "email", "birth_date", "name"} <= left
    design = svc.context("biz-a/ada", "design")
    assert "voice_prompt" not in design["context"] and "accounts" not in design["context"]
    assert "+37060000000" not in json.dumps(design)


def test_review_gets_the_rubric_and_an_audience_is_served_to_every_task(tmp_path):
    svc = approved(tmp_path, REVIEWER, AUDIENCE, PRODUCT)
    review = svc.context("biz-a/strict-eye", "review")["context"]
    assert review["reviewer"]["rubric"][0]["id"] == "hierarchy"
    assert svc.context("biz-a/strict-eye", "design")["context"] == {}
    assert "nothing for design" in svc.context("biz-a/strict-eye", "design")["next"]
    for task in ("design", "build", "write", "review"):
        assert svc.context("biz-a/salon-owner", task)["context"]["audience"]["screen_width"] == 360
    product_review = svc.context("biz-a/shop", "review")["context"]
    assert set(product_review) == {"headline", "guidelines", "references"}


def test_large_token_sets_are_left_out_of_a_summary_and_given_in_full(tmp_path):
    many = {"color": {"$type": "color", **{f"c{i}": {"$value": f"#{i:06x}"} for i in range(400)}}}
    svc = approved(tmp_path, product(brand={"tokens": None}) | {"brand": {"tokens": many}})
    summary = svc.context("biz-a/shop", "design")
    assert "tokens" not in summary["context"]["brand"] and summary["context"]["brand"]["token_count"] == 400
    left = next(x for x in summary["left_out"] if x["section"] == "brand.tokens")
    assert left["chars"] > TOKENS_INLINE_CHARS and "detail='full'" in left["why"]
    full = svc.context("biz-a/shop", "design", detail="full")
    assert len(full["context"]["brand"]["tokens"]) == 400 and full["size"]["chars"] > summary["size"]["chars"]


def test_only_the_approved_version_is_served(tmp_path):
    svc = ProfilesService(home=tmp_path)
    svc.save(PRODUCT, by=AGENT, as_owner=False)
    with pytest.raises(NotFound, match="no approved version yet"):
        svc.context("biz-a/shop", "design")
    svc.decide("biz-a/shop", 1, approve=True, by=OWNER, in_person=True)
    svc.propose("biz-a/shop", {"references": {"add": [{"name": "Stripe", "url": "https://stripe.com"}]}},
                "Stripe's forms are the bar", by=AGENT)
    out = svc.context("biz-a/shop", "design")
    assert out["version"] == 1 and out["draft_waiting"] == 2
    assert [r["name"] for r in out["context"]["references"]] == ["Linear"]
    with pytest.raises(InvalidRequest, match="task is one of"):
        svc.context("biz-a/shop", "deploy")


# ---------------------------------------------------------------- profile_propose

def test_a_proposal_lands_as_a_draft_with_its_reason_and_diff(tmp_path):
    svc = approved(tmp_path, PRODUCT)
    out = svc.propose("biz-a/shop", """
guidelines:
  add:
    - {id: text-contrast, text: Body text is easy to read, check: contrast.text, threshold: 7}
    - {id: no-lorem, text: No placeholder text, advisory: true}
  remove: [tap]
brand:
  tokens: {color: {brand: {"500": {$value: "#2255bb"}}}}
  widths: [360, 1440]
""", "Contrast at 4.5 failed for the grey labels on the photos", by=AGENT)
    assert out["state"] == "draft" and out["version"] == 2 and out["reason"].startswith("Contrast at 4.5")
    assert "guideline no-lorem added: No placeholder text (advisory)" in out["diff"]
    assert "guideline tap removed" in out["diff"]
    assert any(line.startswith("guideline text-contrast changed:") and "at 4.5" in line and "at 7" in line
               for line in out["diff"])
    assert "brand.tokens.color.brand.500 changed: #3366cc -> #2255bb" in out["diff"]
    assert "brand.widths changed: [360, 1440]" in out["diff"]
    assert "You cannot approve it" in out["next"]
    assert svc.store.active("biz-a", "shop").version == 1
    shown = svc.get("biz-a/shop", version=2)
    assert shown["reason"].startswith("Contrast") and shown["diff"] == out["diff"]
    with pytest.raises(OwnerOnly):
        svc.decide("biz-a/shop", 2, approve=True, by=AGENT, in_person=True)
    with pytest.raises(OwnerOnly):
        svc.decide("biz-a/shop", 2, approve=True, by=OWNER, in_person=False)
    svc.decide("biz-a/shop", 2, approve=True, by=OWNER, in_person=True)
    p = svc.store.active("biz-a", "shop").definition
    assert [g.id for g in p.guidelines] == ["calm", "text-contrast", "no-lorem"] and p.check_rules()["contrast.text"] == 7
    assert p.brand.widths == [360, 1440] and p.brand.components         # the rest of the brand merged, not replaced


@pytest.mark.parametrize("change, reason, err, words", [
    ({"headline": "x"}, "r", ProfileInvalid, "headline change with profile_update"),
    ({"environments": []}, "r", ProfileInvalid, "environments change with profile_update"),
    ({}, "r", ProfileInvalid, "empty"),
    ({"references": {"remove": ["Nope"]}}, "r", ProfileInvalid, "names no item"),
    ({"references": {"replace": []}}, "r", ProfileInvalid, "add: "),
    ({"guidelines": {"add": [{"id": "x", "text": "t"}]}}, "r", ProfileInvalid, "either checked"),
    ({"references": [{"name": "Linear", "url": "https://linear.app", "note": "the density we want"}]}, "r",
     ProfileInvalid, "nothing to propose"),
    ({"references": {"add": [{"name": "x", "url": "https://x.dev"}]}}, "  ", ProfileInvalid, "reason"),
    ({"references": {"add": [{"name": "x", "url": "https://x.dev"}]}}, "the key is " + FAKE_STRIPE,
     ProfileSecret, "secret"),
])
def test_a_proposal_refuses_what_it_cannot_change(tmp_path, change, reason, err, words):
    svc = approved(tmp_path, PRODUCT)
    with pytest.raises(err, match=words):
        svc.propose("biz-a/shop", change, reason, by=AGENT)
    assert len(svc.store.versions("biz-a", "shop")) == 1


def test_the_tools_serve_slices_and_save_proposals_as_drafts_even_for_the_owner(tmp_path):
    runtime = Runtime()
    build(runtime, home=tmp_path)
    owner = Ctx(runtime, Principal(kind="human", id="owner"), in_person=True)
    agent = Ctx(runtime, Principal(kind="agent", id="agent-1"))
    call(tools.profile_save, owner, profile=json.dumps(PRODUCT))
    got = call(tools.profile_use, agent, profile="biz-a/shop", task="design")
    assert got["context"]["brand"]["tokens"]["color.brand.500"] == "#3366cc" and agent.seen[-1] == ("read", None)
    out = call(tools.profile_propose, owner, profile="biz-a/shop",
               change="references: {add: [{name: Stripe, url: 'https://stripe.com'}]}", reason="A better form bar")
    assert out["state"] == "draft" and out["diff"] == ["reference Stripe added: https://stripe.com"]
    assert call(tools.profile_propose, agent, profile="biz-a/shop",
                change={"brand": {"widths": [320]}}, reason="Older phones")["state"] == "draft"
    outsider = Ctx(runtime, Principal(kind="agent", id="other"), lanes="biz-b/")
    with pytest.raises(NotFound):
        call(tools.profile_use, outsider, profile="biz-a/shop", task="design")
    specs = {s.name: s for s in tools.tool_specs(None)}
    assert specs["profile_use"].effect is Effect.READ and specs["profile_propose"].effect is Effect.DRAFT
    assert not [n for n in specs if "approve" in n or "decide" in n]
    for name in ("profile_use", "profile_propose"):
        assert "—" not in (specs[name].handler.__doc__ or "")


def test_the_diff_reads_plainly():
    old, new = parse_profile(PRODUCT), parse_profile(product(headline="Booking for barbers", references=None))
    assert profile_diff(old, new) == ["headline changed: Booking for small salons -> Booking for barbers",
                                      "reference Linear removed"]
    assert profile_diff(None, new) == ["a new profile"]


def test_the_owner_reads_a_proposal_with_its_reason_and_changes_in_the_cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentos.profiles.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    svc = approved(tmp_path, PRODUCT)
    svc.propose("biz-a/shop", {"references": {"remove": ["Linear"]}}, "Linear is not our density after all",
                by=AGENT)
    svc.store.close()
    out = CliRunner().invoke(app, ["show", "biz-a/shop", "--version", "2"])
    assert out.exit_code == 0, out.output
    assert "# proposed because: Linear is not our density after all" in out.output
    assert "# change: reference Linear removed" in out.output
