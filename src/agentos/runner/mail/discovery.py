"""Finding a mailbox's servers from its address (docs/EMAIL.md, "Finding the servers").

Six steps in order, stopping at the first that gives both IMAP and SMTP. Everything here is a pure
function or a step over the `Net` port, so the rules are tested without a network, and nothing here
ever holds a password.

The trust rule: `low` means only DNS or a guess named the host. Such a result is always shown, and
connect.py refuses to sign in until a person said yes to it, so a spoofed record cannot collect a
password. Whatever the trust, a person's yes covers the `digest` of what they were shown and nothing
else. TLS is never optional: a config that asks for none is dropped, whatever step it came from.

Each step reads what a stranger's server or DNS said. A step that goes wrong, whatever it raises, is
skipped, and the next one runs. A config picks only which form of the address is the username, so it
writes no text of its own, and what it did say is labelled for whoever reads it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import xml.parsers.expat
from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol
from urllib.parse import urlsplit

from agentos.core.errors import InvalidRequest, NotFound
from agentos.runner.mail import providers
from agentos.runner.mail.providers import STARTTLS, TLS, Provider
from agentos.runner.workflows.sends import normal_address
from agentos.vault.domains import normalize_host, registrable_domain

log = logging.getLogger("agentos.runner")

ISPDB = "https://autoconfig.thunderbird.net/v1.1/"
MAX_CONFIG = 256 * 1024
MAX_ELEMENTS = 2000
MAX_DEPTH = 16
SECURITY = (TLS, STARTTLS)
SOCKET_TYPES = {"ssl": TLS, "starttls": STARTTLS}  # anything else ("plain") is not a choice
PASSWORD_AUTH = frozenset({"password-cleartext", "plain", "password-encrypted", "secure"})
USERNAMES = ("%EMAILADDRESS%", "%EMAILLOCALPART%")
FROM_OUTSIDE = ["imap", "smtp", "notes"]
IMAP_GUESSES = (("imap.", 993, TLS), ("mail.", 993, TLS), ("imap.", 143, STARTTLS), ("mail.", 143, STARTTLS))
SMTP_GUESSES = (("smtp.", 465, TLS), ("mail.", 465, TLS), ("smtp.", 587, STARTTLS), ("mail.", 587, STARTTLS))

OURS, HTTPS, LOW = "ours", "https", "low"


class DiscoveryFailed(NotFound):
    """No step found both servers. A person can still type them."""
    code = "mail_servers_not_found"


class Refused(ValueError):
    """Text from outside that is not taken: a config, a host, an address."""


@dataclass(frozen=True)
class Server:
    host: str
    port: int
    security: str
    username: str
    typed: bool = False  # a person typed this one, so it may be on a private address


@dataclass(frozen=True)
class Found:
    address: str
    imap: Server
    smtp: Server
    source: str
    trust: str
    provider: str | None = None
    needs_app_password: bool = False
    help_url: str | None = None
    words: str = ""
    notes: tuple[str, ...] = ()

    @property
    def needs_yes(self) -> bool:
        """Only DNS or a guess named a host nobody typed: a person must say yes before any login."""
        return self.trust == LOW and not (self.imap.typed and self.smtp.typed)

    def digest(self) -> str:
        """Exactly what a person is shown: which hosts, ports and TLS, for which address. A yes is to
        this, so it cannot be read as a yes to another answer."""
        shown = [self.address, *((s.host, s.port, s.security, s.username) for s in (self.imap, self.smtp))]
        return hashlib.sha256(json.dumps(shown).encode()).hexdigest()

    def public(self) -> dict[str, Any]:
        out = asdict(self)
        out["notes"] = list(self.notes)
        out["needs_yes"] = self.needs_yes
        out["digest"] = self.digest()
        out["untrusted_fields"] = [] if self.trust == OURS else FROM_OUTSIDE
        return out


@dataclass(frozen=True)
class Srv:
    target: str
    port: int
    priority: int = 0
    weight: int = 0


class Net(Protocol):
    """Everything discovery does outside this process."""

    async def mx(self, domain: str) -> list[str]:
        """The domain's mail exchangers, best first."""

    async def srv(self, name: str) -> list[Srv]: ...

    async def addresses(self, host: str) -> list[str]:
        """What a name resolves to."""

    async def fetch(self, url: str) -> bytes | None:
        """An https document, or None: see net.py for what a fetch refuses."""

    async def answers_tls(self, host: str, port: int, security: str) -> bool:
        """Does this host answer with a verified certificate? Sends no credential."""


