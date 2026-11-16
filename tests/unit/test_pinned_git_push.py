"""Pins GitGateway._push for a table of pushes (agentos.git.gateway): what it answers git, what it
records, what it authorizes, what it restores in the mirror and what it pushes upstream, byte for
byte. Fakes stand in for the mirror, the policy context and the upstream credential, so every branch
of the method runs: a bad pack, a ref git refused, force pushes, deletes, tags, protected branches,
an environment's own push, an approval that waits, a refusal, and an upstream failure.

Written from the code before GitGateway._push was split into named steps (tests/unit/pinned.py)."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pinned import check, plain

from agentos.core.errors import ApprovalPending, PolicyDenied
from agentos.git import gateway as gw
from agentos.git.gateway import Caller, GitGateway, Response
from agentos.git.keys import Credential
from agentos.git.remotes import RemoteRule
from agentos.git.upstream import Target, UpstreamError
from agentos.git.wire import Command, Push, Remote, build_report, pkt

ZERO = "0" * 40
A, B, C, D = ("a" * 40), ("b" * 40), ("c" * 40), ("d" * 40)
REMOTE = Remote("github.com", "biz-a/site")
CRED = Credential(ref="git/github", kind="token", host="github.com", repos=("biz-a/site",), label="deploy")
CTYPE = "application/x-git-receive-pack-result"


def report(results: dict[str, str | None], unpack: str = "ok") -> bytes:
    return build_report(results, sideband=False, unpack=unpack)


@dataclass
class FakeMirror:
    out: bytes = b""
    rc: int = 0
    default_branch: str | None = "main"
    head: bytes = b"main\n"
    head_rc: int = 0
    ancestors: dict[tuple[str, str], bool] = field(default_factory=dict)
    push_error: str | None = None
    calls: list[Any] = field(default_factory=list)

    async def refs(self) -> dict[str, str]:
        self.calls.append(["refs"])
        return {"refs/heads/main": A, "refs/tags/v1": C}

    async def git(self, *args: str, stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
        self.calls.append(["git", *args, len(stdin or b"")])
        if args[0] == "symbolic-ref":
            return self.head_rc, self.head, b""
        return self.rc, self.out, b""

    async def restore(self, before: dict[str, str], commands: list[Command]) -> None:
        self.calls.append(["restore", sorted(before.items()), [c.ref for c in commands]])

    async def is_ancestor(self, old: str, new: str) -> bool:
        self.calls.append(["is_ancestor", old[:1], new[:1]])
        return self.ancestors.get((old, new), True)

    async def commits(self, new: str, haves: list[str]) -> tuple[int, list[str]]:
        self.calls.append(["commits", new[:1], [h[:1] for h in haves]])
        return 2, [f"{new[:7]} a change", f"{new[:7]} another"]

    async def push(self, target: Target, auth: Any, commands: list[Command]) -> None:
        self.calls.append(["push", target.url, auth, [c.ref for c in commands]])
        if self.push_error:
            raise UpstreamError(self.push_error)


@dataclass
class FakeCtx:
    raises: Exception | None = None
    calls: list[Any] = field(default_factory=list)

    def trace(self, kind: str, **fields: Any) -> None:
        self.calls.append(["trace", kind, fields])

    async def authorize(self, effect: Any, **kw: Any) -> None:
        self.calls.append(["authorize", effect, kw])
        if self.raises is not None:
            raise self.raises


class Approvals:
    def get(self, approval_id: str) -> Any:
        return type("Ap", (), {"reason": "Ask me first is on for biz-a/dev"})()


class FakeEnvironments:
    """The branches an environment created (EnvironmentManager.branches_created), and what a push noted."""

    def __init__(self, own: tuple[str, ...]):
        self.own = list(own)
        self.noted: list[Any] = []

    def branches_created(self, lane_id: str) -> list[str]:
        return list(self.own)

    def note_branches(self, lane_id: str, *, created: list[str], deleted: list[str]) -> None:
        self.noted.append([lane_id, created, deleted])


class FakeRuntime:
    def __init__(self, approvals: bool = False, own: tuple[str, ...] | None = None):
        self.approvals = Approvals() if approvals else None
        self.environments = FakeEnvironments(own) if own is not None else None

    def get(self, name: str) -> Any:
        return {"approvals": self.approvals, "environments": self.environments}.get(name)


@contextlib.asynccontextmanager
async def fake_credential_env(target: Target, *, home: Path, agent_dir: Path, read_secret: Any, purpose: str):
    yield f"auth for {purpose}"


def caller(*, bound: bool = False, lane: str | None = "biz-a/dev") -> Caller:
    return Caller(grant_id="g1", workspace="biz-a", lane=lane, lanes=(), label="lane biz-a/dev", bound=bound)


def rule(**kw: Any) -> RemoteRule:
    return RemoteRule(scope="biz-a", host="github.com", repo="biz-a/site", added_at="2026-11-01T00:00:00+00:00", **kw)


def refuse(message: str) -> Response:
    return Response(200, CTYPE, b"REFUSED " + message.encode())


@dataclass
class Case:
    commands: list[Command]
    mirror: FakeMirror
    ctx: FakeCtx = field(default_factory=FakeCtx)
    caller: Caller = field(default_factory=caller)
    rule: RemoteRule = field(default_factory=rule)
    credential: Credential | None = CRED
    sideband: bool = False
    approvals: bool = False
    own: tuple[str, ...] | None = None    # an environments service whose environment created these branches


def ok_report(*refs: str) -> bytes:
    return report(dict.fromkeys(refs))


def mirror_for(*refs: str, **kw: Any) -> FakeMirror:
    return FakeMirror(out=ok_report(*refs), **kw)


FEATURE = Command(ZERO, B, "refs/heads/feature")
MAIN_FF = Command(A, B, "refs/heads/main")
MAIN_FORCE = Command(A, D, "refs/heads/main")
FEATURE_FORCE = Command(A, D, "refs/heads/feature")
DELETE_FEATURE = Command(B, ZERO, "refs/heads/feature")
DELETE_MAIN = Command(A, ZERO, "refs/heads/main")
DELETE_TAG = Command(C, ZERO, "refs/tags/v1")
MOVE_TAG = Command(C, D, "refs/tags/v1")
NEW_TAG = Command(ZERO, D, "refs/tags/v2")
PENDING = ApprovalPending("waits", "ap_1")
PENDING_WHY = ApprovalPending("waits", "ap_2", why="it follows text from a web page")

CASES: dict[str, Case] = {
    "bad pack, git's own words": Case([FEATURE], FakeMirror(out=b"unpack index-pack abnormal exit\n")),
    "bad pack, nothing said": Case([FEATURE], FakeMirror(out=b"", rc=1)),
    "bad pack, git failed": Case([FEATURE], FakeMirror(out=b"junk", rc=128)),
    "unpack not ok in the report": Case([FEATURE], FakeMirror(out=report({"refs/heads/feature": None},
                                                                        unpack="index-pack failed"))),
    "one ref refused by git": Case([FEATURE, MAIN_FF], FakeMirror(out=report({"refs/heads/feature": None,
                                                                             "refs/heads/main": "hook declined"}))),
    "one ref refused, side band": Case([FEATURE, MAIN_FF], FakeMirror(out=report({"refs/heads/feature": None,
                                                                                 "refs/heads/main": "stale info"})),
                                       sideband=True),
    "a ref missing from the report": Case([FEATURE, MAIN_FF], mirror_for("refs/heads/feature")),
    "new feature branch": Case([FEATURE], mirror_for("refs/heads/feature")),
    "new feature branch, no credential": Case([FEATURE], mirror_for("refs/heads/feature"), credential=None),
    "fast-forward of the default branch": Case([MAIN_FF], mirror_for("refs/heads/main")),
    "default branch read from HEAD": Case([MAIN_FF], mirror_for("refs/heads/main", default_branch=None)),
    "default branch HEAD unreadable": Case([MAIN_FF], mirror_for("refs/heads/main", default_branch=None, head_rc=1)),
    "default branch HEAD empty": Case([MAIN_FF], mirror_for("refs/heads/main", default_branch=None, head=b"\n")),
    "default branch allowed": Case([MAIN_FF], mirror_for("refs/heads/main"), rule=rule(allow_default_branch=True)),
    "force push of the default branch": Case([MAIN_FORCE], mirror_for("refs/heads/main",
                                                                      ancestors={(A, D): False})),
    "force push of a feature branch": Case([FEATURE_FORCE], mirror_for("refs/heads/feature",
                                                                       ancestors={(A, D): False})),
    "force push of a protected glob": Case([FEATURE_FORCE], mirror_for("refs/heads/feature", ancestors={(A, D): False}),
                                           rule=rule(protect=["feat*"])),
    "delete a feature branch": Case([DELETE_FEATURE], mirror_for("refs/heads/feature")),
    "delete the default branch": Case([DELETE_MAIN], mirror_for("refs/heads/main")),
    "delete a tag": Case([DELETE_TAG], mirror_for("refs/tags/v1")),
    "move a tag": Case([MOVE_TAG], mirror_for("refs/tags/v1")),
    "new tag": Case([NEW_TAG], mirror_for("refs/tags/v2")),
    "several refs at once": Case([FEATURE, MAIN_FF, NEW_TAG], mirror_for("refs/heads/feature", "refs/heads/main",
                                                                         "refs/tags/v2")),
    "env: new branch": Case([FEATURE], mirror_for("refs/heads/feature"), caller=caller(bound=True)),
    "env: fast-forward of the default branch": Case([MAIN_FF], mirror_for("refs/heads/main"),
                                                    caller=caller(bound=True)),
    "env: force push of a feature branch": Case([FEATURE_FORCE], mirror_for("refs/heads/feature",
                                                                            ancestors={(A, D): False}),
                                                caller=caller(bound=True)),
    "env: force push of the default branch": Case([MAIN_FORCE], mirror_for("refs/heads/main",
                                                                           ancestors={(A, D): False}),
                                                  caller=caller(bound=True)),
    "env: delete the default branch": Case([DELETE_MAIN], mirror_for("refs/heads/main"), caller=caller(bound=True)),
    "env: delete a feature branch": Case([DELETE_FEATURE], mirror_for("refs/heads/feature"),
                                         caller=caller(bound=True)),
    "env: delete a tag": Case([DELETE_TAG], mirror_for("refs/tags/v1"), caller=caller(bound=True)),
    "env: move a tag": Case([MOVE_TAG], mirror_for("refs/tags/v1"), caller=caller(bound=True)),
    "env: new tag": Case([NEW_TAG], mirror_for("refs/tags/v2"), caller=caller(bound=True)),
    "env: force push of a branch it created": Case([FEATURE_FORCE], mirror_for("refs/heads/feature",
                                                                                ancestors={(A, D): False}),
                                                    caller=caller(bound=True), own=("feature",)),
    "env: delete a branch it created": Case([DELETE_FEATURE], mirror_for("refs/heads/feature"),
                                            caller=caller(bound=True), own=("feature",)),
    "env: delete another's branch, its own known": Case([DELETE_FEATURE], mirror_for("refs/heads/feature"),
                                                        caller=caller(bound=True), own=("agent/x",)),
    "env: new branch, noted as its own": Case([FEATURE, NEW_TAG], mirror_for("refs/heads/feature", "refs/tags/v2"),
                                              caller=caller(bound=True), own=()),
    "env: fast-forward of another's branch": Case([MAIN_FF], mirror_for("refs/heads/main"),
                                                  caller=caller(bound=True), own=()),
    "env: several rewrites": Case([MAIN_FORCE, MOVE_TAG], mirror_for("refs/heads/main", "refs/tags/v1",
                                                                     ancestors={(A, D): False}),
                                  caller=caller(bound=True)),
    "no lane: a brain's push": Case([FEATURE], mirror_for("refs/heads/feature"), caller=caller(lane=None)),
    "waits for a person": Case([FEATURE], mirror_for("refs/heads/feature"), ctx=FakeCtx(raises=PENDING)),
    "waits, irreversible": Case([MAIN_FF], mirror_for("refs/heads/main"), ctx=FakeCtx(raises=PENDING)),
    "waits, with its own why": Case([MAIN_FF], mirror_for("refs/heads/main"), ctx=FakeCtx(raises=PENDING_WHY)),
    "waits, the approval's note": Case([FEATURE], mirror_for("refs/heads/feature"), ctx=FakeCtx(raises=PENDING),
                                       approvals=True),
    "refused by policy": Case([FEATURE], mirror_for("refs/heads/feature"),
                              ctx=FakeCtx(raises=PolicyDenied("this grant may not commit on biz-a/dev"))),
    "upstream failed": Case([FEATURE], mirror_for("refs/heads/feature",
                                                  push_error="github.com refused the push: permission denied")),
}


def _no_timings(value: Any) -> Any:
    """The pin without the wall-clock step timings the gateway traces (`seconds`): they vary by machine."""
    if isinstance(value, dict):
        return {k: _no_timings(v) for k, v in value.items() if k != "seconds"}
    if isinstance(value, list):
        return [_no_timings(v) for v in value]
    return value


def run(case: Case, tmp: Path) -> dict[str, Any]:
    runtime = FakeRuntime(case.approvals, case.own)
    gateway = GitGateway(home=tmp, remotes=None, runtime=runtime)  # type: ignore[arg-type]
    target = Target(remote=REMOTE, url="https://github.com/biz-a/site.git", credential=case.credential)
    push = Push(list(case.commands), {"report-status", "side-band-64k"} if case.sideband else {"report-status"})
    body = pkt("whatever the client sent\n")
    resp = asyncio.run(gateway._push(case.ctx, case.caller, case.rule, REMOTE, target, case.mirror,  # type: ignore[arg-type]
                                     push, body, refuse))
    out = {"response": [resp.status, resp.content_type, (resp.body or b"").decode("latin-1")],
           "mirror": plain(case.mirror.calls), "ctx": _no_timings(plain(case.ctx.calls))}
    if runtime.environments is not None:
        out["noted"] = plain(runtime.environments.noted)
    return out


def test_push_is_pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw, "credential_env", fake_credential_env)
    check("git_push", {name: run(case, tmp_path) for name, case in CASES.items()})
