"""Agent lanes: the owner's own Claude Code or Codex, hosted on the node (lane kind "agent").

Phase 1 contracts only (docs/design/always-on-agents.md, brief B0). The supervisor, store,
adapters and tools come with briefs B1 to B3 and B5.

    models.AgentLaneSpec, Session, Turn, TurnStatus, AgentState   the shared shapes
    ports.AgentsPort                                               what agentos.events calls

agentos never ships an agent of its own: deleting every runner adapter here leaves a node
with no agent in it (design section 2.2).
"""
