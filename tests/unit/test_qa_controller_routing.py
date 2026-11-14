"""QA for lane routing across nodes (agentos.controller.routing.route): a call's destination must not depend
on the order in which the nodes connected. Bugs are strict xfails pinned to their reason."""

from __future__ import annotations

from agentos.controller.routing import NodeView, ToolDef, route
from agentos.core.models import Effect


def node(name: str, lane_arg: str | None) -> NodeView:
    schema = {"type": "object", "properties": {"on": {"type": "string"}}}
    tool = ToolDef(name="browser_snapshot", description="", input_schema=schema, effect=Effect.READ,
                   lane_arg=lane_arg)
    return NodeView(f"node_{name}", name, {"browser_snapshot": tool}, {"shared/blog"})


def destination(nodes: list[NodeView]) -> tuple[str, dict] | str:
    try:
        target, args = route("browser_snapshot", {"on": "b/shared/blog"}, nodes)
    except Exception as exc:  # noqa: BLE001 - the routing error is the answer under test
        return type(exc).__name__
    return target.name, args


def test_a_qualified_lane_routes_to_its_node_whichever_node_connected_first():
    both_declare = [node("a", "on"), node("b", "on")]
    assert destination(both_declare) == ("b", {"on": "shared/blog"})
    assert destination(list(reversed(both_declare))) == ("b", {"on": "shared/blog"})


def test_the_same_call_routes_the_same_way_whatever_order_the_nodes_connected_in():
    differing = [node("a", None), node("b", "on")]
    assert destination(differing) == destination(list(reversed(differing))) == ("b", {"on": "shared/blog"})
