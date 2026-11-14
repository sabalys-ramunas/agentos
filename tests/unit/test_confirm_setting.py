"""The `[confirmation]` setting: read by the routing that exists, and its floors wherever a value
comes from (docs/CONFIRMATIONS.md, The setting)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from test_policy_support import Clock, make_service

from agentos.policy.confirm import ConfirmLevel, MethodRegistry, default_registry
from agentos.policy.confirmation import ConfirmationConfig, ConfirmationStore
from agentos.policy.errors import Denied, MethodUnavailable
from agentos.policy.routing import PolicyConfig, RoutingTable, load_policy_config
from agentos.policy.store import DB


@pytest.fixture
def store(tmp_path):
    return ConfirmationStore(DB(tmp_path / "policy.db", ""), base=ConfirmationConfig(), clock=lambda: 5.0)


def test_the_default_is_the_contract():
    c = ConfirmationConfig()
    assert (c.commit.when, c.commit.level) == ("auto", "chat")
    assert (c.pay.when, c.pay.level) == ("ask", "present")
    assert (c.irreversible.when, c.irreversible.level) == ("ask", "present")
    assert c.after_untrusted.level == "signed_in"
    assert c.controller_max_level == "present"
    assert c.methods_off == []


@pytest.mark.parametrize("effect,untrusted,want", [
    ("commit", False, "chat"), ("commit", True, "signed_in"), ("pay", False, "present"), ("pay", True, "present"),
    ("irreversible", False, "present"), ("secret", False, "signed_in"), ("exec", False, "signed_in"),
    ("install_base", False, "signed_in"), ("brain", False, "signed_in"), ("draft", False, "signed_in"),
    ("read", True, "signed_in"), (None, False, "signed_in")])
def test_requires_is_the_strictest_thing_that_applies(effect, untrusted, want):
    assert ConfirmationConfig().requires(effect, untrusted=untrusted).word == want


def test_a_raised_overlay_rises_above_a_lower_row():
    c = ConfirmationConfig(after_untrusted={"level": "present"}, pay={"level": "signed_in"})
    assert c.requires("pay").word == "signed_in"
    assert c.requires("pay", untrusted=True).word == "present"
    assert c.requires("commit", untrusted=True).word == "present"


def test_a_callers_floor_only_raises():
    c = ConfirmationConfig()
    assert c.requires("commit", floor=ConfirmLevel.SIGNED_IN).word == "signed_in"
    assert c.requires("pay", floor=ConfirmLevel.SIGNED_IN).word == "present"


# ---- floors when the file loads ----

@pytest.mark.parametrize("toml", [
    'pay = { when = "auto" }', 'irreversible = { when = "auto" }',
    'pay = { level = "chat" }', 'irreversible = { level = "chat" }', 'after_untrusted = { level = "chat" }',
    'commit = { level = "root" }', 'surprise = 1'])
def test_a_file_under_a_floor_refuses_to_load(tmp_path, toml):
    (tmp_path / "policy.toml").write_text(f"[confirmation]\n{toml}\n")
    with pytest.raises(ValidationError):
        load_policy_config(tmp_path)


def test_a_file_that_raises_loads(tmp_path):
    (tmp_path / "policy.toml").write_text('[confirmation]\ncommit = { level = "present" }\n'
                                          'after_untrusted = { level = "present" }\n')
    c = load_policy_config(tmp_path).confirmation
    assert (c.commit.level, c.after_untrusted.level) == ("present", "present")


@pytest.mark.parametrize("toml", [
    'methods_off = ["panel"]', 'methods_off = ["panel", "cli", "passkey", "chat"]', 'methods_off = ["fax"]',
    '[confirmation.workspaces.harbor]\nmethods_off = ["passkey"]'])
def test_a_file_that_turns_off_what_cannot_be_refuses_to_load(tmp_path, toml):
    """Nobody could decide anything: the same check as a change."""
    (tmp_path / "policy.toml").write_text(f"[confirmation]\n{toml}\n")
    with pytest.raises(ValidationError, match="cannot be turned off"):
        load_policy_config(tmp_path)


def test_a_file_may_turn_chat_off(tmp_path):
    (tmp_path / "policy.toml").write_text('[confirmation]\nmethods_off = ["chat"]\n')
    assert load_policy_config(tmp_path).confirmation.methods_off == ["chat"]


@pytest.mark.parametrize("toml", [
    'pay = { level = "device_key" }', 'irreversible = { level = "device_key" }', 'commit = { level = "device_key" }',
    'after_untrusted = { level = "device_key" }', '[confirmation.workspaces.harbor]\npay = { level = "device_key" }'])
def test_a_level_no_ready_method_reaches_refuses_to_start(tmp_path, toml):
    """Every such approval would wait until it expired."""
    (tmp_path / "policy.toml").write_text(f"[confirmation]\n{toml}\n")
    with pytest.raises(ValueError, match="no way to confirm on this node reaches device_key"):
        make_service(tmp_path, clock=Clock())


def test_a_level_a_ready_method_reaches_starts(tmp_path):
    from agentos.policy.confirm import Method

    methods = MethodRegistry([*default_registry().all(), Method("fakewatch", "Fake", ConfirmLevel.DEVICE_KEY)])
    base = ConfirmationConfig(pay={"level": "device_key"})
    store = ConfirmationStore(DB(tmp_path / "policy.db", ""), base=base, clock=lambda: 1.0, methods=methods)
    assert store.current().pay.level == "device_key"


# ---- commit.when is ask_first_default ----

def test_commit_when_is_ask_first_default_in_both_directions():
    assert PolicyConfig(routing=RoutingTable(ask_first_default=True)).confirmation.commit.when == "ask"
    assert PolicyConfig(confirmation={"commit": {"when": "ask"}}).routing.ask_first_default is True
    assert PolicyConfig().confirmation.commit.when == "auto"


def test_a_file_that_says_both_differently_refuses_to_load(tmp_path):
    (tmp_path / "policy.toml").write_text('[routing]\nask_first_default = true\n'
                                          '[confirmation]\ncommit = { when = "auto" }\n')
    with pytest.raises(ValidationError, match="one setting"):
        load_policy_config(tmp_path)


def test_a_changed_commit_when_reaches_the_switches_a_running_node_reads(tmp_path):
    svc, _ = make_service(tmp_path, clock=Clock())
    assert svc.ask_first.is_on("lane", "biz-a/x") is False
    svc.confirmation.change({"commit": {"when": "ask"}}, by="owner", role="owner")
    assert svc.ask_first.is_on("lane", "biz-a/x") is True
    assert svc.routing_now().ask_first_default is True
    # a person's explicit off still stands above it, as it always did
    svc.ask_first.set("lane", "biz-a/x", False, by="owner", person=True)
    assert svc.ask_first.is_on("lane", "biz-a/x") is False
    svc.close()


def test_a_daemon_reads_a_change_another_process_made(tmp_path):
    first, _ = make_service(tmp_path, clock=Clock())
    second, _ = make_service(tmp_path, clock=Clock())
    second.confirmation.change({"pay": {"level": "signed_in"}}, by="owner:cli", role="owner")
    assert first.confirmation.current().pay.level == "signed_in"
    first.close()
    second.close()


# ---- floors on every change ----

@pytest.mark.parametrize("patch", [
    {"pay": {"when": "auto"}}, {"irreversible": {"when": "auto"}}, {"pay": {"level": "chat"}},
    {"irreversible": {"level": "chat"}}, {"after_untrusted": {"level": "chat"}}])
def test_a_change_under_a_floor_is_refused_and_nothing_is_written(store, patch):
    with pytest.raises(Denied) as e:
        store.change(patch, by="owner", role="owner")
    assert e.value.reason == "floor"
    assert store.current() == ConfirmationConfig()


def test_a_change_is_whole_or_not_at_all(store):
    with pytest.raises(Denied):
        store.change({"commit": {"level": "present"}, "pay": {"level": "chat"}}, by="owner", role="owner")
    assert store.current().commit.level == "chat"


def test_the_owner_moves_pay_between_present_and_signed_in(store):
    assert store.change({"pay": {"level": "signed_in"}}, by="owner", role="owner").pay.level == "signed_in"
    assert store.change({"pay": {"level": "present"}}, by="owner", role="owner").pay.level == "present"


def test_the_owner_moves_after_untrusted_at_or_above_signed_in(store):
    """A node with no passkey must be able to come back from `present`."""
    store.change({"after_untrusted": {"level": "present"}}, by="owner", role="owner")
    assert store.change({"after_untrusted": {"level": "signed_in"}}, by="owner", role="owner").after_untrusted.level == "signed_in"
    with pytest.raises(Denied) as e:
        store.change({"after_untrusted": {"level": "chat"}}, by="owner", role="owner")
    assert e.value.reason == "floor"


def test_the_file_holds_after_untrusted_and_says_where_to_lower_it(tmp_path):
    base = ConfirmationConfig(after_untrusted={"level": "present"})
    store = ConfirmationStore(DB(tmp_path / "policy.db", ""), base=base, clock=lambda: 1.0)
    with pytest.raises(Denied, match="policy.toml") as e:
        store.change({"after_untrusted": {"level": "signed_in"}}, by="owner", role="owner")
    assert e.value.reason == "floor"


def test_an_owner_cannot_lower_what_a_controller_raised_only_their_own(store):
    store.change({"after_untrusted": {"level": "present"}}, by="owner", role="owner")
    store.apply_controller({"after_untrusted": {"level": "present"}}, by="controller")
    store.change({"after_untrusted": {"level": "signed_in"}}, by="owner", role="owner")
    assert store.own().after_untrusted.level == "signed_in" and store.current().after_untrusted.level == "present"


def test_a_level_nothing_reaches_is_refused_so_nothing_is_locked_out(store):
    with pytest.raises(MethodUnavailable):
        store.change({"commit": {"level": "device_key"}}, by="owner", role="owner")


@pytest.mark.parametrize("patch", [{"pay": {"level": "signed_in"}}, {"irreversible": {"level": "signed_in"}},
                                   {"after_untrusted": {"level": "present"}}, {"controller_max_level": "signed_in"}])
def test_an_admin_changes_commit_only(store, patch):
    with pytest.raises(Denied) as e:
        store.change(patch, by="m_1", role="admin")
    assert e.value.reason == "owner_only"


def test_an_admin_changes_commit_and_turns_chat_off_but_not_on(store):
    assert store.change({"commit": {"level": "signed_in", "when": "ask"}}, by="m_1", role="admin").commit.when == "ask"
    assert store.change({"methods_off": ["chat"]}, by="m_1", role="admin").methods_off == ["chat"]
    with pytest.raises(Denied):
        store.change({"methods_off": []}, by="m_1", role="admin")
    assert store.change({"methods_off": []}, by="owner", role="owner").methods_off == []


@pytest.mark.parametrize("method", ["panel", "cli", "passkey", "watch", "nope"])
def test_only_chat_can_be_turned_off(store, method):
    from agentos.core.errors import InvalidRequest

    with pytest.raises(InvalidRequest):
        store.change({"methods_off": [method]}, by="owner", role="owner")


def test_a_stored_row_under_a_floor_is_never_in_force(store):
    store.db.execute("INSERT INTO confirmation_setting (id, body, set_by, set_at) "
                     "VALUES (1, '{\"pay\": {\"level\": \"chat\", \"when\": \"auto\"}}', 'x', 0)")
    assert store.current().pay.level == "present"
    assert store.current().pay.when == "ask"


def test_policy_toml_raised_after_a_change_is_not_undone_by_it(tmp_path):
    db = DB(tmp_path / "policy.db", "")
    ConfirmationStore(db, base=ConfirmationConfig(), clock=lambda: 1.0).change(
        {"after_untrusted": {"level": "signed_in"}}, by="owner", role="owner")
    raised = ConfirmationConfig(after_untrusted={"level": "present"})
    assert ConfirmationStore(db, base=raised, clock=lambda: 2.0).current().after_untrusted.level == "present"


# ---- a controller's config_update ----

def test_a_controller_can_only_ask_for_more_and_a_lower_value_has_no_effect(store):
    result = store.apply_controller({"commit": {"level": "signed_in"}, "pay": {"when": "auto"},
                                     "after_untrusted": {"level": "chat"}}, by="controller")
    assert result["refused"] == []
    now = store.current()
    assert (now.commit.level, now.pay.when, now.after_untrusted.level) == ("signed_in", "ask", "signed_in")


def test_a_controller_never_sets_its_own_limit(store):
    result = store.apply_controller({"controller_max_level": "device_key"}, by="controller")
    assert [r["field"] for r in result["refused"]] == ["controller_max_level"]
    assert store.current().controller_max_level == "present"


def test_a_layer_is_kept_apart_from_what_the_node_holds(store):
    store.change({"commit": {"when": "ask"}, "pay": {"level": "signed_in"}}, by="owner", role="owner")
    store.apply_controller({"commit": {"when": "auto", "level": "chat"}, "pay": {"level": "present"}}, by="controller")
    assert (store.own().commit.when, store.own().pay.level) == ("ask", "signed_in")
    assert (store.current().commit.when, store.current().pay.level) == ("ask", "present")


def test_a_row_the_controller_names_in_part_adds_only_that_part(store):
    """`pay: {when}` names no level, so the row's default level must not come in with it."""
    store.change({"pay": {"level": "signed_in"}}, by="owner", role="owner")
    store.apply_controller({"pay": {"when": "ask"}, "workspaces": {"harbor": {"pay": {"when": "ask"}}}}, by="controller")
    assert store.current().pay.level == "signed_in"
    assert store.current().requires("pay", workspace="harbor") == ConfirmLevel.SIGNED_IN


