"""Signed connector bundles: signing and verification with a test key, install from a
bundle, and the `openajar connector` CLI."""

import copy
import json

import pytest
from connector_samples import SITES, connector, write_dir
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from openajar.runner.connectors import cli as connector_cli
from openajar.runner.connectors.compile import compile_connector
from openajar.runner.connectors.service import ConnectorService
from openajar.runner.connectors.signing import (
    BUNDLE_FORMAT,
    BundleRejected,
    build_payload,
    key_id,
    private_pem,
    public_text,
    sign_payload,
    trusted_from,
    verify_bundle,
)
from openajar.runner.fakes import OWNER, Harness

TEST_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))  # a fixed test key, never a real one
OTHER_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
TRUSTED = trusted_from([public_text(TEST_KEY)])


def _bundle(*docs, key=TEST_KEY):
    compiled = [compile_connector(d, sites=SITES) for d in docs or (connector(),)]
    return sign_payload(build_payload(compiled), key)


def test_key_ids_are_stable():
    assert key_id(TEST_KEY) == key_id(TEST_KEY.public_key()) == key_id(public_text(TEST_KEY))
    assert key_id(TEST_KEY).startswith("ed25519:") and key_id(TEST_KEY) != key_id(OTHER_KEY)
    assert TRUSTED == {key_id(TEST_KEY): public_text(TEST_KEY)}


def test_a_signed_bundle_verifies():
    bundle = _bundle()
    assert bundle["format"] == BUNDLE_FORMAT and bundle["signature"]["key_id"] == key_id(TEST_KEY)
    payload, kid = verify_bundle(json.loads(json.dumps(bundle)), TRUSTED)  # survives a JSON round trip
    assert kid == key_id(TEST_KEY)
    assert payload["index"][0]["connector"] == "social"
    assert set(payload["index"][0]["operations"]["profile_summary"]) == {"effect", "contract", "implementation"}


@pytest.mark.parametrize(("tamper", "message"), [
    (lambda b: b["payload"]["connectors"][0].update(version="9.9.9"), "changed after signing"),
    (lambda b: b["payload"]["connectors"][0]["operations"]["profile_summary"]["steps"][0].update(
        url="https://social.example/elsewhere"), "changed after signing"),
    (lambda b: b["signature"].update(sig=b["signature"]["sig"][:-4] + "AAAA"), "changed after signing"),
    (lambda b: b.pop("signature"), "not signed"),
    (lambda b: b["signature"].update(alg="rsa"), "unsupported signature"),
    (lambda b: b.update(format="something/1"), "not a connector bundle"),
    (lambda b: b["signature"].update(key_id=key_id(OTHER_KEY)), "does not trust"),
])
def test_tampered_or_unsigned_bundles_are_rejected(tamper, message):
    bundle = copy.deepcopy(_bundle())
    tamper(bundle)
    with pytest.raises(BundleRejected, match=message):
        verify_bundle(bundle, TRUSTED)


def test_a_bundle_signed_by_an_untrusted_key_is_rejected():
    with pytest.raises(BundleRejected, match="does not trust"):
        verify_bundle(_bundle(key=OTHER_KEY), TRUSTED)
    with pytest.raises(BundleRejected, match="does not trust"):
        verify_bundle(_bundle(), None)  # no catalog key ships yet, so nothing verifies by default


def test_a_trusted_key_whose_id_lies_is_rejected():
    with pytest.raises(BundleRejected, match="does not match its id"):
        verify_bundle(_bundle(), {key_id(TEST_KEY): public_text(OTHER_KEY)})


def test_install_from_a_verified_bundle_records_the_signer(tmp_path):
    h = Harness(tmp_path)
    svc = ConnectorService(h.flows, sites=SITES, trusted=TRUSTED)
    rows = svc.install_bundle(_bundle(), by=OWNER)
    assert rows[0]["flow"] == "connector.social.profile_summary"
    fv = h.flows.get("connector.social.profile_summary")
    assert fv.saved_by["signer"] == key_id(TEST_KEY) and fv.owner_saved
    assert svc.describe()[0]["signer"] == key_id(TEST_KEY)


