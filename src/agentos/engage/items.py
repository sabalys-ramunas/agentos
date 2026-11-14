"""The items inbox (brief B4): approved content made outside agentOS joins a surface's schedule.

    inbox = ItemsInbox(store, registry=Registry(), clock=time.time, files=lane_file_checker,
                       approvals=lambda: runtime.get("approvals"))
    plan = inbox.check(surface, kind, text, title=..., targets=[...], attachments=[...], expires_at=...)
    item = inbox.submit(plan, in_person=True)              # the owner, at the node
    item = inbox.submit(plan, requested_by=p, approval_id="ap_...")   # anyone else, after the owner approved
    inbox.withdraw(item_id, by=principal)
    inbox.sweep()                 # approval outcomes, reservations whose window is over, expiry

    content = ChainedContent([ItemsContent(store)])       # the executor's ContentPort on the node

An item is approved content: every item is recorded as submitted by the owner, and the executor
runs it with `approved=True` (no second approval). So nobody but the owner in person adds one on
their own: the tool (agentos.engage.tools.engage_item_submit) asks the owner to approve the exact
item first, attachments included (each file's sha256, name, mime and size), and `submit` checks
that the approval is that one, used once.

Items add no work. A window takes an item only for an operation its plan already has, under the
same daily and hourly caps; with five posts queued and one post a day planned, one goes out a day.

`ItemsContent.take` reserves the oldest unexpired items of (surface, kind) in one transaction (two
ticks never reserve the same item) for the window the executor names (ports.CONTENT_SESSION).
`settle(used=False)` puts the item back in the queue; `settle(used=True)` makes it `used` (with
the run id and `item_used` in the history), `awaiting` while its run waits for an approval card
(back to `queued` if the owner denies it or it expires, `used` once it goes out), or `failed` when
the run failed after it may have acted (left for the owner, never offered again). A reservation
whose window is over without a settle (the session ended, or it is past its planned end and
WINDOW_GRACE, as a cancelled or crashed window is) goes back to the queue on the next sweep, or
expires when it is past its expiry. Every change writes its history line in the same transaction.

`ChainedContent` is how the drafter (brief B3) plugs in next to items: it asks its ports in order
(items first) and routes each settle back to the port that produced the content, by the content's
id (and, after a restart, by `Content.origin` against each port's `origin`).
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from agentos.core.errors import InvalidRequest, NotFound
from agentos.engage.errors import OwnerOnly
from agentos.engage.governor import is_owner
from agentos.engage.models import Program
from agentos.engage.operations import Registry
from agentos.engage.ports import CONTENT_SESSION, Content, ContentOrigin, ContentPort, ContentUnavailable
from agentos.engage.store import EngageStore, iso

log = logging.getLogger("agentos.engage.items")

ITEM_ID_RE = re.compile(r"^it_[0-9a-f]{16}$")
DEFAULT_TTL = timedelta(days=7)
MAX_TTL = timedelta(days=30)
ABANDONED_AFTER = timedelta(hours=2)       # a reservation with no session, never settled
WINDOW_GRACE = timedelta(hours=2)          # past a window's planned end: its reservations are over
SUBMIT_TOOL = "engage_item_submit"         # the tool an agent's item approval is for
# what came of a run that waited for an approval card and did not go out: nothing was sent
NOT_SENT = frozenset({"denied", "approval_expired", "approval_used", "edit_mismatch", "approval_loop",
                      "approval_mismatch"})
MAX_TARGETS = 1                            # the executor acts on one target per operation
ATTACHMENT_KINDS = ("image", "video")      # agentos.node.files FileType.kind
OWNER_ID = "owner"

SUBREDDIT_RE = re.compile(r"^(r/)?[A-Za-z0-9_]{3,21}$")
SITE_DOMAINS = {"instagram": "instagram.com", "facebook": "facebook.com", "linkedin": "linkedin.com",
                "reddit": "reddit.com", "medium": "medium.com"}


def https_link(hosts: Iterable[str] | None = None, *, domain: str | None = None,
               path: str | None = None) -> Callable[[str], bool]:
    """A check of a link, parsed with urllib: https, no user info or port, a host that is exactly
    one of `hosts` (or `domain` or a subdomain of it; any host with a dot when neither is given),
    and a path that fully matches `path`. `https://www.instagram.com@evil.tld/x` names evil.tld."""
    allowed = frozenset(h.lower() for h in hosts or ())
    path_re = re.compile(path) if path else None

    def ok(value: str) -> bool:
        if any(c.isspace() for c in value) or "\\" in value:
            return False
        try:
            u = urlsplit(value)
            port = u.port
        except ValueError:
            return False
        host = u.hostname or ""
        if u.scheme != "https" or port is not None or "@" in u.netloc or u.netloc.lower() != host:
            return False
        if allowed:
            host_ok = host in allowed
        elif domain:
            host_ok = host == domain or host.endswith("." + domain)
        else:
            host_ok = "." in host.strip(".")
        return host_ok and (path_re is None or bool(path_re.fullmatch(u.path)))

    return ok


ANY_LINK = https_link()


def _pattern(regex: re.Pattern[str]) -> Callable[[str], bool]:
    return lambda value: bool(regex.match(value))


@dataclass(frozen=True)
class Limits:
    """What a site takes for one kind: text length, title length, attachments, and the check of
    a target (None: the kind acts on nothing)."""
    text: int = 2200
    title: int = 300
    attachments: int = 10
    target: Callable[[str], bool] | None = ANY_LINK
    target_hint: str = "a full https:// link to the post"


SITE_LIMITS: dict[tuple[str, str], Limits] = {
    # connector.reddit.post_text: a text post (title up to 300, body up to 40000), no media
    ("reddit", "post"): Limits(text=40_000, title=300, attachments=0, target=_pattern(SUBREDDIT_RE),
                               target_hint="a subreddit name, such as r/coffee"),
    ("reddit", "comment"): Limits(
        text=10_000, title=0, attachments=0,
        target=https_link(("www.reddit.com", "reddit.com", "old.reddit.com"),
                          path=r"/r/[A-Za-z0-9_]{3,21}/comments/[a-z0-9]+(/[^?#]*)?"),
        target_hint="a Reddit post link, such as https://www.reddit.com/r/coffee/comments/abc123/"),
    ("linkedin", "post"): Limits(text=3_000, title=0, attachments=9, target=None),
    ("linkedin", "comment"): Limits(text=1_250, title=0, attachments=1,
                                    target=https_link(("www.linkedin.com",), path=r"/(feed/update|posts)/\S+"),
                                    target_hint="a LinkedIn post link, such as https://www.linkedin.com/feed/update/..."),
}


def limits_for(site: str, kind: str) -> Limits:
    found = SITE_LIMITS.get((site, kind))
    if found is not None:
        return found
    domain = SITE_DOMAINS.get(site)
    return Limits(target=https_link(domain=domain),
                  target_hint=f"a full https:// link to the post on {domain}" if domain else Limits.target_hint)


def _who(p: Any) -> dict[str, Any] | None:
    if p is None:
        return None
    d = p if isinstance(p, dict) else (p.model_dump() if hasattr(p, "model_dump") else dict(vars(p)))
    return {k: d.get(k) for k in ("kind", "id", "owner", "grant_path") if d.get(k) is not None}


def _same(a: Any, b: Any) -> bool:
    """`a` asked for what `b` asked: `b` came from `a`'s grant or one narrowed from it (`b`'s grant path
    starts with `a`'s), never only the same subject id, which two grants may share, and never a
    narrower child of the asker's. An item recorded with no grant path is withdrawn by the owner."""
    a, b = _who(a) or {}, _who(b) or {}
    mine, theirs = a.get("grant_path"), b.get("grant_path")
    return isinstance(mine, str) and mine.startswith("/") and isinstance(theirs, str) and theirs.startswith(mine)


