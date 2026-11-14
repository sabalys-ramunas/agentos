"""The login and fill procedures behind the `login` and `vault_fill` tools.

Order of operations, so nothing is typed that the policy did not allow:
read the page (origin, fields, signal) -> pick the entry (bound + origin match) ->
pre-check the policy -> ctx.authorize(SECRET, node-written summary) -> for each field:
focus, re-read the field and the page URL, resolve through the policy, type.

A password in the vault is the owner's permission to use it (docs/PRINCIPLES.md, 3), so the
authorize step never asks a person: it checks the grant holds `secret` for this lane. The guards are
`decide`'s, run before the authorize and again for each field. While a field is typed the lane's
egress guard holds a short login window for the entry's domain (agentos.node.egress), so the submit
is not held as a possible publish.

Results carry only {ok, step, needs, signal}, and `next` when needs is "code"; never a username,
secret or code.

An entry whose code source is the code card ("relay") waits at the code step for the owner
to type the code in the panel. That can take minutes, so before typing it the field and the
page are read and checked again. No code in time: needs "code"; the owner dismissed the
card: needs "human".
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from agentos.core.errors import InvalidRequest
from agentos.core.models import Effect, Signal
from agentos.core.signals import LOGIN_HUMAN
from agentos.vault.domains import display_host, parse_origin
from agentos.vault.errors import CodeDeclined, EntryNotFound, ResolutionDenied, SecretTooLong, TypistAborted
from agentos.vault.models import EntryKind, EntryMeta
from agentos.vault.policy import (
    ROLE_SINK,
    Decision,
    FieldRole,
    Reason,
    Sink,
    check_origin,
    classify_fields,
    decide,
    lane_bound,
    matching_entries,
    otp_group,
    secure_origin,
)
from agentos.vault.secret import Secret
from agentos.vault.typist import FieldInfo, Typist

TYPE_MAX = 1024          # characters; longer secrets are refused rather than typed for minutes
SETTLE_SECONDS = 15.0


CODE_NEXT = ("The site is waiting for a one-time code and the owner gave none in time. Tell the owner a code "
             "is due for this lane: they type it on the code card in Needs you in the panel. Then take a "
             "browser_snapshot: if the page still shows the code field, call {tool} again with the same arguments "
             "once, and it waits for the code there. Do not call it in a loop.")


def _result(ok: bool, step: str, needs: str | None, signal: Signal) -> dict[str, Any]:
    out = {"ok": ok, "step": step, "needs": needs, "signal": Signal(signal).value}
    if needs == "code":  # without it an agent has no way to tell how to pick the login up again
        out["next"] = CODE_NEXT.format(tool="login")
    return out


def _first(fields: Sequence[FieldInfo], roles: dict[str, FieldRole], role: FieldRole) -> FieldInfo | None:
    return next((f for f in fields if roles.get(f.locator) is role), None)


def _username_for(fields: Sequence[FieldInfo], roles: dict[str, FieldRole], pw: FieldInfo | None) -> FieldInfo | None:
    users = [f for f in fields if roles.get(f.locator) is FieldRole.USERNAME]
    if pw is not None:
        same = [f for f in users if f.form == pw.form and f.frame_origins == pw.frame_origins]
        return same[0] if same else None
    return users[0] if users else None


def _relay(entry: EntryMeta) -> bool:
    return (entry.config.get("catcher") or {}).get("kind") == "relay"


def _no_code(entry: EntryMeta) -> str:
    """What a login needs when no code came: the owner's code (code card) or a code source."""
    return "code" if _relay(entry) else "totp"


def _deny(ctx: Any, entry: EntryMeta | None, lane_id: str, d: Decision, sink: Sink = Sink.FORM_PASSWORD) -> None:
    ctx.trace("vault.deny", entry=entry.id if entry else None, lane=lane_id, host=d.host,
              sink=sink.value, reason=d.reason.value)
    d.check()


