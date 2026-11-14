"""The safety checks a person turned off, as the app reads them (web/src/features/ask-first/overrides.tsx).
The node keeps them as they were written; the owner reads them under Technical details, and nobody else."""

from __future__ import annotations

ORG = "/api/orgs/local"


def test_the_owner_reads_each_override_in_the_shape_the_app_expects(client) -> None:
    listed = client.get(f"{ORG}/overrides?limit=20")
    assert listed.status_code == 200, listed.text
    for row in listed.json():
        assert set(row) >= {"id", "at", "by", "lane", "proxy", "rules", "reason"}
        assert isinstance(row["rules"], list)


def test_a_stranger_reads_none_of_them(client) -> None:
    saved = dict(client.cookies)
    client.cookies.clear()
    try:
        assert client.get(f"{ORG}/overrides").status_code == 401
    finally:
        client.cookies.update(saved)
