"""The git gateway: git's smart-HTTP protocol served to lanes and brains, with the node in between.

A lane's git is configured (agentos.git.service) so that `https://github.com/...`,
`git@github.com:...` and `ssh://git@github.com/...` for each declared host are rewritten to
`http://127.0.0.1:<port>/github.com/...`, and every request carries the caller's gateway token in
`X-Agentos-Git`. The port is a listener the node opens inside the lane's own network namespace
(isolated lanes), or on the node's loopback (brains, and lanes on the node's network). Nothing the
lane sends reaches the real remote: the gateway answers from a mirror and talks to the remote
itself.

For each request, in order:

1. **Who.** The token names a caller (a lane and the grant that started the command, or a brain
   task and its grant). An unknown or expired token, or a lane's token on another lane's listener,
   is refused. The token is no credential for the remote: it only identifies the caller here.
2. **Declared.** The remote must be declared for the caller's lane or workspace
   (agentos.git.remotes); a push needs a declaration that allows pushes.
3. **Authorized.** Under the caller's live grant, through the policy: a fetch is `read`; a push is
   `commit` on the declaration's account (the lane), or `irreversible` when it force-pushes (the old
   commit is not an ancestor of the new one, checked on the mirror with the pushed objects), deletes
   a branch or a tag, moves a tag, or updates a protected branch (the default branch unless the
   declaration allows it, and its `protect` globs). So "Ask me first", approvals, the taint
   downgrade (after untrusted content a push waits for a person) and freezes all apply.
   An agent environment's VM (a `bound` token) is the exception: its push never waits for a
   person, on any branch, the default branch included, before or after untrusted content. A push
   from it that would force-push or delete a protected branch, move or delete a tag, or force-push
   or delete any branch the environment did not create itself (one it pushed first, which was not
   on the remote before) is refused instead (`push_refused`, with the fix: push a new branch and
   open a pull request). The environments service keeps the branches each one created, on its
   record, so a restart of the node keeps them.
4. **The remote.** Fetch: the mirror is brought up to date with the credential, then served with
   `git upload-pack`. Push: the pack goes into the mirror with `git receive-pack`, the refs are
   classified and authorized, and only then the node pushes to the real remote, each ref with a
   lease on the value the lane saw. A refused, waiting or failed push puts the mirror back.

What the lane sees of a refusal is a sentence that says what to do (git prints it as
`remote error:` or `! [remote rejected] ... (<reason>)`), scrubbed of vault values; never key or
token material, which the gateway never has in a message in the first place. Every fetch and push
is traced with the remote, the refs, the caller and the grant.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import logging
import secrets
import time
import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from agentos.core.errors import AgentosError, ApprovalPending, InvalidRequest
from agentos.core.models import Effect
from agentos.git import keys as keymod
from agentos.git import login
from agentos.git.remotes import RemoteRule, Remotes, scope_workspace
from agentos.git.upstream import Mirror, Target, UpstreamError, credential_env, target_for
from agentos.git.wire import (
    Command,
    Push,
    Remote,
    build_report,
    demux,
    error_advertisement,
    parse_gateway_path,
    parse_push,
    parse_report,
    pkt,
)

log = logging.getLogger("agentos.git")

TOKEN_HEADER = "x-agentos-git"
TOKEN_PREFIX = "agtg_"  # redacted in traces and transcripts (policy.redact, brains.transcript)
MAX_HEADERS = 64 * 1024
MAX_FETCH_BODY = 64 * 1024 * 1024       # an upload-pack request: wants and haves
MAX_PUSH_BODY = 1024 * 1024 * 1024      # a push's pack
READ_TIMEOUT = 300.0
MAX_ACTIVE = 4                          # requests at once per listener: a lane cannot pile up push bodies


@dataclass(frozen=True)
class Caller:
    """Who a gateway token stands for. `lane`: the env lane whose command holds it (its listener
    must be the one the request arrives on); `lanes`: the lanes a brain task was given. `repos`:
    the only repositories (slugs) it reaches, when set. `bound`: good only on the listener of its
    own lane (an environment's VM, agentos.git.service.vm_token), never on the node's own."""
    grant_id: str
    workspace: str
    lane: str | None
    lanes: tuple[str, ...]
    label: str
    expires: float | None = None
    task: str | None = None
    repos: tuple[str, ...] = ()
    bound: bool = False

    def scopes(self) -> list[str]:
        """Where a declaration may be, most specific first: the caller's lanes, then its workspace."""
        own = [self.lane] if self.lane else list(self.lanes)
        return [*own, self.workspace]


class Refused(Exception):
    """A request the gateway answers with a sentence for the lane (and a trace)."""

    def __init__(self, message: str, *, code: str = "denied"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class Request:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes | None = None
    stream: AsyncIterator[bytes] | None = None


def oid(sha: str) -> str:
    """A commit id as `sha1:<hex>` (or `sha256:`): the form the trace and approval redaction keep
    whole, where a bare 40-hex string reads as a random token."""
    return f"{'sha256' if len(sha) == 64 else 'sha1'}:{sha}"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class GitGateway:
    """The gateway's state (tokens, mirrors) and request handling. Listeners call `handle_client`."""

    def __init__(self, *, home: Path, remotes: Remotes, runtime: Any, allow_file: bool = False,
                 clock: Callable[[], float] = time.time, agent_dir: Path | None = None):
        self.home = Path(home)
        self.root = self.home / "git"
        self.remotes = remotes
        self.runtime = runtime
        self.allow_file = allow_file
        self.clock = clock
        self.agent_dir = agent_dir or (self.root / "run")
        self._callers: dict[str, Caller] = {}  # sha256 of a token -> who it stands for
        self._tokens: dict[Caller, str] = {}   # the token minted for a caller, reused while it lives
        self._mirrors: dict[str, Mirror] = {}

    # ------------------------------------------------------------------ caller tokens
    def mint(self, caller: Caller) -> str:
        """A new token for `caller`. Only node services call this (the env manager, brains)."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self._callers[token_digest(token)] = caller
        self._tokens[caller] = token
        return token

    def token_for(self, caller: Caller) -> str:
        """The live token already minted for this caller (one per lane and grant), or a new one."""
        token = self._tokens.get(caller)
        if token is not None and token_digest(token) in self._callers:
            return token
        return self.mint(caller)

    def revoke(self, token: str) -> None:
        caller = self._callers.pop(token_digest(token), None)
        if caller is not None and self._tokens.get(caller) == token:
            self._tokens.pop(caller, None)

    def revoke_where(self, pred: Callable[[Caller], bool]) -> int:
        """Revoke every token whose caller matches (a lane going cold, a brain task ending)."""
        gone = [c for c in self._tokens if pred(c)]
        for c in gone:
            self.revoke(self._tokens[c])
        stray = [d for d, c in self._callers.items() if pred(c)]
        for d in stray:
            self._callers.pop(d, None)
        return len(gone) + len(stray)

    def caller(self, token: str | None, *, listener_lane: str | None) -> Caller:
        if not token or not token.startswith(TOKEN_PREFIX):
            raise Refused("this git request has no agentos gateway token; run git from a command the node "
                          "started (lane_exec, lane_job_start, agentos run, a brain task), not with a copied "
                          "config", code="unauthenticated")
        c = self._callers.get(token_digest(token))
        if c is None or (c.expires is not None and c.expires < self.clock()):
            raise Refused("this git gateway token is no longer valid (its command or task ended); run the "
                          "command again", code="unauthenticated")
        if (listener_lane is not None or c.bound) and c.lane != listener_lane:
            raise Refused("this token belongs to another lane", code="unauthenticated")
        if c.bound and not self._environment_open(c.lane):
            self.revoke_where(lambda x: x.lane == c.lane)
            raise Refused("this git gateway token is no longer valid (its environment closed)", code="unauthenticated")
        return c

    def _environment_open(self, lane_id: str | None) -> bool:
        """Is `lane_id` the terminal of an open environment? A VM's token dies with it, even before
        the environment's own teardown reaches vm_down."""
        envs = self.runtime.get("environments") if self.runtime is not None else None
        holds = getattr(envs, "holds_terminal", None)
        if not callable(holds) or lane_id is None:
            return False
        try:
            return bool(holds(lane_id))
        except Exception:  # noqa: BLE001 - an unreadable record is no open environment
            return False

    # ------------------------------------------------------------------ mirrors and credentials
    def mirror(self, remote: Remote) -> Mirror:
        m = self._mirrors.get(remote.slug)
        if m is None:
            m = Mirror(self.root / "mirrors" / remote.host / f"{remote.repo}.git", hooks_dir=self.root / "hooks",
                       allow_file=self.allow_file)
            self._mirrors[remote.slug] = m
        return m

    async def credentials(self) -> list[keymod.Credential]:
        vault = self.runtime.get("vault") if self.runtime is not None else None
        if vault is None:
            return []
        try:
            metas = await vault.list_secrets()
        except AgentosError:
            return []
        return [c for c in (keymod.from_meta(m) for m in metas) if c is not None]

    async def _read_secret(self, ref: str, purpose: str) -> str | None:
        vault = self.runtime.get("vault") if self.runtime is not None else None
        if vault is None:
            raise UpstreamError("this node has no vault, so it holds no git credential")
        try:
            return await vault.get_secret(ref, purpose=purpose)
        except AgentosError as e:
            raise UpstreamError(f"the vault could not be read ({e.code}); the owner runs `agentos vault status` "
                                "on the node") from None

    def _scrub(self, text: str) -> str:
        from agentos.policy.scrub import runtime_scrub

        return runtime_scrub(self.runtime)(text)

    # ------------------------------------------------------------------ policy
    def _context(self, caller: Caller, tool: str, lane: str | None) -> Any:
        policy = self.runtime.get("policy") if self.runtime is not None else None
        if policy is None:
            raise Refused("this node has no policy service, so the git gateway refuses every request")
        try:
            return policy.context_for_grant(caller.grant_id, tool=tool, lane_id=lane)
        except AgentosError as e:
            raise Refused(f"the grant behind this command is no longer valid ({e.message})") from None

    @staticmethod
    def _account(caller: Caller, rule: RemoteRule) -> str | None:
        """The lane a push is a commit on: the caller's own lane; for a brain, the declaration's lane
        when the task was given it; else none (a commit on no account always asks)."""
        if caller.lane:
            return caller.lane
        if rule.on_lane and rule.scope in caller.lanes:
            return rule.scope
        return None

    @staticmethod
    def _check_scope(ctx: Any, caller: Caller, rule: RemoteRule) -> None:
        g = ctx.grant
        ws = scope_workspace(rule.scope)
        if rule.on_lane:
            ok = g.lane_allowed(rule.scope)
        else:
            ok = g.covers(None, ws) or any(g.lane_allowed(x) for x in ([caller.lane] if caller.lane else caller.lanes)
                                           if scope_workspace(x) == ws)
        if not ok:
            raise Refused(f"this command's grant does not reach {rule.scope}, where {rule.slug} is declared")

    # ------------------------------------------------------------------ requests
    async def handle(self, req: Request, *, listener_lane: str | None) -> Response:
        try:
            remote, service = parse_gateway_path(req.target)
        except InvalidRequest:
            return Response(404, "text/plain", b"agentos git gateway: not a repository path\n")
        query = parse_qs(urlsplit(req.target).query)
        if service == "info/refs":
            svc = (query.get("service") or [""])[0]
            if svc not in ("git-upload-pack", "git-receive-pack") or req.method != "GET":
                return Response(403, "text/plain", b"agentos git gateway: smart HTTP only\n")
            try:
                return await self._advertise(req, remote, svc, listener_lane)
            except Refused as e:
                return Response(200, f"application/x-{svc}-advertisement",
                                error_advertisement(svc, "agentos: " + self._scrub(e.message)))
        if req.method != "POST":
            return Response(405, "text/plain", b"agentos git gateway: POST only\n")
        if service == "git-upload-pack":
            try:
                return await self._upload(req, remote, listener_lane)
            except Refused as e:
                return Response(200, "application/x-git-upload-pack-result",
                                pkt(f"ERR agentos: {self._scrub(e.message)}\n"))
        return await self._receive(req, remote, listener_lane)

    def _resolve(self, req: Request, remote: Remote, listener_lane: str | None, *,
                 push: bool) -> tuple[Caller, RemoteRule]:
        caller = self.caller(req.headers.get(TOKEN_HEADER), listener_lane=listener_lane)
        where = caller.lane or caller.workspace
        if caller.repos and remote.slug not in caller.repos:
            self._trace_refusal(caller, remote, "other_repository", push)
            raise Refused(f"this token reaches only {', '.join(caller.repos)} (the repository its environment was "
                          f"opened with), not {remote.slug}", code="undeclared")
        rule = self.remotes.find(remote, caller.scopes())
        if rule is None:
            self._trace_refusal(caller, remote, "undeclared", push)
            raise Refused(f"{remote.slug} is not declared for {where}; the owner runs `agentos git remote allow "
                          f"{caller.workspace} {remote.slug}` on the node (or for one lane: "
                          f"`agentos git remote allow <lane> {remote.slug}`)", code="undeclared")
        if push and not rule.push:
            self._trace_refusal(caller, remote, "fetch_only", push)
            raise Refused(f"{remote.slug} is declared fetch-only for {rule.scope}; pushes need "
                          f"`agentos git remote allow {rule.scope} {remote.slug}` without --read-only",
                          code="undeclared")
        return caller, rule

    def _trace_refusal(self, caller: Caller, remote: Remote, reason: str, push: bool) -> None:
        """A known caller asked for a remote it may not use: on the record, under its grant."""
        try:
            ctx = self._context(caller, "git_push" if push else "git_fetch", None)
            ctx.trace("git_refused", status="denied", lane=caller.lane, remote=remote.slug, via=caller.label,
                      reason=reason)
        except Exception:  # noqa: BLE001 - the refusal stands; only its record is missing
            log.warning("git gateway: could not record a refusal of %s for %s", remote.slug, caller.label)

    async def _target(self, rule: RemoteRule, remote: Remote, *, push: bool) -> Target:
        cred = login.pick(await self.credentials(), remote, rule.scope)  # keys.pick, then env's GitHub login
        if cred is None and push:
            raise Refused(f"the vault holds no credential for {remote.slug}, so the node cannot push there; the "
                          f"owner runs `agentos git key add --host {remote.host} --repo {remote.repo}` on the node",
                          code="no_credential")
        if rule.url and rule.url.startswith(("file://", "/")) and not self.allow_file:
            raise Refused(f"{remote.slug} is declared with a local path, which this node does not allow")
        return target_for(rule, remote, cred)

    async def _authorize_read(self, caller: Caller, rule: RemoteRule, remote: Remote) -> Any:
        account = self._account(caller, rule)
        ctx = self._context(caller, "git_fetch", account)
        self._check_scope(ctx, caller, rule)
        try:
            await ctx.authorize(Effect.READ, lane_id=account, summary=f"Fetch {remote.slug} ({caller.label})")
        except AgentosError as e:
            raise Refused(f"fetching {remote.slug} was refused: {e.message}") from None
        return ctx

    async def _refresh(self, caller: Caller, mirror: Mirror, target: Target, op: str) -> None:
        await mirror.ensure()
        purpose = f"git {op} {target.remote.slug} for {caller.label}"
        try:
            async with credential_env(target, home=self.home, agent_dir=self.agent_dir,
                                      read_secret=self._read_secret, purpose=purpose) as auth:
                await mirror.fetch(target, auth)
        except UpstreamError as e:
            raise Refused(e.message, code="upstream") from None

    async def _advertise(self, req: Request, remote: Remote, svc: str, listener_lane: str | None) -> Response:
        push = svc == "git-receive-pack"
        caller, rule = self._resolve(req, remote, listener_lane, push=push)
        ctx = await self._authorize_read(caller, rule, remote)
        if push:
            try:
                ctx.precheck(Effect.COMMIT, lane_id=self._account(caller, rule))
            except AgentosError as e:
                raise Refused(f"this command's grant may not push to {remote.slug}: {e.message}") from None
        target = await self._target(rule, remote, push=push)
        mirror = self.mirror(remote)
        async with mirror.lock:
            await self._refresh(caller, mirror, target, "push" if push else "fetch")
            rc, out, _err = await mirror.git(svc[len("git-"):], "--stateless-rpc", "--advertise-refs", ".")
        if rc != 0:
            raise Refused(f"the node's mirror of {remote.slug} could not be read; try again")
        ctx.trace("git_fetch", lane=self._account(caller, rule), remote=remote.slug, via=caller.label,
                  vault_ref=target.credential.ref if target.credential else None,
                  purpose="push" if push else "fetch", result="ok")
        body = pkt(f"# service={svc}\n") + b"0000" + out
        return Response(200, f"application/x-{svc}-advertisement", body)

    async def _upload(self, req: Request, remote: Remote, listener_lane: str | None) -> Response:
        caller, rule = self._resolve(req, remote, listener_lane, push=False)
        await self._authorize_read(caller, rule, remote)
        mirror = self.mirror(remote)
        if not (mirror.path / "HEAD").is_file():
            raise Refused("fetch the refs first (the node has no mirror of this repository yet)")
        return Response(200, "application/x-git-upload-pack-result",
                        stream=_stream(mirror, ["upload-pack", "--stateless-rpc", "."], req.body))

    async def _receive(self, req: Request, remote: Remote, listener_lane: str | None) -> Response:
        ctype = "application/x-git-receive-pack-result"
        try:
            push = parse_push(req.body)
        except InvalidRequest as e:
            return Response(400, "text/plain", f"agentos git gateway: {e.message}\n".encode())

        def refuse(message: str) -> Response:
            text = "agentos: " + self._scrub(message)
            refused: dict[str, str | None] = {c.ref: text for c in push.commands}
            return Response(200, ctype, build_report(refused, sideband=push.sideband, messages=[text]))
        try:
            caller, rule = self._resolve(req, remote, listener_lane, push=True)
            account = self._account(caller, rule)
            ctx = self._context(caller, "git_push", account)
            self._check_scope(ctx, caller, rule)
            target = await self._target(rule, remote, push=True)
        except Refused as e:
            return refuse(e.message)
        mirror = self.mirror(remote)
        if not (mirror.path / "HEAD").is_file():
            return refuse("fetch the refs first (the node has no mirror of this repository yet)")
        async with mirror.lock:
            return await self._push(ctx, caller, rule, remote, target, mirror, push, req.body, refuse)

    async def _push(self, ctx: Any, caller: Caller, rule: RemoteRule, remote: Remote, target: Target,
                    mirror: Mirror, push: Push, body: bytes, refuse: Callable[[str], Response]) -> Response:
        """One push, in its steps: receive the pack into the mirror, read what each ref does, refuse what
        an environment may not do, ask the policy, then send it upstream. Anything that stops it puts the
        mirror back as it was."""
        account = self._account(caller, rule)
        before = await mirror.refs()
        rc, out, _ = await mirror.git("receive-pack", "--stateless-rpc", ".", stdin=body)
        failed = await _receive_failed(mirror, push, before, rc, out)
        if failed is not None:
            return failed
        plan = await _plan(mirror, push, rule, remote, before)
        run = _PushRun(ctx, caller, remote, target, mirror, push, before, account, refuse)
        if caller.bound:
            # An agent's own environment (its VM's token): a push never asks a person, on any branch, the
            # default branch included (the owner's decision of 2026-11-13, docs/design/environments.md). What
            # stays is a refusal: history on a protected branch, tags, and any branch the environment did
            # not create itself (a colleague's work) are not rewritten or deleted from one.
            plan.rewrites += _foreign_rewrites(plan, self._own_branches(caller), self._own_heads(caller))
            if plan.rewrites:
                return await _refuse_rewrites(run, plan)
            plan.why = []  # what it does is on the record; none of it waits for a person
        effect = Effect.IRREVERSIBLE if plan.why else Effect.COMMIT
        trace = {"lane": account, "remote": remote.slug, "via": caller.label, "refs": _without_log(plan.refs),
                 "effect": effect.value, "vault_ref": target.credential.ref if target.credential else None}
        refused = await self._authorize_push(run, plan, effect, trace)
        if refused is not None:
            return refused
        return await self._send_upstream(run, plan, trace, out)

    async def _authorize_push(self, run: _PushRun, plan: _PushPlan, effect: Effect,
                              trace: dict[str, Any]) -> Response | None:
        """Ask the policy for the push at its effect. None when it may go; else the mirror is put back,
        the refusal is on the record, and git hears it."""
        names = ", ".join(r["ref"].removeprefix("refs/heads/") for r in plan.refs)
        summary = (f"Push {names} to {run.remote.slug} ({run.caller.label})"
                   + (f": {'; '.join(plan.why)}" if plan.why else ""))
        content = {"git_push": run.remote.slug, "refs": plan.refs}
        try:
            await run.ctx.authorize(effect, lane_id=run.account, summary=summary[:400], content=content)
        except ApprovalPending as e:
            await run.mirror.restore(run.before, run.push.commands)
            run.ctx.trace("git_push", result="approval_pending", approval=e.approval_id, **trace)
            reason = self._pending_reason(e, plan.why).rstrip(".")
            return run.refuse(f"this push waits for a person (approval {e.approval_id}): {reason}. Tell the user; "
                              "once it is approved, push again with the same commits. Do not change or split the "
                              "push to get around it")
        except AgentosError as e:
            await run.mirror.restore(run.before, run.push.commands)
            run.ctx.trace("git_push", result="denied", status="denied", reason=e.message[:300], **trace)
            return run.refuse(f"this push was refused: {e.message}")
        return None

    async def _send_upstream(self, run: _PushRun, plan: _PushPlan, trace: dict[str, Any], out: bytes) -> Response:
        """Push the authorized refs to the real remote with the vault's credential, and answer git with
        the mirror's own report. A remote that fails puts the mirror back."""
        purpose = f"git push {run.remote.slug} for {run.caller.label}"
        try:
            async with credential_env(run.target, home=self.home, agent_dir=self.agent_dir,
                                      read_secret=self._read_secret, purpose=purpose) as auth:
                await run.mirror.push(run.target, auth, run.push.commands, plan.force)
        except UpstreamError as e:
            await run.mirror.restore(run.before, run.push.commands)
            run.ctx.trace("git_push", result="failed", status="error", reason=e.message[:300], **trace)
            return run.refuse(f"the push did not reach {run.remote.slug}: {e.message}")
        run.ctx.trace("git_push", result="pushed", **trace)
        if run.caller.bound:
            self._note_branches(run)
        return Response(200, RECEIVE_RESULT, out)

    def _environments(self) -> Any:
        return self.runtime.get("environments") if self.runtime is not None else None

    def _own_branches(self, caller: Caller) -> set[str]:
        """The branches an environment's VM created on the remote (it pushed them first). None known,
        or no environments service: an empty set, so every existing branch counts as someone else's."""
        fn = getattr(self._environments(), "branches_created", None)
        if not callable(fn) or caller.lane is None:
            return set()
        try:
            return set(fn(caller.lane))
        except Exception:  # noqa: BLE001 - an unreadable record owns nothing
            log.warning("git gateway: could not read the branches %s created", caller.lane)
            return set()

    def _own_heads(self, caller: Caller) -> dict[str, str] | None:
        """{branch: the commit the environment last pushed there} for its own branches, or None when the
        environments service keeps no heads (then the names alone decide). Unreadable: none known, so
        every existing branch counts as someone else's."""
        fn = getattr(self._environments(), "branch_heads", None)
        if not callable(fn) or caller.lane is None:
            return None
        try:
            return dict(fn(caller.lane))
        except Exception:  # noqa: BLE001 - an unreadable record owns nothing
            log.warning("git gateway: could not read the branch heads of %s", caller.lane)
            return {}

    def _note_branches(self, run: _PushRun) -> None:
        """After an environment's push reached the remote: the branches it created are its own from now
        on, at the commit it pushed, and the ones it deleted no longer are. An update keeps a branch its
        own only when it was its own at that commit (a colleague's push in between makes it theirs too)."""
        envs = self._environments()
        fn = getattr(envs, "note_branches", None)
        if not callable(fn) or run.caller.lane is None:
            return
        own, heads_before = self._own_branches(run.caller), self._own_heads(run.caller)
        created = [c.branch for c in run.push.commands if c.branch and c.kind == "create"]
        deleted = [c.branch for c in run.push.commands if c.branch and c.kind == "delete"]
        heads = {c.branch: c.new for c in run.push.commands if c.branch and (
            c.kind == "create" or (c.kind == "update" and _is_own(c.branch, c.old, own, heads_before)))}
        try:
            if created or deleted:
                fn(run.caller.lane, created=created, deleted=deleted)
            note = getattr(envs, "note_heads", None)
            if callable(note) and heads:
                note(run.caller.lane, heads)
        except Exception:  # the push went; only the record of whose branch it is is missing
            log.exception("git gateway: could not record the branches %s pushed", run.caller.lane)

    def _pending_reason(self, e: ApprovalPending, why: list[str]) -> str:
        """Why a push waits, in the words the card has: untrusted text it follows, what makes it
        irreversible, or the routing note (Ask me first on the lane)."""
        if e.why:
            return e.why
        if why:
            return f"{'; '.join(why)}, so a person approves it"
        approvals = self.runtime.get("approvals") if self.runtime is not None else None
        try:
            ap = approvals.get(e.approval_id) if approvals is not None else None
        except AgentosError:
            ap = None
        note = getattr(ap, "reason", None)
        return note or "a person approves pushes from here"

    # ------------------------------------------------------------------ HTTP
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                            listener_lane: str | None) -> None:
        try:
            req = await read_request(reader, writer)
            if req is None:
                return
            try:
                resp = await self.handle(req, listener_lane=listener_lane)
            except Exception:
                log.exception("git gateway request failed")
                resp = Response(500, "text/plain", b"agentos git gateway: internal error; see the node's log\n")
            await write_response(writer, resp)
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except InvalidRequest as e:
            with contextlib.suppress(Exception):
                await write_response(writer, Response(400, "text/plain", f"agentos git gateway: {e.message}\n".encode()))
        finally:
            writer.close()


# ---------------------------------------------------------------------- a push, step by step

RECEIVE_RESULT = "application/x-git-receive-pack-result"


@dataclass
class _PushRun:
    """One push through the gateway, as its steps share it: who pushes, where, the mirror it went into
    and the refs the mirror had before it (`before`, what a stopped push restores)."""
    ctx: Any
    caller: Caller
    remote: Remote
    target: Target
    mirror: Mirror
    push: Push
    before: dict[str, str]
    account: str | None
    refuse: Callable[[str], Response]


@dataclass
class _PushPlan:
    """What a push does, ref by ref: each ref's record (`refs`, the approval binds them), `why` it is
    irreversible, the `rewrites` an environment may not make, and the refs it force-pushes."""
    refs: list[dict[str, Any]] = field(default_factory=list)
    why: list[str] = field(default_factory=list)
    rewrites: list[str] = field(default_factory=list)
    force: set[str] = field(default_factory=set)


def _without_log(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The refs as the trace keeps them: without the commit log lines."""
    return [{k: v for k, v in r.items() if k != "log"} for r in refs]


async def _receive_failed(mirror: Mirror, push: Push, before: dict[str, str], rc: int, out: bytes) -> Response | None:
    """None when receive-pack applied every ref to the mirror. Otherwise the mirror goes back and git
    hears why: git's own words for a bad pack, else each ref's reason."""
    try:
        report, _msgs = demux(out, push.sideband)
        unpack, results = parse_report(report)
    except InvalidRequest:
        unpack, results = "", {}
    applied = [c for c in push.commands if c.ref in results and results[c.ref] is None]
    if unpack == "ok" and len(applied) == len(push.commands):
        return None
    await mirror.restore(before, applied)
    if unpack != "ok" and rc == 0 and out:
        return Response(200, RECEIVE_RESULT, out)  # the pack itself was bad: git's own words, nothing applied
    reasons = {c.ref: (results.get(c.ref) or "not pushed: another ref of this push was refused")
               for c in push.commands}
    return Response(200, RECEIVE_RESULT, build_report(reasons, sideband=push.sideband))


async def _default_branch(mirror: Mirror) -> str | None:
    """The mirror's default branch: as it knows it, else its HEAD."""
    if mirror.default_branch is not None:
        return mirror.default_branch
    src, head, _ = await mirror.git("symbolic-ref", "--short", "HEAD")
    return (head.decode().strip() or None) if src == 0 else None


async def _plan(mirror: Mirror, push: Push, rule: RemoteRule, remote: Remote, before: dict[str, str]) -> _PushPlan:
    """What each ref of the push does, in the order git sent them."""
    default = await _default_branch(mirror)
    plan = _PushPlan()
    for c in push.commands:
        plan.refs.append(await _plan_ref(plan, c, mirror, rule, remote, default, before))
    return plan


async def _plan_ref(plan: _PushPlan, c: Command, mirror: Mirror, rule: RemoteRule, remote: Remote,
                    default: str | None, before: dict[str, str]) -> dict[str, Any]:
    """One ref's record, and what it adds to the plan: a force push, a delete, a tag moved, a protected
    branch (each a reason it is irreversible), and the rewrites an environment may not make."""
    entry: dict[str, Any] = {"ref": c.ref, "kind": c.kind, "old": oid(c.old), "new": oid(c.new)}
    protected = bool(c.branch and rule.protected(c.branch, default))
    if c.kind == "update" and not await mirror.is_ancestor(c.old, c.new):
        plan.force.add(c.ref)
        entry["force"] = True
        plan.why.append(f"it force-pushes {c.ref} (commits on the remote would be lost)")
        if protected:
            plan.rewrites.append(f"it force-pushes {c.branch}, a protected branch of {remote.slug}")
    if c.kind == "delete":
        plan.why.append(f"it deletes {c.ref}")
        if protected or c.tag:
            plan.rewrites.append(f"it deletes {c.branch or c.tag}" + (f", a protected branch of {remote.slug}"
                                                                      if protected else " (a tag)"))
    elif c.tag and c.kind == "update":
        plan.why.append(f"it moves the tag {c.tag}")
        plan.rewrites.append(f"it moves the tag {c.tag}")
    if protected:
        entry["protected"] = True
        plan.why.append(f"{c.branch} is a protected branch of {remote.slug}")
    if c.kind != "delete":
        count, log_lines = await mirror.commits(c.new, sorted(set(before.values())))
        entry["commits"], entry["log"] = count, log_lines
    return entry


def _is_own(branch: str, old: str, own: set[str], heads: dict[str, str] | None) -> bool:
    """Is `branch` the environment's own as the remote has it now (at `old`)? It created the name, and
    the remote still has the commit it last pushed there: a name deleted and recreated by someone else,
    or a branch a colleague added to, is theirs too, so it is not rewritten or deleted from here."""
    if branch not in own:
        return False
    return heads is None or heads.get(branch) == old.split(":", 1)[-1]


def _foreign_rewrites(plan: _PushPlan, own: set[str], heads: dict[str, str] | None = None) -> list[str]:
    """What an environment's push would delete or force-push of branches it did not create itself (or
    that changed on the remote since it last pushed them). A protected branch's rewrite is already in
    the plan; a fast-forward of any branch stays allowed."""
    out = []
    for r in plan.refs:
        branch = r["ref"].removeprefix("refs/heads/") if r["ref"].startswith("refs/heads/") else None
        if branch is None or r.get("protected") or _is_own(branch, str(r.get("old") or ""), own, heads):
            continue
        if r["kind"] == "delete":
            out.append(f"it deletes {branch}, a branch this environment did not create")
        elif r.get("force"):
            out.append(f"it force-pushes {branch}, a branch this environment did not create")
    return out


async def _refuse_rewrites(run: _PushRun, plan: _PushPlan) -> Response:
    """An environment's push that rewrites a protected branch, a tag or a branch it did not create:
    nothing goes, the mirror goes back, and the agent is told to push a new branch instead."""
    await run.mirror.restore(run.before, run.push.commands)
    run.ctx.trace("git_push", result="refused", status="denied", reason="push_refused", lane=run.account,
                  remote=run.remote.slug, via=run.caller.label, refs=_without_log(plan.refs))
    return run.refuse(f"push_refused: {'; '.join(plan.rewrites)}, which an environment may not do; nothing was "
                      "pushed. Fix: push your work to a new branch of your own (git push origin HEAD:<new-branch>) "
                      "and open a pull request")


class Listener:
    """One listening socket of the gateway: for a lane (its namespace's loopback) or the node's own
    loopback (`lane` None). Has the `serve(sock)` / `close()` shape LaneRuntime.listen expects."""

    def __init__(self, gateway: GitGateway, lane: str | None, *, max_active: int = MAX_ACTIVE):
        self.gateway = gateway
        self.lane = lane
        self.max_active = max_active if lane is not None else max_active * 4  # the node's: every brain
        self.active = 0
        self._server: asyncio.AbstractServer | None = None

    async def serve(self, sock: Any) -> asyncio.AbstractServer:
        sock.setblocking(False)
        self._server = await asyncio.start_server(self._client, sock=sock, limit=MAX_HEADERS)
        return self._server

    async def listen(self, host: str, port: int) -> int:
        self._server = await asyncio.start_server(self._client, host=host, port=port, limit=MAX_HEADERS)
        return int(self._server.sockets[0].getsockname()[1])

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.active >= self.max_active:
            with contextlib.suppress(Exception):
                await write_response(writer, Response(503, "text/plain", b"agentos git gateway: busy; try again\n"))
            writer.close()
            return
        self.active += 1
        try:
            await self.gateway.handle_client(reader, writer, listener_lane=self.lane)
        finally:
            self.active -= 1

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
            self._server = None


async def _stream(mirror: Mirror, args: list[str], body: bytes) -> AsyncIterator[bytes]:
    from agentos.git.upstream import git_bin

    proc = await asyncio.create_subprocess_exec(
        git_bin(), *args, cwd=str(mirror.path), env=mirror.env(), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    assert proc.stdin is not None and proc.stdout is not None

    async def feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(body)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()

    feeder = asyncio.create_task(feed())
    try:
        while True:
            chunk = await asyncio.wait_for(proc.stdout.read(65536), READ_TIMEOUT)
            if not chunk:
                break
            yield chunk
    finally:
        feeder.cancel()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        await proc.wait()


async def read_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Request | None:
    """One HTTP/1.1 request: Content-Length or chunked body, gzip or deflate decoded, bounded."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 60)
    except asyncio.LimitOverrunError:
        raise InvalidRequest("request headers too large") from None
    except asyncio.IncompleteReadError:
        return None
    lines = head.decode("latin-1").split("\r\n")
    try:
        method, target, _ = lines[0].split(" ", 2)
    except ValueError:
        raise InvalidRequest("malformed request line") from None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    limit = MAX_PUSH_BODY if target.split("?")[0].endswith("/git-receive-pack") else MAX_FETCH_BODY
    if headers.get("expect", "").lower() == "100-continue":
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        await writer.drain()
    body = b""
    if "chunked" in headers.get("transfer-encoding", "").lower():
        parts = []
        total = 0
        while True:
            size_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), READ_TIMEOUT)
            try:
                size = int(size_line.split(b";")[0].strip(), 16)
            except ValueError:
                raise InvalidRequest("malformed chunk") from None
            if size == 0:
                await asyncio.wait_for(reader.readuntil(b"\r\n"), READ_TIMEOUT)
                break
            total += size
            if total > limit:
                raise InvalidRequest("request body too large")
            parts.append(await asyncio.wait_for(reader.readexactly(size), READ_TIMEOUT))
            await asyncio.wait_for(reader.readexactly(2), READ_TIMEOUT)
        body = b"".join(parts)
    elif headers.get("content-length"):
        try:
            n = int(headers["content-length"])
        except ValueError:
            raise InvalidRequest("bad Content-Length") from None
        if n < 0 or n > limit:
            raise InvalidRequest("request body too large")
        body = await asyncio.wait_for(reader.readexactly(n), READ_TIMEOUT)
    enc = headers.get("content-encoding", "").lower()
    if enc in ("gzip", "x-gzip", "deflate"):
        body = _inflate(body, enc, limit)
    return Request(method.upper(), target, headers, body)


def _inflate(body: bytes, enc: str, limit: int) -> bytes:
    try:
        if enc == "deflate":
            d = zlib.decompressobj()
        else:
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = d.decompress(body, limit + 1)
    except (zlib.error, OSError, gzip.BadGzipFile):
        raise InvalidRequest("the request body is not valid compressed data") from None
    if len(out) > limit:
        raise InvalidRequest("request body too large")
    return out


async def write_response(writer: asyncio.StreamWriter, resp: Response) -> None:
    reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
              500: "Internal Server Error", 503: "Service Unavailable"}.get(resp.status, "Error")
    head = (f"HTTP/1.1 {resp.status} {reason}\r\nContent-Type: {resp.content_type}\r\n"
            "Cache-Control: no-cache, max-age=0, must-revalidate\r\nConnection: close\r\n")
    if resp.stream is None:
        body = resp.body or b""
        writer.write((head + f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
        await writer.drain()
        return
    writer.write((head + "Transfer-Encoding: chunked\r\n\r\n").encode())
    async for chunk in resp.stream:
        writer.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
        await writer.drain()
    writer.write(b"0\r\n\r\n")
    await writer.drain()
