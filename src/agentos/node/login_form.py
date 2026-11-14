"""The sign-in form of a lane, served by the node (docs/API.md, "Assisted login").

The owner starts it from the login queue or the account page. The web app gets a trip with a `form_url` and
opens it on this computer. The page shows the fields the node read from the site's own sign-in, in plain
words, and says plainly that it is agentos on this computer, not the site. What the owner types goes to the
login service, which types it into the lane's browser. The trip stays open for the whole sign-in, so the
form can show the next step; it ends when the sign-in ends.

A captcha, a code or a device approval never goes through this page: those are answered by a person in the
live view (take over) or on the code card. The page says so instead of showing a field.
"""

from __future__ import annotations

import html
from typing import Any

from starlette.responses import HTMLResponse

from agentos.core.errors import AgentosError, InvalidRequest
from agentos.node.forms import Kind, Trip, Trips, ended, field_input, note, page

VALUE_NAMES = ("username", "password", "code")
ENDED_STATES = ("cancelled", "done")
WAITING = {
    "working": "agentos is opening the sign-in page.",
    "waiting": "Approve the sign-in on your phone, then press the button below.",
    "stopped": "The page is one agentos cannot read, or it asks a robot check. Take over the lane and do that step yourself.",
    "paused": "You have the lane. Hand it back to go on from this form.",
}


def _service(runtime: Any) -> Any:
    from agentos.node.login_assist import service_for

    return service_for(runtime)


def _read(form: Any, name: str) -> str:
    return str(form.get(name) or "")


