"""Program installs from pinned sources: apt (the node's signed apt sources), pip (one index),
npm (one registry). Package names are validated strictly, so a package string can never
become a shell word or an extra option."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from openajar.core.errors import InvalidRequest
from openajar.env.paths import EnvSettings

APT_PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,100}(=[A-Za-z0-9.+:~-]{1,80})?$")
PIP_PKG_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}(\[[A-Za-z0-9,._-]{1,100}\])?((==|>=|<=|~=|!=)[A-Za-z0-9.*+!-]{1,60})?$")
NPM_PKG_RE = re.compile(r"^(@[a-z0-9][a-z0-9._-]{0,100}/)?[a-z0-9][a-z0-9._-]{0,100}(@[A-Za-z0-9.^~*+-]{1,60})?$")

SOURCES = ("apt", "pip", "npm")


def validate(source: str, packages: list[str]) -> list[str]:
    if source not in SOURCES:
        raise InvalidRequest(f"source must be one of {', '.join(SOURCES)}")
    if not packages:
        raise InvalidRequest("give at least one package")
    if len(packages) > 50:
        raise InvalidRequest("at most 50 packages per install")
    rx = {"apt": APT_PKG_RE, "pip": PIP_PKG_RE, "npm": NPM_PKG_RE}[source]
    out = []
    for p in packages:
        p = p.strip()
        if not rx.match(p):
            raise InvalidRequest(f"not a valid {source} package spec: {p!r}")
        out.append(p)
    return out


def package_name(source: str, spec: str) -> str:
    if source == "apt":
        return spec.split("=", 1)[0]
    if source == "pip":
        return re.split(r"[\[=<>~!]", spec, maxsplit=1)[0]
    if spec.startswith("@"):
        scope, _, rest = spec[1:].partition("/")
        return "@" + scope + "/" + rest.split("@", 1)[0]
    return spec.split("@", 1)[0]


def apt_hosts_from_sources(root: Path = Path("/etc/apt")) -> list[str]:
    """Hosts of the node's configured (signed) apt sources."""
    hosts: set[str] = set()
    files = []
    if (root / "sources.list").is_file():
        files.append(root / "sources.list")
    d = root / "sources.list.d"
    if d.is_dir():
        files += sorted(d.glob("*.list")) + sorted(d.glob("*.sources"))
    for f in files:
        for line in f.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            urls: list[str] = []
            if line.lower().startswith("uris:"):
                urls = line.split(":", 1)[1].split()
            elif line.startswith(("deb ", "deb-src ")):
                urls = [w for w in line.split() if "://" in w][:1]
            for u in urls:
                h = urlsplit(u).hostname
                if h:
                    hosts.add(h.lower())
    return sorted(hosts) or ["archive.ubuntu.com", "security.ubuntu.com"]


def source_hosts(source: str, settings: EnvSettings) -> list[str]:
    if source == "apt":
        return list(settings.apt_hosts) or apt_hosts_from_sources()
    if source == "pip":
        return list(settings.pip_hosts)
    return list(settings.npm_hosts)


def install_argv(source: str, packages: list[str], settings: EnvSettings) -> tuple[list[str], dict[str, str]]:
    """The exact command an install runs (inside the lane, as its root)."""
    if source == "apt":
        script = ('apt-get update -q 2>&1 | tail -n 3; '
                  'exec apt-get install -y -q --no-install-recommends '
                  '-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "$@"')
        return (["/bin/sh", "-c", script, "apt-get", *packages],
                {"DEBIAN_FRONTEND": "noninteractive", "APT_LISTCHANGES_FRONTEND": "none"})
    if source == "pip":
        return (["python3", "-m", "pip", "install", "--break-system-packages", "--root-user-action=ignore",
                 "--no-cache-dir", "--disable-pip-version-check", "--index-url", settings.pip_index, *packages],
                {"PIP_NO_INPUT": "1"})
    return (["npm", "install", "-g", "--no-fund", "--no-audit", "--no-update-notifier",
             "--registry", settings.npm_registry, *packages],
            {"npm_config_cache": "/tmp/.npm-cache"})


def manifest_argv(source: str, packages: list[str]) -> list[str]:
    names = [package_name(source, p) for p in packages]
    if source == "apt":
        return ["dpkg-query", "-W", "-f=${Package}=${Version}\\n", *names]
    if source == "pip":
        return ["python3", "-m", "pip", "show", "--disable-pip-version-check", *names]
    return ["npm", "ls", "-g", "--depth=0", "--json"]


def parse_manifest(source: str, output: str, packages: list[str]) -> dict[str, str]:
    names = {package_name(source, p) for p in packages}
    out: dict[str, str] = {}
    if source == "apt":
        for line in output.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    elif source == "pip":
        name = None
        for line in output.splitlines():
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Version:") and name:
                out[name] = line.split(":", 1)[1].strip()
    else:
        try:
            deps = json.loads(output or "{}").get("dependencies", {})
        except json.JSONDecodeError:
            deps = {}
        for k, v in deps.items():
            if k in names:
                out[k] = v.get("version", "?")
    return out


def summary(source: str, packages: list[str], hosts: list[str], lane_id: str, *, shared: bool) -> str:
    where = ("onto the node itself (this lane shares the node filesystem, so every lane sees it)"
             if shared else f"into lane {lane_id}'s own overlay")
    return f"Install {source} packages {' '.join(packages)} {where}, from {', '.join(hosts)}"
