"""Who a publish acts as, read from the request itself, and resolved against the lane's own
account and the surfaces that post through it (AUD-01 on the commit gate,
docs/design/egress-guard.md section 8.10).

A lane is a login; a surface is who you post as (agentos.core.models.Surface). On some sites a
login can act for others: a Facebook profile posts as a Page it manages, a LinkedIn member as a
company page, an Instagram login names a business account. The commit gate reads that identity from
the normalized request (`read`, per site: ACTING) and resolves it (`resolve`):

- the lane's own account (its pinned user id, or the signed-in account when none is pinned) ->
  `self`: the publish is the lane's, as before. `assumed` marks a `self` the gate inferred instead
  of read (nothing named, only Meta's form-level `av`, a member urn it cannot compare): the
  publish may still be a Page's through a field not listed here, so every surface's Ask me first
  switch on the lane still holds it;
- the id of an `account` surface through this lane whose id is not the lane's -> `surface`: that
  login speaks for the surface, so the surface's workspace and switch apply;
- an asset id that a surface through this lane names (`acts_as.id`, of the kind the field gives)
  -> `surface`: the publish is authorized on that surface (its workspace, its Ask me first switch,
  its pace, which is the lane's), and the card says "Posts as biz-a (Page)";
- anything else -> `unrecognized`: an id no surface names, a Page of another lane, two fields that
  disagree, a value that is not shaped like an id (never repeated: it comes from the page), or no
  identity the gate can read on a site where a login can act for others. Such a
  publish always asks a person (never automatic, never a standing approval), and is refused for a
  caller that cannot ask (a schedule or a flow running unattended).

On a site whose login only ever posts as itself (Reddit, and every site not in ACTING) nothing is
read and nothing changes: the lane's account is the identity (the AUD-12 pin covers it).

Field names: `verified` says where a field was seen on a real account's traffic (a canary run of 2026-11-04); empty means it comes from the sites' public client behaviour and was
not checked. No Page or company page post has been recorded yet, so every field that names a Page,
a company, a business account or a publication is unverified.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agentos.core.models import ASSET_ID_RE

SELF = "self"
SURFACE = "surface"
UNRECOGNIZED = "unrecognized"

CANARY = "2026-11-04 canary (personal posts and comments)"


@dataclass(frozen=True)
class ActingField:
    """A field that names who a publish acts as. `path`: a dotted path of the flattened body (`[]`
    any index), or `name:<key>` for a key by its last segment anywhere (lower case). `kind` is the
    ActsAs kind an id there resolves against (`actor`: the lane's own account or any asset of the
    site's kind; `member`: the login itself; `organization` / `publication`: an asset)."""
    path: str
    kind: str
    verified: str = ""
    # Meta's form-level `av`: it decides when no other acting field is present; next to one it may
    # name the login (the older Pages experience keeps the profile there), and anything else disagrees
    fallback: bool = False


@dataclass(frozen=True)
class ActingSpec:
    """How one site's publishes name who they act as.

    `for_others`: a login on this site can post as another identity, so an identity the gate cannot
    read is unrecognized. `asset_kind`: the surface kind its assets have (fb_page, li_company,
    ig_business). `fields` in order: the first actor field present decides (`primary`); a later
    actor field that names another identity makes it unrecognized (they disagree). `scan`: a value
    shape that names an asset wherever it sits (a LinkedIn organization urn): one outside the acting
    fields makes the identity unrecognized, since the gate cannot tell whether the post acts as it."""
    site: str
    for_others: bool
    asset_kind: str | None
    fields: tuple[ActingField, ...]
    scan: re.Pattern[str] | None = None
    note: str = ""
    # a body that names no acting field (and no asset where `scan` looks) is the login's own: true
    # where the site names the actor only when it is not the login (LinkedIn's company posts)
    default_self: bool = False


_LI_ORG = re.compile(r"^urn:li:(organization|fsd_company|company|organizationBrand):(\d+)$")
_LI_MEMBER = re.compile(r"^urn:li:(member|person|fsd_profile|fs_miniProfile|profile):([A-Za-z0-9_-]+)$")

ACTING: dict[str, ActingSpec] = {
    "facebook": ActingSpec(
        "facebook", True, "fb_page", (
            ActingField("variables.input.actor_id", "actor", CANARY),
            ActingField("variables.actor_id", "actor"),
            ActingField("variables.input.page_id", "actor"),
            ActingField("variables.pageID", "actor"),
            ActingField("variables.input.target_page_id", "actor"),
            # the form's acting viewer: the profile's id on the canary's personal posts; a Page in
            # the new Pages experience is said to send its own id here (not checked)
            ActingField("av", "actor", CANARY, fallback=True),
        ),
        note="actor_id and av were the profile's own id on every recorded personal post and comment; "
             "how a Page post names the Page (actor_id, av, page_id) is not recorded"),
    "linkedin": ActingSpec(
        "linkedin", True, "li_company", tuple(
            ActingField(f"name:{n}", "actor") for n in (
                "author", "authorurn", "actor", "actorurn", "nonmemberactorurn", "organizationactor",
                "organizationurn", "organizationid", "organization", "companyurn", "companyid", "company",
                "postas", "postingas", "actingentityurn")),
        scan=_LI_ORG, default_self=True,
        note="the canary recorded LinkedIn's operation names, not its fields: a post that names no author "
             "and no organization anywhere is the member's own; any organization urn outside these "
             "fields is unrecognized"),
    "instagram": ActingSpec(
        "instagram", False, "ig_business", (
            ActingField("variables.input.actor_id", "actor"),
            ActingField("variables.actor_id", "actor"),
            ActingField("variables.data.actor_id", "actor"),
            ActingField("name:business_account_id", "actor"),
        ),
        note="an Instagram login posts as itself unless the body names a business account; the canary's "
             "like and follow bodies carry actor_id, the comment it recorded did not"),
    "medium": ActingSpec(
        "medium", False, None, (
            ActingField("name:publicationid", "publication"),
            ActingField("name:publication_id", "publication"),
        ),
        note="a Medium login posts as itself; a post into a publication names it (no surface kind names "
             "a publication yet, so it is unrecognized)"),
}


@dataclass
class Read:
    """What a request names as its acting identity. `fields`: every acting field found, as
    (path, value, ActingField). `primary`: the one that decides. `stray`: asset ids the scan found
    outside the acting fields."""
    site: str
    spec: ActingSpec | None
    fields: list[tuple[str, str, ActingField]] = field(default_factory=list)
    stray: list[tuple[str, str]] = field(default_factory=list)

    @property
    def primary(self) -> tuple[str, str, ActingField] | None:
        return self.fields[0] if self.fields else None

    @property
    def applies(self) -> bool:
        """Does this site's identity need resolving at all (a login that can act for others, or a
        body that names an identity)? False: the lane's own account, nothing changes."""
        return self.spec is not None and (self.spec.for_others or bool(self.fields) or bool(self.stray))


def _path_re(path: str) -> re.Pattern[str]:
    return re.compile("^" + re.escape(path).replace(r"\[\]", r"\[\d+\]") + "$")


def _last(path: str) -> str:
    return re.sub(r"\[\d+\]", "", path).rsplit(".", 1)[-1].lower()


def read(site: str, flat: Mapping[str, Any]) -> Read:
    """The acting fields of one normalized body (publish.flatten: volatile fields dropped)."""
    spec = ACTING.get(site)
    out = Read(site, spec)
    if spec is None:
        return out
    taken: set[str] = set()
    for f in spec.fields:
        if f.path.startswith("name:"):
            name = f.path[5:]
            found = [(k, v) for k, v in flat.items() if _last(k) == name]
        else:
            rx = _path_re(f.path)
            found = [(k, v) for k, v in flat.items() if rx.match(k)]
        for k, v in found:
            if k in taken or isinstance(v, bool) or v is None or str(v).strip() == "":
                continue
            taken.add(k)
            out.fields.append((k, str(v).strip(), f))
    if spec.scan is not None:
        for k, v in flat.items():
            if k not in taken and isinstance(v, str) and spec.scan.match(v.strip()):
                out.stray.append((k, v.strip()))
    return out


def _asset_id(site: str, value: str) -> tuple[str | None, str]:
    """(the kind a value names: "member", "organization" or None for a bare id, the id itself)."""
    if site == "linkedin":
        m = _LI_ORG.match(value)
        if m:
            return "organization", m.group(2)
        m = _LI_MEMBER.match(value)
        if m:
            return "member", m.group(2)
    return None, value


def _shaped(value: str) -> bool:
    """Is this value shaped like an id? Anything else is the page's own text (a name, a sentence, a
    character that turns text around): it is bound with the body, and never repeated in the node's
    words on a card, in a summary or in the gate log."""
    return ASSET_ID_RE.match(value) is not None


def _safe(value: str) -> str:
    return value if _shaped(value) else "a value that is not an id"


def _label_self(lane: str, pinned: Any, session: Mapping[str, Any] | None) -> str:
    handle = getattr(pinned, "handle", None)
    uid = getattr(pinned, "user_id", None) or (session or {}).get("user_id")
    who = handle or uid or lane
    return f"{who} (the lane's own account)"


def resolve(r: Read, *, lane: str, pinned: Any = None, session: Mapping[str, Any] | None = None,
            surfaces: Iterable[Any] = ()) -> dict[str, Any] | None:
    """The acting identity of a publish on `lane`, as the record keeps it (the digest binds it), or
    None when the site's login only posts as itself and the body names nobody (nothing changes).

    `pinned`: the lane's AccountRef (AUD-12); `session`: the signed-in account read from the
    browser's cookies ({site, user_id}); `surfaces`: every Surface on the node (those through this
    lane resolve; those through another lane are named in the reason)."""
    if not r.applies:
        return None
    spec = r.spec
    assert spec is not None
    site = r.site
    own: set[str] = set()
    if pinned is not None and getattr(pinned, "user_id", None) and _site_key(getattr(pinned, "site", "")) == site:
        own.add(str(pinned.user_id).casefold())
    if session and session.get("user_id") and session.get("site") == site:
        own.add(str(session["user_id"]).casefold())
    mine = [s for s in surfaces if getattr(s, "lane", None) == lane and getattr(s, "site", None) == site]
    others = [s for s in surfaces if getattr(s, "lane", None) != lane and getattr(s, "site", None) == site]
    for s in mine:
        if s.acts_as.kind == "account" and s.acts_as.user_id:
            own.add(str(s.acts_as.user_id).casefold())
    base = {"through": lane, "site": site}

    def unrecognized(raw: str | None, path: str | None, why: str, short: str,
                     verified: bool = False) -> dict[str, Any]:
        return {**base, "status": UNRECOGNIZED, "id": raw, "field": path, "verified": verified, "why": why,
                "short": short,
                "label": f"an identity agentos does not know ({raw})" if raw else "an identity agentos cannot read"}

    def as_surface(s: Any, path: str, value: str, verified: bool) -> dict[str, Any]:
        return {**base, "status": SURFACE, "id": value, "field": path, "verified": verified,
                "surface": s.id, "kind": s.acts_as.kind, "label": s.acts_as.label()}

    def account_surface(ident: str) -> Any:
        """The account surface through this lane that this id is, when it has an id of its own."""
        return next((s for s in mine if s.id != lane and s.acts_as.kind == "account"
                     and str(s.acts_as.user_id).casefold() == ident.casefold()), None)

    def one(path: str, value: str, f: ActingField) -> dict[str, Any]:
        verified = bool(f.verified)
        if not _shaped(value):
            return unrecognized(None, path, f"{path} does not hold an id the gate can read", "it cannot be read",
                                verified)
        kind, ident = _asset_id(site, value)
        if f.kind == "publication":
            return unrecognized(value, path, f"the body posts into publication {value}, and no surface names "
                                             "a publication", "no surface names a publication", verified)
        known = ident.casefold() in own
        if known and kind in (None, "member"):
            s = account_surface(ident)
            if s is not None:
                return as_surface(s, path, value, verified)
            return {**base, "status": SELF, "id": value, "field": path, "verified": verified,
                    "label": _label_self(lane, pinned, session), **({"assumed": True} if f.fallback else {})}
        if kind == "member":  # a member only ever posts as itself, but which member the gate cannot compare
            return {**base, "status": SELF, "id": value, "field": path, "verified": verified, "assumed": True,
                    "label": f"the login's own profile (the request names {value})"}
        for s in mine:
            if s.acts_as.kind != "account" and s.acts_as.kind == spec.asset_kind and s.acts_as.id == ident:
                return as_surface(s, path, value, verified)
        elsewhere = next((s for s in others if s.acts_as.asset_id == ident), None)
        if elsewhere is not None:
            return unrecognized(value, path, f"{value} is {elsewhere.acts_as.label()}, the surface {elsewhere.id}, "
                                             f"which posts through {elsewhere.lane}, not this lane",
                                "it posts through another lane", verified)
        return unrecognized(value, path, f"no surface through {lane} names it, and it is not the lane's own "
                                         "account", "no surface names it", verified)

    if r.stray:
        path, value = r.stray[0]
        return unrecognized(value, path, f"the body names {value} in {path}, outside the fields that say who "
                                         "it posts as, so the gate cannot tell whether it posts as it",
                            "the gate cannot tell whether it posts as it")
    if not r.fields:
        if spec.default_self:  # the site names the actor only when it is not the login
            return {**base, "status": SELF, "id": None, "field": None, "verified": False, "assumed": True,
                    "label": "the login's own profile (the request names no company page)"}
        if spec.for_others:
            return unrecognized(None, None, f"a login on {site} can post as a Page or company it manages, and "
                                            "this request names no identity the gate can read",
                                "the request names none")
        return None  # a body that names nobody on a site whose login posts as itself
    first = one(*r.fields[0])
    for path, value, f in r.fields[1:]:
        other = one(path, value, f)
        if f.fallback and other["status"] == SELF:
            continue  # the form keeps the login while the body names who it posts as
        if (other["status"], other.get("surface")) != (first["status"], first.get("surface")):
            head, named = r.fields[0][0], r.fields[0][1]
            return unrecognized(named if _shaped(named) else None, head,
                                f"{head} names {_safe(named)} and {path} names {_safe(value)}: the fields "
                                "disagree on who it posts as", "the fields disagree")
    return first


def _site_key(name: str) -> str:
    from agentos.node.sites import site_key

    try:
        return site_key(name)
    except Exception:  # noqa: BLE001 - an unknown site name: compared as written
        return name


def card_line(acts: Mapping[str, Any] | None) -> str | None:
    """The identity line of a publish card and of a chat confirmation built from it."""
    if not acts:
        return None
    if acts.get("status") in (SURFACE, SELF):
        return f"Posts as {acts.get('label')}"
    raw = acts.get("id")
    return ("Posts as an identity agentos does not know" + (f" ({raw})" if raw else "") +
            f": {acts.get('short') or 'no surface names it'}")


def public(acts: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What a trace row keeps of it: never more than ids and names."""
    if not acts:
        return None
    return {k: acts.get(k) for k in ("status", "id", "field", "surface", "kind", "label", "verified")
            if acts.get(k) is not None}
