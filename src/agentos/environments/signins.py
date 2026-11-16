"""An environment's browser sign-ins across a save: its cookies and local storage (Playwright's
storage state), kept with the save and put back on resume.

- Capture: when a save is made, the storage state of the environment's own browser lane
  (`env/<id>-browser`, never another lane) is read from its live session; with the browser on the
  machine (browser = "vm") a lane closed meanwhile left its last state here (`remember`, on the
  session's close), since that browser starts a fresh context each time.
- Store: `browser.json` in the save's payload, sealed with the saves key like every other payload
  file (agentos.environments.saves.seal_payload) and never in the readable manifest, which holds the
  cookie count only.
- Restore: the state waits here for the lane's browser. On the machine, VmBrowsers.launch opens its
  context with it (`for_launch`; every later launch of that lane gets the newest state too, so a lane
  closed when idle keeps its sign-ins). On the node, a session listener adds the cookies and sets the
  local storage of each origin that has none yet, once (the node keeps a profile afterwards).
- Opt out: environment_open(signins=false), kept with each save; environments.toml save_signins =
  false for the whole node.

Nothing here reads the vault's logins or an account lane: the state comes from the environment's
browser and goes back into it, nowhere else.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("agentos.environments")

PAYLOAD = "browser.json"
CAPTURE_TIMEOUT_S = 20.0
MAX_BYTES = 5 << 20          # a storage state larger than this is not kept (a page that stores media)
MAX_COOKIES = 3000
COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
_CACHE = "_signins"
_LISTENING = "_signins_listening"

# Sets each saved local storage item of this origin that the page does not have yet. It runs in every
# document of the lane's context, so it leaves a marker key in the origin's storage once it is done: an
# item the page removes later (a sign-out) is not set again.
INIT_SCRIPT = """(() => {
  const saved = %s;
  const items = saved[location.origin];
  if (!items) return;
  try {
    const mark = "__agentos_signins_restored";
    if (localStorage.getItem(mark) !== null) return;
    for (const [k, v] of items) if (localStorage.getItem(k) === null) localStorage.setItem(k, v);
    localStorage.setItem(mark, "1");
  } catch (e) {}
})();"""


def enabled(mgr: Any, env: Any) -> bool:
    return bool(getattr(getattr(mgr, "settings", None), "save_signins", True)) and bool(getattr(env, "keep_signins", True))


def _is_env_browser(lane_id: str, env: Any) -> bool:
    return lane_id == env.browser_lane and lane_id.startswith("env/")


def clean(state: Any) -> dict | None:
    """A storage state with only what Playwright reads back (cookies, origins' local storage), or None
    when it holds nothing or does not look like one."""
    if not isinstance(state, dict):
        return None
    cookies = []
    for c in state.get("cookies") or []:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not isinstance(c.get("value"), str):
            continue
        if not isinstance(c.get("domain"), str) or not c["domain"]:
            continue
        row = {k: c[k] for k in COOKIE_KEYS if k in c}
        if row.get("sameSite") not in (None, "Strict", "Lax", "None"):
            row.pop("sameSite", None)
        cookies.append(row)
    origins = []
    for o in state.get("origins") or []:
        if not isinstance(o, dict) or not str(o.get("origin", "")).startswith(("http://", "https://")):
            continue
        items = [{"name": i["name"], "value": i["value"]} for i in o.get("localStorage") or []
                 if isinstance(i, dict) and isinstance(i.get("name"), str) and isinstance(i.get("value"), str)]
        if items:
            origins.append({"origin": o["origin"], "localStorage": items})
    if not cookies and not origins:
        return None
    out = {"cookies": cookies[:MAX_COOKIES], "origins": origins}
    if len(json.dumps(out)) > MAX_BYTES:
        log.warning("an environment's browser state is over %d bytes, so its sign-ins are not kept", MAX_BYTES)
        return None
    return out


# ---------------------------------------------------------------- the node's copy, per environment
def _cache(mgr: Any) -> dict[str, dict]:
    return mgr.__dict__.setdefault(_CACHE, {})


def remember(mgr: Any, env_id: str, state: Any) -> None:
    got = clean(state)
    if got is not None:
        _cache(mgr)[env_id] = got


def pending(mgr: Any, env_id: str) -> dict | None:
    return _cache(mgr).get(env_id)


def drop(mgr: Any, env_id: str) -> None:
    _cache(mgr).pop(env_id, None)


def for_launch(mgr: Any, env: Any) -> dict | None:
    """The storage state a "vm" browser's new context opens with (None: a fresh one)."""
    return pending(mgr, env.id) if enabled(mgr, env) else None


# ---------------------------------------------------------------- capture
def _live(mgr: Any, env: Any) -> Any:
    lanes = mgr._get("lanes")
    fn = getattr(lanes, "live_session", None)
    if not callable(fn) or not _is_env_browser(env.browser_lane, env):
        return None
    with contextlib.suppress(Exception):
        return fn(env.browser_lane)
    return None


async def read_state(context: Any) -> dict | None:
    try:
        return clean(await asyncio.wait_for(context.storage_state(), CAPTURE_TIMEOUT_S))
    except Exception as e:  # noqa: BLE001 - a browser that does not answer saves no sign-ins
        log.info("could not read the browser's sign-ins: %s", getattr(e, "message", e))
        return None


async def capture(mgr: Any, env: Any) -> dict | None:
    """The environment's browser sign-ins for a save: its live session's, else the last a "vm" browser
    left on close. None when it opted out or there are none."""
    if not enabled(mgr, env):
        return None
    session = _live(mgr, env)
    context = getattr(session, "context", None) if session is not None else None
    if context is not None:
        state = await read_state(context)
        if state is not None:
            _cache(mgr)[env.id] = state
            return state
    return pending(mgr, env.id)


def write_payload(folder: Path, state: dict | None) -> int:
    """browser.json in a save's temp folder (0600, sealed with the payload next). Returns its cookies."""
    if not state:
        return 0
    path = Path(folder) / PAYLOAD
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    return len(state.get("cookies") or [])


def read_saved(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        return clean(json.loads(Path(path).read_text()))
    except (OSError, ValueError):
        log.warning("a save's browser sign-ins do not read; the browser starts signed out")
        return None


# ---------------------------------------------------------------- restore
async def apply(context: Any, state: dict) -> None:
    """Into a running context: the cookies, and each origin's local storage where it has none yet."""
    if state.get("cookies"):
        await context.add_cookies(state["cookies"])
    stores = {o["origin"]: [[i["name"], i["value"]] for i in o["localStorage"]] for o in state.get("origins") or []}
    if stores:
        await context.add_init_script(script=INIT_SCRIPT % json.dumps(stores))


async def restore(mgr: Any, env: Any, state: dict | None) -> None:
    """A resume's sign-ins: kept for the lane's browser (it may not run yet), and put into it now when
    it runs on the node already."""
    if state is None or not enabled(mgr, env):
        return
    _cache(mgr)[env.id] = state  # a "vm" browser's every launch opens with the newest (for_launch)
    env = mgr.store.get(env.id)
    session = _live(mgr, env)
    context = getattr(session, "context", None) if session is not None else None
    if context is None:
        return
    try:
        await apply(context, state)
    except Exception as e:  # noqa: BLE001 - the next launch of the lane tries again
        log.info("environment %s: the sign-ins did not go into its running browser: %s", env.id, e)
        return
    if env.browser != "vm":
        drop(mgr, env.id)  # the node's profile keeps them from now on


def listen(mgr: Any, lanes: Any) -> None:
    """Once per node: a browser on the node that starts for an environment with sign-ins waiting gets
    them (a "vm" browser gets them from for_launch instead)."""
    add = getattr(lanes, "add_session_listener", None)
    if not callable(add) or mgr.__dict__.get(_LISTENING):
        return
    mgr.__dict__[_LISTENING] = True

    async def started(lane_id: str, session: Any) -> None:
        found = mgr.env_of_lane(lane_id) if isinstance(lane_id, str) else None
        if found is None or found[1] != "browser":
            return
        env = found[0]
        state = pending(mgr, env.id)
        if env.browser == "vm" or state is None or not enabled(mgr, env) or not _is_env_browser(lane_id, env):
            return
        context = getattr(session, "context", None)
        if context is not None:
            await apply(context, state)
            drop(mgr, env.id)

    add(started)


def restored_words(m: Any, state: dict | None, env: Any) -> str | None:
    """`resumed.signins`, in words."""
    if not getattr(m, "keep_signins", True) or not getattr(env, "keep_signins", True):
        return "not kept: this environment opted out (signins=false)"
    if state is None:
        return None if not getattr(m, "signins", 0) else "the save's sign-ins did not read: the browser starts signed out"
    n = len(state.get("cookies") or [])
    return (f"the browser's sign-ins are back ({n} cookie{'s' if n != 1 else ''}, "
            f"{len(state.get('origins') or [])} site storage{'s' if len(state.get('origins') or []) != 1 else ''})")
