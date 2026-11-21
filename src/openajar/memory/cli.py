"""`openajar memory ...`: workspace memory for the owner, on the node.

Whoever runs this has a shell on the node, so it is the owner in person (`Caller.owner()`):
what it writes is owner trust, `confirm` promotes an agent's note to owner trust, and `forget`
erases without asking anyone else. It works on the node's files directly, like `openajar lane`,
so it needs no running daemon. Agents use the MCP tools instead (openajar.memory.tools).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from openajar.core import catalog
from openajar.core.config import home_from_env, load_config
from openajar.core.errors import OpenajarError
from openajar.core.runtime import Runtime
from openajar.memory.markdown import markdown_export
from openajar.memory.models import Caller, EntryType
from openajar.memory.ports import MemoryPorts, RuntimePolicyPort, runtime_vault_scrub
from openajar.memory.service import MemoryService

app = typer.Typer(no_args_is_help=True, help="Workspace memory: what agents and the owner wrote, with provenance.")

JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")]
TypeOpt = Annotated[list[str] | None, typer.Option("--type", "-t", help="fact, document, decision, preference, "
                                                                          "contact or run_summary (repeatable)")]
OWNER = Caller.owner(session="cli")


def _fail(message: str, code: str = "error") -> NoReturn:
    typer.secho(f"error ({code}): {message}", err=True, fg=typer.colors.RED)
    raise typer.Exit(1)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except OpenajarError as exc:
        typer.secho(catalog.cli_error(exc.code, exc.message), err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None


def _privacy_for(home: Path):
    """The brains package's privacy level per workspace, read from its files (full-cloud, the
    brains default, without them)."""
    def privacy_for(workspace: str) -> str:
        db = home / "brains" / "brains.db"
        try:
            from openajar.brains.settings import load_settings
            from openajar.brains.store import Store
        except ModuleNotFoundError:
            return "full-cloud"
        default = load_settings(home).default_privacy.value
        if not db.exists():
            return default
        return Store(db).workspace(workspace).get("privacy") or default
    return privacy_for


_RUNTIME: dict[int, Runtime] = {}  # the runtime each open service was built with (its action log, for forget)


def _log_forget(svc: MemoryService, entry_id: str, workspace: str | None, reason: str) -> bool:
    """The owner's forget on the node's action log, as an agent's memory_forget is (QA 2026-11-15
    P2-12). Never the content: the entry id, its workspace and that a reason was given. False when this
    node has no action log yet (no policy.db): memory's own chain still records the tombstone."""
    runtime = _RUNTIME.get(id(svc))
    traces = runtime.get("traces") if runtime is not None else None
    if traces is None:
        return False
    from openajar.policy.traces import TraceEvent

    traces.write(TraceEvent(kind="memory_forget", tool="memory_forget", status="ok", principal="owner:cli",
                            principal_kind="human", summary=f"forgot {entry_id}",
                            args={"entry": entry_id, "workspace": workspace, "reason_given": bool(reason)}))
    return True  # a row the log could not take yet is written when the policy service closes


@contextmanager
def _service() -> Iterator[MemoryService]:
    """The memory service over this node's files, with the policy (evidence pins and checks) and
    the vault (scrubbing) when the node has them."""
    home = home_from_env()
    cfg = load_config(home) if (home / "node.toml").exists() else None
    runtime = Runtime(config=cfg) if cfg is not None else Runtime()
    closers: list[Any] = []
    if (home / "policy.db").exists():
        from openajar.policy.service import PolicyService

        policy = PolicyService(home, node_config=cfg)
        runtime.register("policy", policy)
        runtime.register("traces", policy.traces)
        closers.append(policy)
    with contextlib.suppress(ModuleNotFoundError):
        from openajar.vault.service import VaultService

        vault = VaultService(home)
        header = vault.store.header()
        if header is not None:
            if header.mode != "passphrase":
                with contextlib.suppress(OpenajarError):
                    asyncio.run(vault.ensure_unsealed())   # a key-file vault unseals itself
            runtime.register("vault", vault)
    ports = MemoryPorts(policy=RuntimePolicyPort(runtime), vault_scrub=runtime_vault_scrub(runtime),
                        privacy_for=_privacy_for(home), node_id=getattr(cfg, "name", None))
    with _errors():
        svc = MemoryService(home, ports)
    _RUNTIME[id(svc)] = runtime
    try:
        with _errors():
            yield svc
    finally:
        _RUNTIME.pop(id(svc), None)
        svc.close()
        for c in closers:
            with contextlib.suppress(Exception):
                c.close()


