"""The resource routes, in one table: approvals, activity, runs, flows, schedules, nodes, brains,
vault, grants, proxies and notifications (docs/API.md). The handlers live in routes_approvals,
routes_activity and routes_settings."""

from __future__ import annotations

from starlette.routing import Route

from agentos.api.access import Presence
from agentos.api.app import Api, endpoint
from agentos.api.route_ids import grant_family
from agentos.api.routes_activity import ActivityHandlers
from agentos.api.routes_approvals import ApprovalHandlers
from agentos.api.routes_settings import SettingsHandlers

__all__ = ["grant_family", "routes"]  # the runner's and the agenda's routes read grant_family here


def routes(api: Api) -> list[Route]:
    # what waits for a person; what agents did and will do; the node's settings
    wait, did, setting = ApprovalHandlers(api), ActivityHandlers(api), SettingsHandlers(api)

    def e(fn):
        return endpoint(api, fn)

    def owner(fn):
        return endpoint(api, fn, owner=True)

    def role(cap: str, presence: Presence | None = None, step_up: bool = False):
        """The owner, or a member whose role has `cap` somewhere (the handler checks the workspace).
        `presence`: the least the request must be (a person, for a decision; docs/THREAT-MODEL.md)."""
        return lambda fn: endpoint(api, fn, cap=cap, presence=presence, step_up=step_up)

    def person(fn):
        """Any signed-in caller limited to its grant, as `e`, but only a person (Presence.PERSON)."""
        return endpoint(api, fn, presence=Presence.PERSON)

    PERSON = Presence.PERSON

    # Scope (see docs/API.md, "Who may call what"): e = any signed-in caller, limited to its grant
    # inside the handler; owner = the owner or an admin grant only; role(cap) = the owner, an admin
    # grant, or a member whose role has `cap` (agentos.api.members.CAPABILITIES) where it applies.
    return [
        Route("/api/approvals", e(wait.approvals), methods=["GET"]),
        Route("/api/approvals/batch", e(wait.batch), methods=["POST"]),  # presence, then the decide role, in the handler
        Route("/api/approvals/standing", role("standing")(wait.standing_list), methods=["GET"]),
        Route("/api/approvals/standing", role("standing", PERSON, step_up=True)(wait.standing_create), methods=["POST"]),
        Route("/api/approvals/standing/{id}", role("standing")(wait.standing_revoke), methods=["DELETE"]),
        Route("/api/approvals/{id}", e(wait.approval_get), methods=["GET"]),
        Route("/api/approvals/{id}/approve", role("decide", PERSON)(wait.approve), methods=["POST"]),
        Route("/api/approvals/{id}/deny", role("decide")(wait.deny), methods=["POST"]),
        Route("/api/approvals/{id}/clear", role("decide")(wait.approval_clear), methods=["POST"]),
        Route("/api/approvals/{id}/review", e(wait.approval_review), methods=["GET"]),
        Route("/api/approvals/{id}/action", endpoint(api, wait.approval_link, public=True), methods=["POST"]),
        Route("/api/approvals/{id}/preview.png", e(wait.preview_png), methods=["GET"]),
        Route("/api/approvals/{id}/files/{param}", e(wait.approval_file), methods=["GET"]),
        Route("/api/approvals/{id}/messages", e(wait.approval_messages), methods=["GET"]),
        Route("/api/codes", owner(wait.codes), methods=["GET"]),
        Route("/api/codes/{id}", endpoint(api, wait.code_answer, owner=True, presence=PERSON), methods=["POST"]),
        Route("/api/codes/{id}/decline", owner(wait.code_decline), methods=["POST"]),
        Route("/api/questions", e(wait.questions), methods=["GET"]),
        Route("/api/questions/{id}/answer", role("answer", PERSON)(wait.question_answer), methods=["POST"]),
        Route("/api/activity", e(did.activity), methods=["GET"]),
        Route("/api/runs", e(did.runs), methods=["GET"]),
        Route("/api/runs/{id}", e(did.run_get), methods=["GET"]),
        Route("/api/flows", e(did.flows), methods=["GET"]),
        Route("/api/ask-first", e(setting.ask_first_list), methods=["GET"]),
        Route("/api/ask-first", e(setting.ask_first_set), methods=["POST"]),
        Route("/api/confirmation", person(setting.confirmation_get), methods=["GET"]),
        Route("/api/confirmation", person(setting.confirmation_set), methods=["PUT"]),
        Route("/api/confirmation/methods", person(setting.confirmation_methods), methods=["GET"]),
        Route("/api/flows/{id}/run", e(did.flow_run), methods=["POST"]),
        Route("/api/schedules", e(did.schedules), methods=["GET"]),
        Route("/api/schedules", owner(did.schedule_create), methods=["POST"]),
        Route("/api/schedules/{id}", e(did.schedule_get), methods=["GET"]),
        Route("/api/schedules/{id}/fires", e(did.schedule_fires), methods=["GET"]),
        Route("/api/schedules/{id}", role("standing")(did.schedule_update), methods=["PATCH"]),
        Route("/api/schedules/{id}", role("standing")(did.schedule_delete), methods=["DELETE"]),
        Route("/api/nodes", owner(setting.nodes), methods=["GET"]),
        Route("/api/brains", e(setting.brains), methods=["GET"]),
        Route("/api/brains/order", owner(setting.brains_order), methods=["POST"]),
        Route("/api/brains/tasks", owner(setting.brain_tasks), methods=["GET"]),
        Route("/api/brains/tasks/{id}", owner(setting.brain_task), methods=["GET"]),
        Route("/api/vault", role("vault")(setting.vault), methods=["GET"]),
        Route("/api/vault", role("vault")(setting.vault_create), methods=["POST"]),
        Route("/api/vault/{id}", role("vault")(setting.vault_delete), methods=["DELETE"]),
        Route("/api/grants", role("connect")(setting.grants), methods=["GET"]),
        Route("/api/grants", role("connect")(setting.grant_create), methods=["POST"]),
        Route("/api/grants/{id}/revoke", role("connect")(setting.grant_revoke), methods=["POST"]),
        Route("/api/grants/{id}/route", owner(setting.grant_route), methods=["POST"]),
        Route("/api/proxies", owner(setting.proxies), methods=["GET"]),
        Route("/api/proxies", owner(setting.proxy_create), methods=["POST"]),
        Route("/api/settings/notifications", owner(setting.notifications), methods=["GET"]),
        Route("/api/settings/notifications", owner(setting.notifications_put), methods=["PUT"]),
        Route("/api/settings/notifications/test", owner(setting.notifications_test), methods=["POST"]),
    ]
