"""The profiles service (runtime service "profiles"): one profile per person or business, many accounts.

    build(runtime)                       -> ProfilesService, registered as "profiles"
    save(text, by=, as_owner=)           validate a whole profile (YAML or JSON) and save it as a version
    update(ref, changes, by=, as_owner=) the latest version with `changes` merged in, saved the same way
    attach(ref, surface, ...) / detach   a version with that account added or taken out
    approve(ref, version, by=) / reject  owner only
    get(ref) / list(workspace)           read
    status(ref)                          per account: what it should show, and how far it is
    for_surface(surface)                 the approved profile an account is attached to
    voice(surface)                       the voice drafts and agents write in on that account
    context(ref, task)                   the approved profile, sliced for design, build, write or review
    propose(ref, change, reason, by=)    a draft of brand, guidelines or references, with its reason and diff

**Who approves.** The owner in person (the CLI on the node, the panel) saves an approved version
at once. Anyone else saves a draft that does nothing until the owner approves it, so an agent
can never change a public identity on its own. Once a version is approved, nothing asks again:
the owner's save (or approval) and the logins they saved are their consent, and every account is
brought in line without a card by its learned operations (agentos.profiles.sync).

**Accounts.** An account (a surface, `workspace/name`) is attached to at most one approved
profile. Its values come from agentos.profiles.mapping: what the site has, the account's `map`
and `overrides`, the site's limits. A value a site would not take is refused when the profile is
saved (the message names the account and the field), never cut short later.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from agentos.core.config import home_from_env
from agentos.core.errors import InvalidRequest, NotFound
from agentos.profiles import context as ctx_slice
from agentos.profiles import mapping
from agentos.profiles.errors import OwnerOnly, ProfileInvalid, ProfileSecret
from agentos.profiles.fields import clean_text
from agentos.profiles.models import PROFILE_ID_RE, Attachment, Profile, ProfileState, ProfileVersion
from agentos.profiles.screen import flags_note, instruction_flags
from agentos.profiles.sections import refuse_secrets
from agentos.profiles.store import FONT_TYPES, PHOTO_TYPES, SVG, ProfileStore

log = logging.getLogger("agentos.profiles")

SERVICE_NAME = "profiles"
MAX_PROFILE_CHARS = 120_000     # room for a brand's tokens (64 KB of JSON at most)
MAX_VOICE_CHARS = 4000          # what a draft provider and an agent get of the voice
PROPOSABLE = ("brand", "guidelines", "references")
LIST_KEYS = {"guidelines": "id", "references": "name"}
SCREENED_NEXT = (" Some of its text reads like an instruction to agents (`screened`): the owner sees it marked "
                 "before approving. If it is a rule for the work, word it as one.")
# which stored file types each field may name
FILE_KINDS = {"photo": set(PHOTO_TYPES), "image": {*PHOTO_TYPES, SVG}, "font": set(FONT_TYPES)}


def is_owner(by: Any) -> bool:
    """The human owner in person (the CLI on the node, the owner's panel session). Agents,
    flows, brains and other humans are not."""
    if by is None:
        return False
    p = by if isinstance(by, dict) else (by.model_dump() if hasattr(by, "model_dump") else vars(by))
    return p.get("kind") == "human" and bool(p.get("id")) and p.get("id") == p.get("owner", "owner")


def _who(by: Any) -> dict[str, Any] | None:
    if by is None:
        return None
    p = by if isinstance(by, dict) else (by.model_dump() if hasattr(by, "model_dump") else dict(vars(by)))
    return {k: p.get(k) for k in ("kind", "id", "owner") if p.get(k) is not None}


def validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        where = ".".join(str(x) for x in err.get("loc", ()) if not str(x).startswith("function-"))
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{where}: {msg}" if where else msg)
    return "; ".join(parts)


def value_hash(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def split_ref(ref: str) -> tuple[str, str]:
    """`workspace/profile` -> (workspace, profile)."""
    ws, _, pid = str(ref or "").strip().partition("/")
    if not ws or not PROFILE_ID_RE.match(pid or ""):
        raise InvalidRequest(f"a profile is named 'workspace/profile', like 'acme/acme', not {ref!r}")
    return ws, pid


def parse_profile(text: str | dict[str, Any]) -> Profile:
    if isinstance(text, dict):
        data: Any = dict(text)
    else:
        if len(text) > MAX_PROFILE_CHARS:
            raise ProfileInvalid(f"the profile is longer than {MAX_PROFILE_CHARS} characters")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ProfileInvalid(f"the profile is not valid YAML or JSON: {str(e).splitlines()[0]}") from None
        if not isinstance(data, dict):
            raise ProfileInvalid("write the profile as YAML (or JSON) with keys such as profile, workspace, kind, "
                                 "display_name, headline, bio and accounts")
    try:
        return Profile.model_validate(data)
    except ValidationError as e:
        secret = [err for err in e.errors() if err.get("type") == "secret_in_profile"]
        if secret:
            raise ProfileSecret(str(secret[0]["msg"])) from None
        raise ProfileInvalid(validation_message(e)) from None


def load_changes(changes: dict[str, Any] | str, what: str = "changes") -> dict[str, Any]:
    if isinstance(changes, str):
        if len(changes) > MAX_PROFILE_CHARS:
            raise ProfileInvalid(f"{what} are longer than {MAX_PROFILE_CHARS} characters")
        try:
            changes = yaml.safe_load(changes)
        except yaml.YAMLError as e:
            raise ProfileInvalid(f"{what} are not valid YAML or JSON: {str(e).splitlines()[0]}") from None
    if not isinstance(changes, dict):
        raise ProfileInvalid(f"{what} are the fields to set, as YAML or JSON, like {{headline: ..., bio: {{short: ...}}}}")
    return changes


def apply_list(current: list[dict[str, Any]], change: Any, key: str, what: str) -> list[dict[str, Any]]:
    """A list section after a change: a list replaces it; {add: [...], remove: [...]} adds (or
    replaces, by `key`) and removes items, so one rule changes without copying the rest."""
    if isinstance(change, list):
        return change
    if not isinstance(change, dict) or not set(change) <= {"add", "remove"}:
        raise ProfileInvalid(f"{what} is a list (the whole new list) or {{add: [...], remove: [{key}s]}}")
    add, remove = change.get("add") or [], change.get("remove") or []
    if not isinstance(add, list) or not isinstance(remove, list):
        raise ProfileInvalid(f"{what}.add is a list of items and {what}.remove a list of {key}s")
    known = {str(x.get(key)) for x in current}
    missing = [str(r) for r in remove if str(r) not in known]
    if missing:
        raise ProfileInvalid(f"{what}.remove names no item with {key} {', '.join(missing[:5])}")
    adding = {str(a.get(key)): a for a in add if isinstance(a, dict)}
    if len(adding) != len(add):
        raise ProfileInvalid(f"each of {what}.add is an item with its own {key}")
    out = [x for x in current if str(x.get(key)) not in {*map(str, remove), *adding}]
    return out + list(adding.values())


def merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """`changes` over `base`: nested dicts merge, anything else (lists included) replaces, and a
    None removes the key."""
    out = dict(base)
    for k, v in changes.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def account_changes(old: Profile | None, new: Profile) -> dict[str, list[str]]:
    """Per attached account, the site fields whose value the new version changes (or stops
    setting), for the owner's one summary: 'headline changes on 3 accounts'."""
    out: dict[str, list[str]] = {}
    for a in new.accounts:
        after = mapping.resolve(new, a).values
        before_a = old.attachment(a.surface) if old is not None else None
        before = mapping.resolve(old, before_a).values if old is not None and before_a is not None else {}
        changed = [f for f in after if after[f] != before.get(f)]
        changed += [f"{f} (no longer set; the account keeps what it shows)" for f in before if f not in after]
        if changed:
            out[a.surface] = changed
    return out


def summarize_changes(changes: dict[str, list[str]]) -> list[str]:
    by_field: dict[str, int] = {}
    for fields in changes.values():
        for f in fields:
            by_field[f] = by_field.get(f, 0) + 1
    return [f"{f} changes on {n} account{'s' if n != 1 else ''}" for f, n in by_field.items()]


class ProfilesService:
    def __init__(self, runtime: Any = None, *, home: Path | None = None, clock: Any = time.time):
        cfg = runtime.get("config") if runtime is not None else None
        self.runtime = runtime
        self.home = Path(home) if home is not None else (Path(cfg.home) if cfg is not None else home_from_env())
        self.clock = clock
        self.store = ProfileStore(self.home)
        self._lock = threading.RLock()
        self.learner: Any = None        # agentos.profiles.learner.ProfileLearner, set by build()
        self.sync: Any = None           # agentos.profiles.sync.ProfileSync, set by build()

    async def start(self) -> None:
        if self.sync is not None:
            await self.sync.start()

    async def stop(self) -> None:
        if self.sync is not None:
            await self.sync.stop()
        self.store.close()

    # ---------------------------------------------------------------- writing
    def _check(self, profile: Profile) -> list[str]:
        """Problems that refuse a save: a value a site would not take, an account another profile has."""
        problems: list[str] = []
        for a in profile.accounts:
            r = mapping.resolve(profile, a)
            problems += r.problems
            other = self.for_surface(a.surface)
            if other is not None and (other.definition.workspace, other.definition.id) != (profile.workspace, profile.id):
                problems.append(f"{a.surface} is attached to profile {other.definition.workspace}/"
                                f"{other.definition.id}; detach it there first")
        files = {f"photos.{w}": (getattr(profile.photos, w), "photo") for w in ("avatar", "cover")}
        if profile.brand is not None:
            files.update({where: (ref, "font" if ".file" in where else "image")
                          for where, ref in profile.brand.files().items()})
        for where, (ref, kind) in files.items():
            if ref is None:
                continue
            try:
                _path, meta = self.store.photo(ref)
            except NotFound:
                problems.append(f"{where}: no stored {'photo' if kind == 'photo' else 'file'} {ref[:20]}...; upload it first "
                                "(profile_photo_put, or agentos profile photo)")
                continue
            if meta["type"] not in FILE_KINDS[kind]:
                wanted = {"photo": "a JPEG, PNG, WebP or GIF photo", "image": "an image (PNG, JPEG, WebP, GIF or SVG)",
                          "font": "a font (WOFF2, WOFF, TTF or OTF)"}[kind]
                problems.append(f"{where}: {ref[:20]}... is {meta['type']}; it takes {wanted}")
        return problems

    def save(self, text: str | dict[str, Any] | Profile, *, by: Any, as_owner: bool,
             note: str | None = None, reason: str | None = None) -> dict[str, Any]:
        """Validate and save a whole profile. With `as_owner` (the owner in person: the CLI on the
        node, a signed-in panel session, never a bearer token) it is approved at once; else it is
        a draft that waits for the owner. Callers say which: nothing is inferred from `by`."""
        profile = text if isinstance(text, Profile) else parse_profile(text)
        with self._lock:              # the one-profile-per-account check and the save are one step
            problems = self._check(profile)
            if problems:
                raise ProfileInvalid("; ".join(problems[:5]))
            running = self.store.active(profile.workspace, profile.id)
            # an agent's text that other agents will be served is screened like memory's (screen.py)
            flags = [] if as_owner is True else instruction_flags(
                profile, running.definition if running is not None else None)
            if flags:
                reason = (f"{reason}\n" if reason else "") + flags_note(flags)
            pv, created = self.store.put(profile, created_by=_who(by), reason=reason)
            changes = account_changes(running.definition if running is not None else None, profile)
            if as_owner is True and pv.state is ProfileState.DRAFT:
                pv = self.store.decide(profile.workspace, profile.id, pv.version, ProfileState.APPROVED,
                                       by=str((_who(by) or {}).get("id", "owner")), note=note)
        if pv.state is ProfileState.APPROVED and self.sync is not None:
            self.sync.wake()
        out = self._saved(pv, created, changes)
        if flags:
            out["screened"] = flags
            out["next"] += SCREENED_NEXT
        return out

    def _saved(self, pv: ProfileVersion, created: bool, changes: dict[str, list[str]]) -> dict[str, Any]:
        ref = f"{pv.definition.workspace}/{pv.definition.id}"
        if pv.state is ProfileState.APPROVED:
            nxt = ("The profile is live. Fields agentos has learned on a site are changed on every account by "
                   "themselves; profile_sync_status lists the rest, and an agent can teach each one "
                   "(profile_learn_start).")
        else:
            nxt = (f"Saved as draft version {pv.version}. Nothing changes until the owner approves it: "
                   f"agentos profile approve {ref} --version {pv.version}, or in the panel.")
        return {"profile": ref, "version": pv.version, "state": pv.state.value, "hash": pv.hash, "created": created,
                "changes": changes, "summary": summarize_changes(changes), "next": nxt}

    def base_definition(self, ref: str) -> Profile:
        """What a change builds on: the approved version, else (a profile never approved yet) the
        latest draft. Never a draft waiting after the approved one: an owner's edit must not carry
        an agent's unapproved changes live with it."""
        ws, pid = split_ref(ref)
        active = self.store.active(ws, pid)
        return active.definition if active is not None else self.store.version(ws, pid).definition

    def update(self, ref: str, changes: dict[str, Any] | str, *, by: Any, as_owner: bool) -> dict[str, Any]:
        """The approved version (`base_definition`) with `changes` merged in. With no profile by
        that name yet, `changes` is the whole new profile."""
        changes = load_changes(changes)
        ws, pid = split_ref(ref)
        try:
            base = self.base_definition(ref).canonical()
        except NotFound:
            base = {"profile": pid, "workspace": ws}
        for key in ("profile", "workspace"):
            if key in changes and changes[key] != base.get(key):
                raise ProfileInvalid(f"{key} cannot change; save a new profile under the new name instead")
        return self.save(merge(base, changes), by=by, as_owner=as_owner)

    def propose(self, ref: str, change: dict[str, Any] | str, reason: str, *, by: Any) -> dict[str, Any]:
        """An agent's proposed change to the brand, guidelines or references: always a draft, saved
        with its reason and a readable diff, for the owner to approve (decide). The brand merges
        like profile_update (null removes a key); guidelines and references take a whole list, or
        {add: [...], remove: [ids or names]}."""
        change = load_changes(change, "the change")
        other = sorted(set(change) - set(PROPOSABLE))
        if other:
            raise ProfileInvalid(f"profile_propose changes {', '.join(PROPOSABLE)}; {', '.join(other)} "
                                 "change with profile_update")
        if not change:
            raise ProfileInvalid("the change is empty: give brand, guidelines or references")
        try:
            why = clean_text(reason, "reason", 500, lines=True)
            refuse_secrets(why, "reason")
        except PydanticCustomError as e:      # the reason looks like a secret
            raise ProfileSecret(str(e)) from None
        except ValueError as e:
            raise ProfileInvalid(f"reason: {e}") from None
        base = self.base_definition(ref)
        data = base.canonical()
        for key, value in change.items():
            if value is None:
                data.pop(key, None)
            elif key in LIST_KEYS:
                data[key] = apply_list(data.get(key) or [], value, LIST_KEYS[key], key)
            elif isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key] = merge(data[key], value)
            else:
                data[key] = value
        new = parse_profile(data)
        diff = ctx_slice.profile_diff(base, new)
        if not diff:
            raise ProfileInvalid("the change matches the profile as it is; nothing to propose")
        out = self.save(new, by=by, as_owner=False, reason=why)
        out["reason"] = why
        out["diff"] = diff
        out["next"] = (f"Proposed as draft version {out['version']}; it waits for the owner (the panel, or agentos "
                       f"profile approve {ref} --version {out['version']}). You cannot approve it. Keep working with "
                       "the approved profile meanwhile." + (SCREENED_NEXT if out.get("screened") else ""))
        return out

    def attach(self, ref: str, surface: str, *, by: Any, site: str | None = None,
               overrides: dict[str, str] | None = None, as_owner: bool) -> dict[str, Any]:
        profile = self.base_definition(ref)
        accounts = [a for a in profile.accounts if a.surface != surface]
        try:
            accounts.append(Attachment(surface=surface, site=site, overrides=overrides or {}))
        except ValidationError as e:
            raise ProfileInvalid(validation_message(e)) from None
        data = profile.canonical()
        data["accounts"] = [a.model_dump(exclude_none=True) for a in accounts]
        return self.save(data, by=by, as_owner=as_owner)

    def detach(self, ref: str, surface: str, *, by: Any, as_owner: bool) -> dict[str, Any]:
        profile = self.base_definition(ref)
        if profile.attachment(surface) is None:
            raise NotFound(f"{surface} is not attached to {ref}")
        data = profile.canonical()
        data["accounts"] = [a.model_dump(exclude_none=True) for a in profile.accounts if a.surface != surface]
        return self.save(data, by=by, as_owner=as_owner)

    def decide(self, ref: str, version: int, *, approve: bool, by: Any, in_person: bool,
               note: str | None = None) -> dict[str, Any]:
        """Approve or reject one draft, named by its version (never "the latest": another draft
        may have arrived since the owner looked). Only the owner in person: `in_person` is the
        caller's word that this is the CLI on the node or a signed-in panel session."""
        if in_person is not True or not is_owner(by):
            raise OwnerOnly("only the owner approves or rejects a profile, in person (agentos profile approve on "
                            "the node, or the panel). Agents save drafts; they never approve them")
        ws, pid = split_ref(ref)
        with self._lock:
            pv = self.store.version(ws, pid, int(version))
            if pv.state is ProfileState.APPROVED and approve:
                return self._saved(pv, False, {})
            if approve:
                problems = self._check(pv.definition)
                if problems:
                    raise ProfileInvalid("; ".join(problems[:5]))
            running = self.store.active(ws, pid)
            changes = account_changes(running.definition if running is not None else None, pv.definition)
            state = ProfileState.APPROVED if approve else ProfileState.REJECTED
            pv = self.store.decide(ws, pid, pv.version, state, by=str((_who(by) or {}).get("id", "owner")),
                                   note=note)
        if approve and self.sync is not None:
            self.sync.wake()
        return self._saved(pv, False, changes if approve else {})

    def put_photo(self, workspace: str, name: str, data: bytes) -> dict[str, Any]:
        """Keep a photo for the workspace's profiles; set it with photos.avatar or photos.cover."""
        return self.store.put_photo(workspace, name, data)

    # ---------------------------------------------------------------- reading
    def get(self, ref: str, *, version: int | None = None) -> dict[str, Any]:
        """The approved version (else the latest draft) and any draft waiting after it."""
        ws, pid = split_ref(ref)
        active = self.store.active(ws, pid)
        if version is not None:
            pv = self.store.version(ws, pid, version)
            out = pv.public()
            if pv.state is ProfileState.DRAFT:      # what approving it would change, for the owner
                out["diff"] = ctx_slice.profile_diff(active.definition if active is not None else None, pv.definition)
            return out
        latest = self.store.version(ws, pid)
        out = (active or latest).public()
        if active is not None and latest.version > active.version and latest.state is ProfileState.DRAFT:
            out["draft_waiting"] = latest.version
        voice = (active or latest).definition.voice_prompt()
        if voice:
            out["voice_prompt"] = voice
        return out

    def list(self, workspace: str | None = None, *, can_see: Any = None) -> list[dict[str, Any]]:
        out = []
        for pv in self.store.latest(workspace):
            p = pv.definition
            if can_see is not None and not can_see(p.workspace):
                continue
            active = self.store.active(p.workspace, p.id)
            shown = active or pv
            out.append({"profile": f"{p.workspace}/{p.id}", "kind": shown.definition.kind.value,
                        "display_name": shown.definition.display_name, "headline": shown.definition.headline,
                        "accounts": [a.surface for a in shown.definition.accounts],
                        "version": shown.version, "state": shown.state.value,
                        "draft_waiting": pv.version if active is not None and pv.version > active.version
                        and pv.state is ProfileState.DRAFT else None})
        return out

    def context(self, ref: str, task: str, *, detail: str = "summary") -> dict[str, Any]:
        """The approved profile as one task needs it (agentos.profiles.context.slice_for). Never
        a draft: an agent works as what the owner approved."""
        if task not in ctx_slice.TASKS:
            raise InvalidRequest(f"task is one of {', '.join(ctx_slice.TASKS)}, not {str(task)[:20]!r}")
        if detail not in ctx_slice.DETAILS:
            raise InvalidRequest(f"detail is summary or full, not {str(detail)[:20]!r}")
        ws, pid = split_ref(ref)
        active = self.store.active(ws, pid)
        latest = self.store.version(ws, pid)
        if active is None:
            raise NotFound(f"profile {ref} has no approved version yet: the owner approves draft "
                           f"{latest.version} first (the panel, or agentos profile approve {ref} --version "
                           f"{latest.version})")
        waiting = latest.version if latest.version > active.version and latest.state is ProfileState.DRAFT else None
        return ctx_slice.slice_for(active, task, detail=detail, draft_waiting=waiting)

    def for_surface(self, surface: str) -> ProfileVersion | None:
        for pv in self.store.approved():
            if pv.definition.attachment(surface) is not None:
                return pv
        return None

    def voice(self, surface: str) -> str | None:
        pv = self.for_surface(surface)
        text = pv.definition.voice_prompt() if pv is not None else None
        return text[:MAX_VOICE_CHARS] if text else None

    def resolved(self, surface: str) -> mapping.Resolved | None:
        pv = self.for_surface(surface)
        if pv is None:
            return None
        return mapping.resolve(pv.definition, pv.definition.attachment(surface))

    # ---------------------------------------------------------------- sync status
    def status(self, ref: str) -> dict[str, Any]:
        """Per attached account of the approved profile: each site field with its value's state.

        in_sync           a learned operation proved the account shows it
        done_by_hand      someone changed it in the lane and said so (profile_mark_synced), for this value
        failed            the last try failed; it is tried again later
        manual            nobody taught agentos this site's field yet (`learnable` says whether an
                          agent can: profile_learn_start), or it is changed in the lane by hand
        learning          an agent is teaching it now; learn_failed: the last lesson did not verify
        syncing           a learned, verified operation brings it in line at the next sync
        proving_elsewhere learned, and waits for its first save on the account it was learned on
        held              the lane met a challenge or a sign-out; it waits for a person
        relearn           the site changed; an agent should teach it again
        """
        ws, pid = split_ref(ref)
        pv = self.store.active(ws, pid)
        if pv is None:
            raise NotFound(f"profile {ref} has no approved version yet")
        accounts = []
        for a in pv.definition.accounts:
            r = mapping.resolve(pv.definition, a)
            marks = self.store.marks(a.surface)
            fields = {f: {"value": v, "state": "done_by_hand" if (marks.get(f) or {}).get("value_hash") == value_hash(v)
                          else "manual"} for f, v in r.values.items()}
            self._learned_states(a.surface, r, fields)
            accounts.append({"surface": a.surface, "site": r.site, "fields": fields, "skipped": r.skipped,
                             "problems": r.problems})
        return {"profile": ref, "version": pv.version, "accounts": accounts}

    def _learned_states(self, surface: str, r: mapping.Resolved, fields: dict[str, dict[str, Any]]) -> None:
        """Manual fields a learned operation covers (or is being taught) read their sync state."""
        from agentos.node.sites import profile_edit_endpoint
        from agentos.profiles.learner import NOT_LEARNED

        rows = self.store.sync(surface)
        for f in r.manual:
            entry = fields[f]
            row = rows.get(f) or {}
            st = self.learner.state(r.site, f) if self.learner is not None else None
            entry["learnable"] = (f not in NOT_LEARNED and isinstance(entry["value"], str)
                                  and profile_edit_endpoint(r.site, f) is not None)
            if row.get("status") in ("learning", "learn_failed"):
                entry["state"] = row["status"]
                if row.get("error"):
                    entry["error"] = row["error"]
            if st is None or not (st["verified"] or (st["read_verified"] and st["learned_on"] == surface)):
                if st is not None and st["read_verified"] and not st["verified"]:
                    entry["state"] = "proving_elsewhere"     # learned; proved first on another account
                continue
            entry["learned"] = True
            same = row.get("value_hash") == value_hash(entry["value"])
            status = row.get("status") if same else None
            entry["state"] = status if status in ("in_sync", "held", "relearn", "failed") else "syncing"
            if row.get("error") and status in ("held", "relearn", "failed"):
                entry["error"] = row["error"]

    def mark_synced(self, surface: str, fields: list[str], *, by: Any) -> dict[str, Any]:
        """Note that these manual fields now show the profile's value on the account (changed in
        the lane by hand). A later value makes the field manual again."""
        r = self.resolved(surface)
        if r is None:
            raise NotFound(f"{surface} is not attached to an approved profile")
        bad = [f for f in fields if f not in r.manual]
        if bad:
            raise InvalidRequest(f"{', '.join(bad)} on {surface} is not a manual field "
                                 f"(the manual ones are {', '.join(r.manual) or 'none'})")
        who = str((_who(by) or {}).get("id") or "unknown")
        for f in fields:
            self.store.mark(surface, f, value_hash(r.values[f]), by=who)
        return {"surface": surface, "marked": fields}


def build(runtime: Any, *, home: Path | None = None) -> ProfilesService:
    """The service, registered on the runtime when there is one."""
    from agentos.profiles.learner import ProfileLearner
    from agentos.profiles.sync import ProfileSync

    service = ProfilesService(runtime, home=home)
    service.learner = ProfileLearner(service)
    service.sync = ProfileSync(service, service.learner)
    if runtime is not None:
        runtime.register(SERVICE_NAME, service)
    return service
