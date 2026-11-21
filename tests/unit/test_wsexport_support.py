"""Two nodes for the workspace export tests: real stores (lanes, workspaces, vault, runner, policy) on
temporary homes, the real lane manager with a browser-less engine, and the ops fakes for the host and
the daemon. Imported by the other test_wsexport_* modules; its own test is a sanity check.

Every secret here is a dummy with a distinctive marker, so a test can scan the disk for it.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from test_ops_support import FakeHost, fingerprint
from test_vault_support import FAST_KDF

from openajar.core.config import NodeConfig, save_config, save_proxies
from openajar.core.models import LaneKind, LaneSpec, ProxyConfig
from openajar.core.runtime import Runtime
from openajar.node.lanes import LaneManagerImpl
from openajar.node.store import LaneStore
from openajar.policy.service import PolicyService
from openajar.runner.service import build as build_runner
from openajar.vault import crypto
from openajar.vault.service import VaultService, keyfile_path
from openajar.vault.store import VaultStore
from openajar.workspaces.store import WorkspaceStore
from openajar.workspaces.transfer.sources import Sources

PASSPHRASE = "export passphrase for the tests"
LOG_N = 10
IG_PASSWORD = "VAULTSECRET-ig-password-7f3a"
IG_USER = "VAULTSECRET-user@acme.example"
API_KEY = "VAULTSECRET-api-key-0c1d2e"
SEED = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
SHARED_PASSWORD = "VAULTSECRET-shared-9988"
OTHER_PASSWORD = "VAULTSECRET-other-ws-only"
PROXY_PASSWORD = "PROXYSECRET-lt1-password"
MARKERS = (IG_PASSWORD, IG_USER, API_KEY, SEED, SHARED_PASSWORD, OTHER_PASSWORD)


class NoBrowserEngine:
    """The lane manager's engine for tests that never launch a browser."""
    name = "camoufox"

    async def launch(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an export or import must never launch a browser")


class FakeCoordinator:
    """The daemon's ops socket: which lanes are warm, and close/open, which stop or start the
    lane's browser in the FakeHost."""

    def __init__(self, host: FakeHost, cfg: NodeConfig, *, available: bool = True):
        self.host, self.cfg, self.up = host, cfg, available
        self.warm_set: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.restart_on_close: set[str] = set()   # a lane an agent warms again right after the pause

    def available(self) -> bool:
        return self.up

    def warm(self) -> set[str]:
        return set(self.warm_set)

    def start(self, lane_id: str) -> None:
        self.warm_set.add(lane_id)
        self.host.browsers[str(self.cfg.profile_dir(lane_id))] = [4242]

    def close(self, lane_id: str) -> None:
        self.calls.append(("close", lane_id))
        self.host.browsers.pop(str(self.cfg.profile_dir(lane_id)), None)
        self.warm_set.discard(lane_id)

    def open(self, lane_id: str) -> None:
        self.calls.append(("open", lane_id))
        self.start(lane_id)


class Node:
    """One node on a temporary home. `vault_mode`: keyfile, passphrase or None (no vault yet)."""

    def __init__(self, root: Path, name: str, *, proxies: dict[str, ProxyConfig] | None = None,
                 vault_mode: str | None = "keyfile", vault_passphrase: str = "a long vault passphrase"):
        home = root / name
        home.mkdir(mode=0o700, parents=True)
        self.cfg = NodeConfig(home=home, name=name, mcp_token=f"tok-{name}-0123456789")
        save_config(self.cfg)
        if proxies is None:
            proxies = {"lt1": ProxyConfig(label="lt1", host="192.0.2.10", port=1000, username="u1",
                                          password=PROXY_PASSWORD)}
        save_proxies(self.cfg, proxies)
        self.lanes = LaneManagerImpl(self.cfg, LaneStore(self.cfg.db_path), NoBrowserEngine(), displays=None)
        self.vault_passphrase = vault_passphrase
        if vault_mode is not None:
            store = VaultStore(home / "vault.db")
            if vault_mode == "keyfile":
                key = os.urandom(32)
                crypto.write_keyfile(keyfile_path(home), key)
                store.initialize(mode="keyfile", master=key, kdf=None)
            else:
                kdf = crypto.new_kdf(FAST_KDF)
                store.initialize(mode="passphrase", master=crypto.derive_master(vault_passphrase, kdf), kdf=kdf)
        self.vault = VaultService(home, socket_path=home / "run" / "vault.sock")
        self.workspaces = WorkspaceStore(home / "workspaces.db")
        runner = build_runner(Runtime(), home=home, register=False)
        self.flows, self.scheduler = runner.flows, runner.scheduler
        self.policy = PolicyService(home, node_config=self.cfg)
        self.host = FakeHost()
        self.host.hostname = lambda: f"host-of-{name}"  # type: ignore[method-assign]
        self.coordinator = FakeCoordinator(self.host, self.cfg)

    @property
    def home(self) -> Path:
        return self.cfg.home

    def sources(self) -> Sources:
        return Sources(config=self.cfg, lanes=self.lanes, workspaces=self.workspaces, vault=self.vault,
                       flows=self.flows, scheduler=self.scheduler, policy=self.policy, coordinator=self.coordinator,
                       host=self.host)

    def close(self) -> None:
        self.workspaces.close()
        self.flows.store.close()
        self.policy.close()

    # ---- building a workspace -----------------------------------------------------------

    def add_lane(self, lane_id: str, *, proxy: str | None = "lt1", pinned: bool = True,
                 kind: LaneKind = LaneKind.BROWSER) -> Path:
        spec = LaneSpec(id=lane_id, proxy_label=proxy, kind=kind)
        if kind == LaneKind.BROWSER:
            asyncio.run(self.lanes.create(spec))
        else:  # the browser lane manager refuses other kinds; their rows still share the store
            self.lanes.store.add(spec)
            self.cfg.profile_dir(lane_id).mkdir(parents=True, exist_ok=True)
        profile = self.cfg.profile_dir(lane_id)
        (profile / "cookies.sqlite").write_bytes(b"SQLite format 3\x00cookies-of-" + lane_id.encode() + os.urandom(256))
        (profile / "prefs.js").write_text('user_pref("browser.startup.homepage", "about:blank");\n')
        (profile / "storage" / "default" / "https+++www.instagram.com").mkdir(parents=True)
        (profile / "storage" / "default" / "https+++www.instagram.com" / "ls.sqlite").write_bytes(os.urandom(5000))
        (profile / "cache2").mkdir()
        (profile / "cache2" / "entry").write_bytes(b"cached page")
        os.symlink("127.0.0.1:+4242", profile / "lock")
        for p in profile.rglob("*"):
            if not p.is_symlink():
                p.chmod(0o700 if p.is_dir() else 0o600)
        if pinned:
            self.cfg.fingerprint_path(lane_id).write_text(json.dumps(fingerprint(158)))
        return profile

    def build_acme(self) -> None:
        """Workspace acme (two lanes, readiness, vault entries, flows, schedules, grants) next to
        workspace other, which must never leak into an export of acme."""
        self.workspaces.insert_workspace({"id": "acme", "name": "Acme Coffee", "color": "#4f7cff",
                                          "default_proxy": "lt1", "notes": "main brand",
                                          "created_at": "2026-11-01T00:00:00+00:00",
                                          "updated_at": "2026-11-01T00:00:00+00:00"})
        self.add_lane("acme/instagram")
        self.add_lane("acme/reddit", pinned=False)
        self.add_lane("other/x")
        self.workspaces.record_lane({"id": "acme/instagram", "workspace": "acme", "surface": "instagram",
                                     "platform": "instagram", "template": "smb", "home_url": "https://instagram.com/",
                                     "login_url": "https://instagram.com/accounts/login/",
                                     "created_at": "2026-11-01T00:00:00+00:00"})
        self.workspaces.set_readiness("acme/instagram", workspace="acme", platform="instagram", readiness="ready",
                                      note="logged in by hand", by="owner", now="2026-11-02T10:00:00+00:00")
        run = asyncio.run
        run(self.vault.add_entry(name="acme ig", kind="password", domain="instagram.com", lanes=["acme/instagram"],
                                 username=IG_USER, secret=IG_PASSWORD, totp=SEED))
        run(self.vault.add_entry(name="acme api", kind="api_key", domain="example.com", lanes=["acme/*"],
                                 secret=API_KEY, config={"header": "X-Api-Key", "prefix": ""}))
        run(self.vault.add_entry(name="shared login", kind="password", domain="reddit.com",
                                 lanes=["acme/reddit", "other/x"], secret=SHARED_PASSWORD))
        run(self.vault.add_entry(name="other only", kind="password", domain="x.com", lanes=["other/x"],
                                 secret=OTHER_PASSWORD))
        self.flows.save(flow("acme.instagram.post", "acme/instagram", title="v1"),
                        by={"kind": "human", "id": "owner", "owner": "owner"})
        self.flows.save(flow("acme.instagram.post", "acme/instagram", title="v2"),
                        by={"kind": "human", "id": "owner", "owner": "owner"})
        self.flows.save(flow("other.x.read", "other/x"), by="owner")
        self.flows.save(flow("all.reddit.read", "*/reddit"), by="owner")  # spans workspaces
        self.cron, _ = self.scheduler.add(flow="acme.instagram.post", cron="0 9 * * *")
        self.hook, self.hook_extras = self.scheduler.add(flow="acme.instagram.post", webhook=True)
        self.shared_sched, _ = self.scheduler.add(flow="all.reddit.read", on="acme/reddit", cron="0 10 * * *")
        self.scheduler.add(flow="other.x.read", cron="0 11 * * *")
        self.grant, self.grant_token = self.policy.issue(subject={"id": "acme-agent"}, workspaces=["acme"],
                                                         ceiling="draft", ttl_s=7 * 86400)
        self.agent_sched, _ = self.scheduler.add(flow="acme.instagram.post", cron="30 9 * * *",
                                                 principal={"kind": "agent", "id": "acme-agent",
                                                            "grant_id": self.grant.id},
                                                 grant_id=self.grant.id)
        self.child, _ = self.policy.attenuate(self.grant_token, subject={"id": "acme-sub"}, ceiling="read")
        self.policy.issue(subject={"id": "other-agent"}, workspaces=["other"], ttl_s=86400)
        self.policy.issue(subject={"id": "everywhere"}, workspaces=["*"], ttl_s=86400)


def flow(flow_id: str, lane: str, *, title: str = "read") -> dict[str, Any]:
    return {"id": flow_id, "title": title, "lane": lane, "effect": "read",
            "steps": [{"type": "navigate", "url": "https://example.com/"}]}


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and not p.is_symlink()}


def files_containing(root: Path, needles: tuple[str, ...]) -> list[str]:
    """Every file under root whose bytes contain one of the needles (plain or UTF-16)."""
    hits = []
    for p in root.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        data = p.read_bytes()
        for needle in needles:
            if needle.encode() in data or needle.encode("utf-16-le") in data:
                hits.append(f"{p}: {needle}")
    return hits


def test_the_two_nodes_are_sane(tmp_path):
    a = Node(tmp_path, "node-a")
    try:
        a.build_acme()
        assert [i.spec.id for i in a.lanes.list()] == ["acme/instagram", "acme/reddit", "other/x"]
        assert asyncio.run(a.vault.list_entries(lane="acme/instagram"))[0].name == "acme api"
        assert a.host.hostname() == "host-of-node-a"
        assert files_containing(a.home, MARKERS) == []  # the vault seals every value
    finally:
        a.close()
