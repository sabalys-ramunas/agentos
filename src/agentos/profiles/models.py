"""Profiles: one person or business, written once, that many accounts take their public profile from.

    profile: acme                     # a slug, unique in its workspace
    workspace: acme
    kind: business                    # person | business
    display_name: Acme Ltd
    name: {first: Ada, last: Lovelace}   # people only: the name sites with first and last take
    headline: Coffee gear, made in Vilnius
    bio:
      short: Coffee gear, made in Vilnius.            # X, Instagram, Reddit style bios
      long: |                                         # LinkedIn About, Facebook intro
        ...
    phone: "+37060000000"
    email: hello@acme.example
    website: https://acme.example
    location: Vilnius, Lithuania
    links: {shop: https://acme.example/shop}
    photos: {avatar: sha256:..., cover: sha256:...}   # files kept by the profile store (put_photo)
    voice:                            # how agents write as this profile
      personality: Dry, confident, roasts itself before anyone else can.
      tone: short, plain, a little cheeky
      do: [answer the actual question first]
      dont: [discounts in replies, politics]
      emoji: sparing
      examples:
        - {context: a customer says the grinder is loud, reply: "It is. That is the sound of 40 mm burrs working."}
    accounts:
      - surface: acme/linkedin
        overrides: {headline: Coffee gear for offices}
      - surface: acme/instagram

Profiles of kind product, reviewer and audience (docs/design/studio.md, decision 6) carry
the sections in agentos.profiles.sections instead: brand, guidelines, references and
environments for a product, a rubric for a reviewer, a screen, language and network for an
audience. They have no accounts and never sync to a site.

A profile is data, validated here and hashed like a program (`content_hash` over the canonical
JSON). Every save is a new numbered version; the latest approved one is the profile (see
agentos.profiles.service for who approves).

Nothing in a profile is hidden: agents read every field, and every field a site has is sent to
it. Nothing here is a credential either: an account is a surface, and its login stays in the
vault, bound to the account's lane.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentos.core.models import LANE_ID_RE, parse_lane_id

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
PHOTO_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PHONE_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[a-z]{2,}$", re.IGNORECASE)
LINK_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
FIELD_PATH_RE = re.compile(r"^[a-z_]+(\.[a-z_]+)?$")

# What a field may hold. The site limits (agentos.engage.models.PROFILE_LIMITS for LinkedIn) are
# checked when a profile is resolved for an account, so one long bio can still serve sites that
# take it while a site that does not reports a resolve problem.
LIMITS = {"display_name": 100, "first": 50, "last": 50, "headline": 220, "bio.short": 300, "bio.long": 2600,
          "location": 100, "url": 300, "voice.personality": 1000, "voice.tone": 200, "voice.rule": 200,
          "voice.example": 500, "item": 100, "work.description": 2000}


class ProfileKind(StrEnum):
    PERSON = "person"
    BUSINESS = "business"
    PRODUCT = "product"          # a thing being built, with a brand
    REVIEWER = "reviewer"        # a judging job an agent does (words only: it grants nothing)
    AUDIENCE = "audience"        # a kind of user: screen, language, network, goals


# The kinds whose profile is shown on social accounts; the others never sync to a site.
SOCIAL_KINDS = frozenset({ProfileKind.PERSON, ProfileKind.BUSINESS})
# Which kinds each studio section belongs to.
SECTION_KINDS = {
    "brand": frozenset({ProfileKind.PRODUCT, ProfileKind.BUSINESS}),
    "guidelines": frozenset({ProfileKind.PRODUCT, ProfileKind.BUSINESS, ProfileKind.PERSON}),
    "references": frozenset({ProfileKind.PRODUCT, ProfileKind.BUSINESS, ProfileKind.PERSON}),
    "environments": frozenset({ProfileKind.PRODUCT}),
    "reviewer": frozenset({ProfileKind.REVIEWER}),
    "audience": frozenset({ProfileKind.AUDIENCE}),
}
# A person's own fields: a business, product, reviewer or audience has none of them.
PEOPLE_ONLY = ("name", "birth_date", "work_history", "education")


class ProfileState(StrEnum):
    DRAFT = "draft"              # waits for the owner
    APPROVED = "approved"        # the profile (one per profile id)
    SUPERSEDED = "superseded"    # an approved version a later one replaced
    REJECTED = "rejected"        # the owner turned it down


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def clean_text(value: Any, what: str, limit: int, *, lines: bool = False) -> str:
    """A value as it will be typed on a site: trimmed, within `limit`, and free of control and
    invisible characters (they hide text from the owner who approves it). Line breaks only where
    `lines` allows them."""
    text = str(value).replace("\r\n", "\n")
    if lines:
        text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = unicodedata.normalize("NFC", text.strip())
    if not text:
        raise ValueError(f"{what} is empty (leave it out instead)")
    if len(text) > limit:
        raise ValueError(f"{what} has {len(text)} characters; at most {limit}")
    for ch in text:
        if lines and ch == "\n":
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp"):
            raise ValueError(f"{what} has a control or invisible character (U+{ord(ch):04X}); write plain text")
    return text


def _opt(value: Any, what: str, limit: int, *, lines: bool = False) -> str | None:
    return None if value is None else clean_text(value, what, limit, lines=lines)


def clean_url(value: Any, what: str) -> str:
    url = clean_text(value, what, LIMITS["url"])
    if not re.match(r"^https?://[^\s/$.?#][^\s]*$", url, re.IGNORECASE):
        raise ValueError(f"{what} must be a full link starting with https://, not {url[:40]!r}")
    return url


class Name(_Strict):
    first: str
    last: str

    @field_validator("first", "last")
    @classmethod
    def _part(cls, v: str, info: Any) -> str:
        return clean_text(v, f"name.{info.field_name}", LIMITS[info.field_name])


class Bio(_Strict):
    short: str | None = None     # one or two lines
    long: str | None = None      # an About section

    @field_validator("short")
    @classmethod
    def _short(cls, v: str | None) -> str | None:
        return _opt(v, "bio.short", LIMITS["bio.short"], lines=True)

    @field_validator("long")
    @classmethod
    def _long(cls, v: str | None) -> str | None:
        return _opt(v, "bio.long", LIMITS["bio.long"], lines=True)


class Photos(_Strict):
    avatar: str | None = None    # sha256:<hex> of a file in the profile store
    cover: str | None = None

    @field_validator("avatar", "cover")
    @classmethod
    def _photo(cls, v: str | None, info: Any) -> str | None:
        if v is not None and not PHOTO_RE.match(str(v).strip()):
            raise ValueError(f"photos.{info.field_name} is a photo the profile store keeps (sha256: and 64 hex "
                             f"digits, from profile_photo_put), not {str(v)[:40]!r}")
        return None if v is None else str(v).strip()


class Example(_Strict):
    context: str
    reply: str

    @field_validator("context", "reply")
    @classmethod
    def _text(cls, v: str, info: Any) -> str:
        return clean_text(v, f"voice.examples.{info.field_name}", LIMITS["voice.example"], lines=True)


class Voice(_Strict):
    """How agents write as this profile: the brand personality a comment, reply or post follows."""
    personality: str | None = None
    tone: str | None = None
    do: list[str] = Field(default_factory=list, max_length=20)
    dont: list[str] = Field(default_factory=list, max_length=20)
    emoji: Literal["none", "sparing", "often"] | None = None
    topics: list[str] = Field(default_factory=list, max_length=20)
    examples: list[Example] = Field(default_factory=list, max_length=10)

    @field_validator("personality")
    @classmethod
    def _personality(cls, v: str | None) -> str | None:
        return _opt(v, "voice.personality", LIMITS["voice.personality"], lines=True)

    @field_validator("tone")
    @classmethod
    def _tone(cls, v: str | None) -> str | None:
        return _opt(v, "voice.tone", LIMITS["voice.tone"])

    @field_validator("do", "dont", "topics")
    @classmethod
    def _rules(cls, v: list[str], info: Any) -> list[str]:
        return [clean_text(x, f"voice.{info.field_name}", LIMITS["voice.rule"]) for x in v]

    def empty(self) -> bool:
        return not (self.personality or self.tone or self.do or self.dont or self.emoji or self.topics
                    or self.examples)

    def prompt(self, display_name: str) -> str:
        """The voice as plain instructions for whoever writes as the profile (an agent, a draft
        provider). Built from the owner's own words only."""
        out = [f"Write as {display_name}."]
        if self.personality:
            out.append(f"Personality: {self.personality}")
        if self.tone:
            out.append(f"Tone: {self.tone}")
        if self.do:
            out.append("Do: " + "; ".join(self.do))
        if self.dont:
            out.append("Never: " + "; ".join(self.dont))
        if self.emoji:
            out.append({"none": "Emoji: none.", "sparing": "Emoji: at most one, and only when it adds something.",
                        "often": "Emoji: welcome."}[self.emoji])
        if self.topics:
            out.append("Usual topics: " + ", ".join(self.topics))
        for ex in self.examples:
            out.append(f"Example. To: {ex.context}\nReply: {ex.reply}")
        return "\n".join(out)


