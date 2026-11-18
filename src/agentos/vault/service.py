"""The vault service (runtime name "vault").

Holds the master key in memory while unsealed, resolves secrets through the
No-Body-Injection policy, writes `vault.use` traces, and never hands a secret to
anything but its sink. Built by `build(runtime)`; the daemon calls `await start()`
(auto-unseal for key-file vaults, control socket for `agentos vault unseal`) and
`await stop()`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.core.config import home_from_env
from agentos.core.errors import InvalidRequest, ServiceUnavailable
from agentos.core.layout import vault_socket
from agentos.policy.scrub import MIN_SECRET, MIN_USERNAME, Matcher
from agentos.vault import crypto
from agentos.vault.catcher import CodeCatcher, CodeSource, ImapCodeCatcher, ImapCodeSource
from agentos.vault.domains import parse_origin
from agentos.vault.errors import (
    CatcherError,
    CodeDeclined,
    EntryNotFound,
    ResolutionDenied,
    VaultError,
    VaultKeyMissing,
    VaultLockedOut,
    VaultNotInitialized,
    VaultSealed,
    VaultWrongKey,
    WrongPassphrase,
)
from agentos.vault.models import EntryKind, EntryMeta, NewEntry, validate_lanes, validate_new_entry
from agentos.vault.policy import Decision, Reason, Sink, decide, entry_bound, lane_bound, matching_entries
from agentos.vault.relay import CodeRelay, RelayCodeSource
from agentos.vault.secret import Secret
from agentos.vault.store import SEALED_FIELDS, VaultStore, WrongKey
from agentos.vault.totp import code_at, parse_totp, seconds_left, seed_forms
from agentos.vault.typist import FieldInfo, TypistFactory

log = logging.getLogger("agentos.vault")

LOCKOUT_AFTER = 5
CODE_MEMORY_SECONDS = 900
TOTP_MIN_LEFT = 3.0

CatcherFactory = Callable[[str, int, Secret, Secret], CodeCatcher]


def keyfile_path(home: Path) -> Path:
    return Path(home) / "secrets" / "vault.key"


def pending_keyfile_path(keyfile: Path) -> Path:
    return keyfile.with_name(keyfile.name + ".new")


def load_keyfile_master(store: VaultStore, keyfile: Path) -> bytes:
    """The master key from the key file. Finishes an interrupted key-file rotation
    (`vault.key.new` written and the vault rewrapped, but not yet renamed)."""
    pending = pending_keyfile_path(keyfile)
    key: bytes | None
    try:
        key = crypto.read_keyfile(keyfile)
    except VaultKeyMissing:
        key = None
    if key is not None:
        try:
            store.check_master(key)
            return key
        except WrongKey:
            pass
    if pending.exists():
        new_key = crypto.read_keyfile(pending)
        try:
            store.check_master(new_key)
        except WrongKey:
            raise VaultWrongKey(f"neither {keyfile} nor {pending} opens this vault") from None
        os.replace(pending, keyfile)
        crypto.fsync_dir(keyfile.parent)
        log.warning("vault: finished an interrupted key file rotation")
        return new_key
    if key is None:
        raise VaultKeyMissing(f"vault key file {keyfile} is missing")
    raise VaultWrongKey(f"vault key file {keyfile} does not open this vault")


MACHINE_SECRET_MAX = 65536
_MACHINE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_MACHINE_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _file_stamp(path: Path) -> tuple[int, int]:
    """Changes when the file is written, by this process or another (the CLI writes directly)."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _machine_ref(ref: Any) -> str:
    if not isinstance(ref, str) or not _MACHINE_REF_RE.match(ref) or ".." in ref.split("/"):
        raise InvalidRequest("a machine secret ref is 1 to 200 letters, digits and . _ : / -")
    return ref


def _machine_kind(kind: Any) -> str:
    if not isinstance(kind, str) or not _MACHINE_KIND_RE.match(kind):
        raise InvalidRequest("a machine secret kind is a short lowercase word such as oauth_token")
    return kind


def _printable(text: Any, limit: int) -> str:
    text = "" if text is None else str(text)
    return "".join(c for c in text if c.isprintable())[:limit].strip()


