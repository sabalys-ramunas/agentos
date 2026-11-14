"""Remote URLs, repository names, pkt-lines and the receive-pack report.

Pure functions, no I/O, so the gateway's parsing can be tested on bytes alone. Everything here
reads untrusted input (a URL an agent typed, the body of a push), so it refuses rather than
guesses: a host or a repository name it cannot read whole is an error, never a best effort.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from agentos.core.errors import InvalidRequest

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")
_SEGMENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]{0,99}$")
_SCP_RE = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>[^:]+)$")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REF_PART_BAD = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
MAX_REPO_DEPTH = 4  # owner/name, or group/subgroup/.../name on hosts that nest (GitLab)
SERVICES = ("info/refs", "git-upload-pack", "git-receive-pack")
FLUSH = b"0000"


@dataclass(frozen=True)
class Remote:
    """A repository on a host: `github.com` and `owner/name` (lower case, without `.git`)."""
    host: str
    repo: str

    @property
    def slug(self) -> str:
        return f"{self.host}/{self.repo}"


def normalize_host(host: str) -> str:
    """A host name in lower case without a trailing dot; InvalidRequest when it is not one."""
    h = (host or "").strip().lower().rstrip(".")
    if not h or len(h) > 253 or not HOST_RE.match(h):
        raise InvalidRequest(f"not a host name: {host[:80]!r}")
    return h


def normalize_repo(path: str) -> str:
    """`owner/name` from a URL path (`/owner/name.git/`), in lower case. Refuses `..`, a leading
    dot or dash in any part, and more than MAX_REPO_DEPTH parts."""
    p = unquote(path or "").strip().strip("/")
    p = p.removesuffix(".git")
    parts = p.lower().split("/")
    if not 2 <= len(parts) <= MAX_REPO_DEPTH:
        raise InvalidRequest(f"not a repository path (owner/name): {path[:120]!r}")
    for part in parts:
        if part in (".", "..") or not _SEGMENT_RE.match(part) or part.startswith("."):
            raise InvalidRequest(f"not a repository path (owner/name): {path[:120]!r}")
    return "/".join(parts)


def repo_matches(pattern: str, repo: str) -> bool:
    """`owner/name` matches itself; `owner/*` matches every repository directly under owner."""
    if pattern.endswith("/*"):
        head = pattern[:-2]
        return repo.startswith(head + "/") and "/" not in repo[len(head) + 1:]
    return pattern == repo


def normalize_repo_pattern(pattern: str) -> str:
    """A repository or `owner/*`."""
    p = (pattern or "").strip().strip("/").lower()
    if p.endswith("/*"):
        head = p[:-2]
        normalize_repo(head + "/x")  # the owner part must be a valid path
        return head + "/*"
    return normalize_repo(p)


def parse_remote(url: str) -> Remote:
    """The host and repository of a git remote: `https://host/owner/name(.git)`,
    `ssh://[user@]host[:port]/owner/name(.git)`, `user@host:owner/name(.git)`, or the short
    `host/owner/name` the CLI takes. A URL that carries a password or a token is refused: a
    credential belongs in the vault, never in a remote."""
    u = (url or "").strip()
    if not u:
        raise InvalidRequest("empty remote")
    m = _SCP_RE.match(u)
    if m and "://" not in u:
        return Remote(normalize_host(m.group("host")), normalize_repo(m.group("path")))
    if "://" in u:
        parts = urlsplit(u)
        if parts.scheme not in ("https", "http", "ssh", "git+ssh"):
            raise InvalidRequest(f"a git remote is https://, ssh:// or user@host:path, not {parts.scheme}://")
        if parts.password is not None or (parts.scheme.startswith("http") and parts.username):
            raise InvalidRequest("this remote URL carries a credential; store it with `agentos git key add` and "
                                 "use the URL without it")
        if parts.query or parts.fragment:
            raise InvalidRequest("a git remote URL has no query or fragment")
        return Remote(normalize_host(parts.hostname or ""), normalize_repo(parts.path))
    host, _, path = u.partition("/")
    return Remote(normalize_host(host), normalize_repo(path))


def parse_gateway_path(path: str) -> tuple[Remote, str]:
    """`/<host>/<owner>/<name>(.git)/<service>` -> (Remote, service), service being one of SERVICES.
    This is the URL git asks for once the lane's `insteadOf` has rewritten a remote."""
    raw = path.split("?", 1)[0]
    for service in SERVICES:
        if raw.endswith("/" + service):
            rest = raw[: -len(service) - 1].lstrip("/")
            host, _, repo = rest.partition("/")
            return Remote(normalize_host(host), normalize_repo(repo)), service
    raise InvalidRequest("not a git smart-HTTP path")


# ------------------------------------------------------------------ pkt-lines


def pkt(data: bytes | str) -> bytes:
    """One pkt-line: four hex digits of length (including themselves), then the data."""
    b = data.encode() if isinstance(data, str) else data
    if len(b) > 65516:
        raise ValueError("pkt-line too long")
    return b"%04x" % (len(b) + 4) + b


def read_pkts(buf: bytes, pos: int = 0) -> tuple[list[bytes | None], int]:
    """pkt-lines from `pos` up to and including the first flush (None marks it). Returns them and
    the offset after the flush. InvalidRequest on a malformed or short stream."""
    out: list[bytes | None] = []
    while True:
        head = buf[pos:pos + 4]
        if len(head) < 4:
            raise InvalidRequest("the git request ended in the middle of a pkt-line")
        try:
            n = int(head, 16)
        except ValueError:
            raise InvalidRequest("malformed pkt-line") from None
        if n == 0:
            out.append(None)
            return out, pos + 4
        if n < 4 or pos + n > len(buf):
            raise InvalidRequest("malformed pkt-line")
        out.append(buf[pos + 4:pos + n])
        pos += n


