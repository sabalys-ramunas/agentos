"""The prebuilt web app: scripts/web_asset.py packs it, agentos.setup.webapp downloads, checks and unpacks
it, agentos.core.webapp says which copy `/` serves, and the API serves it as static files only.

The release is a loopback HTTP server laid out like GitHub's release downloads
(<base>/v<version>/agentos-web-<version>.tar.gz and its .sha256): nothing reaches GitHub.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import io
import json
import tarfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos import __version__
from agentos.core import webapp
from agentos.setup import webapp as installer

ROOT = Path(__file__).resolve().parents[2]


def _asset_module():
    spec = importlib.util.spec_from_file_location("web_asset", ROOT / "scripts" / "web_asset.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


web_asset = _asset_module()


def _dist(where: Path, marker: str = "first") -> Path:
    dist = where / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(f"<!doctype html><title>agentos</title><div id=root>{marker}</div>")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)")
    (dist / "icon.svg").write_text("<svg/>")
    return dist


class Release:
    """A fake release download server on 127.0.0.1. It is not GitHub, so each publish also pins what it
    published (AGENTOS_WEB_SHA256), as an owner installing from a mirror does."""

    def __init__(self, root: Path, monkeypatch=None):
        self.root = root
        self.monkeypatch = monkeypatch
        handler = functools.partial(_Quiet, directory=str(root))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/download"

    def publish(self, dist: Path, version: str = __version__) -> str:
        _path, digest = web_asset.write_asset(dist, version, self.root / "download" / f"v{version}")
        self.pin(digest)
        return digest

    def pin(self, digest: str) -> None:
        if self.monkeypatch is not None:
            self.monkeypatch.setenv("AGENTOS_WEB_SHA256", digest)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def release(tmp_path, monkeypatch):
    r = Release(tmp_path / "release", monkeypatch)
    (tmp_path / "release" / "download").mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_WEB_RELEASE_URL", r.base)
    monkeypatch.delenv("AGENTOS_PANEL_DIR", raising=False)
    monkeypatch.delenv("AGENTOS_NO_WEB", raising=False)
    monkeypatch.setattr(webapp, "checkout_dist", lambda: tmp_path / "no-checkout" / "web" / "dist")
    yield r
    r.close()


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def _tar(entries: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for info, data in entries:
            if data is not None:
                info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return raw.getvalue()


def _file(name: str, data: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    return tarfile.TarInfo(name), data


def _link(name: str, target: str, kind: bytes = tarfile.SYMTYPE) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info, None


def _serve(release: Release, data: bytes, version: str = __version__, digest: str | None = None) -> None:
    folder = release.root / "download" / f"v{version}"
    folder.mkdir(parents=True, exist_ok=True)
    name = installer.asset_name(version)
    (folder / name).write_bytes(data)
    (folder / f"{name}.sha256").write_text(f"{digest or hashlib.sha256(data).hexdigest()}  {name}\n")
    release.pin(digest or hashlib.sha256(data).hexdigest())


# ---- the packer (scripts/web_asset.py, as the release workflow runs it)


def test_the_packer_writes_a_reproducible_archive_with_index_first(tmp_path):
    dist = _dist(tmp_path)
    a, digest = web_asset.write_asset(dist, "1.2.3", tmp_path / "a")
    b, _ = web_asset.write_asset(dist, "1.2.3", tmp_path / "b")
    assert a.name == "agentos-web-1.2.3.tar.gz" and a.read_bytes() == b.read_bytes()
    assert (tmp_path / "a" / "agentos-web-1.2.3.tar.gz.sha256").read_text() == f"{digest}  {a.name}\n"
    with tarfile.open(a) as tar:
        names = tar.getnames()
        assert names[0] == "index.html" and "assets/index-abc123.js" in names
        assert all(m.isfile() or m.isdir() for m in tar.getmembers())
        assert {m.uid for m in tar.getmembers()} == {0} and {m.mtime for m in tar.getmembers()} == {0}


@pytest.mark.parametrize("bad", ["link", "mock-file", "mock-code", "no-index"])
def test_the_packer_refuses_links_the_mock_and_no_index(tmp_path, bad):
    dist = _dist(tmp_path)
    if bad == "link":
        (dist / "assets" / "out.js").symlink_to(tmp_path / "secret")
    elif bad == "mock-file":
        (dist / "mockServiceWorker.js").write_text("self")
    elif bad == "mock-code":
        (dist / "assets" / "index-abc123.js").write_text("const m = 'agentos-mock-backend'")
    else:
        (dist / "index.html").unlink()
    with pytest.raises(SystemExit):
        web_asset.write_asset(dist, "1.2.3", tmp_path / "out")
    assert not (tmp_path / "out" / "agentos-web-1.2.3.tar.gz").exists()


# ---- download, checksum and unpack


def test_install_downloads_this_release_and_checks_its_sha256(release, home, tmp_path):
    digest = release.publish(_dist(tmp_path))
    got = installer.install(home)
    assert got.version == __version__ and got.sha256 == digest and got.files == 3
    assert (webapp.installed_dir(home) / "assets" / "index-abc123.js").read_text() == "console.log(1)"
    state = webapp.read_state(home)
    assert state["version"] == __version__ and state["sha256"] == digest and state["wanted"] is True
    assert state["url"] == f"{release.base}/v{__version__}/agentos-web-{__version__}.tar.gz"
    assert not [p for p in (home / "web").iterdir() if p.name.startswith(".")]  # no half-unpacked folder left


def test_a_checksum_that_differs_is_refused_and_the_old_app_stays(release, home, tmp_path):
    release.publish(_dist(tmp_path / "one", "first"))
    installer.install(home)
    other = web_asset.pack(_dist(tmp_path / "two", "second"))
    _serve(release, other, digest="0" * 64)
    with pytest.raises(installer.WebAppRefused, match="does not match the release's sha256"):
        installer.install(home)
    assert "first" in (webapp.installed_dir(home) / "index.html").read_text()


def test_no_release_asset_is_missing_not_a_crash(release, home):
    with pytest.raises(installer.WebAppMissing, match="404"):
        installer.install(home)
    assert not webapp.installed_dir(home).exists() and webapp.read_state(home) is None


def _github(release: dict | None, data: bytes = b"archive"):
    """A fake fetch for a GitHub release: the API's release (None: 404), the .sha256 and the archive."""
    def get(url: str, limit: int) -> bytes:
        if url.startswith(installer.GITHUB_API):
            assert url.endswith(f"/repos/me/agentos/releases/tags/v{VERSION}")
            if release is None:
                raise installer.WebAppMissing(f"{url} answered 404")
            return json.dumps(release).encode()
        if url.endswith(".sha256"):
            return f"{hashlib.sha256(data).hexdigest()}  {installer.asset_name(VERSION)}\n".encode()
        return data
    return get


