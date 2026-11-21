"""Site API keys: what is stored, what the grant is, who may touch one, and the limits around them."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from openapi_spec_validator import OpenAPIV31SpecValidator
from siteapi_http_support import API, OTHER, OWNER, READ_PATH, SURFACE, TOKEN, Node
from typer.testing import CliRunner

from openajar.core import layout, migrate
from openajar.core.errors import InvalidRequest, PolicyDenied
from openajar.core.models import Principal
from openajar.runner.siteapis import http_migrations
from openajar.runner.siteapis.cli import app as site_api_app
from openajar.runner.siteapis.http_store import HttpStore
from openajar.runner.siteapis.keys import KEY_RE, SUBJECT, KeyGate, token_hash

AGENT = Principal(kind="agent", id="claude-code")
ADMIN_LIKE = {"kind": "local", "id": "owner", "installed_by": "owner", "owner": "owner"}


@pytest.fixture
async def node(tmp_path):
    async with Node(tmp_path) as n:
        yield n


async def test_a_key_is_256_random_bits_shown_once_and_stored_as_a_hash(node):
    row, key = node.key_row()
    assert KEY_RE.fullmatch(key) and row.prefix == key[:8] and row.hash == token_hash(key) and len(row.hash) == 64
    other = node.key()
    assert other != key
    stored = node.site_http.store.key(row.id)
    assert key not in str(stored) and "hash" not in row.public()
    assert node.keys.lookup(key).id == row.id
    assert node.keys.lookup(key[:-1] + ("A" if key[-1] != "A" else "B")) is None and node.keys.lookup("aos_short") is None


async def test_the_backing_grant_is_narrowed_to_the_key_and_has_no_token(node):
    row, _ = node.key_row(surfaces=[SURFACE], ceiling="draft")
    g = node.policy.grant_by_id(row.grant_id)
    assert g.subject.kind == "agent" and g.subject.id == SUBJECT + row.id
    assert g.lane_allowed(SURFACE) and not g.lane_allowed(OTHER)
    assert g.domain_allowed("portal.northsupply.example") and not g.domain_allowed("elsewhere.example")
    assert g.ceiling.value == "draft" and not g.admin
    assert g.blocks[0].specials is None or not g.blocks[0].specials.model_dump(exclude_none=True)
    record = node.policy.grant_record(row.grant_id)
    assert record["subject"]["id"] == SUBJECT + row.id and "agt" not in str(record)
    home = node.config.home
    for path in home.rglob("*.db*"):  # no grant token anywhere a person or a process could read one back
        assert b"agt2_" not in path.read_bytes(), path


async def test_a_key_with_no_surface_named_covers_only_what_is_bound_today(node):
    row, _ = node.key_row()
    g = node.policy.grant_by_id(row.grant_id)
    assert g.lane_allowed(SURFACE) and g.lane_allowed(OTHER) and not g.lane_allowed("harbor/later")


@pytest.mark.parametrize("ceiling", ["pay", "irreversible", "secret", "exec", "nonsense"])
async def test_the_ceiling_stops_at_commit(node, ceiling):
    with pytest.raises(InvalidRequest, match="ceiling"):
        node.key(ceiling=ceiling)
    assert node.keys.all() == []


async def test_a_key_is_scoped_to_things_that_exist(node):
    for bad in ({"apis": ["nope"]}, {"apis": []}, {"endpoints": ["nope"]}, {"surfaces": ["harbor/nope"]}, {"surfaces": []},
                {"label": ""}, {"label": "x" * 200}, {"rate_per_min": 0}, {"rate_per_min": True},
                {"expires_at": "2020-01-01T00:00:00Z"}, {"expires_at": "soon"}, {"expires_at": "2099-01-01T00:00:00"}):
        with pytest.raises(InvalidRequest):
            node.key(**bad)
    row, _ = node.key_row(endpoints=["open_orders"])
    assert row.endpoints == [f"{API}.open_orders"]


async def test_only_a_person_makes_edits_or_revokes_a_key(node):
    row, _ = node.key_row()
    member = Principal(kind="human", id="m_abc", owner="m_abc")
    for by in (AGENT, member, ADMIN_LIKE, None, {"kind": "human", "id": "owner", "owner": "someone"}):
        with pytest.raises(PolicyDenied):
            node.keys.add(label="x", apis=[API], by=by)
        with pytest.raises(PolicyDenied):
            node.keys.edit(row.id, label="y", by=by)
        with pytest.raises(PolicyDenied):
            node.keys.revoke(row.id, by=by)
    assert len(node.keys.all()) == 1 and node.keys.get(row.id).revoked_at is None


async def test_the_owner_routes_take_a_signed_in_owner_and_no_token(node):
    body = {"label": "ci", "apis": [API]}
    admin = node.grant(subject={"id": "root"}, admin=True, workspaces=["*"])
    for token in (TOKEN, admin):
        async with node.client(token) as c:
            assert (await c.post("/api/site-keys", json=body)).status_code == 403
            assert (await c.get("/api/site-keys")).status_code == 200  # reading stays with the owner routes' rule
    async with node.client(node.grant()) as c:
        assert (await c.get("/api/site-keys")).status_code == 403
        assert (await c.post("/api/site-keys", json=body)).status_code == 403
    assert node.keys.all() == []
    async with node.client() as c:
        assert (await c.post("/api/site-keys", json=body)).status_code == 401
        csrf = await node.session(c)
        assert (await c.post("/api/site-keys", json=body)).status_code == 403  # the CSRF header
        r = await c.post("/api/site-keys", json=body, headers=csrf)
        assert r.status_code == 201, r.text
        made = r.json()
        assert KEY_RE.fullmatch(made["secret"]) and made["key"]["prefix"] == made["secret"][:8]
        listed = (await c.get("/api/site-keys", headers=csrf)).json()["keys"]
        assert made["secret"] not in str(listed) and listed[0]["state"] == "active" and "hash" not in listed[0]
        for bad in ({"by": "someone"}, {"key_id": "x"}, {"grant_id": "x"}):
            assert (await c.patch(f"/api/site-keys/{made['key']['id']}", json=bad, headers=csrf)).status_code == 400
        r = await c.patch(f"/api/site-keys/{made['key']['id']}", json={"label": "ci 2"}, headers=csrf)
        assert r.status_code == 200 and r.json()["label"] == "ci 2"
        r = await c.delete(f"/api/site-keys/{made['key']['id']}", headers=csrf)
        assert r.status_code == 200 and r.json()["state"] == "revoked"
    traces, _ = node.policy.traces.query(kind="panel", limit=50)
    assert {"panel.site_key.create", "panel.site_key.edit", "panel.site_key.revoke"} <= {t.tool for t in traces}
    assert made["secret"] not in str([t.model_dump() for t in traces])


async def test_revoking_a_key_revokes_its_grant_in_the_same_step(node):
    row, key = node.key_row()
    node.keys.revoke(row.id, by=OWNER)
    assert node.policy.grant_record(row.grant_id)["state"] == "revoked"
    assert node.keys.state(node.keys.get(row.id)) == "revoked"
    async with node.client(key) as c:
        assert (await c.get(READ_PATH, params={"on": SURFACE})).status_code == 401


async def test_a_key_stops_even_when_its_row_could_not_be_written(node, monkeypatch):
    row, key = node.key_row(surfaces=[SURFACE])
    monkeypatch.setattr(node.site_http.store, "update_key", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        node.keys.revoke(row.id, by=OWNER)
    monkeypatch.undo()
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 401  # the grant went first


async def test_a_grant_revoked_on_the_grants_page_ends_the_key(node):
    row, key = node.key_row(surfaces=[SURFACE])
    node.policy.revoke(row.grant_id, by="owner")
    assert node.keys.state(row) == "grant_ended"
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 401


async def test_an_edit_of_scope_is_a_new_grant_and_the_old_one_ends(node):
    row, key = node.key_row()
    new = node.keys.edit(row.id, surfaces=[OTHER], by=OWNER)
    assert new.grant_id != row.grant_id and node.policy.grant_record(row.grant_id)["state"] == "revoked"
    assert not node.policy.grant_by_id(new.grant_id).lane_allowed(SURFACE)
    same = node.keys.edit(row.id, label="renamed", rate_per_min=5, by=OWNER)
    assert same.grant_id == new.grant_id and same.rate_per_min == 5
    assert node.keys.lookup(key).id == row.id


async def test_a_key_that_outlives_its_grant_gets_a_new_one_before_the_end(node):
    row, _ = node.key_row(surfaces=[SURFACE])
    node.keys.clock, real = (lambda: real() + 340 * 86400), node.keys.clock
    node.policy.clock, policy_real = (lambda: policy_real() + 340 * 86400), node.policy.clock
    renewed = node.keys.used(row)
    node.keys.clock, node.policy.clock = real, policy_real
    assert renewed.grant_id != row.grant_id and node.policy.grant_record(row.grant_id)["state"] == "active"
    assert node.policy.grant_by_id(renewed.grant_id).subject.id == SUBJECT + row.id


def test_a_keys_database_from_before_renewals_kept_grants_opens_with_none_remembered(tmp_path):
    first = http_migrations.MigrationSet(package=http_migrations.MIGRATIONS.package, db=http_migrations.DB, create=True,
                                         migrations=http_migrations.MIGRATIONS.migrations[:1])
    migrate.migrate(tmp_path, [first], backup=False)
    with sqlite3.connect(tmp_path / http_migrations.DB) as db:
        db.execute("INSERT INTO site_api_keys (id, label, prefix, hash, apis, ceiling, rate_per_min, grant_id, created_at, "
                   "created_by) VALUES ('key_1', 'n8n', 'aos_ab12', 'x', '[]', 'read', 60, 'gr_1', 1.0, 'owner')")
    store = HttpStore(tmp_path)
    assert store.key("key_1")["old_grants"] == {}
    store.close()


def test_root_making_the_keys_database_gives_its_directory_to_the_service_user(tmp_path, monkeypatch):
    given = []
    monkeypatch.setattr(layout.os, "geteuid", lambda: 0)
    monkeypatch.setattr(layout.os, "chown", lambda path, *a, **kw: given.append(Path(path)))
    HttpStore(tmp_path).close()
    assert {tmp_path / "siteapis", tmp_path / "siteapis/http.db"} <= set(given)


async def test_failed_lookups_are_limited_per_source_and_retry_after_says_when(node):
    node.site_http.gate = KeyGate(failed_per_source=3)
    key = node.key(surfaces=[SURFACE])
    async with node.client("aos_" + "A" * 43) as c:
        for _ in range(3):
            assert (await c.get(READ_PATH)).status_code == 401
        r = await c.get(READ_PATH)
    assert r.status_code == 429 and r.json()["code"] == "rate_limited_key" and int(r.headers["retry-after"]) > 0
    async with node.client(key) as c:  # this source tried too often: a right guess must not get through either
        assert (await c.get(READ_PATH)).status_code == 429
    assert node.site_http.gate.failed.blocked("10.0.0.9") is None


async def test_a_call_with_no_key_at_all_costs_no_tries(node):
    node.site_http.gate = KeyGate(failed_per_source=2)
    async with node.client() as c:
        for _ in range(5):
            assert (await c.get(READ_PATH)).status_code == 401


async def test_a_spray_of_sources_cannot_grow_memory_without_bound():
    gate = KeyGate(failed_everyone=50)
    for i in range(500):
        gate.failed.check(f"10.{i // 250}.{i % 250}.1")
    assert sum(1 for q in gate.failed._hits.values() if q) == 50


async def test_the_key_limit_has_ratelimit_headers_and_a_429_has_retry_after(node):
    key = node.key(rate_per_min=2, surfaces=[SURFACE])
    async with node.client(key) as c:
        first = await c.get(READ_PATH)
        assert first.headers["ratelimit-limit"] == "2" and first.headers["ratelimit-remaining"] == "1"
        assert int(first.headers["ratelimit-reset"]) > 0
        await c.get(READ_PATH)
        r = await c.get(READ_PATH)
    assert r.status_code == 429 and r.json()["code"] == "rate_limited_key"
    assert r.headers["ratelimit-remaining"] == "0" and int(r.headers["retry-after"]) > 0


def test_a_refused_call_costs_nothing_and_the_window_slides():
    now = [100.0]
    gate = KeyGate(clock=lambda: now[0])
    assert gate.take("k", 1).remaining == 0
    assert gate.take("k", 1).retry_after == 60
    now[0] = 130.0
    assert gate.take("k", 1).retry_after == 30
    now[0] = 161.0
    assert gate.take("k", 1).retry_after is None


def test_places_among_the_waiters_are_refused_not_queued_and_come_back():
    gate = KeyGate(waiters_per_key=2, waiters_all=3)
    assert gate.enter("a") and gate.enter("a") and not gate.enter("a")
    assert gate.enter("b") and not gate.enter("c")
    gate.leave("a")
    assert gate.enter("c")


def test_the_keys_database_is_found_by_the_migrator_and_so_by_a_backup():
    assert any(s.package == "siteapis_http" and s.db == "siteapis/http.db" for s in migrate.discover())


# ---- the CLI ----

def owner_cli(node, *args):
    return CliRunner().invoke(site_api_app, list(args), env={"OPENAJAR_HOME": str(node.config.home)})


async def test_keys_add_list_and_revoke_from_the_cli(node):
    r = owner_cli(node, "keys", "add", "cron job", "--api", API, "--surface", SURFACE, "--rate", "5")
    assert r.exit_code == 0, r.output
    secret = r.output.strip().splitlines()[-1]
    assert KEY_RE.fullmatch(secret) and "—" not in r.output
    async with node.client(secret) as c:
        assert (await c.get(READ_PATH)).status_code == 200
    listed = owner_cli(node, "keys", "list")
    assert "cron job" in listed.output and secret[:8] in listed.output and secret not in listed.output
    key_id = listed.output.split()[0]
    assert owner_cli(node, "keys", "revoke", key_id).exit_code == 0
    async with node.client(secret) as c:
        assert (await c.get(READ_PATH)).status_code == 401
    assert owner_cli(node, "keys", "add", "x", "--api", "nope").exit_code == 1
    assert owner_cli(node, "keys", "add", "x", "--api", API, "--ceiling", "pay").exit_code == 1


async def test_the_openapi_command_prints_a_valid_document(node, tmp_path):
    r = owner_cli(node, "openapi", API, "--server", "https://node.example")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    assert doc["servers"] == [{"url": "https://node.example"}] and not list(OpenAPIV31SpecValidator(doc).iter_errors())
    assert owner_cli(node, "openapi", "nope").exit_code == 1
    out = tmp_path / "doc.json"
    assert owner_cli(node, "openapi", API, "--out", str(out)).exit_code == 0 and json.loads(out.read_text())
