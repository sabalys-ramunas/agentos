"""Workspace export and import between two nodes: the round trip, renaming, grants, proxies, the vault,
and every way the file or the target can be wrong. The nodes are real stores on temporary homes
(see test_wsexport_support)."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import tarfile
import tempfile
from pathlib import Path

import pytest
from test_wsexport_support import (
    API_KEY,
    IG_PASSWORD,
    IG_USER,
    LOG_N,
    MARKERS,
    OTHER_PASSWORD,
    PASSPHRASE,
    PROXY_PASSWORD,
    SEED,
    SHARED_PASSWORD,
    Node,
    files_containing,
    tree,
)

from openajar.core import age
from openajar.core.models import LaneKind, ProxyConfig
from openajar.workspaces.transfer import export as wsexport
from openajar.workspaces.transfer import import_ as wsimport
from openajar.workspaces.transfer import read as wsread
from openajar.workspaces.transfer.format import BadExport, ExportRefused
from openajar.workspaces.transfer.import_ import ImportOptions


@pytest.fixture
def nodes(tmp_path):
    a, b = Node(tmp_path, "node-a"), Node(tmp_path, "node-b")
    a.build_acme()
    yield a, b
    a.close()
    b.close()


def export(node: Node, dest: Path, workspace: str = "acme", **kw):
    kw.setdefault("log_n", LOG_N)
    if "recipients" not in kw:
        kw.setdefault("passphrase", PASSPHRASE)
    return asyncio.run(wsexport.export_workspace(node.sources(), workspace, dest, **kw))


def read(node: Node, path: Path, **kw):
    if "identities" not in kw:
        kw.setdefault("passphrase", PASSPHRASE)
    return wsread.read_export(path, wsread.staging_dir(node.home, "import"), **kw)


def plan(node: Node, contents, **options):
    return asyncio.run(wsimport.plan_import(contents, node.sources(), ImportOptions(**options)))


def do_import(node: Node, path: Path, *, passphrase: str = PASSPHRASE, **options):
    contents = read(node, path, passphrase=passphrase)
    try:
        p = plan(node, contents, **options)
        assert p.ok, p.blockers
        return p, asyncio.run(wsimport.apply_import(p, contents, node.sources()))
    finally:
        contents.discard()


def decrypted(path: Path) -> bytes:
    """The export's tar, as `age -d file | gunzip` gives it."""
    with path.open("rb") as raw:
        return gzip.decompress(age.Decryptor(raw, PASSPHRASE).read())


def opened(node: Node) -> dict[str, dict]:
    rows = asyncio.run(node.vault._call(lambda m: node.vault.store.open_all(m, ("username", "secret", "totp"))))
    return {meta.name: {"lanes": meta.lanes, "kind": meta.kind.value, "domain": meta.domain, "config": meta.config,
                        **{k: (v.reveal() if v else None) for k, v in values.items()}} for meta, values in rows}


# ---- the round trip ------------------------------------------------------------------------

