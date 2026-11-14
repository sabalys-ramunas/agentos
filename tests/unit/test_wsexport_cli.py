"""`agentos workspace export|import` through typer's CliRunner, between two node homes (the real stores,
the real lane manager without a browser; see test_wsexport_support)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_wsexport_support import IG_PASSWORD, LOG_N, PASSPHRASE, Node, files_containing
from typer.testing import CliRunner

from agentos.core import age
from agentos.workspaces import cli
from agentos.workspaces.cli import app
from agentos.workspaces.transfer import export as wsexport

runner = CliRunner()


@pytest.fixture
def nodes(tmp_path, monkeypatch):
    real = wsexport.export_workspace

    async def quick(*args, **kw):  # a cheap scrypt work factor in tests (real exports use age's 2^18)
        return await real(*args, **{**kw, "log_n": LOG_N})

    monkeypatch.setattr(wsexport, "export_workspace", quick)
    a, b = Node(tmp_path, "node-a"), Node(tmp_path, "node-b")
    a.build_acme()
    managers = {a.home: a.lanes, b.home: b.lanes}
    monkeypatch.setattr(cli, "_lane_manager", lambda cfg: managers[cfg.home])
    yield a, b
    a.close()
    b.close()


def secret_file(tmp_path: Path, name: str, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / name
    path.write_text(text + "\n")
    path.chmod(mode)
    return path


def on(node: Node, monkeypatch, *args: str, input_text: str | None = None):
    monkeypatch.setenv("AGENTOS_HOME", str(node.home))
    return runner.invoke(app, list(args), input=input_text)


def test_export_then_dry_run_then_import(nodes, tmp_path, monkeypatch):
    a, b = nodes
    pw = secret_file(tmp_path, "pw", PASSPHRASE)
    out = tmp_path / "acme.agentos"
    done = on(a, monkeypatch, "export", "acme", "--to", str(out), "--passphrase-file", str(pw))
    assert done.exit_code == 0, done.output
    assert f"Exported workspace acme to {out}" in done.output and "acme/instagram (cold)" in done.output
    assert "3 vault entries" in done.output and "grant tokens" in done.output and "—" not in done.output
    assert IG_PASSWORD not in done.output

    dry = on(b, monkeypatch, "import", str(out), "--passphrase-file", str(pw), "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "Import workspace acme from node node-a" in dry.output and "acme/instagram" in dry.output
    assert "ONE DEVICE, TWO PLACES" in dry.output and "Dry run: nothing was changed." in dry.output
    assert b.lanes.list() == [] and "—" not in dry.output

    real = on(b, monkeypatch, "import", str(out), "--passphrase-file", str(pw), "--yes")
    assert real.exit_code == 0, real.output
    assert "Imported workspace acme: 2 lanes (cold), 3 vault entries, 2 flows, 4 schedules (0 on)" in real.output
    assert "Webhooks have new URLs and secrets" in real.output
    assert [i.spec.id for i in b.lanes.list()] == ["acme/instagram", "acme/reddit"]
    assert not list((b.home / "tmp").iterdir())
    assert files_containing(tmp_path, (IG_PASSWORD,)) == []

    again = on(b, monkeypatch, "import", str(out), "--passphrase-file", str(pw), "--yes")
    assert again.exit_code == 1 and "already exist on this node" in again.output
    assert "Nothing was changed." in again.output


def test_a_dated_name_in_a_directory_and_json(nodes, tmp_path, monkeypatch):
    a, b = nodes
    pw = secret_file(tmp_path, "pw", PASSPHRASE)
    outdir = tmp_path / "exports"
    outdir.mkdir()
    done = on(a, monkeypatch, "export", "acme", "--to", str(outdir), "--passphrase-file", str(pw), "--json")
    assert done.exit_code == 0, done.output
    data = json.loads(done.output)
    path = Path(data["path"])
    assert path.parent == outdir and path.name.startswith("agentos-acme-") and path.suffix == ".agentos"
    assert data["counts"]["vault_entries"] == 3 and "grant tokens" in " ".join(data["never_exported"])
    plan = on(b, monkeypatch, "import", str(path), "--passphrase-file", str(pw), "--dry-run", "--json")
    assert plan.exit_code == 0, plan.output
    assert json.loads(plan.output)["ok"] is True
    result = on(b, monkeypatch, "import", str(path), "--passphrase-file", str(pw), "--yes", "--json",
                "--reissue-grants", "--as", "acme-moved")
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["result"]["lanes"] == ["acme-moved/instagram", "acme-moved/reddit"]
    assert body["result"]["grants"][0]["token"]


def test_reissued_tokens_are_printed_once(nodes, tmp_path, monkeypatch):
    a, b = nodes
    pw = secret_file(tmp_path, "pw", PASSPHRASE)
    out = tmp_path / "acme.agentos"
    on(a, monkeypatch, "export", "acme", "--to", str(out), "--passphrase-file", str(pw))
    done = on(b, monkeypatch, "import", str(out), "--passphrase-file", str(pw), "--yes", "--reissue-grants")
    assert done.exit_code == 0, done.output
    assert "Each token is shown only now" in done.output
    after = done.output.split("Each token is shown only now", 1)[1]
    token = next(line.split()[-1] for line in after.splitlines() if "acme-agent" in line)
    assert b.policy.resolve(token)[0].id == "acme-agent"


def test_a_recipient_and_an_identity(nodes, tmp_path, monkeypatch):
    a, b = nodes
    identity, recipient = age.generate_identity()
    key = secret_file(tmp_path, "key.txt", f"# created by age-keygen\n{identity}")
    out = tmp_path / "acme.agentos"
    done = on(a, monkeypatch, "export", "acme", "--to", str(out), "--recipient", recipient)
    assert done.exit_code == 0, done.output
    pw = secret_file(tmp_path, "pw", PASSPHRASE)
    wrong = on(b, monkeypatch, "import", str(out), "--passphrase-file", str(pw), "--dry-run")
    assert wrong.exit_code == 1 and "encrypted to an age key" in wrong.output
    ok = on(b, monkeypatch, "import", str(out), "--identity", str(key), "--dry-run")
    assert ok.exit_code == 0, ok.output
    bad = on(a, monkeypatch, "export", "acme", "--to", str(tmp_path / "x.agentos"), "--recipient", "age1nope")
    assert bad.exit_code == 1 and "age recipient" in bad.output


def test_a_sealed_passphrase_vault_needs_its_passphrase(nodes, tmp_path, monkeypatch):
    a, _ = nodes
    c = Node(tmp_path, "node-c", vault_mode="passphrase")
    monkeypatch.setattr(cli, "_lane_manager", lambda cfg: {a.home: a.lanes, c.home: c.lanes}[cfg.home])
    try:
        pw = secret_file(tmp_path, "pw", PASSPHRASE)
        out = tmp_path / "acme.agentos"
        on(a, monkeypatch, "export", "acme", "--to", str(out), "--passphrase-file", str(pw))
        sealed = on(c, monkeypatch, "import", str(out), "--passphrase-file", str(pw), "--yes")
        assert sealed.exit_code == 1 and "vault on this node is sealed" in sealed.output
        assert c.lanes.list() == []
        vpw = secret_file(tmp_path, "vpw", c.vault_passphrase)
        done = on(c, monkeypatch, "import", str(out), "--passphrase-file", str(pw), "--yes",
                  "--vault-passphrase-file", str(vpw))
        assert done.exit_code == 0, done.output
        wrong = secret_file(tmp_path, "wrong", "not the vault passphrase at all")
        refused = on(c, monkeypatch, "import", str(out), "--passphrase-file", str(pw), "--yes", "--as", "acme3",
                     "--vault-passphrase-file", str(wrong))
        assert refused.exit_code == 1 and "wrong passphrase" in refused.output
    finally:
        c.close()


def test_bad_inputs_are_one_line_errors(nodes, tmp_path, monkeypatch):
    a, b = nodes
    loose = secret_file(tmp_path, "loose", PASSPHRASE, mode=0o644)
    out = tmp_path / "acme.agentos"
    r = on(a, monkeypatch, "export", "acme", "--to", str(out), "--passphrase-file", str(loose))
    assert r.exit_code == 1 and "readable by others" in r.output and not out.exists()
    pw = secret_file(tmp_path, "pw", PASSPHRASE)
    r = on(a, monkeypatch, "export", "ghost", "--to", str(out), "--passphrase-file", str(pw))
    assert r.exit_code == 1 and "error: not_found: no workspace 'ghost'" in r.output
    on(a, monkeypatch, "export", "acme", "--to", str(out), "--passphrase-file", str(pw))
    r = on(b, monkeypatch, "import", str(out), "--passphrase-file", str(pw), "--map-proxy", "lt1")
    assert r.exit_code == 1 and "--map-proxy takes OLD=NEW" in r.output
    wrong = secret_file(tmp_path, "wrong", "a different passphrase entirely")
    r = on(b, monkeypatch, "import", str(out), "--passphrase-file", str(wrong))
    assert r.exit_code == 1 and "error: bad_export: wrong passphrase" in r.output
    r = on(b, monkeypatch, "import", str(out), "--passphrase-file", str(pw), "--on-vault-conflict", "merge")
    assert r.exit_code == 1 and "vault conflict rule" in r.output
    assert not list((b.home / "tmp").iterdir())
