"""The prebuilt web app: download it for this agentos's release, check it, unpack it under the home.

Each release publishes `agentos-web-<version>.tar.gz` and its `.sha256` (.github/workflows/web-release.yml,
built by scripts/web_asset.py). `install` takes both from the release tag of the agentos that runs it
(v<version>, never "latest"), refuses an archive whose sha256 differs, and unpacks it into
<home>/web/app (agentos.core.webapp): regular files and folders only, nothing absolute, no `..`, no
links, within a size cap. It swaps the folder in whole, so the node never serves half an app.
The app holds no secret: it is static files that call the node's API with the browser's session.

Which sha256 it trusts (`trusted_sha256`). The `.sha256` beside the archive comes from the same release,
so whoever could replace the archive could replace it too. So the sha256 must come from somewhere the
uploader cannot change afterwards: GitHub's own digest of the asset on a release GitHub keeps immutable
(its API's `immutable` and the asset's `digest`: once published, no file or tag of it can change). A
release that is not immutable is refused, and so is any other source (AGENTOS_WEB_RELEASE_URL: a fork, a
mirror, a test server) unless the owner pins the archive's sha256 with AGENTOS_WEB_SHA256. The trust left
is the same as the code's: the repository's release, as GitHub serves it (docs/THREAT-MODEL.md).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentos.core import webapp
from agentos.core.errors import AgentosError

RELEASES = "https://github.com/sabalys-ramunas/agentos/releases/download"
MAX_DOWNLOAD = 64 * 1024**2   # the archive is a few MB; this bounds a wrong or hostile answer
MAX_UNPACKED = 256 * 1024**2
MAX_FILES = 5000
TIMEOUT = 60.0
SHA256 = re.compile(r"^([0-9a-f]{64})(?:\s+\*?(\S+))?\s*$")
GITHUB_RELEASES = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/releases/download$")
GITHUB_API = "https://api.github.com"
MAX_RELEASE_JSON = 1024**2


class WebAppMissing(AgentosError):
    """The release has no web app to download (none published yet, or no network)."""
    code = "web_app_missing"


class WebAppRefused(AgentosError):
    """The download is not the release's file (sha256 differs) or is not a safe archive."""
    code = "web_app_refused"


Fetch = Callable[[str, int], bytes]


@dataclass(frozen=True)
class Installed:
    version: str
    sha256: str
    files: int
    path: Path


def release_base() -> str:
    return (os.environ.get("AGENTOS_WEB_RELEASE_URL") or RELEASES).rstrip("/")


def asset_name(version: str) -> str:
    return f"agentos-web-{version}.tar.gz"


def asset_url(version: str, base: str | None = None) -> str:
    return f"{base or release_base()}/v{version}/{asset_name(version)}"