def test_round_trip_moves_the_workspace_and_nothing_else(nodes, tmp_path, monkeypatch):
    a, b = nodes
    monkeypatch.setattr(wsexport, "SMALL_FILE", 1024)  # files above it are streamed, not read whole
    out = tmp_path / "acme.openajar"
    result = export(a, out)
    assert out.stat().st_mode & 0o777 == 0o600 and not list(tmp_path.glob(".partial-*"))
    assert result.counts["lanes"] == 2 and result.counts["vault_entries"] == 3 and result.counts["grants"] == 1
    assert result.counts["flows"] == 2 and result.counts["flow_versions"] == 3 and result.counts["schedules"] == 4
    assert [lane["copied"] for lane in result.lanes] == ["cold", "cold"]
    assert out.read_bytes().startswith(b"age-encryption.org/v1\n-> scrypt ")

    p, r = do_import(b, out)
    assert r.lanes == ["acme/instagram", "acme/reddit"]
    assert [i.spec.id for i in b.lanes.list()] == ["acme/instagram", "acme/reddit"]
    for lane in r.lanes:
        spec_a, spec_b = a.lanes.get(lane).spec, b.lanes.get(lane).spec
        assert spec_b.model_dump() == spec_a.model_dump()
        assert b.lanes.get(lane).state.value == "cold"
    # profile bytes, minus caches and lock files; the fingerprint exactly
    src_profile = tree(a.cfg.profile_dir("acme/instagram"))
    want = {k: v for k, v in src_profile.items() if not k.startswith("cache2/")}
    assert tree(b.cfg.profile_dir("acme/instagram")) == want
    assert not (b.cfg.profile_dir("acme/instagram") / "lock").exists()
    # import_profile writes the pinned values again (same values, its own layout)
    assert json.loads(b.cfg.fingerprint_path("acme/instagram").read_text()) == json.loads(
        a.cfg.fingerprint_path("acme/instagram").read_text())
    assert not b.cfg.fingerprint_path("acme/reddit").exists()
    for p_ in b.cfg.lane_dir("acme/instagram").rglob("*"):
        assert p_.stat().st_mode & 0o077 == 0, p_
    # the workspace record and readiness
    assert b.workspaces.get_workspace("acme")["name"] == "Acme Coffee"
    assert b.workspaces.get_workspace("acme")["default_proxy"] == "lt1"
    row = b.workspaces.lane_row("acme/instagram")
    assert (row["readiness"], row["note"], row["login_url"], row["updated_by"]) == (
        "ready", "logged in by hand", "https://instagram.com/accounts/login/", "owner")
    # the vault: the same values, re-sealed under node b's key, only bindings inside acme
    va, vb = opened(a), opened(b)
    assert set(vb) == {"acme ig", "acme api", "shared login"}
    for name in ("acme ig", "acme api"):
        assert vb[name] == va[name]
    assert vb["acme ig"]["secret"] == IG_PASSWORD and vb["acme ig"]["username"] == IG_USER
    assert SEED in vb["acme ig"]["totp"]
    assert vb["shared login"]["lanes"] == ["acme/reddit"] and vb["shared login"]["secret"] == SHARED_PASSWORD
    # flows (every version, the same head) and schedules (off, new webhook secret)
    assert [v.version for v in b.flows.versions("acme.instagram.post")] == [
        v.version for v in a.flows.versions("acme.instagram.post")]
    assert b.flows.get("acme.instagram.post").version == a.flows.get("acme.instagram.post").version
    assert b.flows.get("acme.instagram.post").owner_saved
    assert b.flows.get("all.reddit.read").definition.lane == "*/reddit"
    assert b.flows.store.get_flow("other.x.read") is None
    scheds = {s.id: s for s in b.scheduler.list()}
    assert set(scheds) == {a.cron.id, a.hook.id, a.shared_sched.id, a.agent_sched.id}
    assert not any(s.enabled for s in scheds.values())
    hook = scheds[a.hook.id].webhook
    assert hook.token and hook.token != a.hook.webhook.token and hook.secret != a.hook_extras["secret"]
    assert r.webhooks[0]["secret"] == hook.secret
    # grants: definitions only, not re-issued unless asked
    assert r.grants == [] and b.policy.list_grants() == []
    assert any("not re-issued" in n for n in p.notes)
    assert any("ONE DEVICE, TWO PLACES" in w and "node-a" in w for w in p.warnings)
    # the source is untouched
    assert [i.spec.id for i in a.lanes.list()] == ["acme/instagram", "acme/reddit", "other/x"]
    assert a.cfg.profile_dir("acme/instagram").exists() and len(opened(a)) == 4


def test_what_is_never_exported(nodes, tmp_path):
    a, _ = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    plain = decrypted(out)
    with tarfile.open(fileobj=io.BytesIO(plain)) as tar:
        names = tar.getnames()
        proxies = json.loads(tar.extractfile("workspace/proxies.json").read())
    assert not any("cache2" in n or n.endswith("/lock") for n in names)
    for secret in (OTHER_PASSWORD, PROXY_PASSWORD, a.grant_token, a.hook.webhook.token, a.hook_extras["secret"],
                   a.cfg.mcp_token):
        assert secret.encode() not in plain
    for secret in (IG_PASSWORD, API_KEY, SHARED_PASSWORD):
        assert secret.encode() in plain  # inside the encrypted stream only
    assert proxies == [{"label": "lt1", "endpoint": proxies[0]["endpoint"], "on_source": True}]
    assert b"192.0.2.10" not in plain


