"""A minimal S3 client for `openajar backup ... --to s3://bucket/prefix` (AWS Signature V4).

Enough for backups and nothing more: PUT a file, GET it back, HEAD, LIST (with paging) and
DELETE, against AWS S3 or an S3-compatible store (Cloudflare R2, Backblaze B2, MinIO, ...).

Credentials, in order:
  1. AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY (and AWS_SESSION_TOKEN) in the environment
  2. OPENAJAR_HOME/secrets/backup-s3.json (chmod 600):
       {"access_key_id": "...", "secret_access_key": "...", "region": "auto",
        "endpoint": "https://<account>.r2.cloudflarestorage.com"}
Region: AWS_REGION, AWS_DEFAULT_REGION, the file, else us-east-1. Endpoint: AWS_ENDPOINT_URL_S3,
AWS_ENDPOINT_URL, the file, else AWS (virtual-hosted style); a custom endpoint uses path style.

Single PUTs only, so one archive can be up to 5 GiB. Nothing here logs a key or a signature.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import stat
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from openajar.core.errors import OpenajarError

MAX_PUT = 5 * 1024**3
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class S3Error(OpenajarError):
    code = "storage_error"


@dataclass(frozen=True, repr=False)
class Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None

    def __repr__(self) -> str:
        return "Credentials(***)"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _enc(value: str, safe: str = "-_.~") -> str:
    return quote(value, safe=safe)


def sign(method: str, host: str, path: str, query: Mapping[str, str], headers: Mapping[str, str],
         payload_hash: str, creds: Credentials, region: str, now: datetime, service: str = "s3") -> dict[str, str]:
    """Headers to send: the given ones plus x-amz-date, x-amz-content-sha256, the session token
    and Authorization. `path` is the raw (unencoded) object path, starting with '/'."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    out = {k: v for k, v in headers.items()}
    out["host"] = host
    out["x-amz-date"] = amz_date
    out["x-amz-content-sha256"] = payload_hash
    if creds.session_token:
        out["x-amz-security-token"] = creds.session_token
    lowered = {k.lower(): " ".join(str(v).strip().split()) for k, v in out.items()}
    signed = sorted(lowered)
    canonical = "\n".join([
        method,
        "/".join(_enc(seg) for seg in path.split("/")),
        "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(query.items())),
        "".join(f"{k}:{lowered[k]}\n" for k in signed),
        ";".join(signed),
        payload_hash,
    ])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{creds.secret_access_key}".encode(), day), region), service),
                "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={creds.access_key_id}/{scope}, "
                            f"SignedHeaders={';'.join(signed)}, Signature={signature}")
    return out


def _file_config(home: Path | None) -> dict[str, str]:
    if home is None:
        return {}
    path = Path(home) / "secrets" / "backup-s3.json"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {}
    if stat.S_ISLNK(st.st_mode) or st.st_mode & 0o077:
        raise S3Error(f"{path} must be a regular file with permissions 600 (run: chmod 600 {path})")
    try:
        data = json.loads(path.read_text())
    except ValueError:
        raise S3Error(f"{path} is not valid JSON") from None
    if not isinstance(data, dict):
        raise S3Error(f"{path} must hold a JSON object")
    return {k: str(v) for k, v in data.items() if v is not None}


@dataclass
class S3Object:
    key: str
    size: int
    last_modified: str


