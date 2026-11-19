"""Continue tomorrow: one session saves with a note, a second one ("continue <title> in agentos") resumes it.

The second prompt does not say what is left: only the first session's note does. So finishing means
the resume worked (the same machine's work came back, not a new one) and the note was read.
"""

from __future__ import annotations

from env_eval.tasks import Agent, Check, Ctx, Session, Task, done

TITLE = "Todo export"
CSV_OLD = '  throw new Error("toCsv is not written yet");'
CSV_NEW = ("  const cell = (s) => (/[\",\\n]/.test(s) ? `\"${s.replaceAll('\"', '\"\"')}\"` : s);\n"
           "  return \"title,done\\n\" + tasks.map((t) => `${cell(t.title)},${t.done ? \"yes\" : \"no\"}\\n`)"
           ".join(\"\");")
JSON_OLD = '  throw new Error("toJson is not written yet");'
JSON_NEW = ("  const list = tasks.map((t) => ({ title: t.title, done: t.done }));\n"
            "  return JSON.stringify({ count: list.length, done: list.filter((t) => t.done).length, tasks: list },"
            " null, 2);")
NOTE = "The CSV export is done and test/csv.test.js passes. Next: toJson in src/export.js (test/json.test.js)."


async def check(ctx: Ctx) -> list[Check]:
    second = ctx.results("environment_open", 1)
    resumed = [o for o in second if o.get("opened") == "resumed"]
    note = " ".join(str((o.get("resumed") or {}).get("note") or "") for o in resumed).lower()
    new = [o for o in ctx.results("environment_open") if o.get("opened") == "new"]
    passing = [b for b in ctx.new_branches() if ctx.tests_pass(b)[0]]
    pulls = ctx.github.opened(ctx.repo.full_name)
    return [
        ("the second session resumed the saved machine", bool(resumed)),
        ("the first session's note came back, naming the JSON export", "json" in note),
        ("no second machine was opened", len(new) == 1),
        ("every test passes at a pushed branch (CSV from day one, JSON from day two)", bool(passing)),
        ("a pull request was opened for it", any(p["head"]["ref"] in passing for p in pulls)),
        *await ctx.closed_and_gone(),
    ]


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    if session == 0:
        env = await a.open(title=TITLE, repo=ctx.repo.slug, keep="save")
        await a.on_call("file_read", path="src/export.js")
        await a.on_call("file_edit", path="src/export.js", old=CSV_OLD, new=CSV_NEW)
        await a.sh("node --test test/csv.test.js 2>&1 | tail -8", cwd=env["workdir"])
        await a.on_call("environment_close", note=NOTE)
        return done(f'CSV export done; saved. Say "continue {TITLE} in agentos" tomorrow.')
    env = await a.open(title=TITLE)
    note = str((env.get("resumed") or {}).get("note") or "")
    if "toJson" not in note:
        return done(f"the note did not say what is next: {note[:100]}", "blocked")
    await a.on_call("file_edit", path="src/export.js", old=JSON_OLD, new=JSON_NEW)
    await a.sh("npm test 2>&1 | tail -8", cwd=env["workdir"])
    await a.sh("git checkout -q -b todo-export && git add -A && git commit -qm 'Export the task list as CSV and "
               "JSON' && git push -q origin todo-export 2>&1", cwd=env["workdir"])
    pr = await a.via("environment_pull_request", title="Export the task list as CSV and JSON",
                     body="toCsv and toJson, with their tests passing.")
    await a.on_call("environment_close", forget=True)
    return done(f"Resumed, wrote toJson, pushed todo-export and opened {pr['url']}. The machine and its saves are gone.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Day one: single use, so the close throws the work away. Day two: a new machine, the JSON half only."""
    if session == 0:
        env = await a.open(title=TITLE, repo=ctx.repo.slug, keep="none")
        await a.on_call("file_edit", path="src/export.js", old=CSV_OLD, new=CSV_NEW)
        await a.sh("node --test test/csv.test.js 2>&1 | tail -3", cwd=env["workdir"])
        await a.on_call("environment_close", discard=True)
        return done("CSV export done.")
    env = await a.open(title=TITLE, repo=ctx.repo.slug)
    await a.on_call("file_edit", path="src/export.js", old=JSON_OLD, new=JSON_NEW)
    await a.sh("git checkout -q -b todo-export && git commit -qam 'JSON export' && git push -q origin todo-export "
               "2>&1", cwd=env["workdir"])
    pr = await a.via("environment_pull_request", title="JSON export")
    await a.on_call("environment_close")
    return done(f"Opened {pr['url']}.")


TASK = Task(
    id="t03_continue_tomorrow",
    title="save with a note, resume it the next day by its title, finish",
    repo="todo",
    sessions=[
        Session(f'In agentos, start the export in {{repo}} and call the environment "{TITLE}": make '
                "test/csv.test.js pass. I have to stop after that, so keep the machine for tomorrow and leave a "
                "note that the JSON export (test/json.test.js) is next."),
        Session(f"continue {TITLE} in agentos. When it is done, push it on a branch and open the pull request."),
    ],
    check=check, perfect=perfect, careless=careless,
)