def _echo_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


def _read_text(path: str) -> str:
    try:
        return sys.stdin.read() if path == "-" else Path(path).read_text()
    except OSError as exc:
        _fail(f"cannot read {path}: {exc.strerror or exc}")


def _row(e: dict[str, Any]) -> str:
    flags = "".join(("S" if e.get("suspicious") else " ", "P" if e.get("pinned") else " "))
    title = e.get("title") if e.get("title") is not None else "(forgotten)"
    return f"{e['id']:<22} {e['type']:<12} {e['trust']:<10} {flags} {(e.get('at') or '')[:16]:<16} {title[:70]}"


@app.command("list")
def list_cmd(workspace: str, type: TypeOpt = None,
             trust: Annotated[str | None, typer.Option(help="at least this trust: owner, node, agent, untrusted")] = None,
             state: Annotated[list[str] | None, typer.Option(help="live, expired or forgotten (repeatable)")] = None,
             unreviewed: Annotated[bool, typer.Option(help="agent and untrusted entries not yet reviewed")] = False,
             limit: Annotated[int, typer.Option()] = 50, as_json: JsonOpt = False) -> None:
    """List a workspace's entries, newest first (titles only). S: suspicious, P: pinned."""
    with _service() as svc:
        data = svc.list_entries(OWNER, workspace, types=type, trust_min=trust, states=state, unreviewed=unreviewed,
                                limit=limit).data
    if as_json:
        _echo_json(data)
        return
    if not data["entries"]:
        typer.echo(f"no entries in {workspace}")
    for e in data["entries"]:
        typer.echo(_row(e))


@app.command("show")
def show(entry_id: str, version: Annotated[int | None, typer.Option()] = None,
         history: Annotated[bool, typer.Option(help="every version")] = False, as_json: JsonOpt = False) -> None:
    """One entry: content, trust, labels and provenance."""
    with _service() as svc:
        data = svc.get(OWNER, entry_id, version=version, history=history).data
    if as_json:
        _echo_json(data)
        return
    typer.echo(f"{data['id']}@{data['version']}  {data['type']}  {data['trust']} trust  {data['state']}"
               + (f"  key {data['key']}" if data.get("key") else ""))
    typer.echo(f"title:   {data['title'] if data['title'] is not None else '(forgotten)'}")
    typer.echo(f"labels:  {', '.join(data['labels']) or 'none'}")
    prov = data.get("prov") or {}
    who = (prov.get("principal") or {}).get("id")
    typer.echo(f"writer:  {who or data.get('writer')} at {data.get('written_at') or data['updated_at']}"
               + (f", confirmed by {prov['confirmed_by']}" if prov.get("confirmed_by") else ""))
    for ev in prov.get("evidence") or []:
        typer.echo(f"evidence: {ev.get('trace')} {ev.get('tool') or ''} {ev.get('at') or ''} {ev.get('summary') or ''}")
    if data.get("warning"):
        typer.echo(f"note:    {data['warning']}")
    if data.get("tombstone"):
        t = data["tombstone"]
        typer.echo(f"forgotten at {t['at']} by {t['by']}: {t['reason']}")
    if data.get("attrs"):
        typer.echo(f"attrs:   {json.dumps(data['attrs'], sort_keys=True)}")
    if data.get("body"):
        typer.echo("")
        typer.echo(data["body"])
    for v in data.get("history") or []:
        typer.echo(f"  version {v['version']} at {v['at']}: {v['trust']} trust, "
                   f"{'forgotten' if v['forgotten'] else (v['title'] or '')[:60]}")


