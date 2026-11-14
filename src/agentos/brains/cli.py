"""`agentos brains ...`: register brains, sign them in, order them, and watch tasks.

Credentials are read from stdin or a file, never from argv (argv shows up in `ps` and shell
history), and are never printed.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from agentos.brains import cliprobe
from agentos.brains.confine import BrainUnconfined
from agentos.brains.credentials import DEFAULT_CHILD_REF
from agentos.brains.models import BrainKind, BrainSpec, Budget, Privacy, Runner, TaskStatus
from agentos.brains.service import BrainService, build
from agentos.core import catalog
from agentos.core.config import config_problems, load_config
from agentos.core.errors import AgentosError
from agentos.core.models import Principal
from agentos.core.runtime import Runtime

app = typer.Typer(no_args_is_help=True, help="Brains: run your agent on the node with your own subscription.")
tasks_app = typer.Typer(no_args_is_help=True, help="Brain tasks: list, follow, cancel, resume.")
app.add_typer(tasks_app, name="tasks")

# The composition root (agentos.cli) may set this so the CLI shares the daemon's services
# (vault, policy). By default the CLI builds a minimal runtime with just the node config.
runtime_factory: Callable[[], Runtime] | None = None

OWNER = Principal(kind="human", id="owner-cli", owner="owner", grant_id="owner")  # root on the node
NO_VAULT = ("no vault is available on this node, so the credential would be stored in a plain file. "
            "Create one with `agentos vault init` (or unseal it), or pass --insecure-dev-store to accept "
            "an unencrypted development file.")
NONE = "–"


def _service() -> BrainService:
    if runtime_factory is not None:
        rt = runtime_factory()
    else:
        rt = Runtime(config=load_config())
    svc = rt.get("brains")
    return svc if svc is not None else build(rt)


def _run(coro_fn: Callable[[BrainService], Any]) -> Any:
    svc = _service()
    try:
        out = coro_fn(svc)
        if asyncio.iscoroutine(out):
            out = asyncio.run(out)
        return out
    except AgentosError as exc:
        typer.echo(catalog.cli_error(exc.code, exc.message), err=True)
        raise typer.Exit(1) from None
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None


def _guard_dev_store(svc: BrainService, insecure_dev_store: bool) -> None:
    """Crown-jewel credentials go to the vault; the plain dev file only when asked for by name."""
    if svc.creds.backend == "dev-file" and not insecure_dev_store:
        typer.echo(f"error: {NO_VAULT}", err=True)
        raise typer.Exit(1)


def _read_secret(stdin: bool, auth_file: Path | None, prompt: str) -> str:
    if auth_file is not None:
        return auth_file.expanduser().read_text()
    if stdin:
        return sys.stdin.read().strip()
    return typer.prompt(prompt, hide_input=True).strip()


def _table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    typer.echo("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    for r in rows:
        typer.echo("  ".join(c.ljust(w) for c, w in zip(r, widths)))


def _fmt(v: Any) -> str:
    return NONE if v is None or v == "" else str(v)


# ------------------------------------------------------------------ brains
@app.command("add")
def add(
    name: str,
    kind: Annotated[BrainKind, typer.Option(help="subscription, local or metered")],
    provider: str = typer.Option(..., help="anthropic, openai, ollama, openai-compatible; for a decision "
                                           "brain (one that answers typed questions): typesafe or "
                                           "openrouter"),
    model: str | None = typer.Option(None, help="model name or alias (default: the CLI's default)"),
    base_url: str | None = typer.Option(None, help="local brains: the model server URL on your network; "
                                                   "metered openai-compatible: the endpoint, up to and "
                                                   "including /v1; typesafe and openrouter: replaces the "
                                                   "built-in one (https only)"),
    seats: int | None = typer.Option(None, help="concurrent tasks (subscriptions: keep at 1 or 2)"),
    context: int | None = typer.Option(None, help="context window in tokens"),
    vision: bool | None = typer.Option(None, "--vision/--no-vision"),
    tier: int | None = typer.Option(None, help="1 small, 2 standard, 3 frontier"),
    window_hours: float | None = typer.Option(None, help="the provider's rolling usage window"),
    window_tokens: int | None = typer.Option(None, help="your estimate of the window capacity"),
    price_in: float | None = typer.Option(None, help="metered: USD per million input tokens"),
    price_out: float | None = typer.Option(None, help="metered: USD per million output tokens"),
    stdin: bool = typer.Option(False, "--stdin", help="read the credential from stdin"),
    auth_file: Annotated[Path | None, typer.Option(help="codex subscription: path to ~/.codex/auth.json")] = None,
    no_credential: bool = typer.Option(False, help="register now, sign in later with `brains auth`"),
    insecure_dev_store: bool = typer.Option(False, "--insecure-dev-store",
                                            help="without a vault, store the credential unencrypted (dev only)"),
) -> None:
    """Register a brain. Subscriptions are bring-your-own, for your own use on your own node."""
    caps: dict[str, Any] = {k: v for k, v in {"context_tokens": context, "vision": vision, "tier": tier}.items()
                            if v is not None}
    try:
        spec = BrainSpec.template(
            name, kind, provider, model=model, base_url=base_url, seats=seats, window_hours=window_hours,
            window_tokens=window_tokens, price_in_per_mtok=price_in, price_out_per_mtok=price_out,
            caps=caps or None)
    except ValidationError as exc:  # its own text quotes the input, and a URL can hold a password
        typer.echo(f"error: {config_problems(exc)}", err=True)
        raise typer.Exit(1) from None
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None
    credential = None
    if spec.credential and not no_credential:
        hint = {"oauth_token": "Token from `claude setup-token`", "codex_auth": "Contents of ~/.codex/auth.json",
                "api_key": "API key"}[spec.credential]
        credential = _read_secret(stdin, auth_file, hint)
    def go(svc: BrainService):
        if credential:
            _guard_dev_store(svc, insecure_dev_store)
        return svc.add_brain(spec, credential=credential)
    info = _run(go)
    typer.echo(f"added {name}: {info['kind']} via {info['runner']}, state {info['state']}"
               + (f" ({info['state_reason']})" if info["state_reason"] else ""))


CLAUDE_TOKEN_PREFIX = "sk-ant-oat01-"  # public, and the same for everyone: what follows it is the secret
CLAUDE_TOKEN_RE = re.compile(CLAUDE_TOKEN_PREFIX + r"[A-Za-z0-9_-]{20,}")
TOKEN_RUN_RE = re.compile(CLAUDE_TOKEN_PREFIX + r"[A-Za-z0-9_-]*")
TOKEN_SHOWN = "[kept for the vault]"
TERMINAL_CODES_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[ -/]*[0-Z\\^-~]")
UNFINISHED_CODE_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*|\][^\x07\x1b]*\x1b?|[ -/]*)\Z")
SIGNIN_COLUMNS = 400  # wide enough that the CLI never wraps the token across two lines
SIGNIN_TIMEOUT_S = 600.0


def without_codes(raw: str) -> tuple[str, list[int]]:
    """`raw` with its terminal codes taken out, and where in `raw` each character left came from."""
    plain, where, at = [], [], 0
    for code in TERMINAL_CODES_RE.finditer(raw):
        plain.append(raw[at:code.start()])
        where.extend(range(at, code.start()))
        at = code.end()
    plain.append(raw[at:])
    where.extend(range(at, len(raw)))
    return "".join(plain), where


def prefix_at_end(plain: str) -> int | None:
    """Where the end of `plain` could be the start of a token, or None."""
    for at in range(max(len(plain) - len(CLAUDE_TOKEN_PREFIX) + 1, 0), len(plain)):
        if CLAUDE_TOKEN_PREFIX.startswith(plain[at:]):
            return at
    return None


class TokenMask:
    """What a person sees of the CLI's output, and the token kept out of it.

    A read can end anywhere, and a token can have terminal codes inside it, so each read is looked
    at together with what was held back, with the codes taken out. Text that could still become a
    token waits here until the next character settles it, and so does a code cut in two. Nothing
    waits for a line to end: the CLI asks for the code on a line it never ends."""

    def __init__(self) -> None:
        self.held = ""
        self.token: str | None = None

    def feed(self, text: str) -> str:
        return self._through(self.held + text, ended=False)

    def finish(self) -> str:
        """The output is over: a token cut short stays hidden, and anything else may show."""
        return self._through(self.held, ended=True)

    def _through(self, raw: str, *, ended: bool) -> str:
        unfinished = None if ended else UNFINISHED_CODE_RE.search(raw)
        stop = unfinished.start() if unfinished else len(raw)
        plain, where = without_codes(raw[:stop])
        shown, at = "", 0
        for run in TOKEN_RUN_RE.finditer(plain):
            if run.end() == len(plain) and not ended:  # the next character may still belong to it
                stop = where[run.start()]
                break
            if self.token is None and CLAUDE_TOKEN_RE.fullmatch(run.group()):
                self.token = run.group()
            shown += raw[at:where[run.start()]] + TOKEN_SHOWN
            at = where[run.end() - 1] + 1
        else:
            start = None if ended else prefix_at_end(plain)
            if start is not None:
                stop = where[start]
        self.held = raw[stop:]
        return CLAUDE_TOKEN_RE.sub(TOKEN_SHOWN, shown + raw[at:stop])  # a code can hold one too, say as a title


def capture_claude_token(bin_path: str, *, timeout_s: float = SIGNIN_TIMEOUT_S) -> str | None:
    """Run `claude setup-token` on a terminal of its own and keep the token it prints. The person
    sees the sign-in link and types the code as usual. The token is hidden on the way through, and
    the CLI gets a throwaway config directory, so nothing of this sign-in stays on the node."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, SIGNIN_COLUMNS, 0, 0))
    config = tempfile.mkdtemp(prefix="agentos-signin-")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TERM")}
    env.update(HOME=config, CLAUDE_CONFIG_DIR=config)
    proc = subprocess.Popen([bin_path, "setup-token"], stdin=slave, stdout=slave, stderr=slave, env=env,
                            start_new_session=True, close_fds=True)
    os.close(slave)
    keys = sys.stdin.fileno() if sys.stdin.isatty() else None
    saved = termios.tcgetattr(keys) if keys is not None else None
    mask, deadline = TokenMask(), time.monotonic() + timeout_s
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        if keys is not None:
            tty.setraw(keys)
        while mask.token is None and time.monotonic() < deadline:
            ready, _, _ = select.select([master, *([keys] if keys is not None else [])], [], [], 0.2)
            if keys in ready:
                os.write(master, os.read(keys, 1024))
            if master not in ready:
                if proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(master, 4096)
            except OSError:  # Linux ends a closed terminal with EIO
                break
            if not data:
                break
            sys.stdout.write(mask.feed(decoder.decode(data)))
            sys.stdout.flush()
        sys.stdout.write(mask.finish())
        sys.stdout.flush()
        return mask.token
    finally:
        if saved is not None:
            termios.tcsetattr(keys, termios.TCSADRAIN, saved)
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        os.close(master)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        shutil.rmtree(config, ignore_errors=True)