def _parse_time(value: str) -> datetime:
    """An ISO 8601 time with its offset. A date alone, or a time with no offset, is refused: the
    node would have to guess whose midnight or whose clock it means."""
    example = "2026-11-12T18:00:00+02:00 or 2026-11-12T16:00:00Z"
    text = value.strip() if isinstance(value, str) else ""
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        raise InvalidRequest(f"expires_at {str(value)[:40]!r} is not a time; use ISO 8601 with a time zone, "
                             f"such as {example}") from None
    if "T" not in text.upper() and " " not in text:
        raise InvalidRequest(f"expires_at {text[:40]!r} is a date without a time; give a time with a time zone, "
                             f"such as {example}")
    if t.tzinfo is None or t.utcoffset() is None:
        raise InvalidRequest(f"expires_at {text[:40]!r} has no time zone; add one, such as {example}")
    return t


@dataclass(frozen=True)
class ItemPlan:
    """A checked item, not yet queued. `canonical` is what the owner approves (and what an
    approval is bound to): the same arguments always give the same canonical item."""
    surface: str
    kind: str
    text: str
    title: str | None
    targets: tuple[str, ...]
    attachments: tuple[str, ...]
    expires_at: datetime | None       # None: DEFAULT_TTL after it is queued
    warnings: tuple[str, ...] = ()
    files: tuple[dict[str, Any], ...] = ()   # per attachment: id, name, mime, size, sha256

    @property
    def canonical(self) -> dict[str, Any]:
        """What the owner approves: the attachments are the files themselves (sha256, name, mime
        and size), so an approval never covers other bytes under the same file id."""
        return {"item": {"surface": self.surface, "kind": self.kind, "text": self.text, "title": self.title,
                         "targets": list(self.targets), "attachments": [dict(f) for f in self.files],
                         "expires_at": iso(self.expires_at) if self.expires_at else f"{DEFAULT_TTL.days} days after "
                                                                                    "it is queued"}}

    @property
    def summary(self) -> str:
        where = f" to {self.targets[0]}" if self.targets else ""
        out = (f"Add an approved {self.kind} for {self.surface}{where} to its schedule. It goes out without "
               f"asking you again, in a session the program already plans.")
        if self.files:
            from agentos.node.files import human_size

            names = "; ".join(f"{f['name']} ({f['mime'] or 'unknown type'}, {human_size(int(f['size'] or 0))}, "
                              f"sha256 {str(f['sha256'] or 'unknown')[:16]})" for f in self.files)
            out += f" Attachment{'s' if len(self.files) != 1 else ''}: {names}."
        return out


