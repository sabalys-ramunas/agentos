"""What a lane that runs on the node itself can reach (QA 2026-11-10, finding 29).

A lane reaches the node when a command in it can read the node's own files. That is a workbench
(env0) lane, and also a sandboxed lane with one of these dials set to shared (the review of the fix
wave, finding 1): `--preset workbench --network off` or `--preset sandbox --filesystem shared` binds
the node's `/` read-write inside bubblewrap, so the vault and node.toml are right there.

A workbench (env0) lane has no sandbox: its commands run as the user agentosd runs as. That user
owns AGENTOS_HOME, so a command there can read the vault, the node token (node.toml), every lane's
browser profile and reach the node's own MCP. On a root node it is root; on a hardened node it is the
service user (`agentos`), which brains are kept off for exactly this reason (docs/DEVELOPING.md,
"How brains start without root"). This module says so in words, and is the one place that decides
which lanes count:

- creating one needs `--can-read-node-secrets` (EnvManager.create, `agentos env create`);
- every agent command in one waits for a person, never a standing approval or a routing row
  (`facts`, sent by every acting env tool and the panel terminal; PolicyService._runs_on_node);
- its isolation line says so (`label`), and `agentos doctor` warns while one exists (`doctor_rows`).

A lane made before this rule needs no migration: all of it is worked out from the lane's dials each
time, never from a stored flag.
"""

from __future__ import annotations

import os
from pathlib import Path

from agentos.env.models import EnvLane

FLAG = "--can-read-node-secrets"


def node_user() -> str:
    """The user this process (agentosd, or the CLI on the node) runs as: the user a workbench lane runs as."""
    uid = os.geteuid() if hasattr(os, "geteuid") else -1
    if uid == 0:
        return "root"
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return os.environ.get("USER") or f"uid {uid}"


def reaches_node(lane: EnvLane) -> bool:
    """A command in this lane can read the node's own files (the vault, node.toml, every profile).

    Which dials count, and why (EnvLane.contained holds the rule):
    - filesystem shared: bubblewrap binds the node's `/` read-write, so AGENTOS_HOME is in the lane.
      Whatever the network and process dials say, the files are there to read.
    - processes shared: the lane sees every process on the node, as root with CAP_KILL and
      CAP_SETUID. It can signal agentosd and the browsers, and after a setuid to the user of any
      process with fewer capabilities, open that process's /proc/<pid>/root, which is the node's own
      `/`. A private filesystem does not hold against that, so this counts too.
    - network shared, alone, does not: the lane shares the node's loopback, but every service there
      asks for a secret the lane cannot read (the MCP and API tokens, the Xvfb cookie; x11vnc
      listens on a unix socket under AGENTOS_HOME). Its line already says "the node's network,
      unfiltered", and its commands are asked about as the routing table says for a sandbox.
    A VM lane never reaches the node: nothing of it runs there."""
    return not lane.contained


def shared_dials(lane: EnvLane) -> list[str]:
    """The dials that make a sandboxed lane reach the node, in words ("filesystem", "processes")."""
    p = lane.profile
    return [d for d, v in (("filesystem", p.filesystem), ("processes", p.processes)) if v == "shared"]


def facts(lane: EnvLane) -> dict:
    """What the policy is told about a lane, from the node's own record, never from the caller: the
    routing fact `isolated`, and for a lane that reaches the node `runs_on_node`, the reason a person
    sees every agent command in it (PolicyService._runs_on_node)."""
    out: dict = {"isolated": lane.contained}
    if reaches_node(lane):
        out["runs_on_node"] = must_see(lane)
    return out


def reach_words(user: str | None = None) -> str:
    """One plain sentence: what a command in a lane on the node itself can read."""
    user = user or node_user()
    if user == "root":
        return ("As root, a command here can read and change everything on this computer, including the "
                "vault of passwords, the node token and every lane's signed-in browser profile.")
    return (f"As {user}, the user that owns this node's files, a command here can read the vault of "
            "passwords, the node token and every lane's signed-in browser profile, and can reach the "
            "node's own agent connection.")


