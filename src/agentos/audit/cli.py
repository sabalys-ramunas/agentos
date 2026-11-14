"""CLI: `agentos audit ...`, as the owner on the node (and `verify --bundle` anywhere, offline).

`verify` exits 0 when everything in scope is proven, 3 when the log was tampered with, 4 when
part of it cannot be proven (not covered by a checkpoint yet, written before the chain began, or
signed by a key that was reset, or the file is damaged or not a database), and 1 when it could
not run (no log, or it is locked).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from agentos.core.config import home_from_env
from agentos.core.errors import AgentosError
from agentos.policy.auditlog import EXIT_OK, EXIT_UNVERIFIABLE, AuditKeys, Report, Scope, verify_db

app = typer.Typer(no_args_is_help=True, help="The tamper-evident action log: verify, replay, export, share.")
share_app = typer.Typer(no_args_is_help=True, help="Read-only links to one trace (the record of one call) "
                                                   "or one lane's day.")
app.add_typer(share_app, name="share")

TzOpt = Annotated[str | None, typer.Option("--tz", help="Timezone for days (default: this machine's).")]


def _fail(e: AgentosError | str) -> None:
    typer.echo(f"{e.code}: {e.message}" if isinstance(e, AgentosError) else str(e), err=True)
    raise typer.Exit(1)


def _tz(tz: str | None) -> str:
    if tz:
        return tz
    from agentos.api.zone import host_zone

    return host_zone()


def _when(text: str | None, tz: str) -> float | None:
    """A date (the start of that day in `tz`), an ISO datetime (UTC without a zone), or a
    duration back from now (`2d`)."""
    if not text:
        return None
    from agentos.audit.service import day_window
    from agentos.policy.util import parse_duration

    text = text.strip()
    if len(text) == 10 and text[4] == "-":
        return day_window(text, tz)[0]
    if text[-1:] in "smhdw" and text[:-1].isdigit():
        return datetime.now(UTC).timestamp() - parse_duration(text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        _fail(f"not a date, datetime or duration: {text!r}")
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def _store():
    from agentos.policy.traces import TraceStore

    home = home_from_env()
    return TraceStore(home / "traces.db", secrets_dir=home / "secrets")


def _service(*, full: bool = False):
    """The audit service on this node. `full`: with grant labels (policy) and runs (runner)."""
    from agentos.audit.service import AuditService

    home = home_from_env()
    if not full:
        return AuditService(_store(), home=home)
    from agentos.core.config import load_config
    from agentos.policy.service import PolicyService

    cfg = load_config(home) if (home / "node.toml").exists() else None
    policy = PolicyService(home, node_config=cfg)
    runner = None
    try:
        from agentos.core.runtime import Runtime
        from agentos.runner.service import build

        runner = build(Runtime(), home=home)
    except Exception:  # noqa: BLE001  (a replay without runs still shows every call)
        runner = None
    return AuditService(policy.traces, policy=policy, runner=runner, home=home,
                        node=getattr(cfg, "name", None))


def _check_anchors(report: Report, conn: sqlite3.Connection, home: Path, trusted: set[str] | None, *,
                   live: bool, files: list[Path], controller_key: str | None = None) -> None:
    """Gather the anchors (the controller through agentosd, S3 directly, saved files), then fold
    what they say into `report`."""
    from agentos.core.config import load_config
    from agentos.policy import anchors as anchor_rules
    from agentos.policy.auditlog import Finding

    cfg = load_config(home) if (home / "node.toml").exists() else None
    pub, node_id = controller_key or getattr(cfg, "controller_pubkey", ""), getattr(cfg, "node_id", "")
    if files and not pub:
        notes_key = [Finding("unprovable", "no controller key to check receipts with (node.toml pins none): pass "
                                           "--controller-key, the key `agentos controller anchors` exports")]
    else:
        notes_key = []
    held: list[dict] = []
    notes: list[Finding] = []
    sources: list[str] = []
    for f in files:
        try:
            doc = json.loads(Path(f).read_text())
        except (OSError, ValueError) as e:
            _fail(f"{f} could not be read as anchors: {e}")
        got, dropped = anchor_rules.from_document(doc, controller_pubkey=pub, node_id=node_id, source=str(f))
        held += got
        sources.append(f"{len(got)} from {f}")
        bare = sum(1 for a in got if a.get("unsigned"))
        if bare:
            notes.append(Finding("unprovable", f"{bare} anchor(s) in {f} carry no controller signature (an S3 copy "
                                               "or a hand-made list): they are only as good as where the file came "
                                               "from"))
        if dropped:
            notes.append(Finding("unprovable", f"{dropped} anchors in {f} were left out: they are malformed or "
                                               "their controller signature does not verify"))
    targets = list(getattr(cfg, "audit_anchors", []) or []) if live else []
    if live and "controller" in targets and getattr(cfg, "enrolled", False):
        from agentos.ops.layout import ops_socket
        from agentos.ops.system import Host

        resp = Host().unix_request(ops_socket(home), {"op": "audit_anchors"}, timeout=30)
        if not resp or not resp.get("ok"):
            why = (resp or {}).get("error") or "agentosd is not running"
            notes.append(Finding("unprovable", f"the controller's anchors could not be read: "
                                               f"{why.get('message') if isinstance(why, dict) else why}"))
        else:
            got = [a for a in resp.get("anchors") or [] if anchor_rules.valid(a) is None]
            held += got
            sources.append(f"{len(got)} from the controller")
            if resp.get("truncated"):
                notes.append(Finding("unprovable", f"the controller's anchors were read in {resp.get('pages')} pages "
                                                   "and the walk was cut off, so heads after those may be missing"))
            if resp.get("unsigned"):
                notes.append(Finding("unprovable", f"{resp['unsigned']} anchors from the controller were left out: "
                                                   "their signature does not verify"))
    for url in (t for t in targets if str(t).startswith("s3://")):
        from agentos.node.anchors import fetch_s3
        from agentos.ops.s3 import S3Error

        try:
            got = [a for a in fetch_s3(url, home=home) if anchor_rules.valid(a) is None]
        except S3Error as e:
            notes.append(Finding("unprovable", f"the anchors in {url} could not be read: {e}"))
            continue
        held += got
        sources.append(f"{len(got)} from {url}")
    if live and not targets and not files:
        _fail("this node anchors nowhere: set audit_anchors in node.toml, or pass "
              "--anchors-file")
    if not sources:
        notes.append(Finding("unprovable", "no anchors could be read, so a log rewritten with this node's own key "
                                           "would not show"))
    notes = [*notes_key, *notes]
    findings, summary = anchor_rules.check(conn, held, trusted=trusted)
    summary["sources"] = sources
    anchor_rules.fold(report, [*notes, *findings], summary)


def _print_report(report: Report, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(report.public(), indent=2))
        return
    typer.echo(f"status: {report.status}")
    span = f" (seq {report.first}-{report.last})" if report.first is not None else ""
    line = f"checked {report.checked} rows{span}"
    p = report.proven_through
    if p:
        line += f", proven through checkpoint {p['mark']} (seq {p['seq']}, {p['at']})"
    typer.echo(line)
    for k in report.keys:
        typer.echo(f"key {k['kid']} from mark {k['from_mark']}")
    a = report.anchors
    if a is not None:
        through = a.get("anchored_through")
        line = f"anchors: {a['matched']} of {a['anchors']} match ({', '.join(a.get('sources') or []) or 'none read'})"
        if through:
            line += f", anchored through checkpoint {through['mark']} (seq {through['seq']})"
        typer.echo(line)
    for f in report.findings:
        typer.echo(f"- {f.level}: {f.text}")


@app.command("verify")
def verify_cmd(
    since: Annotated[str | None, typer.Option(help="From this date, datetime or duration ago (2d).")] = None,
    until: Annotated[str | None, typer.Option(help="Up to (not including) this date or datetime.")] = None,
    lane: Annotated[str | None, typer.Option(help="Only this lane's rows count as in scope.")] = None,
    bundle: Annotated[Path | None, typer.Option(help="Verify an export bundle offline instead of this node.")] = None,
    trust_key: Annotated[list[str] | None, typer.Option("--trust-key",
                                                        help="An audit key id you trust (repeat).")] = None,
    anchors: Annotated[bool, typer.Option("--anchors", help="Also check the log against every checkpoint "
                                                            "kept off this node (on the controller, or in "
                                                            "S3).")] = False,
    anchors_file: Annotated[list[Path] | None, typer.Option(
        "--anchors-file", help="Check against copies saved elsewhere: a controller export or an S3 copy "
                               "(repeat).")] = None,
    tz: TzOpt = None,
    controller_key: Annotated[str | None, typer.Option(
        "--controller-key", help="The controller key receipts must verify with (default: the one node.toml "
                                 "pins). An auditor passes the key from the controller's own export.")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check the action log: each entry must link to the one before it, and each signed checkpoint (a signed
    summary of the log) must verify with its key. With --anchors, the log must also match the copies kept
    off the node, so a rewritten, shortened or restarted log shows even when this node signed it again."""
    trusted = set(trust_key) if trust_key else None
    if (anchors or anchors_file) and bundle is not None:
        _fail("--anchors checks this node's own log; it does not apply to --bundle")
    if (anchors or anchors_file) and (since or until):
        _fail("--anchors checks the whole log, so it does not take --since or --until (rows outside them would "
              "go unchecked)")
    try:
        if bundle is not None:
            from agentos.audit.service import verify_bundle

            report = verify_bundle(bundle, trusted=trusted)
        else:
            zone = _tz(tz)
            home = home_from_env()
            path = home / "traces.db"
            if not path.exists():
                _fail(f"no action log at {path}")
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                kids = trusted if trusted is not None else AuditKeys(home / "secrets").trusted_kids()
                report = verify_db(conn, trusted=kids,
                                   scope=Scope(since=_when(since, zone), until=_when(until, zone), lane=lane))
                if anchors or anchors_file:
                    _check_anchors(report, conn, home, kids, live=anchors, files=anchors_file or [],
                                   controller_key=controller_key)
            finally:
                conn.close()
    except AgentosError as e:
        _fail(e)
    except sqlite3.DatabaseError as e:
        if not any(w in str(e) for w in ("not a database", "malformed", "corrupt")):
            _fail(f"the action log could not be read: {e}")  # locked, or no access: it could not run
        # a file that is not a database, or a damaged one: nothing in it can be proven (exit 4, not
        # 3: damage alone does not show an edit; `agentos backup` restores a good copy)
        typer.echo(f"status: unverifiable\nthe action log cannot be read, so nothing in it can be proven: {e}. "
                   "Restore traces.db from a backup (`agentos node backup restore`).", err=True)
        raise typer.Exit(EXIT_UNVERIFIABLE) from None
    except sqlite3.Error as e:
        _fail(f"the action log could not be read: {e}")
    _print_report(report, as_json)
    if report.exit_code != EXIT_OK:
        raise typer.Exit(report.exit_code)


