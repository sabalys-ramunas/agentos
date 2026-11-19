"""File tools for terminal lanes: file_read, file_write and file_edit.

An agent codes on its environment's machine as it does on its own computer: read a file with line
numbers, write one, replace an exact string in one. Each is one command over the transport
terminal_exec uses (agentos.env.manager.exec), so it works on every terminal lane, an environment's VM
included, and is authorized as a command (Effect.EXEC with the lane's facts): on a lane that reaches
the node, reading a file there is as sensitive as running `cat`.

Paths: absolute ones as they are; a relative one resolves against the environment's workdir (the
repository) on an environment's machine, else against the lane's home (where a command starts).
Text only: a file with a NUL byte in its first 8 KB, or that is not UTF-8, is refused with what it
is. Sizes are bounded (READ_CHARS a read, WRITE_MAX a write, EDIT_MAX for the file an edit rewrites).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import posixpath
import shlex
from typing import Any

from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Effect
from agentos.core.tools import ToolContext, ToolSpec

READ_LINES = 2000          # lines a read returns by default
READ_CHARS = 40000         # characters a read returns at most; it stops at a whole line
LINE_CHARS = 2000          # a longer line is cut, with how much was left out
WRITE_MAX = 1 << 20        # bytes one write may carry
EDIT_MAX = 1 << 20         # the largest file an edit reads and writes back
SNIFF_BYTES = 8192         # where a NUL byte makes a file binary
FILE_TIMEOUT_S = 30.0
SNIPPET_LINES = 3          # context lines shown around an edit
PATH_MAX = 4096
MARK = "agentos-file"

# Each script prints one header line (MARK, a word, then numbers) and then, for a read, the content.
_HEAD = (f'if [ -d "$f" ]; then echo "{MARK} dir"; exit 0; fi; '
         f'if [ ! -e "$f" ]; then echo "{MARK} missing"; exit 0; fi; '
         f'if [ ! -r "$f" ]; then echo "{MARK} unreadable"; exit 0; fi; '
         'size=$(wc -c < "$f"); '
         f'nul=$(head -c {SNIFF_BYTES} "$f" | LC_ALL=C tr -dc \'\\000\' | wc -c); ')


def _read_script(path: str, first: int, last: int, cap: int) -> str:
    return (f"f={shlex.quote(path)}; {_HEAD}lines=$(wc -l < \"$f\"); endnl=$(tail -c 1 \"$f\" | wc -l); "
            f'echo "{MARK} ok $size $nul $lines $endnl"; '
            f'if [ "$nul" -eq 0 ]; then sed -n "{first},{last}p;{last}q" "$f" | head -c {cap}; fi')


def _whole_script(path: str) -> str:
    return (f"f={shlex.quote(path)}; {_HEAD}"
            f'echo "{MARK} ok $size $nul"; '
            f'if [ "$nul" -eq 0 ] && [ "$size" -le {EDIT_MAX} ]; then cat "$f"; fi')


def _write_script(path: str, expect_sha: str | None = None) -> str:
    guard = ""
    if expect_sha is not None:  # the file must still be what the edit read
        guard = (f'if command -v sha256sum >/dev/null 2>&1 && [ "$(sha256sum < "$f" | cut -c1-64)" != '
                 f'"{expect_sha}" ]; then echo "{MARK} changed"; exit 0; fi; ')
    return (f"f={shlex.quote(path)}; "
            f'if [ -d "$f" ]; then echo "{MARK} dir"; exit 0; fi; '
            'if [ -e "$f" ]; then state=updated; else state=created; fi; '
            f'{guard}mkdir -p -- "$(dirname -- "$f")" && cat > "$f" && echo "{MARK} $state"')


# ------------------------------------------------------------------ helpers
def _mgr(ctx: ToolContext) -> Any:
    return ctx.runtime.require("envs")


def _workdir(ctx: ToolContext, lane_id: str) -> str | None:
    """The repository's folder when the lane is an environment's machine (the environments service
    knows it), else None: the lane's home."""
    svc = ctx.runtime.get("environments") if callable(getattr(ctx.runtime, "get", None)) else None
    find = getattr(svc, "workdir_of", None)
    if not callable(find):
        return None
    try:
        return find(lane_id)
    except Exception:  # noqa: BLE001 - a relative path then starts at the lane's home, as a command does
        return None


