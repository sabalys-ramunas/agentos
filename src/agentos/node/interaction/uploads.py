"""upload: attach files from the lane inbox to a file input, and read back what the input holds.
"""

from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agentos.core.errors import AgentosError, EngineError, InvalidRequest, RefNotFound
from agentos.core.models import Signal
from agentos.core.text import first_line
from agentos.node import human
from agentos.node.human import quietly
from agentos.node.interaction.actions import READBACK_POLL_MS
from agentos.node.interaction.dialogs import DialogGuard, _DialogOpened, _pending_error, dialog_guard
from agentos.node.interaction.urls import normalize_ref

Page = Any           # playwright.async_api.Page
ElementHandle = Any  # playwright.async_api.ElementHandle


CHOOSER_TIMEOUT_MS = 5_000
UPLOAD_READBACK_MS = 2_000


# ---- uploads ------------------------------------------------------------------------------

_FILE_INPUT_JS = """
(el) => {
  const tag = el.tagName.toLowerCase();
  const isFile = tag === 'input' && (el.type || '').toLowerCase() === 'file';
  return {file: isFile, tag, multiple: isFile && !!el.multiple, accept: isFile ? (el.getAttribute('accept') || '') : '',
          disabled: !!el.disabled, connected: el.isConnected};
}
"""

# Watch the page's own change event (in the isolated world; the page cannot see the listener), so
# a widget that reads the files and then clears the input still reads back as attached.
_WATCH_CHANGE_JS = """
(el) => {
  const store = (window.__agentosUploads = window.__agentosUploads || new WeakMap());
  const box = {seen: null};
  store.set(el, box);
  el.addEventListener('change', () => {
    box.seen = Array.from(el.files || [], (f) => ({name: f.name, size: f.size, type: f.type}));
  }, {capture: true, once: true});
}
"""

_READ_FILES_JS = """
(el) => {
  const box = window.__agentosUploads && window.__agentosUploads.get(el);
  return {connected: el.isConnected, files: Array.from(el.files || [], (f) => ({name: f.name, size: f.size, type: f.type})),
          seen: box ? box.seen : null};
}
"""


class UploadResult(BaseModel):
    ok: bool
    ref: str
    url: str
    via: str                                  # "input" (set on the file input) or "chooser" (clicked, then answered)
    files: list[dict[str, Any]]               # what the input holds now: name, size, type
    seen_by_page: list[dict[str, Any]] | None = None  # what the page's change event saw
    signal: Signal = Signal.UNKNOWN
    detail: str | None = None
    signal_detail: str | None = None
    account: dict[str, Any] | None = None


def accept_matches(accept: str, name: str, mime: str) -> bool:
    """Whether a file fits an <input accept="...">: extensions (.jpg), wildcards (image/*) or exact
    types; an empty accept takes anything."""
    tokens = [t.strip().lower() for t in (accept or "").split(",") if t.strip()]
    if not tokens:
        return True
    name, mime = name.lower(), (mime or "").lower()
    aliases = {mime, "image/jpg" if mime == "image/jpeg" else mime}
    for token in tokens:
        if token.startswith("."):
            if name.endswith(token):
                return True
        elif token.endswith("/*"):
            if mime.startswith(token[:-1]):
                return True
        elif token in aliases:
            return True
    return False


def _expected(paths: Sequence[Path], mimes: Sequence[str] | None) -> list[dict[str, Any]]:
    out = []
    for i, p in enumerate(paths):
        mime = (mimes[i] if mimes and i < len(mimes) else None) or mimetypes.guess_type(p.name)[0] or ""
        out.append({"name": p.name, "size": p.stat().st_size, "type": mime})
    return out


def _same_files(got: list[dict[str, Any]] | None, want: list[dict[str, Any]]) -> bool:
    if not got or len(got) != len(want):
        return False
    return all(g.get("name") == w["name"] and g.get("size") == w["size"] for g, w in zip(got, want, strict=True))


def _check_input(info: dict[str, Any], want: list[dict[str, Any]], *, multiple: bool | None = None) -> None:
    if info.get("disabled"):
        raise InvalidRequest("that file input is disabled")
    takes_many = info.get("multiple") if multiple is None else multiple
    if len(want) > 1 and not takes_many:
        raise InvalidRequest(f"that file input takes one file; {len(want)} were given")
    for w in want:
        if not accept_matches(info.get("accept", ""), w["name"], w["type"]):
            raise InvalidRequest(f"that file input accepts {info.get('accept')!r}; {w['name']} ({w['type'] or 'unknown'}) "
                                 "does not fit")


