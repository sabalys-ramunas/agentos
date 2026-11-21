"""Files in and out: a file on the person's computer goes up to the machine, is worked on there, and the
result comes back into their folder (environment_upload, then environment_download). The agent runs the
links' commands in the person's own shell: the scripts here, a model through the few commands moving files
needs (host_tools). Merged from the evals/ suite of 2026-11-20, `files-in-out`. The agent leaves the
machine open, so the checker can compare what came back with what the machine made."""

from __future__ import annotations

import csv
import io
import json
import random
from pathlib import Path

from env_eval.tasks import Agent, Check, Ctx, Session, Task, done, host

SEED = 4
CATEGORIES = ["books", "garden", "kitchen", "tools", "toys"]
OUT = "/work/out/summary.json"
# the person's shell, for a model: what the links' commands use, and looking around; anything else is refused
HOST_TOOLS = ("Bash(curl *)", "Bash(tar *)", "Bash(COPYFILE_DISABLE=1 tar *)", "Bash(A=*)", "Bash(git *)",
              "Bash(ls *)", "Bash(cat *)", "Bash(mkdir *)", "Bash(cp *)", "Bash(mv *)", "Bash(cd *)")


def _rows() -> list[tuple[str, float]]:
    rng = random.Random(500 + SEED)
    return [(rng.choice(CATEGORIES), round(rng.uniform(1, 400), 2)) for _ in range(300)]


def _totals() -> dict[str, float]:
    totals: dict[str, float] = {}
    for category, amount in _rows():
        totals[category] = totals.get(category, 0.0) + amount
    return {k: round(v, 2) for k, v in sorted(totals.items())}


def write_sales(folder: Path) -> None:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["category", "amount"])
    writer.writerows(_rows())
    (folder / "sales.csv").write_text(out.getvalue())


def _came_back(ctx: Ctx) -> tuple[str, dict]:
    """The summary.json nearest the top of the person's folder: (its text, parsed)."""
    found = sorted(ctx.local.rglob("summary.json"), key=lambda p: len(p.parts)) if ctx.local else []
    if not found:
        return "", {}
    text = found[0].read_text(errors="replace")
    try:
        return text, json.loads(text)
    except ValueError:
        return text, {}


async def check(ctx: Ctx) -> list[Check]:
    text, data = _came_back(ctx)
    code, made = ctx.on_machine(f"cat {OUT}")
    _, hostname = ctx.on_machine("hostname")
    return [
        ("summary.json came back into the person's folder", bool(text)),
        ("its totals are right", data.get("totals") == _totals() and data.get("rows") == len(_rows())),
        ("it was made on the machine", bool(hostname.strip()) and data.get("made_on") == hostname.strip()),
        ("it is the file the machine made", code == 0 and bool(text) and made == text),
    ]


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    env = await a.open(title="Sales summary", repo=ctx.repo.slug, keep="none")
    up = await a.on_call("environment_upload", dest="/work/in")
    code, out = await host(ctx, up["pack"] + " && " + up["send"])
    if code != 0:
        raise RuntimeError(f"the upload failed in the person's shell: {out[-300:]}")
    await a.sh(f"python3 summarize.py /work/in/sales.csv {OUT}", cwd=env["workdir"])
    down = await a.on_call("environment_download", path=OUT)
    code, out = await host(ctx, down["fetch"] + " && " + down["unpack"] + " && rm -f out.tgz")
    if code != 0:
        raise RuntimeError(f"the download failed in the person's shell: {out[-300:]}")
    return done("summary.json is in your folder; the machine is open.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Runs the download's commands on the machine (terminal_exec) instead of in the person's own shell."""
    env = await a.open(title="Sales summary", repo=ctx.repo.slug, keep="none")
    up = await a.on_call("environment_upload", dest="/work/in")
    await host(ctx, up["pack"] + " && " + up["send"])
    await a.sh(f"python3 summarize.py /work/in/sales.csv {OUT}", cwd=env["workdir"])
    down = await a.on_call("environment_download", path=OUT)
    await a.raw("terminal_exec", {"on": a.on, "cmd": down["fetch"] + " && " + down["unpack"], "cwd": env["workdir"],
                                  "timeout_s": 60})
    return done("summary.json is downloaded.")


TASK = Task(
    id="t13_files_in_out",
    title="a file from the person's computer up to the machine, and the result back into their folder",
    repo="report",
    sessions=[Session("sales.csv in this folder needs summing by category, with the script in {repo}. In openajar, "
                      f"put the file on a machine as /work/in/sales.csv, run `python3 summarize.py "
                      f"/work/in/sales.csv {OUT}` in the repository there, and bring summary.json back into this "
                      "folder. Leave the machine open: I want to look at it.")],
    check=check, perfect=perfect, careless=careless,
    local=write_sales, host_tools=HOST_TOOLS,
)
