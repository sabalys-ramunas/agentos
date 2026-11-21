"""The dashboard's Environments: each agent's cloud computer by its title, with its state, repository and
branch, cost, time left, git (ahead or pushed) and the proofs it kept. Open ones first."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import DataTable, Static

from openajar.tui import fmt
from openajar.tui.models import Environment
from openajar.tui.state import DashState
from openajar.tui.views import _cursor_key, _restore_cursor
from openajar.tui.widgets import View, resource_note

ENV_STATE = {"starting": ("starting", fmt.WARN), "working": ("working", fmt.INFO), "idle": ("idle", fmt.OK),
             "ready": ("ready", fmt.OK), "closing": ("closing", fmt.MUTED), "closed": ("closed", fmt.DIM),
             "failed": ("did not start", fmt.BAD)}
EMPTY = "No environments yet. When an agent opens one, it shows here with its cost, time left and git."


def euro(value: float | None) -> str:
    if value is None:
        return ""
    return f"€{value:.2f}" if value >= 0.1 or value == 0 else f"€{value:.3f}"


def left_words(env: Environment) -> Text:
    if not env.open:
        return Text("closed", style=fmt.DIM)
    if env.minutes_left is None:
        return Text(fmt.until(env.expires_at) if env.expires_at else "", style=fmt.MUTED)
    m = env.minutes_left
    words = f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"
    return Text(words, style=fmt.WARN if m < 15 else fmt.TEXT)


def git_words(env: Environment) -> Text:
    if env.ahead:
        return Text(f"{env.ahead} not pushed", style=fmt.WARN)
    if env.pushed:
        return Text(f"pushed {fmt.plural(env.pushed, 'time')}", style=fmt.OK)
    if env.ahead == 0:
        return Text("nothing to push", style=fmt.MUTED)
    return Text("", style=fmt.DIM)


def repo_words(env: Environment) -> str:
    repo = (env.repo or "").split("://", 1)[-1].removeprefix("github.com/").removesuffix(".git")
    if not repo:
        return "no repository"
    return f"{repo} · {env.branch}" if env.branch else repo


def env_columns(width: int) -> list[tuple[str, str, int]]:
    cols = [("state", "State", 13), ("cost", "Cost", 8), ("left", "Time left", 12), ("git", "Git", 16)]
    if width >= 110:
        cols.insert(1, ("repo", "Repository", 30))
        cols.append(("proofs", "Proofs", 7))
    used = sum(w + 2 for _, _, w in cols) + 3
    return [("title", "Environment", max(16, width - used)), *cols]


def env_cells(env: Environment, cols: list[tuple[str, str, int]]) -> list[Text]:
    out = []
    for key, _, width in cols:
        if key == "title":
            out.append(Text(fmt.clip(env.title or env.id, width), style=fmt.TEXT if env.open else fmt.MUTED))
        elif key == "state":
            word, style = ENV_STATE.get(env.state, (env.state, fmt.MUTED))
            out.append(Text(word, style=style))
        elif key == "repo":
            out.append(Text(fmt.clip(repo_words(env), width), style=fmt.MUTED))
        elif key == "cost":
            out.append(Text(euro(env.cost_eur), style=fmt.TEXT))
        elif key == "left":
            out.append(left_words(env))
        elif key == "git":
            out.append(git_words(env))
        elif key == "proofs":
            out.append(Text(str(env.proofs) if env.proofs is not None else "", style=fmt.MUTED))
    return out


class EnvironmentsView(View):
    TOPICS = frozenset({"environments", "conn"})

    def compose(self):
        yield Static("Environments", classes="section-title")
        yield Static(id="envs-note", classes="note")
        yield DataTable(id="envs-table", cursor_type="row")

    def refresh_view(self, topics: set[str]) -> None:
        state: DashState = self.state
        width = self.size.width or self.app.size.width or 80
        note = resource_note(state, "environments")
        if note is None and not state.environments:
            note = Text(EMPTY, style=fmt.MUTED)
        widget = self.query_one("#envs-note", Static)
        widget.update(note or "")
        widget.display = note is not None
        table = self.query_one("#envs-table", DataTable)
        keep = _cursor_key(table)
        table.clear(columns=True)
        cols = env_columns(width)
        for _, label, w in cols:
            table.add_column(label, width=w)
        for env in state.environments:
            table.add_row(*env_cells(env, cols), key=env.id)
        _restore_cursor(table, keep)
