"""plugins_list: an agent can see which workers exist and whether they are on. That is all.

No tool turns a plugin on or off, on the node or on a browser, or changes an assignment. Only a person
does, with `openajar plugins` on the node or signed in to the panel.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from openajar.core.models import Effect
from openajar.core.tools import ToolContext, ToolSpec
from openajar.plugins.service import SERVICE_NAME, PluginService, build

PluginArg = Annotated[str | None, Field(description='Ask for one worker by id, for example "browser-worker". '
                                                    "Leave it out to list them all.")]


def _service(runtime: Any) -> PluginService:
    return runtime.get(SERVICE_NAME) or build(runtime)


def _seen(view: Any, ctx: ToolContext) -> dict[str, Any]:
    """A view as this caller may read it: a browser outside its grant is not named."""
    return {**view.to_dict(), "lanes_off": [lane for lane in view.lanes_off if ctx.can_access_lane(lane)]}


def tool_specs(runtime: Any) -> list[ToolSpec]:
    async def plugins_list(ctx: ToolContext, *, plugin: PluginArg = None) -> dict:
        """The workers on this node: whether each is on, why one cannot run, and which brain does each of
        its jobs. `lanes_off` names the browsers you can reach where a person turned the worker off: browser_task is
        refused there, and the browser tools still work. Name one with `plugin` when you need it: you get it
        back when it can run, and otherwise an error that says why. You cannot turn a worker on, on a browser
        or anywhere, or change its brain; only a person can.
        Effect: read. Errors: plugin_disabled (it is off, tell your user), unavailable (it is on and cannot
        run), not_found."""
        await ctx.authorize(Effect.READ, summary="List this node's workers")
        service = _service(runtime)
        if plugin is None:
            return {"plugins": [_seen(view, ctx) for view in service.views()]}
        service.require(plugin)
        return _seen(service.view(plugin), ctx)

    return [ToolSpec(name="plugins_list", handler=plugins_list, effect=Effect.READ, lane_arg=None,
                     tags=("plugins",))]
