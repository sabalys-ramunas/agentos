"""The MCP tool providers: every feature package's `tool_specs(runtime)`, each behind one guard.

The daemon (agentos.node.daemon, the composition root) serves `tool_providers()`; `agentos tools`, the
panel's computer view and the text review read the same list. A package that is not installed is
skipped and one that fails is logged, so the node keeps serving the rest.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Sequence
from typing import Any

from agentos.core.runtime import Runtime
from agentos.core.tools import ToolSpec

log = logging.getLogger("agentos.node.daemon")  # the daemon's log lines, under the name they always had

# Feature packages that may add MCP tools (`tool_specs(runtime) -> list[ToolSpec]`). A package
# that is not installed is skipped; keep this list explicit, so nothing is imported by accident.
FEATURE_TOOLS: tuple[str, ...] = (
    "agentos.node.io_tools",  # files, uploads, tabs and dialogs of browser lanes
    "agentos.node.control_tools",  # lane_control, lane_request_control, lane_wait_control
    "agentos.node.devtools_tools",  # browser_console, browser_network: why a page broke
    "agentos.freeze.tools",  # lane_freeze (an agent can freeze, never unfreeze)
    "agentos.node.vault_tools",  # login, vault_fill, vault_list: the vault types into a lane
    "agentos.workspaces.tools",
    "agentos.policy.tools",
    "agentos.runner.tools",
    "agentos.runner.mail.tools",  # mail_connect, mail_list, mail_read, mail_mark, mail_send
    "agentos.brains.tools",
    "agentos.git.tools",  # git_remotes: what the node can clone and push for a workspace (no secrets)
    "agentos.env.tools",
    "agentos.env.files",  # lane_file_read, lane_file_write, lane_file_edit: code on a lane's machine
    "agentos.vms.tools",  # vm_create, vm_destroy, vm_extend, vms_list (disposable cloud VMs as lanes)
    "agentos.environments.tools",  # environment_open, environment, environment_close (a cloud computer per chat)
    "agentos.environments.uploads",  # environment_upload: a one-use link for a local checkout's tarball
    "agentos.events.tools",
    "agentos.agenda.tools",  # agenda_list, agenda_draft, agenda_propose, ... (approving is not an agent tool)
    "agentos.memory.tools",  # memory_context, memory_search, memory_get, memory_write, memory_forget
    "agentos.profiles.tools",  # profile_list, profile_get, profile_use, profile_propose, profile_save, ...
    "agentos.stealth.tools",  # lane_check: is this lane safe to use right now
    "agentos.studio.tools",  # ui_check, ui_diff, system_check, ...: is this page all right (a URL, not a lane)
)
PLUGINS_TOOLS = "agentos.plugins.tools"

Provider = Callable[[Runtime], list[ToolSpec]]


def _node_tools(runtime: Runtime) -> list[ToolSpec]:
    from agentos.node.tools import tool_specs

    return tool_specs(runtime)


def _guarded(name: str, provider: Provider) -> Provider:
    """A feature provider that fails is logged and contributes nothing; the node keeps serving."""

    def provide(runtime: Runtime) -> list[ToolSpec]:
        try:
            return list(provider(runtime))
        except Exception:
            log.exception("%s.tool_specs failed; its tools are not served", name)
            return []

    return provide


def feature_providers(modules: Sequence[str] = FEATURE_TOOLS) -> list[Provider]:
    """`tool_specs` of every feature package in `modules` that is installed."""
    found: list[Provider] = []
    for name in modules:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name and (name == exc.name or name.startswith(exc.name + ".")):
                log.info("%s is not installed; its tools are not served", name)
            else:
                log.error("%s needs %s, which is missing; its tools are not served", name, exc.name)
            continue
        except Exception:
            log.exception("%s failed to import; its tools are not served", name)
            continue
        provider = getattr(module, "tool_specs", None)
        if not callable(provider):
            log.error("%s has no tool_specs(runtime); its tools are not served", name)
            continue
        log.info("serving tools from %s", name)
        found.append(_guarded(name, provider))
    return found


def _plugin_tools(others: Sequence[Provider]) -> Provider:
    """plugins_list, and the tools of each plugin this daemon loaded, gated so a call stops working
    when a person disables the plugin. Two tools with one name would stop the whole server, so a
    plugin that clashes with `others` or with another plugin fails alone."""

    def provide(runtime: Runtime) -> list[ToolSpec]:
        specs = [spec for provider in feature_providers((PLUGINS_TOOLS,)) for spec in provider(runtime)]
        registry: Any = runtime.get("plugins")
        loaded = registry.loaded() if registry is not None else []
        if not loaded:
            return specs
        taken = {spec.name for provider in others for spec in provider(runtime)} | {spec.name for spec in specs}
        for plugin in loaded:
            modules = [name for name in plugin.tools if name not in FEATURE_TOOLS]
            served = [spec for provider in feature_providers(modules) for spec in provider(runtime)]
            clash = next((spec.name for spec in served if spec.name in taken), None)
            if clash or (modules and not served):
                registry.mark_failed(plugin.id, f"tool {clash} is already served" if clash else "its tools did not load")
                continue
            taken |= {spec.name for spec in served}
            specs += [registry.gated(plugin.id, spec) for spec in served]
        return specs

    return provide


def tool_providers(modules: Sequence[str] = FEATURE_TOOLS) -> list[Provider]:
    providers = [_node_tools, *feature_providers(modules)]
    if modules is FEATURE_TOOLS:  # a caller that names its own modules named all it wants
        providers.append(_plugin_tools(tuple(providers)))
    return providers


class ProviderCatalog:
    """The tools agents see (agentos.core.tools.ToolCatalog), registered as `tool_catalog`. In the daemon,
    what it serves (`served_only`). Anywhere else, every tool this build has, plugins' tools included,
    as `agentos tools` lists them."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    def specs(self, *, served_only: bool) -> list[ToolSpec]:
        from agentos.plugins.entries import PLUGINS

        runtime = self.runtime
        providers = tool_providers()
        if not served_only:
            providers += feature_providers(tuple(m for plugin in PLUGINS for m in plugin.tools))
        specs: dict[str, Any] = {}
        for provider in providers:
            try:
                found = provider(runtime)
            except Exception:  # one feature's tools failing must not stop the review
                log.exception("a tool provider failed while the text review read the tools")
                continue
            for spec in found:
                if not isinstance(spec, ToolSpec) or spec.name in specs:
                    continue
                if served_only and spec.hidden_without and not runtime.has(spec.hidden_without):
                    continue
                specs[spec.name] = spec
        return list(specs.values())

    def params(self, spec: ToolSpec) -> list[Any]:
        from agentos.mcp.registry import tool_params

        return list(tool_params(spec))
