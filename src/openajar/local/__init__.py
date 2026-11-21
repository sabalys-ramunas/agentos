"""openajar local: the node on a person's own computer (macOS or Linux), environments only.

paths     where it keeps its files, and its default port (7410)
home      the home it runs from: node.toml with a port it remembers, the vault's key file
node      the composition: MCP, grants and policy, the vault, the git gateway, VMs and environments
          with their reapers, the audit log and the web app; no browser lanes (openajar.local.lanes)
wake      after a sleep or a restart, the reapers run at once and say what they closed
service   the launchd agent (macOS) or systemd user unit (Linux) that runs `openajar local serve`
register  the agent grant, its token in this computer's store, and Claude Code, Codex and the skills
cli       `openajar local serve | start | stop | status | logs`

docs/LOCAL.md is the person's guide.
"""
