"""What happened on this computer: the action log (Activity), the audit list, runs, and History as stories.

History is built here the way the hosted cloud builds it from its copy:
one story per finished run, one per finished call that no run started, reads folded into one line a day,
days in the computer's own time. Each sentence is made from fields this node really has, never from the
free text of a summary. The node keeps no proof screenshot route for a run, so a story has none.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.routing import BaseRoute

from agentos.api.app import Api
from agentos.api.web import environments as envs_web
from agentos.api.web import telling
from agentos.api.web.base import call, org_route, read, write
from agentos.core.errors import InvalidRequest

PRINCIPAL_KINDS = {"agent", "human", "flow", "brain", "worker"}
# The Activity histogram's windows: bucket size in seconds, and how many buckets (as the cloud draws them).
CHART_WINDOWS = {"24h": (3600, 24), "7d": (6 * 3600, 28), "30d": (86400, 30)}
MAX_CHART_ROWS = 50_000  # the action log in one chart; more is refused with the reason, not drawn short
# A trace status as a run status: what the app's Activity and History words are built on.
TRACE_STATUS = {"started": "running", "ok": "succeeded_unverified", "pending": "waiting_approval",
                "error": "failed", "denied": "cancelled"}
UNFINISHED = {"queued", "running", "waiting_approval"}
MAX_PAGES = 20  # of the action log, 500 rows each, for one history page
PAGE_ROWS = 500

# What a story did, in the cloud's own words for it. The app's HistoryAction is narrower, so each maps to one.
POST, PUBLISH, REPLY, DRAFT, READ, PAY, CHANGE, TASK = "post", "publish", "reply", "draft", "read", "pay", "change", "task"
SEND, SORT, DOWNLOAD, ORDER, REFUND, SUBMIT, WATCH, DELETE = "send", "sort", "download", "order", "refund", "submit", "watch", "delete"
TYPE_OF = {POST: POST, PUBLISH: PUBLISH, REPLY: REPLY, DRAFT: DRAFT, READ: READ, PAY: PAY, CHANGE: CHANGE, TASK: TASK,
           SEND: POST, SORT: CHANGE, DOWNLOAD: READ, ORDER: PAY, REFUND: PAY, SUBMIT: POST, WATCH: READ, DELETE: CHANGE}
KIND_OF = {SEND: "sent", PAY: "money", REFUND: "money", ORDER: "shop", SUBMIT: "forms", WATCH: "research",
           DOWNLOAD: "files", DELETE: "files", TASK: "files", CHANGE: "files"}
VERBS = {"post": POST, "check": READ, "sort": SORT, "drafts": DRAFT, "send": SEND, "invoices": DOWNLOAD,
         "statement": DOWNLOAD, "pay": PAY, "order": ORDER, "listing": SUBMIT, "vat": SUBMIT, "reservations": READ,
         "orders": READ, "refund": REFUND, "payouts": READ, "prices": WATCH, "leads": DRAFT, "reviews": READ,
         "reply": REPLY, "backup": TASK, "cleanup": DELETE}
PUBLISH_SITES = {"medium", "blog", "wordpress", "substack", "web"}
PHOTO_SITES = {"instagram", "facebook", "pinterest"}
SHOWS = ("everything", "posts", "problems", "spending")

CHECKED, NOT_CHECKED, PARTIAL, FAILED, NOT_RUN, RUNNING = "checked", "not_checked", "partial", "failed", "not_run", "running"
VERDICT_OF = {"succeeded_verified": CHECKED, "succeeded_unverified": NOT_CHECKED, "partial": PARTIAL, "failed": FAILED,
              "cancelled": NOT_RUN, "running": RUNNING, "queued": RUNNING, "waiting_approval": RUNNING}
PROBLEMS = {FAILED, PARTIAL}
# Work in an agent's own environment: there is no account to check it on, so it is done, not "not checked".
DONE_HERE = "done"
PHRASES = {  # {place} is filled in: "post on {place}", and "posting on {place}" while it runs
    POST: ("post on {place}", "posting on {place}"), PUBLISH: ("publish on {place}", "publishing on {place}"),
    REPLY: ("reply on {place}", "replying on {place}"), DRAFT: ("write drafts on {place}", "writing drafts on {place}"),
    READ: ("check {place}", "checking {place}"), PAY: ("pay from {place}", "paying from {place}"),
    CHANGE: ("make a change on {place}", "making a change on {place}"),
    TASK: ("run a task on {place}", "running a task on {place}"),
    SEND: ("send an email from {place}", "sending an email from {place}"),
    SORT: ("sort the inbox on {place}", "sorting the inbox on {place}"),
    DOWNLOAD: ("download files from {place}", "downloading files from {place}"),
    ORDER: ("place an order on {place}", "placing an order on {place}"),
    REFUND: ("refund an order on {place}", "refunding an order on {place}"),
    SUBMIT: ("submit the form on {place}", "filling in the form on {place}"),
    WATCH: ("check prices on {place}", "checking prices on {place}"),
    DELETE: ("delete files on {place}", "deleting files on {place}"),
}
# What a browser act did, told only from what its own trace rows know (finding 23 of the 2026-11-10 test). A
# publish is named only when a commit rule matched a request the act sent; else the line says what the hand did.
LIKE, FOLLOW, JOIN, PROFILE = "like", "follow", "join", "profile"
CLICK, TYPE, PICK, PRESS, POINT, SCROLL, UPLOAD, ANSWER = "click", "type", "pick", "press", "point", "scroll", "upload", "answer"
PHRASES.update({
    LIKE: ("like a post on {place}", "liking a post on {place}"),
    FOLLOW: ("follow an account from {place}", "following an account from {place}"),
    JOIN: ("join a group on {place}", "joining a group on {place}"),
    PROFILE: ("change the profile of {place}", "changing the profile of {place}"),
    CLICK: ("click on {place}", "clicking on {place}"), TYPE: ("type on {place}", "typing on {place}"),
    PICK: ("pick an option on {place}", "picking an option on {place}"),
    PRESS: ("press a key on {place}", "pressing a key on {place}"),
    POINT: ("point at something on {place}", "pointing at something on {place}"),
    SCROLL: ("scroll on {place}", "scrolling on {place}"), UPLOAD: ("add a file on {place}", "adding a file on {place}"),
    ANSWER: ("answer the page's question on {place}", "answering the page's question on {place}"),
})
DONE = {LIKE: "Liked a post on {place}", FOLLOW: "Followed an account from {place}", JOIN: "Joined a group on {place}",
        PROFILE: "Changed the profile of {place}", CLICK: "Clicked on {place}", TYPE: "Typed on {place}",
        PICK: "Picked an option on {place}", PRESS: "Pressed a key on {place}", POINT: "Pointed at something on {place}",
        SCROLL: "Scrolled on {place}", UPLOAD: "Added a file on {place}", ANSWER: "Answered the page's question on {place}"}
for _hand in DONE:
    TYPE_OF[_hand] = CHANGE
# The kind of a commit rule (`<site>.<kind>[.<detail>]`, agentos.node.egress.rules) as a story's action.
RULE_ACTIONS = {"post": POST, "comment": REPLY, "like": LIKE, "follow": FOLLOW, "join": JOIN, "profile": PROFILE}
ACT_ACTIONS = {"click": CLICK, "type": TYPE, "fill": TYPE, "select": PICK, "press": PRESS, "hover": POINT, "scroll": SCROLL}
# Browser tools whose effect is not known before they run: told from their own trace rows, never from the effect.
HAND_TOOLS = {"browser_act": None, "browser_upload": UPLOAD, "browser_dialog_answer": ANSWER}
# Rows a call writes about itself (same parent as its tool_end): what the commit gate let go, and the read-back.
SENT_KINDS = ("commit_gate", "commit_observed")
READ_BACK = "act_read_back"
NOTHING = {POST: "Nothing was posted.", PUBLISH: "Nothing was published.", REPLY: "No reply was sent.",
           DRAFT: "No draft was saved.", READ: "Nothing was read.", PAY: "Nothing was paid.",
           CHANGE: "Nothing was changed.", TASK: "The task did not finish.", SEND: "No email was sent.",
           SORT: "The inbox was not sorted.", DOWNLOAD: "Nothing was downloaded.", ORDER: "No order was placed.",
           REFUND: "No refund was made.", SUBMIT: "Nothing was submitted.", WATCH: "No prices were checked.",
           DELETE: "Nothing was deleted."}
NOTHING.update({LIKE: "Nothing was liked.", FOLLOW: "Nobody was followed.", JOIN: "No group was joined.",
                PROFILE: "The profile was not changed.", UPLOAD: "No file was added.",
                **{hand: "Nothing happened on the page." for hand in (CLICK, TYPE, PICK, PRESS, POINT, SCROLL, ANSWER)}})
TRIGGERS = {"schedule": "On its schedule", "cron": "On its schedule", "agent": "Asked from a chat", "panel": "Started here", "tool": "Asked from a chat"}
# Runs a person started with their own hand on the computer (the panel, the command line, the API, Run in a routine).
BY_HAND = {"panel", "cli", "api", "workflow:run"}
# A call is a story once, by its end row. Every other row (its start, the decisions, gate and guard rows such as
# commit_gate or egress_blocked) only tells about a call, so it is never a story of its own.
STORY_KIND = "tool_end"


def routes(api: Api) -> list[BaseRoute]:
    async def activity(request: Request) -> dict[str, Any]:
        q = request.query_params
        limit = _limit(q.get("limit"), 100)
        rows = await _traces(request, lane=q.get("lane") or None, workspace=q.get("workspace") or None,
                             before=q.get("before") or None, limit=limit)
        keep = [row for row in rows[:limit] if _matches(row, q)]
        return _page([trace_out(row) for row in keep], rows, limit, lambda row: row["id"])

    async def audit(request: Request) -> dict[str, Any]:
        q = request.query_params
        limit = _limit(q.get("limit"), 50)
        rows = await _traces(request, before=q.get("before") or None, limit=limit)
        members = await _names(request)
        items = [{"id": row["id"], "at": row["at"], "actor": _actor(row, members),
                  "action": row["kind"] + (f" {row['tool']}" if row.get("tool") else ""),
                  "target": row.get("lane") or row.get("summary") or "", "ip": None} for row in rows[:limit]]
        return _page(items, rows, limit, lambda row: row["id"])

    async def runs(request: Request) -> list[dict[str, Any]]:
        status = request.query_params.get("status")
        limit = _limit(request.query_params.get("limit"), 50)
        query: dict[str, Any] = {"limit": limit, "status": status or None}
        return [run_out(run) for run in await read(request, "/api/runs", query=query)]

    async def run(request: Request) -> dict[str, Any]:
        return run_out(await read(request, f"/api/runs/{request.path_params['id']}"))

    async def cancel(request: Request) -> dict[str, Any]:
        return run_out(await write(request, "POST", f"/api/runs/{request.path_params['id']}/cancel"))

    async def decide(request: Request) -> dict[str, Any]:
        body = await _body(request)
        decision = body.get("decision")
        if decision not in ("retry", "skip", "fail"):
            raise InvalidRequest("decision is retry, skip or fail.")
        return run_out(await write(request, "POST", f"/api/runs/{request.path_params['id']}/decide", {"decision": decision}))

    async def history(request: Request) -> dict[str, Any]:
        return await _history(request, api)

    async def chart(request: Request) -> dict[str, Any]:
        return await _activity_chart(request, api)

    return [
        org_route(api, "/activity", activity, ["GET"]),
        org_route(api, "/audit", audit, ["GET"]),
        org_route(api, "/runs", runs, ["GET"]),
        org_route(api, "/runs/{id}", run, ["GET"]),
        org_route(api, "/runs/{id}/cancel", cancel, ["POST"]),
        org_route(api, "/runs/{id}/decide", decide, ["POST"]),
        org_route(api, "/history", history, ["GET"]),
        org_route(api, "/metrics/activity", chart, ["GET"]),
    ]


# ---- reshaping what the node answers ----

def principal_out(raw: dict[str, Any] | str | None) -> dict[str, Any] | str:
    if not isinstance(raw, dict) or not raw.get("id"):
        return ""
    kind = raw.get("kind") if raw.get("kind") in PRINCIPAL_KINDS else "agent"
    return {"kind": kind, "id": raw["id"], "thread": raw.get("thread")}


# A lane control's action, as the body named it (node.viewer_actions.ACTIONS). Only these reach the app.
CONTROL_ACTIONS = frozenset({"take", "hand_back", "stop_agent", "deny_request"})


def trace_out(row: dict[str, Any]) -> dict[str, Any]:
    cost = row.get("cost") or {}
    args = row.get("args") if isinstance(row.get("args"), dict) else {}
    # `control` is additive: the action of a lane control (take over, hand back), else null.
    control = args.get("control") if args.get("control") in CONTROL_ACTIONS else None
    return {"id": row["id"], "at": row["at"], "kind": row["kind"], "tool": row.get("tool") or "",
            "lane": row.get("lane") or "", "principal": principal_out({"kind": row.get("principal_kind"), "id": row.get("principal")}),
            "effect": row.get("effect") or "", "status": TRACE_STATUS.get(row.get("status"), "failed"),
            "summary": row.get("summary") or "", "cost": {"tokens": int(cost.get("tokens") or 0), "usd": float(cost.get("usd") or 0)},
            "run_id": row.get("run_id"), "control": control}


def run_out(run: dict[str, Any]) -> dict[str, Any]:
    """A run as the app reads it (types.ts `Run`). The node's own status words are the app's."""
    cost = run.get("cost") or {}
    proof = run.get("proof")
    steps = run.get("steps")
    out: dict[str, Any] = {
        "id": run["id"],
        "flow": run.get("flow") or None,
        "lane": run.get("lane") or "",
        "status": run.get("status"),
        "summary": run.get("summary") or "",
        "principal": principal_out(run.get("principal")),
        "started_at": run.get("started_at") or run.get("created_at"),
        "finished_at": run.get("finished_at"),
        "cost": {"tokens": int(cost.get("tokens") or 0), "usd": float(cost.get("usd") or 0)},
        "proof": {"url": proof.get("url"), "id": proof.get("id"), "screenshot": proof.get("screenshot")} if proof else None,
        "approval_id": run.get("approval_id"),
        # Set while a run stopped halfway and waits for a person to say whether the step went through.
        "decision_needed": run.get("decision_needed") or None,
    }
    if steps is not None:
        out["steps"] = [{"id": f"{run['id']}:{index}", "at": step.get("at") or "", "tool": (step.get("summary") or "").split(" ")[0],
                         "summary": step.get("summary") or "", "status": _step_status(step.get("status"))}
                        for index, step in enumerate(steps)]
    return out


def _step_status(value: Any) -> str:
    return {"ok": "succeeded_unverified", "error": "failed"}.get(str(value), str(value))


# ---- the action log ----

def _limit(raw: str | None, default: int) -> int:
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise InvalidRequest("limit is a number.") from None
    return max(1, min(value, 200))


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        raise InvalidRequest("The request body must be JSON.") from None
    if not isinstance(body, dict):
        raise InvalidRequest("The request body must be an object.")
    return body


async def _traces(request: Request, *, lane: str | None = None, workspace: str | None = None,
                  before: str | None = None, limit: int) -> list[dict[str, Any]]:
    """The newest rows of the action log, from `before` on: one row more than `limit`, so the caller knows
    whether there is a next page. The node pages by trace id."""
    query = {"lane": lane, "workspace": workspace, "before": before, "limit": min(limit + 1, PAGE_ROWS)}
    return await read(request, "/api/activity", query=query)


def _page(items: list[dict[str, Any]], rows: list[dict[str, Any]], limit: int, cursor: Any) -> dict[str, Any]:
    more = len(rows) > limit
    return {"items": items, "next_before": cursor(rows[limit - 1]) if more and rows else None}


def _matches(row: dict[str, Any], q: Any) -> bool:
    """The Activity filters the node does not take: who, what effect, what status, and a time window."""
    at = str(row.get("at") or "")
    return (
        (not q.get("principal") or row.get("principal") == q["principal"])
        and (not q.get("effect") or row.get("effect") == q["effect"])
        and (not q.get("status") or TRACE_STATUS.get(row.get("status")) == q["status"])
        and (not q.get("from") or at >= q["from"])
        and (not q.get("to") or at < q["to"])
    )


async def _names(request: Request) -> dict[str, str]:
    reply = await call(request, "GET", "/api/members")  # an admin's list: anyone else reads handles as they are
    members = reply.json() if reply.status == 200 else []
    return {m["id"]: m.get("display_name") or m.get("handle") or m["id"] for m in members if m.get("id")}


def _actor(row: dict[str, Any], members: dict[str, str]) -> str:
    principal = row.get("principal") or ""
    if row.get("principal_kind") == "human":
        return members.get(principal, principal or "A person")
    return telling.agent_name(principal) if principal else "The computer"


# ---- History ----

class _Ctx:
    def __init__(self, zone: ZoneInfo, workspaces: dict[str, str], flows: dict[str, str], approvals: dict[str, dict[str, Any]], people: dict[str, str],
                 routines: dict[str, str] | None = None):
        self.zone = zone
        self.workspaces = workspaces
        self.flows = flows
        self.approvals = approvals
        self.people = people
        self.routines = routines or {}  # a routine's id to its name, for a story to name it
        self.environments: dict[str, str] = {}  # an environment's id to its title

    def day(self, moment: datetime) -> date:
        return moment.astimezone(self.zone).date()


def _when(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    return datetime.fromisoformat(str(stamp))


def _action(effect: str | None, words: str, site_key: str) -> str:
    verb = VERBS.get(words.rsplit(".", 1)[-1])
    if verb == POST and site_key in PUBLISH_SITES:
        return PUBLISH
    if verb is not None:
        return verb
    lowered = words.lower()
    if "reply" in lowered or "comment" in lowered:
        return REPLY
    if effect == "commit":
        return PUBLISH if site_key in PUBLISH_SITES else POST
    if effect == "draft" or "draft" in lowered:
        return DRAFT
    if effect == "pay":
        return PAY
    if effect in ("irreversible", "service", "install_overlay", "install_base", "secret"):
        return CHANGE
    if effect == "exec":
        return TASK
    return READ


def _what(action: str, site_key: str) -> str:
    if action == POST:
        return "a photo" if site_key in PHOTO_SITES else "an update"
    if action == PUBLISH:
        return "an article"
    return ""


def _sentence(action: str, verdict: str, place: str, what: str) -> str:
    base, doing = PHRASES[action]
    if verdict == FAILED:
        return "Could not " + base.format(place=place)
    if verdict == NOT_RUN:
        return "Did not " + base.format(place=place)
    if verdict == RUNNING:
        said = doing.format(place=place)
        return said[:1].upper() + said[1:]
    if action in DONE:
        return DONE[action].format(place=place)
    done = {POST: f"Posted {what} on {place}" if what else f"Posted on {place}",
            PUBLISH: f"Published {what} on {place}" if what else f"Published on {place}",
            REPLY: f"Replied on {place}", DRAFT: f"Saved a draft on {place}", READ: f"Checked {place}",
            PAY: f"Made a payment from {place}", TASK: f"Ran a task on {place}"}
    return done.get(action, f"Made a change on {place}")


def _verdict_text(verdict: str, action: str, site_label: str, reason: str | None) -> str:
    where = site_label or "the site"
    if verdict == CHECKED:
        return "It is live, and we checked."
    if verdict == NOT_CHECKED:
        return f"It ran, but we could not see it on {where} afterwards."
    if verdict == PARTIAL:
        return "Only part of it went out."
    if verdict == FAILED:
        return f"{NOTHING[action]} {reason or telling.SOMETHING_WENT_WRONG}"
    if verdict == NOT_RUN:
        return f"{NOTHING[action]} {reason or 'It was stopped before it ran.'}"
    return "Working on it now."


def _who(principal: dict[str, Any] | str, ctx: _Ctx, site_label: str, flow: str | None) -> dict[str, Any]:
    kind = principal["kind"] if isinstance(principal, dict) else "agent"
    pid = principal["id"] if isinstance(principal, dict) else (principal or "")
    if kind == "human":
        return {"kind": kind, "id": pid, "name": ctx.people.get(pid, "A person"), "brand": None}
    if kind == "flow":  # a routine's own schedule: told by the routine's name, never by its id
        name = ctx.routines.get(flow or "") or (f"A routine on {site_label}" if site_label else "A routine")
        return {"kind": kind, "id": pid, "name": name, "brand": None}
    return {"kind": kind, "id": pid, "name": telling.agent_name(pid), "brand": telling.agent_brand(pid)}


def _decider(value: str | None, people: dict[str, str]) -> str:
    """Who decided, in words: the owner, a member by name, or the handle the decision was made under."""
    if not value:
        return "someone"
    if value.startswith("owner"):
        return "the owner"
    tail = value.split(":", 1)[-1]
    return people.get(tail, tail)


def _started(source: str | None) -> str | None:
    """How a run began, in words: by a person's hand, on a schedule, when something came in, or from a chat."""
    if not source:
        return None
    if source in BY_HAND:
        return TRIGGERS["panel"]
    if source.startswith(("cron", "watch", "schedule")):
        return "On its schedule"
    if source.startswith("route:"):
        return "When something came in"
    return TRIGGERS.get(source)


