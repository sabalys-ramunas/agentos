"""Proofs: screenshots an agent keeps as proof of its work in an environment, and the lookups the
agent's side of an environment needs (which environment a lane is, its workdir). A mixin of
EnvironmentManager.

A proof is a PNG from the environment's browser (browser_screenshot(proof="...")), kept on the node
in AGENTOS_HOME/environments/<id>/proofs/ with a JSON record beside it, so it outlives the VM. It is
listed in environment(), in environment_close's result and on the app's environment page. Each
listing carries a short-lived signed link on the node's own address (the viewer serves it, like the
live view's links): `/view/env/<id>-browser/proof/<proof id>.png?t=...`, signed with the viewer key,
so replacing that key revokes them. Kept PROOF_DAYS after the environment closed, at most
PROOFS_MAX for one environment.
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
from agentos.environments.models import WORKSPACE, Environment

log = logging.getLogger("agentos.environments")

PROOFS_MAX = 30                 # proofs one environment keeps
PROOF_BYTES_MAX = 12 << 20      # one PNG (a full page is large)
PROOF_DAYS = 30                 # how long proofs stay after their environment closed
PROOF_NODE_BYTES_MAX = 1 << 30  # every environment's proofs on this node together: past it the oldest of
                                # closed environments go first, and a new one is refused when none can
PROOF_LINK_S = 900              # a signed link to one lives this long (environment() gives fresh ones)
PROOF_PURPOSE = "proof"
_ID_RE = re.compile(r"^p[a-z0-9]{10}$")
_ENV_RE = re.compile(r"^e[a-z0-9]{6,40}$")
_ID_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"


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
        folder = self._proof_dir(env.id)
        if len(self._records(env.id)) >= PROOFS_MAX:
            raise InvalidRequest(f"environment {env.id} keeps at most {PROOFS_MAX} proofs; keep the ones that show "
                                 "the result")
        self._make_room(len(png))
        folder.mkdir(parents=True, exist_ok=True)
        proof_id = "p" + "".join(secrets.choice(_ID_CHARS) for _ in range(10))
        record = {"id": proof_id, "title": title, "taken_at": clock.iso_or_none(self._clock(), z=True),
                  "page_url": (page_url or None) and str(page_url)[:2000], "width": width, "height": height,
                  "bytes": len(png)}
        (folder / f"{proof_id}.png").write_bytes(png)
        (folder / f"{proof_id}.json").write_text(json.dumps(record))
        return self._listed(env.id, record)

    def _proof_files(self) -> list[tuple[float, int, Path, str]]:
        """Every kept proof on this node: (taken, bytes, its PNG, its environment's id)."""
        root = self.home / "environments"
        out: list[tuple[float, int, Path, str]] = []
        if not root.is_dir():
            return out
        for folder in root.iterdir():
            if not _ENV_RE.match(folder.name) or not (folder / "proofs").is_dir():
                continue
            for png in (folder / "proofs").glob("p*.png"):
                with contextlib.suppress(OSError):
                    st = png.stat()
                    out.append((st.st_mtime, st.st_size, png, folder.name))
        return out

    def _closed(self, env_id: str) -> bool:
        try:
            return not self.store.get(env_id).open
        except (NotFound, ValueError, KeyError):
            return True  # one the node no longer knows is nobody's to keep

    def _make_room(self, need: int) -> None:
        """Keep every environment's proofs on this node under PROOF_NODE_BYTES_MAX together, so no agent
        fills the disk the account lanes and the audit log share: the oldest proofs of closed
        environments go first; when only open ones' remain, the new proof is refused."""
        files = self._proof_files()
        total = sum(size for _, size, _, _ in files)
        for _, size, png, env_id in sorted(files, key=lambda f: f[0]):
            if total + need <= PROOF_NODE_BYTES_MAX:
                return
            if not self._closed(env_id):
                continue
            png.unlink(missing_ok=True)
            png.with_suffix(".json").unlink(missing_ok=True)
            total -= size
            log.info("proofs: removed %s of closed environment %s to stay under the node's cap", png.name, env_id)
        if total + need > PROOF_NODE_BYTES_MAX:
            raise InvalidRequest(f"this node keeps at most {PROOF_NODE_BYTES_MAX >> 20} MB of proofs and the open "
                                 "environments hold that much; keep fewer, or smaller ones (without full_page)")

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
        """The environment's proofs, oldest first: {id, title, taken_at, page_url, width, height}, and
        with `links` a signed `url` and its `expires_at`."""
        return [self._listed(env.id, r) if links else _public(r) for r in self._records(env.id)]

    def proof_png(self, env_id: str, proof_id: str) -> bytes | None:
        if not _ID_RE.match(str(proof_id)):
            return None
        try:
            path = self._proof_dir(env_id) / f"{proof_id}.png"
        except NotFound:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def signed_proof(self, lane_workspace: str, lane_name: str, proof_id: str, token: str | None) -> bytes | None:
        """The PNG a signed link names (the viewer's GET /view/env/<id>-browser/proof/<id>.png), or None
        when the link is not valid, expired, or names another proof."""
        env_id = lane_name.removesuffix("-browser")
        if lane_workspace != WORKSPACE or not lane_name.endswith("-browser"):
            return None
        tokens = self._tokens()
        if tokens is None or tokens.open_blob(PROOF_PURPOSE, token) != f"{env_id}/{proof_id}":
            return None
        return self.proof_png(env_id, proof_id)

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
        out["url"] = (f"http://localhost:{config.mcp_port}/view/{WORKSPACE}/{env_id}-browser/proof/"
                      f"{record['id']}.png?t={token}")
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
    return {k: record.get(k) for k in ("id", "title", "taken_at", "page_url", "width", "height")}
