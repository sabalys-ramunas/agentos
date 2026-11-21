"""QA 2026-11-07 #5: a node without a vault (`openajar up` before `openajar vault init`) still sends its
inventory. A service that is not set up leaves its own section empty with one log line, never a
traceback each cycle; the rest of the inventory goes out."""

from __future__ import annotations

import logging

import pytest

from openajar.core.runtime import Runtime
from openajar.node.channel import reports
from openajar.vault.errors import VaultNotInitialized


class NoVault:
    async def list_entries(self):
        raise VaultNotInitialized("no vault at /x/vault.db; run: openajar vault init")


class BrokenVault:
    async def list_entries(self):
        raise RuntimeError("disk on fire")


class Brains:
    async def list_brains(self):
        return [{"name": "claude", "kind": "claude-code"}]


@pytest.fixture(autouse=True)
def fresh_log_memory():
    reports.reset_section_log()
    yield
    reports.reset_section_log()


async def test_no_vault_leaves_the_vault_section_empty_and_sends_the_rest(caplog):
    rt = Runtime()
    rt.register("vault", NoVault())
    rt.register("brains", Brains())
    caplog.set_level(logging.DEBUG, logger="openajar.channel")
    for _ in range(3):
        out = await reports.inventory(rt)
        assert out["vault"] == []
        assert out["brains"][0]["name"] == "claude"
    lines = [r for r in caplog.records if "vault" in r.getMessage() and r.levelno >= logging.INFO]
    assert len(lines) == 1, [r.getMessage() for r in lines]
    assert lines[0].exc_info is None and "openajar vault init" in lines[0].getMessage()


async def test_a_broken_section_is_left_out_and_warned_once(caplog):
    rt = Runtime()
    rt.register("vault", BrokenVault())
    rt.register("brains", Brains())
    caplog.set_level(logging.DEBUG, logger="openajar.channel")
    for _ in range(3):
        out = await reports.inventory(rt)
        assert "vault" not in out and out["brains"]
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warned) == 1 and "disk on fire" in warned[0].getMessage()
