"""The engagement engine, brief E1 (docs/design/engagement-engine.md): programs, personas, the
planner and the governor, as a dry run."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from itertools import combinations, count, pairwise
from zoneinfo import ZoneInfo

import pytest

from agentos.core.errors import NotFound
from agentos.core.models import Principal
from agentos.engage import governor
from agentos.engage.caps import (
    CAPS,
    FOLKLORE_AGED_LOW,
    FOLKLORE_AGED_LOW_WEEK,
    WARM_UP_STAGES,
    Kind,
    clean_weeks,
    ramped,
)
from agentos.engage.errors import CapCeiling, OwnerOnly, ProgramInvalid
from agentos.engage.governor import AuditFlag, audit, budget, hourly_series, lagged_correlation
from agentos.engage.models import DayPlan, Program, Session
from agentos.engage.persona import draw_persona
from agentos.engage.planner import MIN_GAP, Account, plan_days
from agentos.engage.service import EngageService, parse_program
from agentos.ops import migrate

OWNER = {"kind": "human", "id": "owner", "owner": "owner"}
AGENT = Principal(kind="agent", id="agent-1")
START = date(2026, 11, 5)
VILNIUS = ZoneInfo("Europe/Vilnius")

IG_YAML = """
program: biz-a-ig-coffee
surface: biz-a/instagram
timezone: Europe/Vilnius
phases:
  - {kind: warm_up, days: 10}
  - kind: steady
    daily: {like: 15, comment: 3, follow: 2, story_view: 10}
    targets:
      - {source: hashtags, values: ["#specialtycoffee", latteart]}
      - {source: own_feed}
    filters: {language: [lt, en], min_account_age_days: 90}
