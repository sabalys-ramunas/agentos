"""The signed-out check (AUD-22): is what the author just published visible to everyone else?

A readback from the logged-in author sees posts that a site's spam filter or restrictions hide
from every other visitor. An operation whose readback declares `public: cookieless` gets a
second read after its live readback passed: the driver opens the object's permalink in a
browser with no cookies, the lane's fingerprint and the lane's proxy (on the node:
openajar.node.signed_out), and looks for the object's text. The outcome:

- visible: the run stays `succeeded_verified`, with the check in its proof (`proof.public`);
- not visible: `succeeded_unverified`, note `visible_only_to_author`, re-checked later;
- could not check (no permalink, no driver support, the page did not load): `succeeded_unverified`
  with the reason, re-checked later.

A plain HTTP read cannot stand in: Reddit serves a JavaScript shell to a cookieless GET, and
old.reddit redirects to a login page. The resolved url and text are kept in the run's private
step state (CHECK_PATH), so a re-check needs neither the params nor the page again.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from openajar.core.logs import url_for_log
from openajar.runner.util import Clock, clip, clip_text, host_in

CHECK_PATH = "signed_out"  # the private step state holding {"url", "text"}
TIMEOUT_MS = 20_000
VISIBLE_ONLY_TO_AUTHOR = "visible_only_to_author"
_MISSING = object()


def _lookup(scope: Mapping[str, Any], path: str) -> Any:
    if path.startswith("param:"):
        return scope.get(path[6:], _MISSING)
    cur: Any = scope.get("vars") or {}
    for part in path.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def resolve(spec: Mapping[str, str], scope: Mapping[str, Any], domain: str | None) -> tuple[dict[str, str] | None, str]:
    """({"url", "text"} to check, "") or (None, why it cannot be checked). `spec` is the compiled
    operation's `signed_out` ({"url": <saved path>, "text": <saved path> or "param:<name>"})."""
    url, text = _lookup(scope, spec.get("url", "")), _lookup(scope, spec.get("text", ""))
    if not isinstance(url, str) or not url:
        return None, f"the run saved no permalink at {spec.get('url')!r}"
    if not isinstance(text, str) or not text.strip():
        return None, f"the run has no text at {spec.get('text')!r} to look for"
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        return None, "the permalink is not a plain http(s) address"
    if not domain or not host_in(parts.hostname, domain):
        return None, f"the permalink is not under the operation's domain {domain}"
    return {"url": url, "text": text}, ""


def record(visible: bool | None, *, url: str | None = None, status: int | None = None, note: str | None = None,
           clock: Clock | None = None) -> dict[str, Any]:
    """What proof.public shows: never the text looked for, never a URL's query."""
    out: dict[str, Any] = {"check": "cookieless", "visible": visible}
    if url:
        out["url"] = url_for_log(url)
    if status is not None:
        out["status"] = status
    if note:
        out["note"] = clip_text(note, 300)
    if clock is not None:
        out["checked_at"] = clock.now().isoformat()
    return out


async def run(driver: Any, check: Mapping[str, str], clock: Clock, *, timeout_ms: int = TIMEOUT_MS) -> dict[str, Any]:
    """Ask the driver for the signed-out view of check["url"]; a driver that cannot is "could not check"."""
    fn = getattr(driver, "read_signed_out", None)
    if fn is None:
        return record(None, url=check["url"], note="this node cannot open a page signed out", clock=clock)
    try:
        found = await fn(check["url"], check["text"], timeout_ms=timeout_ms)
    except Exception as e:  # noqa: BLE001 - any failure means "could not check"
        message = getattr(e, "message", None) or f"{type(e).__name__}: {e}"
        return record(None, url=check["url"], note=f"the signed-out read failed: {clip(message, max_str=200)}",
                      clock=clock)
    visible = found.get("visible")
    return record(visible if isinstance(visible, bool) else None, url=check["url"], status=found.get("status"),
                  note=found.get("note"), clock=clock)


def passed(public: Any) -> bool:
    """A run's proof.public, if any, does not stand in the way of `succeeded_verified`."""
    return not isinstance(public, Mapping) or public.get("visible") is True


def reason(public: Mapping[str, Any]) -> str:
    where = public.get("url") or "the permalink"
    if public.get("visible") is False:
        return (f"{VISIBLE_ONLY_TO_AUTHOR}: the author's readback passed, but a signed-out visitor does not see "
                f"its text at {where}; it may be held by the site's filters")
    return f"the signed-out check could not run: {public.get('note') or 'unknown'}"