def test_with_proxies_carries_the_credentials_encrypted(nodes, tmp_path):
    a, _ = nodes
    out = tmp_path / "acme.openajar"
    export(a, out, with_proxies=True)
    plain = decrypted(out)
    assert PROXY_PASSWORD.encode() in plain and PROXY_PASSWORD.encode() not in out.read_bytes()
    c = Node(tmp_path, "node-c", proxies={})
    try:
        p, r = do_import(c, out)
        assert r.proxies_added == ["lt1"] and [row["action"] for row in p.proxies] == ["create"]
        from openajar.core.config import load_proxies

        assert load_proxies(c.cfg)["lt1"].password == PROXY_PASSWORD
    finally:
        c.close()


def test_age_recipient_round_trip(nodes, tmp_path):
    a, b = nodes
    identity, recipient = age.generate_identity()
    out = tmp_path / "acme.openajar"
    export(a, out, recipients=[recipient])
    assert b"-> X25519 " in out.read_bytes()[:200]
    with pytest.raises(BadExport, match="age key"):
        read(b, out)
    contents = read(b, out, identities=[age.parse_identity(identity)])
    try:
        assert contents.lane_names == ["instagram", "reddit"]
    finally:
        contents.discard()
    other, _ = age.generate_identity()
    with pytest.raises(BadExport, match="none of the given"):
        read(b, out, identities=[age.parse_identity(other)])
    assert not list((b.home / "tmp").iterdir())  # failed reads leave no staging behind


# ---- renaming and collisions -----------------------------------------------------------

def test_on_the_page_a_clash_asks_for_another_name_and_never_names_the_flag(nodes, tmp_path):
    """The form on this computer has its own name box, so its clash message says so, and the CLI keeps --as."""
    a, _ = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    contents = read(a, out)
    try:
        p = plan(a, contents, on_page=True)
        assert not p.ok and "Type another name for it" in p.blockers[0]
        assert "--as" not in " ".join(p.blockers) and "already exist on this node" in p.blockers[0]
        cli = plan(a, contents)
        assert "--as NEW" in cli.blockers[0]
    finally:
        contents.discard()


def test_importing_over_itself_is_refused_without_as(nodes, tmp_path):
    a, _ = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    contents = read(a, out)
    try:
        p = plan(a, contents)
        assert not p.ok and "already exist on this node" in p.blockers[0] and "--as NEW" in p.blockers[0]
        with pytest.raises(ExportRefused, match="blockers"):
            asyncio.run(wsimport.apply_import(p, contents, a.sources()))
    finally:
        contents.discard()
    assert [i.spec.id for i in a.lanes.list()] == ["acme/instagram", "acme/reddit", "other/x"]


