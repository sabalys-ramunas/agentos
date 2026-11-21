"""Flow documents, runs, steps and schedules.

A flow is an immutable FlowVersion: a JSON document whose version is the hash of its
content. Steps address elements through structural locators (role + accessible name +
nth + an ancestor anchor + a CSS fallback), never through snapshot refs, which change on
every page load. Every step that changes the target's state (`writes: true`) declares a
readback of the target's own state, so a run can prove it did what it claims.

A flow's effective effect (what grants and approvals see) is the stricter of its declared
`effect` and what its steps can do: navigation, waits, scrolls and reads are `read`; any
input step (click, press, type, fill, select, upload) is `commit`, unless the step says
`"publishes": false` AND the version was saved by the human owner, in which case it is
`draft`. So an agent can never lower a flow below its steps (see src/openajar/runner/README.md).

Locators and conditions live in openajar.core.locator (import them from there). The other names
live in smaller modules: steps (step types and the helpers that walk them), urlpattern, names (ids and patterns), owner (who counts
as the owner), flowdoc (FlowDefinition, FlowVersion) and records (runs, step records,
schedules). This module re-exports the names other packages and the tests import from here.
"""

from __future__ import annotations

from openajar.runner.flowdoc import (
    FlowDefinition,
    SendsSpec,
)
from openajar.runner.names import (
    PARAM_NAME_RE,
    is_connector_id,
)
from openajar.runner.owner import (
    LEARNED_KIND,
    TRY_SOURCE,
    NodeInstall,
    answered_in_person,
    is_owner,
)
from openajar.runner.records import (
    DEFAULT_TIMEZONE,
    SUCCEEDED,
    TERMINAL,
    Run,
    Schedule,
    ScheduleKind,
    StepStatus,
)
from openajar.runner.steps import (
    ExtractField,
    ExtractStep,
    PreflightCheck,
    ReadbackStep,
    ReadJsonStep,
    iter_steps,
    step_at,
    steps_effect,
)
from openajar.runner.urlpattern import (
    MAX_WATCHED_URL,
    names_secret,
    url_pattern,
)

__all__ = [
    "DEFAULT_TIMEZONE",
    "LEARNED_KIND",
    "MAX_WATCHED_URL",
    "PARAM_NAME_RE",
    "SUCCEEDED",
    "TERMINAL",
    "TRY_SOURCE",
    "ExtractField",
    "ExtractStep",
    "FlowDefinition",
    "NodeInstall",
    "PreflightCheck",
    "ReadJsonStep",
    "ReadbackStep",
    "Run",
    "Schedule",
    "ScheduleKind",
    "SendsSpec",
    "StepStatus",
    "answered_in_person",
    "is_connector_id",
    "is_owner",
    "iter_steps",
    "names_secret",
    "step_at",
    "steps_effect",
    "url_pattern",
]
