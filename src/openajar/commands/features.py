"""The feature packages' command groups, mounted on the root command, and what the root wires
into them (the runtime a CLI opens, and the plugins' change log)."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

import typer

from openajar.commands.base import cli_runtime, plugins_cli_runtime


def mount_features(app: typer.Typer) -> None:
    import importlib

    mounts = (
        ("openajar.client.cli", None, None),  # connect, disconnect, nodes, status (on the Mac)
        ("openajar.vault.cli", None, "vault"),
        ("openajar.git.cli", None, "git"),  # git key add|list|remove, git remote allow|list|deny
        ("openajar.policy.cli", None, "policy"),
        ("openajar.policy.cli", "grants_app", "grants"),
        ("openajar.policy.cli", "approvals_app", "approvals"),
        ("openajar.policy.cli", "confirmation_app", "confirmation"),
        ("openajar.audit.cli", None, "audit"),  # verify, replay, export, share (the tamper-evident action log)
        ("openajar.runner.cli", None, "runner"),
        ("openajar.runner.cli", "runs_app", "runs"),
        ("openajar.runner.cli", "flows_app", "flows"),
        ("openajar.runner.cli", "flows_app", "flow"),  # openajar flow pull|push|...: the same group
        ("openajar.runner.cli", "schedules_app", "schedules"),
        ("openajar.runner.connectors.cli", None, "connector"),
        ("openajar.runner.siteapis.cli", None, "site-api"),
        ("openajar.runner.workflows.cli", None, "workflows"),
        ("openajar.runner.mail.cli", None, "mail"),
        ("openajar.agenda.cli", None, "agenda"),  # the agenda: planned actions, their approval and proof
        ("openajar.api.cli", None, "panel"),
        ("openajar.tui.cli", None, None),  # openajar dash
        ("openajar.workspaces.cli", None, "workspace"),
        ("openajar.memory.cli", None, "memory"),  # workspace memory, as the owner in person
        ("openajar.profiles.cli", None, "profile"),  # a person's or business's profile, for all its accounts
        ("openajar.brains.cli", None, "brains"),
        ("openajar.plugins.cli", None, "plugins"),  # list | show | enable | disable | assign (a person only)
        ("openajar.pilot.cli", None, "pilot"),  # which model does each pilot job
        ("openajar.env.cli", None, "env"),
        ("openajar.vms.cli", None, "vm"),  # setup, types, list, destroy, status, image build (cloud VMs)
        # list, show, close, allow, setup (a computer per chat); prepared_cli adds prepare and prepared to its
        # app, resume_cli (which imports prepared_cli) resume, and logins_cli (which imports resume_cli) login
        ("openajar.environments.logins_cli", None, "environments"),
        ("openajar.environments.logins_cli", None, "envs"),  # the same group, shorter
        ("openajar.controller.cli", None, "controller"),  # controller serve | join-token | login | nodes
        ("openajar.webbotauth.cli", None, "identity"),  # init | show | hosts | test-sign (Web Bot Auth)
        ("openajar.freeze.cli", None, "freeze"),  # freeze lane | workspace | node, freeze status
        ("openajar.freeze.cli", "unfreeze_app", "unfreeze"),
    )
    for module_name, attr, command in mounts:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name and module_name.startswith(exc.name):
                continue  # this feature is not part of the build
            raise
        sub = getattr(module, attr or "app", None)
        if isinstance(sub, typer.Typer):
            app.add_typer(sub, name=command)  # no name: its commands join the top level
    _mount_plugin_clis(app)


def _mount_plugin_clis(app: typer.Typer) -> None:
    """A plugin's own commands, named by its manifest, as `openajar <plugin id>`. They are there
    whether the plugin is on or not, and each one refuses when it is off."""
    import importlib

    try:
        from openajar.plugins.entries import PLUGINS
    except ModuleNotFoundError:
        return
    for plugin in PLUGINS:
        if not plugin.cli:
            continue
        try:
            module = importlib.import_module(plugin.cli)
        except ModuleNotFoundError as exc:
            if exc.name and plugin.cli.startswith(exc.name):
                continue  # the plugin's package is not part of this build
            raise
        sub = getattr(module, "app", None)
        if isinstance(sub, typer.Typer):
            app.add_typer(sub, name=plugin.id)


class PluginChanges:
    """`openajar plugins` on the node's action log, which that package cannot import. It opens on
    first use, so a refused command and a plain `list` never touch the log.

    A row that stays waiting in memory dies with this process, so `record` says so and the change
    is undone. The row can still land when `close` tries once more, and a record of a change that
    did not stand is the safe way round."""

    BUSY_TIMEOUT_MS = 5000  # a person is waiting, and the daemon may be writing

    def __init__(self, home: Path):
        self.home = home
        self.traces: Any = None

    def _open(self) -> Any:
        if self.traces is None:
            from openajar.core.errors import ServiceUnavailable
            from openajar.policy.traces import TraceStore

            path = self.home / "traces.db"
            if os.geteuid() == 0 and not path.exists():  # root would own it, and the daemon could not write
                raise ServiceUnavailable("the action log does not exist yet: start agentosd once, then try again")
            self.traces = TraceStore(path, busy_timeout_ms=self.BUSY_TIMEOUT_MS)
        return self.traces

    def record(self, change: Any) -> None:
        from openajar.core.errors import ServiceUnavailable
        from openajar.policy.traces import TraceEvent

        traces = self._open()
        traces.write(TraceEvent(**change.trace()))
        if traces.flush():
            raise ServiceUnavailable("the log is busy or broken, and did not take the row")

    def read(self, plugin: str, limit: int) -> list[dict]:
        from openajar.plugins.models import CHANGE_KIND

        events, _ = self._open().query(kind=CHANGE_KIND, limit=500)
        return [event.public() for event in events if (event.args or {}).get("plugin") == plugin][:limit]

    def close(self) -> None:
        if self.traces is not None:
            self.traces.close()


def wire_feature_clis() -> None:
    with contextlib.suppress(ModuleNotFoundError):
        from openajar.plugins import cli as plugins_cli

        if plugins_cli.runtime_factory is None:
            plugins_cli.runtime_factory = plugins_cli_runtime
        if plugins_cli.change_log_factory is None:
            plugins_cli.change_log_factory = lambda runtime: PluginChanges(runtime.require("config").home)
    try:
        from openajar.brains import cli as brains_cli
    except ModuleNotFoundError:
        return
    if brains_cli.runtime_factory is None:
        brains_cli.runtime_factory = cli_runtime
