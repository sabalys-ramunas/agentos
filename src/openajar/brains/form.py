"""The page a person pastes an AI service's key into, for one trip (docs/WEB-NODE.md).

The node serves it, on its own address, so the key goes from the person to this computer and
nowhere else. The web app only ever hears how far it got, and has no field a key could enter. It is
plain and says what it is, like the mailbox form (openajar.runner.mail.form): no script, nothing
loaded from anywhere, and a key is never written back into a page.
"""

from __future__ import annotations

import html
import secrets
import time
from dataclasses import dataclass, field

from starlette.responses import HTMLResponse

from openajar.runner.mail.form import page

TTL_S = 15 * 60


@dataclass(frozen=True)
class Service:
    """What the app calls `service`, and how it becomes a brain."""
    id: str
    name: str
    brain: str
    kind: str
    provider: str
    model: str | None = None
    effort: str | None = None
    label: str = "Key"
    hint: str = ""
    keys_at: str | None = None
    takes_address: bool = False  # a local model server: an address, and no secret at all


SERVICES: dict[str, Service] = {
    "typesafe": Service("typesafe", "TypeSafe", "typesafe", "metered", "typesafe",
                        keys_at="https://console.typesafe.ai/"),
    "openrouter": Service("openrouter", "OpenRouter", "openrouter", "metered", "openrouter",
                          keys_at="https://openrouter.ai/keys"),
    "claude-max": Service("claude-max", "Your Claude plan", "claude-haiku", "subscription", "anthropic",
                          model="claude-haiku-5-5", effort="xhigh", label="Sign-in token",
                          hint="Run claude setup-token on any computer signed in to your plan, and paste what it prints."),
    "ollama-local": Service("ollama-local", "Local AI", "local-ai", "local", "ollama", label="Model server address",
                            hint="Where the model server listens, for example http://localhost:11434/v1.",
                            takes_address=True),
}


@dataclass
class Handover:
    """One trip of a key. Holds no key: only where it got to."""
    id: str
    service: str
    computer: str
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)
    stage: str = "waiting"  # waiting, checking, done, failed
    expires_at: float = field(default_factory=lambda: time.time() + TTL_S)

    def public(self) -> dict[str, str | None]:
        return {"id": self.id, "service": self.service, "computer": self.computer, "stage": self.stage,
                "form_url": f"/api/brains/form/{self.token}" if self.stage == "waiting" else None}


class Handovers:
    """Kept in memory: a trip lasts minutes, and a restart only means starting again."""

    def __init__(self) -> None:
        self.rows: dict[str, Handover] = {}

    def start(self, service: str, computer: str) -> Handover:
        self._forget_old()
        row = Handover(id=f"ho_{secrets.token_urlsafe(8)}", service=service, computer=computer)
        self.rows[row.id] = row
        return row

    def get(self, handover_id: str) -> Handover | None:
        self._forget_old()
        return self.rows.get(handover_id)

    def by_token(self, token: str) -> Handover | None:
        """A trip waiting for its key. A token works for one key: after that the link has ended."""
        self._forget_old()
        return next((row for row in self.rows.values() if secrets.compare_digest(row.token, token)
                     and row.stage == "waiting"), None)

    def _forget_old(self) -> None:
        now = time.time()
        self.rows = {key: row for key, row in self.rows.items() if row.expires_at > now}


def render(service: Service, computer: str, *, error: str | None = None, status: int = 200) -> HTMLResponse:
    name, where = html.escape(service.name), html.escape(computer)
    problem = f'<p class="note bad">{html.escape(error)}</p>' if error else ""
    get_one = (f'<p class=soft>Get one at <a href="{html.escape(service.keys_at)}" rel="noreferrer">'
               f"{html.escape(service.keys_at)}</a>.</p>" if service.keys_at else "")
    hint = f"<p class=soft>{html.escape(service.hint)}</p>" if service.hint else ""
    kind = "text" if service.takes_address else "password"
    body = (f"<h1>Add {name}</h1><p class=soft>This page comes from {where}. What you enter goes to this computer "
            f"and stays in its vault. Nothing passes through openajar Cloud.</p>{problem}{hint}{get_one}"
            f'<form method="post" autocomplete="off"><label for="value">{html.escape(service.label)}</label>'
            f'<input id="value" name="value" type="{kind}" required autocomplete="off" spellcheck="false">'
            f'<button type="submit">Keep it on {where}</button></form>')
    return page(f"Add {service.name}", body, status=status)


def ended() -> HTMLResponse:
    return page("This link has ended", "<h1>This link has ended</h1><p class=soft>It was used, or ran out of time. "
                                       "Start again from Settings, AI models.</p>", status=410)


def done(service: Service, computer: str) -> HTMLResponse:
    return page("All done", f"<h1 class=good>All done</h1><p>{html.escape(service.name)} is on "
                            f"{html.escape(computer)}. You can close this tab.</p>")
