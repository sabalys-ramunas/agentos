"""The env eval's node: agentos's local node (`agentos local serve`'s composition) in a scratch home.

    python -P tests/env_eval/node_main.py prepare <home> <port> <name>   # node.toml, vault, agent grant
    python -P tests/env_eval/node_main.py serve <home> <port> <github api base>

The harness (tests/env_eval/node.py) writes vms.toml (the docker provider), environments.toml, the
declared fixture repositories and the token for them, and runs `serve` with AGENTOS_GIT_ALLOW_FILE=1,
so remotes declared with a local path are served. `prepare` makes the home as `agentos local serve`
does, with the node's own name (so its containers carry a label of their own), and issues the agent
grant `agentos local` gives Claude Code; it prints {"token": ...} on stdout.

The one seam in `serve`: the environments service's GitHub API base points at the run's stand-in
(tests/env_eval/ghstub.py), so environment_pull_request never reaches github.com. It prints
`READY <port>` once MCP listens. Both run the `agentos` the PYTHONPATH gives them, so
`run.py --ref <sha>` puts a commit's node under the same harness.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

AGENT_SUBJECT = "env-eval@harness"
AGENT_TTL_S = 2 * 86400


def prepare(home: Path, port: int, name: str) -> None:
    from agentos.api.bridge import panel_specials
    from agentos.client.choices import DEFAULT_CEILING, DEFAULT_SPECIALS
    from agentos.core.config import NodeConfig, save_config
    from agentos.local import home as local_home
    from agentos.policy.service import PolicyService

    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    save_config(NodeConfig(home=home, name=name, mcp_host="127.0.0.1", mcp_port=port))
    cfg = local_home.ensure(home, port)  # keeps the name, adds the token and the vault
    policy = PolicyService(home, node_config=cfg)
    try:
        _, token = policy.issue(subject={"kind": "agent", "id": AGENT_SUBJECT}, workspaces=["*"],
                                ceiling=DEFAULT_CEILING, specials=panel_specials(list(DEFAULT_SPECIALS)),
                                ttl_s=AGENT_TTL_S, by="owner", note="the env eval's agents")
    finally:
        policy.close()
    print(json.dumps({"token": token}), flush=True)


async def serve(home: Path, port: int, github_api: str) -> None:
    from agentos.core import logs
    from agentos.local import home as local_home
    from agentos.local.node import build_runtime, run
    from agentos.node.daemon import install_log_scrubbers

    cfg = local_home.ensure(home, port)
    logs.setup(home, verbose=False, journal=False)
    install_log_scrubbers(cfg)
    os.environ["AGENTOS_HOME"] = str(home)  # as run() sets it, before the services read their files
    runtime = build_runtime(cfg)
    envs = runtime.get("environments")
    if envs is not None:
        envs.github_api = github_api  # agentos.environments.pulls reads it (the tests' seam)

    def started(bound: int) -> None:
        print(f"READY {bound}", flush=True)

    await run(cfg, runtime=runtime, on_started=started)


if __name__ == "__main__":
    step, where, at = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
    if step == "prepare":
        prepare(where, at, sys.argv[4])
    else:
        try:
            asyncio.run(serve(where, at, sys.argv[4]))
        except KeyboardInterrupt:
            pass