async def choose_entry(ctx: Any, vault: Any, *, lane_id: str, url: str, entry: str | None) -> EntryMeta:
    """The password entry to log in with: the named one (checked), or the only entry
    bound to this lane whose domain matches the page."""
    if entry is not None:
        try:
            meta = await vault.get_entry(entry)
        except EntryNotFound:
            raise EntryNotFound(f"no vault entry named {entry!r}") from None
        if meta.kind is not EntryKind.PASSWORD:
            _deny(ctx, meta, lane_id, Decision(False, Reason.KIND_SINK, None,
                                               f"a {meta.kind.value} entry cannot be used to log in"))
        if not lane_bound(meta.lanes, lane_id):
            _deny(ctx, meta, lane_id, Decision(False, Reason.LANE_NOT_BOUND, None,
                                               f"entry is not bound to lane {lane_id}"))
        d = check_origin(meta, url)
        if not d.allowed:
            _deny(ctx, meta, lane_id, d)
        return meta
    origin, d = secure_origin(url)
    if origin is None:
        _deny(ctx, None, lane_id, d)
    assert origin is not None
    host = origin.host
    candidates = matching_entries(await vault.list_entries(), lane_id=lane_id, url=url)
    if not candidates:
        _deny(ctx, None, lane_id, Decision(False, Reason.NO_ENTRY, host,
                                           f"no vault entry bound to lane {lane_id} matches {host}"))
    if len(candidates) > 1:
        names = ", ".join(repr(e.name) for e in candidates)
        raise InvalidRequest(f"{len(candidates)} vault entries match {host} on lane {lane_id} ({names}); "
                             "pass entry=<name>")
    return candidates[0]


async def _login_window(typist: Typist, step: str, entry: EntryMeta) -> None:
    """Tell the lane's egress guard (`open_login` or `close_login`, see agentos.vault.typist)."""
    tell = getattr(typist, step, None)
    if tell is not None and entry.domain:
        await tell(entry.domain)


async def _type(typist: Typist, entry: EntryMeta, field: FieldInfo, secret: Secret) -> None:
    """Type a value the vault resolved for this field. The login window opens before the first key,
    because a code field can submit itself on its last digit, and a fill that fails leaves none."""
    if len(secret) > TYPE_MAX:
        raise SecretTooLong(f"the secret is longer than {TYPE_MAX} characters, too long to type")
    await _login_window(typist, "open_login", entry)
    try:
        await typist.type_secret(field, secret.reveal())
    except BaseException:
        await _login_window(typist, "close_login", entry)
        raise


def _pool(fields: Sequence[FieldInfo], fresh: FieldInfo) -> list[FieldInfo]:
    found = False
    out = []
    for f in fields:
        if f.locator == fresh.locator:
            out.append(fresh)
            found = True
        else:
            out.append(f)
    return out if found else [*out, fresh]


async def _fill_secret(ctx: Any, vault: Any, typist: Typist, entry: EntryMeta, lane_id: str,
                       field: FieldInfo, fields: Sequence[FieldInfo], sink: Sink) -> FieldInfo:
    fresh = await typist.focus(field)
    url = await typist.page_url()
    secret = await vault.resolve_field(ctx, ref=entry.id, lane_id=lane_id, url=url, field=fresh,
                                       fields=_pool(fields, fresh), sink=sink)
    await _type(typist, entry, fresh, secret)
    return fresh


async def _fill_code(ctx: Any, vault: Any, typist: Typist, entry: EntryMeta, lane_id: str,
                     field: FieldInfo, fields: Sequence[FieldInfo], since: datetime) -> FieldInfo | None:
    """Type a one-time code (TOTP or caught from email). None if no code arrived."""
    group = otp_group(fields, field)
    fresh = await typist.focus(group[0])
    url = await typist.page_url()
    pool = _pool(fields, fresh)
    code = await vault.one_time_code(ctx, ref=entry.id, lane_id=lane_id, url=url, field=fresh,
                                     fields=pool, since=since)
    if code is None:
        return None
    if _relay(entry):  # the owner took a while: the page must still be the entry's, the field a code field
        fresh = await typist.focus(group[0])
        pool = _pool(await typist.fields(), fresh)
        d = decide(entry, lane_id=lane_id, sink=Sink.FORM_OTP, url=await typist.page_url(), field=fresh,
                   fields=pool)
        if not d.allowed:
            _deny(ctx, entry, lane_id, d, Sink.FORM_OTP)
    if len(group) > 1 and len(group) == len(code):
        digits = code.reveal()
        await _type(typist, entry, fresh, Secret(digits[0]))
        for box, digit in zip(group[1:], digits[1:], strict=True):
            box_fresh = await typist.focus(box)
            d = decide(entry, lane_id=lane_id, sink=Sink.FORM_OTP, url=await typist.page_url(),
                       field=box_fresh, fields=_pool(pool, box_fresh))
            if not d.allowed:
                _deny(ctx, entry, lane_id, d, Sink.FORM_OTP)
            await _type(typist, entry, box_fresh, Secret(digit))
        return group[-1]
    await _type(typist, entry, fresh, code)
    return fresh


