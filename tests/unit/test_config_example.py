"""`openajar config example`: a commented node.toml that never falls behind NodeConfig."""

from __future__ import annotations

import tomllib

from typer.testing import CliRunner

from openajar.cli import app
from openajar.core.config import NodeConfig
from openajar.core.config_report import DESCRIPTIONS, SECRET_FIELDS, UNSET_EXAMPLES, example


def test_every_setting_has_a_plain_description():
    missing = [n for n in NodeConfig.model_fields if not DESCRIPTIONS.get(n)]
    assert missing == [], f"add these to config_report.GROUPS: {missing}"
    assert not set(DESCRIPTIONS) - set(NodeConfig.model_fields), "a description names a setting that is gone"
    assert all("—" not in text for text in DESCRIPTIONS.values())


def test_uncommenting_every_default_line_changes_nothing():
    lines = example()
    assert all(line == "" or line.startswith("#") for line in lines)  # copying the file changes nothing
    settings = [line[2:] for line in lines if line.startswith("# ") and " = " in line
                and line[2:].split(" = ")[0] in NodeConfig.model_fields]
    parsed = tomllib.loads("\n".join(s.split("   #")[0] for s in settings
                                     if s.split(" = ")[0] not in SECRET_FIELDS | set(UNSET_EXAMPLES)))
    defaults = NodeConfig()
    assert set(parsed) == set(NodeConfig.model_fields) - SECRET_FIELDS - set(UNSET_EXAMPLES)
    for name, value in parsed.items():
        assert NodeConfig(**{name: value}).model_dump()[name] == defaults.model_dump()[name], name
    for name, value in UNSET_EXAMPLES.items():  # the example values are valid settings
        NodeConfig(**{name: value})


def test_cli_prints_the_example_with_no_secret():
    result = CliRunner().invoke(app, ["config", "example"])
    assert result.exit_code == 0, result.output
    assert '# mcp_token = "..."' in result.output and "OPENAJAR_HOME" in result.output
    assert "—" not in result.output


def test_an_old_node_toml_with_the_engagement_settings_still_loads(tmp_path):
    """The engagement engine was removed. A node whose node.toml still holds its settings starts as
    before: the old keys are ignored, every other setting is read, and nothing writes them back."""
    from openajar.core.config import load_config, save_config

    (tmp_path / "node.toml").write_text(
        'name = "old-node"\nmcp_port = 7499\nkeepalive = true\nengage_sessions = true\nengage_draft_provider = "http"\n'
        'engage_draft_endpoint = "https://drafts.example/v1"\nengage_draft_secret_ref = "engage/draft-provider/token"\n'
        "engage_draft_timeout_s = 30.0\n")
    (tmp_path / "engage.db").write_bytes(b"SQLite format 3\x00")  # an old engagement store, never opened
    cfg = load_config(tmp_path)
    assert cfg.name == "old-node" and cfg.mcp_port == 7499 and cfg.keepalive is True
    assert not any(name.startswith("engage") for name in cfg.model_dump())
    save_config(cfg)
    assert "engage" not in (tmp_path / "node.toml").read_text()
    assert (tmp_path / "engage.db").read_bytes() == b"SQLite format 3\x00"
