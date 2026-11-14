"""Base images: declarative templates baked into versioned, read-only, content-hashed layers.

A layer is what installing the template's packages on top of the node produced (the
overlay uppers of a throwaway build lane), stored per mount point under
`AGENTOS_HOME/images/<name>@<n>/layer/<point>`. Lanes pin a version (`dev-box@1`) and see
the layer between their own writable layer and the node. Rebuilding makes `@n+1`; old
versions stay until no lane uses them. Curated and small on purpose: four templates ship.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tomllib
from pathlib import Path

from agentos.core.errors import InvalidRequest
from agentos.env.models import ImageTemplate
from agentos.env.paths import EnvPaths
from agentos.env.sandbox import POINTS, Point

BUILTIN_TEMPLATES: dict[str, ImageTemplate] = {
    "dev-box": ImageTemplate(
        name="dev-box",
        description="Shell and toolchains: git, Python with venv and pip, Node.js with npm, curl, jq.",
        apt=["git", "curl", "ca-certificates", "jq", "python3", "python3-venv", "python3-pip", "nodejs", "npm"],
        files={"/etc/agentos-image": "dev-box\n"},
        env={"PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    ),
    "render": ImageTemplate(
        name="render",
        description="Media rendering: ffmpeg and a basic font set.",
        apt=["ffmpeg", "fonts-dejavu-core"],
        files={"/etc/agentos-image": "render\n"},
    ),
    "brain": ImageTemplate(
        name="brain",
        description="An agent runtime: Node.js plus the Claude Code and Codex CLIs, for offloading an agent.",
        apt=["nodejs", "npm", "git", "ca-certificates", "curl"],
        npm=["@anthropic-ai/claude-code", "@openai/codex"],
        files={"/etc/agentos-image": "brain\n"},
        env={"DISABLE_AUTOUPDATER": "1"},
    ),
    "browser": ImageTemplate(
        name="browser",
        description="Browser accounts are built by agentos itself. Other browser engines and fonts are not here yet.",
        placeholder=True,
    ),
}


def load_templates(paths: EnvPaths) -> dict[str, ImageTemplate]:
    out = dict(BUILTIN_TEMPLATES)
    if paths.templates.is_dir():
        for f in sorted(paths.templates.glob("*.toml")):
            data = tomllib.loads(f.read_text())
            data.setdefault("name", f.stem)
            t = ImageTemplate(**data)
            out[t.name] = t
    return out


def template_hash(t: ImageTemplate) -> str:
    body = t.model_dump(exclude={"description"})
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def parse_ref(ref: str) -> tuple[str, int | None]:
    name, _, ver = ref.partition("@")
    if ver and not ver.isdigit():
        raise InvalidRequest(f"image ref must look like name@N: {ref!r}")
    return name, int(ver) if ver else None


def base_fingerprint(status: Path = Path("/var/lib/dpkg/status")) -> str:
    h = hashlib.sha256()
    if status.exists():
        h.update(status.read_bytes())
    return "sha256:" + h.hexdigest()


def tree_hash(root: Path) -> tuple[str, int]:
    """Deterministic hash of a layer: paths, types, modes, owners, contents, link targets,
    whiteouts and opaque markers. Returns (hash, total file bytes)."""
    h = hashlib.sha256()
    size = 0
    if not root.exists():
        return "sha256:" + h.hexdigest(), 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        entries = sorted(dirnames + filenames)
        for name in entries:
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            st = p.lstat()
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                h.update(f"L {rel} {os.readlink(p)}\n".encode())
            elif stat.S_ISDIR(st.st_mode):
                opaque = ""
                try:
                    opaque = os.getxattr(p, "trusted.overlay.opaque", follow_symlinks=False).decode()  # type: ignore[attr-defined]
                except (OSError, AttributeError):
                    pass
                h.update(f"D {rel} {mode:o} {st.st_uid}:{st.st_gid} {opaque}\n".encode())
            elif stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
                h.update(f"C {rel} {st.st_rdev}\n".encode())
            elif stat.S_ISREG(st.st_mode):
                fh = hashlib.sha256()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        fh.update(chunk)
                size += st.st_size
                h.update(f"F {rel} {mode:o} {st.st_uid}:{st.st_gid} {fh.hexdigest()}\n".encode())
            else:
                h.update(f"O {rel}\n".encode())
    return "sha256:" + h.hexdigest(), size


def point_for(path: str) -> tuple[Point, str]:
    """Map an absolute path in the lane to (mount point, path relative to it)."""
    if not path.startswith("/") or ".." in path.split("/"):
        raise InvalidRequest(f"template file paths must be absolute and plain: {path!r}")
    rel = path.lstrip("/")
    best: Point | None = None
    for p in POINTS:
        inside = rel == p.path or rel.startswith(p.path + "/")
        if inside and (best is None or len(p.path) > len(best.path)):
            best = p
    if best is None:
        raise InvalidRequest(f"template files must live under /usr, /etc, /opt, /var or /root: {path!r}")
    return best, rel[len(best.path):].lstrip("/")

