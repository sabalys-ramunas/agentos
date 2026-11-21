"""The pages the node serves for a secret: one trip, one token, no script (docs/WEB-NODE.md).

The web app starts a trip with `POST /api/forms` (the owner, in person) and opens the `form_url` it gets
back on this computer. The page takes what only the owner may type, does the one thing it is for, and
tells the app only how far it got (`GET /api/forms/trips/{id}`). The token is the credential: it needs
no sign-in, works for 15 minutes, and a finished trip ends its link. A password is typed only over https
or from this computer (the SSH tunnel), the same rule as the mailbox and AI key pages
(openajar.runner.mail.form). Nothing here logs, traces or stores a value.

Kinds: `vault_unlock`, `vault_entry`, `proxy`, `workspace_export`, `workspace_import` (here and in
openajar.workspaces.form), `login` (openajar.node.login_form, which keeps its trip open for the whole
sign-in) and `repo_token` (openajar.environments.form, opened by its own route with the panel's step-up). Each kind takes only the plain fields the app may send; the rest is typed on the page. The
API's form route lists every kind (openajar.api.routes_forms.kinds), so this module imports none of them.
"""

from __future__ import annotations

import html
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from starlette.responses import HTMLResponse

from openajar.core.errors import OpenajarError
from openajar.runner.mail import form as mail_form
from openajar.vault.secret import Secret

TTL_S = 15 * 60
KEEP_S = 10 * 60  # a finished trip stays readable for a while, so the app can see how it ended
ENTRY_KINDS = {"password": "Password", "totp": "Secret key", "cookie": "Cookie value", "api_key": "API key"}


@dataclass
class Trip:
    """One visit to one form. Holds no secret: only what the owner asked for, and where it got to."""
    id: str
    kind: str
    owner: str
    fields: dict[str, str]
    subject: str | None = None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)
    stage: str = "waiting"  # waiting: open, done: finished, ended: ran out or was closed
    expires_at: float = field(default_factory=lambda: time.time() + TTL_S)
    outcome: dict[str, Any] | None = None  # what a finished trip may tell the app: never a value typed on it

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "stage": self.stage,
            "subject": self.subject,
            "form_url": f"/api/forms/{self.token}" if self.stage == "waiting" else None,
            "expires_at": datetime.fromtimestamp(self.expires_at, UTC).isoformat(),
            **({"outcome": dict(self.outcome)} if self.outcome is not None else {}),
        }


class Trips:
    """Kept in memory: a trip lasts minutes, and a restart only means starting again."""

    def __init__(self) -> None:
        self.rows: dict[str, Trip] = {}

    def start(self, kind: str, owner: str, fields: dict[str, str], subject: str | None = None) -> Trip:
        self._forget_old()
        trip = Trip(id=f"ft_{secrets.token_urlsafe(8)}", kind=kind, owner=owner, fields=dict(fields), subject=subject)
        self.rows[trip.id] = trip
        return trip

    def by_id(self, trip_id: str) -> Trip | None:
        self._forget_old()
        return self.rows.get(trip_id)

    def by_token(self, token: str) -> Trip | None:
        """A trip whose form is open. Once it is finished or ended, its link means nothing."""
        self._forget_old()
        return next((row for row in self.rows.values() if row.stage == "waiting"
                     and secrets.compare_digest(row.token, token)), None)

    def close(self, trip: Trip, stage: str) -> None:
        trip.stage = stage
        trip.expires_at = time.time() + KEEP_S

    def touch(self, trip: Trip) -> None:
        """A sign-in runs for minutes: the form stays open while it does."""
        trip.expires_at = time.time() + TTL_S

    def _forget_old(self) -> None:
        now = time.time()
        for row in self.rows.values():
            if row.stage == "waiting" and row.expires_at <= now:
                self.close(row, "ended")
        self.rows = {key: row for key, row in self.rows.items() if row.expires_at > now}


def trips_of(runtime: Any) -> Trips:
    """The one store of a node's trips, kept on its runtime so every route sees the same rows. Read from
    the object's own dict: the runtime answers an unknown name with its own error, not AttributeError."""
    store = vars(runtime).get("form_trips")
    if store is None:
        store = Trips()
        vars(runtime)["form_trips"] = store
    return store


def page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    return mail_form.page(title, body, status=status)


def ended() -> HTMLResponse:
    return page("This form has ended", "<h1>This form has ended</h1><p class=soft>It was used, or ran out of time. "
                                       "Start it again from openajar.</p>", status=410)