FileCheck = Callable[[str, str], Any]   # (surface, file id) -> the stored file (kind, status), or raise NotFound


def lane_file_check(runtime: Any) -> FileCheck | None:
    """The node's FileCheck: a surface's files are those of the lane it acts through (its binding's
    lane, else the lane of the same name). None without a node config (the CLI, tests)."""
    config = runtime.get("config") if runtime is not None else None
    if config is None:
        return None

    def check(surface: str, file_id: str) -> Any:
        from agentos.node.io_tools import lane_files

        surfaces = runtime.get("surfaces")
        found = surfaces.find(surface) if surfaces is not None else None
        return lane_files(runtime, found.lane if found is not None else surface).get(file_id)

    return check


class ItemsInbox:
    """Checks, queues, lists, withdraws and sweeps items. `files(surface, file_id)` returns the
    stored file on the surface's lane (agentos.node.files.StoredFile), or raises."""

    def __init__(self, store: EngageStore, *, registry: Registry | None = None, clock: Callable[[], float] = time.time,
                 files: FileCheck | None = None, approvals: Callable[[], Any] | None = None,
                 lane_gone: Callable[[str], str | None] | None = None):
        self.store = store
        self.registry = registry or Registry()
        self.clock = clock
        self.files = files
        self.approvals = approvals     # () -> the node's ApprovalStore (agentos.policy.approvals), or None
        # surface -> why the lane it acts through is gone, or None (agentos.engage.service.lane_gone_check)
        self.lane_gone = lane_gone

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    # ---------------------------------------------------------------- checking
    def check(self, surface: str, kind: str, text: str, *, title: str | None = None,
              targets: Iterable[str] | None = None, attachments: Iterable[str] | None = None,
              expires_at: str | None = None) -> ItemPlan:
        pv = self.store.active(surface)
        if pv is None:
            raise InvalidRequest(f"{surface} has no approved engagement program, so nothing would take an item there. "
                                 "The owner approves a program first")
        program: Program = pv.definition
        site = program.site or ""
        kind = str(kind).strip().lower()
        spec = self.registry.get(site, kind)
        if spec is None or not spec.needs_text:
            takes = sorted(str(s.kind) for s in self.registry.specs(site) if s.needs_text)
            raise InvalidRequest(f"{site} has no {kind!r} that takes words; items can be: "
                                 f"{', '.join(takes) if takes else 'nothing on this site yet'}")
        self._can_run(site, kind)
        dead = self.never_runs(surface, kind)
        if dead is not None:
            raise InvalidRequest(f"{dead[0]}, so this item could never go out and nothing was queued. {dead[1]}")
        lim = limits_for(site, kind)
        text = (text or "").strip()
        if not text:
            raise InvalidRequest("the item has no text")
        if "\x00" in text or (title and "\x00" in title):
            raise InvalidRequest("the item's text has a NUL character")
        max_text = lim.text
        if kind == "comment":
            max_text = min(max_text, program.comments.max_chars)
        if len(text) > max_text:
            raise InvalidRequest(f"the text is {len(text)} characters; a {kind} on {surface} takes at most {max_text}"
                                 + (" (the program's comments.max_chars)" if max_text == program.comments.max_chars
                                    and kind == "comment" else ""))
        title = (title or "").strip() or None
        if title is not None:
            if lim.title == 0:
                raise InvalidRequest(f"a {kind} on {site} has no title; leave it out")
            if len(title) > lim.title:
                raise InvalidRequest(f"the title is {len(title)} characters; {site} takes at most {lim.title}")
        if isinstance(targets, str):
            targets = [targets]
        if isinstance(attachments, str):
            attachments = [attachments]
        found_targets = tuple(str(t).strip() for t in (targets or []) if str(t).strip())
        if len(found_targets) > MAX_TARGETS:
            raise InvalidRequest("give one target per item (a session acts on one target per operation)")
        if found_targets and (lim.target is None or not spec.needs_target):
            raise InvalidRequest(f"a {kind} on {site} acts on nothing; leave targets out")
        for t in found_targets:
            assert lim.target is not None
            if len(t) > 500 or not lim.target(t):
                raise InvalidRequest(f"target {t[:60]!r} is not {lim.target_hint}")
        found_files = tuple(dict.fromkeys(str(a).strip() for a in (attachments or []) if str(a).strip()))
        if len(found_files) > lim.attachments:
            raise InvalidRequest(f"a {kind} on {site} takes no attachments" if lim.attachments == 0 else
                                 f"a {kind} on {site} takes at most {lim.attachments} attachments")
        files = tuple(self._check_file(surface, fid) for fid in found_files)
        now = self.now()
        expires: datetime | None = None
        if expires_at:
            expires = _parse_time(expires_at).astimezone(UTC).replace(microsecond=0)
            if expires <= now:
                raise InvalidRequest("expires_at is in the past; give a time later than now")
            if expires - now > MAX_TTL:
                raise InvalidRequest(f"expires_at is more than {MAX_TTL.days} days away; items wait at most "
                                     f"{MAX_TTL.days} days")
        warnings = []
        if not program.steady.daily.get(_kind(kind), 0):
            warnings.append(f"the program plans no {kind} on {surface}, so this item waits until it does "
                            "(and expires if it never does)")
        elif kind == "comment" and program.comments.mode.value == "off":
            warnings.append(f"comments are off in the program of {surface}, so this item waits until they are on")
        if spec.needs_target and not found_targets:
            warnings.append(f"the item names no target, so it goes out only once there is a target to {kind} on")
        return ItemPlan(surface, kind, text, title, found_targets, found_files, expires, tuple(warnings), files)

    def never_runs(self, surface: str, kind: str) -> tuple[str, str] | None:
        """(why, what the owner does) when no session would ever take a `kind` item on `surface` as things
        stand: the owner's cap for it is 0, the site allows none, or the lane it acts through is gone
        (QA-2 #10). None when one could."""
        from agentos.engage.caps import Kind, cap_row

        k = _kind(kind)
        if isinstance(k, Kind):
            cap = self.store.caps(surface).get(k)
            if cap is not None and cap <= 0:
                return (f"the {kind} cap on {surface} is 0", f"To send it, the owner raises the {kind} cap first.")
            pv = self.store.active(surface)
            row = cap_row((pv.definition.site or "") if pv is not None else "", k)
            if row is not None and row.normal <= 0:
                return (f"a {kind} is never sent on {pv.definition.site if pv else surface}", "")
        if self.lane_gone is not None:
            try:
                gone = self.lane_gone(surface)
            except Exception:
                log.warning("engage: could not check the lane of %s", surface, exc_info=True)
                gone = None
            if gone:
                return (gone, "Create the lane again, or point the account at another lane, first.")
        return None

    def end_dead(self, now: datetime | None = None) -> list[str]:
        """Queued items that can no longer go out (never_runs: a cap dropped to 0, a lane went away)
        end now, withdrawn by the node, each with a notice, rather than waiting silently until they
        expire."""
        now = now or self.now()
        ended: list[str] = []
        verdicts: dict[tuple[str, str], tuple[str, str] | None] = {}
        for item in self.store.items(None, status="queued", limit=10_000):
            key = (item["surface"], item["kind"])
            if key not in verdicts:
                verdicts[key] = self.never_runs(*key)
            dead = verdicts[key]
            if dead is None:
                continue
            why = dead[0]
            if self.store.withdraw_item(item["id"], now=now, by="agentos",
                                        history={"id": item["id"], "kind": item["kind"], "by": {"id": "agentos"},
                                                 "reason": f"{why}, so it could never go out"}):
                ended.append(item["id"])
                self.store.notice("item_ended", f"Item {item['id']} ({item['kind']}) was taken back: {why}, so it "
                                  f"could never go out. {dead[1]}".strip(), surface=item["surface"])
        return ended

    def _can_run(self, site: str, kind: str) -> None:
        """Refuse an item no session could ever send: the operation has no installed flow, no rule
        that checks it landed, or no canary yet. The executor asks the same registry before it acts."""
        from agentos.engage.operations import NO_FLOW, NO_RULE, NO_SPEC, OP_UNVERIFIED, RULE_UNVERIFIED

        spec = self.registry.get(site, kind)
        problem = self.registry.problem(spec)
        if problem is None:
            return
        why = {
            NO_SPEC: f"{site} has no {kind} operation",
            NO_FLOW: f"no {site} connector with a {kind} flow is installed on this node",
            NO_RULE: f"there is no rule that checks a {site} {kind} landed",
            RULE_UNVERIFIED: f"the rule that checks a {site} {kind} landed is not verified yet",
            OP_UNVERIFIED: (f"the {site} {kind} operation changed since its canary"
                            if self.registry.reason(spec) else f"the {site} {kind} operation has not passed its canary"),
        }.get(problem, f"the {site} {kind} operation cannot run ({problem.replace('_', ' ')})")
        fix = {NO_FLOW: "install the connector", OP_UNVERIFIED: "run agentos connector verify"}.get(problem)
        raise InvalidRequest(f"{why}, so this item could never go out and nothing was queued"
                             + (f". To add it, {fix} first" if fix else ""))

    def _check_file(self, surface: str, file_id: str) -> dict[str, Any]:
        from agentos.runner.connectors.surfaces import FILE_ID_RE

        if not FILE_ID_RE.match(file_id):
            raise InvalidRequest(f"attachment {file_id[:40]!r} is not a file id (ids look like in_3f9a0c1b2d4e; "
                                 "put the file on the lane with files_put first)")
        if self.files is None:
            raise InvalidRequest("lane files are not available here, so an item cannot carry attachments")
        try:
            f = self.files(surface, file_id)
        except NotFound:
            raise InvalidRequest(f"no file {file_id} on {surface}'s lane; put it there with files_put first") from None
        status = str(getattr(getattr(f, "status", None), "value", getattr(f, "status", "complete")))
        if status != "complete":
            raise InvalidRequest(f"{file_id} is {status}; only complete files can be attached")
        if getattr(f, "kind", None) not in ATTACHMENT_KINDS:
            raise InvalidRequest(f"{file_id} is {getattr(f, 'mime', None) or 'not a picture or a video'}; "
                                 f"attach images or videos only")
        sha = getattr(f, "sha256", None)
        if not sha:
            raise InvalidRequest(f"{file_id} has no sha256 yet, so the owner could not approve exactly this file; "
                                 "put it on the lane again with files_put")
        return {"id": file_id, "name": str(getattr(f, "name", None) or file_id), "mime": getattr(f, "mime", None),
                "size": int(getattr(f, "size", 0) or 0), "sha256": str(sha)}

    # ---------------------------------------------------------------- queueing
    def submit(self, plan: ItemPlan, *, in_person: bool = False, requested_by: Any = None,
               approval_id: str | None = None) -> dict[str, Any]:
        """Queue a checked item, as submitted by the owner. `in_person`: the owner is at the node (a
        signed-in panel session, the CLI) and the item is queued as is. Anyone else (an agent, or
        a bearer token, the owner's included) comes with `approval_id`: the owner's approval of this
        exact item, already used by the call that asked (state consumed), never used for another
        item. `requested_by` is the principal that asked."""
        now = self.now()
        requester = None if in_person else _who(requested_by)
        pv = self.store.active(plan.surface)   # the connector may have changed since the item was checked
        self._can_run(pv.definition.site or "" if pv is not None else "", plan.kind)
        if not in_person:
            self._check_approval(plan, approval_id)
        expires = plan.expires_at or (now + DEFAULT_TTL).replace(microsecond=0)
        item_id = "it_" + secrets.token_hex(8)
        item = self.store.add_item({
            "id": item_id, "surface": plan.surface, "kind": plan.kind, "text": plan.text,
            "title": plan.title, "targets": list(plan.targets), "attachments": list(plan.attachments),
            "files": [dict(f) for f in plan.files], "expires_at": iso(expires), "submitted_by": OWNER_ID,
            "submitted_at": iso(now), "requested_by": requester, "approval_id": None if in_person else approval_id},
            history={"kind": plan.kind, "expires_at": iso(expires), "targets": list(plan.targets),
                     "attachments": [{k: f[k] for k in ("id", "name", "sha256")} for f in plan.files],
                     "requested_by": requester, "approval_id": None if in_person else approval_id})
        if item is None:
            raise OwnerOnly(f"approval {approval_id} already queued an item; an approval queues one item, once")
        return self.public(item) | {"warnings": list(plan.warnings)}

    def _check_approval(self, plan: ItemPlan, approval_id: str | None) -> None:
        """The approval is the owner's, approved and then used (consumed) by the call that asked,
        for this tool and exactly this item's canonical content."""
        if not approval_id:
            raise OwnerOnly("an item from anyone but the owner in person is queued only after the owner approves it")
        store = self.approvals() if self.approvals is not None else None
        if store is None:
            raise OwnerOnly("approvals are not available here, so only the owner in person can add an item")
        from agentos.policy.approvals import content_digest

        try:
            ap = store.get(approval_id)
        except NotFound:
            raise OwnerOnly(f"there is no approval {approval_id}") from None
        problem = None
        if getattr(ap, "kind", None) != "action" or getattr(ap, "tool", None) != SUBMIT_TOOL:
            problem = "is not an approval of an item"
        elif getattr(ap, "state", None) != "consumed":
            problem = f"is {getattr(ap, 'state', 'unknown')}, not approved and used for this call"
        elif getattr(ap, "content_digest", None) != content_digest(plan.canonical):
            problem = "is for different content"
        if problem:
            raise OwnerOnly(f"approval {approval_id} {problem}, so nothing was queued")

    # ---------------------------------------------------------------- reading
    @staticmethod
    def public(item: dict[str, Any]) -> dict[str, Any]:
        keys = ("id", "surface", "kind", "status", "text", "title", "targets", "attachments", "files", "expires_at",
                "submitted_by", "submitted_at", "requested_by", "used_run_id", "settled_at")
        out = {k: item.get(k) for k in keys}
        if isinstance(out["requested_by"], dict):  # the asker's grant path stays on the node
            out["requested_by"] = {k: v for k, v in out["requested_by"].items() if k != "grant_path"}
        return out

    def get(self, item_id: str) -> dict[str, Any]:
        if not isinstance(item_id, str) or not ITEM_ID_RE.match(item_id):
            raise InvalidRequest(f"not an item id: {str(item_id)[:40]!r} (ids look like it_0123456789abcdef)")
        found = self.store.item(item_id)
        if found is None:
            raise NotFound(f"no item {item_id}")
        return found

    def list(self, surface: str | None = None, *, status: str | None = None, limit: int = 200,
             can_see: Callable[[str], bool] | None = None) -> list[dict[str, Any]]:
        """Oldest first. Without a surface and with `can_see`, only the surfaces it allows, filtered
        in the query (so the limit counts only items the caller may see)."""
        from agentos.engage.store import ITEM_STATUSES

        if status is not None and status not in ITEM_STATUSES:
            raise InvalidRequest(f"status is one of {', '.join(ITEM_STATUSES)}")
        surfaces = None
        if surface is None and can_see is not None:
            surfaces = [s for s in self.store.item_surfaces() if can_see(s)]
        return [self.public(i) for i in self.store.items(surface, surfaces=surfaces, status=status, limit=limit)]

    def counts(self, surface: str) -> dict[str, int]:
        return self.store.item_counts(surface)

    # ---------------------------------------------------------------- withdrawing and sweeping
    def withdraw(self, item_id: str, *, by: Any, as_owner: bool | None = None) -> dict[str, Any]:
        """The owner (`as_owner`, by default whether `by` is the owner), or the principal that asked
        for it, takes a queued item back."""
        item = self.get(item_id)
        owner = is_owner(by) if as_owner is None else as_owner
        if not owner and not _same(by, item.get("requested_by")):
            raise OwnerOnly("only the owner, or whoever asked for this item, can withdraw it")
        who = _who(by) or {}
        if not self.store.withdraw_item(item_id, now=self.now(), by=str(who.get("id") or OWNER_ID),
                                        history={"id": item_id, "kind": item["kind"], "by": who}):
            current = self.store.item(item_id) or item
            raise InvalidRequest(f"{item_id} is {current['status']}, so it cannot be withdrawn"
                                 + (" now (a session has it; try again after the session)"
                                    if current["status"] in ("reserved", "awaiting") else ""))
        return self.public(self.get(item_id))

    def sweep(self, now: datetime | None = None) -> dict[str, list[str]]:
        """Items whose run waited for an approval card follow what came of it (`reconcile`);
        reservations whose window is over without a settle (it ended, or it is past its planned
        end and WINDOW_GRACE, as a cancelled or crashed window is) go back to the queue
        (`item_released`), or expire when past their expiry; then queued items past their expiry
        expire (`item_expired`). One history entry each, written with the change."""
        now = now or self.now()
        settled = self.reconcile(now)
        out = self.store.release_items(now=now, stale_before=now - ABANDONED_AFTER, grace=WINDOW_GRACE)
        expired = [i["id"] for i in out if i["released_to"] == "expired"]
        expired += [i["id"] for i in self.store.expire_items(now=now)]
        settled["failed"] = settled["failed"] + [i["id"] for i in out if i["released_to"] == "failed"]
        ended = self.end_dead(now)
        return {"released": [i["id"] for i in out if i["released_to"] == "queued"], "expired": expired,
                "ended": ended, **settled}

    def reconcile(self, now: datetime | None = None) -> dict[str, list[str]]:
        """Items whose run waited for an approval card: `used` once the run went out, back to
        `queued` when the owner denied the card or it expired (nothing was sent), `failed` when
        the run failed in a way that may have acted (left for the owner)."""
        now = now or self.now()
        out: dict[str, list[str]] = {"used": [], "requeued": [], "failed": []}
        for i in self.store.awaiting_items():
            op, why = i["op_status"], i["op_reason"]
            payload = {"id": i["id"], "kind": i["kind"], "run_id": i["used_run_id"]}
            if op in ("verified", "unverified"):
                if self.store.settle_item(i["id"], "used", now=now, history=("item_used", payload)):
                    out["used"].append(i["id"])
            elif op == "failed" and why in NOT_SENT:
                if self.store.settle_item(i["id"], "queued", now=now,
                                          history=("item_requeued", payload | {"reason": why})):
                    out["requeued"].append(i["id"])
            elif op == "failed" and self.store.settle_item(
                    i["id"], "failed", now=now, history=("item_failed", payload | {"reason": why or "failed"})):
                out["failed"].append(i["id"])
        return out


