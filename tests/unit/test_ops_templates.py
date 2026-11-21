"""`openajar node templates`: the listing, one template's path and variables, and the manifest checks."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from openajar.cli import app
from openajar.ops import templates

runner = CliRunner()
REPO_TEMPLATES = Path(__file__).resolve().parents[2] / "deploy" / "templates"


def test_the_real_templates_are_listed_in_name_order():
    r = runner.invoke(app, ["node", "templates"])
    assert r.exit_code == 0, r.output
    names = [line.split()[0] for line in r.output.splitlines() if line and not line.startswith(("Show", " "))]
    assert names == sorted(names)
    assert "hetzner" in names
    assert "Show one with: openajar node templates NAME" in r.output


def test_showing_one_template_prints_its_path_and_variables():
    r = runner.invoke(app, ["node", "templates", "hetzner"])
    assert r.exit_code == 0, r.output
    assert f"path: {REPO_TEMPLATES / 'hetzner'}" in r.output
    assert f"file: {REPO_TEMPLATES / 'hetzner' / 'cloud-init.yaml'}" in r.output
    assert "create: deploy/templates/hetzner/create.sh" in r.output
    for name in ("ADMIN_SSH_KEY", "ADMIN_USER", "AGENTOS_GIT_REF", "CONTROLLER_URL", "JOIN_TOKEN"):
        assert name in r.output
    assert "required" in r.output and "optional" in r.output


def test_every_variable_appears_in_its_template_directory():
    for template in templates.load_templates().values():
        text = "".join(p.read_text() for p in template.path.rglob("*") if p.is_file())
        for variable in template.variables:
            assert variable.name in text, f"{template.name}: {variable.name} is not used in its files"


def test_an_unknown_template_exits_2_and_names_the_known_ones():
    r = runner.invoke(app, ["node", "templates", "nope"])
    assert r.exit_code == 2
    assert "no template 'nope'" in r.output and "hetzner" in r.output


def test_an_install_without_the_templates_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(templates, "TEMPLATES_DIR", tmp_path / "missing")
    r = runner.invoke(app, ["node", "templates"])
    assert r.exit_code == 1
    assert "not in this install" in r.output


def _template(root: Path, name: str, manifest: str, *files: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "template.toml").write_text(manifest)
    for file in files:
        (directory / file).write_text("x\n")


def test_directories_without_a_manifest_are_skipped(tmp_path):
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "bootstrap.sh").write_text("#!/bin/sh\n")
    _template(tmp_path, "box", 'title = "Box"\nfile = "box.yaml"\n', "box.yaml")
    assert list(templates.load_templates(tmp_path)) == ["box"]


@pytest.mark.parametrize(("manifest", "problem"), [
    ('file = "a.yaml"\n', "missing title"),
    ('title = "T"\n', "missing file"),
    ('title = "T"\nfile = "gone.yaml"\n', "gone.yaml is not in"),
    ('title = "T"\nfile = "a.yaml"\n[[variables]]\ndescription = "no name"\n', "needs a name"),
    ("title = [\n", "template.toml"),
])
def test_a_bad_manifest_is_reported_not_guessed(manifest, problem, tmp_path, monkeypatch):
    _template(tmp_path, "bad", manifest, "a.yaml")
    monkeypatch.setattr(templates, "TEMPLATES_DIR", tmp_path)
    r = runner.invoke(app, ["node", "templates"])
    assert r.exit_code == 1, r.output
    assert problem in r.output
