"""The page a person types a mailbox password into, for one job (docs/EMAIL.md).

The node serves it, on its own address, so what is typed goes to the node and nowhere else. It is
plain and says what it is: this is the owner's own node asking about the owner's own mailbox, and it
never dresses up as a provider's sign-in (docs/PRINCIPLES.md, 9). No script, nothing loaded from
anywhere, and a password is never written back into a page.
"""

from __future__ import annotations

import html
import ipaddress
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse

from openajar.runner.mail.connect import Job
from openajar.runner.mail.discovery import Found, Server

MAX_FORM = 16 * 1024
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; "
                               "frame-ancestors 'none'",
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
SOURCES = {"table": "openajar knows this provider", "table_mx": "openajar knows this provider, from the domain's mail records",
           "ispdb": "Thunderbird's public list", "ispdb_mx": "Thunderbird's public list, for the domain's mail host",
           "autoconfig": "the domain's own https site", "srv": "DNS records only", "guess": "a guess, from what answered",
           "typed": "what you typed"}

STYLE = """
:root{color-scheme:light dark;--ink:#16181d;--soft:#5b6270;--line:#e4e6eb;--page:#f6f7f9;--card:#fff;--act:#16181d;--on:#fff;
--warn:#8a4b00;--warnbg:#fff5e6;--bad:#a4161a;--badbg:#fdecec;--good:#14663a}
@media(prefers-color-scheme:dark){:root{--ink:#eceef2;--soft:#9aa2b1;--line:#2b2f38;--page:#0f1115;--card:#171a20;--act:#eceef2;
--on:#0f1115;--warn:#ffc46b;--warnbg:#2c2212;--bad:#ff9a9e;--badbg:#2e1718;--good:#6fdca1}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:var(--page);
color:var(--ink);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
main{width:100%;max-width:440px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px}
h1{margin:0 0 4px;font-size:20px;letter-spacing:-.01em}p{margin:0 0 14px}.soft{color:var(--soft);font-size:13px}
.note{padding:10px 12px;border-radius:9px;margin:0 0 16px;font-size:14px}.warn{background:var(--warnbg);color:var(--warn)}
.bad{background:var(--badbg);color:var(--bad)}.good{color:var(--good)}
dl{margin:0 0 16px;display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:14px}dt{color:var(--soft)}
dd{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;overflow-wrap:anywhere}
label{display:block;font-weight:600;font-size:13px;margin:0 0 6px}
input,select{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:transparent;color:inherit;font:inherit}
input:focus,select:focus{outline:2px solid var(--act);outline-offset:1px}
.row{display:grid;grid-template-columns:1fr 84px 112px;gap:8px;margin:0 0 12px}.check{display:flex;gap:9px;font-weight:500;font-size:14px;margin:0 0 16px}
.check input{width:auto;margin-top:4px}details{margin:0 0 16px;font-size:14px}summary{cursor:pointer;color:var(--soft);margin-bottom:10px}
button{width:100%;padding:11px;border:0;border-radius:9px;background:var(--act);color:var(--on);font:inherit;font-weight:600;cursor:pointer;margin:4px 0 14px}
a{color:inherit}
"""


def page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    text = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>{html.escape(title)}</title>'
            f"<style>{STYLE}</style></head><body><main>{body}</main></body></html>")
    return HTMLResponse(text, status_code=status, headers=HEADERS)


def ended() -> HTMLResponse:
    return page("This link has ended", "<h1>This link has ended</h1><p class=soft>It was used, ran out of time, or was "
                                       "cancelled. Start again from where you opened it.</p>", status=404)


def not_direct() -> HTMLResponse:
    return page("Open this through the tunnel", "<h1>Open this through the tunnel</h1><p>A password is typed here only over "
                                                "https, or on this computer through the SSH tunnel you were given.</p>", status=403)


def done(address: str, mailbox_id: str) -> HTMLResponse:
    return page("Connected", f'<h1 class=good>Connected</h1><p>{html.escape(address)} is connected as '
                             f'<b>{html.escape(mailbox_id)}</b>. You can close this page.</p>')


def _server_row(kind: str, server: Server | None) -> str:
    host = html.escape(server.host) if server else ""
    port = server.port if server else ""
    security = server.security if server else "tls"
    options = "".join(f'<option value="{v}"{" selected" if v == security else ""}>{w}</option>'
                      for v, w in (("tls", "TLS"), ("starttls", "STARTTLS")))
    need = "" if server else " required"
    return (f'<label>{kind.upper()} server</label><div class=row><input name="{kind}_host" value="{host}" aria-label="{kind} host"'
            f'{need} autocomplete="off" spellcheck="false"><input name="{kind}_port" value="{port}" aria-label="{kind} port"{need} '
            f'inputmode="numeric"><select name="{kind}_security" aria-label="{kind} security">{options}</select></div>')