# ---- pure rules ------------------------------------------------------------------

def split_address(address: Any) -> tuple[str, str, str]:
    """(address, local part, domain), or InvalidRequest."""
    normal = normal_address(address)
    if normal is None:
        raise InvalidRequest("That is not an email address.")
    local, _, domain = normal.rpartition("@")
    return normal, local, domain


def is_public(address: str) -> bool:
    """Is this an address on the open internet? An IPv4 address inside IPv6 is read as the IPv4 one."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inner is None and ip in ipaddress.ip_network("64:ff9b::/96"):
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is not None:
            ip = inner
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def all_public(addresses: list[str]) -> bool:
    """Every address a name gave is public: one private answer among them is enough to refuse."""
    return bool(addresses) and all(is_public(a) for a in addresses)


def check_https_url(url: str) -> str:
    """The host of an https URL that may be fetched, or Refused."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:  # a Location can say anything: `https://x:abc/`, `https://[bad/`
        raise Refused("that is not an address") from None
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise Refused("only a plain https address is fetched")
    if port not in (None, 443):
        raise Refused("only the https port is fetched")
    try:
        return normalize_host(host)
    except ValueError:
        raise Refused("that host is not a host") from None


def redirect_target(base: str, location: str | None) -> str:
    """Where a redirect may go: https only, never a downgrade, or Refused."""
    if not location:
        raise Refused("a redirect with nowhere to go")
    from urllib.parse import urljoin

    try:
        target = urljoin(base, location)
    except ValueError:
        raise Refused("that is not an address") from None
    check_https_url(target)
    return target


def parse_xml(data: bytes) -> list[tuple[str, dict[str, str], str, int]]:
    """A config as (element, attributes, text, depth) in document order. A DOCTYPE is refused outright,
    so no entity can be declared, expanded or fetched: only the five built-in ones exist."""
    if len(data) > MAX_CONFIG:
        raise Refused("that config is too big")
    parser = xml.parsers.expat.ParserCreate()
    rows: list[list[Any]] = []
    stack: list[list[Any]] = []

    def refuse(*_: Any) -> None:
        raise Refused("a config may not declare a DOCTYPE or entities")

    def start(name: str, attrs: dict[str, str]) -> None:
        if len(rows) >= MAX_ELEMENTS or len(stack) >= MAX_DEPTH:
            raise Refused("that config is too big")
        row = [name, attrs, "", len(stack)]
        rows.append(row)
        stack.append(row)

    def end(_: str) -> None:
        stack.pop()

    def text(value: str) -> None:
        if stack:
            stack[-1][2] += value

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.StartElementHandler, parser.EndElementHandler, parser.CharacterDataHandler = start, end, text
    try:
        parser.Parse(data, True)
    except (xml.parsers.expat.ExpatError, LookupError, ValueError):  # LookupError: an encoding that is no encoding
        raise Refused("that config is not XML") from None
    return [(r[0], r[1], r[2].strip(), r[3]) for r in rows]


def _fill(template: str, address: str, local: str, domain: str) -> str:
    return (template.replace("%EMAILADDRESS%", address).replace("%EMAILLOCALPART%", local)
            .replace("%EMAILDOMAIN%", domain))