class S3Target:
    def __init__(self, url: str, *, home: Path | None = None, env: Mapping[str, str] | None = None,
                 timeout: float = 60.0):
        env = os.environ if env is None else env
        parts = urlsplit(url)
        if parts.scheme != "s3" or not parts.netloc:
            raise S3Error(f"not an s3:// URL: {url}")
        self.bucket = parts.netloc
        self.prefix = parts.path.strip("/")
        self.url = f"s3://{self.bucket}/{self.prefix}".rstrip("/")
        cfg = _file_config(home)
        key_id = env.get("AWS_ACCESS_KEY_ID") or cfg.get("access_key_id")
        secret = env.get("AWS_SECRET_ACCESS_KEY") or cfg.get("secret_access_key")
        if not key_id or not secret:
            raise S3Error("no S3 credentials: set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, or write "
                          "secrets/backup-s3.json (chmod 600)")
        self.creds = Credentials(key_id, secret, env.get("AWS_SESSION_TOKEN") or cfg.get("session_token"))
        self.region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or cfg.get("region") or "us-east-1"
        endpoint = env.get("AWS_ENDPOINT_URL_S3") or env.get("AWS_ENDPOINT_URL") or cfg.get("endpoint")
        if endpoint:
            e = urlsplit(endpoint)
            self.scheme, self.host, self.path_style = e.scheme or "https", e.netloc, True
        else:
            self.scheme, self.host, self.path_style = "https", f"{self.bucket}.s3.{self.region}.amazonaws.com", False
        self.timeout = timeout

    def key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def location(self, name: str) -> str:
        return f"s3://{self.bucket}/{self.key(name)}"

    def _path(self, key: str = "") -> str:
        return f"/{self.bucket}/{key}" if self.path_style else f"/{key}"

    def _request(self, method: str, key: str = "", *, query: Mapping[str, str] | None = None,
                 body_path: Path | None = None, body: bytes = b"", sink: Path | None = None,
                 headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        query = dict(query or {})
        path = self._path(key)
        extra = dict(headers or {})
        if body_path is not None:
            size = body_path.stat().st_size
            if size > MAX_PUT:
                raise S3Error(f"{body_path.name} is {size // 1024**3} GiB; single PUTs stop at 5 GiB. Back up to a "
                              "directory and copy it with your own S3 tool.")
            digest = hashlib.sha256()
            with open(body_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    digest.update(chunk)
            payload_hash = digest.hexdigest()
            extra["content-length"] = str(size)
        else:
            payload_hash = hashlib.sha256(body).hexdigest()
            if body or method in ("PUT", "POST"):
                extra["content-length"] = str(len(body))
        signed = sign(method, self.host, path, query, extra, payload_hash, self.creds, self.region, datetime.now(UTC))
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = cls(self.host, timeout=self.timeout)
        target = "/".join(_enc(seg) for seg in path.split("/"))
        if query:
            target += "?" + "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(query.items()))
        try:
            conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            for k, v in signed.items():
                conn.putheader(k, v)
            conn.endheaders()
            if body_path is not None:
                with open(body_path, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        conn.send(chunk)
            elif body:
                conn.send(body)
            resp = conn.getresponse()
            rheaders = {k.lower(): v for k, v in resp.getheaders()}
            if sink is not None and resp.status == 200:
                tmp = sink.with_name(f".{sink.name}.download")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as out:
                    while chunk := resp.read(1024 * 1024):
                        out.write(chunk)
                tmp.replace(sink)
                return resp.status, rheaders, b""
            return resp.status, rheaders, resp.read()
        except OSError as e:
            raise S3Error(f"cannot reach {self.scheme}://{self.host}: {type(e).__name__}") from None
        finally:
            conn.close()

    @staticmethod
    def _error(status: int, body: bytes, what: str) -> S3Error:
        code = ""
        try:
            root = ET.fromstring(body)
            code = root.findtext("Code") or root.findtext(f"{_NS}Code") or ""
        except ET.ParseError:
            pass
        return S3Error(f"{what} failed: HTTP {status}{f' {code}' if code else ''}")

    def put(self, name: str, path: Path) -> None:
        status, _, body = self._request("PUT", self.key(name), body_path=Path(path),
                                        headers={"content-type": "application/octet-stream"})
        if status != 200:
            raise self._error(status, body, f"upload of {name}")

    def head(self, name: str) -> int | None:
        status, headers, _ = self._request("HEAD", self.key(name))
        if status == 404:
            return None
        if status != 200:
            raise S3Error(f"checking {name} failed: HTTP {status}")
        return int(headers.get("content-length", "0"))

    def get(self, name: str, dest: Path) -> None:
        status, _, body = self._request("GET", self.key(name), sink=Path(dest))
        if status != 200:
            raise self._error(status, body, f"download of {name}")

    def delete(self, name: str) -> None:
        status, _, body = self._request("DELETE", self.key(name))
        if status not in (200, 204):
            raise self._error(status, body, f"delete of {name}")

    def list(self) -> list[S3Object]:
        out: list[S3Object] = []
        token: str | None = None
        prefix = f"{self.prefix}/" if self.prefix else ""
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            status, _, body = self._request("GET", "", query=query)
            if status != 200:
                raise self._error(status, body, f"listing {self.url}")
            root = ET.fromstring(body)
            for item in root.iter():
                if item.tag.endswith("Contents"):
                    key = item.findtext(f"{_NS}Key") or item.findtext("Key") or ""
                    size = item.findtext(f"{_NS}Size") or item.findtext("Size") or "0"
                    when = item.findtext(f"{_NS}LastModified") or item.findtext("LastModified") or ""
                    out.append(S3Object(key[len(prefix):], int(size), when))
            truncated = (root.findtext(f"{_NS}IsTruncated") or root.findtext("IsTruncated") or "false") == "true"
            token = root.findtext(f"{_NS}NextContinuationToken") or root.findtext("NextContinuationToken")
            if not truncated or not token:
                return out
