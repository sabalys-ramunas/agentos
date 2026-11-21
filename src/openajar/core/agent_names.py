"""An agent's name as a person reads it: one rule for the web app, the live view and the desktop's top bar.

Two raw values say who an agent is. The grant's subject (`claude-code`, or `claude-code@mac-mini` for a grant
`openajar connect` or `openajar local` made on that computer) and the MCP client it connected with (its
clientInfo, `claude-code 2.1.328`). A person reads neither: a known client by its product name ("Claude
Code"), an unknown one cleaned up ("My bot"), never a version, and the computer only as words of its own
("alex macbook pro 3"), where it helps tell two agents apart. The raw values stay for Technical details.

The web app shows what the API sends from here (an environment's `agent.said`, `agent.via`, `agent.where`);
its principal names (web/src/lib/principal.ts) follow the same table, and the cases both sides must pass are
in web/src/lib/agent-names.cases.json.
"""

from __future__ import annotations

from typing import Any

# What an agent or its MCP client calls itself (grant subject or clientInfo.name), as a person knows it.
KNOWN = {
    "claude-code": "Claude Code", "claude": "Claude", "claude-ai": "Claude", "claude-app": "Claude",
    "claude-desktop": "Claude", "codex": "Codex", "codex-cli": "Codex", "codex-mcp-client": "Codex",
    "chatgpt": "ChatGPT", "chatgpt-app": "ChatGPT", "openai": "ChatGPT", "t3": "T3 Code", "t3-code": "T3 Code",
    "t3code": "T3 Code", "cursor": "Cursor", "cursor-vscode": "Cursor", "gemini-cli": "Gemini CLI",
    "vscode": "VS Code", "visual-studio-code": "VS Code", "windsurf": "Windsurf", "zed": "Zed",
}
# Names that say no one in particular: the environment's default, and the owner's own token.
UNNAMED = frozenset({"", "an agent", "agent", "owner"})
MAX_NAME = 32


def _plain(text: str) -> str:
    """Dashes and underscores as spaces, nothing unprintable, one space between words."""
    text = "".join(c for c in text.replace("-", " ").replace("_", " ") if c.isprintable())
    return " ".join(text.split())


def product(raw: Any) -> str | None:
    """"claude-code 2.1.328", "claude-code" or "claude-code@mac-mini" as "Claude Code": the known product, else
    the name cleaned up and capitalised ("my-bot 0.1" is "My bot"). None when it says no one."""
    if not isinstance(raw, str):
        return None
    words = raw.split("@", 1)[0].split()
    if len(words) > 1 and words[-1][:1].isdigit():
        words = words[:-1]  # the version
    base = " ".join(words)
    if base.strip().lower() in UNNAMED:
        return None
    known = KNOWN.get("-".join(base.lower().replace("_", "-").split()))
    if known:
        return known
    name = _plain(base)[:MAX_NAME].strip()
    return (name[:1].upper() + name[1:]) if name else None


def where(subject: Any) -> str | None:
    """The computer a grant was made for, in words as its name has them: `claude-code@alex-macbook-pro-3` is
    "alex macbook pro 3". None when the subject names none."""
    if not isinstance(subject, str) or "@" not in subject:
        return None
    return _plain(subject.split("@", 1)[1])[:MAX_NAME].strip() or None


def said(subject: Any, client: Any = None) -> str:
    """Who an agent is in one name: its subject's product, else its client's, else "An agent"."""
    return product(subject) or product(client) or "An agent"


def via(subject: Any, client: Any) -> str | None:
    """The app an agent came in through, when it is another product than the agent (Claude Code in T3 Code).
    None when it is the same one, or the agent is only known by its client."""
    came = product(client)
    return came if came and product(subject) and came != said(subject, client) else None


def agent_view(subject: Any, client: Any, *, named: str | None = None) -> dict[str, Any]:
    """An environment's `agent` for the API: the raw `name` and `client` (Technical details) and the words
    `said` ("Claude Code"), `via` ("T3 Code" or null) and `where` ("alex macbook pro 3" or null). `named` is
    the name the live view already gives (openajar.node.viewer_state.env_agent_name), so the two agree."""
    name = named or said(subject, client)
    came = product(client) if product(subject) else None  # an agent known only by its client came in through it
    return {"name": subject, "client": client, "said": name, "via": came if came and came != name else None,
            "where": where(subject)}
