"""Production targets: what an engagement operation acts on, read from the site by connector
read operations (docs/design/engagement-engine.md 5.3).

    targets = ConnectorTargets(store, operations, own=lambda site: {"mirakestreldemo"})
    targets.next(surface, kind, program)            # a target string, or None (the pool is empty)
    await targets.refill(surface, kind, program, parent="claim:engage:...")   # read more

The executor asks `next` first and, when it has nothing, awaits `refill` once inside the
window's lane claim, then asks `next` again (agentos.engage.ports.RefillingTargets). A refill
is one read operation through the OperationPort (the runner in production, a fake in tests),
like any connector run, under the owner's grant:

    site      kind              source (program targets)   read operation              target
    reddit    comment           communities                reddit.browse_subreddit     the post's permalink
    reddit    join              communities                none (the names are given)  the subreddit name
    linkedin  like, comment     own_feed                   linkedin.feed               /feed/update/<urn>/
    linkedin  follow            own_feed                   linkedin.feed               the author's /in/ or /company/ page

Anything else gets None: posts take their target from the content (the owner's items), and a
source with no read operation yet (hashtags, own_engagers, ...) adds nothing.

The pool is small, per surface and kind, in memory, and candidates expire after 3 days. What
`next` never returns, whatever a page said:

- the owner's own accounts and their posts: an author matching any surface of the site (its
  user id, id or name, case-insensitive; on Reddit bind the surface with `--name <username>`,
  on LinkedIn with `--id <public profile name>`);
- anything this surface already engaged with for the same kind: a target on any of its
  operations in engage.db that reached a connector (any status but planned or skipped), and
  anything this port already handed out;
- a post outside the program's communities (Reddit), and more than one post per author in one
  read (no streaks on one person): authors compare by their profile (LinkedIn: kind and public
  name from the link, query and fragment dropped, case folded) or username (Reddit: without u/,
  case folded);
- a LinkedIn update with no author link (a promoted or group item): nobody to judge.

Program filters that need page data the read operations do not return (language, account age,
follower ratio, topics) are not applied here: the reads return ids and links only, because page
text is untrusted. The drafter (brief B3) judges the words.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agentos.engage.caps import Kind
from agentos.engage.models import Program, Source
from agentos.engage.operations import LINKEDIN_PROFILE_RE, REDDIT_POST_RE, SUBREDDIT_RE
from agentos.engage.ports import OperationPort, OpResult, OpStatus

log = logging.getLogger("agentos.engage.targets")

POOL_MAX = 20                         # candidates kept per surface and kind
EXPIRE = timedelta(days=3)            # a candidate older than this is stale (section 5.3)
ENGAGED_STATUSES = ("planned", "skipped")   # op statuses that never reached a connector
LINKEDIN_URN_RE = re.compile(r"^urn:li:activity:[0-9]{10,25}$")


@dataclass(frozen=True)
class ReadOp:
    """A connector read operation, shaped like an OpSpec for the OperationPort."""
    site: str
    kind: str
    flow_id: str
    rule_id: str = ""
    needs_text: bool = False
    needs_target: bool = False


BROWSE_SUBREDDIT = ReadOp("reddit", "read", "connector.reddit.browse_subreddit")
LINKEDIN_FEED = ReadOp("linkedin", "read", "connector.linkedin.feed")


@dataclass
class Candidate:
    target: str
    author: str | None
    seen: datetime


@dataclass
class _Pool:
    items: list[Candidate] = field(default_factory=list)
    given: set[str] = field(default_factory=set)
    turn: int = 0                     # which community the next refill reads (round robin)


def _norm(name: str | None) -> str:
    return (name or "").strip().casefold()


def reddit_post(url: str) -> tuple[str, str] | None:
    """(subreddit, the canonical permalink) of a Reddit post URL, or None."""
    m = REDDIT_POST_RE.match(url or "")
    if m is None:
        return None
    return m.group(1), f"https://www.reddit.com/r/{m.group(1)}/comments/{m.group(2)}/"


def linkedin_profile(url: str | None) -> tuple[str, str] | None:
    """(the public name, the canonical page URL) of a LinkedIn member or company link, or None."""
    m = LINKEDIN_PROFILE_RE.match(url or "")
    if m is None:
        return None
    return m.group(2), f"https://www.linkedin.com/{m.group(1)}/{m.group(2)}/"


def author_key(site: str, author: str | None) -> str | None:
    """Who wrote it, comparable across reads: on LinkedIn the profile's kind and public name from
    the link (query and fragment dropped, case folded: ?miniProfileUrn=... is the same person), on
    Reddit the username without u/ or /user/, case folded. None when there is none to compare."""
    if not author:
        return None
    if site == "linkedin":
        prof = LINKEDIN_PROFILE_RE.match(author.strip())
        return f"{prof.group(1)}/{prof.group(2)}".casefold() if prof else None
    name = author.strip()
    for prefix in ("/user/", "/u/", "user/", "u/"):
        if name.casefold().startswith(prefix):
            name = name[len(prefix):]
            break
    return _norm(name.strip("/")) or None


def communities(program: Program) -> list[str]:
    """The program's community names, in the order written, without r/ and repeats."""
    out: list[str] = []
    for t in program.steady.targets:
        if t.source is Source.COMMUNITIES:
            for v in t.values:
                m = SUBREDDIT_RE.match(v.strip())
                if m and _norm(m.group(1)) not in {_norm(x) for x in out}:
                    out.append(m.group(1))
    return out


