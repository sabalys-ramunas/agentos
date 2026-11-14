"""Fakes shared by the vault tests (a ToolContext, a Typist with scripted pages, lanes).

Imported by the other test_vault_* modules; it holds no tests of its own except a
sanity check of the fakes.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agentos.core.errors import RefNotFound
from agentos.core.models import LaneInfo, LaneKind, LaneSpec, Principal, Signal
from agentos.core.runtime import Runtime
from agentos.vault import crypto
from agentos.vault.errors import TypistAborted
from agentos.vault.service import VaultService, keyfile_path
from agentos.vault.store import VaultStore
from agentos.vault.typist import FieldInfo

FAST_KDF = {"alg": "argon2id", "m": 64, "t": 1, "p": 1}

PASSWORD = "Tr0ub4dor&3-correct-horse"
USERNAME = "owner@example.org"
SEED = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def private_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    os.chmod(home, 0o700)
    return home


def make_vault(home: Path, *, mode: str = "keyfile", passphrase: str = "a long test passphrase",
               **kwargs: Any) -> VaultService:
    store = VaultStore(home / "vault.db")
    if mode == "keyfile":
        key = os.urandom(32)
        crypto.write_keyfile(keyfile_path(home), key)
        store.initialize(mode="keyfile", master=key, kdf=None)
    else:
        kdf = crypto.new_kdf(FAST_KDF)
        store.initialize(mode="passphrase", master=crypto.derive_master(passphrase, kdf), kdf=kdf)
    kwargs.setdefault("socket_path", home / "run" / "vault.sock")
    return VaultService(home, **kwargs)


class FakeCtx:
    def __init__(self, runtime: Any = None, *, deny: Exception | None = None):
        self.principal = Principal(id="test-agent")
        self.runtime = runtime
        self.deny = deny
        self.authorized: list[dict[str, Any]] = []
        self.traces: list[tuple[str, dict[str, Any]]] = []

    async def authorize(self, effect, *, lane_id=None, domain=None, summary=None, content=None) -> None:
        self.authorized.append({"effect": effect, "lane_id": lane_id, "domain": domain, "summary": summary,
                                "content": content})
        if self.deny is not None:
            raise self.deny

    def trace(self, kind: str, **fields: Any) -> None:
        self.traces.append((kind, fields))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.traces]


def fld(locator: str, origin: str = "https://www.instagram.com", **kw: Any) -> FieldInfo:
    chain = kw.pop("chain", None) or (origin,)
    return FieldInfo(locator=locator, frame_origins=tuple(chain), **kw)


def login_form(origin: str = "https://www.instagram.com", form: str = "f1",
               chain: tuple[str, ...] | None = None) -> list[FieldInfo]:
    """A search box plus a username/password form; `chain` puts the form in frames."""
    return [
        fld("search", origin, type="search", name="q"),
        fld("user", origin, type="text", name="username", autocomplete="username", form=form, chain=chain),
        fld("pass", origin, type="password", name="password", autocomplete="current-password", form=form,
            chain=chain),
    ]


@dataclass
class Page:
    url: str
    fields: list[FieldInfo]
    signal: Signal = Signal.OK
    refs: dict[str, str] = field(default_factory=dict)       # snapshot ref -> locator
    on_enter: str | None = None                              # name of the next page on Enter


class FakeTypist:
    """Scripted pages. Pressing Enter moves to page.on_enter. `navigate_on_focus` swaps the
    page when a given locator is focused (to model a page that changes under us);
    `abort_on` makes type_secret raise TypistAborted for that locator."""

    def __init__(self, pages: dict[str, Page], start: str):
        self.pages = pages
        self.current = start
        self.typed: list[tuple[str, str, str]] = []   # (page, locator, text)
        self.pressed: list[tuple[str, str]] = []
        self.focused: list[str] = []
        self.navigate_on_focus: dict[str, str] = {}
        self.abort_on: set[str] = set()
        self.settles = 0

    @property
    def page(self) -> Page:
        return self.pages[self.current]

    def typed_text(self, locator: str) -> list[str]:
        return [t for _, loc, t in self.typed if loc == locator]

    async def page_url(self) -> str:
        return self.page.url

    async def fields(self) -> list[FieldInfo]:
        return list(self.page.fields)

    async def field_for_ref(self, ref: str) -> FieldInfo:
        loc = self.page.refs.get(ref)
        for f in self.page.fields:
            if f.locator == loc:
                return replace(f, ref=ref)
        raise RefNotFound(f"ref {ref} not found")

    async def focus(self, f: FieldInfo) -> FieldInfo:
        self.focused.append(f.locator)
        if f.locator in self.navigate_on_focus:
            self.current = self.navigate_on_focus.pop(f.locator)
        for g in self.page.fields:
            if g.locator == f.locator:
                return g
        raise RefNotFound(f"field {f.locator} is gone")

    async def type_secret(self, f: FieldInfo, text: str) -> None:
        if f.locator in self.abort_on:
            raise TypistAborted("focus moved")
        self.typed.append((self.current, f.locator, text))

    async def press(self, f: FieldInfo, key: str) -> None:
        self.pressed.append((f.locator, key))
        if key == "Enter" and self.page.on_enter:
            self.current = self.page.on_enter

    async def settle(self, timeout: float = 10.0) -> None:
        self.settles += 1

    async def signal(self) -> Signal:
        return self.page.signal


class FakeLanes:
    def __init__(self, lanes: dict[str, LaneKind]):
        self.infos = {lane: LaneInfo(spec=LaneSpec(id=lane, kind=kind)) for lane, kind in lanes.items()}
        self.used: list[str] = []

    def get(self, lane_id: str) -> LaneInfo:
        from agentos.core.errors import LaneNotFound

        if lane_id not in self.infos:
            raise LaneNotFound(f"no lane {lane_id}")
        return self.infos[lane_id]

    @asynccontextmanager
    async def using(self, lane_id: str):
        self.get(lane_id)
        self.used.append(lane_id)
        yield f"page-of-{lane_id}"


def runtime_with(vault: VaultService, typist: FakeTypist | None = None,
                 lanes: dict[str, LaneKind] | None = None) -> Runtime:
    rt = Runtime(vault=vault, lanes=FakeLanes(lanes or {"biz-a/instagram": LaneKind.BROWSER}))
    if typist is not None:
        rt.register("typist", lambda page: typist)
    return rt


def test_fakes_work():
    t = FakeTypist({"a": Page("https://x.example.com", login_form("https://x.example.com"), on_enter="a")}, "a")
    assert t.page.url.startswith("https://")
    assert FakeCtx().kinds() == []