def _shown(found: Found) -> str:
    def where(s: Server) -> str:
        words = "TLS" if s.security == "tls" else "required STARTTLS"
        return html.escape(f"{s.host}:{s.port} ({words})")

    source = html.escape(SOURCES.get(found.source, found.source))
    return (f"<dl><dt>Reading</dt><dd>{where(found.imap)}</dd><dt>Sending</dt><dd>{where(found.smtp)}</dd>"
            f"<dt>Found by</dt><dd>{source}</dd></dl>")


def _until_words(expires_at: float) -> str:
    """When the link stops working, on this computer's clock: the person reads their own time, not UTC. The
    date is added when it is not today."""
    at = datetime.fromtimestamp(expires_at).astimezone()
    when = at.strftime("%H:%M") if at.date() == datetime.now().astimezone().date() else at.strftime("%H:%M on %d %b")
    return f"{when} your time"


def render(job: Job, *, error: str | None = None, status: int = 200) -> HTMLResponse:
    found = job.found
    parts = [f"<h1>Connect {html.escape(job.address)}</h1>",
             f'<p class=soft>As <b>{html.escape(job.mailbox_id)}</b> on your own openajar node.</p>']
    if error:
        parts.append(f'<p class="note bad" role="alert">{html.escape(error)} ({job.tries_left} tries left.)</p>')
    if found is not None and found.words:
        link = f' <a href="{html.escape(found.help_url)}" target="_blank" rel="noreferrer">Open that page</a>.' if found.help_url else ""
        parts.append(f'<p class="note warn">{html.escape(found.words)}{link}</p>')
    parts.append('<form method="post" autocomplete="off">')
    if found is not None:
        parts.append(f'<input type=hidden name=shown value="{found.digest()}">')
    if found is None:
        parts.append('<p class="note warn">No mail servers were found for this address. Type them in. Only TLS or '
                     "required STARTTLS is offered.</p>")
        parts += [_server_row("imap", None), _server_row("smtp", None)]
    else:
        parts.append(_shown(found))
        parts.append(f"<details><summary>Change the servers</summary>{_server_row('imap', found.imap)}"
                     f"{_server_row('smtp', found.smtp)}</details>")
        if found.needs_yes:
            parts.append('<p class="note warn">Only DNS or a guess named these servers, and a false record could '
                         "point anywhere. Check them before you go on.</p>")
            parts.append('<label class=check><input type=checkbox name=confirm value=yes required> '
                         "These are my mail servers.</label>")
    word = "App password" if found is not None and found.needs_app_password else "Password"
    parts.append(f'<label for=p>{word}</label><input id=p type=password name=password required autofocus '
                 'autocomplete="new-password" style="margin-bottom:16px"><button type=submit>Connect</button></form>')
    until = _until_words(job.expires_at)
    who = ("An agent asked to connect this mailbox. What you type goes to your node and to the two servers above. "
           "The agent never sees it, and it does not reach the cloud.") if "agent" in (job.asked_by, job.by) else (
           "You opened this from your panel. What you type goes to this computer and to the two servers above. "
           "The panel never sees it, and it does not reach the cloud.")
    parts.append(f"<p class=soft>{who} This link works until {until}.</p>")
    return page(f"Connect {job.address}", "".join(parts), status=status)


def direct(request: Request) -> bool:
    """https, or this very computer (the SSH tunnel). Anything else would carry a password in the clear."""
    if request.url.scheme == "https":
        return True
    try:
        return ipaddress.ip_address((request.client.host if request.client else "") or "").is_loopback
    except ValueError:
        return False


def same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return not origin or origin == "null" or urlsplit(origin).netloc == request.headers.get("host")


def read_form(raw: bytes) -> dict[str, Any]:
    """The fields, by hand: no validation error can echo a value."""
    if len(raw) > MAX_FORM:
        return {}
    try:
        fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
    except (UnicodeDecodeError, ValueError):
        return {}
    return {k: v[0] for k, v in fields.items()}


def typed_fields(form: dict[str, Any], kind: str, known: Server | None) -> dict[str, Any] | None:
    """What a person changed for one server. A field left as shown is not a change."""
    host, port, security = (str(form.get(f"{kind}_{k}") or "").strip() for k in ("host", "port", "security"))
    if not host or known is not None and (host, port, security) == (known.host, str(known.port), known.security):
        return None
    return {"host": host, "port": port, "security": security}
