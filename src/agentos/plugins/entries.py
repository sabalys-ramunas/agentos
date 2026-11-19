"""The workers agentos ships. Adding one is a reviewed change to this file, and there is no other
way to add one."""

from __future__ import annotations

from agentos.plugins.models import Job, Plugin, Skill, Stage
from agentos.plugins.requirements import DevicePresent, MemoryAtLeast, NotBuilt, ServicePresent

DECIDE = Job("decide", "Answers the small typed questions that steer the work.", (Skill.DECISION,))
WRITE = Job("write", "Writes the text the work needs.", (Skill.TEXT,), optional=True,
            without="The worker cannot write text, so a run that needs new words stops and says so.")
RESCUE = Job("rescue", "Takes over when the plain path gets stuck.", (Skill.TEXT, Skill.TOOLS), optional=True,
             without="When the worker is unsure, it stops for a person.")
WORKER_JOBS = (DECIDE, WRITE, RESCUE)

BROWSER_WORKER = Plugin(
    id="browser-worker",
    name="Browser worker",
    description="Does routine work in a browser lane on its own, and asks a model only when it has to.",
    stage=Stage.WORKS,
    lane_kinds=("browser",),
    services=("agentos.pilot.service",),
    tools=("agentos.pilot.tools",),
    tool_names=("browser_task",),
    needs=(ServicePresent("pilot", "agentos.pilot.service"),
           ServicePresent("brains", "agentos.brains.service")),
    jobs=WORKER_JOBS,
)

# Honest about where this stands: docs/ANDROID.md says spike first and build the engine later. No
# modules are named, because none exist. The hardware needs are checked anyway, so a person can see
# what a node lacks before the engine lands.
ANDROID_WORKER = Plugin(
    id="android-worker",
    name="Android worker",
    description="Would do the same work inside an Android lane, for accounts that need an app.",
    stage=Stage.DECLARED,
    lane_kinds=("android",),
    needs=(NotBuilt("Android engine", "the Android engine does not exist yet, so this worker cannot run on any "
                                      "node (docs/ANDROID.md)"),
           DevicePresent("binder", ("/dev/binder", "/dev/binderfs/binder"),
                         "the binder_linux kernel module is not loaded"),
           MemoryAtLeast(16)),
    jobs=WORKER_JOBS,
)

DESKTOP_WORKER = Plugin(
    id="desktop-worker",
    name="Desktop worker",
    description="Would do the same work in a full desktop session. Planned for later.",
    stage=Stage.LATER,
    lane_kinds=("desktop",),
    needs=(NotBuilt("desktop engine", "the desktop worker is planned for later: no desktop engine exists yet"),),
    jobs=WORKER_JOBS,
)

# Removing it: delete src/agentos/textreview, this entry and its line in README.md. Nothing else
# imports it (tests/unit/test_textreview.py holds that line).
TEXT_REVIEW = Plugin(
    id="text-review",
    name="Agent text review",
    description="Once a week, reads how agents used this node and proposes clearer text for its tools, for a "
                "person to review. It never changes the node.",
    stage=Stage.WORKS,
    lane_kinds=(),
    services=("agentos.textreview.service",),
    cli="agentos.textreview.cli",
    needs=(ServicePresent("brains", "agentos.brains.service"),),
    jobs=(Job("review", "Reads a week of agent sessions and proposes clearer text for the tools.", (Skill.TEXT,)),),
)

PLUGINS: tuple[Plugin, ...] = (BROWSER_WORKER, ANDROID_WORKER, DESKTOP_WORKER, TEXT_REVIEW)
