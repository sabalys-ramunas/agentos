"""A node for the workflow tests: the real policy, runner, events and workflows on the fake site."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from test_policy_support import Clock

from openajar.core.models import Effect
from openajar.events.service import build as build_events
from openajar.policy.service import PolicyService
from openajar.runner.fakes import BASE, Harness, post_flow
from openajar.runner.workflows.mail import Mailbox, Received, SendRefused
from openajar.runner.workflows.service import Actor

LANE = "biz-a/social"
MAIL = "biz-a/mail"
LIST = "list.biz-a.customers"
PEOPLE = ["ana@example.com", "bo@example.com", "cy@example.com"]
OWNER = Actor.owner()


class FakeMail:
    """Stands in for IMAP and SMTP. `refuse` and `lose` say what happens to an address."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.rcpt: list[str] = []
        self.inbox: list[Received] = []
        self.refuse: set[str] = set()
        self.lose: set[str] = set()
        self.seen_uid: list[int | None] = []

    @property
    def to(self) -> list[str]:
        return [str(m["To"]) for m in self.sent]

    async def send(self, box: Mailbox, password: str, message: EmailMessage, *, to: str) -> None:
        if to in self.refuse:
            raise SendRefused("refused")
        if to in self.lose:
            from openajar.runner.workflows.mail import SendInDoubt

            self.sent.append(message)
            self.rcpt.append(to)
            raise SendInDoubt("the connection dropped after the message was handed over")
        self.sent.append(message)
        self.rcpt.append(to)

    async def fetch(self, box: Mailbox, password: str, after_uid: int | None) -> tuple[list[Received], int]:
        self.seen_uid.append(after_uid)
        highest = max((m.uid for m in self.inbox), default=0)
        if after_uid is None:
            return [], highest
        got = [m for m in self.inbox if m.uid > after_uid]
        return got, max((m.uid for m in got), default=after_uid)


class Vault:
    async def get_secret(self, ref: str, *, purpose: str) -> str:
        return "the password"


@dataclass
class Node:
    h: Harness
    svc: PolicyService
    wf: Any
    mail: FakeMail
    events: Any

    @property
    def runner(self) -> Any:
        return self.h.runner

    @property
    def site(self) -> Any:
        return self.h.site

    def agent(self, ceiling: Effect = Effect.COMMIT, workspaces: tuple[str, ...] = ("biz-a",)) -> Actor:
        g, _ = self.svc.issue(subject={"id": "agent-1"}, workspaces=list(workspaces), ceiling=ceiling)
        return Actor({"kind": "agent", "id": "agent-1", "owner": "owner", "grant_id": g.id}, g.id, False, "agent:agent-1")

    async def settle(self, rounds: int = 30) -> None:
        """Run everything due until nothing changes."""
        for _ in range(rounds):
            await self.runner.scheduler.tick()
            await self.events.tick()
            await self.runner.poll_approvals()
            await self.runner.run_until_idle()
            await asyncio.sleep(0)

    async def run(self, flow_id: str, actor: Actor = OWNER, **kw: Any) -> dict[str, Any]:
        view = self.wf.run(flow_id, actor, **kw)
        await self.settle()
        return self.wf.get_run_view(flow_id, view["id"])

    def view(self, flow_id: str, run_id: str) -> dict[str, Any]:
        return self.wf.get_run_view(flow_id, run_id)

    def step(self, view: dict[str, Any], step: str) -> dict[str, Any]:
        return next(s for s in view["steps"] if s["step"] == step)


def make_node(tmp_path, *, site: Any = None) -> Node:
    mail = FakeMail()
    h = Harness(tmp_path / "runner", site=site, mail_transport=mail)
    svc = PolicyService(tmp_path / "policy", runtime=h.runtime, clock=Clock())
    h.runtime.register("policy", svc)
    h.runtime.register("approvals", svc.approvals)
    h.runtime.register("ask_first", svc.ask_first)
    h.flows.ask_first_source = lambda: svc.ask_first
    events = build_events(h.runtime, home=tmp_path / "events", clock=h.clock)
    wf = h.runtime.get("workflows")
    wf.mail.vault = Vault()
    wf.mail.add({"id": MAIL, "address": "hello@biz-a.example", "imap_host": "imap.invalid", "smtp_host": "smtp.invalid",
                 "username": "hello", "secret_ref": "biz-a.mail"}, person=True)
    wf.sends.save_list(LIST, workspace="biz-a", name="Customers", people=[{"email": a, "name": a.split("@")[0]} for a in PEOPLE],
                       person=True, by="owner")
    return Node(h, svc, wf, mail, events)


def read_flow(fid: str = "biz-a.social.read", produces: list[dict] | None = None) -> dict[str, Any]:
    """A read that always works, saved by the owner so what it declares about its fields counts."""
    return {"id": fid, "lane": LANE, "effect": "read", "steps": [{"type": "navigate", "url": f"{BASE}/"}],
            **({"operation": {"produces": produces}} if produces else {})}


def commit_flow(fid: str = "biz-a.social.post", effect: str | None = None) -> dict[str, Any]:
    return post_flow(id=fid, **({"effect": effect} if effect else {}))


def action(id_: str, operation: str, **rest: Any) -> dict[str, Any]:
    return {"id": id_, "kind": "action", "label": id_, "operation": operation, **rest}


def workflow(*steps: dict[str, Any], trigger: dict[str, Any] | None = None, name: str = "A job") -> dict[str, Any]:
    return {"name": name, "workspace": "biz-a", "trigger": trigger or {"kind": "manual"}, "steps": list(steps)}


POST_INPUTS = {"caption": "hello", "image": "a.jpg"}
