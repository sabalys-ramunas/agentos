"""`agentos vault ...` end to end with CliRunner: prompts are faked, output never holds a secret."""

import asyncio
import json
import os
import stat
import tempfile
import threading
from pathlib import Path

import pytest
from test_vault_support import FAST_KDF, PASSWORD, SEED, USERNAME, private_home
from typer.testing import CliRunner

from agentos.vault import cli
from agentos.vault.service import VaultService, keyfile_path
from agentos.vault.store import VaultStore

PHRASE = "a long enough passphrase"
SECRETS = (PASSWORD, USERNAME, SEED, PHRASE, "mail-pw-1")


@pytest.fixture
def sock():
    d = Path(tempfile.mkdtemp(prefix="avc", dir="/tmp"))   # short: unix socket paths max ~104 bytes
    yield d / "v.sock"
    for p in d.iterdir():
        p.unlink()
    d.rmdir()


@pytest.fixture
def home(tmp_path, monkeypatch, sock):
    h = private_home(tmp_path)
    monkeypatch.setenv("AGENTOS_HOME", str(h))
    monkeypatch.setattr(cli, "KDF_PARAMS", FAST_KDF)
    monkeypatch.setattr(cli, "_socket", lambda: sock)
    return h


class Prompts:
    def __init__(self, monkeypatch):
        self.answers: list[str] = []
        self.asked: list[str] = []
        monkeypatch.setattr(cli, "_getpass", self)

    def __call__(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0)


@pytest.fixture
def prompts(monkeypatch):
    return Prompts(monkeypatch)


def run(*args, input=None):
    result = CliRunner().invoke(cli.app, list(args), input=input)
    for s in SECRETS:
        assert s not in result.output, (s, result.output)
    return result


def ok(*args, input=None):
    result = run(*args, input=input)
    assert result.exit_code == 0, result.output
    return result.output


def fails(*args, input=None):
    result = run(*args, input=input)
    assert result.exit_code == 1, result.output
    return result.output


def add_ig(prompts, *extra):
    prompts.answers += [USERNAME, PASSWORD, PASSWORD]
    return ok("add", "ig", "--domain", "https://www.instagram.com/login", "--lane", "biz-a/instagram", *extra)


# ---- init ----------------------------------------------------------------------

def test_init_keyfile(home):
    out = ok("init")
    assert "key file" in out and "—" not in out
    assert stat.S_IMODE(os.stat(keyfile_path(home)).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(home / "vault.db").st_mode) == 0o600
    assert "already exists" in fails("init")


def test_init_refuses_to_overwrite_a_key_file(home):
    keyfile_path(home).parent.mkdir(mode=0o700)
    keyfile_path(home).write_text("old key\n")
    assert "move it away" in fails("init")
    assert keyfile_path(home).read_text() == "old key\n"


def test_init_passphrase(home, prompts):
    prompts.answers += ["short", ]
    assert "at least 12" in fails("init", "--passphrase")
    prompts.answers += [PHRASE, PHRASE + "x"]
    assert "do not match" in fails("init", "--passphrase")
    prompts.answers += [PHRASE, PHRASE]
    assert "starts sealed" in ok("init", "--passphrase")
    assert VaultStore(home / "vault.db").header().mode == "passphrase"


def test_commands_before_init(home):
    assert "agentos vault init" in fails("list")
    assert "initialized: False" in ok("status")


# ---- entries -------------------------------------------------------------------

def test_add_list_show_bind_unbind_remove(home, prompts):
    ok("init")
    out = add_ig(prompts)
    assert "Added 'ig' (password, instagram.com)" in out
    assert prompts.asked == ["Username (Enter for none): ", "Password: ", "Repeat password: "]
    listing = ok("list")
    assert "ig" in listing and "instagram.com" in listing and "biz-a/instagram" in listing
    data = json.loads(ok("list", "--json"))
    assert data[0]["name"] == "ig" and set(data[0]) == {"id", "name", "kind", "domain", "lanes", "created_at",
                                                        "last_used"}
    shown = ok("show", "ig")
    assert "has username: True" in shown and "has secret: True" in shown
    assert "biz-a/*" in ok("bind", "ig", "biz-a/*", "personal/instagram")
    out = ok("unbind", "ig", "biz-a/instagram")
    assert "biz-a/instagram," not in out and "personal/instagram" in out
    assert "lane binding" in fails("bind", "ig", "*")
    assert "Removed 'ig'" in ok("remove", "ig", "--yes")
    assert "No vault entries." in ok("list")
    assert "no vault entry named" in fails("show", "ig")