@app.command("search")
def search(workspace: str, query: Annotated[str, typer.Argument()] = "", type: TypeOpt = None,
           since: Annotated[str | None, typer.Option(help="an ISO time or an age like 7d")] = None,
           limit: Annotated[int, typer.Option()] = 20, as_json: JsonOpt = False) -> None:
    """Keyword search in one workspace, owner and node entries first."""
    with _service() as svc:
        data = svc.search(OWNER, workspace, query, types=type, since=since, limit=limit).data
    if as_json:
        _echo_json(data)
        return
    if not data["hits"]:
        typer.echo("no hits")
    for h in data["hits"]:
        typer.echo(_row(h))
        if h.get("snippet"):
            typer.echo(f"{'':<22} {h['snippet'][:100]}")


@app.command("write")
def write(workspace: str, type: Annotated[str, typer.Option("--type", "-t", help="fact, document, decision, "
                                                                                    "preference or contact")],
          title: Annotated[str, typer.Option()],
          body_from: Annotated[str | None, typer.Option("--body-from", help="file with the body, or - for stdin")] = None,
          key: Annotated[str | None, typer.Option(help="a unique slug such as posting.sunday")] = None,
          attrs: Annotated[str | None, typer.Option(help="attributes as JSON, e.g. '{\"pinned\": true}'")] = None,
          supersedes: Annotated[str | None, typer.Option(help="mem_...@N: a new version of that entry")] = None,
          audience: Annotated[str, typer.Option(help="workspace, or owner (only you)")] = "workspace",
          as_json: JsonOpt = False) -> None:
    """Write an entry as the owner (trusted as the owner's own note). The body comes from a file or stdin."""
    req: dict[str, Any] = {"type": type, "title": title, "body": _read_text(body_from) if body_from else "",
                           "audience": audience}
    if key:
        req["key"] = key
    if supersedes:
        req["supersedes"] = supersedes
    if attrs:
        try:
            req["attrs"] = json.loads(attrs)
        except ValueError:
            _fail("--attrs is not valid JSON")
    with _service() as svc:
        out = svc.write(OWNER, workspace, req)
    if as_json:
        _echo_json(out)
        return
    typer.echo(f"wrote {out['ref']} ({out['type']}, {out['trust']} trust) in {workspace}")


@app.command("confirm")
def confirm(entry_id: str, version: int,
            title: Annotated[str | None, typer.Option(help="change the title first")] = None,
            body_from: Annotated[str | None, typer.Option("--body-from", help="change the body first (file or -)")] = None,
            as_json: JsonOpt = False) -> None:
    """Promote an agent's entry to owner trust (you read it and vouch for it). VERSION must be
    its current version."""
    edit: dict[str, Any] = {}
    if title:
        edit["title"] = title
    if body_from:
        edit["body"] = _read_text(body_from)
    with _service() as svc:
        out = svc.confirm(OWNER, entry_id, version, edit=edit or None)
    if as_json:
        _echo_json(out)
        return
    typer.echo(f"confirmed {entry_id}@{version} as {out['ref']} (owner trust)")


@app.command("review")
def review(entry_id: str) -> None:
    """Mark an entry reviewed without promoting it (it leaves the unreviewed list)."""
    with _service() as svc:
        out = svc.review(OWNER, entry_id)
    typer.echo(f"{out['id']} reviewed at {out['reviewed_at']}")


@app.command("forget")
def forget(entry_id: str, reason: Annotated[str, typer.Option(help="why (kept; the content is not)")],
           yes: Annotated[bool, typer.Option("--yes", "-y", help="do not ask")] = False) -> None:
    """Erase an entry's content in every version, for good. A tombstone and the hashes stay, so
    the history still verifies. Copies agents already read, earlier exports and backups are not
    reached."""
    with _service() as svc:
        row = svc.get(OWNER, entry_id).data
        if not yes:
            typer.confirm(f"Forget {entry_id} ({row['type']}: {row['title']})? This cannot be undone", abort=True)
        out = svc.forget(OWNER, entry_id, reason)
        if not out.get("already") and not _log_forget(svc, entry_id, row.get("workspace"), reason):
            typer.secho("note: this node has no action log yet, so only memory's own history records this",
                        err=True)
    if out.get("already"):
        typer.echo(f"{entry_id} was already forgotten")
        return
    typer.echo(f"forgot {entry_id}: {out['blobs_erased']} pieces of content erased in {out['versions']} version(s)")
    typer.echo(out["note"])


