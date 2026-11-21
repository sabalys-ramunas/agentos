"""Who a brain runs as, for BrainService: the brain identity (agentosd as root, or the agentos-runas
helper), the launcher, the brain's own directories and files, and the AUD-08 confinement check
(openajar.brains.confine) that must pass before any brain starts.
"""

from __future__ import annotations

import os
import pwd
import shutil
import sys
import time
from pathlib import Path, PurePosixPath

from openajar import runas
from openajar.brains import confine, launch
from openajar.core import fs
from openajar.core.errors import InvalidRequest

IDENTITY_TTL = 5.0     # brain_identity() asks the helper again after this long


class IdentityMixin:
    """BrainService's identity and confinement methods. Uses self.settings, self.dir and self.clock."""

    def _helper_socket(self) -> Path | None:
        """The agentos-runas socket, when agentosd is not root and the helper is up."""
        if os.geteuid() == 0 or not self.settings.runas_socket:
            return None
        path = Path(self.settings.runas_socket)
        try:
            return path if path.is_socket() else None
        except OSError:
            return None

    def brain_identity(self) -> launch.BrainIdentity:
        """Who brains run as: one answer for the confinement probe, the attempt, and BrainFiles.

            root agentosd          drops to run_as itself (an account that must exist)
            non-root agentosd      asks agentos-runas, which must serve exactly run_as (whoami), a
                                   user other than agentosd's own
            neither                agentosd's own user: only where the confinement check is not
                                   required (dev on macOS, or confinement = "off") and no helper is
                                   up; anywhere else it is refused

        Raises BrainUnconfined when run_as names no account, the helper is down or serves another
        user, or the brain user would be agentosd's own. Resolved at most every few seconds."""
        now = time.monotonic()
        if self._identity is not None and now - self._identity[0] < IDENTITY_TTL:
            return self._identity[1]
        ident = self._resolve_identity()
        self._identity = (now, ident)
        return ident

    def _resolve_identity(self) -> launch.BrainIdentity:
        name = self.settings.run_as
        me = os.geteuid()
        sock = self._helper_socket()  # None when root
        if me != 0 and sock is None and not self.confinement_required():
            return launch.BrainIdentity(None, me, os.getegid(), "")  # dev: brains as agentosd's own user
        if not name:
            if me != 0:
                raise confine.BrainUnconfined("brains have no user of their own (brains settings run_as is empty) "
                                              "and agentosd is not root: they would run as agentosd's own user")
            return launch.BrainIdentity(None, me, os.getegid(), "")  # root itself: the probe refuses it
        try:
            pw = pwd.getpwnam(name)
        except KeyError:
            raise confine.BrainUnconfined(f"the brain user {name} (brains settings run_as) does not exist; brains "
                                          "would run as agentosd's own user") from None
        if pw.pw_uid == me or pw.pw_uid == 0:
            raise confine.BrainUnconfined(f"the brain user {name} is uid {pw.pw_uid}, which is agentosd's own or "
                                          "root; brains need a user of their own")
        if me == 0:
            return launch.BrainIdentity(name, pw.pw_uid, pw.pw_gid, pw.pw_dir)
        if sock is None:
            raise confine.BrainUnconfined(f"agentosd is not root and the agentos-runas helper is not up at "
                                          f"{self.settings.runas_socket or '(no runas_socket)'}, so brains cannot "
                                          f"start as {name}")
        try:
            info = runas.whoami(sock)
        except runas.RunasError as exc:
            raise confine.BrainUnconfined(f"the agentos-runas helper did not say who brains run as: {exc.message}"
                                          ) from None
        b = info.get("brain") or {}
        if b.get("name") != name or b.get("uid") != pw.pw_uid or b.get("gid") != pw.pw_gid:
            raise confine.BrainUnconfined(
                f"the agentos-runas helper starts brains as {b.get('name')} (uid {b.get('uid')}), not {name} "
                f"(uid {pw.pw_uid}); make its --brain-user and brains settings run_as name the same user")
        return launch.BrainIdentity(name, pw.pw_uid, pw.pw_gid, pw.pw_dir, helper=sock,
                                    groups=tuple(int(g) for g in b.get("groups") or ()),
                                    scopes=bool(info.get("scopes")))

    def _run_as(self) -> pwd.struct_passwd | None:
        """The brain user's account when brains run as one (from brain_identity), else None."""
        ident = self.brain_identity()
        return pwd.getpwnam(ident.name) if ident.name else None

    def _launcher(self, identity: str = "brain") -> launch.Launcher:
        """How programs start as the brain (or, `caller`, the detached shim as agentosd itself)."""
        ident = self.brain_identity()
        if ident.helper is not None:
            caller = identity == "caller"
            return launch.HelperLauncher(ident.helper, identity, detach=caller,
                                         expect_uid=os.geteuid() if caller else ident.uid)
        if identity == "brain" and ident.name:
            return launch.DirectLauncher({"uid": ident.uid, "gid": ident.gid})
        return launch.DirectLauncher(None)

    def _via_helper(self) -> bool:
        return self.brain_identity().helper is not None

    def _brain_files(self) -> launch.BrainFiles:
        return launch.BrainFiles(self._launcher())

    def _brain_dirs(self, task_id: str) -> tuple[Path, Path]:
        """(work, home) for the brain process. With an unprivileged brain user they live in
        that user's home (so OPENAJAR_HOME stays closed to it) and are linked from the task dir.
        A non-root agentosd cannot chown, so the brain user makes them itself (BrainFiles)."""
        tdir = self.task_dir(task_id)
        user = self._run_as()
        if user is None:
            work, home = tdir / "work", tdir / "home"
            for d in (work, home, home / "tmp"):
                d.mkdir(parents=True, exist_ok=True)
            os.chmod(home, 0o700)
            return work, home
        base = Path(user.pw_dir) / "tasks" / task_id
        work, home = base / "work", base / "home"
        if self._via_helper():
            self._brain_files().mkdirs(Path(user.pw_dir) / "tasks", base, work, home, home / "tmp")
        else:
            # The brain owns everything below its home and may have swapped any of these for a link
            # (between attempts, or from another task): resolve each without following one, then
            # chown and chmod the directory that was opened, never a path.
            own = (user.pw_uid, user.pw_gid)
            for d in (base.parent, base, work, home, home / "tmp"):
                fd = fs.open_dir(user.pw_dir, d.relative_to(user.pw_dir), make=True, mode=0o700, owner=own)
                try:
                    os.fchown(fd, *own)
                    os.fchmod(fd, 0o700)
                finally:
                    os.close(fd)
        for name, target in (("work", work), ("home", home)):
            link = tdir / name
            if not os.path.lexists(link):  # never follow it: agentosd may not stat the brain's side
                link.symlink_to(target)
        return work, home

    def _beneath(self, path: Path) -> tuple[Path, PurePosixPath]:
        """(anchor, rel) for a path in a task's work or home dir, for the `fs.*_beneath` calls: the
        anchor is a directory the brain cannot replace (the brain user's home itself, or
        agentosd's own tasks dir), and every name below it is resolved without following a
        link. A path through the task dir's own link (tasks/<id>/home) maps to where it points."""
        user = self._run_as()
        anchor = Path(user.pw_dir) if user is not None else self.tasks_dir
        try:
            return anchor, PurePosixPath(path.relative_to(anchor))
        except ValueError:
            rel = PurePosixPath(path.relative_to(self.tasks_dir))  # <id>/work/...: the daemon's link
            return anchor, (PurePosixPath("tasks") / rel if user is not None else rel)

    def _write_brain_file(self, path: Path, content: str, mode: int) -> None:
        if self._via_helper():
            self._brain_files().write(path, content, mode)
            return
        user = self._run_as()
        own = (user.pw_uid, user.pw_gid) if user is not None and os.geteuid() == 0 else None
        anchor, rel = self._beneath(path)
        # a link at the name is replaced; a linked directory on the way refuses the attempt
        fs.write_beneath(anchor, rel, content, mode=mode, owner=own, dir_mode=0o700)

    # ------------------------------------------------------------------ confinement (AUD-08)
    def confinement_required(self) -> bool:
        mode = self.settings.confinement
        if mode == "off":
            return False
        return mode == "require" or sys.platform.startswith("linux")

    def confinement(self) -> confine.Confinement:
        if self._confinement is None:
            cfg = self.runtime.get("config") if self.runtime is not None else None
            self._confinement = confine.Confinement(
                self.home, launcher=self._launcher, identity=self.brain_identity, python=confine.probe_python(),
                config=cfg, cache_dir=self.dir)
        return self._confinement

    def check_confinement(self, *, force: bool = False) -> confine.Result:
        """Run (or reuse) the AUD-08 check. Raises BrainUnconfined when it fails."""
        result = self.confinement().check(force=force)
        if not result.cached:
            self.ports.trace("brain.confinement", ok=result.ok, reason=result.reason, launcher=result.launcher,
                             uid=result.uid, checked=result.checked)
        if not result.ok:
            raise confine.BrainUnconfined(result.reason)
        return result

    def _scope_limits(self) -> dict[str, int]:
        return {"memory_mb": int(self.settings.memory_mb), "tasks_max": int(self.settings.tasks_max),
                "cpu_quota_pct": int(self.settings.cpu_quota_pct)}

    def _use_scope(self) -> bool:
        mode = self.settings.isolation
        if mode == "none":
            return False
        available = (shutil.which("systemd-run") is not None and os.geteuid() == 0
                     and Path("/run/systemd/system").exists())
        if mode == "scope" and not available:
            raise InvalidRequest("isolation=scope needs systemd and root on the node")
        return available

    def mcp_url(self) -> str:
        if self.settings.mcp_url:
            return self.settings.mcp_url
        cfg = self.runtime.get("config") if self.runtime is not None else None
        return f"http://127.0.0.1:{getattr(cfg, 'mcp_port', 7400)}/mcp"
