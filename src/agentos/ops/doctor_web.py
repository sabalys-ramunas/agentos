"""The doctor's web app check: what the node serves at `/`, and whether an installed copy matches
this agentos (the installer updates agentos; `agentos web update` brings the app along).

Kept out of agentos.ops.doctor, which is at its size limit; it registers this in its CHECKS.
"""

from __future__ import annotations

from typing import Any

from agentos.core import webapp


def check_web(ctx: Any) -> list[Any]:
    from agentos.ops.doctor import PASS, SKIP, WARN, CheckResult

    st = webapp.status(ctx.home)
    data = {"source": st.source, "installed": st.installed, "expected": st.expected}
    if st.source in ("env", "checkout"):
        where = "AGENTOS_PANEL_DIR" if st.source == "env" else "this checkout's own build"
        return [CheckResult("web", "Web app", PASS, f"served from {where} ({st.path})", data=data)]
    if st.mismatched:
        return [CheckResult("web", "Web app", WARN,
                            f"version {st.installed} is installed, but agentos is {st.expected}",
                            "agentos web install (it downloads the matching release's copy)", data=data)]
    if st.source == "installed":
        return [CheckResult("web", "Web app", PASS, f"version {st.installed}, installed in {st.path}", data=data)]
    said = " (you said no)" if st.wanted is False else ""
    return [CheckResult("web", "Web app", SKIP, f"not installed{said}; the node works without it",
                        "agentos web install", data=data)]
