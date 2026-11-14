"""`agentos config example`: a commented node.toml that never falls behind NodeConfig."""

from __future__ import annotations

import tomllib

from typer.testing import CliRunner

from agentos.cli import app
from agentos.core.config import NodeConfig
from agentos.core.config_report import DESCRIPTIONS, SECRET_FIELDS, UNSET_EXAMPLES, example


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
    assert '# mcp_token = "..."' in result.output and "AGENTOS_HOME" in result.output
    assert "—" not in result.output
