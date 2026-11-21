"""`openajar proxy`: the proxies lanes exit through."""

from __future__ import annotations

from typing import Annotated

import typer

from openajar.commands.base import PROXY_LABEL_RE, fail, initialized_config, parse_proxy
from openajar.core.config import (
    load_proxies,
    save_proxies,
)

proxy_app = typer.Typer(no_args_is_help=True, help="Proxies that lanes exit through.")


@proxy_app.command("add")
def proxy_add(
    label: Annotated[str, typer.Argument(help="Short name lanes refer to, for example lt1.")],
    spec: Annotated[str, typer.Argument(help="host:port:user:pass (leave out :pass to be prompted).")],
    yes: Annotated[bool, typer.Option("--yes", help="Point an existing label at another proxy, although the lanes "
                                                    "and devices using it then reach their sites from a new IP.")]
    = False,
) -> None:
    """Store a proxy in secrets/proxies.json (chmod 600). The password is never printed. Pointing
    an existing label at another host, port or user needs --yes: every lane and device using it
    gets a new IP."""
    if not PROXY_LABEL_RE.fullmatch(label):
        raise typer.BadParameter("lowercase letters, digits, dot, dash or underscore", param_hint="LABEL")
    cfg = initialized_config()
    proxy = parse_proxy(label, spec)
    proxies = load_proxies(cfg)
    existed = label in proxies
    old = proxies.get(label)
    if old is not None and (old.host, old.port, old.username) != (proxy.host, proxy.port, proxy.username) and not yes:
        from openajar.core.layout import proxy_users

        users = proxy_users(cfg.home, label)
        who = f" ({'; '.join(users)})" if users else ""
        raise fail(f"proxy {label} already points at {old.host}:{old.port}. Pointing it elsewhere is a new IP for "
                   f"everything that uses it{who}: logged-in accounts may be challenged. Nothing changed. If you "
                   f"still want it: openajar proxy add {label} ... --yes")
    proxies[label] = proxy
    save_proxies(cfg, proxies)
    auth = "with credentials" if proxy.username else "without credentials"
    typer.echo(f"{'Updated' if existed else 'Added'} proxy {label}: {proxy.host}:{proxy.port} {auth}")


@proxy_app.command("list")
def proxy_list() -> None:
    """Proxies by label (credentials are never shown)."""
    proxies = load_proxies(initialized_config())
    if not proxies:
        typer.echo("No proxies. Add one with `openajar proxy add <label> host:port:user:pass`.")
        return
    for p in sorted(proxies.values(), key=lambda p: p.label):
        auth = "with credentials" if p.username else "without credentials"
        typer.echo(f"{p.label}  {p.host}:{p.port}  {auth}")
