"""The S3 target: SigV4 against AWS's published examples, and a round trip with a fake S3."""

from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from typer.testing import CliRunner

from agentos.core.s3 import Credentials, S3Error, S3Target, sign

AWS = Credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
WHEN = datetime(2013, 5, 24, tzinfo=UTC)
EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
HOST = "examplebucket.s3.amazonaws.com"


def signature(headers):
    return headers["Authorization"].split("Signature=")[1]


def test_sigv4_get_object_example():
    h = sign("GET", HOST, "/test.txt", {}, {"Range": "bytes=0-9"}, EMPTY, AWS, "us-east-1", WHEN)
    assert signature(h) == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in h["Authorization"]


def test_sigv4_put_object_example():
    body_hash = "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"
    h = sign("PUT", HOST, "/test$file.text", {},
             {"Date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
             body_hash, AWS, "us-east-1", WHEN)
    assert signature(h) == "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"


def test_sigv4_list_and_subresource_examples():
    h = sign("GET", HOST, "/", {"max-keys": "2", "prefix": "J"}, {}, EMPTY, AWS, "us-east-1", WHEN)
    assert signature(h) == "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"
    h = sign("GET", HOST, "/", {"lifecycle": ""}, {}, EMPTY, AWS, "us-east-1", WHEN)
    assert signature(h) == "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"


def test_credentials_never_show():
    assert "wJal" not in repr(AWS) and "AKIA" not in repr(AWS)


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.auth_seen: list[str] = []
        store = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _key(self):
                path = unquote(urlsplit(self.path).path)
                bucket, _, key = path.lstrip("/").partition("/")
                assert bucket == "bkt"
                return key

            def _reply(self, code, body=b"", headers=None):
                self.send_response(code)
                headers = {"Content-Length": str(len(body)), **(headers or {})}
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _check(self):
                auth = self.headers.get("Authorization", "")
                store.auth_seen.append(auth)
                return auth.startswith("AWS4-HMAC-SHA256 Credential=TESTKEY/")

            def do_PUT(self):
                if not self._check():
                    return self._reply(403, b"<Error><Code>AccessDenied</Code></Error>")
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if hashlib.sha256(body).hexdigest() != self.headers["x-amz-content-sha256"]:
                    return self._reply(400, b"<Error><Code>XAmzContentSHA256Mismatch</Code></Error>")
                store.objects[self._key()] = body
                self._reply(200)

            def do_HEAD(self):
                key = self._key()
                if key not in store.objects:
                    return self._reply(404)
                self._reply(200, headers={"Content-Length": str(len(store.objects[key]))})

            def do_GET(self):
                self._check()
                query = parse_qs(urlsplit(self.path).query)
                if "list-type" in query:
                    prefix = query.get("prefix", [""])[0]
                    items = "".join(f"<Contents><Key>{k}</Key><Size>{len(v)}</Size><LastModified>x</LastModified>"
                                    f"</Contents>" for k, v in sorted(store.objects.items()) if k.startswith(prefix))
                    return self._reply(200, f"<ListBucketResult>{items}<IsTruncated>false</IsTruncated>"
                                            f"</ListBucketResult>".encode())
                key = self._key()
                if key not in store.objects:
                    return self._reply(404, b"<Error><Code>NoSuchKey</Code></Error>")
                self._reply(200, store.objects[key])

            def do_DELETE(self):
                store.objects.pop(self._key(), None)
                self._reply(204)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "TESTSECRET-do-not-print")
    monkeypatch.setenv("AWS_ENDPOINT_URL", fake.endpoint)
    monkeypatch.setenv("AWS_REGION", "auto")
    yield fake
    fake.close()


def test_target_put_head_get_list_delete(s3, tmp_path):
    t = S3Target("s3://bkt/agentos/node-1")
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 3000)
    t.put("a.bin", src)
    assert s3.objects == {"agentos/node-1/a.bin": b"x" * 3000}
    assert t.head("a.bin") == 3000 and t.head("missing") is None
    t.get("a.bin", tmp_path / "back.bin")
    assert (tmp_path / "back.bin").read_bytes() == b"x" * 3000
    assert [o.key for o in t.list()] == ["a.bin"]
    t.delete("a.bin")
    assert t.list() == []
    with pytest.raises(S3Error, match="NoSuchKey"):
        t.get("a.bin", tmp_path / "x")


def test_missing_credentials_and_bad_url(monkeypatch, tmp_path):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    with pytest.raises(S3Error, match="no S3 credentials"):
        S3Target("s3://b/p", home=tmp_path)
    with pytest.raises(S3Error, match="not an s3"):
        S3Target("https://b/p", env={})


def test_credentials_file_must_be_private(tmp_path):
    (tmp_path / "secrets").mkdir()
    f = tmp_path / "secrets" / "backup-s3.json"
    f.write_text('{"access_key_id": "K", "secret_access_key": "S", "endpoint": "http://127.0.0.1:9"}')
    f.chmod(0o644)
    with pytest.raises(S3Error, match="600"):
        S3Target("s3://b/p", home=tmp_path, env={})
    f.chmod(0o600)
    t = S3Target("s3://b/p", home=tmp_path, env={})
    assert t.path_style and t.host == "127.0.0.1:9"


def test_cli_backup_to_s3_round_trip(s3, tmp_path, monkeypatch):
    from test_ops_support import PASSPHRASE, make_home

    from agentos.ops import backup, cli

    cfg = make_home(tmp_path)
    monkeypatch.setenv("AGENTOS_HOME", str(cfg.home))
    monkeypatch.setattr(backup, "create_backup",
                        lambda *a, **kw: _real_create(*a, **{**kw, "log_n": 10}))
    pw = tmp_path / "pw"
    pw.write_text(PASSPHRASE + "\n")
    pw.chmod(0o600)
    runner = CliRunner()
    r = runner.invoke(cli.app, ["backup", "create", "--to", "s3://bkt/agentos", "--passphrase-file", str(pw)])
    assert r.exit_code == 0, r.output
    assert "Backup written: s3://bkt/agentos/agentos-test-node-" in r.output
    (key,) = s3.objects
    assert key.startswith("agentos/agentos-test-node-")
    assert not list((cfg.home / "backups" / "outgoing").glob("*.age"))  # the local copy is gone
    r = runner.invoke(cli.app, ["backup", "list", "--to", "s3://bkt/agentos"])
    assert key.split("/")[-1] in r.output
    r = runner.invoke(cli.app, ["backup", "verify", f"s3://bkt/{key}", "--passphrase-file", str(pw)])
    assert r.exit_code == 0 and "checksums match" in r.output
    target = tmp_path / "restored"
    r = runner.invoke(cli.app, ["backup", "restore", f"s3://bkt/{key}", "--to", str(target),
                                "--passphrase-file", str(pw)])
    assert r.exit_code == 0, r.output
    assert (target / "lanes/personal/reddit/profile/cookies.sqlite").exists()
    assert "TESTSECRET" not in r.output and all("TESTSECRET" not in a for a in s3.auth_seen)


_real_create = __import__("agentos.ops.backup", fromlist=["create_backup"]).create_backup