@app.command("export")
def export_cmd(
    day: Annotated[str, typer.Option(help="The day, like 2026-11-03.")],
    out: Annotated[Path, typer.Option("-o", "--out", help="Where to write the bundle (a zip).")],
    lane: Annotated[str | None, typer.Option(help="Only this lane (others become content-free stubs).")] = None,
    tz: TzOpt = None,
) -> None:
    """Write one day as a self-contained bundle that `agentos audit verify --bundle` checks offline."""
    try:
        result = _service(full=True).export(out, day=day, lane=lane, tz=_tz(tz))
    except AgentosError as e:
        _fail(e)
    context = f", {result['context']} rows of the lane from other days" if result.get("context") else ""
    typer.echo(f"wrote {result['path']}: {result['rows']} rows{context}, {result['stubs']} stubs, {result['files']} files, "
               f"seq {result['first_seq']}-{result['last_seq']}, signed by {result['signed_by']}")


@app.command("replay")
def replay_cmd(
    lane: Annotated[str | None, typer.Option(help="The lane, like biz-a/instagram.")] = None,
    day: Annotated[str | None, typer.Option(help="The day, like 2026-11-03.")] = None,
    trace: Annotated[str | None, typer.Option(help="One trace instead of a lane's day (with --html).")] = None,
    html: Annotated[Path | None, typer.Option(
        "--html", help="Write the replay as one HTML file anyone can open offline, with no access to the "
                       "node.")] = None,
    include_screenshots: Annotated[bool, typer.Option(
        "--include-screenshots", help="With --html: put the screenshots in the file (they show the "
                                      "account's pages).")] = False,
    include_args: Annotated[bool, typer.Option(
        "--include-args", help="With --html: put in what the agent typed, the arguments' values and whole "
                               "addresses with their query (a typed password or code, a session id). Left "
                               "out by default, because the file cannot be taken back.")] = False,
    tz: TzOpt = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """What happened on one lane on one day, step by step, with each step's chain status. With --html,
    the same as one self-contained page to hand to someone."""
    if html is not None:
        _replay_html(html, lane=lane, day=day, trace=trace, tz=_tz(tz), screenshots=include_screenshots,
                     include_args=include_args)
        return
    if trace or include_screenshots or include_args:
        _fail("--trace, --include-screenshots and --include-args go with --html")
    if not lane or not day:
        _fail("replay needs --lane and --day")
    try:
        data = _service(full=True).replay(lane, day, tz=_tz(tz))
    except AgentosError as e:
        _fail(e)
    if as_json:
        typer.echo(json.dumps(data, indent=2, default=str))
        return
    chain = data["chain"]
    proven = chain.get("proven_through")
    through = (proven or {}).get("seq")
    pending = [s for s in data["steps"] if _unsealed(s, through)]
    status = chain["status"]
    if status == "unprovable" and pending and all(_unsealed(s, through) or s["chain"]["status"] == "ok"
                                                  for s in data["steps"]):
        status = "ok so far"  # only rows the next checkpoint will seal: nothing is wrong
    head = f"{data['lane']} on {data['day']} ({data['timezone']}): {len(data['steps'])} steps, chain {status}"
    if proven:
        head += f" (proven through checkpoint {proven['mark']}, seq {proven['seq']})"
    typer.echo(head)
    for f in chain.get("findings", []):
        if f["level"] == "unprovable" and _tail_finding(f, through):
            continue  # said below in plain words
        typer.echo(f"  {f['level']}: {f['text']}")
    if pending:
        typer.echo(f"  not sealed yet: {len(pending)} step(s) after the last checkpoint. {_next_checkpoint(proven)}")
    for s in data["steps"]:
        typer.echo(_step_line(s, through))
    if data.get("truncated"):
        typer.echo("(more steps than one replay shows; use --json with a narrower lane or day)")


def _replay_html(out: Path, *, lane: str | None, day: str | None, trace: str | None, tz: str,
                 screenshots: bool, include_args: bool = False) -> None:
    try:
        made = _service(full=True).replay_page(lane=lane, day=day, trace=trace, tz=tz, screenshots=screenshots,
                                               include_args=include_args, by="owner:cli")
    except AgentosError as e:
        _fail(e)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(made["html"])
    typer.echo("warning: this file shows what the agent did on the account, and anyone who gets it can read it.",
               err=True)
    shots = f", {made['images']} screenshots" if screenshots else ", screenshots left out"
    if made.get("left_out"):
        from agentos.audit.replay_page import EMBED_MB

        shots += f" ({made['left_out']} more left out: the file holds at most {EMBED_MB} MB of them)"
    shots += ", typed text and arguments included" if include_args else ", typed text and arguments left out"
    bad = f", {made['mismatched']} screenshot(s) do not match the log" if made["mismatched"] else ""
    typer.echo(f"wrote {out}: {made['steps']} steps{shots}{bad}, chain {made['chain']}")


@app.command("capture")
def capture_cmd(
    lane: Annotated[str | None, typer.Argument(help="A lane (workspace/name) or a whole workspace "
                                                    "(workspace/*).")] = None,
    on: Annotated[bool | None, typer.Option("--on/--off", help="Turn it on or off for LANE.")] = None,
) -> None:
    """Keep a screenshot of the page after each commit an agent makes on a lane, for the replay. Off by
    default. With no arguments, lists the lanes that keep them."""
    from agentos.audit.captures import settings_for

    settings = settings_for(home_from_env())
    if lane is None:
        pats = settings.patterns()
        typer.echo("\n".join(pats) if pats else "no lane keeps screenshots after a commit")
        return
    if on is None:
        _fail("say --on or --off")
    try:
        pats = settings.set(lane, bool(on), by="owner:cli")
    except AgentosError as e:
        _fail(e)
    typer.echo(f"{lane}: screenshots after a commit are {'kept' if on else 'not kept'}"
               + (f" (on for: {', '.join(pats)})" if pats else ""))


def _unsealed(step: dict[str, Any], through: int | None) -> bool:
    """A step whose rows only wait for the next checkpoint (newer than the last one), not a step the
    chain cannot prove for another reason."""
    return step["chain"]["status"] == "unprovable" and (through is None or step.get("seq", 0) > through)


def _tail_finding(f: dict[str, Any], through: int | None) -> bool:
    seq = f.get("seq") or [None]
    return seq[0] is not None and (through is None or seq[0] > through) and (
        "since checkpoint" in f["text"] or "no checkpoint covers them yet" in f["text"])


def _next_checkpoint(proven: dict[str, Any] | None) -> str:
    """When the next signed checkpoint seals the newest rows, in plain words."""
    from agentos.policy.traces import CHECKPOINT_ROWS, CHECKPOINT_S

    when = ""
    at = (proven or {}).get("at")
    try:
        last = float(at) if isinstance(at, (int, float)) else datetime.fromisoformat(str(at)).timestamp()
        due = datetime.fromtimestamp(last + CHECKPOINT_S, UTC)
        when = f"by {due.strftime('%H:%M')} UTC " if due.timestamp() > time.time() else ""
    except (TypeError, ValueError, OverflowError):
        pass
    return (f"The next checkpoint seals them {when}(one comes {CHECKPOINT_S // 60} minutes after the last while "
            f"actions keep coming, every {CHECKPOINT_ROWS} rows, and at a restart); "
            "`agentos access audit checkpoint` signs one now.")


def _step_line(s: dict[str, Any], through: int | None = None) -> str:
    at = (s.get("at") or "")[11:19]
    seal = "not sealed yet" if _unsealed(s, through) else f"chain {s['chain']['status']}"
    parts = [at, s.get("tool") or s.get("kind") or "?", s.get("effect") or "-", s.get("status") or "-",
             f"[{seal}]"]
    line = "  ".join(parts)
    if s.get("summary"):
        line += f"  {s['summary']}"
    extra = []
    if s.get("grant"):
        extra.append(f"grant {s['grant'].get('label') or s['grant'].get('id')}")
    run = s.get("run")
    if run:
        proof = run.get("proof") or {}
        extra.append(f"run {run['id']} {run.get('status')}" + (f", proof {proof['url']}" if proof.get("url") else ""))
    for a in s.get("approvals") or []:
        word = "approved" if a.get("route") == "approved" else "standing approval"
        extra.append(f"{word} by {a.get('by') or 'someone no longer on record'}"
                     + (f" ({a['how']}, {a.get('level')})" if a.get("how") and a.get("level") else ""))
    if s.get("after_untrusted"):
        extra.append(f"after untrusted text: {s['after_untrusted']}")
    if s.get("error"):
        extra.append(f"error: {s['error']}")
    return line + ("\n          " + " · ".join(extra) if extra else "")


@app.command("checkpoint")
def checkpoint_cmd() -> None:
    """Sign a checkpoint now: a signed summary of the log's latest entry."""
    store = _store()
    try:
        store.flush()
        body = store.checkpoint("manual")
    finally:
        store.close()
    if body is None:
        typer.echo("nothing new since the last checkpoint")
    else:
        typer.echo(f"checkpoint {body['n']}: seq {body['seq']}, {body['count']} rows, signed by {body['kid']}")


@app.command("rotate-key")
def rotate_key_cmd(
    reset: Annotated[bool, typer.Option(help="The old key is lost: start a new one without its signature "
                                             "(verify reports the reset).")] = False,
) -> None:
    """Make a new audit key active, endorsed by the old one in the log."""
    store = _store()
    try:
        out = store.rotate_audit_key(reset=reset)
    except AgentosError as e:
        _fail(e)
    finally:
        store.close()
    typer.echo(f"audit key {out['kid']} is active (mark {out['mark']}); it replaces {out['previous']}"
               + (" without its signature (reset)" if out["reset"] else ""))


@app.command("keys")
def keys_cmd(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """The audit keys the log introduced, and the ones this node trusts."""
    store = _store()
    try:
        keys = store.audit_keys()
        trusted = store.keys.trusted_kids() or set()
    finally:
        store.close()
    rows = [{"kid": k, "pub": p, "trusted": k in trusted} for k, p in keys.items()]
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        typer.echo(f"{r['kid']}  {'trusted' if r['trusted'] else 'not in the trust file'}")


@share_app.command("create")
def share_create_cmd(
    trace: Annotated[str | None,
                     typer.Option(help="Share one trace (the record of one call and what it did).")] = None,
    lane: Annotated[str | None, typer.Option(help="Share one lane's day (with --day).")] = None,
    day: Annotated[str | None, typer.Option(help="The day of a lane-day share.")] = None,
    ttl: Annotated[str, typer.Option(help="How long it works: up to 7d.")] = "24h",
    screenshots: Annotated[bool, typer.Option(help="Include proof screenshots (they show the account).")] = False,
    tz: TzOpt = None,
) -> None:
    """Make a read-only link. It is printed once."""
    svc = _service(full=True)
    try:
        out = svc.share_create(trace_id=trace, lane=lane, day=day, tz=_tz(tz), ttl=ttl, screenshots=screenshots,
                               by="owner:cli")
    except AgentosError as e:
        _fail(e)
    base = _base_url(svc)
    typer.echo(f"share {out['id']} until {out['expires_at']}")
    typer.echo(f"{base}/shared#token={out['token']}")


def _base_url(svc: Any) -> str:
    notifier = getattr(svc.policy, "notifier", None)
    cfg = getattr(notifier, "config", None)
    base = getattr(cfg, "public_base_url", None) or getattr(notifier, "panel_url", None) or "http://localhost:7400"
    return str(base).rstrip("/")


@share_app.command("list")
def share_list_cmd(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    items = _service().shares()
    if as_json:
        typer.echo(json.dumps(items, indent=2))
        return
    for s in items:
        target = s["target"].get("trace") or f"{s['target'].get('lane')} {s['target'].get('day')}"
        typer.echo(f"{s['id']}  {s['state']}  {target}  views {s['views']}  until {s['expires_at']}")


@share_app.command("revoke")
def share_revoke_cmd(share_id: str) -> None:
    try:
        out = _service().share_revoke(share_id, by="owner:cli")
    except AgentosError as e:
        _fail(e)
    typer.echo(f"share {out['id']} is {out['state']}")