def field_input(label: str, name: str, *, kind: str = "text", value: str = "", required: bool = True,
                autocomplete: str = "off") -> str:
    need = " required" if required else ""
    return (f'<label for="{name}">{html.escape(label)}</label>'
            f'<input id="{name}" name="{name}" type="{kind}" value="{html.escape(value)}"{need} '
            f'autocomplete="{autocomplete}" spellcheck="false" style="margin-bottom:16px">')


def note(message: str) -> str:
    return f'<p class="note bad" role="alert">{html.escape(message)}</p>'


class Kind:
    """What one kind of form says and does. A subclass sets its plain fields and the methods below.

    `form` is what the page sent: a dict of text, or a multipart form (`multipart = True`) whose file
    fields are uploads. Nothing a kind does may put a value into an error, a trace or a log line."""

    name = ""
    takes: tuple[str, ...] = ()     # plain fields the app may send when it starts the trip
    needs: tuple[str, ...] = ()     # which of those are required
    multipart = False               # the page posts a file as well (multipart/form-data)

    def title(self, trip: Trip, api: Any) -> str:
        raise NotImplementedError

    def body(self, trip: Trip, api: Any, error: str | None = None) -> str:
        raise NotImplementedError

    async def submit(self, trip: Trip, api: Any, form: Any, trips: Trips) -> HTMLResponse:
        raise NotImplementedError

    async def page(self, trip: Trip, api: Any, trips: Trips) -> HTMLResponse:
        """The page for the trip's link, as it is now. A kind that waits on something else overrides it."""
        return page(self.title(trip, api), self.body(trip, api))

    def again(self, trip: Trip, api: Any, error: str) -> HTMLResponse:
        return page(self.title(trip, api), self.body(trip, api, error), status=400)


class VaultUnlock(Kind):
    """A key-file vault opens with the key file kept on this computer, so the page asks for nothing. Only a
    passphrase vault needs one typed here, and that is the only vault that gets a field."""

    name = "vault_unlock"

    def title(self, trip: Trip, api: Any) -> str:
        return "Unlock the vault"

    def _keyfile(self, api: Any) -> bool:
        try:
            return api.runtime.require("vault").status().get("mode") == "keyfile"
        except OpenajarError:
            return False

    def body(self, trip: Trip, api: Any, error: str | None = None) -> str:
        if self._keyfile(api):
            return ("<h1>Unlock the vault</h1><p class=soft>This vault opens with the key file kept on this computer, "
                    "so nothing needs typing. Nothing is saved on this page.</p>" + (note(error) if error else "")
                    + '<form method="post" autocomplete="off">'
                    + '<button type="submit">Unlock on this computer</button></form>')
        return ("<h1>Unlock the vault</h1><p class=soft>Type the vault's passphrase. It goes to this computer and "
                "nowhere else. Nothing is saved on this page.</p>" + (note(error) if error else "")
                + '<form method="post" autocomplete="off">'
                + field_input("Passphrase", "passphrase", kind="password", autocomplete="off")
                + '<button type="submit">Unlock on this computer</button></form>')

    async def submit(self, trip: Trip, api: Any, form: Any, trips: Trips) -> HTMLResponse:
        keyfile = self._keyfile(api)
        passphrase = str(form.get("passphrase") or "")
        if not keyfile and not passphrase:
            return self.again(trip, api, "The passphrase was empty.")
        vault = api.runtime.require("vault")
        try:
            await vault.unseal(None if keyfile else Secret(passphrase))
        except OpenajarError as exc:
            return self.again(trip, api, exc.message or "The vault did not open. Nothing changed.")
        trips.close(trip, "done")
        return page("Unlocked", "<h1 class=good>Unlocked</h1><p>The vault is open on this computer. "
                                "You can close this tab.</p>")


