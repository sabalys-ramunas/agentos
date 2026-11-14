"""`agentos connector compile|keygen|sign|install|remove|list|bind|unbind|surfaces|run|health`.

compile and sign are maintainer tools (they need no node); the others work on this node's
runner database, as the owner (whoever has a shell on the node is the owner, as for
`agentos flows`).

Surfaces (AUD-01): `bind` says who a connector acts as and through which login, for example

    agentos connector bind biz-a/facebook-page --site facebook --lane personal/facebook \
        --acts-as fb_page --id 100089000000001 --name biz-a

`run` queues an operation on a surface through its binding, its health and its pace, like
the operation's MCP tool; the daemon executes it.

`install` and `remove` change the runner database; a running agentosd serves the new tool list
without a restart (agentos.mcp.reload). The CLI asks it to reload at once through the ops
socket and prints what changed; agentosd also notices by itself within a few seconds.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from agentos.core import catalog
from agentos.core.errors import AgentosError
from agentos.runner.connectors.compile import CompiledConnector, ConnectorInvalid, compile_path
from agentos.runner.connectors.service import ConnectorService
from agentos.runner.connectors.signing import (
    CATALOG_KEYS,
    build_payload,
    key_id,
    load_private_key,
    private_pem,
    public_text,
    sign_payload,
    trusted_from,
)

app = typer.Typer(no_args_is_help=True, help="Connectors: sites as typed operations, compiled into flows.")

PathArg = Annotated[Path, typer.Argument(exists=True, help="a connector directory or connector.json")]


def _fail(e: AgentosError) -> None:
    typer.echo(catalog.cli_error(e.code, e.message), err=True)
    for issue in getattr(e, "issues", [])[8:]:
        if issue.level == "error":
            typer.echo(f"  {issue}", err=True)
    raise typer.Exit(1)


def _compile(path: Path, commit_gate: bool) -> CompiledConnector:
    try:
        return compile_path(path, commit_gate=commit_gate)
    except ConnectorInvalid as e:
        _fail(e)
        raise  # unreachable


def _report(cc: CompiledConnector) -> None:
    c = cc.connector
    typer.echo(f"{c.connector} {c.version} ({c.domain})")
    for name, op in sorted(cc.operations.items()):
        typer.echo(f"  {op.flow_id}  {op.effect}  tool {op.tool_name}")
        typer.echo(f"    contract        {op.contract}")
        typer.echo(f"    implementation  {op.implementation}" + ("  (provisional)" if op.pending else ""))
        typer.echo(f"    proof           {op.proof}"
                   + {"verified": "  (the file's mark names it)", "stale": "  (the file's mark is stale)"}.get(op.mark, ""))
        if op.pending:
            typer.echo(f"    needs runner    {', '.join(op.pending)} (not installable yet)")
    for w in cc.warnings:
        typer.echo(f"  {w}")


@app.command("compile")
def compile_cmd(path: PathArg,
                out: Annotated[Path | None, typer.Option(help="write the compiled flows here as JSON")] = None,
                commit_gate: Annotated[bool, typer.Option(help="a missing commit request block is an error")] = False,
                ) -> None:
    """Validate a connector, lint it strictly and compile each operation into a flow."""
    cc = _compile(path, commit_gate)
    _report(cc)
    if out is not None:
        doc = {"index": cc.index(), "flows": [op.definition for _, op in sorted(cc.operations.items())]}
        out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        typer.echo(f"wrote {out}")


@app.command("keygen")
def keygen_cmd(out: Annotated[Path, typer.Argument(help="where to write the PEM private key")]) -> None:
    """Make an Ed25519 signing key (for tests and a maintainer's offline catalog key)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if out.exists():
        typer.echo(f"error: {out} exists; refusing to overwrite a key", err=True)
        raise typer.Exit(1)
    key = Ed25519PrivateKey.generate()
    out.write_bytes(private_pem(key))
    out.chmod(0o600)
    typer.echo(f"{key_id(key)} {public_text(key)}")


@app.command("sign")
def sign_cmd(paths: Annotated[list[Path], typer.Argument(exists=True, help="connector directories")],
             key: Annotated[Path, typer.Option(exists=True, help="PEM Ed25519 private key")],
             out: Annotated[Path, typer.Option(help="the bundle file to write")]) -> None:
    """Compile connectors and write a signed bundle of them."""
    compiled = [_compile(p, False) for p in paths]
    try:
        bundle = sign_payload(build_payload(compiled), load_private_key(key))
    except AgentosError as e:
        _fail(e)
    out.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    typer.echo(f"wrote {out}: {len(compiled)} connector(s) signed by {bundle['signature']['key_id']}")


@app.command("install")
def install_cmd(path: Annotated[Path, typer.Argument(exists=True, help="a connector directory or a bundle")],
                trust: Annotated[list[str] | None, typer.Option(help="also trust this base64url public key")] = None,
                force: Annotated[bool, typer.Option(help="allow a downgrade (a rollback)")] = False) -> None:
    """Install a connector from the repo directory (unsigned) or a signed bundle file."""
    from agentos.runner.cli import OWNER, _service

    svc = ConnectorService(_service().flows, trusted={**CATALOG_KEYS, **trusted_from(trust or [])})
    try:
        if path.is_dir() or path.name == "connector.json":
            rows = svc.install_dir(path, by=OWNER, force=force)
        else:
            rows = svc.install_bundle(json.loads(path.read_text()), by=OWNER, force=force)
    except ValueError as e:
        typer.echo(f"error: {path} is not valid JSON: {e}", err=True)
        raise typer.Exit(1) from e
    except AgentosError as e:
        _fail(e)
    for r in rows:
        typer.echo(f"{r['flow']}  {r['version']}  {'installed' if r['created'] else 'unchanged'}  "
                   f"contract {r['contract'][:23]}")
    _reload_daemon()


@app.command("remove")
def remove_cmd(connector: Annotated[str, typer.Argument(help="the connector (site key), like reddit")],
               yes: Annotated[bool, typer.Option("--yes", "-y", help="do not ask")] = False) -> None:
    """Remove an installed connector: its operations and their MCP tools. The accounts and pages it was bound
    to stay bound."""
    from agentos.runner.cli import OWNER, _service

    svc = ConnectorService(_service().flows)
    ops = [r["operation"] for r in svc.describe() if r["operation"].split(".")[0] == connector]
    if ops and not yes:
        typer.confirm(f"Remove {connector} ({', '.join(sorted(ops))})? Agents lose its tools.", abort=True)
    try:
        removed = svc.remove(connector, by=OWNER)
    except AgentosError as e:
        _fail(e)
    for flow_id in removed:
        typer.echo(f"{flow_id}  removed")
    _reload_daemon()


def _reload_daemon() -> None:
    """Ask a running agentosd to serve the new tool list now, and say what changed. No daemon (or
    an older one): it serves the tools when it starts, or within seconds by its own poll."""
    from agentos.core.config import home_from_env
    from agentos.core.layout import ops_socket
    from agentos.core.system import Host

    resp = Host().unix_request(ops_socket(home_from_env()), {"op": "reload_tools"}, timeout=30)
    if resp is None:
        typer.echo("agentosd is not running here; it serves these tools when it starts.")
        return
    if not resp.get("ok"):
        typer.echo("This agentosd is older than live tool reload, so it serves the new tools only after a "
                   "restart: agentos node restart (it names the lanes that would close first).")
        return
    tools = resp.get("tools") or {}
    parts = [f"{label}: {', '.join(tools[key])}" for key, label in
             (("added", "new tools"), ("changed", "updated tools"), ("removed", "tools removed")) if tools.get(key)]
    typer.echo("agentosd " + ("; ".join(parts) if parts else "already served these tools") +
               " (no restart, no lane closed).")


@app.command("verify")
def verify_cmd(operation: Annotated[str, typer.Argument(help="site.op, like reddit.comment")],
               note: Annotated[str, typer.Option(help="what the test run proved: the throwaway account, the "
                                                      "object it made and how it was read back")] = "",
               dir: Annotated[Path | None, typer.Option("--dir", help="mark the connector in this directory (and record "
                                                                      "the mark in its connector.json); default: the "
                                                                      "copy installed on this node")] = None,
               day: Annotated[str | None, typer.Option("--date", help="the test run's date, YYYY-MM-DD "
                                                                      "(default: today)")] = None,
               revoke: Annotated[bool, typer.Option(help="clear the mark: a later test run failed")] = False,
               install: Annotated[bool, typer.Option(help="with --dir: install the new version on this node")] = True,
               ) -> None:
    """Mark an operation verified after a test run on a throwaway account (owner only). The mark goes into
    this node's record (AGENTOS_HOME/connector-marks.json) and names the code the test ran: an edit to the
    operation makes it stale. Marks in connector files or bundles are information only."""
    from datetime import date

    from agentos.core.config import home_from_env
    from agentos.runner.connectors.marks import MarkLedger
    from agentos.runner.connectors.verify import mark_installed, mark_verified

    site, _, op = operation.removeprefix("connector.").partition(".")
    if not site or not op or "." in op:
        typer.echo("error: name the operation as site.op, like reddit.comment", err=True)
        raise typer.Exit(1)
    try:
        when = date.fromisoformat(day) if day else date.today()
    except ValueError:
        typer.echo(f"error: --date {day!r} is not YYYY-MM-DD", err=True)
        raise typer.Exit(1) from None
    ledger = MarkLedger(home_from_env())
    if dir is not None:
        try:
            doc, version, proof = mark_verified(dir, op, note=note, day=when, revoke=revoke, ledger=ledger)
        except AgentosError as e:
            _fail(e)
        mark = doc["operations"][op].get("verified") or "not verified"
        typer.echo(f"{site}.{op}: {mark}\n  proof {proof}\n{dir} is now {site} {version}")
        if install:
            install_cmd(dir, trust=None, force=False)
        return
    from agentos.runner.cli import _service

    fv = _service().flows.store.get_flow(f"connector.{site}.{op}")
    meta = (fv.definition.connector or {}) if fv is not None else {}
    try:
        got = mark_installed(ledger, f"{site}.{op}", fv.definition.canonical() if fv is not None else None,
                             domain=str(fv.definition.domain or site) if fv is not None else site,
                             note=note, day=when, revoke=revoke, version=meta.get("version"))
    except AgentosError as e:
        _fail(e)
    if got is None:
        typer.echo(f"{site}.{op}: not verified on this node")
    else:
        typer.echo(f"{site}.{op}: {got['text']}\n  proof {got['proof']} (installed {meta.get('version')})")


@app.command("list")
def list_cmd(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List installed connector operations with their versions, hashes and marks. Learned connectors
    (<site>-profile) show whether the node's replays verified them."""
    from agentos.core.config import home_from_env
    from agentos.runner.cli import _service
    from agentos.runner.connectors.marks import BY_REPLAY, MarkLedger
    from agentos.runner.models import LEARNED_KIND

    svc = ConnectorService(_service().flows)
    ledger = MarkLedger(home_from_env())
    defs = {fv.id: fv.definition.canonical() for fv in svc.installed()}
    rows = svc.describe()
    for r in rows:
        state = ledger.state(r["operation"], defs.get(r["flow"]))
        learned = r.get("kind") == LEARNED_KIND
        if state is not None and state.by == BY_REPLAY and not learned:
            state = None  # a replay's mark counts for a learned operation only
        r["verified"] = "" if state is None else ("stale" if state.stale else state.text)
        r["verified_by"] = None if state is None or state.stale else state.by
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("No connectors installed.")
    for r in rows:
        mark = {"": "not verified", "stale": "verified_stale"}.get(r["verified"], "verified")
        if mark == "verified" and r["verified_by"] == BY_REPLAY:
            mark = "verified by replay"
        source = ("learned on this node" if r.get("kind") == LEARNED_KIND else
                  "signed " + r["signer"] if r["signer"] else "unsigned (repo)")
        typer.echo(f"{r['operation']:<32} {r['version']:<8} {r['effect']:<7} {r['implementation']}  "
                   f"{source}  {mark}")


# ---------------------------------------------------------------- surfaces and runs


def _runner() -> Any:
    from agentos.runner.cli import _service

    return _service()


@app.command("bind")
def bind_cmd(surface: Annotated[str, typer.Argument(help="the account or page id, like biz-a/facebook-page")],
             site: Annotated[str, typer.Option(help="the site key, which is also the connector: facebook")],
             lane: Annotated[str, typer.Option(help="the login it acts through: personal/facebook")],
             acts_as: Annotated[str, typer.Option(help="account, fb_page, li_company or ig_business")],
             id: Annotated[str, typer.Option("--id", help="the account's user id, or the Page or company id")],
             name: Annotated[str | None, typer.Option(help="what you call it: biz-a, @biza.coffee")] = None,
             language: Annotated[str, typer.Option(help="the account's UI language (two letters)")] = "en",
             title: Annotated[str | None, typer.Option(help="a label for the panel")] = None) -> None:
    """Add or change a surface (an account or page a connector acts as): who it acts as, and through which
    lane."""
    from agentos.runner.cli import OWNER

    kind_id = {"user_id": id} if acts_as == "account" else {"id": id}
    data = {"id": surface, "site": site, "lane": lane, "account_language": language, "title": title,
            "acts_as": {"kind": acts_as, "name": name, **kind_id}}
    try:
        s, created = _runner().surfaces.put(data, by=OWNER)
    except AgentosError as e:
        _fail(e)
    typer.echo(f"{s.id}  {'bound' if created else 'updated'}: {s.site} as {s.acts_as.label()} through {s.lane} "
               f"({s.account_language})")


@app.command("unbind")
def unbind_cmd(surface: Annotated[str, typer.Argument(help="the account or page id")]) -> None:
    """Remove a surface (an account or page a connector acts as). Refused while a schedule posts to it."""
    from agentos.runner.cli import OWNER

    try:
        s = _runner().surfaces.remove(surface, by=OWNER)
    except AgentosError as e:
        _fail(e)
    typer.echo(f"{s.id}  removed")


@app.command("surfaces")
def surfaces_cmd(site: Annotated[str | None, typer.Option(help="only this site")] = None,
                 as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List surfaces (the accounts and pages connectors act as): who each one acts as, and through
    which lane."""
    rows = [s.public() for s in _runner().surfaces.list(site=site)]
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("No surfaces yet (agentos connector bind adds one).")
    for r in rows:
        a = r["acts_as"]
        who = f"{a.get('name') or a.get('user_id') or a.get('id')} ({a['kind']})"
        typer.echo(f"{r['id']:<28} {r['site']:<10} {who:<32} through {r['lane']}  {r['account_language']}")


@app.command("run")
def run_cmd(operation: Annotated[str, typer.Argument(help="site.op, like instagram.post_photo")],
            on: Annotated[str, typer.Option(help="the account or page to act as")],
            param: Annotated[list[str] | None, typer.Option("--param", "-p", help="key=value (JSON values ok)")] = None,
            params_file: Annotated[Path | None, typer.Option(help="a JSON file of params")] = None,
            wait: Annotated[float, typer.Option(help="seconds to wait for the daemon to finish it")] = 0.0,
            key: Annotated[str | None, typer.Option("--idempotency-key")] = None) -> None:
    """Queue a connector operation on a surface (an account or page), through the lane it is bound to; the
    daemon runs it."""
    from agentos.core.models import RunStatus
    from agentos.runner.cli import OWNER, _params

    svc = _runner()
    flow_id = operation if operation.startswith("connector.") else f"connector.{operation}"
    try:
        run, created = svc.submit(flow_id=flow_id, params=_params(param, params_file), surface=on,
                                  idempotency_key=key, principal=OWNER, source="cli", authorized=True)
    except AgentosError as e:
        _fail(e)
    typer.echo(f"{run.id} {'queued' if created else 'already exists: ' + str(run.status)} on {run.surface} "
               f"through {run.lane}")
    deadline = time.monotonic() + wait
    while wait and time.monotonic() < deadline:
        run = svc.get(run.id)
        if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
            break
        time.sleep(0.5)
    if wait:
        typer.echo(f"{run.id} {run.status}: {svc.describe(run)['summary']}")


@app.command("health")
def health_cmd(operation: Annotated[str | None, typer.Argument(help="site.op; all when left out")] = None,
               clear: Annotated[bool, typer.Option(help="forget the operation's health (after a fix by hand)")] = False,
               on: Annotated[str | None, typer.Option(help="with --clear: only this account or page")] = None,
               as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Each operation's health per surface (account or page): healthy, broken, held (paused for a reason) or
    unknown."""
    health = _runner().health
    if clear:
        if not operation:
            typer.echo("error: --clear needs the operation (site.op)", err=True)
            raise typer.Exit(1)
        n = health.clear(operation, on)
        typer.echo(f"cleared {n} health record(s) of {operation}")
        return
    rows = health.list(operation)
    if as_json:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        typer.echo("No connector runs recorded yet.")
    for r in rows:
        detail = f"  {r.get('code')} at step {r.get('step')} since {r.get('since')}" if r["state"] == "broken" else ""
        typer.echo(f"{r['operation']:<32} {r['surface']:<28} {r['state']}{detail}")