def test_a_signed_index_that_lies_about_the_contract_is_rejected(tmp_path):
    h = Harness(tmp_path)
    compiled = compile_connector(connector(), sites=SITES)
    payload = build_payload([compiled])
    payload["index"][0]["operations"]["profile_summary"]["contract"] = "sha256:" + "0" * 64
    svc = ConnectorService(h.flows, sites=SITES, trusted=TRUSTED)
    with pytest.raises(BundleRejected, match="contract hash does not match"):
        svc.install_bundle(sign_payload(payload, TEST_KEY), by=OWNER)
    assert svc.installed() == []


def test_a_bundle_installs_all_or_nothing(tmp_path):
    h = Harness(tmp_path)
    svc = ConnectorService(h.flows, sites={**SITES, "other": ("social.example",)}, trusted=TRUSTED)
    svc.install(compile_connector(connector(version="2.0.0"), sites=SITES), by=OWNER)
    other = connector(connector="other", site="other")
    bundle = sign_payload(build_payload([compile_connector(d, sites=svc.sites)
                                         for d in (other, connector(version="1.0.0"))]), TEST_KEY)
    with pytest.raises(Exception, match="downgrade"):
        svc.install_bundle(bundle, by=OWNER)
    assert [fv.id for fv in svc.installed()] == ["connector.social.profile_summary"]


def test_a_bundle_holds_each_connector_once():
    cc = compile_connector(connector(), sites=SITES)
    with pytest.raises(BundleRejected, match="once"):
        build_payload([cc, cc])


# ---------------------------------------------------------------- the CLI


def test_cli_compile_keygen_sign_and_install(tmp_path, monkeypatch):
    import openajar.runner.connectors.lint as lint_mod

    real = lint_mod.site_hosts
    monkeypatch.setattr(lint_mod, "site_hosts", lambda sites=None: real(SITES if sites is None else sites))
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    d = write_dir(tmp_path / "connectors", connector())
    run = CliRunner()

    out = run.invoke(connector_cli.app, ["compile", str(d), "--out", str(tmp_path / "flows.json")])
    assert out.exit_code == 0, out.output
    assert "connector.social.profile_summary  read  tool social_profile_summary" in out.output
    assert "contract        sha256:" in out.output
    compiled = json.loads((tmp_path / "flows.json").read_text())
    assert compiled["flows"][0]["id"] == "connector.social.profile_summary"

    out = run.invoke(connector_cli.app, ["keygen", str(tmp_path / "key.pem")])
    assert out.exit_code == 0, out.output
    kid, pub = out.output.split()
    assert run.invoke(connector_cli.app, ["keygen", str(tmp_path / "key.pem")]).exit_code == 1  # never overwrite

    out = run.invoke(connector_cli.app, ["sign", str(d), "--key", str(tmp_path / "key.pem"),
                                         "--out", str(tmp_path / "social.bundle")])
    assert out.exit_code == 0, out.output
    assert kid in out.output

    out = run.invoke(connector_cli.app, ["install", str(tmp_path / "social.bundle")])
    assert out.exit_code == 1 and "does not trust" in out.output
    out = run.invoke(connector_cli.app, ["install", str(tmp_path / "social.bundle"), "--trust", pub])
    assert out.exit_code == 0, out.output
    assert "connector.social.profile_summary" in out.output and "installed" in out.output

    out = run.invoke(connector_cli.app, ["list"])
    assert out.exit_code == 0 and f"signed {kid}" in out.output
    out = run.invoke(connector_cli.app, ["install", str(d)])  # same content from the repo directory
    assert out.exit_code == 0 and "unchanged" in out.output


def test_cli_compile_reports_errors(tmp_path):
    doc = connector()
    doc["operations"]["profile_summary"]["effect"] = "pay"
    d = write_dir(tmp_path, doc)
    out = CliRunner().invoke(connector_cli.app, ["compile", str(d)])
    assert out.exit_code == 1 and "out of the v1 catalog" in out.output


def test_private_keys_round_trip_as_pem(tmp_path):
    from openajar.runner.connectors.signing import load_private_key

    (tmp_path / "k.pem").write_bytes(private_pem(TEST_KEY))
    assert key_id(load_private_key(tmp_path / "k.pem")) == key_id(TEST_KEY)