@app.command("signin")
def signin(
    provider: str = typer.Argument("claude", help="Who to sign in with. Only claude for now."),
    name: str = typer.Option("claude-haiku", help="What to call this brain."),
    model: str = typer.Option("claude-haiku-5-5", help="The model that helps."),
    effort: str = typer.Option("xhigh", help="How hard it thinks."),
    stdin: bool = typer.Option(False, "--stdin", help="read a token from `claude setup-token` from stdin instead"),
    insecure_dev_store: bool = typer.Option(False, "--insecure-dev-store",
                                            help="without a vault, store the credential unencrypted (dev only)"),
) -> None:
    """Sign in with your Claude subscription, so a small model can help a worker that is unsure.

    No API key: this uses your own subscription, for your own use on your own node. Signing in
    again replaces the token."""
    if provider != "claude":
        typer.echo(f"error: signing in with {provider} is not supported yet; use `agentos brains add`", err=True)
        raise typer.Exit(1)

    async def go(svc: BrainService):
        _guard_dev_store(svc, insecure_dev_store)
        if stdin:
            token = "".join(sys.stdin.read().split())  # a terminal wraps a long token, and copying keeps the break
        else:
            info = svc.probe_runner(Runner.CLAUDE)
            if not info.path:
                typer.echo("error: the claude CLI is not installed on this node; run install_cli.sh", err=True)
                raise typer.Exit(1)
            token = capture_claude_token(info.path)
            typer.echo("")
        if not token or not CLAUDE_TOKEN_RE.fullmatch(token):
            typer.echo("error: no sign-in token came back; nothing was saved. Try again, or paste one with --stdin",
                       err=True)
            raise typer.Exit(1)
        if svc.store.get_brain(name) is not None:
            await svc.set_credential(name, token)
            typer.echo(f"signed in again: {name}")
            return
        spec = BrainSpec.template(name, BrainKind.SUBSCRIPTION, "anthropic", model=model, effort=effort, seats=2)
        info = await svc.add_brain(spec, credential=token)
        typer.echo(f"signed in: {name} ({model}), state {info['state']}"
                   + (f" ({info['state_reason']})" if info["state_reason"] else ""))
    _run(go)