def test_each_update_replaces_the_layer_whole(store):
    store.apply_controller({"commit": {"when": "ask"}, "pay": {"level": "present"}, "methods_off": ["chat"]}, by="controller")
    store.apply_controller({"commit": {"level": "signed_in"}}, by="controller")
    now = store.current()
    assert (now.commit.when, now.commit.level, now.methods_off) == ("auto", "signed_in", [])


def test_a_null_drops_the_layer_and_what_the_node_holds_stays(store):
    store.change({"pay": {"level": "signed_in"}}, by="owner", role="owner")
    store.apply_controller({"commit": {"when": "ask"}, "pay": {"level": "present"}}, by="controller")
    assert store.apply_controller(None, by="controller") == {"applied": [], "refused": []}
    assert (store.current().commit.when, store.current().pay.level) == ("auto", "signed_in")


def test_a_controller_cannot_undo_what_only_the_file_says(tmp_path):
    base = ConfirmationConfig(commit={"when": "ask", "level": "present"}, after_untrusted={"level": "present"})
    store = ConfirmationStore(DB(tmp_path / "policy.db", ""), base=base, clock=lambda: 1.0)
    store.apply_controller({"commit": {"when": "auto", "level": "chat"}, "after_untrusted": {"level": "signed_in"}}, by="c")
    assert store.current() == base