@app.command("verify")
def verify(workspace: Annotated[str | None, typer.Argument(help="leave out for every workspace")] = None,
           as_json: JsonOpt = False) -> None:
    """Check that a workspace's memory is intact: each entry must link to the one before it (exit 1 when it
    does not verify)."""
    with _service() as svc:
        names = [workspace] if workspace else svc.stores.workspaces()
        results = [svc.verify(ws) for ws in names]
    if as_json:
        _echo_json(results)
    else:
        if not results:
            typer.echo("no workspace has memory on this node")
        for r in results:
            state = "ok" if r["ok"] else "FAILED"
            typer.echo(f"{r['workspace']}: {state}, {r['versions']} versions, {r['forgotten']} forgotten")
            for p in r["problems"][:20]:
                typer.echo(f"  {p}")
    if not all(r["ok"] for r in results):
        raise typer.Exit(1)


@app.command("stats")
def stats(workspace: str, as_json: JsonOpt = False) -> None:
    """Live entries and content size against their limits."""
    with _service() as svc:
        out = svc.stats(workspace)
    if as_json:
        _echo_json(out)
        return
    typer.echo(f"{workspace}: {out['live_entries']} of {out['live_entries_cap']} live entries, "
               f"{out['content_bytes']} of {out['content_bytes_cap']} bytes")


def _private_file(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    path.chmod(0o600)


@app.command("export")
def export(workspace: str,
           md: Annotated[Path | None, typer.Option("--md", help="directory for one Markdown file per type")] = None,
           jsonl: Annotated[Path | None, typer.Option("--jsonl", help="file for every record (entries, versions, "
                                                                       "provenance, hashes)")] = None) -> None:
    """Write a workspace's memory out in plaintext, for reading or leaving. The files are written
    with mode 600. To move a workspace to another node use `openajar workspace export` (encrypted)."""
    if (md is None) == (jsonl is None):
        _fail("pass --md DIR or --jsonl FILE")
    typer.secho("warning: this export is plaintext; keep it private and delete it when done", err=True,
                fg=typer.colors.YELLOW)
    with _service() as svc:
        if md is not None:
            md.mkdir(parents=True, exist_ok=True, mode=0o700)
            files = markdown_export(svc, workspace)
            for name, text in files.items():
                _private_file(md / name, text)
            typer.echo(f"wrote {len(files)} file(s) to {md}")
        else:
            assert jsonl is not None
            records = svc.export_records(workspace)
            _private_file(jsonl, "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in records))
            typer.echo(f"wrote {len(records)} record(s) to {jsonl}")


def _documents_from_markdown(path: Path, text: str) -> list[dict[str, Any]]:
    title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), "") or path.stem
    return [{"type": EntryType.DOCUMENT.value, "title": title[:200], "body": text}]


@app.command("import")
def import_cmd(workspace: str, file: Path, as_json: JsonOpt = False) -> None:
    """Bring in notes you vouch for, trusted as the owner's own: a Markdown file becomes one document (its
    first heading is the title); a .jsonl file holds one entry per line ({"type", "title",
    "body", "key", "attrs"}). Agents cannot import; they use memory_write."""
    try:
        text = file.read_text()
    except OSError as exc:
        _fail(f"cannot read {file}: {exc.strerror or exc}")
    if file.suffix == ".jsonl":
        reqs = []
        for i, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                req = json.loads(line)
            except ValueError:
                _fail(f"{file}:{i} is not valid JSON")
            if not isinstance(req, dict):
                _fail(f"{file}:{i} is not an entry")
            reqs.append(req)
    else:
        reqs = _documents_from_markdown(file, text)
    written = []
    with _service() as svc:
        for req in reqs:
            svc.plan_write(OWNER, workspace, req)   # every line checked before anything is stored
        for req in reqs:
            written.append(svc.write(OWNER, workspace, req))
    if as_json:
        _echo_json(written)
        return
    for out in written:
        typer.echo(f"wrote {out['ref']} ({out['type']}, owner trust)")
