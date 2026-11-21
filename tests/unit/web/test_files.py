"""An account's files on a node, as the account page reads them: the inbox and the outbox, a download, an upload
and a delete. They are the tools' own store (openajar.node.files), so the same limits and the same refusals hold.

An upload is only stored. A download is an attachment that is never rendered, and no path leaves the account.
"""

from __future__ import annotations

import base64

import pytest

from openajar.node import files as node_files

TOKEN = "t" * 43
LANE = "/api/orgs/local/lanes/biz-a/social/files"
# a real PNG, one pixel: the sniffer judges the bytes, not the name
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


@pytest.fixture(autouse=True)
def account_folder(node):
    """A lane's folder is made when the lane is, as the node does; its files live inside it."""
    node.get("config").lane_dir("biz-a/social").mkdir(parents=True, exist_ok=True)


def _upload(client, body: bytes, name: str, content_type: str = "image/png"):
    return client.post(LANE, params={"name": name}, content=body, headers={"content-type": content_type})


def test_the_account_lists_its_inbox_outbox_and_the_limits(client) -> None:
    listing = client.get(LANE).json()
    assert listing["lane"] == "biz-a/social"
    assert listing["inbox"] == [] and listing["outbox"] == []
    limits = listing["limits"]
    assert limits["put_bytes"] == node_files.FilesConfig().max_bytes
    assert limits["download_bytes"] == node_files.FilesConfig().download_max_bytes
    assert limits["put"].endswith("MB") and limits["download"].endswith("MB")


def test_an_upload_is_stored_in_the_inbox_and_listed_as_complete(client) -> None:
    out = _upload(client, PNG, "photo.png")
    assert out.status_code == 200, out.text
    stored = out.json()["file"]
    assert stored["id"].startswith("in_") and stored["box"] == "inbox"
    assert stored["status"] == "complete" and stored["kind"] == "image" and stored["mime"] == "image/png"
    assert stored["size"] == len(PNG) and stored["size_text"].endswith("bytes")
    [listed] = client.get(LANE).json()["inbox"]
    assert listed["id"] == stored["id"] and listed["name"] == "photo.png"


def test_the_name_is_cleaned_so_it_cannot_climb_out_of_the_inbox(client) -> None:
    out = _upload(client, PNG, "../../../etc/photo.png")
    assert out.status_code == 200, out.text
    assert out.json()["file"]["name"] == "photo.png"


def test_a_web_page_or_an_unknown_type_is_refused_by_its_bytes(client) -> None:
    page = _upload(client, b"<!doctype html><html><body>hi</body></html>", "page.png")
    assert page.status_code == 400 and "web page" in page.json()["error"]["message"]
    blob = _upload(client, b"\x00\x01\x02 not a known type \xff\xfe", "blob.png", "application/octet-stream")
    assert blob.status_code == 400
    assert client.get(LANE).json()["inbox"] == [], "a refused file leaves nothing behind"


def test_an_empty_upload_and_one_without_a_name_are_refused(client) -> None:
    assert _upload(client, b"", "empty.png").status_code == 400
    assert client.post(LANE, content=PNG, headers={"content-type": "image/png"}).status_code == 400


def test_an_upload_over_the_limit_is_refused_before_it_is_stored(client, monkeypatch) -> None:
    small = node_files.FilesConfig(max_bytes=len(PNG) - 1)
    monkeypatch.setattr(node_files.FilesConfig, "from_node", classmethod(lambda cls, config: small))
    out = _upload(client, PNG, "photo.png")
    assert out.status_code == 400
    assert "larger than the" in out.json()["error"]["message"]
    assert client.get(LANE).json()["inbox"] == []


def test_a_download_is_an_attachment_and_is_never_rendered(client) -> None:
    file_id = _upload(client, PNG, "photo.png").json()["file"]["id"]
    out = client.get(f"{LANE}/{file_id}/download")
    assert out.status_code == 200
    assert out.content == PNG
    assert out.headers["content-disposition"] == 'attachment; filename="photo.png"'
    assert out.headers["x-content-type-options"] == "nosniff"
    assert out.headers["content-type"] == "application/octet-stream"


def test_a_download_of_a_missing_or_badly_named_file_is_refused(client) -> None:
    missing = client.get(f"{LANE}/in_0123456789ab/download")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    not_an_id = client.get(f"{LANE}/notes.txt/download")
    assert not_an_id.status_code == 400 and not_an_id.json()["error"]["code"] == "invalid"
    climbing = client.get(f"{LANE}/..%2F..%2Fetc%2Fpasswd/download")
    assert climbing.status_code == 404, "an encoded slash never reaches a file: no route takes it"


def test_a_delete_removes_the_file_from_the_account(client) -> None:
    file_id = _upload(client, PNG, "photo.png").json()["file"]["id"]
    out = client.delete(f"{LANE}/{file_id}")
    assert out.status_code == 200, out.text
    assert out.json()["deleted"]["id"] == file_id
    assert client.get(LANE).json()["inbox"] == []
    assert client.get(f"{LANE}/{file_id}/download").status_code == 404


def test_a_token_cannot_see_upload_or_delete_an_account_file(client) -> None:
    file_id = _upload(client, PNG, "photo.png").json()["file"]["id"]
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    checks = (
        ("GET", LANE, None, {}),
        ("GET", f"{LANE}/{file_id}/download", None, {}),
        ("POST", LANE, PNG, {"params": {"name": "other.png"}, "headers": {"content-type": "image/png"}}),
        ("DELETE", f"{LANE}/{file_id}", None, {}),
    )
    for method, url, body, extra in checks:
        headers = {**bearer, **extra.pop("headers", {})}
        response = client.request(method, url, headers=headers, content=body, **extra)
        assert response.status_code == 403, (method, url, response.text)
        assert response.json()["error"]["code"] == "denied"
    assert [row["id"] for row in client.get(LANE).json()["inbox"]] == [file_id], "nothing changed"


def test_an_unknown_account_is_not_found(client) -> None:
    response = client.get("/api/orgs/local/lanes/biz-a/nowhere/files")
    assert response.status_code == 404
