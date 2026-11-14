"""Lane files: names, types, limits, containment, downloads, and URL fetches (no browser)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from agentos.core.errors import EngineError, InvalidRequest, NotFound
from agentos.core.models import ProxyConfig
from agentos.node import files as files_mod
from agentos.node.files import (
    CSV,
    JPEG,
    MB,
    MP4,
    PDF,
    PNG,
    TXT,
    FetchPolicy,
    FilesConfig,
    FileStatus,
    LaneFiles,
    check_fetch_url,
    display_url,
    disposition_filename,
    fit_extension,
    is_private_host,
    resolve_public,
    sanitize_name,
    sniff,
)

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64
PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"


def ftyp(major: bytes, *compatible: bytes) -> bytes:
    body = major + b"\x00\x00\x02\x00" + b"".join(compatible)
    return (8 + len(body)).to_bytes(4, "big") + b"ftyp" + body + b"\x00" * 32


@pytest.fixture
def lane_files(tmp_path: Path) -> LaneFiles:
    lane = tmp_path / "lanes" / "acme" / "instagram"
    lane.mkdir(parents=True, mode=0o700)
    return LaneFiles(lane / "files", FilesConfig(max_bytes=1 * MB, download_max_bytes=2 * MB))


def inbox_entries(lane_files: LaneFiles) -> list[str]:
    inbox = lane_files.root / "inbox"
    return sorted(p.name for p in inbox.iterdir()) if inbox.is_dir() else []


# ---- names --------------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "expected"), [
    ("photo.png", "photo.png"),
    ("../../etc/passwd", "passwd"),
    ("..\\..\\Windows\\win.ini", "win.ini"),
    ("/var/lib/agentos/secrets/proxies.json", "proxies.json"),
    ("C:\\Users\\ada\\Pictures\\launch day.jpg", "launch day.jpg"),
    ("", "file"),
    (None, "file"),
    (".", "file"),
    ("..", "file"),
    ("...", "file"),
    ("/", "file"),
    (".bashrc", "bashrc"),
    ("  spaced   out  name .png ", "spaced out name .png"),
    ("tab\there\nnewline.txt", "tab here newline.txt"),
    ("nul\x00byte\x07bell.png", "nulbytebell.png"),
    ("invoice\u202egpj.exe", "invoicegpj.exe"),          # right-to-left override hides the real extension
    ("zero\u200bwidth.png", "zerowidth.png"),
    ('what<is>:"this"|?*.png', "what_is___this____.png"),
    ("$(rm -rf ~)`id`.png", "_(rm -rf ~)_id_.png"),
    ("CON.txt", "_CON.txt"),
    ("nul", "_nul"),
    ("Café menu.pdf", "Café menu.pdf"),
    ("trailing dots...", "trailing dots"),
])
def test_sanitize_name(raw, expected):
    assert sanitize_name(raw) == expected


def test_sanitize_name_normalizes_unicode_and_limits_length():
    assert sanitize_name("Cafe\u0301.png") == "Café.png"  # NFC
    long = "a" * 400 + ".jpeg"
    out = sanitize_name(long)
    assert out.endswith(".jpeg") and len(out.encode()) <= files_mod.MAX_NAME_BYTES
    wide = "ž" * 200 + ".png"
    out = sanitize_name(wide)
    assert out.endswith(".png") and len(out.encode()) <= files_mod.MAX_NAME_BYTES


def test_sanitize_name_is_idempotent():
    for raw in ("../x.png", "a\u202eb.pdf", "  x  ", "CON", "é" * 300):
        once = sanitize_name(raw)
        assert sanitize_name(once) == once


@pytest.mark.parametrize(("name", "ftype", "expected"), [
    ("photo.png", PNG, "photo.png"),
    ("photo.PNG", PNG, "photo.PNG"),
    ("photo.jpeg", JPEG, "photo.jpeg"),
    ("photo.png", JPEG, "photo.jpg"),        # the bytes win over the name
    ("photo", JPEG, "photo.jpg"),
    ("evil.exe", PNG, "evil.png"),
    ("clip.mov", MP4, "clip.mp4"),
    ("report.v2.final", PDF, "report.v2.pdf"),
    ("notes.md", TXT, "notes.txt"),
    ("data.csv", CSV, "data.csv"),
    ("archive.tar.gz", PNG, "archive.tar.png"),
    ("version 1.2.3 release notes", TXT, "version 1.2.3 release notes.txt"),
])
def test_fit_extension(name, ftype, expected):
    assert fit_extension(name, ftype) == expected


# ---- types --------------------------------------------------------------------------------

@pytest.mark.parametrize(("head", "mime"), [
    (PNG_BYTES, "image/png"),
    (JPEG_BYTES, "image/jpeg"),
    (b"GIF89a" + b"\x00" * 20, "image/gif"),
    (b"GIF87a" + b"\x00" * 20, "image/gif"),
    (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image/webp"),
    (b"II*\x00" + b"\x00" * 20, "image/tiff"),
    (b"MM\x00*" + b"\x00" * 20, "image/tiff"),
    (ftyp(b"heic", b"mif1", b"heic"), "image/heic"),
    (ftyp(b"mif1", b"mif1", b"heic"), "image/heic"),
    (ftyp(b"mif1", b"mif1"), "image/heif"),
    (ftyp(b"avif", b"avif", b"mif1", b"miaf"), "image/avif"),
    (ftyp(b"isom", b"isom", b"iso2", b"avc1", b"mp41"), "video/mp4"),
    (ftyp(b"mp42", b"mp42", b"isom"), "video/mp4"),
    (ftyp(b"qt  ", b"qt  "), "video/quicktime"),
    (b"\x00\x00\x00\x08wide\x00\x00\x00\x00mdat", "video/quicktime"),
    (ftyp(b"3gp5", b"3gp5", b"isom"), "video/3gpp"),
    (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm", "video/webm"),
    (b"\x1a\x45\xdf\xa3\x93\x42\x82\x88matroska", "video/x-matroska"),
    (b"RIFF\x24\x00\x00\x00AVI LIST", "video/x-msvideo"),
    (PDF_BYTES, "application/pdf"),
    (b"date,post,likes\n2026-11-01,Launch,42\n", "text/plain"),
    ("Grüße aus Vilnius, ačiū\n".encode(), "text/plain"),
    (b"\xef\xbb\xbfwith a byte order mark\n", "text/plain"),
    (b"PK\x03\x04" + b"\x00" * 26, "application/zip"),
])
def test_sniff_by_magic_bytes(head, mime):
    found = sniff(head)
    assert found is not None and found.mime == mime


def test_sniff_ignores_the_name_except_for_csv():
    assert sniff(PNG_BYTES, "not-really.pdf").mime == "image/png"
    assert sniff(b"a,b\n1,2\n", "table.csv") is CSV
    assert sniff(b"a,b\n1,2\n", "table.txt") is TXT
    assert sniff(b"<html><script>alert(1)</script></html>", "page.html") is TXT  # text; the inbox refuses markup


@pytest.mark.parametrize("head", [
    b"MZ\x90\x00\x03\x00\x00\x00",                    # a Windows executable
    b"\x7fELF\x02\x01\x01\x00",                        # a Linux executable
    ftyp(b"M4A ", b"M4A ", b"mp42", b"isom"),          # audio only
    ftyp(b"crx ", b"crx "),                            # a camera raw we do not know
    b"\x00\x01\x02\x03binary\x00with nuls",
    bytes(range(1, 32)) * 4,                           # control characters, no NUL
    b"\xff\xfe\x00\x00not utf-8 \x80\x81",
    b"",
    b"   \n\t ",
])
def test_sniff_refuses_what_it_does_not_know(head):
    assert sniff(head) is None or sniff(head).kind not in {"image", "video", "pdf", "csv", "txt"}


def test_text_cut_inside_a_multibyte_character_is_still_text():
    sample = ("ą" * 40_000).encode()[: files_mod.SNIFF_BYTES - 1]  # ends half way through a character
    assert sniff(sample) is TXT


# ---- inbox writes and limits --------------------------------------------------------------

def test_put_bytes_stores_privately_with_sniffed_type(lane_files):
    stored = lane_files.put_bytes("../../hero shot.png", JPEG_BYTES)
    assert stored.id.startswith("in_") and stored.box == "inbox"
    assert stored.name == "hero shot.jpg" and stored.mime == "image/jpeg" and stored.kind == "image"
    assert stored.sha256 == hashlib.sha256(JPEG_BYTES).hexdigest()
    path = lane_files.path(stored.id)
    assert path == (lane_files.root / "inbox" / stored.id / "hero shot.jpg").resolve()
    assert path.read_bytes() == JPEG_BYTES
    assert path.stat().st_mode & 0o777 == 0o600
    for d in (lane_files.root, lane_files.root / "inbox", lane_files.root / "inbox" / stored.id):
        assert d.stat().st_mode & 0o777 == 0o700
    meta = json.loads((path.parent / ".meta.json").read_text())
    assert meta["name"] == "hero shot.jpg" and (path.parent / ".meta.json").stat().st_mode & 0o777 == 0o600
    assert [f.id for f in lane_files.list()] == [stored.id]


def test_size_limits(lane_files):
    cap = lane_files.config.max_bytes
    exact = PDF_BYTES + b"0" * (cap - len(PDF_BYTES))
    assert lane_files.put_bytes("max.pdf", exact).size == cap
    with pytest.raises(InvalidRequest, match="larger than the 1.0 MB limit"):
        lane_files.put_bytes("big.pdf", exact + b"0")
    with pytest.raises(InvalidRequest, match="empty"):
        lane_files.put_bytes("nothing.png", b"")


@pytest.mark.parametrize("data", [b"MZ\x90\x00" * 10, b"PK\x03\x04" + b"\x00" * 40, ftyp(b"M4A ", b"M4A ")])
def test_disallowed_types_never_reach_the_inbox(lane_files, data):
    with pytest.raises(InvalidRequest, match="the inbox takes only"):
        lane_files.put_bytes("innocent.png", data)
    assert lane_files.list() == []
    assert inbox_entries(lane_files) == []


@pytest.mark.parametrize("data", [b"<!DOCTYPE html><html><body>Sign in</body></html>", b"  <html lang=en>",
                                  b"\xef\xbb\xbf<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>",
                                  b"<?xml version='1.0'?><svg/>"])
def test_web_pages_and_svg_are_refused_even_though_they_are_text(lane_files, data):
    with pytest.raises(InvalidRequest, match="web page or SVG"):
        lane_files.put_bytes("photo.png", data)


def test_allowed_kinds_are_configurable(tmp_path):
    (tmp_path / "lane").mkdir()
    only_images = LaneFiles(tmp_path / "lane" / "files", FilesConfig(allowed_kinds=frozenset({"image"})))
    only_images.put_bytes("a.png", PNG_BYTES)
    with pytest.raises(InvalidRequest, match="takes only image"):
        only_images.put_bytes("a.pdf", PDF_BYTES)


def test_from_node_reads_optional_limits():
    class Cfg:
        files_max_mb = 50
        downloads_max_mb = 1024

    cfg = FilesConfig.from_node(Cfg())
    assert cfg.max_bytes == 50 * MB and cfg.download_max_bytes == 1024 * MB
    assert FilesConfig.from_node(object()) == FilesConfig()


# ---- ids, traversal and links -------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "../in_0123456789ab", "in_0123456789ab/../../x", "/etc/passwd", "in_123",
                                 "in_0123456789AB", "out_0123456789abc", "x_0123456789ab", "in_0123456789ab\x00"])
def test_file_ids_are_strict(lane_files, bad):
    with pytest.raises(InvalidRequest, match="not a file id"):
        lane_files.get(bad)


def test_unknown_ids_are_not_found(lane_files):
    lane_files.ensure()
    with pytest.raises(NotFound):
        lane_files.get("in_0123456789ab")


def test_a_content_symlink_is_refused(lane_files, tmp_path):
    stored = lane_files.put_bytes("a.png", PNG_BYTES)
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG_BYTES)
    content = lane_files.root / "inbox" / stored.id / "a.png"
    content.unlink()
    content.symlink_to(secret)
    with pytest.raises(InvalidRequest, match="symlink"):
        lane_files.path(stored.id)
    with pytest.raises(InvalidRequest, match="symlink"):
        lane_files.read(stored.id, 100)


def test_a_hard_link_is_refused(lane_files, tmp_path):
    stored = lane_files.put_bytes("a.png", PNG_BYTES)
    os.link(lane_files.root / "inbox" / stored.id / "a.png", tmp_path / "elsewhere.png")
    with pytest.raises(InvalidRequest, match="hard links"):
        lane_files.path(stored.id)


def test_a_symlinked_entry_directory_is_invisible(lane_files, tmp_path):
    stored = lane_files.put_bytes("a.png", PNG_BYTES)
    entry = lane_files.root / "inbox" / stored.id
    outside = tmp_path / "planted"
    entry.rename(outside)
    entry.symlink_to(outside, target_is_directory=True)
    assert lane_files.list() == []
    with pytest.raises(NotFound):
        lane_files.path(stored.id)


def test_a_symlinked_inbox_is_refused(lane_files, tmp_path):
    stored = lane_files.put_bytes("a.png", PNG_BYTES)
    inbox = lane_files.root / "inbox"
    elsewhere = tmp_path / "other-inbox"
    inbox.rename(elsewhere)
    inbox.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(InvalidRequest, match="symlink"):
        lane_files.path(stored.id)
    with pytest.raises(InvalidRequest, match="symlink"):
        lane_files.put_bytes("b.png", PNG_BYTES)
    assert lane_files.list() == []


def test_a_symlinked_files_root_is_refused(tmp_path):
    lane = tmp_path / "lane"
    lane.mkdir()
    (tmp_path / "decoy").mkdir()
    (lane / "files").symlink_to(tmp_path / "decoy", target_is_directory=True)
    with pytest.raises(InvalidRequest, match="symlink"):
        LaneFiles(lane / "files").put_bytes("a.png", PNG_BYTES)


def test_tampered_metadata_cannot_point_outside(lane_files):
    stored = lane_files.put_bytes("a.png", PNG_BYTES)
    meta_path = lane_files.root / "inbox" / stored.id / ".meta.json"
    for bad_name in ("../../../../etc/passwd", "/etc/passwd", "..", ".meta.json"):
        meta = json.loads(meta_path.read_text())
        meta["name"] = bad_name
        meta_path.write_text(json.dumps(meta))
        assert lane_files.list() == []
        with pytest.raises(NotFound):
            lane_files.path(stored.id)
    meta["name"], meta["id"] = "a.png", "in_aaaaaaaaaaaa"
    meta_path.write_text(json.dumps(meta))
    assert lane_files.list() == []  # an id that does not match its directory


def test_inbox_only_where_asked(lane_files, tmp_path):
    entry = lane_files.begin_download("report.csv", "https://example.com/r.csv")
    src = tmp_path / "r.csv"
    src.write_bytes(b"a,b\n")
    lane_files.finish_download(entry.id, src)
    with pytest.raises(InvalidRequest, match="only files in the lane's inbox"):
        lane_files.path(entry.id, box="inbox")
    assert lane_files.path(entry.id).read_bytes() == b"a,b\n"


def test_stale_temporary_entries_are_swept(lane_files):
    lane_files.ensure()
    stale = lane_files.root / "inbox" / ".tmp-in_0123456789ab"
    stale.mkdir()
    old = time.time() - files_mod.STALE_TMP_SECONDS - 10
    os.utime(stale, (old, old))
    fresh = lane_files.root / "inbox" / ".tmp-in_ba9876543210"
    fresh.mkdir()
    lane_files.ensure()
    assert not stale.exists() and fresh.exists()


# ---- outbox -------------------------------------------------------------------------------

def test_download_lifecycle(lane_files, tmp_path):
    entry = lane_files.begin_download("../Invoice 2026-10?.pdf", "https://billing.example.com/inv.pdf?sig=SECRET#x")
    assert entry.status is FileStatus.DOWNLOADING and entry.id.startswith("out_")
    assert entry.name == "Invoice 2026-10_.pdf"
    assert entry.url == "https://billing.example.com/inv.pdf"  # no signed query
    assert lane_files.get(entry.id).status is FileStatus.DOWNLOADING
    with pytest.raises(InvalidRequest, match="downloading"):
        lane_files.path(entry.id)

    src = tmp_path / "artifact"
    src.write_bytes(PDF_BYTES)
    done = lane_files.finish_download(entry.id, src)
    assert done.status is FileStatus.COMPLETE and done.mime == "application/pdf" and done.size == len(PDF_BYTES)
    assert lane_files.path(entry.id).read_bytes() == PDF_BYTES
    assert "owner_pid" not in done.public()


def test_a_download_shorter_than_announced_is_partial(lane_files, tmp_path):
    entry = lane_files.begin_download("backup.bin", None)
    src = tmp_path / "artifact"
    src.write_bytes(b"\x01" * 1000)
    done = lane_files.finish_download(entry.id, src, expected_size=5000)
    assert done.status is FileStatus.PARTIAL and "only 1000 arrived" in done.error
    assert lane_files.finish_download(lane_files.begin_download("ok.bin", None).id, src,
                                      expected_size=1000).status is FileStatus.COMPLETE


def test_downloads_of_any_type_are_kept_but_capped(lane_files, tmp_path):
    zip_entry = lane_files.begin_download("export.zip", None)
    src = tmp_path / "z"
    src.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
    assert lane_files.finish_download(zip_entry.id, src).mime == "application/zip"

    big = lane_files.begin_download("huge.bin", None)
    src.write_bytes(b"\x00" * (2 * MB + 1))
    failed = lane_files.finish_download(big.id, src)
    assert failed.status is FileStatus.FAILED and "larger than the 2.0 MB download limit" in failed.error
    assert not (lane_files.root / "outbox" / big.id / "huge.bin").exists()


def test_failed_and_partial_downloads_are_never_complete(lane_files):
    a = lane_files.begin_download("a.bin", None)
    assert lane_files.fail_download(a.id, FileStatus.PARTIAL, "connection lost", size=10).status is FileStatus.PARTIAL
    b = lane_files.begin_download("b.bin", None)
    lane_files.fail_download(b.id, FileStatus.FAILED, "cancelled")
    statuses = {f.id: f.status for f in lane_files.list()}
    assert statuses == {a.id: FileStatus.PARTIAL, b.id: FileStatus.FAILED}


def test_a_download_left_by_a_dead_daemon_reads_as_partial(lane_files):
    entry = lane_files.begin_download("a.bin", None)
    meta_path = lane_files.root / "outbox" / entry.id / ".meta.json"
    meta = json.loads(meta_path.read_text())
    meta["owner_pid"] = os.getpid() + 100_000
    meta_path.write_text(json.dumps(meta))
    found = lane_files.get(entry.id)
    assert found.status is FileStatus.PARTIAL and "restarted" in found.error


async def test_delete_cancels_a_running_download(lane_files):
    entry = lane_files.begin_download("live.bin", None)
    cancelled = []

    async def cancel():
        cancelled.append(True)
        files_mod.unregister_download(lane_files.root, entry.id)

    files_mod.register_download(lane_files.root, entry.id, cancel)
    await lane_files.delete(entry.id)
    assert cancelled == [True]
    assert not (lane_files.root / "outbox" / entry.id).exists()


async def test_delete_removes_only_its_entry(lane_files, tmp_path):
    keep = lane_files.put_bytes("keep.png", PNG_BYTES)
    gone = lane_files.put_bytes("gone.png", PNG_BYTES)
    outside = tmp_path / "outside.txt"
    outside.write_text("still here")
    (lane_files.root / "inbox" / gone.id / "link").symlink_to(outside)
    await lane_files.delete(gone.id)
    assert outside.read_text() == "still here"
    assert [f.id for f in lane_files.list()] == [keep.id]


# ---- URL checks ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://example.com/a.png",
    "ftp://example.com/a.png",
    "file:///etc/passwd",
    "data:image/png;base64,AAAA",
    "javascript:alert(1)",
    "https://",
    "https://user:pw@example.com/a.png",
    "https://user@example.com/a.png",
    "https://localhost/a.png",
    "https://LOCALHOST./a.png",
    "https://foo.localhost/a.png",
    "https://127.0.0.1/a.png",
    "https://127.1/a.png",
    "https://2130706433/a.png",
    "https://0x7f.0.0.1/a.png",
    "https://0177.0.0.1/a.png",
    "https://0.0.0.0/a.png",
    "https://10.1.2.3/a.png",
    "https://172.16.0.1/a.png",
    "https://192.168.1.1/a.png",
    "https://169.254.169.254/latest/meta-data/",
    "https://100.64.0.1/a.png",                       # CGNAT and the mesh
    "https://198.18.0.1/a.png",
    "https://224.0.0.1/a.png",
    "https://[::1]/a.png",
    "https://[fe80::1]/a.png",
    "https://[fc00::1]/a.png",
    "https://[::ffff:127.0.0.1]/a.png",
    "https://[::ffff:10.0.0.1]/a.png",
    "https://[64:ff9b::a00:1]/a.png",                 # NAT64 of 10.0.0.1
    "https://metadata.google.internal/computeMetadata/v1/",
    "https://router/a.png",
    "https://printer.local/a.png",
    "https://nas.home.arpa/a.png",
    "https://example.com:notaport/a.png",
])
def test_fetch_refuses_non_https_and_private_targets(url):
    with pytest.raises(InvalidRequest):
        check_fetch_url(url)


@pytest.mark.parametrize("url", ["https://example.com/a.png", "https://cdn.example.org:8443/x/y.jpg?w=1",
                                 "https://8.8.8.8/a.png", "https://[2606:4700::1111]/a.png"])
def test_fetch_allows_public_https(url):
    assert check_fetch_url(url) == url


def test_private_host_names():
    assert is_private_host("10.0.0.1") and is_private_host("intranet") and not is_private_host("example.com")


async def test_names_that_resolve_to_private_addresses_are_refused(monkeypatch):
    async def fake_getaddrinfo(host, port, **kw):
        table = {"rebind.example.com": ["93.184.216.34", "127.0.0.1"], "inner.example.com": ["10.0.0.7"],
                 "good.example.com": ["93.184.216.34", "2606:2800:220:1::1"]}
        family = {True: socket.AF_INET6, False: socket.AF_INET}
        return [(family[":" in a], socket.SOCK_STREAM, 6, "", (a, port)) for a in table[host]]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(InvalidRequest, match="private address"):
        await resolve_public("rebind.example.com", 443)
    with pytest.raises(InvalidRequest, match=r"private address \(10.0.0.7\)"):
        await resolve_public("inner.example.com", 443)
    assert await resolve_public("good.example.com", 443) == ["93.184.216.34", "2606:2800:220:1::1"]


async def test_the_network_backend_never_connects_to_a_private_resolution(monkeypatch):
    connected = []

    async def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    backend = files_mod._network_backend(FetchPolicy(), frozenset())

    async def record(*args, **kwargs):
        connected.append(args)

    monkeypatch.setattr(backend._inner, "connect_tcp", record)
    with pytest.raises(InvalidRequest, match="private address"):
        await backend.connect_tcp("evil.example.com", 443)
    assert connected == []


def test_display_url_and_disposition():
    assert display_url("https://u:p@cdn.example.com:8443/a/b.png?token=x#f") == "https://cdn.example.com:8443/a/b.png"
    assert display_url("blob:https://x/123") == "blob:"
    assert display_url(None) is None
    assert disposition_filename('attachment; filename="report.csv"') == "report.csv"
    assert disposition_filename("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf") == "résumé.pdf"
    assert disposition_filename("inline") is None
    assert disposition_filename(None) is None


# ---- fetch end to end against a local server ----------------------------------------------

LOCAL = FetchPolicy(https_only=False, allow_private=True)  # only for this local test server


class _Site(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: ClassVar[list[dict]] = []

    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, body: bytes = b"", headers: dict | None = None, chunked: bool = False) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(body), 4096):
                part = body[i:i + 4096]
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        _Site.seen.append({"path": self.path, "headers": dict(self.headers)})
        path = self.path
        if path.startswith("http://"):  # a forward-proxy request
            if self.headers.get("Proxy-Authorization") != "Basic " + base64.b64encode(b"lane:hunter2").decode():
                return self._send(407, b"", {"Proxy-Authenticate": "Basic"})
            path = "/" + path.split("/", 3)[3]
        if path == "/hero.png":
            return self._send(200, PNG_BYTES, {"Content-Type": "image/png"})
        if path == "/download?id=7":
            return self._send(200, JPEG_BYTES, {"Content-Disposition": 'attachment; filename="../../Cover Art.png"'})
        if path == "/redirect":
            return self._send(302, b"", {"Location": "/hero.png"})
        if path == "/loop":
            return self._send(302, b"", {"Location": "/loop"})
        if path == "/to-file":
            return self._send(302, b"", {"Location": "file:///etc/passwd"})
        if path == "/to-metadata":
            return self._send(302, b"", {"Location": "http://169.254.169.254/latest/meta-data/"})
        if path == "/page.png":
            return self._send(200, b"<html>" + b"x" * 10_000 + b"</html>", {"Content-Type": "text/html"})
        if path == "/elf.png":
            return self._send(200, b"\x7fELF" + b"\x00" * 20_000)
        if path == "/huge-declared":
            self.send_response(200)
            self.send_header("Content-Length", str(50 * MB))
            self.end_headers()
            return
        if path == "/huge-chunked":
            return self._send(200, PDF_BYTES + b"0" * (MB + 10), chunked=True)
        if path == "/empty":
            return self._send(200, b"")
        return self._send(404, b"not here")


@pytest.fixture
def site() -> Iterator[str]:
    _Site.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


async def test_put_url_streams_into_the_inbox(lane_files, site):
    stored = await lane_files.put_url("hero", f"{site}/redirect", policy=LOCAL)
    assert stored.name == "hero.png" and stored.mime == "image/png" and stored.source == "url"
    assert stored.url == f"{site}/hero.png"
    assert lane_files.path(stored.id).read_bytes() == PNG_BYTES
    assert stored.sha256 == hashlib.sha256(PNG_BYTES).hexdigest()
    assert _Site.seen[0]["headers"]["User-Agent"].startswith("agentos/")


async def test_put_url_names_from_content_disposition_and_fits_the_type(lane_files, site):
    stored = await lane_files.put_url("", f"{site}/download?id=7", policy=LOCAL)
    assert stored.name == "Cover Art.jpg" and stored.mime == "image/jpeg"
    assert stored.url == f"{site}/download"  # the query is not kept


@pytest.mark.parametrize(("path", "error"), [
    ("/loop", "too many redirects"),
    ("/to-file", "only https or http"),
    ("/missing", "HTTP 404"),
    ("/page.png", "web page"),
    ("/elf.png", "the inbox takes only"),
    ("/huge-declared", "over the 1.0 MB limit"),
    ("/huge-chunked", "larger than the 1.0 MB limit"),
    ("/empty", "empty"),
])
async def test_put_url_refusals_leave_nothing_behind(lane_files, site, path, error):
    with pytest.raises(InvalidRequest, match=error):
        await lane_files.put_url("x.png", f"{site}{path}", policy=LOCAL)
    assert inbox_entries(lane_files) == []


async def test_put_url_refuses_a_redirect_to_a_private_target(lane_files, site, monkeypatch):
    # Treat the test server's 127.0.0.1 as public so only the redirect target is private.
    real_private, real_public = files_mod.is_private_host, files_mod.is_public_ip
    monkeypatch.setattr(files_mod, "is_private_host", lambda h: h != "127.0.0.1" and real_private(h))
    monkeypatch.setattr(files_mod, "is_public_ip", lambda ip: str(ip) == "127.0.0.1" or real_public(ip))
    with pytest.raises(InvalidRequest, match="169.254.169.254"):
        await lane_files.put_url("x.png", f"{site}/to-metadata", policy=FetchPolicy(https_only=False))
    assert [s["path"] for s in _Site.seen] == ["/to-metadata"]
    assert inbox_entries(lane_files) == []


async def test_put_url_goes_through_the_lane_proxy(lane_files, site):
    port = int(site.rsplit(":", 1)[1])
    proxy = ProxyConfig(label="lt1", host="127.0.0.1", port=port, username="lane", password="hunter2")
    stored = await lane_files.put_url("p.png", "http://cdn.example.com/hero.png", proxy=proxy, policy=LOCAL)
    assert lane_files.path(stored.id).read_bytes() == PNG_BYTES
    assert _Site.seen[0]["path"] == "http://cdn.example.com/hero.png"  # the proxy saw the request, not cdn

    wrong = ProxyConfig(label="lt1", host="127.0.0.1", port=port, username="lane", password="nope")
    with pytest.raises(InvalidRequest, match="HTTP 407"):
        await lane_files.put_url("p.png", "http://cdn.example.com/hero.png", proxy=wrong, policy=LOCAL)


async def test_put_url_reports_unreachable_hosts_as_engine_errors(lane_files):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(EngineError, match="failed"):
        await lane_files.put_url("x.png", f"http://127.0.0.1:{port}/a.png", policy=LOCAL)
    assert inbox_entries(lane_files) == []


async def test_put_url_times_out(lane_files, monkeypatch):
    async def never(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(files_mod, "fetch_url", never)
    slow = LaneFiles(lane_files.root, FilesConfig(fetch_timeout=0.2))
    with pytest.raises(EngineError, match="did not finish within 0.2 s"):
        await slow.put_url("x.png", "https://example.com/a.png")
    assert inbox_entries(slow) == []
