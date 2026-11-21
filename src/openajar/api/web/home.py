"""Home's summary on a node: `GET /api/orgs/local/home`, the shape `HomeSummary` below (web/src/features/home/summary.ts).

One sentence, what needs you, whether everything else is fine, what was done today by kind and how much of it
was checked on the real site, the next routines, and AI usage today against a usual day. Days are the computer's
own.

The hosted cloud builds this from its copy of the history. A node builds
it from reads it really has: today's stories (`history.stories_between`), its status and workspaces, its schedules
and routines, and the action log for spend. Every read runs as the caller, so a member sees what the node lets
them see. A node cannot know about another computer's outage, so only a computer it can see down is named here.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from typing import Any, NotRequired, TypedDict
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.routing import BaseRoute

from openajar.api.app import Api
from openajar.api.web import history, telling
from openajar.api.web.accounts import checks_of
from openajar.api.web.base import org_route, read
from openajar.api.web.environments import without_environments

COMING_UP = 3
USUAL_DAYS = 14
# Far enough from usual to say so, and far enough to put it in Needs you.
LESS, MORE, UNUSUAL = 0.67, 1.5, 3.0
# Something that went out into the world: what "Today, in pictures" shows first.
WENT_OUT = frozenset({history.POST, history.PUBLISH, history.REPLY, history.SEND, history.PAY, history.REFUND,
                      history.ORDER, history.SUBMIT, history.DELETE})
_KINDS = (
    ("posts", (history.POST, history.PUBLISH), "post published", "posts published"),
    ("emails", (history.SEND,), "email sent", "emails sent"),
    ("replies", (history.REPLY,), "reply sent", "replies sent"),
    ("inboxes", (history.SORT,), "inbox sorted", "inboxes sorted"),
    ("drafts", (history.DRAFT,), "draft written", "drafts written"),
    ("downloads", (history.DOWNLOAD,), "download for the books", "downloads for the books"),
    ("payments", (history.PAY,), "payment", "payments"),
    ("orders", (history.ORDER,), "order placed", "orders placed"),
    ("refunds", (history.REFUND,), "refund", "refunds"),
    ("forms", (history.SUBMIT,), "form submitted", "forms submitted"),
    ("prices", (history.WATCH,), "price check", "price checks"),
    ("deletions", (history.DELETE,), "clean-up", "clean-ups"),
    ("changes", (history.CHANGE,), "change", "changes"),
    ("tasks", (history.TASK,), "task run", "tasks run"),
    ("checks", (history.READ,), "check", "checks"),
)
# A lane's state, as the app's Home words it: what the node really knows of an account.
SIGNAL_STATES = {"captcha": "captcha", "checkpoint": "checkpoint", "logged_out": "logged_out",
                 "blocked": "blocked", "rate_limited": "rate_limited"}
# `waiting`: a person can fix it from the panel (sign in, solve the check). `stuck`: it will not work until the
# technical side looks or the site lets go. Anything else is all good, asleep or working.
STUCK = frozenset({"quarantined", "blocked", "rate_limited", "frozen", "not_safe"})
WAITING = frozenset({"captcha", "checkpoint", "logged_out"})
REASONS = {
    "quarantined": "The account is switched off for safety.",
    "blocked": "The site is refusing the account for now.",
    "rate_limited": "The site asked the account to slow down.",
    "frozen": "The account is frozen.",
    "not_safe": "The account is not safe to use right now.",
}
MAX_PAGES = 20  # of the action log, 500 rows each, for the spend of two weeks


class Sentence(TypedDict):
    lead: str
    rest: str


class Problem(TypedDict):
    kind: str
    lead: str
    lane: NotRequired[str]
    node: NotRequired[str]
    state: NotRequired[str]
    rest: NotRequired[str]


class Today(TypedDict):
    done: int
    on_its_own: int
    went_out_on_its_own: int
    checked: int
    not_checked: int
    by_kind: dict[str, int]
    chips: list[str]
    working_now: int
    line: str
    pictures: list[dict[str, Any]]


class Coming(TypedDict):
    schedule_id: str
    flow: str
    lane: str | None
    workspace_name: str | None
    place: str | None
    title: str
    at: str
    when: str
    sentence: str


class AiUsage(TypedDict):
    today_usd: float
    usual_usd: float | None
    ratio: float | None
    label: str
    unusual: bool


class HomeSummary(TypedDict):
    timezone: str
    date: str
    needs_you: int
    all_fine: bool
    sentence: Sentence
    problems: list[Problem]
    today: Today
    coming_up: list[Coming]
    ai_usage: AiUsage | None


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _words(count: int) -> str:
    """Small numbers read better as words at the start of a sentence."""
    return {1: "One", 2: "Two", 3: "Three"}.get(count, str(count))


def _at(stamp: str | None) -> datetime | None:
    return datetime.fromisoformat(str(stamp)) if stamp else None


def _midnight(day: Any, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)


def _when(moment: datetime, zone: ZoneInfo, now: datetime) -> str:
    local, today = moment.astimezone(zone), now.astimezone(zone).date()
    clock = local.strftime("%H:%M")
    if local.date() == today:
        return f"Today at {clock}"
    if local.date() == today + timedelta(days=1):
        return f"Tomorrow at {clock}"
    return f"{local.strftime('%A')} at {clock}"


def on_its_own(story: dict[str, Any]) -> bool:
    """Done without anyone being asked: a routine that runs on its own, or a look."""
    return bool((story.get("why") or {}).get("on_its_own", True))


def today_strip(stories: list[dict[str, Any]], working: int, workspaces: int) -> Today:
    done = [story for story in stories if history.counts_as_done(story)]
    alone = [story for story in done if on_its_own(story)]
    checked = sum(story["verdict"] == history.CHECKED for story in done)
    actions = Counter(story["action"] for story in done)
    by_kind = {key: sum(actions[action] for action in members) for key, members, _, _ in _KINDS}
    chips = [_plural(by_kind[key], one, many) for key, _, one, many in _KINDS if by_kind[key]]
    if done:
        line = (
            f"Your agents did {_plural(len(done), 'thing', 'things')} across "
            f"{_plural(workspaces, 'workspace', 'workspaces')} today, and {checked} "
            f"{'was' if checked == 1 else 'were'} checked on the real site afterwards."
        )
    else:
        line = "Nothing has finished here today."
    if working:
        line += f" {_words(working)} {'is' if working == 1 else 'are'} working right now."
    return {
        "done": len(done),
        "on_its_own": len(alone),
        "went_out_on_its_own": sum(story["action"] in WENT_OUT for story in alone),
        "checked": checked,
        "not_checked": len(done) - checked,
        "by_kind": by_kind,
        "chips": chips,
        "working_now": working,
        "line": line,
        "pictures": [],  # the node keeps no proof picture for a run, so there is none to show
    }


def checked_line(today: Today) -> str:
    done, checked, working = today["done"], today["checked"], today["working_now"]
    line = (
        f"{_words(checked) if checked else 'None'} of {'it' if done == 1 else 'them'} "
        f"{'was' if checked == 1 else 'were'} checked on the real site afterwards."
        if done > 1 or checked
        else "It was not checked on the real site afterwards."
    )
    if checked == done and done > 1:
        line = f"All {done} were checked on the real site afterwards."
    if working:
        line += f" {_words(working)} {'is' if working == 1 else 'are'} working right now."
    return line


def hero_sentence(needs_you: int, counted: dict[str, int], problems: list[Problem], alone: int) -> tuple[str, str]:
    """Automation first: when nothing urgent waits (a computer offline, an account that will not work, a yes
    that did not happen), Home leads with what got done on its own, and the questions follow. A problem
    is never hidden behind "Everything else is fine" or "All good"."""
    urgent = bool(problems) or counted.get("failures", 0) > 0 or counted.get("accounts", 0) > 0
    asks = f"{_words(needs_you)} {'needs' if needs_you == 1 else 'need'} you." if needs_you else "Nothing needs you."
    if alone and not urgent:
        return f"Your agents did {_plural(alone, 'thing', 'things')} on their own today.", asks
    if needs_you:
        lead = f"{_words(needs_you)} {'thing needs' if needs_you == 1 else 'things need'} you."
        return lead, problems[0]["lead"] if problems else "Everything else is fine."
    if problems:
        return problems[0]["lead"], problems[0].get("rest") or "Nothing needs you."
    return "All good.", "Nothing needs you."


def not_safe(lane: dict[str, Any]) -> bool:
    """The account's own last check failed: its profile is gone or its sign-in is broken. The same rule as the
    account page and Accounts (web lib/states.ts): a warning is not a stop."""
    check = lane.get("check") or {}
    return check.get("safe") is False or check.get("level") == "fail"


def account_state(lane: dict[str, Any]) -> str:
    """Worst first, from what the node reports of a lane. A node cannot see a computer's own outage here, so
    `offline` is never claimed for an account, and the rule is the cloud's (fleet/accounts.py)."""
    if lane.get("frozen") is True:
        return "frozen"
    if lane.get("state") == "quarantined":
        return "quarantined"
    if not_safe(lane):
        return "not_safe"
    if lane.get("signal") in SIGNAL_STATES:
        return SIGNAL_STATES[lane["signal"]]
    if lane.get("logged_in_hint") == "needs_login":
        return "logged_out"
    if lane.get("state") == "active":
        return "working"
    if lane.get("state") == "cold":
        return "asleep"
    return "ok"


