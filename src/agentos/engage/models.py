"""Programs: the only thing an agent or the owner writes (docs/design/engagement-engine.md 5.2).

A program is data, validated here, hashed like a FlowDefinition (`content_hash` over the
canonical JSON), stored as numbered versions, and approved by the owner. A version is never
changed; an edit is a new version that needs approval again.

    program: biz-a-ig-coffee
    surface: biz-a/instagram
    risk: careful
    phases:
      - {kind: warm_up, days: 10}
      - kind: steady
        daily: {like: 15, comment: 3, follow: 2, story_view: 10}
        targets: [{source: hashtags, values: [specialtycoffee, latteart]}, {source: own_feed}]
        filters: {language: [lt, en], min_account_age_days: 90}
    comments: {mode: approve, topics: [coffee], max_chars: 140}
    profile:                       # optional, LinkedIn: the public profile the account should show
      name: {first: Ada, last: Lovelace}
      headline: Analyst of engines
      about: Notes on the analytical engine.
      photo: in_3f9a2c71           # a files_put id in the lane's inbox
      cover: in_8b1d0e44
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentos.core.models import LANE_ID_RE, parse_lane_id, utcnow
from agentos.engage.caps import CAPS, SITES, Kind, Risk, site_kinds

PROGRAM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
TAG_RE = re.compile(r"^[\w .'&-]{1,60}$")
LANG_RE = re.compile(r"^[a-z]{2}$")

# Section 5.6: comments and replies on these sites always wait for the owner; `auto` is refused.
APPROVE_ONLY_SITES = ("linkedin", "reddit")

# Section 5.3: topics a program never engages with, whatever it lists.
SENSITIVE_TOPICS = ("politics", "religion", "tragedy", "health_claims", "minors", "adult", "gambling",
                    "giveaways", "engagement_bait")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Mode(StrEnum):
    APPROVE = "approve"   # every draft waits for the owner
    AUTO = "auto"         # owner only; LinkedIn and Reddit never
    OFF = "off"


class Source(StrEnum):
    OWN_FEED = "own_feed"
    OWN_ENGAGERS = "own_engagers"
    OWN_POST_COMMENTS = "own_post_comments"
    HASHTAGS = "hashtags"
    TOPICS = "topics"
    COMMUNITIES = "communities"
    LOCATIONS = "locations"
    ACCOUNTS = "accounts"
    FOLLOWERS_OF = "followers_of"   # owner only, small when on (section 5.3)


VALUE_SOURCES = frozenset({Source.HASHTAGS, Source.TOPICS, Source.COMMUNITIES, Source.LOCATIONS,
                           Source.ACCOUNTS, Source.FOLLOWERS_OF})


class Target(_Strict):
    source: Source
    values: list[str] = Field(default_factory=list, max_length=50)
    lookback_days: int | None = Field(default=None, ge=1, le=90)

    @field_validator("values")
    @classmethod
    def _values(cls, v: list[str]) -> list[str]:
        out = []
        for raw in v:
            item = str(raw).strip().lstrip("#@")
            if not TAG_RE.match(item):
                raise ValueError(f"target value {raw!r} must be a plain name, tag or community (at most 60 characters)")
            out.append(item)
        return out

    @model_validator(mode="after")
    def _needs_values(self) -> Target:
        if self.source in VALUE_SOURCES and not self.values:
            raise ValueError(f"source {self.source.value} needs values (the tags, communities or accounts to use)")
        return self


class Filters(_Strict):
    language: list[str] = Field(default_factory=list, max_length=10)
    min_account_age_days: int | None = Field(default=None, ge=0, le=3650)
    min_posts: int | None = Field(default=None, ge=0, le=10000)
    max_following_ratio: float | None = Field(default=None, gt=0, le=100)
    active_within_days: int | None = Field(default=30, ge=1, le=365)
    skip_private: bool = True
    exclude_topics: list[str] = Field(default_factory=lambda: list(SENSITIVE_TOPICS), max_length=50)

    @field_validator("language")
    @classmethod
    def _langs(cls, v: list[str]) -> list[str]:
        for lang in v:
            if not LANG_RE.match(lang):
                raise ValueError(f"language {lang!r} must be a two-letter code like 'lt' or 'en'")
        return v

    @field_validator("exclude_topics")
    @classmethod
    def _sensitive_always(cls, v: list[str]) -> list[str]:
        # the sensitive list is a floor: a program can add topics, never take these away
        return list(dict.fromkeys([*SENSITIVE_TOPICS, *v]))


class WarmUp(_Strict):
    kind: Literal["warm_up"] = "warm_up"
    days: int = Field(default=14, ge=3, le=60)


class Steady(_Strict):
    kind: Literal["steady"] = "steady"
    daily: dict[Kind, int] = Field(default_factory=dict)     # targets, not promises
    targets: list[Target] = Field(default_factory=lambda: [Target(source=Source.OWN_FEED)], max_length=20)
    filters: Filters = Field(default_factory=Filters)

    @field_validator("daily")
    @classmethod
    def _daily(cls, v: dict[Kind, int]) -> dict[Kind, int]:
        for kind, n in v.items():
            if not 0 <= int(n) <= 1000:
                raise ValueError(f"daily {kind.value} must be between 0 and 1000")
        return {k: int(n) for k, n in sorted(v.items())}


Phase = Annotated[WarmUp | Steady, Field(discriminator="kind")]


class Comments(_Strict):
    mode: Mode = Mode.APPROVE
    voice: str | None = Field(default=None, max_length=200)    # e.g. memory:biz-a/voice
    topics: list[str] = Field(default_factory=list, max_length=20)
    max_chars: int = Field(default=140, ge=20, le=500)


class Replies(_Strict):
    mode: Mode = Mode.APPROVE
    within_hours: int = Field(default=6, ge=1, le=72)


class Community(_Strict):
    reply_to_comments: Replies = Field(default_factory=Replies)
    thank_new_followers: bool = False      # off: mass thank-yous are spam-shaped


class Posting(_Strict):
    calendar: str | None = Field(default=None, max_length=120)    # the content queue (section 5.7)
    min_gap_minutes: int = Field(default=180, ge=30, le=10080)


class Report(_Strict):
    daily: bool = True
    to: Literal["owner"] = "owner"


# Section `profile:` (brief B2). LinkedIn's own limits are not written down anywhere in this repo,
# so these are the brief's: names 1 to 50 characters each, headline 220, About 2600.
PROFILE_LIMITS = {"first": 50, "last": 50, "headline": 220, "about": 2600}
PROFILE_SITES = ("linkedin",)        # the sites with profile operations (connectors/linkedin profile_*)
PROFILE_FILE_RE = re.compile(r"^in_[0-9a-f]{6,40}$")   # a files_put id (agentos.node.files)
# a photo the profile store keeps (agentos.profiles), by its content: an account attached to a
# profile wants it, and the executor copies it into the lane's inbox only when the edit runs
PROFILE_PHOTO_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _profile_text(value: str, what: str, limit: int, *, lines: bool = False) -> str:
    """A profile value as it will be typed: trimmed, within `limit`, and free of control and
    invisible characters (format characters such as zero-width spaces and direction marks hide
    text from the person who approves it). The About section may have line breaks."""
    text = str(value).strip()
    if not text:
        raise ValueError(f"{what} is empty (leave it out to keep the account's own)")
    if len(text) > limit:
        raise ValueError(f"{what} has {len(text)} characters; LinkedIn takes at most {limit}")
    for ch in text:
        if lines and ch == "\n":
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp"):
            raise ValueError(f"{what} has a control or invisible character (U+{ord(ch):04X}); write plain text")
    return text


class ProfileName(_Strict):
    first: str
    last: str

    @field_validator("first", "last")
    @classmethod
    def _part(cls, v: str, info: Any) -> str:
        return _profile_text(v, f"profile.name.{info.field_name}", PROFILE_LIMITS[info.field_name])


class Profile(_Strict):
    """The public profile the account should show. Every field is optional: one left out is never
    changed. The engine edits a field only while what the account last showed differs from it.
    An edit of a program's own `profile:` waits for the owner's approval card; one whose values
    come from the approved profile the account is attached to (agentos.profiles) does not
    (agentos.engage.executor Executor._ask). A photo is a files_put id, or a profile photo's
    content hash (`sha256:...`), copied into the lane when the edit runs."""
    name: ProfileName | None = None
    headline: str | None = None
    about: str | None = None
    photo: str | None = None
    cover: str | None = None

    @field_validator("headline")
    @classmethod
    def _headline(cls, v: str | None) -> str | None:
        return None if v is None else _profile_text(v, "profile.headline", PROFILE_LIMITS["headline"])

    @field_validator("about")
    @classmethod
    def _about(cls, v: str | None) -> str | None:
        if v is None:
            return None
        text = "\n".join(line.rstrip() for line in str(v).replace("\r\n", "\n").split("\n"))
        return _profile_text(text, "profile.about", PROFILE_LIMITS["about"], lines=True)

    @field_validator("photo", "cover")
    @classmethod
    def _file(cls, v: str | None, info: Any) -> str | None:
        if v is not None and not (PROFILE_FILE_RE.match(str(v).strip()) or PROFILE_PHOTO_RE.match(str(v).strip())):
            raise ValueError(f"profile.{info.field_name} is a file id from files_put (in_ and hex digits), "
                             f"not {str(v)[:40]!r}")
        return None if v is None else str(v).strip()

    def fields(self) -> list[str]:
        """The fields it sets, in the order the engine edits them."""
        return [f for f in ("name", "headline", "about", "photo", "cover") if getattr(self, f) is not None]


class Program(_Strict):
    id: str = Field(alias="program")
    surface: str
    site: str | None = None          # instagram, facebook, ...; from the surface name when left out
    risk: Risk = Risk.CAREFUL
    timezone: str | None = None      # the lane's when left out (its pinned fingerprint), else UTC
    phases: list[Phase] = Field(min_length=1, max_length=2)
    comments: Comments = Field(default_factory=Comments)
    community: Community = Field(default_factory=Community)
    posting: Posting = Field(default_factory=Posting)
    report: Report = Field(default_factory=Report)
    profile: Profile | None = None   # the public profile it should show (brief B2); None: never edited

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not PROGRAM_ID_RE.match(v):
            raise ValueError("program must be a lowercase name with dashes, like 'biz-a-ig-coffee'")
        return v

    @field_validator("surface")
    @classmethod
    def _surface(cls, v: str) -> str:
        if not LANE_ID_RE.match(v):
            raise ValueError("surface must look like 'workspace/name', for example 'biz-a/instagram'")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"timezone {v!r} is not a known time zone (use a name like 'Europe/Vilnius')") from None
        return v

    @model_validator(mode="after")
    def _check(self) -> Program:
        if self.site is None:
            name = parse_lane_id(self.surface)[1]
            self.site = name.split("-", 1)[0]
        if self.site not in SITES:
            raise ValueError(f"site {self.site!r} is not one the engine knows ({', '.join(SITES)}); "
                             "set `site` when the surface name does not say it")
        kinds = [p.kind for p in self.phases]
        if kinds.count("steady") != 1 or kinds[-1] != "steady":
            raise ValueError("phases must end with exactly one steady phase (an optional warm_up comes first)")
        allowed = set(site_kinds(self.site))
        if self.steady.daily.get(Kind.PROFILE_EDIT):
            raise ValueError("profile edits are not a daily target: write the profile you want under `profile:`, "
                             "and the engine makes the edits it needs, under the profile_edit cap")
        if self.profile is not None and self.profile.fields() and self.site not in PROFILE_SITES:
            raise ValueError(f"{self.site} has no profile operations in the engine (only "
                             f"{', '.join(PROFILE_SITES)}); leave `profile:` out")
        for kind, n in self.steady.daily.items():
            if n and kind not in allowed:
                hint = (" Reddit votes are never chosen by software (section 5.4)."
                        if self.site == "reddit" and kind is Kind.LIKE else "")
                raise ValueError(f"{self.site} has no {kind.value} action in the engine "
                                 f"(it has {', '.join(k.value for k in CAPS[self.site])}).{hint}")
        if self.comments.mode is Mode.AUTO and self.site in APPROVE_ONLY_SITES:
            raise ValueError(f"comments on {self.site} stay in approve mode (section 5.6)")
        if self.community.reply_to_comments.mode is Mode.AUTO and self.site in APPROVE_ONLY_SITES:
            raise ValueError(f"replies on {self.site} stay in approve mode (section 5.6)")
        return self

    # ---- shape ----
    @property
    def workspace(self) -> str:
        return parse_lane_id(self.surface)[0]

    @property
    def warm_up(self) -> WarmUp | None:
        return next((p for p in self.phases if isinstance(p, WarmUp)), None)

    @property
    def steady(self) -> Steady:
        return next(p for p in self.phases if isinstance(p, Steady))

    @property
    def warm_up_days(self) -> int:
        return self.warm_up.days if self.warm_up is not None else 0

    def owner_only_parts(self) -> list[str]:
        """What in this program only the owner may choose (section 5.10)."""
        out = []
        if self.risk is Risk.NORMAL:
            out.append("risk normal")
        if self.comments.mode is Mode.AUTO:
            out.append("auto comments")
        if self.community.reply_to_comments.mode is Mode.AUTO:
            out.append("auto replies")
        if any(t.source is Source.FOLLOWERS_OF for t in self.steady.targets):
            out.append("the followers_of source")
        return out

    # ---- identity (as FlowDefinition) ----
    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    def content_hash(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


class ProgramState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"   # an older approved version, replaced by a newer approval


class ProgramVersion(BaseModel):
    definition: Program
    version: int
    hash: str
    state: ProgramState = ProgramState.DRAFT
    created_at: datetime = Field(default_factory=utcnow)
    created_by: dict[str, Any] | None = None
    approved_at: datetime | None = None
    approved_by: str | None = None
    starts_on: date | None = None    # day 1 of the program in the lane's time zone (set on approval)

    @property
    def id(self) -> str:
        return self.definition.id


class Session(BaseModel):
    start: datetime                  # aware, in the lane's time zone
    minutes: float
    actions: dict[Kind, int] = Field(default_factory=dict)
    seen: int = 0                    # posts the session scrolls past or opens

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)


class DayPlan(BaseModel):
    surface: str
    day: date
    timezone: str
    program: str | None = None
    program_hash: str | None = None
    phase: str                       # "warm_up" or "steady"
    phase_day: int                   # 1-based day within the phase
    stage: str | None = None         # the warm-up stage, or None when steady
    ramp_weeks: int = 0
    quiet: bool = False
    sessions: list[Session] = Field(default_factory=list)
    targets: dict[Kind, int] = Field(default_factory=dict)
    caps: dict[Kind, int] = Field(default_factory=dict)
    hourly: dict[Kind, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    def totals(self) -> dict[Kind, int]:
        out: dict[Kind, int] = {}
        for s in self.sessions:
            for k, n in s.actions.items():
                out[k] = out.get(k, 0) + n
        return dict(sorted(out.items()))
