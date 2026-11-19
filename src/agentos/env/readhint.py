"""A one-line nudge in terminal_exec's result when the command only read a file: file_read does that.

Agents read files through terminal_exec (`cat src/price.js`, `head -50 README.md`, `sed -n '1,40p' f`) even
with file_read in their list (the evals, 2026-11-18: Claude Haiku did it in both its tasks). The
file tools number the lines, page long files and are what file_edit expects. The command still
runs as it is; its result only adds `hint`. A command that does anything else (a pipe into grep, a
redirect, a heredoc, a test run) gets nothing, so an ordinary command costs no token.
"""

from __future__ import annotations

import re
import shlex

READERS = frozenset({"cat", "head", "nl", "less", "more", "bat"})
AFTER = frozenset({"head", "tail", "nl", "cat"})  # what may follow a reader in a pipe: still just the file
KEYWORDS = frozenset({"do", "then", "else", "{"})
TAKES_VALUE = {"head": frozenset({"-n", "-c", "--lines", "--bytes"}), "sed": frozenset({"-e", "-f"})}
STATEMENT = re.compile(r"&&|\|\||;|\n")
SKIP = re.compile(r"(?<!2)>|<|\$\(|`")  # a redirect, a heredoc, a command substitution: not a plain read
QUIET = re.compile(r"^2>(&1|/dev/null)$")  # stderr away: still a plain read
HARMLESS = frozenset({"echo", "printf", "for", "done", "fi", "}", "true", "ls", "pwd", "cd"})


def read_path(cmd: str) -> str | None:
    """The file a command reads and does nothing else with, "" when it reads one it names through a
    variable or a glob, None when it does more than read."""
    found: str | None = None
    for statement in STATEMENT.split(cmd or ""):
        statement = statement.strip()
        if not statement:
            continue
        if SKIP.search(statement):
            return None
        path = _pipeline(statement)
        if path is None:
            if _harmless(statement):
                continue
            return None
        found = path if found is None else found
    return found


def _harmless(statement: str) -> bool:
    """A statement around the reads that does nothing of its own: an echo of a heading, a loop's words."""
    words = [w for w in statement.split() if w not in KEYWORDS]
    return not words or words[0] in HARMLESS


def _pipeline(statement: str) -> str | None:
    parts = [p.strip() for p in statement.split("|")]
    try:
        words = [shlex.split(p) for p in parts]
    except ValueError:
        return None
    if not words or not all(words):
        return None
    head = words[0]
    while head and head[0] in KEYWORDS:
        head = head[1:]
    if not head or any(w[0] not in AFTER or _files(w[0], w[1:]) for w in words[1:]):
        return None
    files = _files(head[0].rsplit("/", 1)[-1], head[1:])
    return files[0] if files else None


def _files(tool: str, args: list[str]) -> list[str]:
    """The files a reader names; [] when it is no reader (sed only as `sed -n`)."""
    if tool == "sed":
        if "-n" not in args:
            return []
        args = [a for a in args if a != "-n"]
        args = args[1:] if args and not args[0].startswith("-") else args  # the script
    elif tool not in READERS:
        return []
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in TAKES_VALUE.get(tool, ()):
            skip = True
            continue
        if a.startswith("-") or QUIET.match(a):
            continue
        out.append("" if any(c in a for c in "$*?[") else a)
    return out


def file_read_hint(cmd: str, cwd: str | None = None) -> str | None:
    """`hint` for terminal_exec's result, or None."""
    path = read_path(cmd)
    if path is None:
        return None
    if path and cwd and not path.startswith("/"):
        path = f"{cwd.rstrip('/')}/{path}"
    arg = f'path="{path}"' if path else "path=..."
    return f"file_read({arg}) reads a file with line numbers, as file_edit expects."
