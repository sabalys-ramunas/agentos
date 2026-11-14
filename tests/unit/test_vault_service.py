"""VaultService: sealing, concurrency, key changes under a running daemon, resolution,
traces, redaction and the control socket."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest
from test_vault_support import PASSWORD, SEED, USERNAME, FakeCtx, fld, login_form, make_vault, private_home

from agentos.core.config import NodeConfig
from agentos.core.runtime import Runtime
from agentos.vault import control, crypto
from agentos.vault.errors import (
    ResolutionDenied,
    VaultBusy,
    VaultKeyInsecure,
    VaultLockedOut,
    VaultNotInitialized,
    VaultSealed,
    WrongPassphrase,
)
from agentos.vault.policy import Sink
from agentos.vault.service import VaultService, build, keyfile_path, pending_keyfile_path
from agentos.vault.store import VaultStore

PHRASE = "correct horse battery staple"
TOP = "https://www.instagram.com/accounts/login/"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def home(tmp_path):
    return private_home(tmp_path)


async def add_ig(v: VaultService, **kw):
    base = {"name": "ig", "kind": "password", "domain": "instagram.com", "lanes": ["biz-a/instagram"],
                "username": USERNAME, "secret": PASSWORD}
    base.update(kw)
    return await v.add_entry(**base)


def no_secret_in(*objs):
    text = json.dumps(objs, default=str)
    for s in (PASSWORD, USERNAME, SEED, PHRASE):
        assert s not in text


# ---- sealing -------------------------------------------------------------------

async def test_uninitialized_vault(home):
    v = VaultService(home)
    assert v.state() == "uninitialized" and v.status() == {"initialized": False, "state": "uninitialized"}
    with pytest.raises(VaultNotInitialized):
        await v.unseal("x")
    with pytest.raises(VaultNotInitialized):
        await add_ig(v)


async def test_keyfile_vault_unseals_itself(home):
    v = make_vault(home)
    assert v.state() == "sealed"
    await add_ig(v)                                   # lazy unseal from the key file
    assert v.state() == "unsealed"
    await v.start(control=False)
    assert v.status()["entries"] == 1


async def test_owner_sealed_keyfile_vault_stays_sealed(home):
    v = make_vault(home)
    await v.start(control=False)
    v.seal()
    with pytest.raises(VaultSealed) as exc:
        await add_ig(v)
    assert "agentos vault unseal" in exc.value.message
    assert (await v.list_entries()) == []             # metadata listing works while sealed
    await v.unseal()
    await add_ig(v)


async def test_insecure_key_file_keeps_the_vault_sealed_with_a_clear_error(home):
    v = make_vault(home)
    os.chmod(keyfile_path(home), 0o644)
    await v.start(control=False)                      # never crashes the daemon
    st = v.status()
    assert st["state"] == "sealed" and "vault_key_insecure" in st["last_error"]
    with pytest.raises(VaultKeyInsecure):
        await add_ig(v)


async def test_passphrase_vault_starts_sealed(home):
    v = make_vault(home, mode="passphrase", passphrase=PHRASE)
    await v.start(control=False)
    assert v.state() == "sealed"
    with pytest.raises(VaultSealed):
        await add_ig(v)
    with pytest.raises(WrongPassphrase):
        await v.unseal("wrong passphrase!!")
    st = await v.unseal(PHRASE)
    assert st["state"] == "unsealed"
    await add_ig(v)
    v.seal()
    assert v.state() == "sealed"
    with pytest.raises(VaultSealed):
        await v._open("ig", ("secret",))
    # a new process (restart) starts sealed again
    assert VaultService(home).state() == "sealed"


async def test_concurrent_unseal(home):
    v = make_vault(home, mode="passphrase", passphrase=PHRASE)
    results = await asyncio.gather(*[v.unseal(PHRASE) for _ in range(5)], v.unseal("nope nope nope"),
                                   return_exceptions=True)
    assert all(isinstance(r, dict) and r["state"] == "unsealed" for r in results[:5])
    assert isinstance(results[5], WrongPassphrase)
    assert v.state() == "unsealed"                    # a wrong guess never seals a working vault


async def test_wrong_passphrase_lockout(home):
    clock = Clock()
    v = make_vault(home, mode="passphrase", passphrase=PHRASE, clock=clock)
    for _ in range(5):
        with pytest.raises(WrongPassphrase):
            await v.unseal("guess guess guess")
    with pytest.raises(VaultLockedOut):
        await v.unseal(PHRASE)
    assert v.status()["locked_out_seconds"] > 0
    clock.t += 31
    assert (await v.unseal(PHRASE))["state"] == "unsealed"


async def test_passphrase_changed_elsewhere_seals_the_daemon(home):
    v = make_vault(home, mode="passphrase", passphrase=PHRASE)
    await v.unseal(PHRASE)
    await add_ig(v)
    store = VaultStore(home / "vault.db")
    old = crypto.derive_master(PHRASE, store.header().kdf)
    kdf = crypto.new_kdf({"alg": "argon2id", "m": 64, "t": 1, "p": 1})
    store.rewrap(old, crypto.derive_master("brand new passphrase", kdf), mode="passphrase", kdf=kdf)
    with pytest.raises(VaultSealed) as exc:
        await v._open("ig", ("secret",))
    assert "rotated" in exc.value.message
    assert v.state() == "sealed"
    await v.unseal("brand new passphrase")
    assert (await v._open("ig", ("secret",)))[1]["secret"].reveal() == PASSWORD


async def test_data_key_rotated_elsewhere_is_transparent(home):
    v = make_vault(home, mode="passphrase", passphrase=PHRASE)
    await v.unseal(PHRASE)
    await add_ig(v)
    store = VaultStore(home / "vault.db")
    store.rotate_data_key(crypto.derive_master(PHRASE, store.header().kdf))
    assert (await v._open("ig", ("secret",)))[1]["secret"].reveal() == PASSWORD


async def test_key_file_rotated_elsewhere_is_reloaded(home):
    v = make_vault(home)
    await add_ig(v)
    store = VaultStore(home / "vault.db")
    old = crypto.read_keyfile(keyfile_path(home))
    new_key = os.urandom(32)
    crypto.write_keyfile(pending_keyfile_path(keyfile_path(home)), new_key)
    store.rewrap(old, new_key, mode="keyfile", kdf=None)
    os.replace(pending_keyfile_path(keyfile_path(home)), keyfile_path(home))
    assert (await v._open("ig", ("secret",)))[1]["secret"].reveal() == PASSWORD


def test_build_registers_under_vault(home):
    rt = Runtime(config=NodeConfig(home=home))
    svc = build(rt)
    assert rt.vault is svc and svc.home == home
    assert PASSWORD not in repr(svc)


# ---- resolution ----------------------------------------------------------------

async def test_resolve_field_traces_use_without_secrets(home):
    v = make_vault(home)
    meta = await add_ig(v)
    ctx = FakeCtx()
    fields = login_form()
    pw = await v.resolve_field(ctx, ref="ig", lane_id="biz-a/instagram", url=TOP, field=fields[2],
                               fields=fields, sink=Sink.FORM_PASSWORD)
    user = await v.resolve_field(ctx, ref="ig", lane_id="biz-a/instagram", url=TOP, field=fields[1],
                                 fields=fields, sink=Sink.FORM_USERNAME)
    assert pw.reveal() == PASSWORD and user.reveal() == USERNAME
    assert ctx.kinds() == ["vault.use", "vault.use"]
    _kind, fields_ = ctx.traces[0]
    assert fields_["entry"] == meta.id and fields_["lane"] == "biz-a/instagram"
    assert fields_["host"] == "www.instagram.com" and fields_["sink"] == "form_password"
    no_secret_in(ctx.traces)
    got = await v.get_entry("ig")
    assert got.use_count == 2 and got.last_used is not None


async def test_resolution_denied_is_traced_and_raises(home):
    v = make_vault(home)
    await add_ig(v)
    ctx = FakeCtx()
    evil = login_form("https://instagram.com.evil.io")
    with pytest.raises(ResolutionDenied) as exc:
        await v.resolve_field(ctx, ref="ig", lane_id="biz-a/instagram", url="https://instagram.com.evil.io/",
                              field=evil[2], fields=evil, sink=Sink.FORM_PASSWORD)
    assert exc.value.reason == "domain_mismatch"
    assert ctx.kinds() == ["vault.deny"] and ctx.traces[0][1]["reason"] == "domain_mismatch"
    no_secret_in(ctx.traces, exc.value.message, str(exc.value), repr(exc.value))
    with pytest.raises(ResolutionDenied) as exc:
        await v.resolve_field(ctx, ref="ig", lane_id="biz-b/instagram", url=TOP, field=login_form()[2],
                              fields=login_form(), sink=Sink.FORM_PASSWORD)
    assert exc.value.reason == "lane_not_bound"
    assert (await v.get_entry("ig")).use_count == 0


async def test_totp_code_waits_for_a_fresh_window(home):
    clock = Clock(1_800_000_028.5)             # 1.5 s left in the 30 s window
    slept = []

    async def sleep(s):
        slept.append(s)
        clock.t += s

    v = make_vault(home, clock=clock, sleep=sleep)
    await add_ig(v, totp=SEED)
    ctx = FakeCtx()
    otp = [fld("otp", type="text", autocomplete="one-time-code", form="o")]
    code = await v.one_time_code(ctx, ref="ig", lane_id="biz-a/instagram", url=TOP, field=otp[0], fields=otp)
    assert slept and slept[0] > 1.5
    import pyotp
    assert code.reveal() == pyotp.TOTP(SEED).at(int(clock.t))
    assert ctx.traces[-1][1]["source"] == "totp"
    no_secret_in(ctx.traces)
    assert code.reveal() not in json.dumps(ctx.traces)


async def test_auth_header_for_api_keys(home):
    v = make_vault(home)
    await v.add_entry(name="api", kind="api_key", domain="example.com", lanes=["biz-a/*"], secret="sk-123456")
    ctx = FakeCtx()
    name, value = await v.resolve_auth_header(ctx, lane_id="biz-a/instagram", url="https://api.example.com/v1")
    assert name == "Authorization" and value.reveal() == "Bearer sk-123456"
    with pytest.raises(ResolutionDenied):
        await v.resolve_auth_header(ctx, lane_id="biz-a/instagram", url="https://api.example.com.evil.io/")
    with pytest.raises(ResolutionDenied):
        await v.resolve_auth_header(ctx, lane_id="biz-a/instagram", url="http://api.example.com/")
    assert "sk-123456" not in json.dumps(ctx.traces)
    await v.add_entry(name="api2", kind="api_key", domain="other.com", lanes=["biz-a/*"], secret="k-987654",
                      config={"header": "X-Api-Key"})
    name, value = await v.resolve_auth_header(ctx, lane_id="biz-a/instagram", url="https://other.com/")
    assert (name, value.reveal()) == ("X-Api-Key", "k-987654")


async def test_scrub_redacts_bound_secrets_and_recent_codes(home):
    v = make_vault(home)
    await add_ig(v, totp=SEED)
    await v.add_entry(name="other", kind="password", domain="reddit.com", lanes=["personal/reddit"],
                      secret="reddit-secret-pw")
    otp = [fld("otp", type="text", autocomplete="one-time-code", form="o")]
    code = await v.one_time_code(FakeCtx(), ref="ig", lane_id="biz-a/instagram", url=TOP, field=otp[0],
                                 fields=otp)
    text = f"value={PASSWORD} code {code.reveal()} reddit-secret-pw"
    out = v.scrub(text, "biz-a/instagram")
    assert PASSWORD not in out and code.reveal() not in out and "reddit-secret-pw" in out
    assert "reddit-secret-pw" not in v.scrub(text)
    v.seal()
    assert v.scrub("nothing here") == "nothing here"


# ---- control socket ------------------------------------------------------------

@pytest.fixture
def sock_path():
    d = Path(tempfile.mkdtemp(prefix="av", dir="/tmp"))
    yield d / "run" / "v.sock"
    for p in sorted(d.rglob("*"), reverse=True):
        p.unlink() if not p.is_dir() else p.rmdir()
    d.rmdir()


async def test_control_socket_unseal_seal_status(home, sock_path):
    v = make_vault(home, mode="passphrase", passphrase=PHRASE, socket_path=sock_path)
    await v.start()
    try:
        assert oct(sock_path.stat().st_mode)[-3:] == "600"
        call = lambda p: asyncio.to_thread(control.request, sock_path, p)
        assert (await call({"op": "status"}))["status"]["state"] == "sealed"
        bad = await call({"op": "unseal", "passphrase": "wrong wrong wrong"})
        assert not bad["ok"] and bad["error"]["code"] == "vault_wrong_passphrase"
        assert "wrong wrong wrong" not in json.dumps(bad)
        ok = await call({"op": "unseal", "passphrase": PHRASE})
        assert ok["ok"] and ok["status"]["state"] == "unsealed" and PHRASE not in json.dumps(ok)
        assert (await call({"op": "seal"}))["status"]["state"] == "sealed"
        assert (await call({"op": "nope"}))["error"]["code"] == "invalid"
        assert (await call({"op": "unseal", "passphrase": 5}))["error"]["code"] == "invalid"
        with pytest.raises(VaultBusy):
            await control.start_server(v, sock_path)       # a live daemon owns the socket
    finally:
        await v.stop()
    assert not sock_path.exists()


async def test_control_socket_replaces_a_stale_socket_file(home, sock_path):
    sock_path.parent.mkdir(mode=0o700, parents=True)
    sock_path.write_text("stale")
    v = make_vault(home, socket_path=sock_path)
    await v.start()
    try:
        resp = await asyncio.to_thread(control.request, sock_path, {"op": "status"})
        assert resp["status"]["state"] == "unsealed"
    finally:
        await v.stop()


async def test_control_socket_bad_json(home, sock_path):
    v = make_vault(home, socket_path=sock_path)
    await v.start()
    try:
        def raw():
            import socket
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(str(sock_path))
                s.sendall(b"{not json " + PHRASE.encode() + b"\n")
                return s.recv(4096)
        out = await asyncio.to_thread(raw)
        assert b"bad request" in out and PHRASE.encode() not in out
    finally:
        await v.stop()