def _server(fields: dict[str, list[str]], address: str, local: str, domain: str) -> Server | None:
    """One config server, or None when it cannot be used: no TLS, no password sign-in, a bad host."""
    security = SOCKET_TYPES.get((fields.get("socketType") or [""])[0].lower())
    auth = {a.lower() for a in fields.get("authentication", [])}
    if security is None or (auth and not auth & PASSWORD_AUTH):
        return None
    try:
        host = normalize_host(_fill((fields.get("hostname") or [""])[0], address, local, domain))
        port = int((fields.get("port") or [""])[0])
    except ValueError:
        return None
    if not 0 < port < 65_536:
        return None
    template = (fields.get("username") or [""])[0]
    return Server(host, port, security, _fill(template, address, local, domain) if template in USERNAMES else address)


def parse_config(data: bytes, address: str) -> tuple[Server, Server] | None:
    """(IMAP, SMTP) from a Thunderbird autoconfig document, implicit TLS first, or None when it does
    not offer both over TLS. Refused when the document itself is not acceptable."""
    address, local, domain = split_address(address)
    groups: dict[str, list[Server]] = {"imap": [], "smtp": []}
    rows = parse_xml(data)
    i = 0
    while i < len(rows):
        name, attrs, _, depth = rows[i]
        i += 1
        if name not in ("incomingServer", "outgoingServer"):
            continue
        fields: dict[str, list[str]] = {}
        while i < len(rows) and rows[i][3] > depth:
            fields.setdefault(rows[i][0], []).append(rows[i][2])
            i += 1
        kind = attrs.get("type", "").lower()
        if kind in groups:
            server = _server(fields, address, local, domain)
            if server is not None:
                groups[kind].append(server)
    best = [sorted(groups[k], key=lambda s: SECURITY.index(s.security)) for k in ("imap", "smtp")]
    return (best[0][0], best[1][0]) if best[0] and best[1] else None


def from_provider(address: str, provider: Provider, source: str = "table") -> Found:
    def server(row: tuple[str, int, str]) -> Server:
        return Server(row[0], row[1], row[2], address)

    return Found(address, server(provider.imap), server(provider.smtp), source, OURS, provider.id,
                 provider.needs_app_password, provider.help_url, provider.words)


def pick_srv(records: list[Srv]) -> Srv | None:
    """The best record. A target of `.` says the service is not offered here (RFC 2782)."""
    usable = [r for r in records if r.target.rstrip(".") and 0 < r.port < 65_536]
    return min(usable, key=lambda r: (r.priority, -r.weight)) if usable else None


def typed(found: Found, *, imap: dict[str, Any] | None = None, smtp: dict[str, Any] | None = None) -> Found:
    """What a person corrected. A host they typed counts as seen, and may be private."""
    def apply(server: Server, change: dict[str, Any] | None) -> Server:
        return typed_server(change, fallback=server) if change else server

    return replace(found, imap=apply(found.imap, imap), smtp=apply(found.smtp, smtp))


def typed_server(fields: dict[str, Any], *, fallback: Server | None = None, username: str = "") -> Server:
    """One server as a person gave it. A security level other than the two is not offered."""
    host = fields.get("host") or (fallback.host if fallback else "")
    port = fields.get("port") or (fallback.port if fallback else 0)
    security = fields.get("security") or (fallback.security if fallback else TLS)
    try:
        host, port = normalize_host(str(host)), int(port)
    except (TypeError, ValueError):
        raise InvalidRequest("That server needs a host and a port.") from None
    if not 0 < port < 65_536:
        raise InvalidRequest("A port is a number from 1 to 65535.")
    if security not in SECURITY:
        raise InvalidRequest("A mail server is reached over tls or starttls. Nothing else is offered.")
    name = str(fields.get("username") or (fallback.username if fallback else username))
    if not name or len(name) > 200 or re.search(r"[\r\n\x00]", name):
        raise InvalidRequest("That is not a username.")
    return Server(host, port, security, name, typed=True)