class LoginForm(Kind):
    name = "login"

    def _login(self, trip: Trip, api: Any) -> dict[str, Any] | None:
        return _service(api.runtime).get(trip.subject or "")

    def title(self, trip: Trip, api: Any) -> str:
        login = self._login(trip, api) or {}
        return f"Sign in to {login.get('site_name') or login.get('host') or trip.subject}"

    async def page(self, trip: Trip, api: Any, trips: Trips) -> HTMLResponse:
        login = self._login(trip, api)
        if login is None or login.get("state") in ENDED_STATES:
            trips.close(trip, "ended" if login is None or login.get("state") == "cancelled" else "done")
            return ended()
        trips.touch(trip)
        return page(self.title(trip, api), self.body(trip, api))

    def body(self, trip: Trip, api: Any, error: str | None = None) -> str:
        login = self._login(trip, api) or {"state": "none"}
        state = login.get("state")
        site = html.escape(str(login.get("site_name") or login.get("host") or trip.subject or "the site"))
        lane = html.escape(str(trip.subject or ""))
        head = (f"<h1>Sign in to {site}</h1>"
                f"<p class=soft>This page is agentos on this computer, not {site}. It is typed into the lane "
                f"<code>{lane}</code>'s own browser by this computer, and the session stays on it.</p>")
        message = note(error) if error else (note(str(login["error"])) if login.get("error") else "")
        if state == "signed_in":
            return head + self._signed_in(login)
        if state == "ask" and login.get("step"):
            return head + message + self._step(login["step"], trip)
        words = WAITING.get(str(state), "agentos is reading the page.")
        extra = self._actions(state, trip)
        return head + message + f"<p>{html.escape(words)}</p>" + extra + self._stop(trip)

    def _step(self, step: dict[str, Any], trip: Trip) -> str:
        kind = step.get("kind")
        heading = html.escape(str(step.get("heading") or ""))
        says = html.escape(str(step.get("message") or ""))
        top = (f"<h2 style=\"font-size:16px;margin:0 0 4px\">{heading}</h2>" if heading else "") + (
            f"<p class=soft>{says}</p>" if says else "")
        form_open = '<form method="post" autocomplete="off">'
        step_id = f'<input type="hidden" name="step" value="{html.escape(str(step.get("id") or ""))}">'
        if kind == "sign_in":
            inputs = []
            for item in step.get("fields") or []:
                name = str(item.get("name") or "")
                if name not in ("username", "password") or name not in VALUE_NAMES:
                    continue
                inputs.append(field_input(str(item.get("label") or name), name,
                                          kind="password" if item.get("secret") else "text",
                                          required=True))
            if not inputs:
                return top + self._stop(trip)
            return (top + form_open + step_id + "".join(inputs)
                    + '<input type="hidden" name="action" value="step">'
                    + '<button type="submit">Continue</button></form>' + self._stop(trip))
        if kind == "approve":
            return top + "<p>Approve the sign-in on your phone, then press the button.</p>" + self._actions("waiting", trip)
        if kind == "code":
            return top + ("<p>The site asks for a code. A code is not typed on this page: take over the lane to type it, "
                          "or answer the code request in Needs you.</p>" + self._actions("paused", trip))
        if kind in ("choose", "confirm"):
            buttons = "".join(
                f'<button type="submit" name="choice" value="{html.escape(str(choice.get("id") or ""))}">'
                f'{html.escape(str(choice.get("label") or ""))}</button>'
                for choice in step.get("choices") or [] if choice.get("id"))
            return top + form_open + step_id + '<input type="hidden" name="action" value="step">' + buttons + "</form>" + self._stop(trip)
        return top + ("<p>The site asks something agentos cannot answer for you. Take over the lane to do it "
                      "yourself.</p>" + self._actions("paused", trip))

    def _actions(self, state: Any, trip: Trip) -> str:
        if state == "paused":
            return self._button("resume", "Back to this form")
        if state == "stopped":
            return self._button("take", "Take over the lane")
        if state == "waiting":
            return self._button("refresh", "I approved it, read the page again")
        return self._button("refresh", "Read the page again")

    def _signed_in(self, login: dict[str, Any]) -> str:
        head = '<p class=good><b>Signed in.</b> The site accepted the sign-in on this computer.</p>'
        if login.get("can_save"):
            return (head + '<form method="post" autocomplete="off"><input type="hidden" name="action" value="save">'
                    + '<p class=soft>Save this login in the vault, and agentos signs this lane in next time with no form. '
                      "It stays on this computer, bound to this lane and this site.</p>"
                    + field_input("Name for the vault entry (optional)", "name", required=False)
                    + '<button type="submit">Save to the vault</button></form>'
                    + '<form method="post" autocomplete="off"><input type="hidden" name="action" value="done">'
                      '<button type="submit">Done without saving</button></form>')
        if login.get("saved"):
            saved = html.escape(str(login["saved"]))
            return (head + f"<p>Saved as <code>{saved}</code>. agentos signs this lane in from the vault.</p>"
                    + '<form method="post" autocomplete="off"><input type="hidden" name="action" value="done">'
                      '<button type="submit">Done</button></form>')
        return head + ('<form method="post" autocomplete="off"><input type="hidden" name="action" value="done">'
                       '<button type="submit">Done</button></form>')

    def _button(self, action: str, label: str) -> str:
        return (f'<form method="post" autocomplete="off"><input type="hidden" name="action" value="{action}">'
                f'<button type="submit">{html.escape(label)}</button></form>')

    def _stop(self, trip: Trip) -> str:
        return ('<form method="post" autocomplete="off"><input type="hidden" name="action" value="cancel">'
                '<button type="submit" style="background:transparent;color:inherit;border:1px solid var(--line)">'
                "Stop the sign-in</button></form>")

    async def submit(self, trip: Trip, api: Any, form: Any, trips: Trips) -> HTMLResponse:
        from agentos.api.routes_login import read_values

        svc = _service(api.runtime)
        action = _read(form, "action") or "step"
        lane = trip.subject or ""
        owner = trip.owner
        values: dict[str, Any] = {}
        try:
            if action == "step":
                step_id = _read(form, "step")
                choice = _read(form, "choice") or None
                if choice is None:
                    values = read_values({k: _read(form, k) for k in VALUE_NAMES if _read(form, k)} or None)
                await svc.submit(lane, step=step_id, values=values, choice=choice, by=owner)
            elif action == "refresh":
                await svc.refresh(lane, by=owner)
            elif action == "take":
                await svc.take(lane, by=owner)
            elif action == "resume":
                await svc.resume(lane, by=owner)
            elif action == "save":
                name = _read(form, "name").strip() or None
                if name is not None and len(name) > 64:
                    raise InvalidRequest("The name is at most 64 characters.")
                await svc.save(lane, by=owner, name=name)
            elif action == "done":
                await svc.cancel(lane, by=owner, done=True)
                trips.close(trip, "done")
                return page("Done", "<h1 class=good>Done</h1><p>The sign-in is finished. You can close this tab.</p>")
            elif action == "cancel":
                await svc.cancel(lane, by=owner, done=False)
                trips.close(trip, "ended")
                return page("Stopped", "<h1>Stopped</h1><p>The sign-in was stopped. Nothing else was typed. "
                                       "You can close this tab.</p>")
            else:
                return self.again(trip, api, "That is not something this form does.")
        except AgentosError as exc:
            return self.again(trip, api, exc.message or "That did not work. Nothing was typed.")
        finally:
            values.clear()
        trips.touch(trip)
        return page(self.title(trip, api), self.body(trip, api))

