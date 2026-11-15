"""The page a person pastes a repository token into, served by the node (agentos.node.forms).

The web app never holds the token (docs/WEB-NODE.md, "Rules that do not bend"). The owner, in person and
with the panel's step-up, asks for a link to this page for one repository card
(`POST /api/repo-connect/{id}/form`, agentos.environments.api). The page is the node's own: no script, no
external asset, a strict CSP and no-store, as every page in agentos.node.forms. Its POST checks the token
with the host and with git and keeps it (agentos.environments.connect.connect_with_token), which approves
the card, so the agent's approval_wait returns. A token that does not work leaves the link open to try
again until it runs out; one that works ends it.

The token is read from the form by hand into a `Secret` and goes only where connect_with_token sends it.
Nothing here logs, traces or returns it, and no error it shows can echo it.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from starlette.responses import HTMLResponse

from agentos.core.errors import AgentosError, NotFound
from agentos.environments import connect
from agentos.node.forms import Kind, Trip, Trips, field_input, note, page
from agentos.vault.secret import Secret

KIND = "repo_token"
MAX_TOKEN = 1024
EMPTY = "Paste the token: one line, from GitHub's token page."


def _host_name(slug: str) -> str:
    host = slug.split("/", 1)[0]
    return "GitHub" if host == "github.com" else host


def _link(href: str, words: str) -> str:
    return f'<a href="{html.escape(href)}" target="_blank" rel="noreferrer noopener">{html.escape(words)}</a>'


def _expiry_words(when: str | None) -> str:
    if not when:
        return ""
    try:
        moment = datetime.fromisoformat(when)
        day = f"{moment.day} {moment:%B %Y}"
    except ValueError:
        return ""
    return f"<p>GitHub says this token expires on {html.escape(day)}. Make a new one before then.</p>"


class RepoToken(Kind):
    """One repository card's token. The node sets its fields when it opens the trip: the card's approval id,
    its repository and why it asks. The app sends none of them, so none is in `takes`."""

    name = KIND

    def title(self, trip: Trip, api: Any) -> str:
        slug = trip.fields.get("repo", "")
        return f"Give a new token for {slug}" if trip.fields.get("reason") == "refused" else f"Connect {slug}"

    def body(self, trip: Trip, api: Any, error: str | None = None) -> str:
        slug = trip.fields.get("repo", "")
        host = _host_name(slug)
        links = connect.links(slug)
        stopped = "The token this computer had for it stopped working. " if trip.fields.get("reason") == "refused" else ""
        make = ""
        if links.get("token_url"):
            make = (f'<p class=soft>{_link(links["token_url"], "Make a fine-grained token")}'
                    + (f' or {_link(links["classic_token_url"], "a classic one")}' if links.get("classic_token_url") else "")
                    + ".</p>")
        return (f"<h1>{html.escape(self.title(trip, api))}</h1>"
                f"<p class=soft>{stopped}This page is agentos on this computer, not {html.escape(host)}. Paste a "
                f"token from your own {html.escape(host)} account: you do not need to be an admin of the "
                f"repository. agentos checks it with {html.escape(host)}, then keeps it on this computer only. "
                "The agent never sees it.</p>"
                + (note(error) if error else "")
                + '<form method="post" autocomplete="off">'
                + field_input("Token", "token", kind="password", autocomplete="off")
                + "<p class=soft>A classic token needs the repo scope. A fine-grained token needs this repository, "
                  "with Contents: Read (Read and write to push).</p>"
                + make
                + '<button type="submit">Connect</button></form>')

    async def submit(self, trip: Trip, api: Any, form: Any, trips: Trips) -> HTMLResponse:
        raw = form.pop("token", "") if isinstance(form, dict) else ""
        if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_TOKEN:
            return self.again(trip, api, EMPTY)
        token = Secret(raw.strip())
        del raw
        try:
            out = await connect.connect_with_token(api.svc("environments"), trip.fields["card"], token)
        except NotFound:
            # The card closed meanwhile (declined, ran out, or connected some other way): so does its link.
            trips.close(trip, "ended")
            return page("This card is closed", "<h1>This card is closed</h1><p class=soft>It was answered, ran out "
                                               "of time, or its repository is connected already. Nothing was kept. "
                                               "You can close this tab.</p>", status=410)
        except AgentosError as exc:
            # Said here, on the page the person is looking at. The link stays open to try again.
            return self.again(trip, api, exc.message or "That did not work. Nothing was kept.")
        trip.outcome = {"repo": out["repo"], "token_expires_at": out.get("expires_at")}
        trips.close(trip, "done")
        return page("Connected", f"<h1 class=good>Connected</h1><p>The agent can open {html.escape(out['repo'])} "
                                 "now. You can close this tab.</p>" + _expiry_words(out.get("expires_at")))


def open_trip(trips: Trips, card: dict[str, str]) -> Trip:
    """A link to the page for one open card. Fields are the node's own, from the card's row."""
    return trips.start(KIND, "owner", {"card": card["id"], "repo": card["repo"], "reason": card["reason"]},
                       subject=card["repo"])