class Job(_Strict):
    company: str
    title: str
    start: str | None = None      # YYYY-MM
    end: str | None = None        # YYYY-MM, None while current
    description: str | None = None

    @field_validator("company", "title")
    @classmethod
    def _text(cls, v: str, info: Any) -> str:
        return clean_text(v, f"work_history.{info.field_name}", LIMITS["item"])

    @field_validator("start", "end")
    @classmethod
    def _month(cls, v: str | None, info: Any) -> str | None:
        if v is not None and not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", str(v)):
            raise ValueError(f"work_history.{info.field_name} is a month as YYYY-MM")
        return None if v is None else str(v)

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str | None) -> str | None:
        return _opt(v, "work_history.description", LIMITS["work.description"], lines=True)


class School(_Strict):
    school: str
    degree: str | None = None
    start: str | None = None
    end: str | None = None

    @field_validator("school", "degree")
    @classmethod
    def _text(cls, v: str | None, info: Any) -> str | None:
        return _opt(v, f"education.{info.field_name}", LIMITS["item"])


class Attachment(_Strict):
    """An account that takes its public profile from this one."""
    surface: str                                  # workspace/name, as a lane or engage program names it
    site: str | None = None                       # from the surface name when left out
    overrides: dict[str, str] = Field(default_factory=dict)   # field path -> the value this account shows
    map: dict[str, str] = Field(default_factory=dict)         # site field -> profile field path

    @field_validator("surface")
    @classmethod
    def _surface(cls, v: str) -> str:
        if not LANE_ID_RE.match(v):
            raise ValueError(f"account {v!r} must look like 'workspace/name', for example 'acme/linkedin'")
        return v

    @field_validator("overrides", "map")
    @classmethod
    def _paths(cls, v: dict[str, str], info: Any) -> dict[str, str]:
        out = {}
        for key, value in v.items():
            if not FIELD_PATH_RE.match(key):
                raise ValueError(f"accounts.{info.field_name} key {key!r} is a field like 'headline' or 'bio.short'")
            if info.field_name == "map" and not FIELD_PATH_RE.match(str(value)):
                raise ValueError(f"accounts.map value {value!r} is a profile field like 'bio.short'")
            out[key] = clean_text(value, f"accounts.{info.field_name}.{key}", LIMITS["bio.long"], lines=True)
        return out

    @model_validator(mode="after")
    def _site(self) -> Attachment:
        if self.site is None:
            self.site = parse_lane_id(self.surface)[1].split("-", 1)[0]
        return self