# ---- the steps -------------------------------------------------------------------

async def _config_at(net: Net, url: str, address: str, notes: list[str]) -> tuple[Server, Server] | None:
    try:
        data = await net.fetch(url)
        return parse_config(data, address) if data else None
    except Refused as e:
        notes.append(f"Skipped a config from {urlsplit(url).hostname}: {e}.")
    except Exception as e:  # noqa: BLE001 - whatever a stranger's answer can raise, this step is over
        log.debug("a config step was skipped (%s)", type(e).__name__)
        notes.append(f"Skipped a config from {urlsplit(url).hostname}.")
    return None


async def _safe(net: Net, found: Found, notes: list[str]) -> Found | None:
    """A host named from outside must not lead inside: check what each one resolves to."""
    for server in (found.imap, found.smtp):
        try:
            answers = await net.addresses(server.host)
        except Exception:  # noqa: BLE001
            answers = []
        if not all_public(answers):
            notes.append(f"Refused {server.host}: it does not resolve to a public address.")
            return None
    return found


async def _guess(net: Net, domain: str, guesses: tuple[tuple[str, int, str], ...], username: str) -> Server | None:
    for prefix, port, security in guesses:
        host = f"{prefix}{domain}"
        try:
            if all_public(await net.addresses(host)) and await net.answers_tls(host, port, security):
                return Server(host, port, security, username)
        except Exception:  # noqa: BLE001, S112
            continue
    return None


async def _srv_server(net: Net, name: str, security: str, username: str) -> Server | None:
    try:
        best = pick_srv(await net.srv(name))
        return Server(normalize_host(best.target), best.port, security, username) if best else None
    except Exception:  # noqa: BLE001
        return None


async def discover(address: Any, net: Net) -> Found:
    """The servers for an address. Only the domain leaves this node, never the address."""
    address, _, domain = split_address(address)
    notes: list[str] = []

    known = providers.by_domain(domain)
    if known is not None:
        return from_provider(address, known)
    try:
        exchangers = await net.mx(domain)
    except Exception:  # noqa: BLE001
        exchangers = []
    known = providers.by_mx(exchangers)
    if known is not None:
        return from_provider(address, known, "table_mx")

    async def from_config(urls: list[str], source: str) -> Found | None:
        for url in urls:
            pair = await _config_at(net, url, address, notes)
            if pair is not None:
                found = await _safe(net, Found(address, pair[0], pair[1], source, HTTPS), notes)
                if found is not None:
                    return found
        return None

    found = await from_config([f"{ISPDB}{domain}"], "ispdb")
    found = found or await from_config([f"https://autoconfig.{domain}/mail/config-v1.1.xml",
                                        f"https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml"], "autoconfig")
    if found is None:
        for host in exchangers[:3]:
            try:
                parent = registrable_domain(host)
            except Exception:  # noqa: BLE001
                parent = None
            if parent and parent != domain:
                found = await from_config([f"{ISPDB}{parent}"], "ispdb_mx")
                if found is not None:
                    break
    if found is None:
        imap = await _srv_server(net, f"_imaps._tcp.{domain}", TLS, address)
        smtp = await _srv_server(net, f"_submissions._tcp.{domain}", TLS, address) or \
            await _srv_server(net, f"_submission._tcp.{domain}", STARTTLS, address)
        if imap is not None and smtp is not None:
            found = await _safe(net, Found(address, imap, smtp, "srv", LOW), notes)
    if found is None:
        imap = await _guess(net, domain, IMAP_GUESSES, address)
        smtp = await _guess(net, domain, SMTP_GUESSES, address) if imap is not None else None
        if imap is not None and smtp is not None:
            found = Found(address, imap, smtp, "guess", LOW)
    if found is None:
        raise DiscoveryFailed(f"No mail servers were found for {domain}. Type them in, and they are used as you typed them.")
    return replace(found, notes=tuple(notes))
