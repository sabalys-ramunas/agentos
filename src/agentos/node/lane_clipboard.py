"""Read what a warm lane's browser copied, so the live view can put it on the viewer's own clipboard.

The VNC clipboard cannot carry it: x11vnc and the lane's browser exchange no selections in either
direction (measured with x11vnc 0.9.16 and 0.9.17 and Camoufox on Firefox 158). So the node asks
the lane's X server itself: `xclip -o -selection clipboard -t UTF8_STRING` with the lane's DISPLAY
and its Xauthority file (agentos.node.display), the way any X client on that display would.

The text is a person's: it is returned to the caller and nothing else. It is never logged, traced
or stored here, and no error message carries it. Only the live view's clipboard route calls this,
for a person who has control (agentos.node.viewer_app, docs/THREAT-MODEL.md "Viewer links").
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from typing import Any

from agentos.core.errors import AgentosError, ServiceUnavailable

XCLIP = "xclip"
INSTALL_XCLIP = "apt-get install -y xclip"  # run as root; the catalog never says sudo
TIMEOUT_SECONDS = 2.0
MAX_BYTES = 1024 * 1024
CHUNK = 64 * 1024
STDERR_BYTES = 4096
# What xclip says when the clipboard holds no text: an image only, or no program owns it. Both are
# "nothing copied" (xclip 0.13: "Error: target UTF8_STRING not available").
NOTHING_COPIED = (b"not available", b"no selection", b"no owner")


class XclipMissing(ServiceUnavailable):
    """xclip is not installed on the node, so the lane's clipboard cannot be read."""
    code = "xclip_missing"

    def __init__(self, message: str = "This node cannot read the lane's clipboard yet: the xclip program is not "
                                      f"installed. Install it on the node as root with `{INSTALL_XCLIP}`, then copy again."):
        super().__init__(message)


class ClipboardTooLarge(AgentosError):
    """What the lane copied is over the size the live view takes (MAX_BYTES), so none of it was read."""
    code = "clipboard_too_large"


class ClipboardUnreadable(AgentosError):
    """The lane's clipboard could not be read as text: the browser did not answer in time, the text
    is not UTF-8, or xclip failed on the lane's display."""
    code = "clipboard_unreadable"


Spawn = Callable[..., Any]


async def read_clipboard(display: str, xauthority: str | None, *, spawn: Spawn | None = None,
                         timeout: float = TIMEOUT_SECONDS, limit: int = MAX_BYTES) -> str:
    """The text on the lane's CLIPBOARD selection, or "" when nothing is copied there (no text on
    it, an image only, or nobody owns it). No shell; a fresh, minimal environment; `timeout`
    seconds at most; over `limit` bytes is refused (ClipboardTooLarge), never cut short.

    Raises XclipMissing, ClipboardTooLarge or ClipboardUnreadable."""
    spawn = spawn or asyncio.create_subprocess_exec
    env = {"DISPLAY": display, "PATH": os.environ.get("PATH") or "/usr/bin:/bin"}
    if xauthority:
        env["XAUTHORITY"] = xauthority
    try:
        proc = await spawn(XCLIP, "-o", "-selection", "clipboard", "-t", "UTF8_STRING",
                           stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                           stderr=asyncio.subprocess.PIPE, env=env)
    except FileNotFoundError:
        raise XclipMissing() from None
    except OSError as e:
        raise ClipboardUnreadable(f"The node could not start xclip ({e.strerror or 'no reason given'}). "
                                  "Run `agentos doctor` on the node.") from None

    async def drain() -> tuple[bytes, bytes, int]:
        chunks: list[bytes] = []
        size = 0
        while chunk := await proc.stdout.read(CHUNK):
            size += len(chunk)
            if size > limit:
                raise ClipboardTooLarge(f"What is copied in the lane is over {limit // (1024 * 1024)} MB, too much "
                                        "to bring over. Copy a smaller part.")
            chunks.append(chunk)
        err = await proc.stderr.read(STDERR_BYTES)
        return b"".join(chunks), err, await proc.wait()

    try:
        out, err, code = await asyncio.wait_for(drain(), timeout)
    except TimeoutError:
        raise ClipboardUnreadable(f"The lane's browser did not hand over what it copied within {timeout:g} "
                                  "seconds. Copy again in the lane, then try once more.") from None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
    if code != 0:
        if not err.strip() or any(word in err.lower() for word in NOTHING_COPIED):
            return ""
        # xclip's own message (a display it cannot open, say): never the clipboard's content
        said = err.decode("utf-8", "replace").strip().splitlines()[0][:160]
        raise ClipboardUnreadable(f"xclip could not read the lane's clipboard ({said}). Run `agentos doctor` "
                                  "on the node.")
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        raise ClipboardUnreadable("What is copied in the lane is not text this page can read. Copy it again as "
                                  "plain text.") from None
