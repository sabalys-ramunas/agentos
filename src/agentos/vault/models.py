"""Vault entry models and input validation.

`EntryMeta` is everything about an entry that may be shown to a human or an agent:
never a username, secret, seed or code. `NewEntry` carries the sealed fields as
`Secret`s until the store encrypts them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from agentos.core.models import LANE_ID_RE
from agentos.vault.domains import host_of, normalize_host, registrable_domain
from agentos.vault.errors import EntryInvalid
from agentos.vault.secret import Secret


class EntryKind(StrEnum):
    PASSWORD = "password"   # username (optional) + password, optional TOTP seed
    TOTP = "totp"           # a TOTP seed on its own
    COOKIE = "cookie"       # a Cookie header value for the domain
    API_KEY = "api_key"     # resolved only into an HTTP auth header
    NOTE = "note"           # stored sealed; never resolves into any sink


WILDCARD_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}/\*$")
NAME_MAX = 80
SECRET_MAX = {
    EntryKind.PASSWORD: 4096,
    EntryKind.TOTP: 2048,
    EntryKind.COOKIE: 16384,
    EntryKind.API_KEY: 8192,
    EntryKind.NOTE: 65536,
}
USERNAME_MAX = 1024
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")


class EntryMeta(BaseModel):
    """Public, non-secret description of a vault entry."""

    id: str
    name: str
    kind: EntryKind
    domain: str | None = None
    hosts: list[str] = Field(default_factory=list)
    lanes: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    has_username: bool = False
    has_secret: bool = False
    has_totp: bool = False
    created_at: datetime
    updated_at: datetime
    last_used: datetime | None = None
    use_count: int = 0
    key_version: int = 1

    def public(self) -> dict[str, Any]:
        """The /api/vault shape (docs/API.md): names, domains and lanes only."""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind.value,
            "domain": self.domain,
            "lanes": list(self.lanes),
            "created_at": self.created_at.isoformat(),
            "last_used": self.last_used.isoformat() if self.last_used else None,
        }

    def listing(self) -> dict[str, Any]:
        """What vault_list returns to an agent."""
        return {"name": self.name, "kind": self.kind.value, "domain": self.domain, "lanes": list(self.lanes)}

    def detail(self) -> dict[str, Any]:
        """`agentos vault show`: metadata only."""
        return self.model_dump(mode="json", exclude={"key_version"})


@dataclass(repr=False)
class NewEntry:
    name: str
    kind: EntryKind
    domain: str | None = None
    hosts: list[str] = field(default_factory=list)
    lanes: list[str] = field(default_factory=list)
    username: Secret | None = None
    secret: Secret | None = None
    totp: Secret | None = None        # canonical otpauth:// URI
    config: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"NewEntry(name={self.name!r}, kind={self.kind.value!r}, domain={self.domain!r})"


def validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise EntryInvalid("entry name must be a string")
    name = unicodedata.normalize("NFC", name).strip()
    if not name or len(name) > NAME_MAX:
        raise EntryInvalid(f"entry name must be 1 to {NAME_MAX} characters")
    if any(unicodedata.category(c).startswith("C") for c in name):
        raise EntryInvalid("entry name must not contain control characters")
    return name


def validate_lane_pattern(pattern: str) -> str:
    if not isinstance(pattern, str):
        raise EntryInvalid("lane binding must be a string")
    p = pattern.strip()
    if LANE_ID_RE.match(p) or WILDCARD_RE.match(p):
        return p
    raise EntryInvalid(
        f"lane binding {pattern!r} must be a lane id like 'biz-a/instagram' or a workspace wildcard like 'biz-a/*'"
    )


def validate_lanes(patterns: list[str] | tuple[str, ...] | None) -> list[str]:
    return sorted({validate_lane_pattern(p) for p in (patterns or [])})


def validate_domain(value: str) -> str:
    """Registrable domain of `value` (a domain, host or URL), in A-label form."""
    if not isinstance(value, str) or not value.strip():
        raise EntryInvalid("domain is required")
    try:
        reg = registrable_domain(host_of(value))
    except ValueError:
        reg = None
    if reg is None:
        raise EntryInvalid(
            f"{value!r} is not a registrable domain (a public suffix, an unknown TLD, or malformed); "
            "use a domain such as example.com"
        )
    return reg


def validate_hosts(hosts: list[str] | None, domain: str) -> list[str]:
    out = set()
    for h in hosts or []:
        try:
            norm = normalize_host(host_of(h))
            reg = registrable_domain(norm)
        except ValueError:
            raise EntryInvalid(f"allowed host {h!r} is not a valid host") from None
        if reg != domain:
            raise EntryInvalid(f"allowed host {h!r} is not under the entry's domain {domain}")
        out.add(norm)
    return sorted(out)


def _check_secret(label: str, value: Secret | None, limit: int, *, single_line: bool) -> None:
    if value is None:
        return
    raw = value.reveal()
    if not raw:
        raise EntryInvalid(f"{label} must not be empty")
    if len(raw.encode("utf-8")) > limit:
        raise EntryInvalid(f"{label} is longer than {limit} bytes")
    if "\x00" in raw:
        raise EntryInvalid(f"{label} must not contain NUL characters")
    if single_line and ("\r" in raw or "\n" in raw):
        raise EntryInvalid(f"{label} must be a single line")
    # A typed value goes in key by key: a tab moves focus to the next field (the rest lands
    # wherever that is) and an escape can close a dialog. The typist refuses such a value at
    # type time; refusing it here tells the owner while they can still fix it.
    allowed = "" if single_line else "\t\n\r"
    if any(unicodedata.category(c) == "Cc" and c not in allowed for c in raw):
        what = "tabs, escapes or other control characters" if single_line else "control characters other than tabs and line breaks"
        raise EntryInvalid(f"{label} must not contain {what}")


def validate_config(kind: EntryKind, config: dict[str, Any] | None) -> dict[str, Any]:
    config = dict(config or {})
    allowed = {"header", "prefix", "catcher", "env", "envs"}
    unknown = set(config) - allowed
    if unknown:
        raise EntryInvalid(f"unknown entry config keys: {', '.join(sorted(unknown))}")
    if "header" in config or "prefix" in config:
        if kind is not EntryKind.API_KEY:
            raise EntryInvalid("header and prefix apply to api_key entries only")
        header = config.get("header", "Authorization")
        prefix = config.get("prefix", "Bearer " if str(header).lower() == "authorization" else "")
        if not isinstance(header, str) or not HEADER_NAME_RE.match(header):
            raise EntryInvalid("header must be an HTTP header name")
        if header.lower() in {"cookie", "host", "content-length", "transfer-encoding"}:
            raise EntryInvalid(f"header {header!r} cannot carry an api key")
        if not isinstance(prefix, str) or len(prefix) > 32 or any(c in prefix for c in "\r\n\x00"):
            raise EntryInvalid("prefix must be a short single-line string")
        config["header"], config["prefix"] = header, prefix
    if "env" in config:
        config["env"] = _env_binding(kind, config["env"])
    if "envs" in config:
        config["envs"] = _envs_binding(kind, config["envs"])
    if "catcher" in config:
        from agentos.vault.catcher import validate_catcher_config

        if kind not in (EntryKind.PASSWORD, EntryKind.TOTP):
            raise EntryInvalid("a code catcher applies to password entries only")
        config["catcher"] = validate_catcher_config(config["catcher"])
    return config


REPO_RE = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")


def _env_binding(kind: EntryKind, value: Any) -> dict[str, Any]:
    """An api key the app in agent environments uses (agentos.environments.appsecrets): the variable that
    holds its placeholder there, and the one repository it is for (None: every environment)."""
    if kind is not EntryKind.API_KEY:
        raise EntryInvalid("an environment variable binding applies to api_key entries only")
    if not isinstance(value, dict) or set(value) - {"var", "repo"}:
        raise EntryInvalid("env is {var, repo}")
    var, repo = value.get("var"), value.get("repo")
    if not isinstance(var, str) or not ENV_VAR_RE.match(var):
        raise EntryInvalid("env.var is a variable name in capitals, like STRIPE_SECRET_KEY")
    if repo is not None and (not isinstance(repo, str) or not REPO_RE.match(repo)):
        raise EntryInvalid("env.repo is a repository, like github.com/me/shop")
    return {"var": var, "repo": repo}


ENV_TITLE_MAX = 80
LOGIN_URL_MAX = 500


def _env_scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) != 1:
        raise EntryInvalid("an environment scope is one of {title}, {repo} or {all: true}")
    (key, v), = value.items()
    if key == "all" and v is True:
        return {"all": True}
    if key == "repo" and isinstance(v, str) and REPO_RE.match(v):
        return {"repo": v}
    if key == "title" and isinstance(v, str) and v.strip() and len(v) <= ENV_TITLE_MAX \
            and not any(unicodedata.category(c).startswith("C") for c in v):
        return {"title": " ".join(v.split())}
    raise EntryInvalid("an environment scope is a title (1 to 80 characters), a repository like "
                       "github.com/me/shop, or all")


def _envs_binding(kind: EntryKind, value: Any) -> dict[str, Any]:
    """A login the owner bound to agent environments (agentos.environments.logins): the environments
    it covers (`scopes`), the username the owner's list shows and the login page the node opens (`user`,
    `url`: never a secret), and whether `agentos environments login add` made the entry (`made`: removed with its
    last binding)."""
    if kind is not EntryKind.PASSWORD:
        raise EntryInvalid("an environment login binding applies to password entries only")
    if not isinstance(value, dict) or set(value) - {"scopes", "user", "url", "made"}:
        raise EntryInvalid("envs is {scopes, user, url, made}")
    scopes = value.get("scopes") or []
    if not isinstance(scopes, list) or len(scopes) > 50:
        raise EntryInvalid("envs.scopes is a list of at most 50 scopes")
    out_scopes: list[dict[str, Any]] = []
    for scope in (_env_scope(s) for s in scopes):
        if scope not in out_scopes:
            out_scopes.append(scope)
    user, url = value.get("user"), value.get("url")
    if user is not None and (not isinstance(user, str) or not user or len(user) > USERNAME_MAX or "\n" in user):
        raise EntryInvalid("envs.user is the login's username, on one line")
    if url is not None and (not isinstance(url, str) or len(url) > LOGIN_URL_MAX
                            or not url.startswith(("https://", "http://"))):
        raise EntryInvalid("envs.url is the site's login page, an http or https address")
    return {"scopes": out_scopes, "user": user, "url": url, "made": bool(value.get("made"))}


def validate_new_entry(entry: NewEntry) -> NewEntry:
    """Normalize and check a new entry. Raises EntryInvalid; never echoes secret values."""
    try:
        kind = EntryKind(entry.kind)
    except ValueError:
        raise EntryInvalid(f"kind must be one of {', '.join(k.value for k in EntryKind)}") from None
    name = validate_name(entry.name)
    seed = entry.totp
    if kind is EntryKind.TOTP and seed is None:
        seed, entry = entry.secret, NewEntry(**{**entry.__dict__, "secret": None})
    entry = NewEntry(**{**entry.__dict__, "totp": seed})
    if kind is EntryKind.NOTE:
        domain = validate_domain(entry.domain) if entry.domain else None
    else:
        domain = validate_domain(entry.domain or "")
    hosts = validate_hosts(entry.hosts, domain) if domain else []
    if entry.hosts and not domain:
        raise EntryInvalid("allowed hosts need a domain")
    lanes = validate_lanes(entry.lanes)
    single_line = kind in (EntryKind.PASSWORD, EntryKind.API_KEY, EntryKind.COOKIE, EntryKind.TOTP)
    _check_secret("secret", entry.secret, SECRET_MAX[kind], single_line=single_line)
    _check_secret("username", entry.username, USERNAME_MAX, single_line=True)
    _check_secret("totp seed", entry.totp, SECRET_MAX[EntryKind.TOTP], single_line=True)
    if kind in (EntryKind.PASSWORD, EntryKind.COOKIE, EntryKind.API_KEY, EntryKind.NOTE) and not entry.secret:
        raise EntryInvalid(f"a {kind.value} entry needs a secret")
    if kind is EntryKind.TOTP and not entry.totp:
        raise EntryInvalid("a totp entry needs a seed")
    if kind is EntryKind.TOTP and entry.secret is not None:
        raise EntryInvalid("a totp entry carries only a seed")
    if entry.username is not None and kind is not EntryKind.PASSWORD:
        raise EntryInvalid("only password entries carry a username")
    if entry.totp is not None and kind not in (EntryKind.PASSWORD, EntryKind.TOTP):
        raise EntryInvalid("only password and totp entries carry a TOTP seed")
    totp = entry.totp
    if totp is not None:
        from agentos.vault.totp import canonical_uri

        totp = Secret(canonical_uri(totp.reveal(), label=name, issuer=domain))
    secret = entry.secret
    return NewEntry(
        name=name,
        kind=kind,
        domain=domain,
        hosts=hosts,
        lanes=lanes,
        username=entry.username,
        secret=secret,
        totp=totp,
        config=validate_config(kind, entry.config),
    )
