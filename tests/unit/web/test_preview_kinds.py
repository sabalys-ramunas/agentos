"""The Needs you previews told from what an approval binds: a git push, a cloud machine, a new lane and a command
in one (agentos.api.web.cardwords). The content is the approval's preview, as the node writes it (indented JSON,
cut at its length limit with a note), so a long push log still reads."""

from __future__ import annotations

import json

from agentos.api.web import cardwords
from agentos.api.web.decisions import Ctx, approval_out

PUSH = {"git_push": "github.com/harbor/shop", "refs": [
    {"ref": "refs/heads/main", "kind": "update", "old": "a" * 40, "new": "b" * 40, "force": True, "protected": True,
     "commits": 3, "log": ["b1 Fix the cart total", "b2 Round prices", "b3 Test the checkout"]},
    {"ref": "refs/tags/v1.4", "kind": "create", "old": None, "new": "b" * 40, "commits": 3, "log": []},
]}


def told(content: dict, effect: str, lane: str = "dev/run-shop", cut: int | None = None) -> dict:
    text = json.dumps(content, indent=1)
    if cut is not None:
        text = text[:cut] + f"\n[preview truncated: {len(text) - cut} more characters are not shown]"
    return cardwords.preview_of({"effect": effect, "lane": lane, "preview": {"text": text}}, "Shop")


def test_a_git_push_names_each_branch_with_its_commits_and_what_makes_it_final() -> None:
    preview = told(PUSH, "irreversible")
    assert preview["kind"] == "push"
    data = preview["data"]
    assert data["remote"] == "github.com/harbor/shop"
    main, tag = data["push_refs"]
    assert main == {"ref": "refs/heads/main", "name": "main", "kind": "update", "force": True, "protected": True,
                    "commits": 3, "log": ["b1 Fix the cart total", "b2 Round prices", "b3 Test the checkout"]}
    assert tag["name"] == "v1.4" and tag["kind"] == "create" and tag["force"] is False


def test_a_push_cut_short_still_reads_what_is_whole() -> None:
    long = {"git_push": "github.com/harbor/shop", "refs": [
        {"ref": "refs/heads/main", "kind": "update", "force": False, "protected": False, "commits": 300,
         "log": [f"c{i:03} A commit message that is long enough to fill the preview" for i in range(300)]}]}
    preview = told(long, "commit", cut=1900)
    assert preview["kind"] == "push"
    [main] = preview["data"]["push_refs"]
    assert main["name"] == "main" and len(main["log"]) <= cardwords.PUSH_LOG_LINES


def test_a_cloud_machine_says_its_size_time_limit_and_price() -> None:
    preview = told({"create": "dev/ci", "profile": "small", "server_type": "cpx21", "ttl_minutes": 90,
                    "price_hourly_eur": 0.0119, "max_eur": 0.04}, "vm", lane="dev/ci")
    assert preview == {"kind": "vm", "data": {"computer": "dev/ci", "server_type": "cpx21", "vm_profile": "small",
                                              "price_hourly_eur": 0.0119, "max_cost_eur": 0.04, "ttl_minutes": 90}}


def test_a_new_lane_and_its_first_command() -> None:
    lane = told({"on": "dev/run-shop", "preset": "sandbox", "overrides": {"network": "isolated", "memory_mb": 4096}},
                "exec")
    assert lane["kind"] == "lane" and lane["data"]["computer"] == "dev/run-shop"
    assert lane["data"]["lane_preset"] == "sandbox"
    assert {"label": "Network", "value": "isolated"} in lane["data"]["fields"]
    command = told({"cmd": "pnpm test", "cwd": "src/shop", "env": {"CI": "1"}, "timeout_s": 1800,
                    "sync": {"dest": "src/shop", "manifest": ["x" * 64], "clean": False}}, "exec")
    assert command["kind"] == "lane"
    data = command["data"]
    assert data["command"] == "pnpm test" and data["cwd"] == "src/shop" and data["timeout_s"] == 1800
    assert data["sync_dest"] == "src/shop" and data["computer"] == "dev/run-shop"


def test_other_records_keep_their_kinds() -> None:
    assert told({"cancel": "job-0123456789"}, "exec")["kind"] == "form"
    assert told(PUSH | {"git_push": 3}, "commit")["kind"] == "form"


def test_the_approval_the_app_reads_has_the_push_kind() -> None:
    item = {"id": "ap_1", "state": "pending", "effect": "irreversible", "lane": "dev/run-shop", "summary": "Push main",
            "preview": {"text": json.dumps(PUSH, indent=1)}, "requested_by": {}}
    out = approval_out(item, Ctx({}, set(), {}))
    assert out["preview"]["kind"] == "push" and out["effect"] == "irreversible"