def _kind(kind: str) -> Any:
    from agentos.engage.caps import Kind

    try:
        return Kind(kind)
    except ValueError:
        return kind


class ItemsContent:
    """The executor's ContentPort over the items inbox."""
    origin = ContentOrigin.ITEM

    def __init__(self, store: EngageStore, *, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def take(self, surface: str, kind: str, n: int) -> list[Content]:
        """Reserve up to `n` items for the window in CONTENT_SESSION (the executor sets it)."""
        return [Content(id=i["id"], origin=ContentOrigin.ITEM, text=i["text"], title=i["title"],
                        targets=tuple(i["targets"]), attachments=tuple(i["attachments"]), approved=True)
                for i in self.store.reserve_items(surface, str(kind), n, now=self.now(),
                                                  session_id=CONTENT_SESSION.get())]

    def settle(self, surface: str, content: Content, *, used: bool, run_id: str | None = None) -> None:
        """used=False hands the item back. used=True follows the operation of `run_id` (the
        executor records it first): waiting for an approval card, the item waits too (`awaiting`,
        until reconcile); failed, or still `attempted` because the window stopped mid-run, it may
        have acted, so it is `failed` and left for the owner; otherwise it is `used`."""
        now = self.now()
        if not used:
            to, history = "queued", None
        else:
            op = self.store.op_by_run(run_id) if run_id else self.store.op_by_content(ContentOrigin.ITEM.value,
                                                                                      content.id)
            status = (op or {}).get("status")
            payload = {"id": content.id, "run_id": run_id}
            if status == "awaiting_approval":
                to, history = "awaiting", ("item_awaiting", payload)
            elif status in ("failed", "attempted"):
                to, history = "failed", ("item_failed", payload | {"reason": (op or {}).get("reason") or "failed"})
            else:
                to, history = "used", ("item_used", payload)
        if not self.store.settle_item(content.id, to, now=now, run_id=run_id, history=history):
            log.warning("engage: item %s could not become %s when its window settled it", content.id, to)


class ChainedContent:
    """ContentPorts asked in order (items first, then the drafter): `take` fills `n` from the first
    port that has content, then the next; `settle` goes back to the port that produced the content.

        ChainedContent([ItemsContent(store), Drafts(...)])

    A port may set `origin` (a ContentOrigin): after a restart, when the chain no longer remembers
    who produced a content, settle goes to the first port of that origin."""

    def __init__(self, ports: Iterable[ContentPort]):
        self.ports = list(ports)
        self._by: dict[tuple[str, str], ContentPort] = {}

    def take(self, surface: str, kind: str, n: int) -> list[Content]:
        out: list[Content] = []
        for port in self.ports:
            if len(out) >= n:
                break
            for c in port.take(surface, kind, n - len(out)):
                self._by[(str(c.origin), c.id)] = port
                out.append(c)
        return out

    async def atake(self, surface: str, kind: str, n: int) -> list[Content]:
        """`take`, awaiting the ports that call out (`atake`, the drafter). When such a port is
        unavailable (ContentUnavailable) and nothing came from the ports before it, it raises."""
        out: list[Content] = []
        for port in self.ports:
            if len(out) >= n:
                break
            atake = getattr(port, "atake", None)
            try:
                got = await atake(surface, kind, n - len(out)) if atake is not None else \
                    port.take(surface, kind, n - len(out))
            except ContentUnavailable:
                if out:
                    return out
                raise
            for c in got:
                self._by[(str(c.origin), c.id)] = port
                out.append(c)
        return out

    def settle(self, surface: str, content: Content, *, used: bool, run_id: str | None = None) -> None:
        port = self._by.pop((str(content.origin), content.id), None)
        if port is None:
            port = next((p for p in self.ports if getattr(p, "origin", None) == content.origin), None)
        if port is None:
            log.warning("engage: no content port settles %s %s", content.origin, content.id)
            return
        port.settle(surface, content, used=used, run_id=run_id)
