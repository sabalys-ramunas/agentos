"""Text an agent saves that will be served to other agents, screened the way workspace memory
screens an untrusted entry (openajar.memory.scan.suspicious_reasons): guidelines, a reviewer's
rubric and examples, and reference notes are handed to agents by profile_use, so text in them
that reads as an instruction to an agent (addresses agents, an override phrase, a link, an
encoded blob) is marked for the owner before they approve it.

Like memory's, it is a hint for the owner, never a security boundary: nothing is refused (a
guideline may well say "always"), the draft's reason carries what was found, and only an
approved version is ever served.
"""

from __future__ import annotations

from typing import Any

from openajar.memory.scan import suspicious_reasons

MAX_SHOWN = 6


def _texts(profile: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for g in getattr(profile, "guidelines", None) or []:
        out[f"guidelines[{g.id}].text"] = g.text
    for r in getattr(profile, "references", None) or []:
        if r.note:
            out[f"references[{r.name}].note"] = r.note
    rev = getattr(profile, "reviewer", None)
    if rev is not None:
        for i, c in enumerate(rev.cares_about):
            out[f"reviewer.cares_about[{i}]"] = c
        for item in rev.rubric:
            out[f"reviewer.rubric[{item.id}].question"] = item.question
        for i, ex in enumerate(rev.examples):
            for part in ("situation", "verdict", "why"):
                text = getattr(ex, part)
                if text:
                    out[f"reviewer.examples[{i}].{part}"] = text
    return out


def instruction_flags(new: Any, old: Any | None = None) -> list[dict[str, Any]]:
    """Each text in `new` that is not already in `old` (the approved version) and reads as an
    instruction to agents: [{field, reasons}]."""
    before = set(_texts(old).values()) if old is not None else set()
    flags = []
    for field, text in _texts(new).items():
        if text in before:
            continue
        reasons = suspicious_reasons(text)
        if reasons:
            flags.append({"field": field, "reasons": reasons})
    return flags


def flags_note(flags: list[dict[str, Any]]) -> str:
    """One paragraph for the owner's approval card."""
    shown = "; ".join(f"{f['field']} ({', '.join(f['reasons'])})" for f in flags[:MAX_SHOWN])
    more = f"; and {len(flags) - MAX_SHOWN} more" if len(flags) > MAX_SHOWN else ""
    return (f"Screened: {len(flags)} text(s) read like an instruction to agents, check them before approving: "
            f"{shown}{more}.")