def test_a_level_nothing_reaches_is_not_taken_from_a_controller_so_nothing_is_locked_out(store):
    result = store.apply_controller({"pay": {"level": "device_key"}, "commit": {"when": "ask"}}, by="controller")
    assert [r["field"] for r in result["refused"]] == ["pay"]
    assert store.current().pay.level == "present" and store.current().commit.when == "ask"


def test_a_controller_sends_nonsense_and_nothing_changes(store):
    assert store.apply_controller("all of it", by="controller")["refused"]
    assert store.apply_controller({"surprise": 1}, by="controller")["refused"]
    assert store.apply_controller({"commit": "soon", "methods_off": ["panel"]}, by="controller")["refused"]
    assert store.current() == ConfirmationConfig()


def test_a_broken_layer_is_never_in_force_and_never_breaks_the_nodes_setting(store):
    store.change({"pay": {"level": "signed_in"}}, by="owner", role="owner")
    store.db.execute("INSERT INTO confirmation_controller (id, body, set_by, set_at) VALUES (1, 'not json', 'x', 0)")
    assert store.current() == store.own()


def test_what_people_read_says_what_a_controller_asks_for(store):
    assert store.public()["controller"] is None
    store.apply_controller({"commit": {"when": "ask"}}, by="controller:org:a")
    assert store.public()["controller"]["commit"] == {"when": "ask"}
    assert store.public()["controller"]["set_by"] == "controller:org:a"


def test_methods_listed_say_what_is_on(store):
    store.change({"methods_off": ["chat"]}, by="owner", role="owner")
    rows = {m["id"]: m for m in store.methods_public()}
    assert rows["chat"]["on"] is False and rows["chat"]["can_turn_off"] is True
    assert rows["panel"]["can_turn_off"] is False
    assert rows["watch"]["status"] == "planned"


def test_a_ready_method_with_a_new_level_opens_that_level(tmp_path):
    from agentos.policy.confirm import Method

    methods = MethodRegistry([*default_registry().all(), Method("fakewatch", "Fake", ConfirmLevel.DEVICE_KEY)])
    store = ConfirmationStore(DB(tmp_path / "policy.db", ""), base=ConfirmationConfig(), clock=lambda: 1.0,
                              methods=methods)
    assert store.change({"pay": {"level": "device_key"}}, by="owner", role="owner").pay.level == "device_key"