def _machine_label(label: Any, ref: str) -> str:
    return _printable(label, 200) or ref


def _machine_purpose(purpose: Any) -> str:
    out = _printable(purpose, 120)
    if not out:
        raise InvalidRequest("reading a machine secret needs a purpose")
    return out


def _machine_scope(scope: Any) -> dict[str, Any]:
    scope = dict(scope or {})
    if len(scope) > 20 or any(not isinstance(k, str) or len(k) > 40 for k in scope):
        raise InvalidRequest("a machine secret scope is a small map of names to plain values")
    for v in scope.values():
        if not (v is None or isinstance(v, bool | int | float) or (isinstance(v, str) and len(v) <= 200)):
            raise InvalidRequest("a machine secret scope holds only short plain values")
    return scope


def _as_secret(value: str | Secret | None) -> Secret | None:
    if value is None or isinstance(value, Secret):
        return value
    if not isinstance(value, str):
        raise InvalidRequest("secret values must be strings")
    return Secret(value)


class VaultService:
    name = "vault"

    def __init__(self, home: Path, *, socket_path: Path | None = None,
                 catcher_factory: CatcherFactory | None = None,
                 clock: Callable[[], float] = time.time, sleep=asyncio.sleep):
        self.home = Path(home)
        self.store = VaultStore(self.home / "vault.db")
        self.keyfile = keyfile_path(self.home)
        self.socket_path = Path(socket_path) if socket_path else vault_socket(self.home)  # short when home is long
        self.catcher_factory = catcher_factory
        self.typist_factory: TypistFactory | None = None
        # an environment lane's title and repository (agentos.environments.logins sets it): what decides
        # which environments a login the owner bound to them covers (agentos.vault.policy.entry_bound)
        self.lane_facts: Callable[[str], dict[str, Any] | None] | None = None
        self._clock, self._sleep = clock, sleep
        self._master: bytearray | None = None
        self._sealed_by_owner = False
        self._mutex = threading.Lock()
        self._unseal_lock: asyncio.Lock | None = None
        self._failures = 0
        self._locked_until = 0.0
        self._last_error: str | None = None
        self._recent_codes: dict[str, list[tuple[float, Secret]]] = {}
        self._scrub_gen = 0  # bumped when the key changes: the cached scrub matchers are rebuilt
        self._scrub_cache: dict[str | None, tuple[tuple[Any, ...], Matcher]] = {}
        self._control: Any = None
        self.relay = CodeRelay(clock=clock)   # open code cards (the "relay" code source)

    def __repr__(self) -> str:
        return f"VaultService(home={str(self.home)!r}, state={self.state()!r})"

    # ---- lifecycle --------------------------------------------------------------

    async def start(self, *, control: bool = True) -> None:
        """Check the database, unseal a key-file vault, open the control socket.
        Never raises for vault problems: they show in status() and the vault stays sealed."""
        try:
            header = await asyncio.to_thread(self.store.header)
            if header is not None:
                await asyncio.to_thread(self.store.quick_check)
                if header.mode == "keyfile":
                    await self.unseal()
        except VaultError as e:
            self._last_error = f"{e.code}: {e.message}"
            log.error("vault: %s", self._last_error)
        if control and self._control is None:
            from agentos.vault.control import start_server

            try:
                self._control = await start_server(self, self.socket_path)
            except (OSError, VaultError) as e:
                log.error("vault: control socket unavailable: %s", e)

    async def stop(self) -> None:
        if self._control is not None:
            self._control.close()
            try:
                await self._control.wait_closed()
            except Exception as e:  # noqa: BLE001
                log.debug("vault: control socket close: %s", type(e).__name__)
            self._control = None
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass
        self._drop_master()

    # ---- sealing ----------------------------------------------------------------

    def state(self) -> str:
        try:
            header = self.store.header()
        except VaultError:
            return "error"
        if header is None:
            return "uninitialized"
        return "unsealed" if self._master is not None else "sealed"

    def status(self) -> dict[str, Any]:
        try:
            header = self.store.header()
            count = self.store.count() if header else 0
        except VaultError as e:
            return {"initialized": None, "state": "error", "error": {"code": e.code, "message": e.message}}
        if header is None:
            return {"initialized": False, "state": "uninitialized"}
        out: dict[str, Any] = {
            "initialized": True,
            "state": "unsealed" if self._master is not None else "sealed",
            "mode": header.mode,
            "key_version": header.key_version,
            "entries": count,
            "machine_secrets": self._machine_count(),
        }
        wait = self._locked_until - self._clock()
        if wait > 0:
            out["locked_out_seconds"] = int(wait) + 1
        if self._last_error:
            out["last_error"] = self._last_error
        return out

    def _machine_count(self) -> int:
        try:
            return len(self.store.list_machine())
        except VaultError:
            return 0

    def _drop_master(self) -> None:
        with self._mutex:
            if self._master is not None:
                for i in range(len(self._master)):
                    self._master[i] = 0
            self._master = None
            self._scrub_gen += 1
            self._scrub_cache.clear()

    def _set_master(self, key: bytes) -> None:
        with self._mutex:
            self._master = bytearray(key)
            self._sealed_by_owner = False
            self._scrub_gen += 1
            self._last_error = None

    async def unseal(self, passphrase: str | Secret | None = None) -> dict[str, Any]:
        """Key-file vaults: re-read the key file. Passphrase vaults: derive and check."""
        if self._unseal_lock is None:
            self._unseal_lock = asyncio.Lock()
        async with self._unseal_lock:
            header = await asyncio.to_thread(self.store.header)
            if header is None:
                raise VaultNotInitialized(f"no vault at {self.store.path}; run: agentos vault init")
            if header.mode == "keyfile":
                key = await asyncio.to_thread(load_keyfile_master, self.store, self.keyfile)
            else:
                wait = self._locked_until - self._clock()
                if wait > 0:
                    raise VaultLockedOut(f"too many wrong passphrases; try again in {int(wait) + 1} seconds")
                secret = _as_secret(passphrase)
                if not secret:
                    raise InvalidRequest("this vault is sealed with a passphrase; give the passphrase to unseal it")
                key = await asyncio.to_thread(crypto.derive_master, secret.reveal(), header.kdf)
                try:
                    await asyncio.to_thread(self.store.check_master, key)
                except WrongKey:
                    self._failures += 1
                    if self._failures >= LOCKOUT_AFTER:
                        self._locked_until = self._clock() + min(300, 30 * 2 ** (self._failures - LOCKOUT_AFTER))
                    raise WrongPassphrase("wrong passphrase") from None
                self._failures = 0
            self._set_master(key)
        return self.status()

    def seal(self) -> dict[str, Any]:
        self._drop_master()
        with self._mutex:
            self._sealed_by_owner = True
        self._recent_codes.clear()
        self.relay.cancel_all()  # nothing is typed into a site after the owner sealed the vault
        return self.status()

    def _require_master(self) -> bytes:
        with self._mutex:
            if self._master is not None:
                return bytes(self._master)
            sealed_by_owner = self._sealed_by_owner
        header = self.store.header()
        if header is None:
            raise VaultNotInitialized(f"no vault at {self.store.path}; run: agentos vault init")
        if header.mode == "keyfile" and not sealed_by_owner:
            key = load_keyfile_master(self.store, self.keyfile)
            self._set_master(key)
            return key
        raise VaultSealed("the vault is sealed; run: agentos vault unseal")

    def _with_master(self, fn: Callable[[bytes], Any]) -> Any:
        """Run fn(master). If the key changed under us (rotated from the CLI), reload a
        key-file key once; a passphrase vault goes back to sealed."""
        master = self._require_master()
        try:
            return fn(master)
        except WrongKey:
            self._drop_master()
        header = self.store.header()
        if header is not None and header.mode == "keyfile" and not self._sealed_by_owner:
            master = self._require_master()
            try:
                return fn(master)
            except WrongKey:
                self._drop_master()
        raise VaultSealed("the vault key changed (it was rotated); unseal again: agentos vault unseal")

    async def ensure_unsealed(self) -> None:
        """Raise VaultSealed (or a key-file error) now, before a human is asked to approve."""
        await asyncio.to_thread(self._with_master, self.store.check_master)

    async def _call(self, fn: Callable[[bytes], Any]) -> Any:
        self._scrub_gen += 1  # an entry may change: the file's stamp alone can miss a write in the same tick
        try:
            return await asyncio.to_thread(self._with_master, fn)
        finally:
            self._scrub_gen += 1

    # ---- entries (metadata and admin) -------------------------------------------

    def facts_for(self, lane_id: str) -> dict[str, Any] | None:
        """The environment of an environment's lane, as entry_bound reads it; None for any other lane, an
        environment that is not open, or a node without environments (then no scoped login is used)."""
        if self.lane_facts is None or not str(lane_id).startswith("env/"):
            return None
        try:
            return self.lane_facts(lane_id)
        except Exception:
            log.warning("vault: could not read the environment of %s", lane_id, exc_info=True)
            return None

    async def list_entries(self, lane: str | None = None) -> list[EntryMeta]:
        entries = await asyncio.to_thread(self.store.list)
        if lane is not None:
            facts = self.facts_for(lane)
            entries = [e for e in entries if entry_bound(e, lane, facts)]
        return entries

    async def get_entry(self, ref: str) -> EntryMeta:
        return await asyncio.to_thread(self.store.get, ref)

    async def add_entry(self, *, name: str, kind: str | EntryKind, domain: str | None = None,
                        lanes: Sequence[str] = (), hosts: Sequence[str] = (),
                        username: str | Secret | None = None, secret: str | Secret | None = None,
                        totp: str | Secret | None = None, config: dict[str, Any] | None = None,
                        replace: bool = False) -> EntryMeta:
        entry = validate_new_entry(NewEntry(
            name=name, kind=kind, domain=domain, hosts=list(hosts), lanes=list(lanes),
            username=_as_secret(username), secret=_as_secret(secret), totp=_as_secret(totp),
            config=config or {},
        ))
        return await self._call(lambda m: self.store.add(m, entry, replace=replace))

    async def remove_entry(self, ref: str) -> EntryMeta:
        return await asyncio.to_thread(self.store.remove, ref)

    async def bind(self, ref: str, lanes: Sequence[str]) -> EntryMeta:
        add = validate_lanes(list(lanes))
        current = await self.get_entry(ref)
        return await self._call(lambda m: self.store.update(m, current.id, lanes=sorted({*current.lanes, *add})))

    async def unbind(self, ref: str, lanes: Sequence[str]) -> EntryMeta:
        drop = set(lanes)
        current = await self.get_entry(ref)
        return await self._call(
            lambda m: self.store.update(m, current.id, lanes=[p for p in current.lanes if p not in drop]))

    async def set_config(self, ref: str, config: dict[str, Any]) -> EntryMeta:
        return await self._call(lambda m: self.store.update(m, ref, config=config))

    # ---- workspace export and import (agentos.workspaces.transfer.export, agentos.workspaces.transfer.import_) ----------

    async def export_entries(self, workspace: str) -> list[tuple[EntryMeta, dict[str, Secret | None]]]:
        """Every entry with a binding inside `workspace` (a lane `workspace/name` or the wildcard
        `workspace/*`), integrity-checked, with its sealed fields opened. Only for a workspace export,
        which re-encrypts them at once; a sealed vault raises VaultSealed."""
        prefix = f"{workspace}/"
        rows = await self._call(lambda m: self.store.open_all(m, SEALED_FIELDS))
        return [(meta, values) for meta, values in rows if any(p.startswith(prefix) for p in meta.lanes)]

    async def import_entries(self, entries: Sequence[NewEntry], *, on_conflict: str = "error") -> dict[str, Any]:
        """Seal entries carried over from another node, all in one transaction (all or nothing).
        on_conflict (same name here): error, skip or rename. Returns add_many's counts and metas."""
        if on_conflict not in ("error", "skip", "rename"):
            raise InvalidRequest("on_conflict must be error, skip or rename")
        checked = [validate_new_entry(e) for e in entries]
        return await self._call(lambda m: self.store.add_many(m, checked, on_conflict=on_conflict))

    # ---- machine secrets (opaque, no sink) -----------------------------------------
    # For other node services: a brain's subscription credential, a brain task's child grant.
    # Sealed like every entry; never listed with entries, never resolved into a page; every read
    # records its purpose. Values never appear in logs, errors or return values other than
    # get_secret's.

    async def put_secret(self, ref: str, value: str | Secret, *, kind: str = "secret", label: str = "",
                         scope: dict[str, Any] | None = None) -> dict[str, Any]:
        ref, kind, label, scope = _machine_ref(ref), _machine_kind(kind), _machine_label(label, ref), \
            _machine_scope(scope)
        secret = _as_secret(value)
        if not secret:
            raise InvalidRequest("a machine secret must not be empty")
        if len(secret.reveal().encode("utf-8")) > MACHINE_SECRET_MAX or "\x00" in secret.reveal():
            raise InvalidRequest(f"a machine secret must be at most {MACHINE_SECRET_MAX} bytes, without NUL")
        meta = await self._call(lambda m: self.store.put_machine(m, ref, secret, kind=kind, label=label, scope=scope))
        log.info("vault: stored machine secret %s (%s)", ref, kind)
        return meta

    async def get_secret(self, ref: str, *, purpose: str) -> str | None:
        """The value of a machine secret, or None. `purpose` is recorded with the read."""
        ref, purpose = _machine_ref(ref), _machine_purpose(purpose)
        secret = await self._call(lambda m: self.store.open_machine(m, ref, purpose=purpose))
        if secret is None:
            return None
        log.info("vault: machine secret %s read for %s", ref, purpose)
        return secret.reveal()

    async def has_secret(self, ref: str) -> bool:
        """Whether a machine secret exists, without unsealing it."""
        return await asyncio.to_thread(self.store.has_machine, _machine_ref(ref))

    async def delete_secret(self, ref: str) -> None:
        ref = _machine_ref(ref)
        if await asyncio.to_thread(self.store.delete_machine, ref):
            log.info("vault: deleted machine secret %s", ref)

    async def list_secrets(self) -> list[dict[str, Any]]:
        """Machine secrets: refs, kinds, labels and use counts, never values."""
        return await asyncio.to_thread(self.store.list_machine)

    # ---- resolution -------------------------------------------------------------

    def _trace(self, ctx: Any, kind: str, **fields: Any) -> None:
        try:
            ctx.trace(kind, **fields)
        except Exception:
            log.exception("vault: trace failed")

    def _denied(self, ctx: Any, entry: EntryMeta | None, lane_id: str, sink: Sink, d: Decision) -> None:
        self._trace(ctx, "vault.deny", entry=entry.id if entry else None, lane=lane_id, host=d.host,
                    sink=sink.value, reason=d.reason.value)
        d.check()

    async def _open(self, ref: str, fields: tuple[str, ...]) -> tuple[EntryMeta, dict[str, Secret | None]]:
        return await self._call(lambda m: self.store.open(m, ref, fields))

    async def _used(self, ctx: Any, meta: EntryMeta, lane_id: str, host: str | None, sink: Sink, **extra: Any) -> None:
        self._trace(ctx, "vault.use", entry=meta.id, lane=lane_id, host=host, sink=sink.value, **extra)
        try:
            await asyncio.to_thread(self.store.record_use, meta.id)
        except VaultError as e:  # stats only; never fail a login over a counter
            log.warning("vault: could not record use of %s: %s", meta.id, e.code)

    async def resolve_field(self, ctx: Any, *, ref: str, lane_id: str, url: str, field: FieldInfo,
                            fields: Sequence[FieldInfo], sink: Sink) -> Secret:
        """The username or password for a structurally identified login field."""
        if sink not in (Sink.FORM_USERNAME, Sink.FORM_PASSWORD):
            raise InvalidRequest("resolve_field fills username and password fields; use one_time_code")
        column = "username" if sink is Sink.FORM_USERNAME else "secret"
        meta, values = await self._open(ref, (column,))
        d = decide(meta, lane_id=lane_id, sink=sink, url=url, field=field, fields=fields,
                   lane_facts=self.facts_for(lane_id))
        if not d.allowed:
            self._denied(ctx, meta, lane_id, sink, d)
        value = values[column]
        if value is None:
            self._denied(ctx, meta, lane_id, sink, Decision(False, Reason.NO_USERNAME, d.host, "entry has no value"))
        await self._used(ctx, meta, lane_id, d.host, sink, field=column)
        assert value is not None
        return value

    async def one_time_code(self, ctx: Any, *, ref: str, lane_id: str, url: str, field: FieldInfo,
                            fields: Sequence[FieldInfo], since: datetime | None = None) -> Secret | None:
        """A TOTP code computed from the entry's seed, a code caught from email, or a code the
        owner typed into the panel's code card. None when the source timed out; CodeDeclined
        when the owner dismissed the card."""
        meta, values = await self._open(ref, ("totp",))
        d = decide(meta, lane_id=lane_id, sink=Sink.FORM_OTP, url=url, field=field, fields=fields,
                   lane_facts=self.facts_for(lane_id))
        if not d.allowed:
            self._denied(ctx, meta, lane_id, Sink.FORM_OTP, d)
        seed = values["totp"]
        extra: dict[str, Any] = {}
        if seed is not None:
            spec = parse_totp(seed.reveal())
            left = seconds_left(spec, self._clock())
            if left < TOTP_MIN_LEFT:
                await self._sleep(left + 0.25)
            code = code_at(spec, self._clock())
            source = "totp"
        else:
            code_source = self._code_source(ctx, meta, lane_id)
            code = None
            if code_source is not None:
                code, extra = await self._wait_code(ctx, code_source, meta, lane_id, d.host,
                                                    since or datetime.now(UTC))
            source = "manual" if code_source is not None and code_source.kind == "relay" else "email"
            if code is None:
                self._trace(ctx, "vault.code_timeout", entry=meta.id, lane=lane_id, host=d.host, **extra)
                return None
        self._remember_code(lane_id, code)
        await self._used(ctx, meta, lane_id, d.host, Sink.FORM_OTP, field="one_time_code", source=source, **extra)
        return code

    def _code_source(self, ctx: Any, meta: EntryMeta, lane_id: str) -> CodeSource | None:
        cfg = meta.config.get("catcher")
        if not cfg:
            return None
        if cfg.get("kind") == "relay":
            return RelayCodeSource(self.relay, cfg)
        return ImapCodeSource(cfg, lambda: self._mailbox(ctx, cfg, lane_id))

    async def _wait_code(self, ctx: Any, source: CodeSource, meta: EntryMeta, lane_id: str, host: str | None,
                         since: datetime) -> tuple[Secret | None, dict[str, Any]]:
        """Arm the source and wait. A relay source's request is traced from open to close; the
        trace never holds the code."""
        pending = await source.arm(ctx, meta, lane_id=lane_id, site=meta.domain, not_before=since)
        if pending.request_id is None:
            return await pending.wait(), {}
        request = getattr(pending, "request", None)
        audit = {"request": pending.request_id, "consent": "manual",
                 "requester": request.requester.get("id") if request is not None else None}
        self._trace(ctx, "vault.code_requested", entry=meta.id, lane=lane_id, host=host, source="manual", **audit)
        try:
            code = await pending.wait()
        except CodeDeclined:
            self._trace(ctx, "vault.code_declined", entry=meta.id, lane=lane_id, host=host, source="manual",
                        by=request.closed_by if request is not None else None, **audit)
            raise
        if code is not None and request is not None:
            latency = int(((request.closed_at or self._clock()) - request.created_at) * 1000)
            self._trace(ctx, "vault.code_used", entry=meta.id, lane=lane_id, host=host, source="manual",
                        by=request.closed_by, latency_ms=latency, **audit)
        return code, {"request": pending.request_id}

    async def _mailbox(self, ctx: Any, cfg: dict[str, Any], lane_id: str) -> CodeCatcher:
        try:
            account, values = await self._open(cfg["account"], ("username", "secret"))
        except EntryNotFound:
            raise CatcherError(f"the code catcher's mailbox entry {cfg['account']!r} does not exist") from None
        url = f"imaps://{cfg['host']}:{cfg['port']}" if cfg["port"] != 143 else f"imap://{cfg['host']}:143"
        d = decide(account, lane_id=lane_id, sink=Sink.IMAP_LOGIN, url=url)
        if not d.allowed:
            self._denied(ctx, account, lane_id, Sink.IMAP_LOGIN, d)
        username, password = values["username"], values["secret"]
        if username is None or password is None:
            raise CatcherError(f"mailbox entry {account.name!r} needs a username and a password")
        await self._used(ctx, account, lane_id, d.host, Sink.IMAP_LOGIN)
        factory = self.catcher_factory or (
            lambda host, port, user, pw: ImapCodeCatcher(host, port, user, pw, clock=self._clock, sleep=self._sleep))
        return factory(cfg["host"], cfg["port"], username, password)

    def _remember_code(self, lane_id: str, code: Secret) -> None:
        now = self._clock()
        kept = [(t, c) for t, c in self._recent_codes.get(lane_id, []) if t > now]
        kept.append((now + CODE_MEMORY_SECONDS, code))
        self._recent_codes[lane_id] = kept[-10:]

    async def _pick(self, lane_id: str, url: str, ref: str | None, kind: EntryKind) -> EntryMeta:
        if ref is not None:
            return await self.get_entry(ref)
        entries = matching_entries(await self.list_entries(), lane_id=lane_id, url=url, kinds=(kind,),
                                   lane_facts=self.facts_for(lane_id))
        if not entries:
            try:
                host = parse_origin(url).host
            except ValueError:
                host = "this page"
            raise ResolutionDenied(f"vault refused: no {kind.value} entry bound to lane {lane_id} matches {host}",
                                   reason=Reason.NO_ENTRY.value)
        if len(entries) > 1:
            raise InvalidRequest(f"{len(entries)} vault entries match; name one: "
                                 + ", ".join(e.name for e in entries))
        return entries[0]

    async def resolve_auth_header(self, ctx: Any, *, lane_id: str, url: str,
                                  ref: str | None = None) -> tuple[str, Secret]:
        """(header name, header value) for an api_key entry, for the node's egress
        forwarder only. Never return this to a tool caller."""
        picked = await self._pick(lane_id, url, ref, EntryKind.API_KEY)
        meta, values = await self._open(picked.id, ("secret",))
        d = decide(meta, lane_id=lane_id, sink=Sink.AUTH_HEADER, url=url)
        if not d.allowed:
            self._denied(ctx, meta, lane_id, Sink.AUTH_HEADER, d)
        secret = values["secret"]
        assert secret is not None
        await self._used(ctx, meta, lane_id, d.host, Sink.AUTH_HEADER)
        header = meta.config.get("header", "Authorization")
        prefix = meta.config.get("prefix", "Bearer " if header.lower() == "authorization" else "")
        return header, Secret(prefix + secret.reveal())

    async def resolve_app_secret(self, ctx: Any, *, ref: str, lane_id: str, url: str) -> Secret:
        """An api_key entry's value for the node's swap of an agent environment's request to the bound
        host (agentos.environments.swap): the entry must be bound to the lane and the host one of its
        own. Never return this to a tool caller."""
        meta, values = await self._open(ref, ("secret",))
        d = decide(meta, lane_id=lane_id, sink=Sink.APP_REQUEST, url=url)
        if not d.allowed:
            self._denied(ctx, meta, lane_id, Sink.APP_REQUEST, d)
        secret = values["secret"]
        if secret is None:
            self._denied(ctx, meta, lane_id, Sink.APP_REQUEST, Decision(False, Reason.NO_ENTRY, d.host,
                                                                         "entry has no value"))
        await self._used(ctx, meta, lane_id, d.host, Sink.APP_REQUEST)
        assert secret is not None
        return secret

    # ---- redaction --------------------------------------------------------------

    def scrub(self, text: str, lane_id: str | None = None, *, codes_lane: str | None = None) -> str:
        """Replace any known secret (passwords, api keys, cookies, TOTP seeds, usernames of
        MIN_USERNAME or more characters, recent one-time codes) in `text` with [redacted], in any
        letter case and percent-encoded (agentos.policy.scrub says what is not caught). For
        snapshots, traces and logs. Secrets of entries bound to `lane_id` (all entries, and the
        machine secrets, when None). Recent codes of `lane_id`, else of `codes_lane` (page text a
        tool read on that lane), else of every lane.

        Raises when the vault cannot be read (VaultBusy, a damaged file): the caller withholds the
        text (runtime_scrub, the log filter) rather than pass it unscrubbed."""
        if not text:
            return text
        now = self._clock()
        only = lane_id if lane_id is not None else codes_lane
        codes = [c.reveal() for lane, kept in list(self._recent_codes.items())
                 if only is None or lane == only for t, c in kept if t > now]
        if codes:
            text = Matcher(c for c in codes if len(c) >= MIN_SECRET).sub(text)
        return self._stored_matcher(lane_id).sub(text)

    def _scrub_stamp(self) -> tuple[Any, ...]:
        try:
            return (self._scrub_gen, self._master is not None, *(
                _file_stamp(p) for p in (self.store.path, self.store.path.with_name(self.store.path.name + "-wal"))))
        except OSError:
            return ()

    def _stored_matcher(self, lane_id: str | None) -> Matcher:
        """The matcher for the stored values, built once per state of the vault file and the key.
        Building decrypts every entry (milliseconds); scrub runs on every log line and snapshot.

        A read that fails is tried once more, then raises: a matcher missing values is never
        built, so never cached. A matcher is cached only when nothing changed while it was built
        (a write through this service, or to the file from another process)."""
        stamp = self._scrub_stamp()
        cached = self._scrub_cache.get(lane_id)
        if stamp and cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            values = self._stored_values(lane_id)
        except VaultError:  # VaultBusy and the like: once more, then the caller withholds the text
            values = self._stored_values(lane_id)
        matcher = Matcher(values)
        if stamp and self._scrub_stamp() == stamp:
            self._scrub_cache[lane_id] = (stamp, matcher)
        return matcher

    def _stored_values(self, lane_id: str | None) -> list[str]:
        """Every value to scrub. Raises VaultError when the vault cannot be read: an empty list
        here would let every password through."""
        if self._master is None:
            return []
        rows = self._with_master(lambda m: self.store.open_all(m, ("username", "secret", "totp"), skip_bad=True))
        values: list[str] = []
        for meta, vals in rows:
            if meta.kind is EntryKind.NOTE or (lane_id is not None and not lane_bound(meta.lanes, lane_id)):
                continue
            if vals["secret"] is not None and len(vals["secret"].reveal()) >= MIN_SECRET:
                values.append(vals["secret"].reveal())
            if vals["username"] is not None and len(vals["username"].reveal()) >= MIN_USERNAME:
                values.append(vals["username"].reveal())
            if vals["totp"] is not None:
                values.extend(seed_forms(vals["totp"].reveal()))
        if lane_id is None:  # a brain's subscription token, a task's child grant, a git deploy key
            for s in self._with_master(self.store.machine_values):
                v = s.reveal()
                if len(v) >= MIN_SECRET:
                    values.append(v)
                if "\n" in v:  # a PEM key: each body line on its own too, so part of it is caught
                    values.extend(line.strip() for line in v.splitlines()
                                  if len(line.strip()) >= 16 and not line.startswith("-----"))
        return values

    # ---- typist -----------------------------------------------------------------

    def typist_for(self, runtime: Any) -> TypistFactory:
        factory = self.typist_factory or (runtime.get("typist") if runtime is not None else None)
        if factory is None:
            raise ServiceUnavailable("typist is not configured on this node, so the vault cannot type into lanes")
        return factory


def build(runtime: Any) -> VaultService:
    cfg = runtime.get("config") if runtime is not None else None
    home = Path(cfg.home) if cfg is not None else home_from_env()
    service = VaultService(home)
    if runtime is not None:
        service.relay.runtime = runtime   # code cards push through "notifier" and publish on "events"
        runtime.register("vault", service)
    return service
