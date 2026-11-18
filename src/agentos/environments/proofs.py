"""Proofs: screenshots and recordings an agent keeps as proof of its work in an environment, and the
lookups the agent's side of an environment needs (which environment a lane is, its workdir). A mixin
of EnvironmentManager.

A proof is a PNG from the environment's browser (browser_screenshot(proof="...")), or a video of it
(browser_record, agentos.environments.recording: an MP4 and a GIF preview), kept on the node in
AGENTOS_HOME/environments/<id>/proofs/ with a JSON record beside it (`kind` "screenshot" or "video"),
so it outlives the VM. It is listed in environment(), in environment_close's result and on the app's
environment page. Each listing carries a short-lived signed link on the node's own address (the viewer
serves it, like the live view's links): `/view/env/<id>-browser/proof/<proof id>.png?t=...` (a video's
`.mp4`, its preview's `.gif`, with the same token), signed with the viewer key, so replacing that key
revokes them. Kept PROOF_DAYS after the environment closed, at most PROOFS_MAX for one environment,
and every environment's together at most PROOF_NODE_BYTES_MAX on the node, videos counted.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentos.core import clock
from agentos.core.errors import InvalidRequest, NotFound
from agentos.environments import media
from agentos.environments.models import WORKSPACE, Environment

log = logging.getLogger("agentos.environments")

PROOFS_MAX = 30                 # proofs one environment keeps (screenshots and videos)
PROOF_BYTES_MAX = 12 << 20      # one PNG (a full page is large)
VIDEO_BYTES_MAX = 64 << 20      # one recording's MP4 (a few MB a minute; GitHub takes 100 MB a file)
GIF_BYTES_MAX = 8 << 20         # its GIF preview (GitHub shows an image up to 10 MB inline)
PROOF_DAYS = 30                 # how long proofs stay after their environment closed
PROOF_NODE_BYTES_MAX = 1 << 30  # every environment's proofs on this node together: past it the oldest of
                                # closed environments go first, and a new one is refused when none can
PROOF_LINK_S = 900              # a signed link to one lives this long (environment() gives fresh ones)
PROOF_PURPOSE = "proof"
_ID_RE = re.compile(r"^p[a-z0-9]{10}$")
_ENV_RE = re.compile(r"^e[a-z0-9]{6,40}$")
_ID_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"
MEDIA = {".png": "image/png", ".mp4": "video/mp4", ".gif": "image/gif"}  # a proof's files, by their type


class Proofs:
    """For EnvironmentManager (its home, store, runtime and clock)."""

    # ================================================================ lanes
    def lane_environment(self, lane_id: str) -> Environment | None:
        """The environment a lane belongs to (env/<id> or env/<id>-browser), or None."""
        workspace, _, name = str(lane_id or "").partition("/")
        env_id = name.removesuffix("-browser")
        if workspace != WORKSPACE or not _ENV_RE.match(env_id):
            return None
        try:
            env = self.store.get(env_id)
        except (NotFound, ValueError, KeyError):
            return None
        return env if lane_id in (env.terminal_lane, env.browser_lane) else None

    def browser_environment(self, lane_id: str) -> Environment | None:
        """The open environment whose browser this lane is, or None (an account lane among them)."""
        env = self.lane_environment(lane_id)
        return env if env is not None and env.open and lane_id == env.browser_lane else None

    def is_browser_lane(self, lane_id: str) -> bool:
        """Is this lane an open environment's own browser (no account, no vault entry)? The viewer asks."""
        return self.browser_environment(lane_id) is not None

    def workdir_of(self, lane_id: str) -> str | None:
        """Where a relative path on an environment's machine starts: its repository's folder."""
        env = self.lane_environment(lane_id)
        return env.workdir if env is not None and lane_id == env.terminal_lane else None

    # ================================================================ proofs
    def _proof_dir(self, env_id: str) -> Path:
        if not _ENV_RE.match(env_id):
            raise NotFound(f"no environment {env_id}")
        return self.home / "environments" / env_id / "proofs"

    def keep_proof(self, env: Environment, png: bytes, *, title: str, page_url: str | None = None,
                   width: int | None = None, height: int | None = None) -> dict:
        """Store a screenshot as proof. Returns the proof as environment() lists it (with its link)."""
        if not png.startswith(b"\x89PNG"):
            raise InvalidRequest("a proof is a PNG")
        if len(png) > PROOF_BYTES_MAX:
            raise InvalidRequest(f"the shot is {len(png)} bytes; a proof is at most {PROOF_BYTES_MAX}. Take it "
                                 "without full_page")
        folder, proof_id = self._new_proof(env, len(png))
        record = {"id": proof_id, "kind": "screenshot", "title": title,
                  "taken_at": clock.iso_or_none(self._clock(), z=True),
                  "page_url": (page_url or None) and str(page_url)[:2000], "width": width, "height": height,
                  "bytes": len(png)}
        (folder / f"{proof_id}.png").write_bytes(png)
        (folder / f"{proof_id}.json").write_text(json.dumps(record))
        return self._listed(env.id, record)

    def keep_video(self, env: Environment, mp4: Path, gif: Path | None, *, title: str, ended: str = "stopped",
                   page_url: str | None = None) -> dict:
        """Store a recording (agentos.environments.recording) as proof: its MP4, and its GIF preview when
        there is one, moved into the proofs folder. Its length and size are read from the MP4 here, not
        taken from the machine. Returns the proof as environment() lists it."""
        size = mp4.stat().st_size
        facts = media.mp4_facts(mp4)
        if facts is None:
            raise InvalidRequest("the recording is not an MP4 this node can read")
        if size > VIDEO_BYTES_MAX:
            raise InvalidRequest(f"the recording is {size >> 20} MB; a video proof is at most {VIDEO_BYTES_MAX >> 20} "
                                 "MB. Record a shorter flow")
        preview = media.gif_size(gif) if gif is not None and gif.is_file() else None
        gif_bytes = gif.stat().st_size if preview is not None and gif is not None else 0
        if gif_bytes > GIF_BYTES_MAX:
            preview, gif_bytes = None, 0
        folder, proof_id = self._new_proof(env, size + gif_bytes)
        record = {"id": proof_id, "kind": "video", "title": title,
                  "taken_at": clock.iso_or_none(self._clock(), z=True),
                  "page_url": (page_url or None) and str(page_url)[:2000], "width": facts["width"],
                  "height": facts["height"], "duration_s": facts["duration_s"], "bytes": size, "ended": ended,
                  "gif": ({"width": preview[0], "height": preview[1], "bytes": gif_bytes} if preview else None)}
        shutil.move(str(mp4), folder / f"{proof_id}.mp4")
        if preview is not None and gif is not None:
            shutil.move(str(gif), folder / f"{proof_id}.gif")
        (folder / f"{proof_id}.json").write_text(json.dumps(record))
        return self._listed(env.id, record)

    def _new_proof(self, env: Environment, need: int) -> tuple[Path, str]:
        """The folder and a new id for one more proof of `env`, with room for `need` bytes on the node."""
        folder = self._proof_dir(env.id)
        if len(self._records(env.id)) >= PROOFS_MAX:
            raise InvalidRequest(f"environment {env.id} keeps at most {PROOFS_MAX} proofs; keep the ones that show "
                                 "the result")
        self._make_room(need)
        folder.mkdir(parents=True, exist_ok=True)
        return folder, "p" + "".join(secrets.choice(_ID_CHARS) for _ in range(10))

    def _proof_files(self) -> list[tuple[float, int, list[Path], str]]:
        """Every kept proof on this node: (taken, bytes of its files, its files, its environment's id)."""
        root = self.home / "environments"
        out: list[tuple[float, int, list[Path], str]] = []
        if not root.is_dir():
            return out
        for folder in root.iterdir():
            if not _ENV_RE.match(folder.name) or not (folder / "proofs").is_dir():
                continue
            by_id: dict[str, list[Path]] = {}
            for path in (folder / "proofs").glob("p*.*"):
                if path.suffix in MEDIA and _ID_RE.match(path.stem):
                    by_id.setdefault(path.stem, []).append(path)
            for paths in by_id.values():
                with contextlib.suppress(OSError):
                    stats = [p.stat() for p in paths]
                    out.append((min(st.st_mtime for st in stats), sum(st.st_size for st in stats), paths,
                                folder.name))
        return out

    def _closed(self, env_id: str) -> bool:
        try:
            return not self.store.get(env_id).open
        except (NotFound, ValueError, KeyError):
            return True  # one the node no longer knows is nobody's to keep

    def _make_room(self, need: int) -> None:
        """Keep every environment's proofs on this node (screenshots and videos) under
        PROOF_NODE_BYTES_MAX together, so no agent fills the disk the account lanes and the audit log
        share: the oldest proofs of closed environments go first; when only open ones' remain, the new
        proof is refused."""
        files = self._proof_files()
        total = sum(size for _, size, _, _ in files)
        for _, size, paths, env_id in sorted(files, key=lambda f: f[0]):
            if total + need <= PROOF_NODE_BYTES_MAX:
                return
            if not self._closed(env_id):
                continue
            for path in paths:
                path.unlink(missing_ok=True)
            paths[0].with_suffix(".json").unlink(missing_ok=True)
            total -= size
            log.info("proofs: removed %s of closed environment %s to stay under the node's cap", paths[0].stem, env_id)
        if total + need > PROOF_NODE_BYTES_MAX:
            raise InvalidRequest(f"this node keeps at most {PROOF_NODE_BYTES_MAX >> 20} MB of proofs and the open "
                                 "environments hold that much; keep fewer, or smaller ones (a shorter recording, "
                                 "a shot without full_page)")

    def _records(self, env_id: str) -> list[dict]:
        folder = self._proof_dir(env_id)
        out = []
        for path in sorted(folder.glob("p*.json")) if folder.is_dir() else []:
            with contextlib.suppress(OSError, ValueError):
                rec = json.loads(path.read_text())
                if isinstance(rec, dict) and _ID_RE.match(str(rec.get("id"))):
                    out.append(rec)
        out.sort(key=lambda r: str(r.get("taken_at") or ""))
        return out

    def proofs(self, env: Environment, *, links: bool = True) -> list[dict]:
        """The environment's proofs, oldest first: {id, kind, title, taken_at, page_url, width, height},
        a video's duration_s, bytes, ended and gif, and with `links` a signed `url` (a video's gif_url too)
        and its `expires_at`."""
        return [self._listed(env.id, r) if links else _public(r) for r in self._records(env.id)]

    def proof_path(self, env_id: str, proof_id: str, ext: str = "png") -> Path | None:
        """A proof's file: its PNG, or a video's MP4 or GIF; None when there is none."""
        if not _ID_RE.match(str(proof_id)) or f".{ext}" not in MEDIA:
            return None
        try:
            path = self._proof_dir(env_id) / f"{proof_id}.{ext}"
        except NotFound:
            return None
        return path if path.is_file() else None

    def proof_png(self, env_id: str, proof_id: str) -> bytes | None:
        path = self.proof_path(env_id, proof_id, "png")
        try:
            return path.read_bytes() if path is not None else None
        except OSError:
            return None

    def signed_proof_path(self, lane_workspace: str, lane_name: str, proof_id: str, ext: str,
                          token: str | None) -> Path | None:
        """The file a signed link names (the viewer's GET /view/env/<id>-browser/proof/<id>.<ext>), or None
        when the link is not valid, expired, or names another proof. One link opens every file of its proof."""
        env_id = lane_name.removesuffix("-browser")
        if lane_workspace != WORKSPACE or not lane_name.endswith("-browser"):
            return None
        tokens = self._tokens()
        if tokens is None or tokens.open_blob(PROOF_PURPOSE, token) != f"{env_id}/{proof_id}":
            return None
        return self.proof_path(env_id, proof_id, ext)

    def signed_proof(self, lane_workspace: str, lane_name: str, proof_id: str, token: str | None) -> bytes | None:
        """The PNG a signed link names, or None (see signed_proof_path)."""
        path = self.signed_proof_path(lane_workspace, lane_name, proof_id, "png", token)
        try:
            return path.read_bytes() if path is not None else None
        except OSError:
            return None

    def _tokens(self) -> Any:
        tokens = self._get("viewer_links")
        if tokens is None:
            config = self._get("config")
            if config is None:
                return None
            from agentos.node.viewer_tokens import ViewTokens

            tokens = ViewTokens.for_config(config)
        return tokens

    def _listed(self, env_id: str, record: dict) -> dict:
        out = _public(record)
        tokens = self._tokens()
        config = self._get("config")
        if tokens is None or config is None:
            return out
        try:
            token, expires = tokens.sign_blob(PROOF_PURPOSE, f"{env_id}/{record['id']}", PROOF_LINK_S)
        except Exception:
            log.warning("could not sign a link to proof %s", record.get("id"), exc_info=True)
            return out
        base = f"http://localhost:{config.mcp_port}/view/{WORKSPACE}/{env_id}-browser/proof/{record['id']}"
        if out["kind"] == "video":
            out["url"] = f"{base}.mp4?t={token}"
            if out.get("gif"):
                out["gif_url"] = f"{base}.gif?t={token}"
        else:
            out["url"] = f"{base}.png?t={token}"
        out["expires_at"] = clock.iso_or_none(datetime.fromtimestamp(expires, UTC), z=True)
        return out

    def prune_proofs(self, now: datetime | None = None) -> int:
        """Delete the proofs of environments closed more than PROOF_DAYS ago (and of ones the node no
        longer knows). Returns how many environments' proofs went."""
        root = self.home / "environments"
        if not root.is_dir():
            return 0
        cutoff = (now or self._clock()) - timedelta(days=PROOF_DAYS)
        gone = 0
        for folder in root.iterdir():
            if not _ENV_RE.match(folder.name) or not (folder / "proofs").is_dir():
                continue
            try:
                env = self.store.get(folder.name)
                ended = env.closed_at if not env.open else None
            except (NotFound, ValueError, KeyError):
                env, ended = None, None
            if env is not None and (ended is None or ended > cutoff):
                continue
            if env is None and datetime.fromtimestamp(folder.stat().st_mtime, UTC) > cutoff:
                continue
            shutil.rmtree(folder / "proofs", ignore_errors=True)
            gone += 1
        return gone


def _public(record: dict) -> dict:
    out = {k: record.get(k) for k in ("id", "title", "taken_at", "page_url", "width", "height")}
    out["kind"] = "video" if record.get("kind") == "video" else "screenshot"
    if out["kind"] == "video":
        gif = record.get("gif") if isinstance(record.get("gif"), dict) else None
        out.update(duration_s=record.get("duration_s"), bytes=record.get("bytes"), ended=record.get("ended"),
                   gif={k: gif.get(k) for k in ("width", "height", "bytes")} if gif else None)
    return out