def _started_by(principal: dict[str, Any] | str | None) -> str | None:
    """How a row began when its source says nothing (a call): a person's hand, or an agent. The same words as
    `_started`, so a row and its detail agree on who started it."""
    if not isinstance(principal, dict):
        return None
    if principal.get("kind") == "human":
        return "Started by hand"
    return "Asked by an agent" if principal.get("kind") == "agent" else None


def _why(source: str | None, approval_id: str | None, ctx: _Ctx, principal: dict[str, Any] | str | None = None) -> dict[str, Any]:
    decided = ctx.approvals.get(approval_id or "")
    approval = None
    text = _started(source) or _started_by(principal)
    # One rule for the row and its detail: a person's hand (the principal) is not "on its own".
    by_hand = isinstance(principal, dict) and principal.get("kind") == "human"
    if decided is not None and decided.get("decided_at"):
        approval = {"by": decided.get("decided_by"), "at": decided["decided_at"], "state": decided.get("state"),
                    "method": decided.get("decided_method"), "level": decided.get("decided_level"),
                    "where": (decided.get("decided_detail") or {}).get("client")}
        if decided.get("state") in ("approved", "consumed"):
            text = f"Approved by {_decider(decided.get('decided_by'), ctx.people)}"
    # Only a run nobody started with their hand and nobody had to say yes to ran on its own.
    return {"trigger": source, "text": text, "approval": approval, "on_its_own": approval_id is None and not by_hand}