def fetch(url: str, limit: int) -> bytes:
    """The body at url, at most `limit` bytes. A 404 or no network is WebAppMissing."""
    if not url.startswith(("https://", "http://")):
        raise WebAppRefused(f"Not a web address: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "agentos-web-install"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # http(s) only, checked above
            body = resp.read(limit + 1)
    except urllib.error.HTTPError as e:
        raise WebAppMissing(f"{url} answered {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise WebAppMissing(f"could not reach {url}: {getattr(e, 'reason', e)}") from None
    if len(body) > limit:
        raise WebAppRefused(f"{url} is larger than {limit // 1024**2} MB")
    return body


def parse_sha256(text: str, name: str) -> str:
    """The hex digest in a `.sha256` file (`<hex>  <name>`, as sha256sum writes it)."""
    for line in text.strip().splitlines():
        m = SHA256.match(line.strip().lower())
        if m and (m.group(2) is None or m.group(2) == name.lower()):
            return m.group(1)
    raise WebAppRefused(f"The release's {name}.sha256 does not hold a sha256 for {name}.")


def trusted_sha256(version: str, base: str, get: Fetch) -> str:
    """The sha256 the archive must have, from a source its uploader cannot change after the release: the
    owner's own pin (AGENTOS_WEB_SHA256), else GitHub's digest of the asset on an immutable GitHub release.
    WebAppRefused, with the fix, for anything else."""
    name = asset_name(version)
    pin = os.environ.get("AGENTOS_WEB_SHA256", "").strip().lower()
    if pin:
        if not re.fullmatch(r"[0-9a-f]{64}", pin):
            raise WebAppRefused("AGENTOS_WEB_SHA256 is not a sha256 (64 hex digits), so nothing was installed.")
        return pin
    m = GITHUB_RELEASES.match(base)
    if m is None:
        raise WebAppRefused(f"{base} is not a GitHub release, so nothing but its own .sha256 says {name} is the "
                            "released one, and nothing was installed. Set AGENTOS_WEB_SHA256 to the archive's sha256 "
                            "you checked, or unset AGENTOS_WEB_RELEASE_URL to install from GitHub.")
    raw = get(f"{GITHUB_API}/repos/{m[1]}/{m[2]}/releases/tags/v{version}", MAX_RELEASE_JSON)
    try:
        release = json.loads(raw)
    except ValueError:
        raise WebAppRefused(f"GitHub's answer about release v{version} is not readable, so nothing was installed.") \
            from None
    if not isinstance(release, dict) or release.get("immutable") is not True:
        raise WebAppRefused(f"Release v{version} of {m[1]}/{m[2]} is not immutable on GitHub, so its files could be "
                            "replaced after it was published, and nothing was installed. Set AGENTOS_WEB_SHA256 to "
                            "the archive's sha256 you checked, or build the web app from a checkout.")
    asset = next((a for a in release.get("assets") or [] if isinstance(a, dict) and a.get("name") == name), None)
    digest = str((asset or {}).get("digest") or "").lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise WebAppMissing(f"release v{version} on GitHub lists no {name} with a digest")
    return digest.removeprefix("sha256:")


def _member_problem(m: tarfile.TarInfo) -> str | None:
    name = m.name
    parts = PurePosixPath(name).parts
    if not name or name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        return "an absolute path"
    if ".." in parts or "\\" in name or "\x00" in name:
        return "a path that leads out of the folder"
    if not (m.isfile() or m.isdir()):
        return "a link or a special file"
    return None


def check_archive(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """The members, when every one is safe to unpack; WebAppRefused names the first that is not."""
    members = tar.getmembers()
    if len(members) > MAX_FILES:
        raise WebAppRefused(f"The archive has more than {MAX_FILES} files.")
    total = 0
    for m in members:
        problem = _member_problem(m)
        if problem:
            raise WebAppRefused(f"The archive holds {problem} ({m.name!r}), so nothing was unpacked.")
        total += m.size
        if total > MAX_UNPACKED:
            raise WebAppRefused(f"The archive unpacks to more than {MAX_UNPACKED // 1024**2} MB.")
    return members


def unpack(data: bytes, dest: Path) -> int:
    """Unpack a checked .tar.gz into dest (which must be empty); the number of files."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = check_archive(tar)
            # the "data" filter refuses the same things again, and drops modes a web file needs none of
            tar.extractall(dest, members=members, filter="data")
    except (tarfile.TarError, EOFError, OSError) as e:
        raise WebAppRefused(f"The archive could not be unpacked: {e}") from None
    return sum(1 for m in members if m.isfile())


def _write_state(home: Path, **data: object) -> None:
    path = webapp.state_path(home)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def install(home: Path, version: str | None = None, *, base: str | None = None,
            get: Fetch | None = None) -> Installed:
    """Download this agentos's web app (or `version`'s), check it and put it in place."""
    from agentos import __version__

    version = version or __version__
    get = get or fetch
    name = asset_name(version)
    url = asset_url(version, base)
    listed = parse_sha256(get(url + ".sha256", 4096).decode("utf-8", "replace"), name)
    expected = trusted_sha256(version, (base or release_base()).rstrip("/"), get)
    if listed != expected:
        raise WebAppRefused(f"The release's {name}.sha256 ({listed[:12]}) is not the sha256 it was published with "
                            f"({expected[:12]}), so nothing was installed.")
    data = get(url, MAX_DOWNLOAD)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise WebAppRefused(f"{name} does not match the release's sha256 (got {actual[:12]}, "
                            f"expected {expected[:12]}), so nothing was installed.")
    web = Path(home) / webapp.WEB_DIR
    web.mkdir(mode=0o700, parents=True, exist_ok=True)
    incoming = Path(tempfile.mkdtemp(prefix=".incoming-", dir=web))
    try:
        files = unpack(data, incoming)
        if not webapp.built(incoming):
            raise WebAppRefused(f"{name} has no index.html at its top, so it is not the web app.")
        _swap(incoming, webapp.installed_dir(home))
    finally:
        shutil.rmtree(incoming, ignore_errors=True)
    _write_state(home, version=version, sha256=actual, wanted=True, url=url)
    return Installed(version=version, sha256=actual, files=files, path=webapp.installed_dir(home))


def _swap(new: Path, target: Path) -> None:
    """Put `new` at `target` whole: the old folder moves aside first and goes after."""
    old = None
    if target.exists() or target.is_symlink():
        old = target.with_name(f".old-{os.getpid()}")
        shutil.rmtree(old, ignore_errors=True)
        target.rename(old)
    new.rename(target)
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def remove(home: Path) -> bool:
    """Take the installed app away and remember the no (upgrades leave it away). True if one was there."""
    target = webapp.installed_dir(home)
    there = target.exists()
    if there:
        shutil.rmtree(target)
    _write_state(home, wanted=False)
    return there


def decline(home: Path) -> None:
    """Remember that the person said no, so the installer does not fetch it on its own."""
    if not webapp.built(webapp.installed_dir(home)):
        _write_state(home, wanted=False)


def update(home: Path, *, base: str | None = None, get: Fetch | None = None) -> tuple[str, Installed | None]:
    """Bring the app to this agentos's version, unless the person said no: ("current" | "declined" |
    "installed", the new copy). What the installer runs when it does not start setup."""
    st = webapp.status(home)
    if st.wanted is False:
        return "declined", None
    if st.installed == st.expected:
        return "current", None
    return "installed", install(home, base=base, get=get)