def is_zero(sha: str) -> bool:
    return bool(sha) and set(sha) == {"0"}


def check_sha(sha: str) -> str:
    if not _SHA_RE.match(sha):
        raise InvalidRequest("a push command names an object that is not a full commit id")
    return sha


def check_ref(ref: str) -> str:
    """A branch or tag name a push may update (`refs/heads/...`, `refs/tags/...`), checked as
    `git check-ref-format` would. Every other namespace is refused."""
    if not (ref.startswith(("refs/heads/", "refs/tags/"))):
        raise InvalidRequest(f"a push may update branches and tags only, not {ref[:120]!r}")
    if len(ref) > 255 or ".." in ref or "@{" in ref or ref.endswith((".", "/", ".lock")) or "//" in ref:
        raise InvalidRequest(f"not a valid ref name: {ref[:120]!r}")
    for part in ref.split("/"):
        if not part or part.startswith(".") or _REF_PART_BAD.search(part):
            raise InvalidRequest(f"not a valid ref name: {ref[:120]!r}")
    return ref


@dataclass(frozen=True)
class Command:
    """One ref update of a push: `old` is what the client believes the remote has."""
    old: str
    new: str
    ref: str

    @property
    def kind(self) -> str:
        if is_zero(self.old):
            return "create"
        if is_zero(self.new):
            return "delete"
        return "update"

    @property
    def branch(self) -> str | None:
        return self.ref[len("refs/heads/"):] if self.ref.startswith("refs/heads/") else None

    @property
    def tag(self) -> str | None:
        return self.ref[len("refs/tags/"):] if self.ref.startswith("refs/tags/") else None


@dataclass
class Push:
    commands: list[Command]
    capabilities: set[str]

    @property
    def sideband(self) -> bool:
        return bool({"side-band-64k", "side-band"} & self.capabilities)


def parse_push(body: bytes) -> Push:
    """The ref updates at the head of a receive-pack request (before the pack). Shallow lines are
    skipped; a signed push is refused (this gateway never offers push certificates)."""
    lines, _ = read_pkts(body)
    commands: list[Command] = []
    caps: set[str] = set()
    for raw in lines:
        if raw is None:
            break
        text, _, capstr = raw.rstrip(b"\n").partition(b"\0")
        if not commands and capstr:
            caps = {c.decode("ascii", "replace") for c in capstr.split() if c}
        line = text.decode("utf-8", "replace")
        if line.startswith("shallow "):
            continue
        if line.startswith("push-cert"):
            raise InvalidRequest("signed pushes are not supported through the node")
        fields = line.split(" ")
        if len(fields) != 3:
            raise InvalidRequest("malformed push command")
        old, new, ref = fields
        commands.append(Command(check_sha(old), check_sha(new), check_ref(ref)))
    if not commands:
        raise InvalidRequest("a push with no ref updates")
    if len({c.ref for c in commands}) != len(commands):
        raise InvalidRequest("a push names the same ref twice")
    return Push(commands, caps)


def demux(output: bytes, sideband: bool) -> tuple[bytes, list[str]]:
    """Split a receive-pack response into its report (band 1) and its messages (bands 2 and 3).
    Without a side band the whole output is the report."""
    if not sideband:
        return output, []
    data, messages, pos = b"", [], 0
    while pos < len(output):
        lines, pos = read_pkts(output, pos)
        for line in lines:
            if line is None or not line:
                continue
            band, payload = line[0], line[1:]
            if band == 1:
                data += payload
            else:
                messages.append(payload.decode("utf-8", "replace"))
    return data, messages


def parse_report(report: bytes) -> tuple[str, dict[str, str | None]]:
    """(unpack status, {ref: None for ok, or the reason it was refused}) from a report-status."""
    lines, _ = read_pkts(report)
    unpack = ""
    refs: dict[str, str | None] = {}
    for raw in lines:
        if raw is None:
            break
        line = raw.rstrip(b"\n").decode("utf-8", "replace")
        if line.startswith("unpack "):
            unpack = line[len("unpack "):]
        elif line.startswith("ok "):
            refs[line[3:]] = None
        elif line.startswith("ng "):
            ref, _, why = line[3:].partition(" ")
            refs[ref] = why or "refused"
    return unpack, refs


def build_report(results: Mapping[str, str | None], *, sideband: bool, messages: Sequence[str] = (),
                 unpack: str = "ok") -> bytes:
    """A receive-pack report: `ok <ref>` or `ng <ref> <reason>` for each ref, and, on a side band,
    `messages` first on band 2 (git prints them as `remote: ...`)."""
    inner = pkt(f"unpack {unpack}\n")
    for ref, why in results.items():
        inner += pkt(f"ok {ref}\n" if why is None else f"ng {ref} {_one_line(why)}\n")
    inner += FLUSH
    if not sideband:
        return inner
    out = b""
    for msg in messages:
        for line in msg.splitlines() or [""]:
            out += pkt(b"\x02" + (line[:900] + "\n").encode())
    for i in range(0, len(inner), 65000):
        out += pkt(b"\x01" + inner[i:i + 65000])
    return out + FLUSH


def error_advertisement(service: str, message: str) -> bytes:
    """A smart-HTTP ref advertisement that is an error: git prints `remote error: <message>`."""
    return pkt(f"# service={service}\n") + FLUSH + pkt(f"ERR {_one_line(message)}\n")


def _one_line(text: str) -> str:
    return " ".join(str(text).split())[:900]
