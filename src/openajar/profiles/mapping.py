"""From one profile to what one account shows: the site fields and their limits.

    r = resolve(profile, attachment)
    r.values      {site field: value}, after the site's defaults, the account's `map` and `overrides`
    r.manual      the site fields with a value: a learned operation brings them in line
                  (openajar.profiles.sync), or an agent (or the owner) changes them in the lane by
                  hand, from profile_sync_status
    r.skipped     {site field: why} for a field left out (not on this kind of profile)
    r.problems    values the site would not take (too long, a mapping to nothing): shown to the owner,
                  never cut short

SITES lists each site's profile fields: the profile field it takes by default and the site's own
length limit. Limits come from each site's own forms as of 2026 and err on the short side; a
value over one is a problem for that account only (give it an override). Product, reviewer and
audience profiles are never shown on a site: they resolve to nothing but a problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openajar.profiles.fields import PHOTO_RE
from openajar.profiles.models import SOCIAL_KINDS, Attachment, Profile, ProfileKind


@dataclass(frozen=True)
class SiteField:
    source: str            # the profile field it takes by default
    limit: int | None      # the site's length limit, None for photos and links
    people_only: bool = False


def _f(source: str, limit: int | None = None, *, people_only: bool = False) -> SiteField:
    return SiteField(source, limit, people_only)


SITES: dict[str, dict[str, SiteField]] = {
    "linkedin": {
        "name": _f("name", 50, people_only=True),
        "headline": _f("headline", 220),
        "about": _f("bio.long", 2600),
        "photo": _f("photos.avatar"),
        "cover": _f("photos.cover"),
        "website": _f("website"),
        "location": _f("location", 100),
        "phone": _f("phone"),
    },
    "facebook": {
        "name": _f("display_name", 75),
        "bio": _f("bio.short", 101),
        "about": _f("bio.long", 2000),
        "photo": _f("photos.avatar"),
        "cover": _f("photos.cover"),
        "website": _f("website"),
        "phone": _f("phone"),
        "email": _f("email"),
        "location": _f("location", 100),
    },
    "instagram": {
        "name": _f("display_name", 30),
        "bio": _f("bio.short", 150),
        "website": _f("website"),
        "photo": _f("photos.avatar"),
        "phone": _f("phone"),
        "email": _f("email"),
    },
    "reddit": {
        "display_name": _f("display_name", 30),
        "about": _f("bio.short", 200),
        "photo": _f("photos.avatar"),
        "cover": _f("photos.cover"),
    },
    "x": {
        "name": _f("display_name", 50),
        "bio": _f("bio.short", 160),
        "location": _f("location", 30),
        "website": _f("website"),
        "photo": _f("photos.avatar"),
        "cover": _f("photos.cover"),
    },
    "threads": {"bio": _f("bio.short", 150), "website": _f("website"), "photo": _f("photos.avatar")},
    "tiktok": {"name": _f("display_name", 30), "bio": _f("bio.short", 80), "photo": _f("photos.avatar")},
    "youtube": {
        "name": _f("display_name", 50),
        "description": _f("bio.long", 1000),
        "photo": _f("photos.avatar"),
        "cover": _f("photos.cover"),
    },
    "medium": {"name": _f("display_name", 50), "bio": _f("bio.short", 160), "photo": _f("photos.avatar")},
    "blog": {},
}
ALIASES = {"twitter": "x", "ig": "instagram", "fb": "facebook", "yt": "youtube"}
# Where each site's login lives: the domain a vault entry for an account's login is saved under.
SITE_DOMAINS = {"linkedin": "linkedin.com", "facebook": "facebook.com", "instagram": "instagram.com",
                "reddit": "reddit.com", "x": "x.com", "threads": "threads.net", "tiktok": "tiktok.com",
                "youtube": "google.com", "medium": "medium.com"}
PHOTO_FIELDS = ("photo", "cover")
MULTILINE = ("about", "bio", "description")      # site fields that keep line breaks


def site_of(name: str) -> str:
    return ALIASES.get(name, name)


@dataclass
class Resolved:
    surface: str
    site: str
    values: dict[str, Any] = field(default_factory=dict)
    manual: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {"surface": self.surface, "site": self.site, "values": self.values,
                "manual": self.manual, "skipped": self.skipped, "problems": self.problems}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "first") and hasattr(value, "last"):
        return f"{value.first} {value.last}"
    return str(value)


def _wrong_type(name: str, spec: SiteField, value: Any) -> str | None:
    """Why `value` cannot be what the site field shows, or None: a photo is a stored photo, a
    name may be a first and last name, everything else is one piece of text (line breaks only
    in an About or a description)."""
    if name in PHOTO_FIELDS:
        return None if isinstance(value, str) and PHOTO_RE.match(value) else "takes a photo (photos.avatar or photos.cover)"
    if hasattr(value, "first") and hasattr(value, "last"):
        return None if name == "name" else "takes text, not a first and last name"
    if not isinstance(value, str):
        return "takes text"
    if "\n" in value and name not in MULTILINE:
        return "takes one line"
    return None


def resolve(profile: Profile, attachment: Attachment) -> Resolved:
    """What `attachment`'s account should show, from `profile` (pure)."""
    site = site_of(attachment.site or "")
    out = Resolved(attachment.surface, site)
    if profile.kind not in SOCIAL_KINDS:      # the model refuses accounts on these too; never sync one
        out.problems.append(f"{attachment.surface}: a {profile.kind.value} profile is not shown on social accounts")
        return out
    fields = SITES.get(site)
    if fields is None:
        out.problems.append(f"{site} is not a site profiles know ({', '.join(sorted(SITES))}); "
                            "set the account's site")
        return out
    for key in [*attachment.overrides, *attachment.map]:
        if key not in fields:
            out.problems.append(f"{attachment.surface}: {site} has no profile field {key!r} "
                                f"(it has {', '.join(fields) or 'none'})")
    for name, spec in fields.items():
        if spec.people_only and profile.kind is not ProfileKind.PERSON:
            if name in attachment.overrides or name in attachment.map:
                out.problems.append(f"{attachment.surface}: {name} is a person's first and last name; a business "
                                    "page's name changes by hand")
            out.skipped[name] = "people only"
            continue
        source = attachment.map.get(name, spec.source)
        if name in attachment.overrides:
            value: Any = attachment.overrides[name]
        else:
            value = profile.value(source)
        if value is None:
            continue
        wrong = _wrong_type(name, spec, value)
        if wrong:
            out.problems.append(f"{attachment.surface}: {name} {wrong} (it takes {source!r})")
            continue
        if spec.limit is not None:
            n = len(_text(value) or "")
            if n > spec.limit:
                out.problems.append(f"{attachment.surface}: {name} has {n} characters and {site} takes at most "
                                    f"{spec.limit}. Shorten {source} or give this account an override")
                continue
        out.values[name] = value.model_dump() if hasattr(value, "model_dump") else value
        out.manual.append(name)
    return out


def sites() -> list[dict[str, Any]]:
    """Every site an account can be on: its login domain and its profile fields (for the panel)."""
    return [{"site": site, "domain": SITE_DOMAINS.get(site),
             "fields": [{"name": n, "source": f.source, "limit": f.limit,
                         "people_only": f.people_only} for n, f in fields.items()]}
            for site, fields in SITES.items() if site in SITE_DOMAINS]
