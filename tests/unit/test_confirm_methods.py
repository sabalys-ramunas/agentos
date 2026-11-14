"""The levels, the method registry and the seam for a device (docs/CONFIRMATIONS.md, Methods)."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentos.policy import confirm
from agentos.policy.confirm import ConfirmLevel, DeviceKeyMethod, DeviceStore, Person, Proof, default_registry
from agentos.policy.errors import MethodUnavailable, ProofTooWeak
from agentos.policy.store import DB

SRC = Path(__file__).resolve().parents[2] / "src" / "agentos"
DIGEST = "sha256:" + "a" * 64
PLANNED = ("mac", "ios", "android", "watch", "totp")


def approval(**kw):
    base = {"id": "ap_1", "kind": "action", "effect": "commit", "requires": "chat", "follows": None,
            "preview": "the post", "content_hash": "h1", "content_digest": DIGEST, "preview_cut": lambda: False}
    return SimpleNamespace(**{**base, **kw})


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture
def devices(tmp_path):
    return DeviceStore(DB(tmp_path / "policy.db", ""), clock=lambda: 100.0)


class FakeWatch(DeviceKeyMethod):
    """What a real device method adds: a class. The test holds its private key, as a device would."""

    def __init__(self, devices):
        super().__init__("fakewatch", "A fake watch", devices, status="ready", how="a push to the watch")


def enrolled(devices):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, devices.add(person="owner", method="fakewatch", public_key=b64(raw), label="Test watch")


def test_the_device_table_has_the_contracts_columns_and_no_route_fills_it(devices, tmp_path):
    columns = {r["name"] for r in devices.db.all("PRAGMA table_info(confirm_devices)")}
    assert columns == {"id", "person", "method", "public_key", "label", "added_at", "last_used_at", "revoked_at"}
    routes = (SRC / "api" / "routes_members.py").read_text() + (SRC / "api" / "routes_resources.py").read_text()
    assert "confirm_devices" not in routes and "devices.add" not in routes


def test_levels_are_ordered_and_a_word_round_trips():
    assert ConfirmLevel.CHAT < ConfirmLevel.SIGNED_IN < ConfirmLevel.PRESENT < ConfirmLevel.DEVICE_KEY
    assert [ConfirmLevel.of(w).word for w in confirm.LEVEL_WORDS] == ["chat", "signed_in", "present", "device_key"]
    with pytest.raises(ValueError):
        ConfirmLevel.of("root")


def test_the_registry_holds_the_contract_table():
    table = {m.id: (m.level.word, m.status) for m in default_registry().all()}
    assert table == {"panel": ("signed_in", "ready"), "cli": ("signed_in", "ready"), "passkey": ("present", "ready"),
                     "chat": ("chat", "ready"), "telegram": ("chat", "ready"),
                     "mac": ("device_key", "planned"), "ios": ("device_key", "planned"),
                     "android": ("device_key", "planned"), "watch": ("device_key", "planned"),
                     "totp": ("present", "planned")}


@pytest.mark.parametrize("method", [*PLANNED, "fax"])
def test_a_planned_or_unknown_method_is_listed_and_refused(method):
    with pytest.raises(MethodUnavailable) as e:
        default_registry().verify(approval(), Proof(method, ConfirmLevel.DEVICE_KEY))
    assert e.value.code == "method_unavailable"


def test_a_proof_never_says_more_than_its_method_reaches():
    out = default_registry().verify(approval(), Proof("panel", ConfirmLevel.DEVICE_KEY))
    assert out.level == ConfirmLevel.SIGNED_IN


def test_a_method_turned_off_is_refused():
    with pytest.raises(MethodUnavailable):
        default_registry().verify(approval(), Proof("chat", ConfirmLevel.CHAT), off=["chat"])


@pytest.mark.parametrize("change", [{"effect": "pay"}, {"effect": "irreversible"}, {"kind": "question"},
                                    {"follows": [{"kind": "web_page", "host": "evil.example"}]},
                                    {"requires": "signed_in"}, {"requires": None}, {"preview": None},
                                    {"preview_cut": lambda: True}])
def test_chat_is_refused_by_the_method_itself_whatever_the_row_says(change):
    with pytest.raises(ProofTooWeak):
        default_registry().verify(approval(**change), Proof("chat", ConfirmLevel.CHAT))


@pytest.mark.parametrize("change", [{"effect": "pay"}, {"effect": "irreversible"}, {"kind": "question"},
                                    {"follows": [{"kind": "web_page", "host": "evil.example"}]},
                                    {"requires": "signed_in"}, {"requires": "present"}, {"requires": None},
                                    {"preview": None}, {"preview_cut": lambda: True}, {"methods": ["panel", "chat"]}])
def test_telegram_is_refused_by_the_method_itself_as_chat_is(change):
    with pytest.raises(ProofTooWeak):
        default_registry(telegram_paired=lambda: True).verify(approval(**{"methods": ["telegram"], **change}),
                                                              Proof("telegram", ConfirmLevel.CHAT))


def test_telegram_reaches_chat_and_no_more_and_only_the_paired_owner():
    reg = default_registry(telegram_paired=lambda: True)
    assert reg.verify(approval(methods=["telegram"]), Proof("telegram", ConfirmLevel.DEVICE_KEY)).level == ConfirmLevel.CHAT
    assert reg.reaching(approval(), Person("owner")) == ["panel", "cli", "chat", "telegram"]
    assert "telegram" not in reg.reaching(approval(), Person("member-1"))
    assert "telegram" not in default_registry(telegram_paired=lambda: False).reaching(approval(), Person("owner"))
    with pytest.raises(MethodUnavailable):
        reg.verify(approval(methods=["telegram"]), Proof("telegram", ConfirmLevel.CHAT), off=["telegram"])


def test_methods_are_what_could_reach_it_for_this_person():
    reg = default_registry()
    assert reg.reaching(approval(), Person("owner")) == ["panel", "cli", "chat"]
    assert reg.reaching(approval(), Person("owner", passkey=True)) == ["panel", "cli", "passkey", "chat"]
    assert reg.reaching(approval(), Person("owner"), off=["chat"]) == ["panel", "cli"]
    assert reg.reaching(approval(requires="present", effect="pay"), Person("owner", passkey=True)) == ["passkey"]
    assert reg.reaching(approval(requires="present", effect="pay"), Person("owner")) == []


def test_a_new_device_method_reaches_device_key_by_a_real_signature(devices):
    reg = default_registry(devices)
    reg.register(FakeWatch(devices))
    key, device = enrolled(devices)
    ap = approval(requires="device_key", effect="pay")
    assert reg.reaching(ap, Person("owner")) == ["fakewatch"]
    signed = {"device": device.id, "signature": b64(key.sign(confirm.signed_message(ap)))}
    out = reg.verify(ap, Proof("fakewatch", ConfirmLevel.DEVICE_KEY, detail=signed))
    assert (out.method, out.level) == ("fakewatch", ConfirmLevel.DEVICE_KEY)
    assert devices.get(device.id).last_used_at == 100.0


def _refused(reg, ap, detail):
    with pytest.raises(ProofTooWeak):
        reg.verify(ap, Proof("fakewatch", ConfirmLevel.DEVICE_KEY, detail=detail))


def test_a_signature_binds_the_content_and_the_approval(devices):
    reg = default_registry(devices)
    reg.register(FakeWatch(devices))
    key, device = enrolled(devices)
    ap = approval(requires="device_key")
    good = b64(key.sign(confirm.signed_message(ap)))
    _refused(reg, approval(requires="device_key", content_digest="sha256:" + "b" * 64),
             {"device": device.id, "signature": good})  # other content
    _refused(reg, approval(requires="device_key", id="ap_2"), {"device": device.id, "signature": good})  # replayed
    other = Ed25519PrivateKey.generate()
    _refused(reg, ap, {"device": device.id, "signature": b64(other.sign(confirm.signed_message(ap)))})
    _refused(reg, ap, {"device": device.id, "signature": "not base64 !"})
    _refused(reg, ap, {"device": device.id})
    _refused(reg, ap, {"signature": good})


def test_a_revoked_device_or_another_methods_device_signs_nothing(devices):
    reg = default_registry(devices)
    reg.register(FakeWatch(devices))
    key, device = enrolled(devices)
    ap = approval(requires="device_key")
    devices.revoke(device.id)
    _refused(reg, ap, {"device": device.id, "signature": b64(key.sign(confirm.signed_message(ap)))})
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    elsewhere = devices.add(person="owner", method="otherwatch", public_key=b64(raw), label="Other")
    _refused(reg, ap, {"device": elsewhere.id, "signature": b64(key.sign(confirm.signed_message(ap)))})


def test_nothing_outside_the_registry_names_a_device():
    """`watch` and `totp` mean other things elsewhere (a member's watch-only control, a vault entry's kind)."""
    devices = re.compile(r"""["'](mac|ios|android)["']""")
    for path in SRC.rglob("*.py"):
        if path.name != "confirm.py" and ("policy" in path.parts or "api" in path.parts):
            assert not devices.search(path.read_text()), f"{path} names a device"
    rules = re.compile(r"""["'](mac|ios|android|watch|totp)["']""")
    for name in ("policy/routing.py", "policy/approvals.py"):
        assert not rules.search((SRC / name).read_text()), f"{name} names a device"


def test_a_token_proves_no_person_however_it_reads():
    for via in ("owner_token", "token"):
        proof = confirm.arrival(via=via)
        assert (proof.method, proof.level, proof.counts) == (None, None, ConfirmLevel.SIGNED_IN)
    assert confirm.arrival(via=None, decider=object()).level is None  # a grant, not in person
    assert confirm.arrival(via="push_link").counts is None


def test_a_person_in_a_session_and_the_cli_are_signed_in():
    assert confirm.arrival(via=None).method == "cli"
    assert confirm.arrival(via="session").level == ConfirmLevel.SIGNED_IN
    assert confirm.arrival(via="passkey", decider=object(), in_person=True).method == "panel"
