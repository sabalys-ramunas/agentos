"""Workspace memory with provenance (docs/design/workspace-memory.md): a per-workspace store of
facts, documents, decisions, preferences, contacts and run summaries, where the node computes
each version's trust and labels and serves them back with the data. Runtime service "memory".

    from openajar.memory import SERVICE_NAME, build, Caller, MemoryScope
    memory = build(runtime)                       # registers "memory"
    read = memory.context(caller, "biz-a", task="draft this week's posts")
    ctx.taint(*read.taint)                        # already added through the policy port
"""

from openajar.memory.common import MemoryRead, WritePlan
from openajar.memory.models import Caller, EntryType, Limits, MemoryScope, Trust, WriteRequest
from openajar.memory.service import SERVICE_NAME, MemoryService, build

__all__ = [
    "SERVICE_NAME",
    "Caller",
    "EntryType",
    "Limits",
    "MemoryRead",
    "MemoryScope",
    "MemoryService",
    "Trust",
    "WritePlan",
    "WriteRequest",
    "build",
]
