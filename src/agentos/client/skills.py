"""The Claude Code skills that come with agentos (agentos-environment, agentos-lanes, agentos-offload,
agentos-studio).

They ship inside the package (`agentos/client/skills/<name>/SKILL.md`), so an installed
`agentos` with no checkout has them. `agentos connect` copies them into Claude Code's skills
folder, `$CLAUDE_CONFIG_DIR/skills` or `~/.claude/skills`, when Claude Code is here; the last
`agentos connect --remove` takes them out again. A skill folder is ours only when its SKILL.md
says `name: <the skill>`; anything else in the folder is left alone.
"""

from __future__ import annotations

import os
import re
from importlib import resources
from pathlib import Path

NAMES = ("agentos-environment", "agentos-lanes", "agentos-offload", "agentos-studio")


def skills_dir(home: Path) -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(env).expanduser() if env else home / ".claude") / "skills"


def source(name: str) -> str:
    return (resources.files("agentos.client") / "skills" / name / "SKILL.md").read_text()


def state(home: Path) -> str:
    """"installed", "outdated" or "missing", for every skill together."""
    dest = skills_dir(home)
    found = [(dest / n / "SKILL.md") for n in NAMES]
    if not all(p.is_file() for p in found):
        return "missing"
    same = all(p.read_text() == source(n) for p, n in zip(found, NAMES, strict=True))
    return "installed" if same else "outdated"


def ours(text: str, name: str) -> bool:
    """A SKILL.md agentos wrote: its frontmatter names the skill and says agentos ships it."""
    head = text.split("---", 2)[1] if text.startswith("---") and text.count("---") >= 2 else ""
    return bool(re.search(rf"^name: {re.escape(name)}\s*$", head, re.MULTILINE)) and "agentos" in head.lower()


def install(home: Path) -> str:
    """Copy or update every skill. Returns "installed", "updated", "already installed", or says
    which ones it kept because someone else wrote them."""
    before = state(home)
    if before == "installed":
        return "already installed"
    kept = []
    for name in NAMES:
        target = skills_dir(home) / name / "SKILL.md"
        if target.is_file() and target.read_text() != source(name) and not ours(target.read_text(), name):
            kept.append(name)  # the owner's own file under our name: never overwritten
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source(name))
    if kept:
        return f"kept your own {', '.join(kept)}"
    return "updated" if before == "outdated" else "installed"


def refresh(home: Path) -> str | None:
    """After an update: the installed agentos skills brought up to this agentos's copies (install, which
    keeps a file someone else wrote under our name). Only where agentos installed them before (one of
    ours is there): a person who never installed them, or removed them, gets none. None: nothing to do.
    The local service runs it when it starts (agentos.local.cli serve)."""
    dest = skills_dir(home)
    present = [n for n in NAMES if (dest / n / "SKILL.md").is_file() and ours((dest / n / "SKILL.md").read_text(), n)]
    if not present or state(home) == "installed":
        return None
    return install(home)


def remove(home: Path) -> list[str]:
    removed = []
    for name in NAMES:
        target = skills_dir(home) / name / "SKILL.md"
        if target.is_file() and ours(target.read_text(), name):
            target.unlink()
            try:
                target.parent.rmdir()
            except OSError:
                pass  # something else of the owner's is in it
            removed.append(name)
    return removed