def resolve_path(path: Any, workdir: str | None) -> str:
    """The path a command on the lane uses: an absolute one normalized, a relative one under the
    workdir (left relative when there is none: it starts at the lane's home)."""
    if not isinstance(path, str) or not path.strip():
        raise InvalidRequest('path is a file, like "src/app.py" or "/work/shop/src/app.py"')
    if len(path) > PATH_MAX or any(c in path for c in "\x00\n\r"):
        raise InvalidRequest("path is one line of at most 4096 characters")
    if path == "~" or path.startswith("~/"):
        raise InvalidRequest("give an absolute path, or one relative to the repository (not ~)")
    if path.startswith("/"):
        return posixpath.normpath(path)
    if workdir:
        return posixpath.normpath(posixpath.join(workdir, path))
    return posixpath.normpath(path)


def _raw(res: Any) -> bytes:
    if getattr(res, "stdout_encoding", "utf-8") == "base64":
        try:
            return base64.b64decode(res.stdout)
        except (ValueError, binascii.Error):
            return b""
    return (res.stdout or "").encode()


def _header(res: Any, path: str) -> tuple[list[str], bytes]:
    """(the header's words after MARK, the rest of stdout). Raises with what the file is."""
    raw = _raw(res)
    first, _, rest = raw.partition(b"\n")
    words = first.decode("utf-8", "replace").split()
    if not words or words[0] != MARK:
        said = (res.stderr or res.stdout or "").strip()[-300:]
        raise InvalidRequest(f"could not reach {path} on the lane (exit {res.exit_code}): {said or 'no answer'}")
    kind = words[1] if len(words) > 1 else ""
    if kind == "missing":
        raise NotFound(f"no file {path}; terminal_exec(cmd=\"ls\") lists a folder, file_write makes a file")
    if kind == "dir":
        raise InvalidRequest(f"{path} is a folder, not a file; list it with terminal_exec(cmd=\"ls -la {path}\")")
    if kind == "unreadable":
        raise InvalidRequest(f"{path} cannot be read by the lane's user")
    return words[1:], rest


def _numbers(words: list[str]) -> list[int]:
    try:
        return [int(w) for w in words[1:]]
    except ValueError:
        raise InvalidRequest(f"the lane answered {' '.join(words)!r} about the file") from None


def _binary(path: str, size: int) -> InvalidRequest:
    return InvalidRequest(f"{path} is a binary file ({size} bytes), not text, so it is not read or edited "
                          "here; use terminal_exec (file, xxd, base64) for it")


def _decode(data: bytes, path: str, size: int, *, cut: bool) -> str:
    """UTF-8 text. `cut`: the end may stop inside a character (a read stopped at its cap)."""
    if cut:
        from agentos.env.execs import _trim_utf8_end

        data, _ = _trim_utf8_end(data)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidRequest(f"{path} is not UTF-8 text ({size} bytes), so it is not read or edited here; use "
                             "terminal_exec (iconv, file) for it") from None


def _numbered(lines: list[str], first: int) -> str:
    out = []
    for i, line in enumerate(lines):
        if len(line) > LINE_CHARS:
            line = line[:LINE_CHARS] + f" [agentos: {len(line) - LINE_CHARS} more characters on this line]"
        out.append(f"{first + i:>6}\t{line}")
    return "\n".join(out)


def _sha(data: str | bytes) -> str:
    b = data.encode() if isinstance(data, str) else data
    return hashlib.sha256(b).hexdigest()


PREVIEW_CHARS = 240   # of a write's text, or an edit's old and new, in the summary a person approves