VERSION = "9.8.7"
GH = "https://github.com/me/agentos/releases/download"


def test_only_an_immutable_github_releases_own_digest_is_trusted(home, monkeypatch):
    monkeypatch.delenv("AGENTOS_WEB_SHA256", raising=False)
    data = _tar([_file("index.html", b"<!doctype html>")])
    name = installer.asset_name(VERSION)
    good = {"immutable": True, "assets": [{"name": name, "digest": "sha256:" + hashlib.sha256(data).hexdigest()}]}
    assert installer.install(home, VERSION, base=GH, get=_github(good, data)).sha256 == hashlib.sha256(data).hexdigest()
    with pytest.raises(installer.WebAppRefused, match="not immutable"):  # its files could be replaced: refused
        installer.install(home, VERSION, base=GH, get=_github({**good, "immutable": False}, data))
    other = {**good, "assets": [{"name": name, "digest": "sha256:" + "1" * 64}]}
    with pytest.raises(installer.WebAppRefused, match="not the sha256 it was published with"):
        installer.install(home, VERSION, base=GH, get=_github(other, data))  # archive and .sha256 replaced together
    with pytest.raises(installer.WebAppRefused, match="AGENTOS_WEB_SHA256"):
        installer.install(home, VERSION, base="https://mirror.example/download", get=_github(good, data))
    monkeypatch.setenv("AGENTOS_WEB_SHA256", hashlib.sha256(data).hexdigest())  # the owner's own pin, for a mirror
    assert installer.install(home, VERSION, base="https://mirror.example/download", get=_github(None, data))
    monkeypatch.setenv("AGENTOS_WEB_SHA256", "not-a-sha")
    with pytest.raises(installer.WebAppRefused, match="64 hex"):
        installer.install(home, VERSION, base=GH, get=_github(good, data))