def _run_story(run: dict[str, Any], ctx: _Ctx, effects: dict[str, str]) -> dict[str, Any]:
    lane = run.get("lane") or ""
    site_key, site_label = telling.site_of(lane)
    place = telling.place_of(lane, ctx.workspaces)
    flow = run.get("flow")
    effect = effects.get(flow or "")
    action = _action(effect, flow or "", site_key)
    verdict = VERDICT_OF.get(run["status"], NOT_CHECKED)
    detail = telling.error_text(run.get("error")) if verdict in (FAILED, NOT_RUN, PARTIAL) else None
    reason = telling.plain_failure(detail) if verdict == FAILED else (telling.named_failure(detail) if verdict == NOT_RUN else None)
    proof = run.get("proof") or {}
    at = run.get("finished_at") or run.get("started_at") or run.get("created_at")
    return {
        "id": run["id"], "source": "run", "at": at, "started_at": run.get("started_at") or run.get("created_at"),
        "lane": lane, "workspace": lane.split("/", 1)[0] if lane else None,
        "workspace_name": ctx.workspaces.get(lane.split("/", 1)[0] if lane else "", None),
        "place": place, "site": {"key": site_key, "label": site_label}, "action": TYPE_OF[action],
        "sentence": _sentence(action, verdict, place, _what(action, site_key)), "verdict": verdict,
        "verdict_text": _verdict_text(verdict, action, site_label, reason), "reason": reason, "detail": detail,
        "status": run["status"], "kind": KIND_OF.get(action),
        "proof": {"url": proof.get("url"), "screenshot": None} if proof.get("url") else None,
        "who": _who(principal_out(run.get("principal")), ctx, site_label, flow),
        "why": _why(run.get("source"), run.get("approval_id"), ctx, principal_out(run.get("principal"))),
        "steps": [],
        "technical": {"run_id": run["id"], "flow": flow, "tool": None, "effect": effect, "cost": run.get("cost") or {"tokens": 0, "usd": 0}},
    }