class VaultEntry(Kind):
    name = "vault_entry"
    takes = ("name", "kind", "domain", "lanes")
    needs = ("name", "kind", "domain", "lanes")

    def title(self, trip: Trip, api: Any) -> str:
        return f"Add {trip.fields.get('name', 'a vault entry')}"

    def body(self, trip: Trip, api: Any, error: str | None = None) -> str:
        kind = trip.fields.get("kind", "password")
        secret_word = ENTRY_KINDS.get(kind, "Secret")
        lanes = ", ".join(x for x in trip.fields.get("lanes", "").split(",") if x)
        head = (f"<h1>Add {html.escape(trip.fields.get('name', ''))}</h1>"
                f"<p class=soft>This page is openajar on this computer, not the site. What you type goes to this "
                f"computer's vault and nowhere else.</p>")
        facts = (f"<dl><dt>Name</dt><dd>{html.escape(trip.fields.get('name', ''))}</dd>"
                 f"<dt>Site</dt><dd>{html.escape(trip.fields.get('domain', ''))}</dd>"
                 f"<dt>Accounts</dt><dd>{html.escape(lanes)}</dd></dl>")
        user = field_input("Username or email", "username", required=False, autocomplete="off") if kind == "password" else ""
        return (head + (note(error) if error else "") + facts + '<form method="post" autocomplete="off">' + user
                + field_input(secret_word, "secret", kind="password") + '<button type="submit">Keep it in the vault</button></form>')

    async def submit(self, trip: Trip, api: Any, form: Any, trips: Trips) -> HTMLResponse:
        secret = str(form.get("secret") or "")
        if not secret:
            return self.again(trip, api, "That was empty. Nothing was saved.")
        username = str(form.get("username") or "").strip() or None
        vault = api.runtime.require("vault")
        lanes = [x for x in trip.fields.get("lanes", "").split(",") if x]
        try:
            meta = await vault.add_entry(name=trip.fields["name"], kind=trip.fields["kind"],
                                         domain=trip.fields.get("domain") or None, lanes=lanes,
                                         username=Secret(username) if username else None, secret=Secret(secret))
        except OpenajarError as exc:
            return self.again(trip, api, exc.message or "That did not work. Nothing was saved.")
        trips.close(trip, "done")
        return page("Saved", f'<h1 class=good>Saved</h1><p>{html.escape(meta.name)} is in the vault on this computer. '
                             "You can close this tab.</p>")


class ProxyForm(Kind):
    name = "proxy"
    takes = ("label", "host", "port", "username")
    needs = ("label", "host", "port")

    def title(self, trip: Trip, api: Any) -> str:
        return f"Proxy {trip.fields.get('label', '')}"

    def body(self, trip: Trip, api: Any, error: str | None = None) -> str:
        label, host, port = (html.escape(trip.fields.get(k, "")) for k in ("label", "host", "port"))
        return (f"<h1>Proxy {label}</h1><p class=soft>Where it connects: {host}:{port}. The password, if it has one, "
                "goes to this computer's proxy list and nowhere else.</p>" + (note(error) if error else "")
                + '<form method="post" autocomplete="off">'
                + field_input("Password (leave empty if it has none)", "password", kind="password", required=False)
                + '<label class=check style="display:flex;gap:9px;font-weight:500"><input type="checkbox" '
                  'name="confirm" value="yes" style="width:auto;margin-top:4px"> Point an existing proxy with this '
                  "name here too. Accounts that use it may be challenged.</label>"
                + '<button type="submit">Keep it on this computer</button></form>')

    async def submit(self, trip: Trip, api: Any, form: Any, trips: Trips) -> HTMLResponse:
        from openajar.core.aio import maybe_await
        from openajar.core.config import load_proxies, proxies_by_label, save_proxies
        from openajar.core.models import ProxyConfig

        password = str(form.get("password") or "") or None
        try:
            proxy = ProxyConfig(label=trip.fields["label"], host=trip.fields["host"], port=int(trip.fields["port"]),
                                username=trip.fields.get("username") or None, password=password)
        except (ValueError, TypeError):
            # Never the exception's text: a validation error can echo the password typed on this page.
            return self.again(trip, api, "That address is not valid. Nothing was saved.")
        old = (await proxies_by_label(api.optional("proxies"), api.config)).get(proxy.label)
        if old is not None and not form.get("confirm"):
            was = tuple((old.get(k) if isinstance(old, dict) else getattr(old, k, None)) for k in ("host", "port", "username"))
            if was != (proxy.host, proxy.port, proxy.username):
                return self.again(trip, api, f"Proxy {proxy.label} already points at {was[0]}:{was[1]}. Tick the box to point "
                                        "it at the new address. Nothing was saved.")
        svc = api.optional("proxies")
        if svc is not None:
            await maybe_await(svc.add(proxy))
        elif api.config is not None:
            current = load_proxies(api.config)
            current[proxy.label] = proxy
            save_proxies(api.config, current)
        else:
            return self.again(trip, api, "Proxies are not set up on this computer.")
        trips.close(trip, "done")
        return page("Saved", f'<h1 class=good>Saved</h1><p>Proxy {html.escape(proxy.label)} is on this computer. '
                             "You can close this tab.</p>")