def test_the_release_workflow_never_replaces_a_file_on_a_release():
    text = (ROOT / ".github" / "workflows" / "web-release.yml").read_text()
    [upload] = [line for line in text.splitlines() if "run: gh release upload" in line]
    assert "--clobber" not in upload and "types: [created]" in text  # attached to the draft, before it is published


def test_no_network_is_missing(home, monkeypatch):
    monkeypatch.setenv("AGENTOS_WEB_RELEASE_URL", "http://127.0.0.1:9/download")  # nothing listens on 9
    with pytest.raises(installer.WebAppMissing, match="could not reach"):
        installer.install(home)


def test_the_download_is_pinned_to_the_installed_version():
    assert installer.asset_url("0.3.0", "https://example.invalid/r") == (
        "https://example.invalid/r/v0.3.0/agentos-web-0.3.0.tar.gz")
    assert installer.RELEASES.startswith("https://github.com/") and "latest" not in installer.asset_url("1.0.0")


@pytest.mark.parametrize("entries", [
    [_file("index.html"), _file("../escape.txt")],
    [_file("index.html"), _file("assets/../../escape.txt")],
    [_file("index.html"), _file("/tmp/escape.txt")],
    [_file("index.html"), _link("assets/out", "/etc")],
    [_file("index.html"), _link("assets/out", "../../..")],
    [_file("index.html"), _link("hard", "/etc/passwd", tarfile.LNKTYPE)],
], ids=["dotdot", "nested-dotdot", "absolute", "symlink-absolute", "symlink-up", "hardlink"])
def test_unsafe_archives_are_refused_and_nothing_is_unpacked(release, home, tmp_path, entries):
    _serve(release, _tar(entries))
    with pytest.raises(installer.WebAppRefused, match="nothing was unpacked"):
        installer.install(home)
    assert not webapp.installed_dir(home).exists()
    assert not (tmp_path / "escape.txt").exists() and not (home / "escape.txt").exists()
    assert not [p for p in (home / "web").iterdir() if p.name.startswith(".incoming")]


def test_an_archive_without_index_is_not_the_app(release, home):
    _serve(release, _tar([_file("readme.txt")]))
    with pytest.raises(installer.WebAppRefused, match="no index.html"):
        installer.install(home)


def test_a_sha256_file_for_another_name_is_refused():
    with pytest.raises(installer.WebAppRefused):
        installer.parse_sha256("a" * 64 + "  other.tar.gz\n", "agentos-web-1.0.0.tar.gz")
    assert installer.parse_sha256("A" * 64 + " *agentos-web-1.0.0.tar.gz", "agentos-web-1.0.0.tar.gz") == "a" * 64


# ---- upgrade and remove


def test_update_follows_the_version_and_a_remove_sticks(release, home, tmp_path):
    release.publish(_dist(tmp_path / "old", "old"), version="0.0.1")
    installer.install(home, "0.0.1")
    assert webapp.status(home).mismatched
    release.publish(_dist(tmp_path / "new", "new"))
    what, got = installer.update(home)
    assert what == "installed" and got.version == __version__
    assert "new" in (webapp.installed_dir(home) / "index.html").read_text()
    assert installer.update(home) == ("current", None)
    assert installer.remove(home) is True and not webapp.installed_dir(home).exists()
    assert installer.update(home) == ("declined", None)  # the installer leaves a removed app away
    assert webapp.status(home).wanted is False


def test_the_web_cli_installs_reports_and_removes(release, home, tmp_path, monkeypatch):
    from agentos.cli import app

    release.publish(_dist(tmp_path))
    runner = CliRunner()
    r = runner.invoke(app, ["web", "status", "--home", str(home)])
    assert r.exit_code == 0 and "not installed" in r.output
    r = runner.invoke(app, ["web", "update", "--home", str(home)])  # never asked: the installer adds it
    assert r.exit_code == 0 and f"installed {__version__}" in r.output, r.output
    r = runner.invoke(app, ["web", "status", "--home", str(home)])
    assert f"version {__version__}, installed in" in r.output
    r = runner.invoke(app, ["web", "remove", "--home", str(home)])
    assert "Removed the web app" in r.output
    r = runner.invoke(app, ["web", "update", "--home", str(home)])
    assert "you said no" in r.output and not webapp.installed_dir(home).exists()
    monkeypatch.setenv("AGENTOS_NO_WEB", "1")
    assert "skipped" in runner.invoke(app, ["web", "update", "--home", str(home)]).output
    monkeypatch.delenv("AGENTOS_NO_WEB")
    r = runner.invoke(app, ["web", "install", "--home", str(home)])
    assert r.exit_code == 0 and "Installed the web app" in r.output
    for out in (r.output,):
        assert "—" not in out