def _args(row: dict[str, Any]) -> dict[str, Any]:
    return row["args"] if isinstance(row.get("args"), dict) else {}


def sent_rules(about: list[dict[str, Any]]) -> list[str]:
    """The commit rules a call's requests matched and that really left: what the commit gate approved and sent,
    or a publish the guard counted. An unknown request, a read and a draft step name no publish."""
    out = []
    for row in about:
        rule = _args(row).get("effect_rule")
        if row.get("kind") not in SENT_KINDS or row.get("status") != "ok" or not isinstance(rule, str):
            continue
        if row.get("kind") == "commit_observed" and _args(row).get("sent") is False:
            continue
        if rule.split(".")[1:2] and rule.split(".")[1] in RULE_ACTIONS:
            out.append(rule)
    return out


def read_back_changed(about: list[dict[str, Any]]) -> bool:
    """The act read its own target back afterwards and saw it change: what happened was checked."""
    return any(row.get("kind") == READ_BACK and _args(row).get("changed") is True for row in about)


def _hand_action(row: dict[str, Any], about: list[dict[str, Any]]) -> str:
    """A browser act in words: the publish a matched commit rule names, else what the hand did."""
    rules = sent_rules(about)
    if rules:
        return RULE_ACTIONS[rules[0].split(".")[1]]
    fixed = HAND_TOOLS.get(row.get("tool") or "")
    if fixed is not None:
        return fixed
    return ACT_ACTIONS.get(str(_args(row).get("action") or ""), CLICK)