def test_as_renames_lanes_flows_bindings_and_schedules(nodes, tmp_path):
    a, _ = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    contents = read(a, out)
    try:
        p = plan(a, contents, rename_to="acme2", reissue_grants=True)
        assert not p.ok and any("vault entries named" in b and "--on-vault-conflict" in b for b in p.blockers)
        assert any("all.reddit.read" in b for b in p.blockers) is False  # identical versions: already here
        p = plan(a, contents, rename_to="acme2", reissue_grants=True, vault_conflict="rename")
        assert p.ok, p.blockers
        assert p.warnings[0].startswith("SAME DEVICE TWICE")
        r = asyncio.run(wsimport.apply_import(p, contents, a.sources()))
    finally:
        contents.discard()
    assert r.lanes == ["acme2/instagram", "acme2/reddit"]
    assert tree(a.cfg.profile_dir("acme2/instagram")) == {
        k: v for k, v in tree(a.cfg.profile_dir("acme/instagram")).items() if not k.startswith("cache2/")}
    renamed = a.flows.get("acme2.instagram.post")
    assert renamed.definition.lane == "acme2/instagram" and len(a.flows.versions("acme2.instagram.post")) == 2
    assert a.flows.get("acme.instagram.post").definition.lane == "acme/instagram"  # the original is untouched
    v = opened(a)
    assert v["acme ig (2)"]["lanes"] == ["acme2/instagram"] and v["acme ig (2)"]["secret"] == IG_PASSWORD
    assert v["acme api (2)"]["lanes"] == ["acme2/*"] and v["acme ig"]["lanes"] == ["acme/instagram"]
    moved = [s for s in a.scheduler.list() if s.lane and s.lane.startswith("acme2/")]
    assert len(moved) == 4 and {s.flow_id for s in moved} == {"acme2.instagram.post", "all.reddit.read"}
    assert all(s.id not in {a.cron.id, a.hook.id} for s in moved)  # ids taken here: fresh ones
    assert a.workspaces.get_workspace("acme2")["name"] == "Acme Coffee"
    assert a.workspaces.lane_row("acme2/instagram")["readiness"] == "ready"
    grant = a.policy.grant_record(r.grants[0]["id"])
    assert grant["scope"]["blocks"][0]["workspaces"] == ["acme2"]


def test_reissued_grants_get_new_tokens_and_their_schedules(nodes, tmp_path):
    a, b = nodes
    out = tmp_path / "acme.openajar"
    exported = export(a, out)
    assert any("1 narrowed grant" in n for n in exported.notes)
    _, r = do_import(b, out, reissue_grants=True, enable_schedules=True)
    assert [(g["from"], g["subject"]) for g in r.grants] == [(a.grant.id, "acme-agent")]
    token = r.grants[0]["token"]
    assert token != a.grant_token
    principal, grant = b.policy.resolve(token)
    assert principal.id == "acme-agent" and grant.ceiling.value == "draft" and grant.lane_allowed("acme/instagram")
    assert not grant.lane_allowed("other/x")
    with pytest.raises(Exception, match="not valid|not issued"):  # the old token means nothing here
        b.policy.resolve(a.grant_token)
    scheds = {s.id: s for s in b.scheduler.list()}
    agent = scheds[a.agent_sched.id]
    assert agent.grant_id == r.grants[0]["id"] and agent.principal["grant_id"] == r.grants[0]["id"]
    assert agent.enabled and scheds[a.cron.id].enabled
    assert len(b.policy.list_grants()) == 1  # the narrowed child and the other workspaces' grants stayed


def test_a_schedule_whose_grant_is_not_reissued_stays_off(nodes, tmp_path):
    a, b = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    p, _ = do_import(b, out, enable_schedules=True)
    scheds = {s.id: s for s in b.scheduler.list()}
    assert not scheds[a.agent_sched.id].enabled and scheds[a.cron.id].enabled
    assert any(a.agent_sched.id in n and "--reissue-grants" in n for n in p.notes)


# ---- proxies -----------------------------------------------------------------------------

def test_a_lane_never_comes_in_without_its_proxy(nodes, tmp_path):
    a, _ = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    c = Node(tmp_path, "node-c", proxies={"de1": ProxyConfig(label="de1", host="198.51.100.4", port=8000)})
    try:
        contents = read(c, out)
        try:
            p = plan(c, contents)
            assert not p.ok and "never falls back to this node's own IP" in p.blockers[0]
            mapped = plan(c, contents, proxy_map={"lt1": "de1"})
            assert mapped.ok and {lane["proxy"] for lane in mapped.lanes} == {"de1"}
            bad = plan(c, contents, proxy_map={"lt1": "nope"})
            assert any("no proxy 'nope'" in b for b in bad.blockers)
            loose = plan(c, contents, allow_missing_proxy=True)
            assert loose.ok and any("fail closed" in w for w in loose.warnings)
            r = asyncio.run(wsimport.apply_import(loose, contents, c.sources()))
        finally:
            contents.discard()
        from openajar.core.config import load_proxies

        placeholder = load_proxies(c.cfg)["lt1"]
        assert placeholder.host.endswith(".invalid") and r.proxies_added == ["lt1"]
        assert c.lanes.get("acme/instagram").spec.proxy_label == "lt1"
    finally:
        c.close()