def test_add_reads_secrets_from_stdin_json(home):
    ok("init")
    payload = json.dumps({"username": USERNAME, "secret": PASSWORD, "totp": SEED})
    ok("add", "ig", "--domain", "instagram.com", "--lane", "biz-a/instagram", "--stdin", input=payload)
    v = VaultStore(home / "vault.db")
    key = cli.load_keyfile_master(v, keyfile_path(home))
    _, vals = v.open(key, "ig", ("username", "secret", "totp"))
    assert vals["username"].reveal() == USERNAME and vals["secret"].reveal() == PASSWORD
    assert "JSON object" in fails("add", "x", "--domain", "a.com", "--stdin", input="[1]")


def test_add_validation_errors(home, prompts):
    ok("init")
    prompts.answers += [USERNAME, PASSWORD, PASSWORD]
    assert "registrable domain" in fails("add", "x", "--domain", "co.uk")
    prompts.answers += [USERNAME, PASSWORD, "different"]
    assert "do not match" in fails("add", "x", "--domain", "a.com")
    add_ig(prompts)
    prompts.answers += [USERNAME, PASSWORD, PASSWORD]
    assert "already exists" in fails("add", "ig", "--domain", "instagram.com")


def test_api_key_and_totp_entries(home, prompts):
    ok("init")
    prompts.answers += ["sk-live-123456"]
    out = ok("add", "api", "--kind", "api_key", "--domain", "api.example.com", "--header", "X-Api-Key",
             "--prefix", "")
    assert "sk-live-123456" not in out
    prompts.answers += [SEED]
    ok("add", "2fa", "--kind", "totp", "--domain", "github.com")
    kinds = {e["name"]: e["kind"] for e in json.loads(ok("list", "--json"))}
    assert kinds == {"api": "api_key", "2fa": "totp"}


def test_insecure_key_file_is_refused(home, prompts):
    ok("init")
    os.chmod(keyfile_path(home), 0o644)
    prompts.answers += [USERNAME, PASSWORD, PASSWORD]
    out = fails("add", "ig", "--domain", "instagram.com")
    assert "refusing to use it" in out and "vault_key_insecure" in out
    assert "vault_key_insecure" in ok("status")


def test_passphrase_mode_prompts_and_wrong_passphrase(home, prompts):
    prompts.answers += [PHRASE, PHRASE]
    ok("init", "--passphrase")
    prompts.answers += ["wrong passphrase!!"]
    assert "wrong passphrase" in fails("add", "ig", "--domain", "instagram.com")
    prompts.answers += [PHRASE, USERNAME, PASSWORD, PASSWORD]
    ok("add", "ig", "--domain", "instagram.com", "--lane", "biz-a/instagram")
    prompts.answers += [PHRASE]
    ok("bind", "ig", "personal/*")


def test_catcher_command(home, prompts):
    ok("init")
    add_ig(prompts)
    out = ok("catcher", "ig", "--account", "mail", "--host", "imap.gmail.com", "--sender", "@mail.instagram.com")
    assert "on (imap.gmail.com, from @mail.instagram.com)" in out
    assert "required" in fails("catcher", "ig", "--host", "imap.gmail.com")
    assert "off" in ok("catcher", "ig", "--off")


def test_catcher_command_code_card(home, prompts):
    ok("init")
    add_ig(prompts)
    assert "the code card" in ok("catcher", "ig", "--relay", "--timeout", "120")
    store = VaultStore(home / "vault.db")
    assert store.get("ig").config["catcher"] == {"kind": "relay", "window_seconds": 300, "timeout_seconds": 120}
    assert "must be 30 to 900" in fails("catcher", "ig", "--relay", "--timeout", "5")


# ---- rotation ------------------------------------------------------------------

def test_rotate_key_keyfile(home, prompts):
    ok("init")
    add_ig(prompts)
    old = keyfile_path(home).read_text()
    assert "version 2" in ok("rotate-key")
    assert keyfile_path(home).read_text() == old
    assert "New key file" in ok("rotate-key", "--master")
    assert keyfile_path(home).read_text() != old
    assert stat.S_IMODE(os.stat(keyfile_path(home)).st_mode) == 0o600
    store = VaultStore(home / "vault.db")
    key = cli.load_keyfile_master(store, keyfile_path(home))
    assert store.open(key, "ig", ("secret",))[1]["secret"].reveal() == PASSWORD