def test_the_web_cli_fails_in_words_without_a_release(release, home):
    from agentos.cli import app

    r = CliRunner().invoke(app, ["web", "install", "--home", str(home)])
    assert r.exit_code == 1 and "404" in r.output and "The node works without it" in r.output


# ---- which copy `/` serves


def _client(**settings):
    from starlette.testclient import TestClient

    from agentos.api import ApiSettings, PanelStore, build_api
    from agentos.dev.apidemo import demo_runtime

    return TestClient(build_api(demo_runtime(), ApiSettings(store=PanelStore(None), owner_token="t" * 32, mount_features=False,
                                                                **settings)))


def test_a_checkout_build_comes_first_then_the_installed_copy(tmp_path, home, monkeypatch):
    monkeypatch.delenv("AGENTOS_PANEL_DIR", raising=False)
    checkout = tmp_path / "repo" / "web" / "dist"
    monkeypatch.setattr(webapp, "checkout_dist", lambda: checkout)
    assert webapp.panel_dir(home) == checkout and webapp.status(home).source == "none"
    installed = webapp.installed_dir(home)
    installed.mkdir(parents=True)
    (installed / "index.html").write_text("installed")
    assert webapp.panel_dir(home) == installed and webapp.status(home).source == "installed"
    checkout.mkdir(parents=True)
    (checkout / "index.html").write_text("checkout")
    assert webapp.panel_dir(home) == checkout and webapp.status(home).source == "checkout"
    monkeypatch.setenv("AGENTOS_PANEL_DIR", str(tmp_path / "elsewhere"))
    assert webapp.panel_dir(home) == tmp_path / "elsewhere" and webapp.status(home).source == "env"


def test_the_node_serves_an_app_installed_after_it_started(release, home, tmp_path):
    client = _client(home=home)
    assert "agentos web install" in client.get("/").text  # no app yet: the page says how to add it
    release.publish(_dist(tmp_path))
    installer.install(home)
    index = client.get("/")
    assert index.status_code == 200 and "<div id=root>first</div>" in index.text
    assert client.get("/assets/index-abc123.js").text == "console.log(1)"
    # static files only, from under the app's folder: no traversal, no link out of it
    (home / "secret.txt").write_text("outside")
    (webapp.installed_dir(home) / "assets" / "out.txt").symlink_to(home / "secret.txt")
    for path in ("/../secret.txt", "/%2e%2e/%2e%2e/secret.txt", "/assets/../../secret.txt", "/assets/out.txt"):
        assert "outside" not in client.get(path).text
    installer.remove(home)
    assert "agentos web install" in client.get("/").text


def test_a_linked_index_out_of_the_folder_is_not_served(tmp_path, home, monkeypatch):
    monkeypatch.delenv("AGENTOS_PANEL_DIR", raising=False)
    dist = tmp_path / "dist"
    dist.mkdir()
    (tmp_path / "secret.html").write_text("outside")
    (dist / "index.html").symlink_to(tmp_path / "secret.html")
    client = _client(panel_dir=dist)
    assert "outside" not in client.get("/").text


# ---- the doctor


def test_the_doctor_warns_about_a_mismatched_copy(release, home, tmp_path):
    from agentos.ops.doctor_web import check_web

    class Ctx:
        pass

    ctx = Ctx()
    ctx.home = home
    assert check_web(ctx)[0].status == "skip"
    release.publish(_dist(tmp_path), version="0.0.1")
    installer.install(home, "0.0.1")
    row = check_web(ctx)[0]
    assert row.status == "warn" and "0.0.1" in row.detail and row.fix.startswith("agentos web install")
    release.publish(_dist(tmp_path / "now"))
    installer.install(home)
    assert check_web(ctx)[0].status == "pass"