def _shares(lane: EnvLane) -> str:
    """"the node's filesystem", "the node's filesystem and processes"."""
    return "the node's " + " and ".join(shared_dials(lane))


def label(user: str | None = None) -> str:
    """The isolation line of a workbench lane."""
    user = user or node_user()
    return (f"env0 (workbench): runs directly on the node as {user}, sharing its filesystem, processes "
            f"and network. No isolation. {reach_words(user)} Every agent command in it waits for a person.")


def sandbox_label(lane: EnvLane, user: str | None = None) -> str:
    """What a sandboxed lane that still reaches the node adds to its isolation line."""
    return (f"It shares {_shares(lane)}, so the sandbox does not keep it off the node. {reach_words(user)} "
            "Every agent command in it waits for a person.")


def create_refusal(lane_id: str, user: str | None = None, lane: EnvLane | None = None) -> str:
    """Why `env create` refused a lane that reaches the node without the flag, and how to say yes."""
    if lane is not None and lane.isolated:
        what = f"{lane_id} would share {_shares(lane)}, so its sandbox would not keep it off the node."
        other = "otherwise leave the filesystem and processes private (overlay or ephemeral, isolated)."
    else:
        what = f"{lane_id} would be a workbench lane, which has no sandbox."
        other = "otherwise use --preset sandbox (needs a root node)."
    return (f"{what} {reach_words(user)} Nothing was created. If that is what you want, run the command "
            f"again with {FLAG}; {other}")


def must_see(lane: EnvLane, user: str | None = None) -> str:
    """The reason a command in such a lane waits for a person, shown on the approval card."""
    if lane.isolated:
        return (f"it runs in lane {lane.id}, which shares {_shares(lane)}, so its sandbox does not keep it "
                f"off the node. {reach_words(user)}")
    return (f"it runs on the node itself with no sandbox (workbench lane {lane.id}). {reach_words(user)}")


def doctor_rows(home: Path) -> list[dict]:
    """`agentos doctor` rows for env lanes, read from env.db without starting the env runtime.
    A WARN while a lane on the node itself exists, and one for isolated lanes this node cannot run."""
    from agentos.env.paths import EnvPaths

    paths = EnvPaths(Path(home))
    if not paths.db.exists():
        return []
    import sqlite3

    try:  # read-only: doctor run as root must not leave root-owned WAL files beside agentosd's database
        conn = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True, timeout=2.0)
        try:
            lanes = [EnvLane.model_validate_json(row[0]) for row in conn.execute("SELECT spec FROM lanes")]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - doctor reports, never crashes
        return [{"id": "env.lanes", "title": "Terminal lanes", "status": "warn",
                 "detail": "could not read the list of terminal and service lanes (env.db)",
                 "fix": "agentos env list (as the node's service user) says why"}]
    rows: list[dict] = []
    open_lanes = sorted(lane.id for lane in lanes if reaches_node(lane))
    if open_lanes:
        names = ", ".join(open_lanes)
        rows.append({"id": "env.workbench", "title": "Lanes that reach the node", "status": "warn",
                     "detail": f"{len(open_lanes)} lane(s) run on the node itself, or share its filesystem "
                               f"or processes: {names}. {reach_words()}",
                     "fix": f"remove each one you do not need: agentos env remove {open_lanes[0]}",
                     "data": {"lanes": open_lanes, "user": node_user()}})
    from agentos.env.manager import isolation_reason

    reason = isolation_reason()
    stuck = sorted(lane.id for lane in lanes if lane.isolated)
    if reason and stuck:
        rows.append({"id": "env.isolation", "title": "Sandboxed lanes", "status": "warn",
                     "detail": f"{len(stuck)} sandboxed lane(s) cannot run on this node ({reason}): "
                               f"{', '.join(stuck)}",
                     "fix": f"remove them (agentos env remove {stuck[0]}), or run agentosd as root",
                     "data": {"lanes": stuck}})
    return rows
