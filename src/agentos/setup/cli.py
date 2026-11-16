"""`agentos setup`: the guided setup of agentos on a laptop (local mode).

A person installs the CLI (scripts/install.sh ends by starting this) and answers a few questions:
where VMs come from (Hetzner, or Docker here), their GitHub login, the limits, then the local
service starts and Claude Code and Codex are connected. Quick start takes good defaults; step by
step asks each setting. Every step can be skipped, and `agentos setup <step>` runs one again to
change one thing. `--yes` answers every question with its default, for scripts.

The steps are in agentos.setup.steps; this module is the command, the order, and the
prompts. Nothing here ever prints a token.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import click
import typer

STEPS = ("vms", "github", "limits", "agents", "ssh", "check", "test-vm")
STEP_TITLES = {"vms": "VMs", "github": "GitHub", "limits": "Limits", "agents": "Your agents",
               "ssh": "SSH from your shell", "check": "Health check", "test-vm": "A test VM"}


@dataclass
class SetupOptions:
    yes: bool = False
    home: Path | None = None
    hetzner_token_file: Path | None = None
    docker: bool = False
    github_token_file: Path | None = None


class Ui:
    """Short lines and prompts. With `yes`, every question takes its default and nothing is read."""

    def __init__(self, yes: bool = False):
        self.yes = yes

    def say(self, text: str = "", **style) -> None:
        if style:
            typer.secho(text, **style)
        else:
            typer.echo(text)

    def head(self, number: int | None, total: int, title: str) -> None:
        self.say()
        self.say(f"Step {number} of {total}: {title}" if number else title, bold=True)

    def choose(self, question: str, options: list[tuple[str, str]], default: str) -> str:
        keys = [k for k, _ in options]
        if self.yes:
            return default
        self.say(question)
        for i, (_key, label) in enumerate(options, 1):
            self.say(f"  {i}  {label}")
        while True:
            raw = typer.prompt("Choose", default=str(keys.index(default) + 1)).strip().lower()
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return keys[int(raw) - 1]
            if raw in keys:
                return raw
            self.say(f"Type a number from 1 to {len(options)}.")

    def confirm(self, question: str, default: bool) -> bool:
        return default if self.yes else typer.confirm(question, default=default)

    def number(self, question: str, default: float, *, whole: bool = False) -> float:
        if self.yes:
            return default
        kind = int if whole else float
        shown = int(default) if whole or float(default).is_integer() else default
        return kind(typer.prompt(question, default=shown, type=kind))

    def secret(self, question: str) -> str:
        """A hidden answer ("" when there is none, or in --yes mode)."""
        if self.yes:
            return ""
        return typer.prompt(question, hide_input=True, default="", show_default=False).strip()


@dataclass
class Ctx:
    """What one run knows: where, how it asks, and what each step found (for the summary)."""
    home: Path
    ui: Ui
    quick: bool
    opts: SetupOptions
    vms: str = ""
    github: str = ""
    github_login: str = ""
    limits: str = ""
    keep: str = ""
    service_url: str = ""
    ssh: str = ""
    agents: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)


def resolve_home(given: Path | None) -> Path:
    """--home, else $AGENTOS_HOME, else the local default (~/.agentos)."""
    if given is not None:
        return Path(given).expanduser()
    if os.environ.get("AGENTOS_HOME"):
        return Path(os.environ["AGENTOS_HOME"]).expanduser()
    from agentos.setup import steps as setup_steps

    return setup_steps.local_home()


WELCOME = (
    "agentos gives each of your agents its own computer in the cloud: a terminal, a real browser and your repo.",
    "A small service on this computer serves your agents. It rents a VM only while an agent works in one.",
    "What it costs: nothing until a VM opens. A Hetzner cx23 is about EUR 0.011 an hour.",
    "Your tokens stay on this computer. A VM never sees them.",
    "Every step can be skipped. Run `agentos setup` again any time to change something.",
)


def welcome(ui: Ui) -> None:
    ui.say("Welcome to agentos", bold=True)
    for line in WELCOME:
        ui.say(line)


def run_all(ctx: Ctx) -> None:
    from agentos.setup import steps as s

    order = [("vms", s.vms), ("github", s.github), ("limits", s.limits), ("agents", s.agents), ("ssh", s.ssh),
             ("check", s.check), ("test-vm", s.test_vm)]
    for i, (name, step) in enumerate(order, 1):
        ctx.ui.head(i, len(order), STEP_TITLES[name])
        step(ctx)
    s.summary(ctx)


def run_one(ctx: Ctx, step: str) -> None:
    from agentos.setup import steps as s

    ctx.ui.head(None, 0, STEP_TITLES[step])
    {"vms": s.vms, "github": s.github, "limits": s.limits, "agents": s.agents, "ssh": s.ssh, "check": s.check,
     "test-vm": s.test_vm}[step](ctx)
    if step in ("vms", "limits"):
        s.restart_if_running(ctx)


def setup(
    step: Annotated[str | None, typer.Argument(
        metavar="[STEP]", help="Run one step again: vms, github, limits, agents, ssh, check or test-vm.")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Take every default and ask nothing (for scripts).")]
    = False,
    hetzner_token_file: Annotated[Path | None, typer.Option(
        "--hetzner-token-file", help="Read the Hetzner Cloud API token from this file (- for stdin).")] = None,
    docker: Annotated[bool, typer.Option("--docker", help="Use Docker on this computer for VMs (free).")] = False,
    github_token_file: Annotated[Path | None, typer.Option(
        "--github-token-file", help="Read a GitHub token from this file (- for stdin).")] = None,
    home: Annotated[Path | None, typer.Option(help="Where agentos keeps its files (default: ~/.agentos).")] = None,
) -> None:
    """Set up agentos on this computer: VMs, GitHub, limits, and your agents.

    Quick start takes good defaults; step by step asks each setting. To change one thing, run its step again, like `agentos setup github`."""
    if step is not None and step not in STEPS:
        raise typer.BadParameter(f"{step!r} is not a step; the steps are {', '.join(STEPS)}", param_hint="STEP")
    opts = SetupOptions(yes=yes, home=home, hetzner_token_file=hetzner_token_file, docker=docker,
                        github_token_file=github_token_file)
    where = resolve_home(home)
    where.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ["AGENTOS_HOME"] = str(where)  # every command this runs (vm, vault, environments) uses it
    ui = Ui(yes=yes)
    try:
        if step is not None:
            run_one(Ctx(where, ui, quick=False, opts=opts), step)
        else:
            welcome(ui)
            mode = ui.choose("\nHow do you want to set it up?",
                             [("quick", "Quick start: good defaults, a few questions"),
                              ("steps", "Step by step: choose each setting")], "quick")
            run_all(Ctx(where, ui, quick=mode == "quick", opts=opts))
    except (click.exceptions.Abort, typer.Abort, EOFError):
        ui.say("\nSetup stopped. What was saved stays saved. Run `agentos setup` again any time "
               "(in a script: agentos setup --yes).")
        raise typer.Exit(1) from None