def test_a_label_that_points_elsewhere_is_a_warning(nodes, tmp_path):
    a, _ = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    c = Node(tmp_path, "node-c", proxies={"lt1": ProxyConfig(label="lt1", host="198.51.100.9", port=1000)})
    try:
        contents = read(c, out)
        try:
            p = plan(c, contents)
            assert p.ok and p.proxies[0]["action"] == "differs"
            assert any("new IP" in w for w in p.warnings)
        finally:
            contents.discard()
    finally:
        c.close()


# ---- the vault on the target ---------------------------------------------------------------

def test_a_sealed_target_vault_is_refused(nodes, tmp_path):
    a, _ = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    c = Node(tmp_path, "node-c", vault_mode="passphrase")
    try:
        contents = read(c, out)
        try:
            p = plan(c, contents)
            assert not p.ok and any("sealed" in b and "openajar vault unseal" in b for b in p.blockers)
            asyncio.run(c.vault.unseal(c.vault_passphrase))
            assert plan(c, contents).ok
            c.vault.seal()  # sealed by the owner: refused again
            assert not plan(c, contents).ok
        finally:
            contents.discard()
    finally:
        c.close()


def test_no_vault_on_the_target_is_refused(nodes, tmp_path):
    a, _ = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    c = Node(tmp_path, "node-c", vault_mode=None)
    try:
        contents = read(c, out)
        try:
            p = plan(c, contents)
            assert any("openajar vault init" in b for b in p.blockers)
        finally:
            contents.discard()
    finally:
        c.close()


def test_a_sealed_source_vault_refuses_the_export(tmp_path):
    a = Node(tmp_path, "node-a", vault_mode="passphrase")
    try:
        asyncio.run(a.vault.unseal(a.vault_passphrase))
        a.build_acme()
        a.vault.seal()
        with pytest.raises(ExportRefused, match="vault is sealed"):
            export(a, tmp_path / "acme.openajar")
        assert not list(tmp_path.glob("*.openajar")) and not list(tmp_path.glob(".partial-*"))
    finally:
        a.close()


# ---- bad files -------------------------------------------------------------------------------

@pytest.fixture
def exported(nodes, tmp_path):
    a, b = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    return b, out


def _expect_bad(node: Node, path: Path, match: str, **kw) -> None:
    with pytest.raises(BadExport, match=match):
        read(node, path, **kw)
    assert not list((node.home / "tmp").iterdir())
    assert node.lanes.list() == []


def test_wrong_passphrase(exported):
    b, out = exported
    _expect_bad(b, out, "wrong passphrase", passphrase="not the export passphrase")