def test_rotate_key_passphrase(home, prompts):
    prompts.answers += [PHRASE, PHRASE]
    ok("init", "--passphrase")
    prompts.answers += [PHRASE, USERNAME, PASSWORD, PASSWORD]
    ok("add", "ig", "--domain", "instagram.com")
    new = "another long passphrase"
    prompts.answers += [PHRASE, new, new]
    assert "Passphrase changed" in ok("rotate-key", "--master")
    prompts.answers += [PHRASE]
    assert "wrong passphrase" in fails("bind", "ig", "x/*")
    prompts.answers += [new]
    ok("bind", "ig", "x/*")


# ---- unseal / seal / status ----------------------------------------------------

def test_unseal_without_daemon(home, prompts):
    prompts.answers += [PHRASE, PHRASE]
    ok("init", "--passphrase")
    prompts.answers += [PHRASE]
    assert "no agentos daemon is running" in fails("unseal")
    prompts.answers += ["nope nope nope"]
    assert "wrong passphrase" in fails("unseal")
    assert "nothing to seal" in ok("seal")
    assert "daemon: not running" in ok("status")


class Daemon:
    """A VaultService with its control socket, on its own event loop in a thread."""

    def __init__(self, home: Path, sock: Path):
        self.loop = asyncio.new_event_loop()
        self.service = VaultService(home, socket_path=sock)
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.service.start(), self.loop).result(10)
        return self.service

    def __exit__(self, *exc):
        asyncio.run_coroutine_threadsafe(self.service.stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def test_unseal_and_seal_a_running_daemon(home, prompts, sock):
    prompts.answers += [PHRASE, PHRASE]
    ok("init", "--passphrase")
    with Daemon(home, sock) as service:
        assert "daemon: sealed" in ok("status")
        prompts.answers += ["wrong wrong wrong"]
        assert "vault_wrong_passphrase" in fails("unseal")
        prompts.answers += [PHRASE]
        assert "Vault unsealed (0 entries)." in ok("unseal")
        assert service.state() == "unsealed"
        assert "daemon: unsealed" in ok("status")
        assert "Vault sealed." in ok("seal")
        assert service.state() == "sealed"


# ---- import --------------------------------------------------------------------

APPLE = f"""Title,URL,Username,Password,Notes,OTPAuth
instagram.com ({USERNAME}),https://www.instagram.com/,{USERNAME},{PASSWORD},,otpauth://totp/x?secret={SEED}
gmail,https://mail.google.com,me@gmail.com,mail-pw-1,,
app,androidapp://com.x,me,zzz-pw,,
"""


def test_import_dry_run_then_apply_with_shred(home, tmp_path):
    ok("init")
    src = tmp_path / "Passwords.csv"
    src.write_text(APPLE)
    out = ok("import", str(src), "--bind", "instagram.com=biz-a/instagram", "--dry-run")
    assert "Format: apple" in out and "Would import 2 entries: password 2 (1 with TOTP)." in out
    assert "instagram.com" in out and "biz-a/instagram" in out and "(unbound)" in out
    assert "no web domain 1" in out and "zzz-pw" not in out
    assert ok("list").strip() == "No vault entries."
    out = ok("import", str(src), "--bind", "instagram.com=biz-a/instagram", "--shred")
    assert "Imported 2 entries" in out and "Shredded" in out
    assert not src.exists()
    names = sorted(e["name"] for e in json.loads(ok("list", "--json")))
    assert names == ["gmail", "instagram.com"]
    src.write_text(APPLE)
    out = ok("import", str(src), "--only-bound")
    assert "Imported 0 entries" in out and "still holds every password" in out


def test_import_errors(home, tmp_path):
    ok("init")
    src = tmp_path / "x.csv"
    src.write_text(APPLE)
    assert "domain=workspace/lane" in fails("import", str(src), "--bind", "instagram.com")
    assert "--on-conflict" in fails("import", str(src), "--on-conflict", "merge")
    src.write_text("a,b\n1,2\n")
    assert "no password or TOTP column" in fails("import", str(src))
