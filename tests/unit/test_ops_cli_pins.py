"""Pins: what `agentos lane check|audit|redteam`, `workspace export|import` and `backup create|verify`
print on a small home, with times, sizes and paths masked.

The outputs live in tests/golden/ops-cli/. A change that is meant to change them rewrites them with
AGENTOS_UPDATE_GOLDEN=1; read the diff before committing it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from test_ops_support import PASSPHRASE, make_home
from typer.testing import CliRunner

from agentos import cli as root_cli
from agentos.core.system import Host

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "ops-cli"
UPDATE = os.environ.get("AGENTOS_UPDATE_GOLDEN") == "1"

MASKS = [
    (r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?", "<TS>"),
    (r"\d{8}T\d{6}Z?", "<TS>"),
    (r"\d{4}-\d{2}-\d{2}", "<DATE>"),
    (r"\b\d{2}:\d{2} UTC", "<HM> UTC"),
    (r'"(disk_free_bytes|free_bytes|used_bytes|size|bytes|profile_bytes)": \d+', r'"\1": <N>'),
    (r"\b[0-9a-f]{16,}\b", "<HEX>"),
    (r"\b\d+(\.\d+)?\s?(B|KiB|MiB|GiB|KB|MB|bytes)\b", "<SIZE>"),
    (r'"(hostname)": "[^"]*"', r'"\1": "<HOST>"'),
    (r"\(host [^)]*\)", "(host <HOST>)"),
    (r"by agentos [0-9][^ ,\n\"]*", "by agentos <VER>"),
    (r'"agentos_version": "[^"]*"', '"agentos_version": "<VER>"'),
]

runner = CliRunner()


def mask(text: str, tmp: Path) -> str:
    text = text.replace(str(tmp.resolve()), "<TMP>").replace(str(tmp), "<TMP>")
    for pat, rep in MASKS:
        text = re.sub(pat, rep, text)
    return text


@pytest.fixture
def home(tmp_path, monkeypatch):
    cfg = make_home(tmp_path, lanes={"personal/reddit": 150, "work/site": None})
    monkeypatch.setenv("AGENTOS_HOME", str(cfg.home))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(Host, "camoufox", lambda self, cache=None: {"verstr": "150.0.1"})
    pw = tmp_path / "pw"
    pw.write_text(PASSPHRASE + "\n")
    pw.chmod(0o600)
    return cfg, pw


def run(tmp: Path, *args: str) -> str:
    r = runner.invoke(root_cli.app, list(args))
    return mask(f"$ agentos {' '.join(args)}\nexit {r.exit_code}\n{r.output}", tmp)


def pin(name: str, text: str) -> None:
    path = GOLDEN / f"{name}.txt"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert path.exists(), f"no golden output {path.name}: run with AGENTOS_UPDATE_GOLDEN=1"
    assert text == path.read_text(), f"{name} differs from {path}"


def test_lane_check_audit_and_redteam(home, tmp_path):
    pin("lane-check-all", run(tmp_path, "lane", "check", "--all", "--offline"))
    pin("lane-check-all-json", run(tmp_path, "lane", "check", "--all", "--offline", "--json"))
    pin("lane-check-one", run(tmp_path, "lane", "check", "work/site", "--offline"))
    pin("lane-check-none", run(tmp_path, "lane", "check"))
    pin("lane-audit", run(tmp_path, "lane", "audit"))
    pin("lane-audit-json", run(tmp_path, "lane", "audit", "--json"))
    pin("lane-redteam-pick-one", run(tmp_path, "lane", "redteam"))


def test_workspace_export_and_import_plan(home, tmp_path):
    _, pw = home
    out = tmp_path / "personal.agentos"
    pin("workspace-export", run(tmp_path, "workspace", "export", "personal", "--to", str(out),
                                "--passphrase-file", str(pw), "--json"))
    pin("workspace-import-dry-run", run(tmp_path, "workspace", "import", str(out), "--as", "copy", "--dry-run",
                                        "--passphrase-file", str(pw), "--json"))
    pin("workspace-import-dry-run-text", run(tmp_path, "workspace", "import", str(out), "--as", "copy",
                                             "--dry-run", "--passphrase-file", str(pw)))


def test_backup_create_and_verify(home, tmp_path):
    _, pw = home
    target = tmp_path / "backups"
    target.mkdir()
    pin("backup-create", run(tmp_path, "backup", "create", "--to", str(target), "--passphrase-file", str(pw),
                             "--keep", "0"))
    archive = next(target.iterdir())
    pin("backup-verify", run(tmp_path, "backup", "verify", str(archive), "--passphrase-file", str(pw), "--json"))