@app.command("auth")
def auth(name: str, stdin: bool = typer.Option(False, "--stdin", help="read the credential from stdin"),
         auth_file: Annotated[Path | None, typer.Option(help="codex subscription: path to ~/.codex/auth.json")]
         = None,
         insecure_dev_store: bool = typer.Option(False, "--insecure-dev-store",
                                                 help="without a vault, store the credential unencrypted (dev only)"),
         ) -> None:
    """Store or replace a brain's credential (after `claude setup-token` or `codex login`)."""
    async def go(svc: BrainService):
        spec = svc.get_spec(name)
        if not spec.credential:
            typer.echo(f"{name} does not use a credential")
            return
        _guard_dev_store(svc, insecure_dev_store)
        await svc.set_credential(name, _read_secret(stdin, auth_file, "Credential"))
        typer.echo(f"stored the credential for {name} ({svc.creds.backend})")
    _run(go)


@app.command("list")
def list_cmd(as_json: bool = typer.Option(False, "--json")) -> None:
    """Brains on this node, in routing order."""
    rows = _run(lambda svc: svc.list_brains())
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    _table([[r["name"], r["kind"], r["provider"], r["state"], f"{r['seats']['in_use']}/{r['seats']['total']}",
             _fmt(r["window_used_pct"]), _fmt(r["cli"]["version"]), _fmt(r["state_reason"])] for r in rows],
           ["NAME", "KIND", "PROVIDER", "STATE", "SEATS", "WINDOW%", "CLI", "REASON"])