# the sections build on the helpers above, so they are imported here, not at the top
from agentos.profiles.sections import (
    Audience,
    Brand,
    Environment,
    Guideline,
    Reference,
    Reviewer,
    environments_ok,
    guidelines_ok,
    references_ok,
    refuse_secrets,
)


class Profile(_Strict):
    id: str = Field(alias="profile")
    workspace: str
    kind: ProfileKind = ProfileKind.PERSON
    display_name: str
    name: Name | None = None
    headline: str | None = None
    bio: Bio = Field(default_factory=Bio)
    birth_date: date | None = None
    phone: str | None = None
    email: str | None = None
    location: str | None = None
    website: str | None = None
    links: dict[str, str] = Field(default_factory=dict)
    languages: list[str] = Field(default_factory=list, max_length=10)
    photos: Photos = Field(default_factory=Photos)
    work_history: list[Job] = Field(default_factory=list, max_length=30)
    education: list[School] = Field(default_factory=list, max_length=20)
    skills: list[str] = Field(default_factory=list, max_length=50)
    voice: Voice = Field(default_factory=Voice)
    accounts: list[Attachment] = Field(default_factory=list, max_length=50)
    # studio sections (agentos.profiles.sections); empty ones stay out of the hash
    brand: Brand | None = None
    guidelines: list[Guideline] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    environments: list[Environment] = Field(default_factory=list)
    reviewer: Reviewer | None = None
    audience: Audience | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not PROFILE_ID_RE.match(v):
            raise ValueError("profile must be a lowercase name with dashes, like 'acme' or 'ada'")
        return v

    @field_validator("workspace")
    @classmethod
    def _ws(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,30}$", v):
            raise ValueError("workspace must be a workspace name, like 'acme'")
        return v

    @field_validator("display_name")
    @classmethod
    def _display(cls, v: str) -> str:
        return clean_text(v, "display_name", LIMITS["display_name"])

    @field_validator("headline")
    @classmethod
    def _headline(cls, v: str | None) -> str | None:
        return _opt(v, "headline", LIMITS["headline"])

    @field_validator("location")
    @classmethod
    def _location(cls, v: str | None) -> str | None:
        return _opt(v, "location", LIMITS["location"])

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        digits = re.sub(r"[\s().-]", "", str(v))
        if not PHONE_RE.match(digits):
            raise ValueError("phone is an international number starting with + and the country code, "
                             "like +37060000000")
        return digits

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        text = clean_text(v, "email", 254)
        if not EMAIL_RE.match(text):
            raise ValueError(f"email {text[:40]!r} is not an email address")
        return text

    @field_validator("website")
    @classmethod
    def _website(cls, v: str | None) -> str | None:
        return None if v is None else clean_url(v, "website")

    @field_validator("links")
    @classmethod
    def _links(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 20:
            raise ValueError("links holds at most 20 links")
        out = {}
        for key, url in v.items():
            if not LINK_KEY_RE.match(key):
                raise ValueError(f"links key {key!r} is a short lowercase name like 'shop' or 'youtube'")
            out[key] = clean_url(url, f"links.{key}")
        return out

    @field_validator("languages")
    @classmethod
    def _langs(cls, v: list[str]) -> list[str]:
        for lang in v:
            if not re.match(r"^[a-z]{2}$", lang):
                raise ValueError(f"language {lang!r} must be a two-letter code like 'lt' or 'en'")
        return v

    @field_validator("skills")
    @classmethod
    def _skills(cls, v: list[str]) -> list[str]:
        return [clean_text(s, "skills", LIMITS["item"]) for s in v]

    @field_validator("guidelines")
    @classmethod
    def _guidelines(cls, v: list[Guideline]) -> list[Guideline]:
        return guidelines_ok(v)

    @field_validator("references")
    @classmethod
    def _references(cls, v: list[Reference]) -> list[Reference]:
        return references_ok(v)

    @field_validator("environments")
    @classmethod
    def _environments(cls, v: list[Environment]) -> list[Environment]:
        return environments_ok(v)

    @model_validator(mode="after")
    def _check(self) -> Profile:
        if self.kind not in SOCIAL_KINDS:
            for f in PEOPLE_ONLY:
                if getattr(self, f):
                    raise ValueError(f"a {self.kind.value} profile has no {f} (a person's own field); leave it out")
            if self.accounts:
                raise ValueError(f"a {self.kind.value} profile is not shown on social accounts; leave `accounts` "
                                 "out (attach accounts to a person or business profile)")
        for section, kinds in SECTION_KINDS.items():
            if getattr(self, section) and self.kind not in kinds:
                names = " or ".join(sorted(k.value for k in kinds))
                raise ValueError(f"{section} belongs to a {names} profile, not a {self.kind.value} one")
        for section in SECTION_KINDS:
            value = getattr(self, section)
            if value:
                refuse_secrets(value.model_dump(mode="json") if hasattr(value, "model_dump")
                               else [x.model_dump(mode="json") for x in value], section)
        if self.kind is ProfileKind.BUSINESS and self.name is not None:
            raise ValueError("a business has a display_name, not a first and last name; leave `name` out")
        if self.kind is ProfileKind.BUSINESS and self.birth_date is not None:
            raise ValueError("a business has no birth date; leave `birth_date` out")
        seen: set[str] = set()
        for a in self.accounts:
            if a.surface in seen:
                raise ValueError(f"account {a.surface} is listed twice")
            seen.add(a.surface)
            if parse_lane_id(a.surface)[0] != self.workspace:
                raise ValueError(f"account {a.surface} is in another workspace; a profile's accounts are in "
                                 f"its own workspace ({self.workspace})")
        return self

    # ---- reading ----
    def value(self, path: str) -> Any:
        """A field by its path (`headline`, `bio.short`, `photos.avatar`, `name`), None when unset."""
        obj: Any = self
        for part in path.split("."):
            obj = getattr(obj, part, None) if obj is not None else None
        return obj

    def attachment(self, surface: str) -> Attachment | None:
        return next((a for a in self.accounts if a.surface == surface), None)

    def voice_prompt(self) -> str | None:
        return None if self.voice.empty() else self.voice.prompt(self.display_name)

    def check_rules(self) -> dict[str, float | None]:
        """The checked guidelines as {check id: threshold} (agentos.studio.contracts.CheckContext.rules)."""
        return {g.check: g.threshold for g in self.guidelines if g.check}

    # ---- identity ----
    def canonical(self) -> dict[str, Any]:
        out = self.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=False)
        for section in SECTION_KINDS:      # a profile without them hashes as it did before they existed
            if out.get(section) in ([], {}):
                out.pop(section)
        return out

    def content_hash(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


class ProfileVersion(BaseModel):
    definition: Profile
    version: int
    hash: str
    state: ProfileState
    created_at: str
    created_by: dict[str, Any] | None = None
    decided_at: str | None = None
    decided_by: str | None = None
    note: str | None = None
    reason: str | None = None        # why an agent proposed it (profile_propose)

    def public(self) -> dict[str, Any]:
        return {"profile": self.definition.id, "workspace": self.definition.workspace, "version": self.version,
                "hash": self.hash, "state": self.state.value, "created_at": self.created_at,
                "created_by": (self.created_by or {}).get("id"), "decided_at": self.decided_at,
                "decided_by": self.decided_by, "note": self.note,
                **({"reason": self.reason} if self.reason else {}),
                "definition": self.definition.canonical()}
