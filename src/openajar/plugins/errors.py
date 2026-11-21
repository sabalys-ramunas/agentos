"""Plugin errors. They subclass the core errors, so every surface maps them to stable codes."""

from __future__ import annotations

from openajar.core.errors import Conflict, PolicyDenied, ServiceUnavailable


class PluginDisabled(ServiceUnavailable):
    """The worker is off on this node, and only a person can turn it on. No tool can."""
    code = "plugin_disabled"


class StateBroken(ServiceUnavailable):
    """$OPENAJAR_HOME/plugins.db cannot be trusted, so it was left as it is, and nothing is on."""
    code = "plugin_state_broken"


class RequirementsUnmet(Conflict):
    """`openajar plugins enable` refused: the message lists each unmet reason. Nothing changed."""
    code = "plugin_requirements_unmet"


class OnlyAPerson(PolicyDenied):
    """Run from inside a brain task or a lane, where only a person at the node's own shell may act."""
    code = "plugin_person_only"


class ChangeNotRecorded(ServiceUnavailable):
    """The action log could not keep the record, so the change was undone. `disable` is the one
    exception: turning a worker off always goes through, and the message says so."""
    code = "plugin_change_not_recorded"
