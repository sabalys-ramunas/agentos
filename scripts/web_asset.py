"""Package a built web app (web/dist) as the release asset `agentos web install` downloads.

    python3 scripts/web_asset.py [--dist web/dist] [--version X.Y.Z] [--out dist-release]

Writes agentos-web-<version>.tar.gz and agentos-web-<version>.tar.gz.sha256 (sha256sum's format).
The archive holds the files of web/dist at its top (index.html first), regular files and folders
only, with fixed owners, modes and times, so the same build packs to the same bytes. It refuses a
folder with no index.html, with a link in it, or with the mock backend in it (web/build/guard.ts
keeps the mock out of `vite build`; this checks again, since this file is what people install).
The release workflow (.github/workflows/web-release.yml) runs it after `pnpm build`.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import re
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOCK_FILES = ("mockServiceWorker.js", "sw-mock.js")
MOCK_MARKERS = (b"agentos-mock-backend", b"mockServiceWorker")
MTIME = 0  # fixed, so a rebuild of the same files gives the same archive


def version_of_checkout() -> str:
    text = (ROOT / "src" / "agentos" / "__init__.py").read_text()
    m = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if not m:
        raise SystemExit("no __version__ in src/agentos/__init__.py")
    return m.group(1)


def files_of(dist: Path) -> list[Path]:
    """Every file under dist, index.html first, the rest sorted. Refuses links and the mock."""
    if not (dist / "index.html").is_file():
        raise SystemExit(f"{dist} has no index.html: build the web app first (pnpm build in web/)")
    found: list[Path] = []
    for path in sorted(dist.rglob("*")):
        rel = path.relative_to(dist).as_posix()
        if path.is_symlink():
            raise SystemExit(f"{rel} is a link: a release archive holds regular files only")
        if path.is_dir():
            continue
        if path.name in MOCK_FILES:
            raise SystemExit(f"{rel} is the mock backend: build in live mode (VITE_API_MODE=live)")
        if path.suffix in (".js", ".mjs", ".html") and any(m in path.read_bytes() for m in MOCK_MARKERS):
            raise SystemExit(f"{rel} holds mock code: build in live mode (VITE_API_MODE=live)")
        found.append(path)
    found.sort(key=lambda p: (p.relative_to(dist).as_posix() != "index.html", p.relative_to(dist).as_posix()))
    return found


def _info(name: str, *, size: int = 0, folder: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if folder else tarfile.REGTYPE
    info.mode = 0o755 if folder else 0o644
    info.size = size
    info.mtime = MTIME
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def pack(dist: Path) -> bytes:
    """The .tar.gz bytes of dist."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        folders: set[str] = set()
        for path in files_of(dist):
            rel = path.relative_to(dist)
            for parent in reversed(rel.parents[:-1]):  # each folder once, before its files
                name = parent.as_posix()
                if name not in folders:
                    folders.add(name)
                    tar.addfile(_info(name, folder=True))
            data = path.read_bytes()
            tar.addfile(_info(rel.as_posix(), size=len(data)), io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=MTIME, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def write_asset(dist: Path, version: str, out: Path) -> tuple[Path, str]:
    """Write the archive and its .sha256 into out; (the archive's path, its sha256)."""
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-.+][0-9A-Za-z.]+)?", version):
        raise SystemExit(f"{version!r} is not a version like 0.3.0")
    data = pack(dist)
    out.mkdir(parents=True, exist_ok=True)
    name = f"agentos-web-{version}.tar.gz"
    digest = hashlib.sha256(data).hexdigest()
    (out / name).write_bytes(data)
    (out / f"{name}.sha256").write_text(f"{digest}  {name}\n")
    return out / name, digest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dist", type=Path, default=ROOT / "web" / "dist", help="the built web app (default: web/dist)")
    p.add_argument("--version", help="the release (default: src/agentos/__init__.py's)")
    p.add_argument("--out", type=Path, default=ROOT / "dist-release", help="where to write (default: dist-release/)")
    args = p.parse_args(argv)
    path, digest = write_asset(args.dist.resolve(), args.version or version_of_checkout(), args.out)
    print(f"{path} ({path.stat().st_size // 1024} KB)\nsha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
