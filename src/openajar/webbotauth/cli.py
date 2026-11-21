"""`openajar identity`: a lane's signed agent identity (Web Bot Auth, Phase 1: nothing is published).

    openajar identity init acme/research --agent https://agents.example
    openajar identity hosts add acme/research https://example.com --reason "read-only research"
    openajar identity show acme/research
    openajar identity test-sign acme/research https://example.com/some/page [--send]
    openajar identity hosts remove acme/research https://example.com

These commands are the only writers of a lane's `sign_hosts` (the owner, on the node). Changes
take effect when the lane next opens. A lane with no hosts signs nothing and gets no route.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Annotated, Any

import typer

from openajar.core import catalog
from openajar.core.config import NodeConfig, home_from_env, load_config
from openajar.core.errors import OpenajarError
from openajar.core.models import parse_lane_id
from openajar.webbotauth import base, keys, settings
from openajar.webbotauth.keys import IdentityError
from openajar.webbotauth.route import TRACE_NAME
from openajar.webbotauth.settings import LaneIdentity
from openajar.webbotauth.trace import IdentityTrace
from openajar.webbotauth.verify import verify_request

app = typer.Typer(no_args_is_help=True, help="Signed agent identity (Web Bot Auth) for browser lanes.")
hosts_app = typer.Typer(no_args_is_help=True, help="The hosts a lane may sign requests to (empty by default).")
app.add_typer(hosts_app, name="hosts")

BY = "owner:cli"
STATEMENT = "On the listed hosts this lane identifies itself as an automated agent."

LaneArg = Annotated[str, typer.Argument(help="The lane, as workspace/name.")]


def _config() -> NodeConfig:
    home = home_from_env()
    if not (home / "node.toml").exists():
        raise typer.Exit(_fail(f"no node here ({home}/node.toml is missing); run openajar node init first"))
    return load_config(home)


def _fail(message: str) -> int:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    return 1


def _lane(lane_id: str) -> str:
    try:
        parse_lane_id(lane_id)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    return lane_id


def _lane_record(cfg: NodeConfig, lane_id: str) -> Any:
    from openajar.node.store import LaneStore

    rec = LaneStore(cfg.db_path).get(lane_id)
    if rec is None:
        raise typer.Exit(_fail(f"no lane {lane_id} on this node"))
    return rec


def _loaded(cfg: NodeConfig, lane_id: str) -> tuple[LaneIdentity, keys.LaneKey]:
    ident = settings.load(cfg.secrets_dir, lane_id)
    key = keys.load_key(cfg.secrets_dir, lane_id)
    if ident is None or key is None:
        raise IdentityError(f"{lane_id} has no identity yet; run: openajar identity init {lane_id} --agent <https URL>")
    return ident, key


def _state(cfg: NodeConfig, ident: LaneIdentity) -> str:
    if ident.profile is None or tuple(ident.profile) != settings.profile_binding(cfg.profile_dir(ident.lane_id)):
        return "off: the lane's profile changed since init (run init --force)"
    if not ident.hosts:
        return "off: no hosts to sign for, so the lane signs nothing and has no route"
    if not ident.signature_agent:
        return "off: no signature agent URL (run init --agent)"
    return f"signing for {len(ident.hosts)} host(s) from the lane's next launch"


def _view(cfg: NodeConfig, ident: LaneIdentity, key: keys.LaneKey) -> dict[str, Any]:
    trace = IdentityTrace(cfg.lane_dir(ident.lane_id) / TRACE_NAME)
    last = next((r for r in reversed(trace.read(500)) if r.get("kind") == "signed"), None)
    return {"lane": ident.lane_id, "state": _state(cfg, ident), "keyid": key.keyid, "public_jwk": key.public_jwk(),
            "signature_agent": ident.signature_agent, "form": ident.form, "covered": list(base.COMPONENTS),
            "window_s": base.WINDOW, "hosts": [h.model_dump(mode="json") for h in ident.hosts],
            "last_signed": {k: last.get(k) for k in ("at", "origin", "path", "status")} if last else None,
            "published": False}


def _print_view(view: dict[str, Any]) -> None:
    typer.echo(f"Lane {view['lane']}: {view['state']}.")
    typer.echo(f"  key id           {view['keyid']}")
    typer.echo(f"  signature agent  {view['signature_agent'] or 'not set'} ({view['form']} form, not published)")
    typer.echo(f"  covers           {', '.join(view['covered'])}; {view['window_s']} s per signature")
    if view["hosts"]:
        typer.echo("  hosts:")
        for h in view["hosts"]:
            typer.echo(f"    {h['origin']}  ({h['reason']}; added by {h['added_by']})")
        typer.echo(f"  {STATEMENT}")
    else:
        typer.echo("  hosts            none (off)")
    if view["last_signed"]:
        ls = view["last_signed"]
        typer.echo(f"  last signed      {ls['at']} {ls['origin']}{ls['path']} (status {ls['status']})")


@app.command("init")
def init(
    lane_id: LaneArg,
    agent: Annotated[str | None, typer.Option("--agent", help="The Signature-Agent https URL (your operator "
                                                              "origin, where Phase 2 publishes the key).")] = None,
    form: Annotated[str | None, typer.Option(help="Signature-Agent form: bare (default, what Cloudflare "
                                                  "accepts) or dict (the draft's).")] = None,
    force: Annotated[bool, typer.Option("--force", help="A new key and a new binding to the lane's profile, "
                                                        "with no hosts (signing off).")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Make the lane's identity key (or keep the one it has), bound to its profile."""
    cfg = _config()
    lane_id = _lane(lane_id)
    _lane_record(cfg, lane_id)
    binding = settings.profile_binding(cfg.profile_dir(lane_id))
    if binding is None:
        raise typer.Exit(_fail(f"{lane_id} has no profile directory yet"))
    try:
        ident = settings.load(cfg.secrets_dir, lane_id)
        if ident is not None and not force and tuple(ident.profile or ()) != binding:
            raise IdentityError(f"{lane_id} already has an identity made for another profile (a recreated or "
                                "imported lane). Run init --force: it makes a new key and clears the hosts")
        if force or ident is None:
            key = keys.create_key(cfg.secrets_dir, lane_id, replace=force)
            ident = LaneIdentity(lane_id=lane_id, signature_agent=ident.signature_agent if ident else None)
        else:
            key = keys.create_key(cfg.secrets_dir, lane_id)
        if agent is not None:
            ident.signature_agent = settings.normalize_agent_url(agent)
        if form is not None:
            if form not in base.FORMS:
                raise IdentityError(f"--form must be one of {', '.join(base.FORMS)}")
            ident.form = form  # type: ignore[assignment]
        ident.keyid, ident.profile, ident.updated_by = key.keyid, binding, BY
        settings.save(cfg.secrets_dir, ident)
    except OpenajarError as e:
        typer.secho(catalog.cli_error(e.code, e.message), err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    view = _view(cfg, ident, key)
    if as_json:
        typer.echo(json.dumps(view, indent=2, default=str))
    else:
        _print_view(view)


@app.command("show")
def show(lane_id: LaneArg, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """The lane's key id, signature agent, hosts and state. Never the private key."""
    cfg = _config()
    try:
        ident, key = _loaded(cfg, _lane(lane_id))
    except OpenajarError as e:
        typer.secho(catalog.cli_error(e.code, e.message), err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    view = _view(cfg, ident, key)
    if as_json:
        typer.echo(json.dumps(view, indent=2, default=str))
    else:
        _print_view(view)


@hosts_app.command("add")
def hosts_add(
    lane_id: LaneArg,
    origin: Annotated[str, typer.Argument(help="https://host[:port] (a bare host means https).")],
    reason: Annotated[str, typer.Option("--reason", help="Why this lane may declare itself here.")],
) -> None:
    """Let the lane sign its requests to one origin, from its next launch."""
    cfg = _config()
    try:
        ident, _ = _loaded(cfg, _lane(lane_id))
        if not ident.signature_agent:
            raise IdentityError(f"set the signature agent first: openajar identity init {lane_id} --agent <https URL>")
        entry = settings.add_host(ident, origin, reason=reason, by=BY)
        settings.save(cfg.secrets_dir, ident)
    except OpenajarError as e:
        typer.secho(catalog.cli_error(e.code, e.message), err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    typer.echo(f"{lane_id} signs its requests to {entry.origin} from its next launch. {STATEMENT}")


@hosts_app.command("remove")
def hosts_remove(lane_id: LaneArg, origin: Annotated[str, typer.Argument()]) -> None:
    """Stop signing for one origin, at once (an open lane re-reads its settings)."""
    cfg = _config()
    try:
        ident, _ = _loaded(cfg, _lane(lane_id))
        removed = settings.remove_host(ident, origin, by=BY)
        settings.save(cfg.secrets_dir, ident)
    except OpenajarError as e:
        typer.secho(catalog.cli_error(e.code, e.message), err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    if not removed:
        raise typer.Exit(_fail(f"{lane_id} does not sign for {origin}"))
    left = len(ident.hosts)
    typer.echo(f"{lane_id} no longer signs for {settings.normalize_origin(origin)}, from its next request; "
               + (f"{left} host(s) left." if left else "no hosts left, so its next launch installs no route."))


@app.command("test-sign")
def test_sign(
    lane_id: LaneArg,
    url: Annotated[str, typer.Argument(help="A URL on one of the lane's hosts.")],
    method: Annotated[str, typer.Option(help="The request method to sign.")] = "GET",
    send: Annotated[bool, typer.Option("--send", help="Send the signed request (through the lane's proxy) "
                                                      "and print the status and the start of the body.")] = False,
    reveal: Annotated[bool, typer.Option("--reveal", help="Print the Signature header too. It is a bearer "
                                                          "token for 120 s.")] = False,
) -> None:
    """Sign one request as the lane would, check it with the strict verifier, and optionally send it."""
    cfg = _config()
    try:
        ident, key = _loaded(cfg, _lane(lane_id))
        origin = base.origin_of(url)
        if origin not in ident.origins:
            raise IdentityError(f"{origin} is not one of {lane_id}'s hosts; this lane never signs for it")
        if not ident.signature_agent:
            raise IdentityError("no signature agent URL; run init --agent")
        signed = key.sign_request(method=method, url=url, signature_agent=ident.signature_agent, form=ident.form)
    except (OpenajarError, ValueError) as e:
        raise typer.Exit(_fail(getattr(e, "message", str(e)))) from None
    check = verify_request(method, url, signed.headers, {key.keyid: key.public_key()}, nonce_bytes=64)
    for name, value in signed.headers.items():
        shown = value if reveal or name != "Signature" else f"{signed.label}=:<64-byte signature, hidden>:"
        typer.echo(f"{name}: {shown}")
    typer.echo(f"local strict check: {'valid' if check.valid else 'INVALID: ' + str(check.reason)}")
    if not send:
        return
    request = urllib.request.Request(url, method=method.upper(), headers=signed.headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(_proxies(cfg, lane_id)), _UnsignedRedirects())
    try:
        with opener.open(request, timeout=20) as resp:
            status, body = resp.status, resp.read(400)
            if resp.geturl() != url:
                typer.echo(f"redirected to {resp.geturl()} (followed without the signature, as the lane does)")
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read(400)
    except OSError as e:
        raise typer.Exit(_fail(f"sending failed: {e}")) from None
    typer.echo(f"sent: HTTP {status}: {body.decode(errors='replace').strip()[:300]}")


class _UnsignedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect the way the lane does: a new request that carries no signature, so a
    signature only reaches the URL it was made for (QA-identity-2)."""

    SIGNATURE_HEADERS = frozenset({"signature", "signature-input", "signature-agent"})

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            for store in (new.headers, new.unredirected_hdrs):
                for name in [n for n in store if n.lower() in self.SIGNATURE_HEADERS]:
                    del store[name]
        return new


def _proxies(cfg: NodeConfig, lane_id: str) -> dict[str, str]:
    from openajar.core.config import load_proxies

    rec = _lane_record(cfg, lane_id)
    label = rec.spec.proxy_label
    if not label:
        return {}
    proxy = load_proxies(cfg).get(label)
    if proxy is None:
        raise typer.Exit(_fail(f"the lane's proxy {label!r} is not configured"))
    auth = f"{proxy.username}:{proxy.password}@" if proxy.username else ""
    server = f"http://{auth}{proxy.host}:{proxy.port}"
    return {"http": server, "https": server}