async def _read(typist: Typist) -> tuple[str, list[FieldInfo], dict[str, FieldRole]]:
    url = await typist.page_url()
    fields = await typist.fields()
    return url, fields, classify_fields(fields)


async def login_flow(ctx: Any, vault: Any, typist: Typist, *, lane_id: str, entry: str | None = None,
                     settle_seconds: float = SETTLE_SECONDS) -> dict[str, Any]:
    signal = await typist.signal()
    if signal is Signal.CAPTCHA:
        return _result(False, "start", "captcha", signal)
    url, fields, roles = await _read(typist)
    chosen = await choose_entry(ctx, vault, lane_id=lane_id, url=url, entry=entry)
    origin = parse_origin(url)
    pw = _first(fields, roles, FieldRole.PASSWORD)
    user = _username_for(fields, roles, pw)
    otp = _first(fields, roles, FieldRole.OTP)
    if pw is None and user is None and otp is None:
        return _result(False, "no_login_form", None, signal)
    # Pre-check the first field, so a refused login is refused before the vault is opened.
    first, first_sink = (user, Sink.FORM_USERNAME) if (user is not None and chosen.has_username) else (
        (pw, Sink.FORM_PASSWORD) if pw is not None else (otp, Sink.FORM_OTP))
    if first is None:  # only a username field, but the entry has no username
        return _result(False, "no_login_form", "human", signal)
    if first_sink is Sink.FORM_OTP and not (chosen.has_totp or chosen.config.get("catcher")):
        return _result(False, "start", "totp", signal)   # already at the code step; a human has the code
    d = decide(chosen, lane_id=lane_id, sink=first_sink, url=url, field=first, fields=fields)
    if not d.allowed:
        _deny(ctx, chosen, lane_id, d, first_sink)

    await vault.ensure_unsealed()
    await ctx.authorize(
        Effect.SECRET, lane_id=lane_id, domain=chosen.domain,
        summary=f"Log in to {display_host(origin.host)} on lane {lane_id} with vault entry '{chosen.name}'",
        content={"action": "login", "entry": chosen.id, "origin": str(origin), "lane": lane_id},
    )

    step = "start"
    try:
        if user is not None and chosen.has_username:
            typed = await _fill_secret(ctx, vault, typist, chosen, lane_id, user, fields, Sink.FORM_USERNAME)
            step = "username"
            if pw is None and otp is None:
                await typist.press(typed, "Enter")
                await typist.settle(settle_seconds)
                url, fields, roles = await _read(typist)
                if not check_origin(chosen, url).allowed:
                    return _result(False, step, "human", await typist.signal())
                pw = _first(fields, roles, FieldRole.PASSWORD)
                otp = _first(fields, roles, FieldRole.OTP)
        since = datetime.now(UTC)
        if pw is not None:
            typed = await _fill_secret(ctx, vault, typist, chosen, lane_id, pw, fields, Sink.FORM_PASSWORD)
            step = "password"
            since = datetime.now(UTC)
            await typist.press(typed, "Enter")
            await typist.settle(settle_seconds)
            step = "submitted"
            url, fields, roles = await _read(typist)
            otp = _first(fields, roles, FieldRole.OTP)
        elif otp is None:
            signal = await typist.signal()
            return _result(False, step, "captcha" if signal is Signal.CAPTCHA else "human", signal)

        if otp is not None and check_origin(chosen, url).allowed:
            if not (chosen.has_totp or chosen.config.get("catcher")):
                return _result(False, step, "totp", await typist.signal())
            typed = await _fill_code(ctx, vault, typist, chosen, lane_id, otp, fields, since)
            if typed is None:
                return _result(False, step, _no_code(chosen), await typist.signal())
            step = "totp" if chosen.has_totp else "code"
            await typist.press(typed, "Enter")
            await typist.settle(settle_seconds)
    except TypistAborted:
        ctx.trace("vault.abort", entry=chosen.id, lane=lane_id, step=step)
        return _result(False, step, "human", await typist.signal())
    except CodeDeclined:
        return _result(False, step, "human", await typist.signal())

    signal = await typist.signal()
    url, fields, roles = await _read(typist)
    left = {roles[f.locator] for f in fields if f.locator in roles}
    if signal is Signal.CAPTCHA:
        return _result(False, step, "captcha", signal)
    if signal in LOGIN_HUMAN:
        return _result(False, step, "human", signal)
    if not check_origin(chosen, url).allowed:
        # Redirected off the entry's site (an SSO hop or a challenge page): we cannot
        # tell success from a detour, so a human or the site's session signal decides.
        return _result(False, "left_site", "human", signal)
    if FieldRole.OTP in left:
        return _result(False, step, "totp" if not (chosen.has_totp or _relay(chosen)) else "human", signal)
    if FieldRole.PASSWORD in left:
        return _result(False, step, "human", signal)
    return _result(True, "done", None, signal)