@app.command("remove")
def remove(name: str) -> None:
    """Remove a brain and delete its stored credential."""
    _run(lambda svc: svc.remove_brain(name))
    typer.echo(f"removed {name}")


@app.command("order")
def order(names: list[str]) -> None:
    """Set the order within each kind (subscription, then local, then metered)."""
    final = _run(lambda svc: svc.set_order(names))
    typer.echo("order: " + ", ".join(final))


@app.command("use")
def use(name: str, workspace: str | None = typer.Option(None, help="default for this workspace only")) -> None:
    """Make a brain the default for a workspace (or the whole node)."""
    out = _run(lambda svc: svc.use(name, workspace) if workspace else svc.use_node_default(name))
    typer.echo(f"default brain for {out['workspace'] or 'this node'}: {out['default_brain']}")


@app.command("privacy")
def privacy(workspace: str, level: Privacy) -> None:
    """Set where a workspace's prompts may go: local-only, redacted-cloud or full-cloud."""
    out = _run(lambda svc: svc.set_privacy(workspace, level))
    typer.echo(f"{out['workspace']}: {out['privacy']}")


@app.command("child-token")
def child_token(stdin: bool = typer.Option(False, "--stdin")) -> None:
    """Store the grant token brains use to call back into agentos when the node has no policy
    service to mint per-task child grants. Never use the owner token here."""
    async def go(svc: BrainService):
        token = _read_secret(stdin, None, "Child grant token")
        if await svc.ports.is_owner_token(token):
            typer.echo("error: that is the owner token; brains must get a narrower child grant", err=True)
            raise typer.Exit(1)
        await svc.creds.put(DEFAULT_CHILD_REF, token, kind="grant_token", label="default brain child grant")
        typer.echo(f"stored ({svc.creds.backend})")
    _run(go)