def _preview(text: str, chars: int = PREVIEW_CHARS) -> str:
    """The start of a text as it would be written, quoted with its line breaks shown (\\n), so a person
    approving a write on the node sees what goes in, not only its hash."""
    shown = json.dumps(text[:chars], ensure_ascii=False)
    return shown + (f" (+{len(text) - chars} more characters)" if len(text) > chars else "")


async def _authorize(ctx: ToolContext, on: str, summary: str, content: dict, *, preview: str = "") -> Any:
    """As terminal_exec authorizes a command on this lane (agentos.env.tools). The summary says where it
    runs first, then what it writes, so a long text never pushes the place out of the card."""
    from agentos.env.tools import _clip, _facts, _where

    mgr = _mgr(ctx)
    lane = mgr.get(on)
    mgr.check_runnable(lane)
    text = f"{summary} {_where(lane)}" + (f". {preview}" if preview else "")
    await ctx.authorize(Effect.EXEC, lane_id=on, summary=_clip(text), content=content, facts=_facts(lane))
    return mgr


async def _run(ctx: ToolContext, mgr: Any, on: str, script: str, *, stdin: str | None = None,
               max_bytes: int) -> Any:
    from agentos.env.tools import _grant_id

    return await mgr.exec(on, script, timeout_s=FILE_TIMEOUT_S, stdin=stdin, grant_id=_grant_id(ctx),
                          max_bytes=max_bytes)


# ------------------------------------------------------------------ tools
async def lane_file_read(ctx: ToolContext, *, on: str, path: str | None = None, paths: list[str] | None = None,
                         offset: int = 1, limit: int = READ_LINES) -> dict:
    """Read a text file with line numbers (as cat -n); a relative path starts at the repository.
    Returns content, lines, total_lines and next_offset when there is more (at most 2000 lines or
    40000 characters a call). `paths` reads several files in one call (up to 20; a * in the last part,
    like "src/*.js", reads each match), each with its lines, within the same 40000 characters. Binary
    files are refused. Effect: exec. Errors: not_found, invalid.

    Args:
        path: One file, like "src/app.py".
        paths: Several files, like ["package.json", "src/*.js"], instead of path.
        offset: The first line (1 is the first).
        limit: How many lines.
    """
    try:
        first, count = max(1, int(offset)), max(1, min(int(limit), 100000))
    except (TypeError, ValueError):
        raise InvalidRequest("offset and limit are line counts, like offset=1, limit=200") from None
    workdir = _workdir(ctx, on)
    if paths is not None:
        return await _read_many(ctx, on, workdir, paths if path is None else [path, *paths], first, count)
    if path is None:
        raise InvalidRequest('give path (one file) or paths (several), like path="src/app.py"')
    target = resolve_path(path, workdir)
    mgr = await _authorize(ctx, on, f"Read {target}", {"read": target, "offset": first, "limit": count})
    return await _read_one(ctx, mgr, on, target, first, count, READ_CHARS)


async def _read_one(ctx: ToolContext, mgr: Any, on: str, target: str, first: int, count: int, budget: int) -> dict:
    """One file's lines from `first`, at most `count` of them and about `budget` characters."""
    cap = budget * 4 + 4096  # bytes; the characters are counted below
    res = await _run(ctx, mgr, on, _read_script(target, first, first + count - 1, cap), max_bytes=cap + 512)
    words, body = _header(res, target)
    size, nul, newlines, ends = _numbers(words)
    if nul:
        raise _binary(target, size)
    text = _decode(body, target, size, cut=len(body) >= cap)
    total = newlines + (1 if size and not ends else 0)  # a last line without a newline counts too
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    shown, chars = [], 0
    for line in lines:
        cost = min(len(line), LINE_CHARS) + 8
        if shown and chars + cost > budget:
            break
        shown.append(line)
        chars += cost
    if len(body) >= cap and len(shown) == len(lines) and len(shown) > 1:
        shown.pop()  # the last line may be cut by the cap: it comes again with next_offset
    last = first + len(shown) - 1
    out: dict[str, Any] = {"path": target, "size": size, "total_lines": total,
                           "lines": f"{first}-{last}" if shown else None, "content": _numbered(shown, first)}
    if not shown:
        out["note"] = "empty file" if size == 0 else f"the file has {total} lines; offset {first} is past its end"
    if shown and last < total:
        out["next_offset"] = last + 1
    return out


