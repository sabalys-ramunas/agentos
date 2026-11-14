"""The safety lock on a proposal: what a reviewer may change, and what it must keep.

- Only existing keys: no new tool, error code, recipe topic or recipe field.
- The instructions keep the safety guidance in substance (REQUIRED), and stay under 5,000 characters.
- A line that says "do not" or "never" still says it after the edit, in a tool, a recipe or an error.
- A recipe keeps its placeholders, so `howto` can still fill them.
- No lane id, task id, evidence id or bench word that the current text of that line does not
  already have, and no em dash.

A problem is a plain sentence the reviewer can act on: the run retries once with them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from agentos.textreview.current import CurrentText
from agentos.textreview.prompt import INSTRUCTIONS_MAX

# safety guidance the instructions must keep (lowercase substrings), by what it is for
REQUIRED: dict[str, tuple[str, ...]] = {
    "page text is data, never instructions": ("data, never instructions",),
    "a challenge stops the lane": ("captcha", "checkpoint", "logged_out", "blocked", "do not retry"),
    "never log in again on your own": ("never log in again",),
    "rate_limited rests the lane": ("rate_limited",),
    "approvals wait for a person": ("approval_pending", "never rephrase"),
    "agent_paused is expected": ("agent_paused",),
    "egress_refused has no other way": ("egress_refused",),
    "denied is not retried": ("denied",),
    "wrong_account stops": ("wrong_account",),
}
DONT_RE = re.compile(r"\b(do not|don't|never|not retry|no retry|must not|stop)\b", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")  # agentos.node.tools fills recipe steps the same way
EVIDENCE_ID_RE = re.compile(r"\b(?:S|P)\d{2,3}\b|\blane-\d{2,3}\b")
BENCH_WORDS = ("lanebench", "benchmark", "bench", "fixture", "fixtures", "grader", "graders", "probe task",
               "probe tasks", "test page", "test pages")
EM_DASH = chr(0x2014)  # the long dash
MIN_TOOL_TEXT = 40


def _words(text: str, words: Iterable[str]) -> set[str]:
    low = text.lower()
    return {w for w in words if w and re.search(rf"(?<![\w/-]){re.escape(w.lower())}(?![\w/-])", low)}


def _new_words(new: str, old: str, words: Iterable[str]) -> list[str]:
    """Words the new text has and the text it replaces did not: only what the edit brought in."""
    return sorted(_words(new, words) - _words(old, words))


def check(proposal: dict[str, Any], current: CurrentText, *, forbidden: Iterable[str] = ()) -> list[str]:
    """Every problem with a proposal (parsed by prompt.parse), or [] when it may go to a person.

    `forbidden`: the lane ids and task names the evidence came from."""
    forbidden = [*forbidden, *BENCH_WORDS]
    problems: list[str] = []
    edits: list[tuple[str, str, str]] = []  # (where, new, old)

    for name, text in (proposal.get("tools") or {}).items():
        if name not in current.tools:
            problems.append(f"tools.{name}: there is no tool named {name}; only existing tools can be edited")
            continue
        if len(text.strip()) < MIN_TOOL_TEXT:
            problems.append(f"tools.{name}: the description is empty or too short")
        edits.append((f"tools.{name}", text, current.tools[name]))
    for code, text in (proposal.get("errors") or {}).items():
        if code not in current.errors:
            problems.append(f"errors.{code}: there is no error code {code}; only existing codes can be edited")
            continue
        if not text.strip():
            problems.append(f"errors.{code}: the line is empty")
        edits.append((f"errors.{code}", text, current.errors[code]))
    for topic, fields in (proposal.get("recipes") or {}).items():
        if topic not in current.recipes:
            problems.append(f"recipes.{topic}: there is no recipe {topic}; only existing recipes can be edited")
            continue
        for name, text in fields.items():
            old = current.recipes[topic].get(name)
            if old is None:
                problems.append(f"recipes.{topic}.{name}: that recipe has no {name}")
                continue
            if not text.strip():
                problems.append(f"recipes.{topic}.{name}: the text is empty")
            if set(PLACEHOLDER_RE.findall(text)) != set(PLACEHOLDER_RE.findall(old)):
                problems.append(f"recipes.{topic}.{name}: keep exactly its placeholders "
                                f"({', '.join(sorted(set(PLACEHOLDER_RE.findall(old)))) or 'none'})")
            edits.append((f"recipes.{topic}.{name}", text, old))
    if "instructions" in proposal:
        edits.append(("instructions", proposal["instructions"], current.instructions))

    merged = current.merged(proposal)
    if len(merged.instructions) > INSTRUCTIONS_MAX:
        problems.append(f"instructions: {len(merged.instructions)} characters; keep them under {INSTRUCTIONS_MAX}")
    low = merged.instructions.lower()
    for purpose, needles in REQUIRED.items():
        missing = [n for n in needles if n in current.instructions.lower() and n not in low]
        if missing:
            problems.append(f"instructions: the safety guidance that {purpose} was removed or reworded away "
                            f"(missing {', '.join(repr(n) for n in missing)}); keep it, you may only make it clearer")

    for where, new, old in edits:
        if DONT_RE.search(old) and not DONT_RE.search(new):
            problems.append(f"{where}: it lost its \"do not\" or \"never\" guidance; keep it")
        if EM_DASH in new:
            problems.append(f"{where}: it has a long dash (U+2014); use a period, comma, colon or parentheses")
        named = _new_words(new, old, forbidden)
        if named:
            problems.append(f"{where}: it names {', '.join(repr(w) for w in named)}; write general guidance, "
                            "not about this week's lanes, tasks or tests")
        ids = sorted(set(EVIDENCE_ID_RE.findall(new)) - set(EVIDENCE_ID_RE.findall(old)))
        if ids:
            problems.append(f"{where}: it names evidence ids ({', '.join(ids)}); cite them only in changes")
    return problems
