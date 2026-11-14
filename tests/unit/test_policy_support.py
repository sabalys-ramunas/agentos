"""Shared fixtures for the policy tests (no tests here): a fake clock, a service, fake tools."""

from __future__ import annotations

from agentos.core.config import NodeConfig
from agentos.core.models import Effect
from agentos.core.runtime import Runtime
from agentos.core.tools import ToolSpec
from agentos.policy.confirm import ConfirmLevel, Proof
from agentos.policy.service import PolicyService

T0 = 1_800_000_000.0  # 2027-01-15, a fixed "now"
OWNER = "owner-token-0123456789abcdef"
PRESENT = Proof("passkey", ConfirmLevel.PRESENT)  # a person proved just now: what a payment asks of a decision


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def make_service(home, *, clock=None, transport=None, owner=OWNER, config=None) -> tuple[PolicyService, Runtime]:
    rt = Runtime()
    cfg = NodeConfig(home=home, mcp_token=owner)
    rt.register("config", cfg)
    svc = PolicyService(home, node_config=cfg, runtime=rt, clock=clock or Clock(), transport=transport,
                        config=config)
    rt.register("policy", svc)
    rt.register("approvals", svc.approvals)
    rt.register("traces", svc.traces)
    rt.register("notifier", svc.notifier)
    return svc, rt


async def h_read(ctx, *, on: str) -> dict:
    """Read a lane."""
    await ctx.authorize(Effect.READ, lane_id=on)
    return {"on": on, "who": ctx.principal.id}


async def h_post(ctx, *, on: str, caption: str) -> dict:
    """Post a caption."""
    await ctx.authorize(Effect.COMMIT, lane_id=on, domain="instagram.com",
                        summary=f"Post a caption to instagram.com as {on}", content={"caption": caption})
    return {"posted": caption}


async def h_exec(ctx, *, on: str, cmd: str, preset: str = "sandbox") -> dict:
    """Run a command."""
    await ctx.authorize(Effect.EXEC, lane_id=on, summary=f"run {cmd!r} in {on}", content=cmd,
                        facts={"preset": preset})
    return {"ran": cmd}


async def h_node(ctx) -> dict:
    """Node-level read."""
    await ctx.authorize(Effect.READ)
    return {"ok": True}


SPECS = [
    ToolSpec(name="lane_read", handler=h_read),
    ToolSpec(name="lane_post", handler=h_post, effect=Effect.DRAFT),
    ToolSpec(name="lane_exec", handler=h_exec, effect=Effect.EXEC),
    ToolSpec(name="node_info", handler=h_node, lane_arg=None),
]


def issue_v1(svc: PolicyService, *, subject: dict | None = None, workspaces: list[str] | None = None,
             ceiling: Effect = Effect.COMMIT, ttl_s: int = 3600, **fields):
    """A version 1 grant, as nodes issued before token v2 (still accepted in the migration
    window). Returns (Grant, token)."""
    from agentos.policy import tokens
    from agentos.policy.grants import Grant, Subject

    block = svc._block(parent=None, version=1, sub=Subject(**(subject or {"id": "claude-code"})),
                       workspaces=workspaces or ["biz-a"], ceiling=ceiling, exp=int(svc.clock()) + ttl_s,
                       depth=fields.pop("depth", 2), specials=fields.pop("specials", None),
                       budget=fields.pop("budget", None), **fields)
    token = tokens.mint(svc.key, block.to_data())
    g = Grant([block], token_hash=tokens.token_hash(token))
    svc._register(g, source="issued", by="owner")
    return g, token