comments: {mode: approve, topics: [coffee], max_chars: 140}
"""


def program(surface: str = "biz-a/instagram", *, daily: dict | None = None, warm: int | None = 10,
            risk: str = "careful", tz: str = "Europe/Vilnius", site: str | None = None) -> Program:
    phases: list[dict] = [{"kind": "warm_up", "days": warm}] if warm else []
    phases.append({"kind": "steady", "daily": daily or {"like": 15, "comment": 3, "follow": 2, "story_view": 10}})
    data = {"program": surface.replace("/", "-"), "surface": surface, "risk": risk, "timezone": tz, "phases": phases}
    if site:
        data["site"] = site
    return Program.model_validate(data)


def account(prog: Program, seed: str, **kw) -> Account:
    return Account(prog, draw_persona(prog.surface, seed), ZoneInfo(prog.timezone or "UTC"), kw.pop("starts_on", START),
                   **kw)


def all_sessions(plans: list[DayPlan]) -> list[Session]:
    return [s for p in plans for s in p.sessions]


# ---------------------------------------------------------------- programs


def test_a_program_hashes_canonically_whatever_the_text_looks_like():
    a = parse_program(IG_YAML)
    as_json = json.dumps(a.canonical())
    b = parse_program(as_json)
    assert a.content_hash() == b.content_hash() and a.content_hash().startswith("sha256:")
    assert a.site == "instagram" and a.steady.targets[0].values == ["specialtycoffee", "latteart"]
    assert "politics" in a.steady.filters.exclude_topics      # the sensitive list is a floor
    changed = parse_program(IG_YAML.replace("like: 15", "like: 16"))
    assert changed.content_hash() != a.content_hash()


@pytest.mark.parametrize(("text", "needle"), [
    ("program: x1\nsurface: biz-a/reddit\nphases: [{kind: steady, daily: {like: 3}}]", "votes"),
    ("program: x1\nsurface: biz-a/linkedin\ncomments: {mode: auto}\nphases: [{kind: steady}]", "approve mode"),
    ("program: x1\nsurface: biz-a/linkedin\ncommunity: {reply_to_comments: {mode: auto}}\nphases: [{kind: steady}]",
     "replies on linkedin stay in approve mode"),
    ("program: x1\nsurface: biz-a/reddit\ncommunity: {reply_to_comments: {mode: auto}}\nphases: [{kind: steady}]",
     "replies on reddit stay in approve mode"),
    ("program: x1\nsurface: biz-a/instagram\nphases: [{kind: steady}, {kind: warm_up}]", "end with"),
    ("program: x1\nsurface: biz-a/myspace\nphases: [{kind: steady}]", "site"),
    ("program: x1\nsurface: biz-a/instagram\ntimezone: Mars/Base\nphases: [{kind: steady}]", "time zone"),
    ("like 15 posts a day please", "YAML"),
])
def test_invalid_programs_say_what_to_fix(text, needle):
    with pytest.raises(ProgramInvalid, match=needle):
        parse_program(text)


def test_versions_count_up_and_the_same_content_is_the_same_version(tmp_path):
    svc = EngageService(home=tmp_path)
    one = svc.draft("biz-a/instagram", IG_YAML, by=AGENT)
    again = svc.draft("biz-a/instagram", IG_YAML, by=AGENT)
    two = svc.draft("biz-a/instagram", IG_YAML.replace("like: 15", "like: 12"), by=AGENT)
    assert (one["version"], again["version"], two["version"]) == (1, 1, 2)
    assert not again["created"] and one["hash"] == again["hash"] != two["hash"]
    assert [v.created_by["kind"] for v in svc.versions("biz-a-ig-coffee")] == ["agent", "agent"]
    assert one["caps_day_1"]["like"] == 0 and one["caps_first_steady_day"]["like"] == 10
    with pytest.raises(ProgramInvalid, match="drafted for"):
        svc.draft("biz-b/instagram", IG_YAML, by=AGENT)


# ---------------------------------------------------------------- owner only


def test_an_agent_cannot_approve_a_program_or_raise_a_cap(tmp_path):
    svc = EngageService(home=tmp_path)
    svc.draft("biz-a/instagram", IG_YAML, by=AGENT)
    with pytest.raises(OwnerOnly):
        svc.approve("biz-a-ig-coffee", by=AGENT)
    with pytest.raises(OwnerOnly):
        svc.approve("biz-a-ig-coffee", by={"kind": "human", "id": "va-1", "owner": "owner"})  # another human
    with pytest.raises(OwnerOnly):
        svc.set_cap("biz-a/instagram", "like", 20, by=AGENT)
    with pytest.raises(OwnerOnly):
        svc.set_cap("biz-a/instagram", "like", None, by=AGENT)
    for owner_only in ("risk: normal\n", "comments: {mode: auto}\n"):
        with pytest.raises(OwnerOnly):
            svc.draft("biz-a/instagram", IG_YAML.replace("comments: {mode: approve, topics: [coffee], "
                                                         "max_chars: 140}\n", "") + owner_only, by=AGENT)
    with pytest.raises(OwnerOnly, match="followers_of"):
        svc.draft("biz-a/instagram", IG_YAML.replace("{source: own_feed}", "{source: followers_of, values: [rival]}"),
                  by=AGENT)
    assert svc.store.active("biz-a/instagram") is None and svc.store.caps("biz-a/instagram") == {}

    fv = svc.approve("biz-a-ig-coffee", by=OWNER)
    assert fv.state.value == "approved" and fv.starts_on is not None
    with pytest.raises(CapCeiling):
        svc.set_cap("biz-a/instagram", "like", 81, by=OWNER)        # above the hard ceiling
    with pytest.raises(CapCeiling):
        svc.set_cap("biz-a/instagram", "join", 1, by=OWNER)         # instagram has no join
    assert svc.set_cap("biz-a/instagram", "like", 80, by=OWNER)["hard_ceiling"] == 80
    assert svc.store.caps("biz-a/instagram") == {Kind.LIKE: 80}


def test_the_mcp_tools_only_draft_and_read(tmp_path):
    import asyncio

    from agentos.core.runtime import Runtime
    from agentos.engage import tools

    svc = EngageService(home=tmp_path)
    runtime = Runtime(engage=svc)
    seen = []

    class Ctx:
        principal = AGENT

        def __init__(self):
            self.runtime = runtime

        async def authorize(self, effect, **kw):
            seen.append((effect.value, kw.get("lane_id")))

        def can_access_lane(self, lane_id):
            return lane_id.startswith("biz-a/")

    specs = {s.name: s for s in tools.tool_specs(runtime)}
    assert set(specs) == {"engage_program_draft", "engage_status", "engage_item_submit", "engage_items",
                          "engage_item_withdraw", "engage_digest"}
    assert not any("approve" in name or "cap" in name for name in specs)
    out = asyncio.run(tools.engage_program_draft(Ctx(), surface="biz-a/instagram", program=IG_YAML))
    assert out["state"] == "draft" and "approve" in out["next"]
    svc.draft("biz-b/instagram", IG_YAML.replace("biz-a", "biz-b"), by=OWNER)
    status = asyncio.run(tools.engage_status(Ctx()))
    assert [s["surface"] for s in status["surfaces"]] == ["biz-a/instagram"] and status["dry_run"]
    assert seen == [("draft", "biz-a/instagram"), ("read", None)]


# ---------------------------------------------------------------- caps and ramp


def test_the_caps_tables_stay_well_under_the_folklore():
    for (site, kind), low in FOLKLORE_AGED_LOW.items():
        assert CAPS[site][kind].normal <= low / 2
    for (site, kind), low in FOLKLORE_AGED_LOW_WEEK.items():
        assert CAPS[site][kind].week_normal <= low * 0.65          # "about half"
    for rows in CAPS.values():
        for row in rows.values():
            assert row.start <= row.careful <= row.normal


def test_the_ramp_adds_15_percent_a_week_only_after_clean_weeks():
    assert [ramped(10, 40, w) for w in range(0, 12, 2)] == [10, 13, 17, 23, 30, 40]
    assert ramped(10, 40, 30) == 40 and ramped(10, 40, 4, halvings=1) == 8
    s = date(2026, 1, 5)
    assert clean_weeks(s, s + timedelta(days=6)) == 0
    assert clean_weeks(s, s + timedelta(days=21)) == 3
    # a warning in week 1 drops that week and freezes the next two
    assert clean_weeks(s, s + timedelta(days=35), [s + timedelta(days=9)]) == 2


def test_warm_up_stages_hold_the_design_limits():
    prog = program(warm=14)
    for day in range(1, 15):
        b = budget(prog, START, START + timedelta(days=day - 1))
        stage = next(s for s in reversed(WARM_UP_STAGES) if day >= s.first_day)
        assert b.phase == "warm_up" and b.stage == stage
        for kind, cap in b.caps.items():
            assert cap <= stage.limits.get(kind, 0)
        if day <= 3:
            assert not any(b.caps.get(k) for k in (Kind.LIKE, Kind.FOLLOW, Kind.COMMENT))
    assert budget(prog, START, START + timedelta(days=14)).phase == "steady"


SIM_PROGRAMS = [
    program("biz-a/instagram", daily={"like": 200, "comment": 50, "follow": 50, "story_view": 200, "post": 5}),
    program("biz-a/instagram-two", site="instagram", risk="normal", warm=None,
            daily={"like": 200, "comment": 50, "follow": 50, "story_view": 200, "post": 5}),
    program("biz-a/linkedin", daily={"like": 40, "comment": 9, "follow": 20, "post": 4}, tz="America/New_York"),
    program("biz-a/reddit", warm=None, daily={"comment": 10, "join": 5, "post": 3}, tz="UTC"),
    program("biz-a/facebook", daily={"like": 80, "comment": 10}),
]


def test_caps_are_never_exceeded_over_365_simulated_days():
    accounts = [account(p, f"seed-{i}") for i, p in enumerate(SIM_PROGRAMS)]
    accounts[0].overrides = {Kind.LIKE: 60}        # an owner raise, still under the hard ceiling
    plans = plan_days(accounts, START, 365)
    for a in accounts:
        site, risk = a.program.site, a.program.risk
        week_totals: dict[date, dict[Kind, int]] = {}
        for p in plans[a.surface]:
            totals = p.totals()
            for kind, n in totals.items():
                row = CAPS[site][kind]
                assert n <= p.caps[kind] <= row.normal, (a.surface, p.day, kind)
                if a.overrides.get(kind) is None:
                    assert p.caps[kind] <= row.ceiling(risk)
                week = p.day - timedelta(days=p.day.weekday())
                week_totals.setdefault(week, {}).setdefault(kind, 0)
                week_totals[week][kind] += n
            for s in p.sessions:              # no hour holds more than the hourly cap
                for kind, n in s.actions.items():
                    assert n <= p.hourly[kind]
            for i, j in combinations(range(len(p.sessions)), 2):
                si, sj = p.sessions[i], p.sessions[j]
                if sj.start - si.end < timedelta(hours=1):
                    for kind in set(si.actions) | set(sj.actions):
                        between = sum(x.actions.get(kind, 0) for x in p.sessions[i:j + 1])
                        assert between <= p.hourly[kind]
        for week, totals in week_totals.items():
            for kind, n in totals.items():
                if CAPS[site][kind].week_ceiling(risk) is not None:
                    assert n <= CAPS[site][kind].week_ceiling(risk), (a.surface, week)
    # the ramp really ramps: a careful account reaches its ceiling within the year
    last = plans["biz-a/facebook"][-1]
    assert last.caps[Kind.LIKE] == CAPS["facebook"][Kind.LIKE].careful
    assert plans["biz-a/instagram"][-1].caps[Kind.LIKE] == 60


def test_sessions_follow_the_timing_rules():
    accounts = [account(program(f"biz-a/instagram-{i}", site="instagram"), f"t{i}") for i in range(3)]
    plans = plan_days(accounts, START, 120)
    for a in accounts:
        prev = None
        for p in plans[a.surface]:
            starts = [s.start for s in p.sessions]
            assert starts == sorted(starts)
            for x, y in pairwise(starts):
                assert y - x >= MIN_GAP
            for s in p.sessions:
                assert s.start.minute % 5 and s.start.date() == p.day and 3 <= s.minutes <= 30
                acts = sum(n for k, n in s.actions.items() if k in (Kind.LIKE, Kind.COMMENT, Kind.FOLLOW))
                assert acts <= 0.25 * s.seen + 1e-9          # most posts seen get no action
            if p.phase == "steady" and not p.quiet:
                assert 1 <= len(p.sessions) <= 4
            if prev is not None:
                for s in p.sessions:
                    for y in prev.sessions:
                        mins = abs((s.start.hour * 60 + s.start.minute) - (y.start.hour * 60 + y.start.minute))
                        assert mins >= 9                    # never yesterday's time give or take 10 minutes
            prev = p
    counts = [len(p.sessions) for a in accounts for p in plans[a.surface] if p.phase == "steady" and not p.quiet]
    assert sum(1 for c in counts if 2 <= c <= 4) / len(counts) > 0.95


def test_seeded_runs_are_deterministic():
    def run(seed: str):
        return [p.model_dump_json() for p in plan_days([account(program(), seed)], START, 60)["biz-a/instagram"]]

    assert run("alpha") == run("alpha")
    assert run("alpha") != run("beta")
    assert draw_persona("biz-a/instagram", "alpha") == draw_persona("biz-a/instagram", "alpha")


def test_the_service_plans_the_same_days_from_the_same_persona(tmp_path):
    a, b = EngageService(home=tmp_path / "a"), EngageService(home=tmp_path / "b")
    for svc in (a, b):
        svc.store.put_persona(draw_persona("biz-a/instagram", "same-seed"))
        svc.draft("biz-a/instagram", IG_YAML, by=OWNER)
    one = [p.model_dump_json() for p in a.plan("biz-a/instagram", days=14, start=START)]
    two = [p.model_dump_json() for p in b.plan("biz-a/instagram", days=14, start=START)]
    assert one == two and len(one) == 14
    assert a.store.plans("biz-a/instagram", START, START + timedelta(days=14)) == []   # a dry run saves nothing


def test_no_two_accounts_of_one_company_start_within_20_minutes():
    zones = ["Europe/Vilnius", "Europe/Vilnius", "Europe/London", "UTC", "Europe/Vilnius", "Europe/Berlin"]
    progs = [program(f"biz-a/instagram-{i}", site="instagram", tz=z) for i, z in enumerate(zones)]
    progs.append(program("biz-b/instagram", tz="Europe/Vilnius"))
    accounts = [account(p, f"c{i}") for i, p in enumerate(progs)]
    plans = plan_days(accounts, START, 90)
    company = [s.start for a in accounts if a.program.workspace == "biz-a" for s in all_sessions(plans[a.surface])]
    assert len(company) > 6 * 90
    company.sort()
    for x, y in pairwise(company):
        assert y - x >= governor.COMPANY_GAP


def test_quiet_days_come_at_the_persona_rate_and_never_three_in_a_row():
    prog = program(warm=None)
    seen, expected = 0, 0.0
    for i in range(24):
        a = account(prog, f"q{i}")
        days = plan_days([a], START, 365)["biz-a/instagram"]
        quiet = [p.quiet for p in days]
        assert "TTT" not in "".join("T" if q else "F" for q in quiet)
        rate = sum(quiet) / len(quiet)
        assert abs(rate - a.persona.quiet_p) < 0.06, (a.persona.quiet_p, rate)
        seen += sum(quiet)
        expected += a.persona.quiet_p * len(quiet)
        assert all(not p.sessions for p in days if p.quiet)
    assert abs(seen - expected) / expected < 0.1
    assert 0.08 * 365 * 24 * 0.85 < seen < 0.2 * 365 * 24


def test_the_persona_drifts_slowly():
    p = draw_persona("biz-a/instagram", "drift")
    weeks = [p.for_day(START + timedelta(days=7 * w)) for w in range(52)]
    for x, y in pairwise(weeks):
        assert abs(x.wake_h - y.wake_h) * 60 < 20
        assert abs(x.liker - y.liker) / x.liker < 0.1
    assert len({round(w.wake_h, 3) for w in weeks}) > 10        # it does move
    assert all(6.0 <= w.wake_h <= 9.5 and 0.125 <= w.liker <= 0.25 for w in weeks)


# ---------------------------------------------------------------- correlation audit


def _shifted(plans: list[DayPlan], hours: int, surface: str) -> list[DayPlan]:
    out = []
    for p in plans:
        q = p.model_copy(deep=True)
        q.surface = surface
        for s in q.sessions:
            s.start += timedelta(hours=hours)
        out.append(q)
    return out


def test_the_audit_flags_a_pair_in_sync_and_not_independent_accounts():
    progs = [program(f"biz-a/instagram-{i}", site="instagram", warm=None) for i in range(8)]
    accounts = [account(p, f"a{i}") for i, p in enumerate(progs)]
    plans = plan_days(accounts, START, 14)
    since = datetime.combine(START, datetime.min.time(), tzinfo=UTC) - timedelta(hours=3)
    series = {s: hourly_series(ps, since) for s, ps in plans.items()}
    assert audit(series) == []                                 # independent personas move independently
    twin = _shifted(plans["biz-a/instagram-0"], 1, "biz-b/instagram")   # the same habits, an hour later
    series["biz-b/instagram"] = hourly_series(twin, since)
    flags = audit(series)
    assert flags == [AuditFlag("biz-a/instagram-0", "biz-b/instagram", flags[0].correlation, 1, False)]
    assert flags[0].correlation > 0.9
    r, lag = lagged_correlation([0, 1, 0, 0, 2, 0, 0, 1], [0, 0, 1, 0, 0, 2, 0, 0])
    assert lag == 1 and r > 0.9


def test_the_nightly_job_saves_plans_and_redraws_a_persona_in_sync(tmp_path, monkeypatch):
    # pinned seeds: about one random persona in six has a quiet first day, with nothing to count or audit
    seeds = count()
    monkeypatch.setattr("agentos.engage.service.secrets.token_hex", lambda n: f"{next(seeds):032x}")
    clock = {"t": datetime(2026, 11, 5, 1, 0, tzinfo=UTC).timestamp()}
    svc = EngageService(home=tmp_path, clock=lambda: clock["t"])
    for ws in ("biz-a", "biz-b"):
        svc.draft(f"{ws}/instagram", IG_YAML.replace("biz-a", ws), by=OWNER)
        svc.approve(f"{ws}-ig-coffee", by=OWNER)
    out = svc.nightly()
    assert sorted(out["drawn"]) == ["biz-a/instagram 2026-11-05", "biz-a/instagram 2026-11-06",
                                    "biz-b/instagram 2026-11-05", "biz-b/instagram 2026-11-06"]
    assert svc.nightly()["drawn"] == []                         # once per day
    assert svc.store.counters("biz-a/instagram", date(2026, 11, 5))   # planned counts, nothing done
    # make biz-b's saved days a copy of biz-a's: the audit must catch it and redraw biz-b
    for p in svc.store.plans("biz-a/instagram", date(2026, 11, 5), date(2026, 11, 6)):
        svc.store.put_plan(_shifted([p], 0, "biz-b/instagram")[0])
    before = svc.store.persona("biz-b/instagram")
    flags = svc.run_audit(datetime(2026, 11, 7, 0, 0, tzinfo=UTC))
    assert [(f["a"], f["b"]) for f in flags] == [("biz-a/instagram", "biz-b/instagram")]
    after = svc.store.persona("biz-b/instagram")
    assert after.redraws == before.redraws + 1 and after != before
    assert svc.store.notices("biz-b/instagram")[0]["kind"] == "in_sync"
    status = svc.status("biz-a/instagram")["surfaces"][0]
    assert status["phase"].startswith("warming up: day 1 of 10") and status["today"]["saved"]
    with pytest.raises(NotFound):
        svc.status("biz-c/instagram")


# ---------------------------------------------------------------- store


def test_engage_db_is_in_the_node_migration_set(tmp_path):
    assert any(s.package == "engage" for s in migrate.discover())
    EngageService(home=tmp_path).store.close()
    [st] = [s for s in migrate.status(tmp_path) if s.package == "engage"]
    assert st.exists and st.current == st.latest == 5 and not st.pending
    assert (tmp_path / "engage" / "engage.db").stat().st_mode & 0o777 == 0o600


def test_the_cli_drafts_approves_and_prints_a_dry_run(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentos.engage.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    (tmp_path / "p.yaml").write_text(IG_YAML)
    run = CliRunner().invoke
    out = run(app, ["program", "draft", "biz-a/instagram", str(tmp_path / "p.yaml")])
    assert out.exit_code == 0 and "version 1" in out.output
    assert run(app, ["plan", "biz-a/instagram"]).exit_code == 2          # only --dry-run in E1
    out = run(app, ["program", "approve", "biz-a-ig-coffee"])
    assert out.exit_code == 0 and "approved" in out.output
    out = run(app, ["plan", "biz-a/instagram", "--dry-run", "--days", "7"])
    assert out.exit_code == 0 and out.output.count("warm-up day") == 7 and "Dry run" in out.output
    out = run(app, ["cap", "biz-a/instagram", "like", "500"])
    assert out.exit_code == 1 and "cap_ceiling" in out.output
    assert "—" not in out.output
