"""`openajar policy notify` for members: hours, member channels, rotate, remove, test."""

from __future__ import annotations

import json

import pytest
from test_policy_support import OWNER
from typer.testing import CliRunner

from openajar.core.config import NodeConfig, save_config
from openajar.policy.cli import app

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path))
    save_config(NodeConfig(home=tmp_path, mcp_token=OWNER))
    return tmp_path


def run(*args, input=None):
    return runner.invoke(app, ["notify", *args], input=input)


def test_hours_for_the_owner_members_and_defaults(home):
    out = run("hours", "--quiet", "23:00-07:00", "--timezone", "Europe/Vilnius", "--digest", "08:00,18:00")
    assert out.exit_code == 0, out.output
    shown = json.loads(out.stdout)
    assert shown["quiet_hours"] == ["23:00", "07:00"] and shown["digest_at"] == ["08:00", "18:00"]
    assert shown["urgent_breaks_quiet"] is True  # the owner's default
    off = json.loads(run("hours", "--quiet", "off", "--digest", "off", "--batch", "0").stdout)
    assert off["quiet_hours"] is None and off["digest_at"] == [] and off["batch_window_s"] == 0
    d = json.loads(run("hours", "--defaults", "--quiet", "22:00-07:30", "--batch", "15m").stdout)
    assert d["quiet_hours"] == ["22:00", "07:30"] and d["batch_window_s"] == 900
    assert run("hours", "--quiet", "8am-9pm").exit_code != 0
    assert run("hours", "--timezone", "Mars/Base").exit_code != 0


def test_a_member_channel_rotate_remove_and_test(home):
    made = run("member", "m_va")
    assert made.exit_code == 0 and "subscribe to the topic openajar-" in made.stdout
    topic = made.stdout.split("topic ")[1].split()[0]
    shown = json.loads(run("show", "--member", "m_va").stdout)
    assert shown["topic"] == topic and shown["quiet_hours"] == ["21:00", "08:00"]  # the member defaults
    assert topic not in run("show").stdout  # the node view never shows a member's topic
    hours = json.loads(run("hours", "--member", "m_va", "--urgent-breaks-quiet").stdout)
    assert hours["urgent_breaks_quiet"] is True and "topic" not in hours
    rotated = run("rotate", "m_va")
    assert rotated.exit_code == 0 and topic not in rotated.stdout.split("topic ")[1].split(";")[0]
    hook = run("member", "m_va", "--channel", "webhook", "--webhook-url", "https://va.example/h",
               "--secret-from", "-", input="whsec_va_0123456789")
    assert hook.exit_code == 0 and "channel webhook" in hook.stdout
    assert "whsec_va" not in run("show", "--member", "m_va").stdout
    assert run("member", "m_va", "--channel", "carrier-pigeon").exit_code != 0
    removed = run("remove", "m_va")
    assert removed.exit_code == 0 and "no more pushes" in removed.stdout
    assert json.loads(run("show", "--member", "m_va").stdout)["channel"] == "none"
    tested = run("test", "--member", "m_va")
    assert tested.exit_code == 0 and tested.stdout.strip() == "log: delivered"
    assert run("remove", "owner").exit_code != 0
