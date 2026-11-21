"""The owner's sign-in from a saved login (openajar.node.login_assist.OwnerVaultContext): the vault's flow runs
with this context, so its two promises are tested here. It refuses another account's entry, and what it
records goes to a sink as names and steps, never as a value."""

from __future__ import annotations

import asyncio

import pytest

from openajar.core.errors import PolicyDenied
from openajar.node.login_assist import OwnerVaultContext


def test_the_owner_context_refuses_another_account_and_allows_its_own():
    ctx = OwnerVaultContext(None, lane_id="biz-a/shop", by="owner")
    assert ctx.can_access_lane("biz-a/shop") and not ctx.can_access_lane("biz-b/shop")
    assert asyncio.run(ctx.authorize("secret", lane_id="biz-a/shop")) is None
    with pytest.raises(PolicyDenied):
        asyncio.run(ctx.authorize("secret", lane_id="biz-b/shop"))


def test_what_the_vault_records_goes_to_the_sink_and_holds_no_value():
    seen: list[tuple[str, dict]] = []
    ctx = OwnerVaultContext(None, lane_id="biz-a/shop", by="owner", sink=lambda kind, fields: seen.append((kind, fields)))
    ctx.trace("vault.use", entry="v_1", lane="biz-a/shop", sink="form_password")
    assert seen == [("vault.use", {"entry": "v_1", "lane": "biz-a/shop", "sink": "form_password"})]
    assert all("hunter2" not in repr(fields) for _, fields in seen)


def test_a_context_with_no_sink_still_traces_nothing_and_fails_nothing():
    ctx = OwnerVaultContext(None, lane_id="biz-a/shop", by="owner")
    ctx.trace("vault.abort", entry="v_1")
    ctx.taint("untrusted", source="page")
    assert ctx.principal is None
