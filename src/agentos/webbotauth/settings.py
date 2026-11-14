"""A lane's identity settings: `$AGENTOS_HOME/secrets/webbotauth/<workspace>/<name>.json` (mode 600).

    {"lane_id": "acme/research", "signature_agent": "https://agents.example", "form": "bare",
     "keyid": "...", "profile": [dev, inode],
     "hosts": [{"origin": "https://example.com", "reason": "...", "added_by": "owner:cli", "added_at": "..."}]}

`hosts` is the lane's `sign_hosts` list: the origins the lane may sign requests to. It is empty
by default, so a lane signs nothing and gets no route (design sections 5.5 and 12.6). Only the
owner changes it: the file sits in `secrets/` (mode 700, owned by the node's user), the CLI
(`agentos identity hosts add|remove`) is the only writer, and no MCP tool or API route can change
it. An agent can neither read the key nor add a host.

`profile` binds the identity to the lane's profile directory (its device and inode at
`agentos identity init`). A lane deleted and made again under the same id, or a profile copied
in by an import, gets a new directory, so it does not inherit the old lane's identity: signing
stays off until the owner runs `agentos identity init` for it (design section 10, rows 10 and 11).
"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError

from agentos.core.config import write_private
from agentos.webbotauth import base
from agentos.webbotauth.keys import IdentityError, check_private, ensure_private_dirs, identity_dir, lane_path

_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
MAX_HOSTS = 32
MAX_REASON = 200


def _now() -> datetime:
    return datetime.now(UTC)


class SignHost(BaseModel):
    origin: str
    reason: str
    added_by: str
    added_at: datetime = Field(default_factory=_now)


class LaneIdentity(BaseModel):
    lane_id: str
    signature_agent: str | None = None
    form: Literal["bare", "dict"] = "bare"
    keyid: str | None = None
    profile: tuple[int, int] | None = None
    hosts: list[SignHost] = Field(default_factory=list)
    updated_by: str | None = None
    updated_at: datetime = Field(default_factory=_now)

    @property
    def origins(self) -> list[str]:
        return [h.origin for h in self.hosts]

    def host(self, origin: str) -> SignHost | None:
        return next((h for h in self.hosts if h.origin == origin), None)


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_origin(text: str) -> str:
    """An origin the lane may sign for: `https://host[:port]`. A bare host means https. Plain
    `http://` is accepted only for a loopback host (local test servers): a signature sent in the
    clear can be read and replayed by anyone on the path. No paths, wildcards, user info or IPv6
    literals (each origin becomes a Playwright glob, and these keep it exact)."""
    raw = (text or "").strip()
    if not raw:
        raise IdentityError("an empty host")
    if "://" not in raw:
        raw = "https://" + raw
    if any(c in raw for c in "*?[]{}\\ \t") or not raw.isascii():
        raise IdentityError(f"{text!r} is not a plain host (no wildcards, spaces or non-ASCII; use punycode)")
    u = urlsplit(raw)
    scheme = u.scheme.lower()
    host = (u.hostname or "").lower()
    if u.username is not None or u.password is not None:
        raise IdentityError(f"{text!r} holds user info")
    if u.path not in ("", "/") or u.query or u.fragment:
        raise IdentityError(f"{text!r} has a path or query; list the origin only, for example https://example.com")
    try:
        port = u.port
    except ValueError:
        raise IdentityError(f"{text!r} has a bad port") from None
    if port == 0:
        raise IdentityError(f"{text!r} has a bad port")
    if not host or ":" in host:
        raise IdentityError(f"{text!r} has no host, or an IPv6 literal (not supported)")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOST.fullmatch(host):
            raise IdentityError(f"{text!r} is not a valid host name") from None
    if scheme == "http":
        if not is_loopback_host(host):
            raise IdentityError(f"{text!r}: only https origins can be signed for (http only on loopback, for tests)")
    elif scheme != "https":
        raise IdentityError(f"{text!r}: only https origins can be signed for")
    return base.origin_of(f"{scheme}://{host}" + (f":{port}" if port else "") + "/")


def normalize_agent_url(text: str) -> str:
    """The `Signature-Agent` URL: the https origin whose
    /.well-known/http-message-signatures-directory will list the lane's key (Phase 2 publishes it)."""
    raw = (text or "").strip()
    u = urlsplit(raw)
    if u.scheme.lower() != "https":
        raise IdentityError(f"{text!r}: the signature agent must be an https URL")
    origin = normalize_origin(raw)
    return origin


def settings_path(secrets_dir: Path, lane_id: str) -> Path:
    return lane_path(secrets_dir, lane_id, ".json")


def load(secrets_dir: Path, lane_id: str) -> LaneIdentity | None:
    """The lane's settings, or None when it has none. Raises IdentityError for an unsafe or broken file."""
    path = settings_path(secrets_dir, lane_id)
    if not path.exists() and not path.is_symlink():
        return None
    check_private(path.parent, kind="directory")
    check_private(identity_dir(secrets_dir), kind="directory")
    check_private(path)
    try:
        ident = LaneIdentity.model_validate_json(path.read_text())
    except (ValidationError, ValueError) as e:
        raise IdentityError(f"{path} is not valid identity settings: {str(e).splitlines()[0]}") from None
    if ident.lane_id != lane_id:
        raise IdentityError(f"{path} belongs to lane {ident.lane_id}, not {lane_id}")
    for h in ident.hosts:  # a hand edit must not widen what the CLI would accept
        if normalize_origin(h.origin) != h.origin:
            raise IdentityError(f"{path}: {h.origin!r} is not a normalized origin")
    return ident


def save(secrets_dir: Path, ident: LaneIdentity) -> Path:
    ensure_private_dirs(secrets_dir, ident.lane_id)
    path = settings_path(secrets_dir, ident.lane_id)
    write_private(path, json.dumps(json.loads(ident.model_dump_json()), indent=1, sort_keys=True) + "\n")
    return path


def delete(secrets_dir: Path, lane_id: str) -> bool:
    try:
        settings_path(secrets_dir, lane_id).unlink()
    except FileNotFoundError:
        return False
    return True


def profile_binding(profile_dir: Path) -> tuple[int, int] | None:
    try:
        st = Path(profile_dir).stat()
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino)


def clean_reason(reason: str) -> str:
    text = " ".join((reason or "").split())
    if not text:
        raise IdentityError("say why this lane may sign for the host (--reason), for example "
                            "'read-only research, the site accepts signed agents'")
    if len(text) > MAX_REASON:
        raise IdentityError(f"the reason is longer than {MAX_REASON} characters")
    return text


def add_host(ident: LaneIdentity, origin: str, *, reason: str, by: str) -> SignHost:
    origin = normalize_origin(origin)
    entry = SignHost(origin=origin, reason=clean_reason(reason), added_by=by)
    hosts = [h for h in ident.hosts if h.origin != origin]
    if len(hosts) >= MAX_HOSTS:
        raise IdentityError(f"a lane signs for at most {MAX_HOSTS} hosts")
    ident.hosts = [*hosts, entry]
    ident.updated_by, ident.updated_at = by, _now()
    return entry


def remove_host(ident: LaneIdentity, origin: str, *, by: str) -> bool:
    origin = normalize_origin(origin)
    before = len(ident.hosts)
    ident.hosts = [h for h in ident.hosts if h.origin != origin]
    ident.updated_by, ident.updated_at = by, _now()
    return len(ident.hosts) != before