def _sizes(files: list[dict[str, Any]]) -> str:
    return ", ".join(f"{f.get('name')} ({f.get('size')} bytes)" for f in files)


class UploadMixin:
    """upload: attach inbox files to a file input or the button that opens its chooser."""

    # ---- upload ------------------------------------------------------------------------

    async def upload(self, page: Page, ref: str, paths: Sequence[Path | str], *, mimes: Sequence[str] | None = None,
                     root: Path | None = None, chooser_timeout_ms: int = CHOOSER_TIMEOUT_MS) -> UploadResult:
        """Attach files to the file input `ref` names, or to the one its button (or label) opens a
        chooser for, then read back what the input holds. With `root`, every path must be a regular
        file inside it (the lane inbox)."""
        files = [Path(p) for p in paths]
        if not files:
            raise InvalidRequest("no files to upload")
        base = root.resolve() if root is not None else None
        for f in files:
            if f.is_symlink() or not f.is_file():
                raise InvalidRequest(f"{f.name} is not a regular file")
            if base is not None and not f.resolve().is_relative_to(base):
                raise InvalidRequest(f"{f.name} is outside the lane inbox; only inbox files can be uploaded")
        want = _expected(files, mimes)
        ref = normalize_ref(ref)
        self._track(page)
        guard = dialog_guard(page)
        if guard is not None and (open_dialog := guard.pending_on(page)) is not None:
            raise _pending_error(open_dialog, f"upload to {ref}")
        await human.still(page)
        target = await self._resolve(page, ref)
        info = await quietly(target.evaluate(_FILE_INPUT_JS))
        if info is None or not info.get("connected"):
            raise RefNotFound(f"ref {ref} is no longer on the page. Take a new snapshot.")
        try:
            if info["file"]:
                via, element = "input", target
                _check_input(info, want)
                await element.evaluate(_WATCH_CHANGE_JS)
                await self._race(page, element.set_input_files([str(f) for f in files]), guard)
            else:
                via = "chooser"
                chooser = await self._open_chooser(page, target, ref, guard, chooser_timeout_ms)
                element = chooser.element
                chosen = await element.evaluate(_FILE_INPUT_JS)
                _check_input(chosen, want, multiple=chooser.is_multiple())
                await element.evaluate(_WATCH_CHANGE_JS)
                await self._race(page, chooser.set_files([str(f) for f in files]), guard)
            now, seen = await self._race(page, self._read_files(element, want), guard)
        except _DialogOpened as opened:
            raise _pending_error(opened.dialog, f"finish the upload to {ref}") from None
        except AgentosError:
            raise
        except Exception as e:
            raise EngineError(f"upload to {ref} failed: {first_line(e, 300)}") from e

        if _same_files(now, want):
            ok, detail = True, f"the input holds {_sizes(now)}"
        elif _same_files(seen, want):
            ok, detail = True, f"the page read {_sizes(seen)} and then cleared the input (normal for upload widgets)"
        else:
            ok = False
            detail = (f"the input holds {_sizes(now) or 'nothing'} after setting {_sizes(want)}; "
                      "take a snapshot to see what the page did")
        return UploadResult(ok=ok, ref=ref, url=page.url, via=via, files=now, seen_by_page=seen, detail=detail,
                            **(await self.report(page)).public())

    async def _open_chooser(self, page: Page, target: ElementHandle, ref: str, guard: DialogGuard | None,
                            timeout_ms: int) -> Any:
        """Click the button the way a person would and answer the file chooser it opens."""
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        try:
            async with page.expect_file_chooser(timeout=timeout_ms) as chooser_info:
                await self._race(page, human.click(page, target), guard)
            return await chooser_info.value
        except PlaywrightTimeout:
            raise InvalidRequest(f"clicking {ref} did not open a file chooser within {timeout_ms / 1000:g} s; pass the "
                                 "ref of a file input or of the button that opens one") from None

    async def _read_files(self, element: ElementHandle, want: list[dict[str, Any]]) -> tuple[list, list | None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + UPLOAD_READBACK_MS / 1000
        while True:
            state = await quietly(element.evaluate(_READ_FILES_JS)) or {}
            now, seen = state.get("files") or [], state.get("seen")
            if _same_files(now, want) or _same_files(seen, want) or loop.time() >= deadline:
                return now, seen
            await asyncio.sleep(READBACK_POLL_MS / 1000)
