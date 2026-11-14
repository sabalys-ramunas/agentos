"""Test totals read from a command's own output: the summary line a test runner prints at its end.

`agentos run` jobs are often test suites, and a page that lists them wants "118 passed, 2 failed"
without reading megabytes. The node does not run the tests and does not know their result: it
reads what the runner printed, so a summary is marked `source: "output"`. When no known runner's
summary is in the text, there is none (None), never a guess.

Known: pytest, vitest, jest, swift test (XCTest and swift-testing) and go test (`-v`, where each
test prints its own line). The text is the end of stdout and stderr; when several runners match,
the one printed last wins.
"""

from __future__ import annotations

import re
from typing import Any

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
NUMBER_WORD = r"(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed|deselected|todo|pending)"

# pytest: "==== 2 failed, 118 passed, 3 skipped in 12.34s ====", or "-q": "118 passed in 3.2s"
PYTEST = re.compile(rf"^[=\s]*((?:{NUMBER_WORD}(?:,\s*)?)+)(?:,\s*\d+\s+warnings?)?\s+in\s+[\d.]+s\b.*$", re.MULTILINE)
# vitest: " Tests  2 failed | 118 passed | 3 skipped (123)"
VITEST = re.compile(r"^\s*Tests\s{2,}((?:\d+\s+\w+\s*\|?\s*)+)\((\d+)\)\s*$", re.MULTILINE)
# jest: "Tests:       2 failed, 3 skipped, 118 passed, 123 total"
JEST = re.compile(r"^\s*Tests:\s+((?:\d+\s+\w+,\s*)*)(\d+)\s+total\s*$", re.MULTILINE)
# XCTest: "Executed 42 tests, with 1 failure (0 unexpected) in 0.512 (0.6) seconds"
XCTEST = re.compile(r"^.*Executed\s+(\d+)\s+tests?,\s+with\s+(\d+)\s+failures?.*$", re.MULTILINE)
# swift-testing: "Test run with 12 tests passed after 0.003 seconds." or "... failed after ... with 2 issues."
SWIFT_TESTING = re.compile(r"^.*Test run with\s+(\d+)\s+tests?(?:\s+in\s+\d+\s+suites?)?\s+(passed|failed)\b.*$", re.MULTILINE)
SWIFT_ISSUES = re.compile(r"with\s+(\d+)\s+issues?")
# go test -v: a line per top-level test
GO_LINE = re.compile(r"^--- (PASS|FAIL|SKIP): \S+", re.MULTILINE)


def _counts(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for n, word in re.findall(NUMBER_WORD, text):
        key = {"error": "failed", "errors": "failed", "xpassed": "passed", "todo": "skipped", "pending": "skipped",
               "xfailed": "skipped", "deselected": "skipped"}.get(word, word)
        out[key] = out.get(key, 0) + int(n)
    return out


def _summary(runner: str, passed: int, failed: int, skipped: int, total: int | None = None) -> dict[str, Any]:
    return {"runner": runner, "passed": passed, "failed": failed, "skipped": skipped,
            "total": total if total is not None else passed + failed + skipped, "source": "output"}


def _last(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    found = None
    for found in pattern.finditer(text):
        pass
    return found


def read(text: str) -> dict[str, Any] | None:
    """The test summary in `text` (the end of a command's output), or None."""
    text = ANSI.sub("", text or "")
    found: list[tuple[int, dict[str, Any]]] = []
    if m := _last(PYTEST, text):
        c = _counts(m.group(1))
        found.append((m.start(), _summary("pytest", c.get("passed", 0), c.get("failed", 0), c.get("skipped", 0))))
    if m := _last(VITEST, text):
        c = _counts(m.group(1))
        found.append((m.start(), _summary("vitest", c.get("passed", 0), c.get("failed", 0), c.get("skipped", 0),
                                          int(m.group(2)))))
    if m := _last(JEST, text):
        c = _counts(m.group(1))
        found.append((m.start(), _summary("jest", c.get("passed", 0), c.get("failed", 0), c.get("skipped", 0),
                                          int(m.group(2)))))
    if m := _last(XCTEST, text):
        total, failed = int(m.group(1)), int(m.group(2))
        found.append((m.start(), _summary("swift", max(0, total - failed), failed, 0, total)))
    if m := _last(SWIFT_TESTING, text):
        total = int(m.group(1))
        issues = SWIFT_ISSUES.search(m.group(0))
        failed = (int(issues.group(1)) if issues else 1) if m.group(2) == "failed" else 0
        failed = min(failed, total)
        found.append((m.start(), _summary("swift", total - failed, failed, 0, total)))
    lines = GO_LINE.findall(text)
    if lines:
        last = list(GO_LINE.finditer(text))[-1]
        found.append((last.start(), _summary("go", lines.count("PASS"), lines.count("FAIL"), lines.count("SKIP"))))
    if not found:
        return None
    return max(found, key=lambda pair: pair[0])[1]