MANY_MAX = 20     # files one read of several returns
GLOB_CHARS = "*?["


def _glob_script(patterns: list[str]) -> str:
    """The files each pattern's last part matches (find -name, so no shell expands anything), one a line."""
    parts = [f"find {shlex.quote(posixpath.dirname(p) or '.')} -maxdepth 1 -type f "
             f"-name {shlex.quote(posixpath.basename(p))} 2>/dev/null | LC_ALL=C sort | head -n {MANY_MAX + 1}"
             for p in patterns]
    return f'echo "{MARK} ok"; ' + "; ".join(parts)


async def _read_many(ctx: ToolContext, on: str, workdir: str | None, paths: Any, first: int, count: int) -> dict:
    """file_read(paths=[...]): each file in turn within one READ_CHARS budget; a file that cannot be read
    says why in its own entry, and files past the budget are named so the agent reads them next."""
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise InvalidRequest('paths is a list of files, like ["package.json", "src/*.js"]')
    targets = [resolve_path(p, workdir) for p in paths[:MANY_MAX]]
    mgr = await _authorize(ctx, on, f"Read {', '.join(targets)}"[:300],
                           {"read": targets, "offset": first, "limit": count})
    globs = [t for t in targets if any(c in posixpath.basename(t) for c in GLOB_CHARS)]
    found: dict[str, list[str]] = {}
    if globs:
        res = await _run(ctx, mgr, on, _glob_script(globs), max_bytes=64 * 1024)
        _, body = _header(res, "the patterns")
        matched = body.decode("utf-8", "replace").splitlines()
        found = {g: [m for m in matched if posixpath.dirname(m) == (posixpath.dirname(g) or ".")] for g in globs}
    files: list[str] = []
    for t in targets:
        files += found.get(t, []) if t in found else [t]
    files = list(dict.fromkeys(files))
    out: dict[str, Any] = {"files": []}
    left = READ_CHARS
    for i, target in enumerate(files[:MANY_MAX]):
        if left < LINE_CHARS:
            out["not_read"] = files[i:MANY_MAX]
            out["note"] = "The reply is full: read these with file_read(paths=[...]) next."
            break
        try:
            one = await _read_one(ctx, mgr, on, target, first, count, left)
        except (InvalidRequest, NotFound) as exc:
            out["files"].append({"path": target, "error": f"{exc.code}: {exc.message}"})
            continue
        left -= len(one["content"]) + 64
        out["files"].append(one)
    empty = [g for g in globs if not found.get(g)]
    if empty:
        out["no_match"] = empty
    if len(files) > MANY_MAX or len(paths) > MANY_MAX:
        out["note"] = (out.get("note", "") + f" At most {MANY_MAX} files a call.").strip()
    return out


async def lane_file_write(ctx: ToolContext, *, on: str, path: str, content: str) -> dict:
    """Write a whole text file (at most 1 MB), replacing it; folders are made. A relative path
    starts at the repository. To change part of one, file_edit. Effect: exec."""
    if not isinstance(content, str):
        raise InvalidRequest("content is the file's text")
    data = content.encode()
    if len(data) > WRITE_MAX:
        raise InvalidRequest(f"content is {len(data)} bytes; one write carries at most {WRITE_MAX}. Write it in "
                             "parts with terminal_exec, or generate it on the machine")
    target = resolve_path(path, _workdir(ctx, on))
    mgr = await _authorize(ctx, on, f"Write {len(data)} bytes to {target}",
                           {"write": target, "content": "sha256:" + _sha(data)},
                           preview=f"Text: {_preview(content)}")
    res = await _run(ctx, mgr, on, _write_script(target), stdin=content, max_bytes=4096)
    words, _ = _header(res, target)
    if words[0] not in ("created", "updated"):
        raise InvalidRequest(f"could not write {target}: {(res.stderr or '').strip()[-300:] or words[0]}")
    ctx.trace("file_write", lane=on, path=target, bytes=len(data))
    return {"path": target, "state": words[0], "bytes": len(data),
            "lines": content.count("\n") + (1 if content and not content.endswith("\n") else 0)}