async def fill_flow(ctx: Any, vault: Any, typist: Typist, *, lane_id: str, field_ref: str,
                    entry: str) -> dict[str, Any]:
    """Fill one structurally identified login field (from a snapshot ref) from an entry.
    The field's role decides what is typed: password, username or one-time code."""
    field = await typist.field_for_ref(field_ref)
    fields = await typist.fields()
    url = await typist.page_url()
    try:
        meta = await vault.get_entry(entry)
    except EntryNotFound:
        raise EntryNotFound(f"no vault entry named {entry!r}") from None
    pool = _pool(fields, field)
    role = classify_fields(pool).get(field.locator)
    if role is None:
        try:
            host = parse_origin(url).host
        except ValueError:
            host = None
        _deny(ctx, meta, lane_id, Decision(False, Reason.NOT_A_LOGIN_FIELD, host,
                                           "the field is not a login field (password, username or one-time code)"))
    assert role is not None
    sink = ROLE_SINK[role]
    d = decide(meta, lane_id=lane_id, sink=sink, url=url, field=field, fields=pool)
    if not d.allowed:
        _deny(ctx, meta, lane_id, d, sink)
    origin = parse_origin(url)
    await vault.ensure_unsealed()
    label = {FieldRole.PASSWORD: "password", FieldRole.USERNAME: "username", FieldRole.OTP: "one-time code"}[role]
    await ctx.authorize(
        Effect.SECRET, lane_id=lane_id, domain=meta.domain,
        summary=f"Fill the {label} field on {display_host(origin.host)} on lane {lane_id} "
                f"from vault entry '{meta.name}'",
        content={"action": "fill", "entry": meta.id, "origin": str(origin), "lane": lane_id,
                 "field": role.value},
    )
    try:
        if sink is Sink.FORM_OTP:
            if await _fill_code(ctx, vault, typist, meta, lane_id, field, pool, datetime.now(UTC)) is None:
                needs = _no_code(meta)
                out = {"ok": False, "field": role.value, "needs": needs, "signal": (await typist.signal()).value}
                if needs == "code":
                    out["next"] = CODE_NEXT.format(tool="vault_fill")
                return out
        else:
            await _fill_secret(ctx, vault, typist, meta, lane_id, field, pool, sink)
    except TypistAborted:
        ctx.trace("vault.abort", entry=meta.id, lane=lane_id, step=role.value)
        return {"ok": False, "field": role.value, "needs": "human", "signal": (await typist.signal()).value}
    except CodeDeclined:
        return {"ok": False, "field": role.value, "needs": "human", "signal": (await typist.signal()).value}
    return {"ok": True, "field": role.value, "needs": None, "signal": (await typist.signal()).value}


__all__ = ["TYPE_MAX", "ResolutionDenied", "choose_entry", "fill_flow", "login_flow"]