@app.command("test")
def test(name: str, live: bool = typer.Option(False, help="also run a one-turn task on the real brain"),
         timeout: float = typer.Option(180, help="seconds to wait for the live task")) -> None:
    """Check a brain: its CLI is installed and its flags are unchanged, its credential is stored, and its
    state is ok. With --live, run one short task and report what came back. A decision brain (one that
    answers typed questions, with no CLI) runs no task, so --live asks it one harmless question and reports
    the latency and the tokens."""
    async def go(svc: BrainService):
        spec = svc.get_spec(name)
        if spec.caps.decisions:
            await _test_decision_brain(svc, spec, live)
            return
        info = svc.probe(spec)
        typer.echo(f"cli: {spec.runner.value} {_fmt(info.version)} at {_fmt(info.path)} "
                   f"(verified {cliprobe.PINNED[spec.runner.value]['verified']})")
        for p in info.problems:
            typer.echo(f"  problem: {p}")
        for w in info.warnings:
            typer.echo(f"  warning: {w}")
        state, reason = await svc.effective_state(spec)
        typer.echo(f"state: {state.value}" + (f" ({reason})" if reason else ""))
        typer.echo(f"credentials: {svc.creds.backend}" + (
            " (DEVELOPMENT FALLBACK FILE, not encrypted; configure the vault)" if svc.creds.backend == "dev-file"
            else ""))
        if not live:
            return
        r = await svc.submit(OWNER, prompt="Reply with exactly the word OK and nothing else.",
                             workspace="agentos-test", brain=name,
                             budget=Budget(max_turns=2, max_minutes=max(timeout / 60, 1), max_usd=0.05),
                             wait_prepare=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            await svc.tick()
            st = svc.status(r["id"])
            if st["status"] not in ("queued", "starting", "running"):
                break
            await asyncio.sleep(1)
        st = svc.status(r["id"])
        typer.echo(f"live task {r['id']}: {st['status']} ({_fmt(st['status_reason'])})")
        if st.get("result_preview"):
            typer.echo(f"reply: {st['result_preview'][:200]}")
        if st["status"] in ("queued", "starting", "running"):
            await svc.cancel(r["id"], wait=True)
            typer.echo("timed out; cancelled the live task")
            raise typer.Exit(1)
        if st["status"] != TaskStatus.SUCCEEDED_UNVERIFIED.value:
            raise typer.Exit(1)
    _run(go)


async def _test_decision_brain(svc: BrainService, spec: BrainSpec, live: bool) -> None:
    typer.echo(f"endpoint: {spec.base_url}/systemone, model {spec.model} (runs in process, no CLI)")
    state, reason = await svc.effective_state(spec)
    typer.echo(f"state: {state.value}" + (f" ({reason})" if reason else ""))
    typer.echo(f"credentials: {svc.creds.backend}" + (
        " (DEVELOPMENT FALLBACK FILE, not encrypted; configure the vault)" if svc.creds.backend == "dev-file" else ""))
    if not live:
        return
    try:
        r = await svc.probe_decision(OWNER, spec.name)
    finally:
        await svc.wire.aclose()
    usage = r["usage"]
    tokens = f"{usage['input_tokens']} tokens in, {usage['output_tokens']} out"
    typer.echo(f"live decision on {r['brain']} ({r['model']}): {r['latency_ms']} ms, "
               f"{tokens}{' (estimated)' if usage['estimated'] else ''}, ${r['cost_usd']:.6f}")
    typer.echo(f"answer: {json.dumps(r['answers'])}")


@app.command("doctor")
def doctor() -> None:
    """CLI versions and flag checks for every runner, isolation, brain user, credential store."""
    svc = _service()
    for runner in Runner:
        info = svc.probe_runner(runner)
        typer.echo(f"{runner.value}: {_fmt(info.version)} ({'ok' if info.ok else 'PROBLEM'}) "
                   f"verified {cliprobe.PINNED[runner.value]['verified']}")
        for p in info.problems + info.warnings:
            typer.echo(f"  {p}")
    try:
        ident = svc.brain_identity()
    except BrainUnconfined as exc:
        typer.echo(f"brain user: REFUSED: {exc.message}")
    else:
        helper = ident.helper
        scoped = ident.scopes if helper else svc._use_scope()
        typer.echo(f"isolation: {'systemd scope' if scoped else 'process group + rlimits'}"
                   f"{', a PID namespace per brain' if helper else ''}")
        typer.echo(f"brain user: {f'{ident.name} (uid {ident.uid})' if ident.name else 'same as agentos (not isolated)'}"
                   f"{f' (through agentos-runas at {helper})' if helper else ''}")
    if svc.confinement_required():
        r = svc.confinement().check()
        typer.echo(f"confinement: {'ok' if r.ok else 'REFUSED'}: {r.reason}")
    else:
        typer.echo(f"confinement: not checked (confinement = {svc.settings.confinement})")
    typer.echo(f"credentials: {svc.creds.backend}")
    typer.echo(f"mcp callback: {svc.mcp_url()}")


@app.command("confinement")
def confinement(as_json: bool = typer.Option(False, "--json")) -> None:
    """Run the isolation check now, as the brain user: can it reach the node's secrets? Exit 1 if so."""
    svc = _service()
    r = svc.confinement().check(force=True)
    if as_json:
        typer.echo(json.dumps(r.to_dict(), indent=2))
    else:
        typer.echo(f"{'ok' if r.ok else 'REFUSED'}: {r.reason} ({r.launcher})")
        for item in r.reachable:
            typer.echo(f"  reachable: {item}")
    if not r.ok:
        raise typer.Exit(1)


# ------------------------------------------------------------------ tasks
@tasks_app.command("list")
def tasks_list(status: str | None = typer.Option(None), workspace: str | None = typer.Option(None),
               limit: int = 30, as_json: bool = typer.Option(False, "--json")) -> None:
    """Recent brain tasks, newest first."""
    rows = _run(lambda svc: svc.list_tasks(status=status, workspace=workspace, limit=limit))
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    _table([[r["id"], r["workspace"], r["status"], _fmt(r["brain"]), str(r["attempt"]), str(r["tokens"]),
             f"{r['cost_usd']:.2f}", (r["prompt_preview"] or "")[:40]] for r in rows],
           ["ID", "WORKSPACE", "STATUS", "BRAIN", "TRY", "TOKENS", "USD", "PROMPT"])


@tasks_app.command("show")
def tasks_show(task_id: str) -> None:
    """Full status of one task (JSON)."""
    typer.echo(json.dumps(_run(lambda svc: svc.status(task_id)), indent=2))


@tasks_app.command("tail")
def tasks_tail(task_id: str, since: int | None = typer.Option(None), limit: int = 50,
               follow: bool = typer.Option(False, "--follow", "-f", help="keep printing until it ends")) -> None:
    """Print transcript events. Ctrl-C stops following; the task keeps running."""
    svc = _service()
    cursor = since
    try:
        while True:
            data = svc.tail(task_id, since=cursor, limit=limit)
            if data.get("gap"):
                typer.echo(f"(older events were rotated away; showing from {data['first_available']})")
            for ev in data["events"]:
                typer.echo(f"{ev['seq']:>6} {ev.get('kind', ''):<11} {(ev.get('text') or '')[:300]}")
            cursor = data["next"]
            if not follow or data["status"] not in ("queued", "starting", "running"):
                if follow:
                    typer.echo(f"[{data['status']}] {_fmt(data.get('status_reason'))}")
                return
            time.sleep(1)
    except KeyboardInterrupt:
        return
    except AgentosError as exc:
        typer.echo(catalog.cli_error(exc.code, exc.message), err=True)
        raise typer.Exit(1) from None


@tasks_app.command("cancel")
def tasks_cancel(task_id: str, wait: bool = typer.Option(True, help="wait until it has stopped")) -> None:
    """Cancel a task: a graceful stop, then a kill."""
    out = _run(lambda svc: svc.cancel(task_id, wait=wait))
    typer.echo(f"{task_id}: {out['status']} ({_fmt(out['status_reason'])})")


@tasks_app.command("resume")
def tasks_resume(task_id: str, brain: str | None = typer.Option(None)) -> None:
    """Resume an interrupted, failed or cancelled task with its transcript as context."""
    out = _run(lambda svc: svc.resume(task_id, brain=brain))
    typer.echo(f"{task_id}: {out['status']} ({_fmt(out['status_reason'])})")