def _call_story(row: dict[str, Any], ctx: _Ctx, about: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    about = about or []
    lane = row.get("lane") or ""
    site_key, site_label = telling.site_of(lane)
    place = telling.place_of(lane, ctx.workspaces)
    status = TRACE_STATUS.get(row["status"], "failed")
    verdict = {"failed": FAILED, "cancelled": NOT_RUN}.get(status, NOT_CHECKED)
    hand = (row.get("tool") or "") in HAND_TOOLS
    if verdict == NOT_CHECKED and hand and read_back_changed(about):
        verdict = CHECKED
    detail = (row.get("summary") or row.get("error")) if verdict != NOT_CHECKED and verdict != CHECKED else None
    reason = telling.plain_failure(detail) if verdict == FAILED else None
    action = _hand_action(row, about) if hand else _action(row.get("effect"), row.get("tool") or "", site_key)
    principal = principal_out({"kind": row.get("principal_kind"), "id": row.get("principal")})
    sentence = _sentence(action, verdict, place, "" if hand else _what(action, site_key))
    verdict_text = _verdict_text(verdict, action, site_label, reason)
    if verdict == NOT_RUN:  # a call agentos refused before it ran: say that, not "Did not ..."
        sentence = "Stopped before " + PHRASES[action][1].format(place=place)
        verdict_text = f"agentos refused this call before it ran, so nothing happened. {NOTHING[action]}"
    elif verdict == CHECKED and hand:
        verdict_text = "It happened, and we checked: the page showed the change afterwards."
    return {
        "id": row["id"], "source": "call", "at": row["at"], "started_at": row["at"], "lane": lane,
        "workspace": row.get("workspace"), "workspace_name": ctx.workspaces.get(row.get("workspace") or "", row.get("workspace")),
        "place": place, "site": {"key": site_key, "label": site_label}, "action": TYPE_OF[action],
        "sentence": sentence, "verdict": verdict,
        "verdict_text": verdict_text, "reason": reason, "detail": detail,
        "status": status, "kind": KIND_OF.get(action), "proof": None,
        "who": _who(principal, ctx, site_label, None), "why": _why(None, None, ctx, principal), "steps": [],
        "technical": {"run_id": row.get("run_id"), "flow": None, "tool": row.get("tool") or None,
                      "effect": row.get("effect") or None, "cost": row.get("cost") or {"tokens": 0, "usd": 0}},
    }


# What a call in an environment did, in a person's words: the tool's family, then the step's own label.
ENV_KINDS = (("command", ("terminal_exec", "terminal_job_start", "terminal")), ("browser", ("browser_", "ui_", "browser_view")),
             ("files", ("files_",)), ("git", ("git_", "environment_push")))
ENV_STEP = {"command": "Ran a command in the terminal", "browser": "Used the browser",
            "files": "Moved files", "git": "Worked with git", "other": "Took a step"}
ENV_COUNTS = {"command": ("ran {n} command", "ran {n} commands"), "browser": ("used the browser once", "used the browser {n} times"),
              "files": ("moved files once", "moved files {n} times"), "git": ("worked with git once", "worked with git {n} times"),
              "other": ("took {n} other step", "took {n} other steps")}
SESSION_GAP = timedelta(minutes=30)  # calls further apart than this are another session in the same environment


def _env_kind(tool: str) -> str:
    for kind, prefixes in ENV_KINDS:
        if tool.startswith(prefixes):
            return kind
    return "other"


def _joined(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def environment_stories(rows: list[dict[str, Any]], ctx: _Ctx) -> list[dict[str, Any]]:
    """An agent's calls in its own environment, one story per session (same environment, same agent, no
    gap over SESSION_GAP): "Worked in qa-env: ran 9 commands and used the browser twice". There is no
    account to check, so a session is done, never "not checked"; the raw commands are technical details."""
    sessions: list[list[dict[str, Any]]] = []
    last: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in sorted(rows, key=lambda r: r["at"]):
        key = (str(envs_web.environment_id(row["lane"])), str(row.get("principal") or ""))
        open_one = last.get(key)
        if open_one is None or _when(row["at"]) - _when(open_one[-1]["at"]) > SESSION_GAP:
            open_one = last[key] = []
            sessions.append(open_one)
        open_one.append(row)
    return [_environment_story(session, ctx) for session in sessions]


def _environment_story(rows: list[dict[str, Any]], ctx: _Ctx) -> dict[str, Any]:
    env_id = str(envs_web.environment_id(rows[0]["lane"]))
    title = ctx.environments.get(env_id, "an environment")
    newest = rows[-1]
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[_env_kind(str(row.get("tool") or ""))] += 1
    order = ["command", "browser", "files", "git", "other"]
    parts = [ENV_COUNTS[k][0 if counts[k] == 1 else 1].format(n=counts[k]) for k in order if counts[k]]
    errors = sum(TRACE_STATUS.get(row["status"]) == "failed" for row in rows)
    refused = sum(row["status"] == "denied" for row in rows)
    text = "It ran on the agent's own computer, so there is no account to check."
    if errors:
        text += f" {errors} of the {len(rows)} steps ended with an error, which is normal while coding."
    if refused:
        text += f" agentos refused {refused} {'step' if refused == 1 else 'steps'}."
    principal = principal_out({"kind": newest.get("principal_kind"), "id": newest.get("principal")})
    commands = [str(row.get("summary") or row.get("tool") or "") for row in rows]
    cost = {"tokens": sum(int((row.get("cost") or {}).get("tokens") or 0) for row in rows),
            "usd": round(sum(float((row.get("cost") or {}).get("usd") or 0) for row in rows), 6)}
    lane = f"{envs_web.WORKSPACE}/{env_id}"
    return {
        "id": newest["id"], "source": "call", "at": newest["at"], "started_at": rows[0]["at"], "lane": lane,
        "workspace": envs_web.WORKSPACE, "workspace_name": None, "environment": {"id": env_id, "title": title},
        "place": title, "site": {"key": "environment", "label": title}, "action": TASK,
        "sentence": f"Worked in {title}: {_joined(parts)}", "verdict": DONE_HERE, "verdict_text": text,
        "reason": None, "detail": "\n".join(commands)[:4000], "status": "succeeded_unverified", "kind": KIND_OF.get(TASK),
        "proof": None, "who": _who(principal, ctx, title, None), "why": _why(None, None, ctx, principal),
        "steps": [{"id": row["id"], "at": row["at"], "label": ENV_STEP[_env_kind(str(row.get("tool") or ""))],
                   "status": TRACE_STATUS.get(row["status"], "failed")} for row in rows],
        "technical": {"run_id": None, "flow": None, "tool": ", ".join(sorted({str(r.get("tool")) for r in rows})),
                      "effect": newest.get("effect") or None, "cost": cost},
    }


def _is_quiet_read(story: dict[str, Any]) -> bool:
    return story["action"] == READ and story["verdict"] in (CHECKED, NOT_CHECKED)


def counts_as_done(story: dict[str, Any]) -> bool:
    """What "done" means, the one rule Home's "Done today" and History's day lines both use: agents' and
    routines' work that went through. A person's own action is theirs, not done for them; a failure, or a
    part that went out, is a problem; and a look at a site is a check (its own count), not a thing done."""
    if story["verdict"] not in (CHECKED, NOT_CHECKED, DONE_HERE) or story["action"] == READ:
        return False
    return (story.get("who") or {}).get("kind") != "human"


def _reads_line(stories: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_lane: dict[str, dict[str, Any]] = {}
    for story in stories:
        entry = by_lane.setdefault(story["lane"], {"lane": story["lane"], "place": story["place"], "site": story["site"], "times": 0, "last_at": story["at"]})
        entry["times"] += 1
        entry["last_at"] = max(entry["last_at"], story["at"])
    if not by_lane:
        return None
    times = sum(entry["times"] for entry in by_lane.values())
    accounts = len(by_lane)
    noun = "account" if accounts == 1 else "accounts"
    ordered = sorted(by_lane.values(), key=lambda entry: (-entry["times"], entry["lane"]))
    return {"times": times, "accounts": accounts, "sentence": f"Also checked {accounts} {noun} {times} {'time' if times == 1 else 'times'}.", "by_account": ordered}


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')}"


def _group_days(stories: list[dict[str, Any]], ctx: _Ctx, today: date) -> list[dict[str, Any]]:
    days: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for story in stories:
        days[ctx.day(_when(story["at"]))].append(story)
    out = []
    for day in sorted(days, reverse=True):
        items = sorted(days[day], key=lambda story: story["at"], reverse=True)
        quiet = [story for story in items if _is_quiet_read(story)]
        shown = [story for story in items if not _is_quiet_read(story)]
        done = [story for story in items if counts_as_done(story)]
        checked = sum(story["verdict"] == CHECKED for story in done)
        problems = sum(story["verdict"] in PROBLEMS for story in items)
        payments = sum(story["action"] == PAY and story["verdict"] != FAILED for story in items)
        summary = f"{len(done)} {'thing' if len(done) == 1 else 'things'} done · {checked} checked"
        if payments:
            summary += f" · {payments} {'payment' if payments == 1 else 'payments'}"
        if problems:
            summary += f" · {problems} {'problem' if problems == 1 else 'problems'}"
        out.append({"date": day.isoformat(), "label": _day_label(day, today), "summary": summary, "done": len(done),
                    "checked": checked, "problems": problems, "payments": payments, "stories": shown,
                    "reads": _reads_line(quiet)})
    return out


async def stories_between(request: Request, api: Api, start: datetime, end: datetime,
                          workspace: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], _Ctx]:
    """The stories that finished from `start` to `end`, newest first; the runs still going, and the
    context they were told in. Every read runs as the caller, so the node's own scope applies."""
    now = datetime.now(UTC)
    zone = ZoneInfo(api.timezone)
    names = {row["id"]: row.get("name") or row["id"] for row in await read(request, "/api/workspaces")}
    listed = await read(request, "/api/flows")
    flows = {flow["id"]: flow.get("effect") or "" for flow in listed}
    # Every routine by its name: a recording's title, and a routine built in the app by its own name.
    routines = {flow["id"]: flow["title"] for flow in listed if flow.get("title")}
    routines.update({row["id"]: row["name"] for row in await read(request, "/api/workflows") if row.get("name")})
    decided = [item for state in ("approved", "denied") for item in await read(request, "/api/approvals", query={"state": state, "limit": 200})]
    approvals = {item["id"]: item for item in decided if item.get("kind") != "question"}
    ctx = _Ctx(zone, names, flows, approvals, await _names(request), routines)
    ctx.environments = envs_web.titles(api)

    runs = await read(request, "/api/runs", query={"limit": 500})
    stories: list[dict[str, Any]] = []
    working: list[dict[str, Any]] = []
    for run in runs:
        if workspace and (run.get("lane") or "").split("/", 1)[0] != workspace:
            continue
        if run["status"] == "running" and (_when(run.get("started_at")) or now) >= now - timedelta(hours=6):
            story = _run_story(run, ctx, flows)
            working.append({**story, "headline": f"{story['who']['name']} is {story['sentence'][:1].lower()}{story['sentence'][1:]}"})
        finished = _when(run.get("finished_at") or run.get("started_at"))
        if run["status"] in UNFINISHED or finished is None or not (start <= finished < end):
            continue
        stories.append(_run_story(run, ctx, flows))

    calls = await _calls(request, start, workspace)
    about: dict[str, list[dict[str, Any]]] = defaultdict(list)  # a call's own rows, by the call's start
    for row in calls:
        if row.get("parent_id") and row["kind"] != STORY_KIND:
            about[row["parent_id"]].append(row)
    in_environments: list[dict[str, Any]] = []
    for row in calls:
        if row["kind"] != STORY_KIND or row["status"] in ("started", "pending") or row.get("run_id") \
                or not row.get("lane") or not row.get("tool"):
            continue
        if not (start <= _when(row["at"]) < end):
            continue
        if envs_web.environment_id(row["lane"]) in ctx.environments:
            in_environments.append(row)
            continue
        stories.append(_call_story(row, ctx, about.get(row.get("parent_id") or "", [])))
    stories += environment_stories(in_environments, ctx)

    stories.sort(key=lambda story: story["at"], reverse=True)
    return stories, working, ctx


async def _history(request: Request, api: Api) -> dict[str, Any]:
    q = request.query_params
    workspace = q.get("workspace") or None
    show = q.get("show") or "everything"
    if show not in SHOWS:
        raise InvalidRequest(f"show is one of {', '.join(SHOWS)}.")
    try:
        days = max(1, min(int(q.get("days") or 7), 31))
    except ValueError:
        raise InvalidRequest("days is a number.") from None
    zone = ZoneInfo(api.timezone)
    now = datetime.now(UTC)
    today = now.astimezone(zone).date()
    try:
        before = date.fromisoformat(q["before"]) if q.get("before") else None
    except ValueError:
        raise InvalidRequest("before is a day, like 2026-11-03.") from None
    last_day = before - timedelta(days=1) if before else today
    first_day = last_day - timedelta(days=days - 1)
    retention = int(getattr(getattr(api.svc("traces"), "store", None), "retention_days", 0) or 0)
    oldest = today - timedelta(days=retention) if retention else first_day
    first_day = max(first_day, oldest)
    start = datetime.combine(first_day, time(0), tzinfo=zone).astimezone(UTC)
    end = min(datetime.combine(last_day + timedelta(days=1), time(0), tzinfo=zone).astimezone(UTC), now + timedelta(seconds=1))

    stories, working, ctx = await stories_between(request, api, start, end, workspace)
    counts = {
        "everything": len(stories),
        "problems": sum(story["verdict"] in PROBLEMS for story in stories),
        "posts": sum(story["action"] in (POST, PUBLISH, REPLY) for story in stories),
        "spending": sum(story["action"] == PAY for story in stories),
    }
    shown = stories
    if show == "problems":
        shown = [story for story in stories if story["verdict"] in PROBLEMS]
    elif show == "posts":
        shown = [story for story in stories if story["action"] in (POST, PUBLISH, REPLY)]
    elif show == "spending":
        shown = [story for story in stories if story["action"] == PAY]
    return {
        "timezone": api.timezone,
        "counts": counts,
        "working_now": sorted(working, key=lambda story: story["at"], reverse=True) if before is None else [],
        "days": _group_days(shown, ctx, today),
        "next_before": first_day.isoformat() if first_day > oldest else None,
        "retention_days": retention,
    }


def _instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidRequest("start and end are times, like 2026-11-01T00:00:00Z.") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def _window(request: Request, start: datetime, workspace: str | None, *, limit: int, lane: str | None = None,
                  too_long: str = "That is more than the export can hold. Export a shorter time.") -> list[dict[str, Any]]:
    """Every row of the action log from `start` on, newest first. Refused past `limit`: a long export is
    answered with a reason, never with a file that quietly stops."""
    rows: list[dict[str, Any]] = []
    before = None
    while True:
        page = await read(request, "/api/activity", query={"workspace": workspace, "lane": lane, "before": before, "limit": PAGE_ROWS})
        rows += page
        if len(rows) > limit:
            raise InvalidRequest(too_long)
        if len(page) < PAGE_ROWS or not page or _when(page[-1]["at"]) < start:
            return rows
        before = page[-1]["id"]


async def _calls(request: Request, start: datetime, workspace: str | None) -> list[dict[str, Any]]:
    """The action log back to `start`, newest first: pages of it, stopping once a page is older."""
    rows: list[dict[str, Any]] = []
    before = None
    for _ in range(MAX_PAGES):
        page = await read(request, "/api/activity", query={"workspace": workspace, "before": before, "limit": PAGE_ROWS})
        rows += page
        if len(page) < PAGE_ROWS or not page:
            break
        before = page[-1]["id"]
        if _when(page[-1]["at"]) < start:
            break
    return rows


@dataclass
class _Chart:
    """The Activity histogram's buckets over one window, and what is counted into them."""
    start: datetime
    end: datetime
    size: int
    lane: str | None
    workspace: str | None
    buckets: list[dict[str, Any]]

    @classmethod
    def over(cls, span: str, lane: str | None, workspace: str | None) -> _Chart:
        size, count = CHART_WINDOWS[span]
        stamp = int(datetime.now(UTC).timestamp())
        end_at = stamp - stamp % size + size  # the bucket that holds now is the last one
        start = datetime.fromtimestamp(end_at - size * count, UTC)
        buckets = [{"at": datetime.fromtimestamp(start.timestamp() + index * size, UTC).isoformat()
                    .replace("+00:00", "Z"), "events": 0,
                    "runs": {"succeeded_verified": 0, "succeeded_unverified": 0, "partial": 0, "failed": 0},
                    "approvals": {"requested": 0, "approved": 0, "denied": 0}, "cost": {"tokens": 0, "usd": 0.0}}
                   for index in range(count)]
        return cls(start, datetime.fromtimestamp(end_at, UTC), size, lane, workspace, buckets)

    def bucket(self, moment: datetime | None) -> dict[str, Any] | None:
        """The bucket that holds `moment`, or None outside the window."""
        if moment is None or not (self.start <= moment < self.end):
            return None
        return self.buckets[int((moment - self.start).total_seconds() // self.size)]

    def outside(self, lane: str | None, workspace: str | None = None) -> bool:
        """Whether a row of this lane (and workspace) is outside the lane or workspace charted."""
        if self.lane and lane != self.lane:
            return True
        return bool(self.workspace and (workspace or (lane or "").split("/", 1)[0]) != self.workspace)

    def add_action(self, row: dict[str, Any]) -> None:
        bucket = self.bucket(_when(row["at"]))
        if bucket is None or row.get("kind") == "tool_start":  # one call is one action, not its start and end
            return
        bucket["events"] += 1
        cost = row.get("cost") or {}
        bucket["cost"]["tokens"] += int(cost.get("tokens") or 0)
        bucket["cost"]["usd"] = round(bucket["cost"]["usd"] + float(cost.get("usd") or 0), 6)

    def add_run(self, run: dict[str, Any]) -> None:
        if self.outside(run.get("lane")) or run["status"] not in CHART_RUNS:
            return
        bucket = self.bucket(_when(run.get("finished_at")))
        if bucket is not None:
            bucket["runs"][run["status"]] += 1

    def add_approval(self, item: dict[str, Any]) -> None:
        if item.get("kind") == "question" or self.outside(item.get("lane"), item.get("workspace")):
            return
        asked = self.bucket(_when(item.get("created_at")))
        if asked is not None:
            asked["approvals"]["requested"] += 1
        decided = self.bucket(_when(item.get("decided_at")))
        if decided is not None and item.get("state") in ("approved", "denied"):
            decided["approvals"]["approved" if item["state"] == "approved" else "denied"] += 1

    def totals(self) -> dict[str, Any]:
        b = self.buckets
        return {
            "events": sum(x["events"] for x in b),
            "runs": sum(sum(x["runs"].values()) for x in b),
            "approvals": sum(x["approvals"]["requested"] for x in b),
            "cost": {"tokens": sum(x["cost"]["tokens"] for x in b), "usd": round(sum(x["cost"]["usd"] for x in b), 6)},
        }


async def _activity_chart(request: Request, api: Api) -> dict[str, Any]:
    """The Activity histogram: per bucket, the actions logged, the runs that finished (by what they proved),
    the approvals asked, decided yes and no, and what the model cost. Built from the same facts as Activity."""
    q = request.query_params
    span = q.get("range") or "24h"
    if span not in CHART_WINDOWS:
        raise InvalidRequest(f"range is one of {', '.join(CHART_WINDOWS)}.")
    lane = q.get("lane") or None
    workspace = (lane.split("/", 1)[0] if lane else None) or q.get("workspace") or None
    chart = _Chart.over(span, lane, workspace)
    rows = await _window(request, chart.start, workspace, lane=lane, limit=MAX_CHART_ROWS,
                         too_long="There are too many actions to chart in this range. Try a shorter one.")
    for row in rows:
        chart.add_action(row)
    listed = await read(request, "/api/runs", query={"limit": 500})  # newest first, and the node has no older page
    oldest = _when(listed[-1].get("created_at")) if len(listed) >= 500 else None
    if oldest is not None and oldest >= chart.start:
        raise InvalidRequest("There are too many runs to chart in this range. Try a shorter one.")
    for run in listed:
        chart.add_run(run)
    # the four states are four reads of the node: at once, not one after another
    states = ("pending", "approved", "denied", "expired")
    pages = await asyncio.gather(*(read(request, "/api/approvals", query={"state": state, "limit": 500})
                                   for state in states))
    for page in pages:
        for item in page:
            chart.add_approval(item)
    return {"range": span, "bucket_seconds": chart.size, "buckets": chart.buckets, "totals": chart.totals()}


CHART_RUNS = {"succeeded_verified", "succeeded_unverified", "partial", "failed"}