def test_tampered_bytes(exported, tmp_path):
    b, out = exported
    blob = bytearray(out.read_bytes())
    blob[len(blob) // 2] ^= 0x40
    bad = tmp_path / "tampered.openajar"
    bad.write_bytes(bytes(blob))
    _expect_bad(b, bad, "damaged or altered")


def test_partial_file(exported, tmp_path):
    b, out = exported
    blob = out.read_bytes()
    for cut in (len(blob) // 3, len(blob) - 100, 300):
        bad = tmp_path / f"partial-{cut}.openajar"
        bad.write_bytes(blob[:cut])
        _expect_bad(b, bad, "truncated|damaged|incomplete")


def test_not_an_export(exported, tmp_path):
    b, _ = exported
    junk = tmp_path / "junk.openajar"
    junk.write_bytes(b"PK\x03\x04 a zip file")
    _expect_bad(b, junk, "not an encrypted openajar export")
    backup_like = tmp_path / "other.openajar"
    raw = io.BytesIO()
    enc = age.Encryptor(raw, PASSPHRASE, log_n=LOG_N)
    with tarfile.open(fileobj=enc, mode="w|gz") as tar:
        info = tarfile.TarInfo("agentos-backup/format.json")
        data = b'{"format": "agentos-backup"}'
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    enc.close()
    backup_like.write_bytes(raw.getvalue())
    _expect_bad(b, backup_like, "not an openajar workspace export")


def _rewrite(out: Path, dest: Path, change) -> None:
    """Decrypt, change the tar members, encrypt again with the same passphrase (an attacker who
    knows the passphrase, or a bug): the manifest checks must still catch it."""
    raw = io.BytesIO()
    enc = age.Encryptor(raw, PASSPHRASE, log_n=LOG_N)
    with tarfile.open(fileobj=io.BytesIO(decrypted(out))) as src, tarfile.open(fileobj=enc, mode="w|gz") as tar:
        for member in src.getmembers():
            data = src.extractfile(member).read() if member.isreg() else None
            for m, d in change(member, data):
                if d is not None:
                    m.size = len(d)
                tar.addfile(m, io.BytesIO(d) if d is not None else None)
    enc.close()
    dest.write_bytes(raw.getvalue())


def test_a_changed_profile_file_fails_its_checksum(exported, tmp_path):
    b, out = exported
    bad = tmp_path / "changed.openajar"

    def change(member, data):
        if member.name.endswith("instagram/profile/prefs.js"):
            data = b'user_pref("network.proxy.type", 0);\n'
        yield member, data

    _rewrite(out, bad, change)
    _expect_bad(b, bad, "checksum mismatch")


def test_an_added_or_removed_member_is_caught(exported, tmp_path):
    b, out = exported
    added = tmp_path / "added.openajar"

    def add(member, data):
        yield member, data
        if member.name == "workspace/lanes/instagram/profile/prefs.js":
            extra = tarfile.TarInfo("workspace/lanes/instagram/profile/user.js")
            yield extra, b'user_pref("x", 1);'

    _rewrite(out, added, add)
    _expect_bad(b, added, "does not match")
    removed = tmp_path / "removed.openajar"

    def drop(member, data):
        if not member.name.endswith("vault.json"):
            yield member, data

    _rewrite(out, removed, drop)
    _expect_bad(b, removed, "does not match")
    escape = tmp_path / "escape.openajar"

    def traverse(member, data):
        yield member, data
        if member.name == "workspace/workspace.json":
            yield tarfile.TarInfo("workspace/../../evil"), b"x"

    _rewrite(out, escape, traverse)
    _expect_bad(b, escape, "unsafe path")


# ---- consistency: warm lanes -------------------------------------------------------------

def test_a_warm_lane_is_paused_and_warmed_again(nodes, tmp_path):
    a, _ = nodes
    a.coordinator.start("acme/instagram")
    result = export(a, tmp_path / "acme.openajar")
    assert {lane["id"]: lane["copied"] for lane in result.lanes} == {"acme/instagram": "paused", "acme/reddit": "cold"}
    assert a.coordinator.calls == [("close", "acme/instagram"), ("open", "acme/instagram")]
    assert result.lanes[0]["was_warm"] is True


def test_a_warm_lane_with_no_daemon_is_refused(nodes, tmp_path):
    a, _ = nodes
    a.coordinator.start("acme/reddit")
    a.coordinator.up = False
    with pytest.raises(ExportRefused, match="no daemon answers"):
        export(a, tmp_path / "acme.openajar")
    assert not list(tmp_path.glob("*.openajar")) and not list(tmp_path.glob(".partial-*"))


def test_a_browser_that_starts_during_the_copy_fails_the_export(nodes, tmp_path):
    a, _ = nodes

    def agent_warms_it(rel: str) -> None:
        if "reddit/profile" in rel:
            a.host.browsers[str(a.cfg.profile_dir("acme/reddit"))] = [777]

    with pytest.raises(ExportRefused, match="opened again while it was being copied"):
        export(a, tmp_path / "acme.openajar", fail_after=agent_warms_it)
    assert not list(tmp_path.glob("*.openajar")) and not list(tmp_path.glob(".partial-*"))


def test_an_interrupted_export_leaves_no_file(nodes, tmp_path):
    a, _ = nodes

    def boom(rel: str) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        export(a, tmp_path / "acme.openajar", fail_after=boom)
    assert list(tmp_path.glob("*acme*")) == []


def test_an_existing_file_is_never_overwritten(nodes, tmp_path):
    a, _ = nodes
    out = tmp_path / "acme.openajar"
    out.write_text("keep me")
    with pytest.raises(ExportRefused, match="already exists"):
        export(a, out)
    assert out.read_text() == "keep me"


def test_terminal_lanes_and_unknown_workspaces(nodes, tmp_path):
    a, _ = nodes
    a.add_lane("acme/shell", proxy=None, pinned=False, kind=LaneKind.TERMINAL)
    result = export(a, tmp_path / "acme.openajar")
    assert result.not_exported == [{"lane": "acme/shell", "why": "terminal lanes are not exported yet"}]
    assert result.counts["lanes"] == 2
    with pytest.raises(Exception, match="no workspace 'ghost'"):
        export(a, tmp_path / "ghost.openajar", workspace="ghost")
    with pytest.raises(Exception, match="at least 12"):
        export(a, tmp_path / "short.openajar", passphrase="short")


# ---- an import is all or nothing ------------------------------------------------------------

def test_a_failed_import_is_rolled_back(nodes, tmp_path, monkeypatch):
    a, _ = nodes
    out = tmp_path / "acme.openajar"
    export(a, out, with_proxies=True)
    c = Node(tmp_path, "node-c", proxies={})
    before_proxies = (c.cfg.proxies_path).read_bytes()
    try:
        contents = read(c, out)
        try:
            p = plan(c, contents, reissue_grants=True)
            assert p.ok, p.blockers
            real = c.scheduler.adopt

            def adopt(s, *, enabled):
                if s.id == a.agent_sched.id:
                    raise RuntimeError("disk full")
                return real(s, enabled=enabled)

            monkeypatch.setattr(c.scheduler, "adopt", adopt)
            with pytest.raises(wsimport.ImportFailed, match="removed again"):
                asyncio.run(wsimport.apply_import(p, contents, c.sources()))
        finally:
            contents.discard()
        assert c.lanes.list() == [] and not (c.home / "lanes" / "acme").exists()
        assert c.workspaces.get_workspace("acme") is None and c.workspaces.lane_rows() == {}
        assert asyncio.run(c.vault.list_entries()) == []
        assert c.flows.list() == [] and c.scheduler.list() == []
        assert all(g["state"] == "revoked" for g in c.policy.list_grants(include_inactive=True))
        assert c.cfg.proxies_path.read_bytes() == before_proxies
        assert not list((c.home / "tmp").iterdir())
    finally:
        c.close()


# ---- no plaintext secret ever touches the disk ----------------------------------------------

def test_no_plaintext_secret_touches_the_disk(nodes, tmp_path, monkeypatch):
    a, b = nodes
    scratch = tmp_path / "systmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    out = tmp_path / "acme.openajar"
    seen_partial: list[list[str]] = []

    def scan_midway(rel: str) -> None:  # while the export is being written
        seen_partial.append(files_containing(tmp_path, MARKERS))

    export(a, out, fail_after=scan_midway)
    assert seen_partial and all(hits == [] for hits in seen_partial)
    contents = read(b, out)
    try:
        assert files_containing(tmp_path, MARKERS) == []  # staging holds profiles only, never vault.json
        p = plan(b, contents, reissue_grants=True)
        shown = json.dumps(p.to_dict())
        assert not any(m in shown for m in MARKERS)  # the plan never carries a value
        r = asyncio.run(wsimport.apply_import(p, contents, b.sources()))
        assert files_containing(tmp_path, MARKERS) == []
    finally:
        contents.discard()
    assert files_containing(tmp_path, (r.grants[0]["token"],)) == []
    assert opened(b)["acme ig"]["secret"] == IG_PASSWORD  # and yet it all arrived


def test_lanes_on_a_device_come_in_on_that_device(nodes, tmp_path):
    """AUD-02: a company's lanes that share a device are one computer again after the import,
    the computer their sites know (the device's pin), never a new draw per lane."""
    a, b = nodes
    asyncio.run(a.lanes.create_device("acme-pc", from_lane="acme/instagram", confirm=True))
    asyncio.run(a.lanes.set_device("acme/reddit", "acme-pc"))
    pin = a.cfg.device_pin_path("acme-pc").read_bytes()
    out = tmp_path / "acme.openajar"
    export(a, out)
    p, r = do_import(b, out)
    assert [d["name"] for d in p.devices] == ["acme-pc"] and p.devices[0]["from_lane"] == "acme/instagram"
    assert r.lanes == ["acme/instagram", "acme/reddit"]
    assert {i.spec.id: i.spec.device for i in b.lanes.list()} == {"acme/instagram": "acme-pc", "acme/reddit": "acme-pc"}
    assert json.loads(b.cfg.device_pin_path("acme-pc").read_text()) == json.loads(pin)
    assert b.lanes.store.device("acme-pc").workspace == "acme"
    assert b.lanes.device_pin_state("acme-pc").pinned
    # the same device already on the target: refused, nothing changed
    contents = read(b, out)
    try:
        again = plan(b, contents, rename_to="acme2")
        assert any("device acme-pc already exists" in x for x in again.blockers)
    finally:
        contents.discard()


def test_a_failed_import_removes_the_device_it_made(nodes, tmp_path, monkeypatch):
    a, b = nodes
    asyncio.run(a.lanes.create_device("acme-pc", from_lane="acme/instagram", confirm=True))
    asyncio.run(a.lanes.set_device("acme/reddit", "acme-pc"))
    out = tmp_path / "acme.openajar"
    export(a, out)
    contents = read(b, out)
    try:
        p = plan(b, contents)
        assert p.ok, p.blockers

        def broken(*args, **kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr(wsimport, "_apply_workspace", broken)
        with pytest.raises(wsimport.ImportFailed, match="removed again"):
            asyncio.run(wsimport.apply_import(p, contents, b.sources()))
    finally:
        contents.discard()
    assert b.lanes.list() == [] and b.lanes.store.devices() == []
    assert not b.cfg.device_dir("acme-pc").exists()


@pytest.mark.parametrize(("typed", "made"), [("Harbor Copy", "harbor-copy"), ("  Biz A!! ", "biz-a"),
                                             ("Šiauliai café", "siauliai-cafe"), ("!!!", "workspace"), ("x" * 40, "x" * 31)])
def test_a_typed_name_makes_a_valid_id(typed, made):
    from openajar.workspaces.models import check_workspace_id, workspace_id_from

    assert workspace_id_from(typed) == made and check_workspace_id(made) == made


def test_a_name_typed_on_the_page_is_the_display_name_and_its_id_is_made_from_it(nodes, tmp_path):
    a, _ = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    contents = read(a, out)
    try:
        p = plan(a, contents, display_name="Acme Copy", on_page=True, reissue_grants=True, vault_conflict="rename")
        assert p.ok, p.blockers
        assert p.workspace_to == "acme-copy"
        r = asyncio.run(wsimport.apply_import(p, contents, a.sources()))
        # the id is one nothing here has: a second copy named the same gets a suffix, never a clash
        again = plan(a, contents, display_name="Acme Copy", on_page=True, reissue_grants=True, vault_conflict="rename")
        assert again.workspace_to == "acme-copy-2" and again.ok, again.blockers
    finally:
        contents.discard()
    assert r.workspace == "acme-copy"
    assert a.workspaces.get_workspace("acme-copy")["name"] == "Acme Copy"
    assert a.workspaces.get_workspace("acme")["name"] == "Acme Coffee"  # the original keeps its own name
    assert [i.spec.id for i in a.lanes.list() if i.spec.id.startswith("acme-copy/")] == ["acme-copy/instagram", "acme-copy/reddit"]