def has_source(program: Program, source: Source) -> bool:
    return any(t.source is source for t in program.steady.targets)


class ConnectorTargets:
    def __init__(self, store: Any, operations: OperationPort, *, own: Callable[[str], Iterable[str]] | None = None,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.operations = operations
        self.own = own or (lambda site: ())
        self.clock = clock
        self._pools: dict[tuple[str, str], _Pool] = {}

    # ---------------------------------------------------------------- reading
    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def _pool(self, surface: str, kind: str) -> _Pool:
        return self._pools.setdefault((surface, str(kind)), _Pool())

    def _engaged(self, surface: str, kind: str) -> set[str]:
        """Targets this surface already acted on (or tried to) for `kind`."""
        return {str(o["target"]) for o in self.store.ops(surface, kind=str(kind))
                if o.get("target") and o.get("status") not in ENGAGED_STATUSES}

    def _own(self, site: str) -> set[str]:
        return {_norm(x) for x in self.own(site) if x}

    def _is_own(self, site: str, author: str | None) -> bool:
        if not author:
            return False
        own = self._own(site)
        prof = linkedin_profile(author)
        key = author_key(site, author)
        return (_norm(author) in own or (prof is not None and _norm(prof[0]) in own)
                or (site != "linkedin" and key is not None and key in own))

    def next(self, surface: str, kind: str, program: Program) -> str | None:
        site, kind = program.site or "", str(kind)
        if site == "reddit" and kind == Kind.JOIN:
            return self._next_join(surface, program)
        pool = self._pool(surface, kind)
        engaged = self._engaged(surface, kind)
        cutoff = self.now() - EXPIRE
        pool.items = [c for c in pool.items if c.seen >= cutoff]
        while pool.items:
            c = pool.items.pop(0)
            if c.target in engaged or c.target in pool.given or self._is_own(site, c.author):
                continue
            pool.given.add(c.target)
            return c.target
        return None

    def _next_join(self, surface: str, program: Program) -> str | None:
        """A community of the program the surface has not joined (or tried to) yet."""
        pool = self._pool(surface, Kind.JOIN)
        done = {_norm(t.removeprefix("r/")) for t in self._engaged(surface, Kind.JOIN)} | {_norm(t) for t in pool.given}
        for name in communities(program):
            if _norm(name) not in done:
                pool.given.add(name)
                return name
        return None

    # ---------------------------------------------------------------- refilling
    def _read_for(self, site: str, kind: str, program: Program, pool: _Pool) -> tuple[ReadOp, dict[str, Any], str] | None:
        """The read to run for `kind` now: (operation, params, what it reads), or None."""
        if site == "reddit" and kind == Kind.COMMENT:
            names = communities(program)
            if not names:
                return None
            name = names[pool.turn % len(names)]
            pool.turn += 1
            return BROWSE_SUBREDDIT, {"subreddit": name}, name
        if site == "linkedin" and kind in (Kind.LIKE, Kind.COMMENT, Kind.FOLLOW):
            return (LINKEDIN_FEED, {}, "feed") if has_source(program, Source.OWN_FEED) else None
        return None

    def _candidates(self, site: str, kind: str, program: Program, posts: list[Any]) -> list[Candidate]:
        now = self.now()
        allowed = {_norm(n) for n in communities(program)}
        out: list[Candidate] = []
        authors: set[str] = set()
        for p in posts:
            if not isinstance(p, dict):
                continue
            author = p.get("author") if site == "reddit" else p.get("author_url")
            author = author if isinstance(author, str) else None
            if site == "reddit":
                found = reddit_post(str(p.get("url") or ""))
                if found is None or _norm(found[0]) not in allowed:
                    continue
                target = found[1]
            else:
                urn = str(p.get("urn") or "")
                prof = linkedin_profile(author)
                if not LINKEDIN_URN_RE.match(urn) or prof is None:
                    continue  # no author link (a promoted item, a group's): nobody to judge, so not a target
                target = prof[1] if kind == Kind.FOLLOW else f"https://www.linkedin.com/feed/update/{urn}/"
            who = author_key(site, author) or target
            if who in authors:
                continue
            authors.add(who)
            out.append(Candidate(target, author, now))
        return out

    async def refill(self, surface: str, kind: str, program: Program, *,
                     parent: str | None = None) -> OpResult | None:
        """Run one read operation for `kind` and add what it found to the pool. Returns the read's
        result (its signal matters: a captcha while reading stops the window), or None when
        there is nothing to read for `kind`."""
        site, kind = program.site or "", str(kind)
        pool = self._pool(surface, kind)
        read = self._read_for(site, kind, program, pool)
        if read is None:
            return None
        op, params, what = read
        bucket = self.now().strftime("%Y-%m-%dT%H")      # one read of a thing per hour at most, per window
        # the window's claim parent (a per-window secret) is in the key: the adapter adopts a run
        # under a key only when its parent is this window's, so a read another window made in the
        # same hour would come back foreign_run instead of reading again
        window = hashlib.sha256(parent.encode()).hexdigest()[:12] if parent else "none"
        key = f"engage-read:{surface}:{op.flow_id}:{what}:{bucket}:{window}"
        res = await self.operations.run(surface, op, params, idempotency_key=key, ask=False, parent=parent)
        if res.status not in (OpStatus.VERIFIED, OpStatus.UNVERIFIED):
            log.info("engage: %s read %s for %s found nothing (%s)", surface, op.flow_id, kind,
                     res.signal or res.error or res.status.value)
            return res
        posts = res.output.get("posts") if isinstance(res.output, dict) else None
        known = {c.target for c in pool.items} | pool.given
        added = 0
        for c in self._candidates(site, kind, program, posts if isinstance(posts, list) else []):
            if c.target in known or len(pool.items) >= POOL_MAX:
                continue
            pool.items.append(c)
            known.add(c.target)
            added += 1
        log.info("engage: %s read %s for %s: %d new candidates", surface, op.flow_id, kind, added)
        return res


def surface_identities(runtime: Any) -> Callable[[str], set[str]]:
    """site -> every id and name the owner's surfaces of that site are bound with."""
    def own(site: str) -> set[str]:
        surfaces = runtime.get("surfaces") if runtime is not None else None
        if surfaces is None:
            return set()
        out: set[str] = set()
        for s in surfaces.list(site=site):
            a = s.acts_as
            out |= {x for x in (a.user_id, a.id, a.name) if x}
        return out
    return own