async def lane_file_edit(ctx: ToolContext, *, on: str, path: str, old: str, new: str,
                         replace_all: bool = False) -> dict:
    """Replace exact text in a file (a relative path starts at the repository). `old` must occur
    once, with its whitespace, unless replace_all=true; else nothing changes and the error says how
    many times it occurs. Returns replaced and snippet (the changed lines). Effect: exec. Errors:
    not_found, invalid."""
    if not isinstance(old, str) or not old:
        raise InvalidRequest("old is the exact text to replace; to write a whole file use file_write")
    if not isinstance(new, str):
        raise InvalidRequest("new is the text to put in place of old")
    if old == new:
        raise InvalidRequest("old and new are the same, so there is nothing to change")
    target = resolve_path(path, _workdir(ctx, on))
    mgr = await _authorize(ctx, on, f"Edit {target}",
                           {"edit": target, "old": "sha256:" + _sha(old), "new": "sha256:" + _sha(new),
                            "replace_all": bool(replace_all)},
                           preview=f"Replace {_preview(old, PREVIEW_CHARS // 2)} with {_preview(new, PREVIEW_CHARS // 2)}")
    res = await _run(ctx, mgr, on, _whole_script(target), max_bytes=EDIT_MAX + 4096)
    words, body = _header(res, target)
    size, nul = _numbers(words)[:2]
    if nul:
        raise _binary(target, size)
    if size > EDIT_MAX:
        raise InvalidRequest(f"{target} is {size} bytes; an edit reads at most {EDIT_MAX}. Change it with "
                             "terminal_exec (sed, a script)")
    text = _decode(body, target, size, cut=False)
    found = text.count(old)
    if found == 0:
        raise InvalidRequest(f"old was not found in {target}. Read the lines with file_read and copy them "
                             "exactly, whitespace and indentation included")
    if found > 1 and not replace_all:
        raise InvalidRequest(f"old occurs {found} times in {target}, so it is not changed. Add surrounding lines "
                             "so it occurs once, or pass replace_all=true")
    start = text.index(old)
    changed = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    res = await _run(ctx, mgr, on, _write_script(target, _sha(body)), stdin=changed, max_bytes=4096)
    words, _ = _header(res, target)
    if words[0] == "changed":
        raise InvalidRequest(f"{target} changed on the machine while it was edited, so it was not written. Read "
                             "it again and repeat the edit")
    if words[0] not in ("created", "updated"):
        raise InvalidRequest(f"could not write {target}: {(res.stderr or '').strip()[-300:] or words[0]}")
    ctx.trace("file_edit", lane=on, path=target, replaced=found if replace_all else 1)
    return {"path": target, "replaced": found if replace_all else 1, "snippet": _snippet(changed, start, new)}


def _snippet(text: str, start: int, new: str) -> str:
    """The lines around the first replacement, numbered, at most about 40 of them."""
    lines = text.split("\n")
    first = text.count("\n", 0, start)
    last = first + new.count("\n")
    lo, hi = max(0, first - SNIPPET_LINES), min(len(lines), last + SNIPPET_LINES + 1)
    if hi - lo > 40:
        hi = lo + 40
    return _numbered(lines[lo:hi], lo + 1)


def tool_specs(runtime: Any) -> list[ToolSpec]:
    t = ("env", "terminal")
    return [
        ToolSpec("file_read", lane_file_read, Effect.EXEC, tags=t, hidden_without="envs"),
        ToolSpec("file_write", lane_file_write, Effect.EXEC, tags=(*t, "changes_state"), hidden_without="envs"),
        ToolSpec("file_edit", lane_file_edit, Effect.EXEC, tags=(*t, "changes_state"), hidden_without="envs"),
    ]
