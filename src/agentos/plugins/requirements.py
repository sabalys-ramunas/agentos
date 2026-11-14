"""Requirements: each one is checked on this node and says, in a plain sentence, why it passes or
fails. Nothing here assumes: a service, a lane kind, a device and memory are all looked at.

The facts come in through NodeFacts, so a test can describe any machine, and a check never raises.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentos.plugins.models import Check

GB = 1024**3
# A machine sold as 16 GB reports less, because the kernel and firmware keep some: the 4 GB node
# in docs/ANDROID.md reports 3819 MB (93%). So a stated size counts from 90% of it.
MEMORY_TOLERANCE = 0.9

# What provides each lane kind on a node, by module name. agentos.core.models.LaneKind lists the
# kinds; a test keeps this in step with it.
LANE_KIND_MODULES: dict[str, str] = {
    "browser": "agentos.node.lanes",
    "terminal": "agentos.env.service",
    "service": "agentos.env.service",
    "android": "agentos.android",
    "agent": "agentos.agents",
}


def module_installed(name: str) -> bool:
    """Is this module part of this build? Finds it without running it."""
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def memory_bytes() -> int | None:
    """Total memory of this machine, or None when it cannot be read."""
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class NodeFacts:
    """What a check may look at. `running` says the runtime is the daemon's and this plugin has
    loaded, so a service must really be on it. Until then, and always in the CLI (which is another
    process, with no daemon services on its runtime), a service counts when its module is part of
    this build: the daemon builds a plugin's own services only after the plugin is on."""
    runtime: Any
    running: bool = False
    installed: Callable[[str], bool] = module_installed
    exists: Callable[[str], bool] = os.path.exists
    memory: Callable[[], int | None] = memory_bytes


@dataclass(frozen=True)
class ServicePresent:
    """A runtime service, by its well-known name. `module` is its provider's name, as a string in
    the daemon's lists: the module is looked for, never imported."""
    service: str
    module: str
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            object.__setattr__(self, "label", f"{self.service} service")

    def check(self, facts: NodeFacts) -> Check:
        if facts.runtime is not None and facts.runtime.has(self.service):
            return Check(self.label, True, f"the {self.service} service is running on this node")
        if facts.running:
            return Check(self.label, False, f"the {self.service} service is not running on this node")
        if facts.installed(self.module):
            return Check(self.label, True, f"the {self.service} service is part of this build")
        return Check(self.label, False, f"the {self.service} service is not part of this build yet, so this "
                                        "worker has nothing to run on")


@dataclass(frozen=True)
class LaneKindSupported:
    kind: str

    @property
    def label(self) -> str:
        return f"{self.kind} lanes"

    def check(self, facts: NodeFacts) -> Check:
        module = LANE_KIND_MODULES.get(self.kind)
        if module is None:
            return Check(self.label, False, f"agentos has no {self.kind} lanes yet")
        if facts.installed(module):
            return Check(self.label, True, f"this node supports {self.kind} lanes")
        return Check(self.label, False, f"this node does not support {self.kind} lanes: {module} is not part "
                                        "of this build")


@dataclass(frozen=True)
class DevicePresent:
    """A device node. Any one of `paths` is enough."""
    label: str
    paths: tuple[str, ...]
    missing: str  # what its absence means, in a plain clause

    def check(self, facts: NodeFacts) -> Check:
        found = next((path for path in self.paths if facts.exists(path)), None)
        if found:
            return Check(self.label, True, f"{found} is present")
        return Check(self.label, False, f"{self.paths[0]} is missing: {self.missing}")


@dataclass(frozen=True)
class MemoryAtLeast:
    gb: int

    @property
    def label(self) -> str:
        return f"{self.gb} GB of memory"

    def check(self, facts: NodeFacts) -> Check:
        total = facts.memory()
        if total is None:
            return Check(self.label, False, f"the memory of this machine could not be read, and this worker "
                                            f"needs at least {self.gb} GB")
        have = f"{total / GB:.1f} GB"
        if total >= self.gb * GB * MEMORY_TOLERANCE:
            return Check(self.label, True, f"this machine has {have} of memory (needs {self.gb} GB)")
        return Check(self.label, False, f"this machine has {have} of memory, and this worker needs at least "
                                        f"{self.gb} GB")


@dataclass(frozen=True)
class NotBuilt:
    """The code this worker would run does not exist. Fails on every node, so a declared worker
    can never be enabled, and a state file that says otherwise changes nothing."""
    label: str
    reason: str

    def check(self, facts: NodeFacts) -> Check:
        return Check(self.label, False, self.reason)