def face_of(state: str) -> str:
    return "stuck" if state in STUCK else "waiting" if state in WAITING else "all_good"


def stuck_accounts(workspaces: list[dict[str, Any]]) -> list[Problem]:
    """Accounts that will not work until the technical side looks or the site lets go."""
    names = {row["id"]: row.get("name") or row["id"] for row in workspaces}
    problems: list[Problem] = []
    for workspace in workspaces:
        for lane in workspace.get("lanes") or []:
            state = account_state(lane)
            if state not in STUCK:
                continue
            reason = REASONS[state].removeprefix("The account ").rstrip(".")
            place = telling.place_of(lane["id"], names)
            problems.append({"kind": "account_stuck", "lane": lane["id"], "state": state, "lead": f"{place} {reason}."})
    return problems


def with_checks(workspaces: list[dict[str, Any]], checks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Each lane with its own last check, `check` as the account page reads it (level and safe), or None."""
    return [{**workspace, "lanes": [{**lane, "check": checks.get(lane["id"])} for lane in workspace.get("lanes") or []]}
            for workspace in workspaces]


def offline_computers(status: dict[str, Any], workspaces: list[dict[str, Any]]) -> list[Problem]:
    """Computers the node knows are down. Its own is up, since it is answering."""
    problems: list[Problem] = []
    names = {row["id"]: row.get("name") or row["id"] for row in workspaces}
    for node in status.get("nodes") or []:
        if node.get("state") != "down":
            continue
        affected = sorted({names.get(lane["workspace"], lane["workspace"]) for workspace in workspaces
                           for lane in workspace.get("lanes") or [] if lane.get("node") == node["id"]})
        if not affected:
            continue
        joined = " and ".join(affected) if len(affected) <= 2 else f"{', '.join(affected[:-1])} and {affected[-1]}"
        problems.append({"kind": "computer_offline", "node": node["id"], "lead": f"{node['id']} is offline.",
                         "rest": f"{joined}'s accounts are paused until it is back."})
    return problems


async def coming_up(request: Request, workspaces: list[dict[str, Any]], zone: ZoneInfo, now: datetime) -> list[Coming]:
    """The next routines on a schedule, soonest first. A schedule that is switched off is not coming up."""
    names = {row["id"]: row.get("name") or row["id"] for row in workspaces}
    routines = {row["schedule_id"]: row for row in await read(request, "/api/workflows") if row.get("schedule_id")}
    upcoming = []
    for row in await read(request, "/api/schedules"):
        due = _at(row.get("next_run"))
        routine = routines.get(row["id"])
        if due is None or due <= now or row.get("enabled") is False or (routine is None and not row.get("flow")):
            continue
        flow = routine["id"] if routine else row["flow"]
        # A routine's lane is on the routine: `ws/*` means every account of that business, which names no place.
        lane = (routine or {}).get("lane") or row.get("lane")
        workspace = (lane or "").split("/", 1)[0] or None
        place = telling.place_of(lane, names) if lane and not lane.endswith("/*") else None
        title = routine["name"] if routine else flow
        when = _when(due, zone, now)
        name = names.get(workspace, workspace) if workspace else None
        upcoming.append((due, {
            "schedule_id": row["id"], "flow": flow, "lane": lane, "workspace_name": name, "place": place,
            "title": title, "at": row["next_run"], "when": when,
            "sentence": f"{name}: {title} · {when}" if name else f"{title} · {when}",
        }))
    upcoming.sort(key=lambda item: item[0])
    return [item for _, item in upcoming[:COMING_UP]]


def usage_words(today: float, usual: float | None) -> AiUsage:
    if usual is None or usual <= 0:
        label = "No usual day to compare with yet." if today else "No AI usage yet today."
        return {"today_usd": round(today, 2), "usual_usd": None, "ratio": None, "label": label, "unusual": False}
    ratio = today / usual
    if ratio < LESS:
        words = "less than a usual day"
    elif ratio <= MORE:
        words = "about the same as a usual day"
    else:
        words = f"{ratio:.0f} times a usual day" if ratio >= 2 else "more than a usual day"
    return {
        "today_usd": round(today, 2),
        "usual_usd": round(usual, 2),
        "ratio": round(ratio, 2),
        "label": f"AI usage today: ${today:.2f}, {words}.",
        "unusual": ratio >= UNUSUAL,
    }


async def spend(request: Request, zone: ZoneInfo, now: datetime) -> AiUsage | None:
    """What the model cost so far today, against the same hours on the last two weeks. None when the node
    cannot read back far enough to give a usual day honestly."""
    midnight = _midnight(now.astimezone(zone).date(), zone)
    since = midnight - timedelta(days=USUAL_DAYS)
    elapsed = now - midnight
    by_day: dict[int, float] = defaultdict(float)
    today = 0.0
    reached = False
    before = None
    for _ in range(MAX_PAGES):
        page = await read(request, "/api/activity", query={"before": before, "limit": 500})
        for row in page:
            if row["kind"] == "tool_start":  # one call is one action, not its start and end
                continue
            at = _at(row["at"])
            if at is None:
                continue
            usd = float((row.get("cost") or {}).get("usd") or 0)
            if at >= midnight:
                today += usd
                continue
            if at < since:
                continue
            back = (midnight - at).days + (1 if (midnight - at) % timedelta(days=1) else 0)
            if at - (midnight - timedelta(days=back)) <= elapsed:
                by_day[back] += usd
        if len(page) < 500 or not page:
            reached = True
            break
        before = page[-1]["id"]
        if _at(page[-1]["at"]) < since:
            reached = True
            break
    days = [by_day[back] for back in range(1, USUAL_DAYS + 1) if by_day.get(back)]
    usual = statistics.median(days) if reached and len(days) >= 3 else None
    return usage_words(today, usual)


def routes(api: Api) -> list[BaseRoute]:
    async def home(request: Request) -> HomeSummary:
        zone = ZoneInfo(api.timezone)
        now = datetime.now(UTC)
        today = now.astimezone(zone).date()
        stories, working, _ = await history.stories_between(request, api, _midnight(today, zone),
                                                            now + timedelta(seconds=1), None)
        status = await read(request, "/api/status")
        # an environment's lanes are the agent's own computer, on Environments, not accounts here
        workspaces = with_checks(without_environments(api, await read(request, "/api/workspaces")),
                                 await checks_of(request))
        running = len({story["lane"] for story in working})
        active = sum(lane.get("state") == "active" for workspace in workspaces for lane in workspace.get("lanes") or [])
        strip = today_strip(stories, max(running, active), len(workspaces))
        offline = offline_computers(status, workspaces)
        stuck = stuck_accounts(workspaces)
        problems = offline + stuck
        failures = sum(story["verdict"] == history.FAILED and (story["why"].get("approval") or {}).get("state") in ("approved", "consumed")
                       for story in stories)
        lead, rest = hero_sentence(status.get("needs_you_all", status["needs_you"]), {"failures": failures, "accounts": len(stuck)}, problems,
                                   strip["on_its_own"])
        if lead.startswith("Your agents did"):
            # The sentence already says how much got done, so the line under it says how sure we are.
            strip["line"] = checked_line(strip)
        owner = bool((status.get("principal") or {}).get("owner"))
        return {
            "timezone": api.timezone,
            "date": today.isoformat(),
            "needs_you": status.get("needs_you_all", status["needs_you"]),
            "all_fine": not problems,
            "sentence": {"lead": lead, "rest": rest},
            "problems": problems,
            "today": strip,
            "coming_up": await coming_up(request, workspaces, zone, now),
            # Spend is the owner's to see, the same as the node's own status.
            "ai_usage": await spend(request, zone, now) if owner else None,
        }

    return [org_route(api, "/home", home, ["GET"])]
