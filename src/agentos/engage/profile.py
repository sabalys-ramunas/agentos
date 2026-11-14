"""Profile edits (brief B2): keep the account's public profile at what its program asks for.

A program's optional `profile:` (agentos.engage.models.Profile), or for an account attached to a
profile the one agentos.profiles resolves for it (`effective`), is the public profile the account
should show: its name, headline, About section, photo and cover. This module decides which edits
are due and hands each one to the executor as an operation of its own kind:

    due(program, known, blocked)       the fields that differ, in the fixed order name, headline,
                                       about, photo, cover (pure)
    due_edits(program, known, cap)     the first `cap` of them as Edits (pure)
    edits(store, program)              due_edits from the account's history, uncapped
    next_edit(store, program)          the edit a window's next `profile_edit` operation makes
    changes(old, new)                  what a new version changes in `profile:`, for the owner

**What the account last showed** (`known`) comes from the account's own operation rows
(store.ops, kind `profile_edit`): an edit the readback proved (verified: the profile page,
reloaded as the account, showed exactly the value) makes that value known, and so does a text
edit that ended `already_current` (its preflight found exactly one element showing exactly the
value: nothing was saved, and nothing needs to be). Every other failure, a `preflight_mismatch`
included (the element was missing or not alone), is a failure: the field is tried again on a
later day (a failure blocks it for the rest of its own day), under the cap, and after
FAILURE_NOTICE failures in a row for the same value the owner gets a notice. A field never
edited is unknown, so it is due. An edit still in flight (attempted, waiting for the owner's
approval, done but not proved) blocks the same value from being tried again for BLOCK_TTL, until
`reconcile` settles it, or until the owner resets the field; one the owner denied blocks that
value until a reset. A new value in the program is due again.

**The owner's reset** (`agentos engage profile reset <surface> [--field F]`, `reset`) forgets
what the rows say about a field: the rows up to then are ignored, so the field is unknown (due)
and nothing blocks it. It is kept in the store's meta table (`profile_reset:<surface>:<field>`,
the last op id it covers); nothing else is persisted: the rows are the record.

**The hook** in the executor is small. The planner plans `profile_edit` operations (at most the
day's cap, in the day's first session, see agentos.engage.planner) when `edits(store, program)`
is not empty. When a window runs, `unchecked` names the due fields' operations that may not run
yet (no verified rule or canary mark): the executor then holds `profile_edit` alone (a kind
hold, reason `no_valid_rule`) and the window's other operations go on. It checks nothing when
the day's profile_edit cap is 0 or a profile_edit hold is already in force. Each `profile_edit`
operation then asks `next_edit` for its field: none due means it is skipped (`profile_current`),
else the field's OpSpec runs with the value as its target (the params come from the program,
never from Content, and the operation's inputs are readOnly), with `ask=False`: the value is one
the owner approved (the program, or the profile the account is attached to), which is their
consent, so no card is made; the policy still routes it like any commit ("Ask me first" on the
lane makes it wait). The executor's `reconcile` settles profile rows with every
other kind's (an attempted row whose run finished, an unverified one a later check proved), and
then `notify_failures` sends the notice.

The day's profile_edit cap is the one caps.day_limits gives the executor and the digest for every
kind: the plan's, and an owner who lowers it (`agentos engage cap <surface> profile_edit 0`)
stops edits at once.

The target of a profile row is the value it sets: the text, a files_put id, or for a name
`{"first", "last"}` as JSON (agentos.engage.operations.linkedin_profile_name_params reads it).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from agentos.engage.caps import Kind
from agentos.runner.util import norm_lines

if TYPE_CHECKING:
    from agentos.engage.models import Program
    from agentos.engage.operations import Registry
    from agentos.engage.store import EngageStore

PROFILE_EDIT = str(Kind.PROFILE_EDIT)
FIELDS = ("name", "headline", "about", "photo", "cover")       # the order edits are made in
TEXT_FIELDS = ("name", "headline", "about")
PHOTO_FIELDS = ("photo", "cover")
STORED_PHOTO = "sha256:"   # a photo the profile store keeps, by content (agentos.profiles)
KINDS = {f: f"profile_{f}" for f in FIELDS}                    # the registry's kinds (operations.PROFILE_SPECS)
RULE_FIELDS = {f"linkedin.profile.{f}": f for f in FIELDS}       # a row's rule_id -> its field
# rows that keep their value from being tried again: in flight, done but not proved, or denied
IN_FLIGHT = ("attempted", "awaiting_approval", "unverified")
DENIED = ("denied",)
ALREADY = "already_current"          # the run's code when its one element showed exactly the value
CURRENT = "profile_current"                                     # the skip reason when nothing is due
BLOCK_TTL = timedelta(days=3)        # how long an edit in flight blocks its value
FAILURE_NOTICE = 3                   # failures in a row for one value before the owner hears of it
RESET_KEY = "profile_reset:{surface}:{field}"
NOTICE_KEY = "profile_notice:{surface}:{field}"


@dataclass(frozen=True)
class Edit:
    field: str
    target: str            # the value it sets, as the row's target

    @property
    def kind(self) -> str:
        return KINDS[self.field]


def encode(program: Program | None, field: str) -> str | None:
    """The value `field` should have, as a row's target; None when the program leaves it out."""
    profile = program.profile if program is not None else None
    if profile is None:
        return None
    value = getattr(profile, field)
    if value is None:
        return None
    if field == "name":
        return json.dumps({"first": value.first, "last": value.last}, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    return str(value)


def desired(program: Program | None) -> dict[str, str]:
    return {f: v for f in FIELDS if (v := encode(program, f)) is not None}


def _same(field: str, a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a is b
    if field == "about":           # spaces normalized, line breaks kept, as the readback compares it
        return norm_lines(a) == norm_lines(b)
    return a == b


def due(program: Program | None, known: dict[str, str], blocked: Iterable[tuple[str, str]] = ()) -> list[str]:
    """The fields whose wanted value differs from what the account last showed (`known`), in the
    fixed order, leaving out a (field, value) still in flight or denied (`blocked`)."""
    want = desired(program)
    stop = set(blocked)
    return [f for f in FIELDS if f in want and not _same(f, want[f], known.get(f)) and (f, want[f]) not in stop]


def due_edits(program: Program | None, known: dict[str, str], cap: int,
              blocked: Iterable[tuple[str, str]] = ()) -> list[Edit]:
    """At most `cap` due edits (a cap of 0 makes none), in the fixed order."""
    want = desired(program)
    return [Edit(f, want[f]) for f in due(program, known, blocked)[:max(0, cap)]]


def _at(row: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(row["at"]) if row.get("at") else None
    except ValueError:
        return None


def _resets(store: EngageStore, surface: str) -> dict[str, int]:
    out = {}
    for f in FIELDS:
        v = store.meta(RESET_KEY.format(surface=surface, field=f))
        out[f] = int(v) if v and v.isdigit() else 0
    return out


def _rows(store: EngageStore, surface: str) -> list[tuple[str, str, dict[str, Any]]]:
    """(field, value, row) of the surface's profile rows after each field's reset, oldest first."""
    reset = _resets(store, surface)
    out = []
    for row in store.ops(surface, kind=PROFILE_EDIT):
        field = RULE_FIELDS.get(row.get("rule_id") or "")
        value = row.get("target")
        if field is None or value is None or int(row["id"]) <= reset[field]:
            continue
        out.append((field, value, row))
    return out


def _failed(field: str, row: dict[str, Any]) -> bool:
    """A run that did not make the edit and is no reason to stop asking: not denied, not current."""
    reason = row.get("reason") or ""
    return row.get("status") == "failed" and reason not in DENIED and not (reason == ALREADY and field in TEXT_FIELDS)


def history(store: EngageStore, surface: str, *, now: datetime | None = None,
            day: date | None = None) -> tuple[dict[str, str], set[tuple[str, str]]]:
    """(known, blocked) from the surface's profile rows, oldest first: the last value each field
    was proved (or found) to show, and the (field, value) pairs not to try again yet. With `now`,
    an edit in flight blocks only for BLOCK_TTL after it was attempted; with `day`, a failure on
    that day blocks its value for the rest of the day."""
    known: dict[str, str] = {}
    blocked: set[tuple[str, str]] = set()
    for field, value, row in _rows(store, surface):
        status, reason = row.get("status"), row.get("reason") or ""
        at = _at(row)
        if status == "verified" or (status == "failed" and reason == ALREADY and field in TEXT_FIELDS):
            known[field] = value
            blocked.discard((field, value))
        elif status in IN_FLIGHT:
            if now is None or (at is not None and now - at < BLOCK_TTL):
                blocked.add((field, value))
        elif (status == "failed" and reason in DENIED) or (
                day is not None and _failed(field, row) and row.get("day") == day.isoformat()):
            blocked.add((field, value))         # denied: until a reset; failed: for the rest of its day
    return known, blocked


def lane_file(store: EngageStore, surface: str, target: str) -> str | None:
    """The lane inbox file id an edit of a photo field sends: a files_put id as it is, and a
    profile photo (`sha256:...`, the row's target) its copy in the surface's lane, made now if
    there is none (ProfilesService.lane_photo). None when the photo cannot be had."""
    if not str(target).startswith(STORED_PHOTO):
        return target
    source = getattr(store, "profiles", None)
    if source is None:
        return None
    try:
        return source.lane_photo(surface, target, copy=True)
    except Exception:  # the edit waits; the next session tries again
        import logging

        logging.getLogger("agentos.engage").warning("profiles: no lane copy of %s for %s", target, surface,
                                                    exc_info=True)
        return None


def from_profile(store: EngageStore, surface: str) -> bool:
    """Does the surface take its profile from an approved profile it is attached to
    (agentos.profiles, `store.profiles`), rather than from its program's own `profile:` block?
    False when no profiles store is there or it cannot be read (an edit then asks the owner)."""
    source = getattr(store, "profiles", None)
    if source is None:
        return False
    try:
        return source.for_surface(surface) is not None
    except Exception:  # unreadable: the edit asks, as an inline program's does
        import logging

        logging.getLogger("agentos.engage").warning("profiles: %s not resolved", surface, exc_info=True)
        return False


def effective(store: EngageStore, program: Program | None) -> Program | None:
    """The program with the profile its account should show: the approved profile the account is
    attached to (agentos.profiles, `store.profiles`) replaces the program's own `profile:`, so the
    profile is written once for every account. A program on an account no profile covers is
    returned as it is."""
    source = getattr(store, "profiles", None)
    if program is None or source is None:
        return program
    try:
        attached = source.for_surface(program.surface)
        if attached is None:
            return program
        wanted = source.engine_profile(program.surface, program.site)
    except Exception:  # a broken profiles store must not stop the account's engagement
        import logging

        logging.getLogger("agentos.engage").warning("profiles: %s not resolved", program.surface, exc_info=True)
        return program.model_copy(update={"profile": None})
    return program.model_copy(update={"profile": wanted})


def edits(store: EngageStore, program: Program | None, *, now: datetime | None = None,
          day: date | None = None) -> list[Edit]:
    """Every due edit for the program's surface, uncapped (the planner caps them). An account
    attached to a profile wants what that profile resolves to (`effective`)."""
    program = effective(store, program)
    if program is None or program.profile is None:
        return []
    known, blocked = history(store, program.surface, now=now, day=day)
    return due_edits(program, known, len(FIELDS), blocked)


def due_count(store: EngageStore, program: Program | None, *, now: datetime | None = None) -> int:
    return len(edits(store, program, now=now))


def next_edit(store: EngageStore, program: Program, *, now: datetime | None = None,
              day: date | None = None) -> Edit | None:
    """The edit the next `profile_edit` operation of a window makes, or None (all current)."""
    found = edits(store, program, now=now, day=day)
    return found[0] if found else None


def checked_kinds(store: EngageStore, program: Program, kinds: Iterable[str], *, now: datetime | None = None,
                  day: date | None = None) -> list[str]:
    """The kinds a validity check looks up: `profile_edit` becomes the operations of the fields
    due now (none due: nothing to check, the operation is skipped when it comes)."""
    out: list[str] = []
    for kind in kinds:
        if kind == PROFILE_EDIT:
            out += [e.kind for e in edits(store, program, now=now, day=day)]
        else:
            out.append(kind)
    return out


def unchecked(store: EngageStore, registry: Registry, program: Program, kinds: Iterable[str], *,
              cap: int, held: bool, now: datetime | None = None, day: date | None = None) -> list[str]:
    """The due fields' operations a window's `profile_edit` operations may not run yet (sorted),
    for a profile_edit hold. Nothing when the window plans no profile edit, the day's cap is 0 or
    a profile_edit hold is in force already."""
    if PROFILE_EDIT not in kinds or cap <= 0 or held:
        return []
    site = program.site or ""
    due = checked_kinds(store, program, [PROFILE_EDIT], now=now, day=day)
    return sorted({k for k in due if not registry.valid(registry.get(site, k))})


def reset(store: EngageStore, surface: str, fields: Iterable[str] | None = None) -> list[str]:
    """The owner's reset: forget what the rows say about these fields (every field by default),
    known and blocked alike. Returns the fields reset."""
    chosen = list(fields) if fields is not None else list(FIELDS)
    bad = [f for f in chosen if f not in FIELDS]
    if bad:
        raise ValueError(f"no profile field {bad[0]!r}: the fields are {', '.join(FIELDS)}")
    last = max((int(r["id"]) for r in store.ops(surface, kind=PROFILE_EDIT)), default=0)
    for f in chosen:
        store.set_meta(RESET_KEY.format(surface=surface, field=f), str(last))
        store.set_meta(NOTICE_KEY.format(surface=surface, field=f), str(last))
    return chosen


def failures(store: EngageStore, surface: str) -> dict[str, tuple[str, int, int]]:
    """Per field: (value, failures in a row for it, the first of those rows' id), counting back
    from the field's newest row while it is a failure (_failed) for the same value."""
    by_field: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for field, value, row in _rows(store, surface):
        if row.get("status") in ("planned", "skipped", "held"):
            continue
        by_field.setdefault(field, []).append((value, row))
    out = {}
    for field, rows in by_field.items():
        value = rows[-1][0]
        n, first = 0, 0
        for v, row in reversed(rows):
            if v != value or not _failed(field, row):
                break
            n, first = n + 1, int(row["id"])
        if n:
            out[field] = (value, n, first)
    return out


def notify_failures(store: EngageStore, surface: str) -> list[str]:
    """One notice for each field whose edit failed FAILURE_NOTICE times in a row for the same
    value (once per run of failures). Returns the fields noticed."""
    sent = []
    for field, (_value, n, first_id) in failures(store, surface).items():
        key = NOTICE_KEY.format(surface=surface, field=field)
        if n < FAILURE_NOTICE or int(store.meta(key) or 0) >= first_id:
            continue                                    # too few, or this run of failures was noticed
        store.set_meta(key, str(first_id))
        store.notice("profile", f"{surface}: the {field} edit failed {n} times in a row, so the profile still "
                     f"does not show it. It is tried again on a later day. Check the lane in its live view and the "
                     f"runs' errors; `agentos engage profile reset {surface} --field {field}` forgets what the "
                     "engine knows about the field.", surface=surface)
        sent.append(field)
    return sent


def is_profile(kind: Any) -> bool:
    return str(kind) == PROFILE_EDIT


def changes(old: Program | None, new: Program) -> list[str]:
    """The profile fields a new version sets to another value than the running one, or stops
    setting, as short phrases for the owner ("headline", "photo (no longer set)")."""
    before, after = desired(old), desired(new)
    out = []
    for f in FIELDS:
        if f in after and not _same(f, after[f], before.get(f)):
            out.append(f)
        elif f in before and f not in after:
            out.append(f"{f} (no longer set; the account keeps what it shows)")
    return out
