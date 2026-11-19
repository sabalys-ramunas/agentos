"""Pre-flight on a real node: one scratch lane, two public test pages, every layer once.

Run it on the node, as the user agentosd runs as, with the node's own install:

    AGENTOS_HOME=/var/lib/agentos /opt/agentos/.venv/bin/python scripts/preflight_node.py

It creates the lane preflight/scratch (no proxy, no account, so it leaves from the node's own
IP and touches no account site), issues a one-hour agent grant for that lane only, and drives
it over MCP the way an agent does: navigate, snapshot, act, wait, screenshot, claim and queue,
an approval decided by the owner, take control and hand back, the egress guard, the lane
check, then a freeze and an unfreeze. The freeze revokes the grant for the lane alone, and a
second, wider grant (the whole preflight workspace) is kept but refused on the frozen lane, then
works again after the unfreeze: a lane freeze never kills a grant that reaches beyond it, so the
owner's own computer keeps its grant. It then reads the audit log,
the replay, the logs and the panel API, closes and deletes the lane, and prints one line per
step. The pages are https://example.com and https://httpbin.org/forms/post (public test pages,
no login, nothing is submitted). It never prints a token or a sign-in code.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.client.mcp_http import McpError, McpSession, ToolError
from agentos.core.config import load_config

LANE = "preflight/scratch"
WORKSPACE = "preflight"
PAGE = "https://example.com/"
FORM = "https://httpbin.org/forms/post"
AGENTOS = str(Path(sys.executable).with_name("agentos"))
RESULTS: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}", flush=True)
    return ok


def cli(*args: str, check: bool = False) -> tuple[int, str]:
    proc = subprocess.run([AGENTOS, *args], check=False, capture_output=True, text=True, timeout=180,
                          env=os.environ)
    out = (proc.stdout + proc.stderr).strip()
    if check and proc.returncode != 0:
        raise RuntimeError(f"agentos {' '.join(args)} failed ({proc.returncode}): {out[-300:]}")
    return proc.returncode, out


def tool(session: McpSession, name: str, **args: Any) -> Any:
    return session.call(name, args)


def refused(session: McpSession, name: str, **args: Any) -> str:
    """The error text of a call that must fail, or '' when it did not fail."""
    try:
        session.call(name, args)
    except ToolError as e:
        return str(e)
    except McpError as e:  # a revoked grant: the node refuses the token itself (401)
        return f"{e.failure.value}: {e.detail}"
    return ""


def ref_for(outline: str, label: str) -> str | None:
    for line in outline.splitlines():
        if label.lower() in line.lower():
            m = re.search(r"\[ref=((?:f\d+)?e\d+)\]", line)
            if m:
                return m.group(1)
    return None


def text_of(result: Any) -> str:
    return result if isinstance(result, str) else json.dumps(result, default=str)


def issue_grant() -> tuple[str, str]:
    _, out = cli("grants", "issue", "--subject", "preflight-agent", "--lane", LANE, "--ceiling", "commit",
                 "--ttl", "1h", "--special", "memory", "--memory-write", "--memory-workspace", WORKSPACE,
                 check=True)
    token = next(line.strip() for line in out.splitlines() if line.strip().startswith("agt"))
    grant_id = next(w for w in out.split() if w.startswith("gr_"))
    return grant_id, token


def issue_wide_grant() -> tuple[str, str]:
    """A grant for the whole preflight workspace: a freeze of the scratch lane must keep it."""
    _, out = cli("grants", "issue", "--subject", "preflight-wide", "--workspace", WORKSPACE, "--ceiling", "read",
                 "--ttl", "1h", check=True)
    token = next(line.strip() for line in out.splitlines() if line.strip().startswith("agt"))
    grant_id = next(w for w in out.split() if w.startswith("gr_"))
    return grant_id, token


def drive(cfg: Any, token: str, wide: str) -> None:
    with McpSession(cfg.mcp_port, token, timeout=120) as s:
        who = tool(s, "whoami")
        step("whoami", isinstance(who, dict) and bool(who.get("node")), f"node {who.get('node')}")
        info = tool(s, "grant_info")
        step("grant_info explains the grant", bool(info.get("explain")), (info.get("explain") or [""])[1][:90])

        opened = tool(s, "browser_open", on=LANE)
        step("browser_open", opened.get("state") in ("warm", "active"), f"state {opened.get('state')}")

        nav = tool(s, "browser_navigate", url=PAGE, on=LANE)
        nav_text = text_of(nav)
        step("browser_navigate example.com", "Example Domain" in nav_text and '"signal": "ok"' in nav_text.replace(
            "'", '"'), nav_text.splitlines()[0][:110])
        snap = text_of(tool(s, "browser_snapshot", on=LANE))
        step("browser_snapshot", "[ref=e" in snap and "Example Domain" in snap, f"{snap.count('[ref=')} refs")

        went = text_of(tool(s, "browser_navigate", url=FORM, on=LANE))
        form = text_of(tool(s, "browser_snapshot", on=LANE))
        name_ref = ref_for(form, "Customer name") or ref_for(form, "custname")
        step("form page outline", name_ref is not None,
             f"customer name field {name_ref}" if name_ref else
             f"navigate: {went.splitlines()[0][:100]} | outline: {' / '.join(form.splitlines()[:14])[:400]}")
        if name_ref:
            act = tool(s, "browser_act", on=LANE, ref=name_ref, action="type", text="Preflight", snapshot="delta")
            act_text = text_of(act)
            step("browser_act type, delta outline", '"ok": true' in act_text.replace("'", '"').lower()
                 or "ok: true" in act_text.lower(), act_text.splitlines()[0][:110])
        waited = tool(s, "browser_wait", on=LANE, for_text="Pizza Size", timeout_s=10)
        step("browser_wait for text", "met" in text_of(waited).lower() or "true" in text_of(waited).lower(),
             text_of(waited)[:90])
        shot = s._rpc("tools/call", {"name": "browser_screenshot", "arguments": {"on": LANE}})
        kinds = [c.get("type") for c in shot.get("content") or []]
        step("browser_screenshot", "image" in kinds and not shot.get("isError"), f"content {kinds}")

        claim = tool(s, "browser_claim", on=LANE, purpose="preflight", ttl_s=60)
        queue = tool(s, "browser_queue", on=LANE)
        rel = tool(s, "browser_release", on=LANE)
        step("browser_claim, browser_queue, browser_release", bool(claim.get("lease")) and rel.get("released") is True,
             f"holder {queue.get('holder') or (queue.get('claim') or {}).get('holder')}")

        pending = tool(s, "memory_write", workspace=WORKSPACE, type="decision",
                       title="Preflight decision", body="A decision the owner must approve.")
        approval_id = pending.get("approval_id") if isinstance(pending, dict) else None
        step("an approval is created", pending.get("status") == "approval_pending" and bool(approval_id),
             f"{pending.get('status')} {approval_id}")
        if approval_id:
            code, out = cli("approvals", "approve", approval_id)
            step("the owner approves on the node", code == 0, out.splitlines()[-1][:110] if out else "")
            again = tool(s, "memory_write", workspace=WORKSPACE, type="decision",
                         title="Preflight decision", body="A decision the owner must approve.")
            step("the approved call runs", isinstance(again, dict) and bool(again.get("id")),
                 f"entry {again.get('id') if isinstance(again, dict) else again}")

        code, _ = cli("lane", "take", LANE)
        blocked = refused(s, "browser_snapshot", on=LANE)
        step("take control stops the agent", code == 0 and "human_in_control" in blocked, blocked[:90])
        code, _ = cli("lane", "handback", LANE, "--note", "preflight hand back")
        after = text_of(tool(s, "browser_snapshot", on=LANE))
        step("hand back, with the note", code == 0 and "preflight hand back" in after,
             "note in the next result" if "preflight hand back" in after else after[:90])

        cli("lane", "sites", LANE, "add", "example.com", check=True)
        off_site = refused(s, "browser_navigate", url=FORM, on=LANE)
        on_site = text_of(tool(s, "browser_navigate", url=PAGE, on=LANE))
        cli("lane", "sites", LANE, "clear")
        step("egress guard refuses an off-list site", "egress_refused" in off_site and "Example Domain" in on_site,
             off_site.splitlines()[0][:100] if off_site else "not refused")

        check = tool(s, "browser_check", on=LANE)
        step("browser_check", check.get("level") in ("ok", "warn"),
             f"{check.get('level')}: {', '.join(c['id'] for c in check.get('checks', []) if c['level'] != 'ok')}")

        code, out = cli("freeze", "lane", LANE, "--reason", "preflight")
        frozen = refused(s, "browser_snapshot", on=LANE)
        step("freeze stops the agent at once",
             code == 0 and any(w in frozen for w in ("frozen", "denied", "unauthorized")),
             frozen.splitlines()[0][:100] if frozen else "not stopped")
        revoked = re.search(r"revoked (\d+) grant", out)
        step("the freeze revokes only the lane's own grant", bool(revoked) and revoked.group(1) == "1",
             revoked.group(0) if revoked else out.splitlines()[-1][:100] if out else "")
    with McpSession(cfg.mcp_port, wide, timeout=30) as w:
        held = refused(w, "browser_snapshot", on=LANE)
        step("a wider grant is kept and refused on the frozen lane", "frozen" in held,
             held.splitlines()[0][:100] if held else "it was not refused")
    code, out = cli("unfreeze", "lane", LANE)
    step("unfreeze on the node", code == 0, out.splitlines()[0][:100] if out else "")
    time.sleep(1.5)  # agentosd lifts it within a second
    try:
        with McpSession(cfg.mcp_port, token, timeout=30) as s:
            denied = refused(s, "browser_snapshot", on=LANE)
    except Exception as e:  # noqa: BLE001 - a refused token may fail at initialize
        denied = str(e)
    step("the frozen grant stays revoked", bool(denied), denied.splitlines()[0][:100] if denied else "it still works")
    try:
        with McpSession(cfg.mcp_port, wide, timeout=30) as w:
            back = text_of(tool(w, "browser_snapshot", on=LANE))
        step("the wider grant works again after the unfreeze", "[ref=" in back or "Example" in back, back[:90])
    except Exception as e:  # noqa: BLE001
        step("the wider grant works again after the unfreeze", False, f"{type(e).__name__}: {str(e)[:120]}")


def panel(cfg: Any, day: str) -> None:
    from agentos.api.auth import create_login_code

    link = create_login_code(cfg.home, base_url=f"http://127.0.0.1:{cfg.mcp_port}")
    code = link.url.split("code=", 1)[1]
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    base = f"http://127.0.0.1:{cfg.mcp_port}"
    req = urllib.request.Request(f"{base}/api/session", data=json.dumps({"code": code}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(req, timeout=20) as r:
        signed = r.status == 200
    with opener.open(f"{base}/api/lanes", timeout=20) as r:
        lanes = json.loads(r.read())
    lanes = lanes.get("lanes", lanes) if isinstance(lanes, dict) else lanes
    mine = next((lane for lane in lanes if lane.get("id") == LANE), None)
    step("panel sign-in and lanes", signed and mine is not None and "control" in mine and "frozen" in mine,
         f"{LANE}: control {mine.get('control', {}).get('state') if mine else '?'}, frozen {mine.get('frozen') if mine else '?'}")
    with opener.open(f"{base}/api/audit/replay?lane={LANE}&day={day}&tz=UTC", timeout=30) as r:
        replay = json.loads(r.read())
    step("panel replay of the lane's day", len(replay.get("steps", [])) > 5,
         f"{len(replay.get('steps', []))} steps, chain {replay.get('chain', {}).get('status')}")
    built = ui_built(cfg)
    if built is None:
        print("skip  panel page: the web app is not built on this node (no web/dist). MCP and the API work; "
              "deploy/README.md says how to add one.", flush=True)
        return
    with opener.open(f"{base}/", timeout=20) as r:
        step("panel page", r.status == 200 and b"<div" in r.read(2000), f"index served ({built})")


def ui_built(cfg: Any) -> str | None:
    """The web app build this install has (web/dist next to the code), or None."""
    exe = Path(sys.executable)  # .venv/bin/python: not resolved, it links to the interpreter elsewhere
    root = exe.parents[2] if exe.parent.name == "bin" and len(exe.parents) > 2 else None
    roots = [r for r in (root, Path(__file__).resolve().parents[1]) if r is not None]
    for base in roots:
        if (base / "web" / "dist" / "index.html").is_file():
            return "web/dist"
    return None


def close_and_delete(cfg: Any) -> tuple[int, str]:
    """Close the lane in the daemon with the node's own token (read here, never printed), then
    delete it: `lane list` reads the store, which can say cold while the daemon still has it warm."""
    with McpSession(cfg.mcp_port, cfg.mcp_token, timeout=60) as s:
        s.call("browser_close", {"on": LANE})
    time.sleep(1)
    return cli("lane", "delete", LANE, "--yes")


def main() -> int:
    cfg = load_config()
    day = datetime.now(UTC).date().isoformat()
    print(f"pre-flight on {cfg.name} ({cfg.home}), lane {LANE}", flush=True)
    if any(line.split()[:1] == [LANE] for line in cli("lane", "list")[1].splitlines()):
        close_and_delete(cfg)  # left over from an earlier run
    cli("lane", "create", LANE, check=True)
    grant_id, token = issue_grant()
    step("agent grant for the scratch lane", token.startswith("agt"), grant_id)
    wide_id, wide = issue_wide_grant()
    try:
        drive(cfg, token, wide)
    except Exception as e:  # noqa: BLE001 - report and go on to the checks and cleanup
        step("drive the lane over MCP", False, f"{type(e).__name__}: {str(e)[:160]}")
    finally:  # never leave the scratch lane frozen
        if "Nothing is frozen" not in cli("freeze", "status")[1]:
            cli("unfreeze", "lane", LANE)
            time.sleep(1.5)
    code, out = cli("audit", "verify")
    step("audit verify: no tampering", code in (0, 4), out.splitlines()[0][:100] if out else "")
    code, out = cli("audit", "replay", "--lane", LANE, "--day", day)
    step("audit replay (CLI)", code == 0 and LANE in out, f"{out.count(chr(10))} lines")
    # as the service user this reads the daemon's own log file (it cannot read the system journal)
    code, out = cli("logs", "--lane", LANE, "--since", "30m", "--json")
    lines = [line for line in out.splitlines() if line.startswith("{")]
    step("structured logs for the lane", code == 0 and bool(lines),
         f"{len(lines)} lines" if code == 0 else out.splitlines()[-1][:120] if out else f"exit {code}")
    try:
        panel(cfg, day)
    except Exception as e:  # noqa: BLE001
        step("panel", False, f"{type(e).__name__}: {str(e)[:160]}")
    try:
        code, out = close_and_delete(cfg)
        step("cleanup: lane closed and deleted", code == 0, out.splitlines()[-1][:100] if out else "")
    except Exception as e:  # noqa: BLE001
        step("cleanup", False, f"{type(e).__name__}: {str(e)[:160]}")
    cli("grants", "revoke", grant_id)
    cli("grants", "revoke", wide_id)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} steps ok" + (
        "; failed: " + ", ".join(r[0] for r in failed) if failed else ""), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
