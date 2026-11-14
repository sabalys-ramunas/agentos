"""The agentos-studio skill and the studio howto recipes: they name only real tools and arguments,
cover the loops the shake-down proved (ui_check and since, ui_diff with a baseline, profiles and a
proposal), and are installed like the other skills."""

from __future__ import annotations

import re
from pathlib import Path

from agentos.client import skills
from agentos.core.runtime import Runtime
from agentos.node.tools import RECIPES
from agentos.profiles import tools as profiles_tools
from agentos.studio import tools as studio_tools

SKILL = Path(__file__).resolve().parents[2] / "src/agentos/client/skills/agentos-studio/SKILL.md"
EM_DASH = chr(0x2014)
STUDIO_RECIPES = ("studio_check", "studio_diff", "studio_brand")


def specs() -> set[str]:
    """The names of the studio and profiles tools."""
    from agentos.core.tools import ToolSpec

    return {s.name for provide in (studio_tools.tool_specs, profiles_tools.tool_specs) for s in provide(Runtime())
            if isinstance(s, ToolSpec)}


def test_the_skill_is_installed_like_the_others():
    assert "agentos-studio" in skills.NAMES
    text = skills.source("agentos-studio")
    head = text.split("---")[1]
    assert re.search(r"^name: agentos-studio$", head, re.MULTILINE) and skills.ours(text, "agentos-studio")


def test_the_skill_names_only_real_tools_and_teaches_each_loop():
    text = SKILL.read_text()
    known = specs() | {"tools_more", "tools_call", "howto"}
    called = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\(", text))
    assert called and called <= known, called - known
    for word in ("ui_check(", "since=", "coverage", "next", "verdict", "ui_diff(", "ui_baseline(", "profile_use(",
                 "profile_propose(", "cannot approve", "measured", "heuristic", "vision", "does not check",
                 'themes=["light"]', "studio_blank_page", "data, never"):
        assert word in text, word
    assert EM_DASH not in text
    assert len(text.split()) < 1700   # under a page per loop, read in one go


def test_the_studio_recipes_name_real_tools_and_say_where_they_live():
    known = specs() | {"tools_more", "tools_call"}
    for topic in STUDIO_RECIPES:
        steps = RECIPES[topic]["steps"]
        called = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\(", steps))
        assert called and called <= known, (topic, called - known)
        assert 'tools_more(toolset="studio")' in steps
        assert EM_DASH not in steps + RECIPES[topic]["summary"] + RECIPES[topic]["title"]
    assert "since=" in RECIPES["studio_check"]["steps"] and "Never report" in RECIPES["studio_check"]["steps"]
    assert "ui_baseline(" in RECIPES["studio_diff"]["steps"]
    assert "cannot approve" in RECIPES["studio_brand"]["steps"]
