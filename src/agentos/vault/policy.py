"""The No-Body-Injection rule, as pure functions.

A secret resolves only when ALL of these hold:

1. the entry's kind may flow into the requested sink (passwords into login form fields,
   api keys into an auth header, cookies into the cookie jar, notes nowhere);
2. the lane is bound to the entry (an exact lane id or a `workspace/*` wildcard), and for a login
   the owner bound to agent environments (config `envs`) on an environment's lane, the lane's
   environment is one it covers: by title, by repository or every one (`lane_facts`, read from the
   node's own records by the vault service; none known is no environment covered);
3. the target origin is https (http only on localhost / loopback IPs), and its host's
   registrable domain equals the entry's domain, in A-label form (Google's country domains
   count as one site), and the host is in the entry's allowed hosts when it has any;
4. for form sinks: the field lives in the top document or in frames that are all the
   same origin as the top document (a cross-origin iframe anywhere in the chain is
   refused), it is an enabled, visible, editable `<input>`, and its structural role
   (password / username / one-time-code) is the role the sink expects.

Field roles come from structure only (type, autocomplete tokens, form association,
attribute names), never from localized label text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from agentos.core.models import parse_lane_id
from agentos.vault.domains import Origin, is_loopback, parse_origin, registrable_domain, same_site
from agentos.vault.errors import ResolutionDenied
from agentos.vault.models import WILDCARD_RE, EntryKind, EntryMeta
from agentos.vault.typist import FieldInfo


class Sink(StrEnum):
    FORM_USERNAME = "form_username"
    FORM_PASSWORD = "form_password"
    FORM_OTP = "form_otp"
    AUTH_HEADER = "auth_header"
    COOKIE = "cookie"
    IMAP_LOGIN = "imap_login"   # node-side login to the mailbox of a code catcher
    APP_REQUEST = "app_request"  # the node's swap: an agent environment's app request to the bound host


class FieldRole(StrEnum):
    USERNAME = "username"
    PASSWORD = "password"
    OTP = "one_time_code"


class Reason(StrEnum):
    OK = "ok"
    NO_ENTRY = "no_entry"
    KIND_SINK = "kind_sink_mismatch"
    NO_USERNAME = "no_username"
    NO_CODE_SOURCE = "no_code_source"
    LANE_NOT_BOUND = "lane_not_bound"
    BAD_URL = "bad_url"
    INSECURE_SCHEME = "insecure_scheme"
    DOMAIN_MISMATCH = "domain_mismatch"
    HOST_NOT_ALLOWED = "host_not_allowed"
    CROSS_ORIGIN_FRAME = "cross_origin_frame"
    NO_FIELD = "no_field"
    NOT_A_LOGIN_FIELD = "not_a_login_field"
    FIELD_ROLE_MISMATCH = "field_role_mismatch"
    FIELD_NOT_INTERACTABLE = "field_not_interactable"


FORM_SINKS = frozenset({Sink.FORM_USERNAME, Sink.FORM_PASSWORD, Sink.FORM_OTP})
ROLE_SINK = {FieldRole.USERNAME: Sink.FORM_USERNAME, FieldRole.PASSWORD: Sink.FORM_PASSWORD, FieldRole.OTP: Sink.FORM_OTP}
KIND_SINKS: dict[EntryKind, frozenset[Sink]] = {
    EntryKind.PASSWORD: frozenset({Sink.FORM_USERNAME, Sink.FORM_PASSWORD, Sink.FORM_OTP, Sink.IMAP_LOGIN}),
    EntryKind.TOTP: frozenset({Sink.FORM_OTP}),
    EntryKind.API_KEY: frozenset({Sink.AUTH_HEADER, Sink.APP_REQUEST}),
    EntryKind.COOKIE: frozenset({Sink.COOKIE}),
    EntryKind.NOTE: frozenset(),
}

_MESSAGES = {
    Reason.NO_ENTRY: "no vault entry bound to lane {lane} matches {host}",
    Reason.KIND_SINK: "a {kind} entry cannot be used for {sink}",
    Reason.NO_USERNAME: "entry has no username",
    Reason.NO_CODE_SOURCE: "entry has no TOTP seed or code catcher",
    Reason.LANE_NOT_BOUND: "entry is not bound to lane {lane}",
    Reason.BAD_URL: "the page has no usable origin",
    Reason.INSECURE_SCHEME: "secrets are only sent over https (http only on localhost)",
    Reason.DOMAIN_MISMATCH: "{host} is not on the entry's domain {domain}",
    Reason.HOST_NOT_ALLOWED: "{host} is not one of the entry's allowed hosts",
    Reason.CROSS_ORIGIN_FRAME: "the field is inside a frame from another origin",
    Reason.NO_FIELD: "no field to fill",
    Reason.NOT_A_LOGIN_FIELD: "the field is not a login field (password, username or one-time code)",
    Reason.FIELD_ROLE_MISMATCH: "the field is a {role} field, not a {want} field",
    Reason.FIELD_NOT_INTERACTABLE: "the field is hidden, disabled or read-only",
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: Reason
    host: str | None = None
    message: str = ""

    def check(self) -> Decision:
        if not self.allowed:
            raise ResolutionDenied(f"vault refused: {self.message}", reason=self.reason.value)
        return self


def _deny(reason: Reason, at: str | None = None, **fmt: object) -> Decision:
    return Decision(False, reason, at, _MESSAGES[reason].format(**fmt))


# ---- lane binding ---------------------------------------------------------------

def lane_bound(patterns: Iterable[str], lane_id: str) -> bool:
    """Exact lane id or `workspace/*`. Malformed lane ids and patterns never match."""
    try:
        workspace, _ = parse_lane_id(lane_id)
    except (ValueError, TypeError):
        return False
    for p in patterns:
        if p == lane_id:
            return True
        if WILDCARD_RE.match(p) and p[:-2] == workspace:
            return True
    return False


ENV_WORKSPACE = "env"  # agentos.environments.models.WORKSPACE: every agent environment's lanes


def _same_title(a: str, b: str) -> bool:
    """Titles as agentos.environments.picking.same_title compares them: case and spaces aside."""
    return " ".join(a.split()).lower() == " ".join(b.split()).lower()


def env_scope_allows(entry: EntryMeta, lane_id: str, facts: Mapping[str, object] | None) -> bool:
    """For an entry the owner bound to agent environments (config `envs`, agentos.environments.logins):
    on an environment's lane, does one of its scopes cover the lane's environment (`facts`: its title,
    repository and grant family)? A title scope covers only the family recorded when the owner bound it
    (a title is the agent's own choice; one with no family covers nothing). Any other lane, or an entry
    with no such binding: True (its lanes decide)."""
    envs = (getattr(entry, "config", None) or {}).get("envs")
    if not isinstance(envs, dict) or not str(lane_id).startswith(ENV_WORKSPACE + "/"):
        return True
    if not facts:
        return False
    title, repo, family = str(facts.get("title") or ""), facts.get("repo"), facts.get("family")
    for scope in envs.get("scopes") or []:
        if scope.get("all") is True:
            return True
        if scope.get("title") and title and _same_title(str(scope["title"]), title) \
                and scope.get("family") and family and scope["family"] == family:
            return True
        if scope.get("repo") and repo and scope["repo"] == repo:
            return True
    return False


def entry_bound(entry: EntryMeta, lane_id: str, facts: Mapping[str, object] | None = None) -> bool:
    """The entry may be used on this lane: bound to it (lane_bound), and for an environment's lane
    within the environments the owner bound it to (env_scope_allows)."""
    return lane_bound(entry.lanes, lane_id) and env_scope_allows(entry, lane_id, facts)


# ---- origins --------------------------------------------------------------------

def secure_origin(url: str, *, schemes: tuple[str, ...] = ("http", "https")) -> tuple[Origin | None, Decision]:
    """The URL's origin if it is usable for a secret at all (parseable, and encrypted
    unless it is loopback), with the decision."""
    try:
        origin = parse_origin(url, schemes=schemes)
    except ValueError:
        return None, _deny(Reason.BAD_URL)
    if origin.scheme in ("http", "imap") and not is_loopback(origin.host):
        return None, _deny(Reason.INSECURE_SCHEME, origin.host)
    return origin, Decision(True, Reason.OK, origin.host)


def check_origin(entry: EntryMeta, url: str, *, schemes: tuple[str, ...] = ("http", "https")) -> Decision:
    origin, d = secure_origin(url, schemes=schemes)
    if origin is None:
        return d
    try:
        reg = registrable_domain(origin.host)
    except ValueError:
        reg = None
    if not entry.domain or not same_site(reg, entry.domain):
        return _deny(Reason.DOMAIN_MISMATCH, origin.host, host=origin.host, domain=entry.domain)
    if entry.hosts and origin.host not in entry.hosts:
        return _deny(Reason.HOST_NOT_ALLOWED, origin.host, host=origin.host)
    return Decision(True, Reason.OK, origin.host)


def same_origin_chain(top: Origin, chain: Sequence[str]) -> bool:
    if not chain:
        return False
    for raw in chain:
        try:
            if parse_origin(raw) != top:
                return False
        except ValueError:
            return False  # opaque ("null"), about:blank, data:, ...
    return True


# ---- structural field roles -----------------------------------------------------

TEXTLIKE = frozenset({"text", "email", "tel", "number", ""})
_USER_HINT = re.compile(r"(user|login|e-?mail|account|identifier|ident|uname|phone)", re.IGNORECASE)
_OTP_HINT = re.compile(r"(otp|2fa|mfa|one.?time|passcode|verif|security.?code|auth.?code|totp|code|token|pin)", re.IGNORECASE)


def interactable(f: FieldInfo) -> bool:
    return f.tag == "input" and f.visible and not f.disabled and not f.readonly


def _ftype(f: FieldInfo) -> str:
    return (f.type or "text").lower()


def _same_form(a: FieldInfo, b: FieldInfo) -> bool:
    return a.form == b.form and tuple(a.frame_origins) == tuple(b.frame_origins)


def classify_fields(fields: Sequence[FieldInfo]) -> dict[str, FieldRole]:
    """Structural role of each login field, keyed by locator. Fields without a role
    (search boxes, comments, newsletter inputs, new-password fields) are absent."""
    roles: dict[str, FieldRole] = {}
    live = [f for f in fields if interactable(f)]
    _explicit_roles(live, roles)
    _username_before_password(live, roles)
    groups: dict[tuple, list[FieldInfo]] = {}
    for f in live:
        groups.setdefault((f.form, tuple(f.frame_origins)), []).append(f)
    for group in groups.values():
        _username_first_step(group, roles)
    for group in groups.values():
        _code_by_structure(group, roles)
    return roles


def _explicit_roles(live: list[FieldInfo], roles: dict[str, FieldRole]) -> None:
    """1. explicit signals: autocomplete tokens and password inputs."""
    for f in live:
        t, toks = _ftype(f), f.autocomplete_tokens
        if "one-time-code" in toks and (t in TEXTLIKE or t == "password"):
            roles[f.locator] = FieldRole.OTP
        elif t == "password":
            if "new-password" not in toks:
                roles[f.locator] = FieldRole.PASSWORD
        elif t in TEXTLIKE and "username" in toks:
            roles[f.locator] = FieldRole.USERNAME


def _username_before_password(live: list[FieldInfo], roles: dict[str, FieldRole]) -> None:
    """2. the text-like input right before a password field in the same form."""
    for i, p in enumerate(live):
        if roles.get(p.locator) is not FieldRole.PASSWORD:
            continue
        if any(roles.get(o.locator) is FieldRole.USERNAME and _same_form(o, p) for o in live):
            continue
        before = [o for o in live[:i] if _same_form(o, p) and _ftype(o) in TEXTLIKE and o.locator not in roles]
        if before:
            roles[before[-1].locator] = FieldRole.USERNAME


def _username_first_step(group: list[FieldInfo], roles: dict[str, FieldRole]) -> None:
    """3. username-first step: the only free text-like input of a form that has no password."""
    if any(roles.get(f.locator) in (FieldRole.PASSWORD, FieldRole.USERNAME) for f in group):
        return
    free = [f for f in group if _ftype(f) in TEXTLIKE and f.locator not in roles]
    if len(free) == 1:
        f = free[0]
        hinted = _USER_HINT.search(f.name) or _USER_HINT.search(f.id)
        code_like = _OTP_HINT.search(f.name) or _OTP_HINT.search(f.id)
        if _ftype(f) == "email" or "email" in f.autocomplete_tokens or (hinted and not code_like):
            roles[f.locator] = FieldRole.USERNAME


def _code_by_structure(group: list[FieldInfo], roles: dict[str, FieldRole]) -> None:
    """4. one-time-code by structure: numeric input with a code-like name, in a form with
    no password field; or a group of 4 to 8 single-character inputs."""
    if any(roles.get(f.locator) is FieldRole.PASSWORD for f in group):
        return
    free = [f for f in group if f.locator not in roles and (_ftype(f) in TEXTLIKE or _ftype(f) == "password")]
    singles = [f for f in free if f.maxlength == 1]
    if 4 <= len(singles) <= 8 and len(singles) == len(free):
        for f in singles:
            roles[f.locator] = FieldRole.OTP
        return
    for f in free:
        numeric = f.inputmode.lower() == "numeric" or _ftype(f) in ("tel", "number")
        sized = f.maxlength is None or 4 <= f.maxlength <= 10
        if numeric and sized and (_OTP_HINT.search(f.name) or _OTP_HINT.search(f.id)):
            roles[f.locator] = FieldRole.OTP


def otp_group(fields: Sequence[FieldInfo], first: FieldInfo) -> list[FieldInfo]:
    """The single-character boxes of a split one-time-code input, in order (or [first])."""
    if first.maxlength != 1:
        return [first]
    roles = classify_fields(fields)
    group = [f for f in fields if roles.get(f.locator) is FieldRole.OTP and _same_form(f, first) and f.maxlength == 1]
    return group or [first]


# ---- the decision ---------------------------------------------------------------

def decide(
    entry: EntryMeta,
    *,
    lane_id: str,
    sink: Sink,
    url: str,
    field: FieldInfo | None = None,
    fields: Sequence[FieldInfo] = (),
    lane_facts: Mapping[str, object] | None = None,
) -> Decision:
    """May `entry` resolve into `sink` at `url` (the top document URL for form sinks,
    the request URL for an auth header or cookie, `imaps://host:port` for IMAP)? `lane_facts`:
    the title and repository of the environment whose lane it is (entry_bound)."""
    if sink not in KIND_SINKS.get(entry.kind, frozenset()):
        return _deny(Reason.KIND_SINK, kind=entry.kind.value, sink=sink.value)
    if sink is Sink.FORM_USERNAME and not entry.has_username:
        return _deny(Reason.NO_USERNAME)
    if sink is Sink.FORM_OTP and not (entry.has_totp or entry.config.get("catcher")):
        return _deny(Reason.NO_CODE_SOURCE)
    if not entry_bound(entry, lane_id, lane_facts):
        return _deny(Reason.LANE_NOT_BOUND, lane=lane_id)
    schemes = ("imaps", "imap") if sink is Sink.IMAP_LOGIN else ("http", "https")
    origin_decision = check_origin(entry, url, schemes=schemes)
    if not origin_decision.allowed or sink not in FORM_SINKS:
        return origin_decision
    host = origin_decision.host
    if field is None:
        return _deny(Reason.NO_FIELD, host)
    top = parse_origin(url)
    if not same_origin_chain(top, field.frame_origins):
        return _deny(Reason.CROSS_ORIGIN_FRAME, host)
    if not interactable(field):
        return _deny(Reason.FIELD_NOT_INTERACTABLE, host)
    pool = list(fields) if any(f.locator == field.locator for f in fields) else [*fields, field]
    pool = [field if f.locator == field.locator else f for f in pool]
    role = classify_fields(pool).get(field.locator)
    if role is None:
        return _deny(Reason.NOT_A_LOGIN_FIELD, host)
    if ROLE_SINK[role] is not sink:
        want = {v: k for k, v in ROLE_SINK.items()}[sink]
        return _deny(Reason.FIELD_ROLE_MISMATCH, host, role=role.value, want=want.value)
    return Decision(True, Reason.OK, host)


def matching_entries(entries: Iterable[EntryMeta], *, lane_id: str, url: str,
                     kinds: Iterable[EntryKind] = (EntryKind.PASSWORD,),
                     lane_facts: Mapping[str, object] | None = None) -> list[EntryMeta]:
    """Entries bound to the lane whose domain matches the page (for picking one)."""
    kinds = set(kinds)
    return [
        e for e in entries
        if e.kind in kinds and entry_bound(e, lane_id, lane_facts) and check_origin(e, url).allowed
    ]
