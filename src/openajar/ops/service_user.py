"""`openajar migrate-service-user`: move agentosd from root to its own user, and back.

The installer already knows how to run agentosd as a dedicated user with the hardened unit
(`deploy/install-node.sh --systemd --user agentos --hardened`). This makes the switch on a node
that runs as root one reversible command:

  1. preflight      root, systemd, an installed prefix with the hardened unit, OPENAJAR_HOME
                    outside /root and /home, the service runs as root today, and nothing on the
                    node needs root (see below). Fails: nothing changed.
  2. save           the installed unit, /etc/logrotate.d/agentos and the owner of OPENAJAR_HOME
                    go to /etc/systemd/system/<unit>.service-user-rollback.json (0600). Fails:
                    nothing changed.
  3. drain, stop    every warm lane closes gracefully (as `openajar upgrade`), then the unit stops.
                    A failed stop starts it again and changes nothing.
  4. install        the prefix's own installer with --systemd --no-restart --skip-apt --user U
                    --hardened: creates the user, chowns OPENAJAR_HOME to it (and go-rwx), copies
                    root's Camoufox into OPENAJAR_HOME/.cache, installs the hardened unit, creates
                    the brain user (brains settings `run_as`, passed as OPENAJAR_BRAIN_USER) and
                    installs agentos-runas.service (openajar.runas), the root helper that starts
                    brains as exactly that user for an unprivileged agentosd.
  5. start, verify  the unit is active as U, MCP answers (401 without the token, 2xx with it),
                    OPENAJAR_HOME and everything in it belong to U with private modes (doctor),
                    agentos-runas is active and installed for that brain user, and the AUD-08
                    confinement check passes: run as U, it asks the helper who brains run as
                    (it must be run_as), starts the probe as that user through the helper, and
                    the probe can neither traverse OPENAJAR_HOME nor reach the other places it
                    checks (openajar.brains.confine).
  Any failure in 4 or 5 rolls back on the spot, the same way `--rollback` does later: stop, put
  the saved unit and logrotate file back, remove agentos-runas (or put back the one that was
  there), chown OPENAJAR_HOME to its old owner, start, verify as root. The saved state stays until
  a rollback succeeds, so `--rollback` works days later.

What refuses the switch, because it would quietly get worse rather than fail:
- brains have no user of their own (brains settings `run_as` is empty), or the confinement check is
  turned off (`confinement = "off"`): under U a brain could end up running as U next to the vault;
- the brain user is root, is U itself, is a login account, or is in U's group, a privileged group
  (openajar.runas.PRIVILEGED_GROUPS: adm, sudo, docker, systemd-journal, disk...) or a group that
  owns anything in OPENAJAR_HOME (it could read what those groups can);
- the prefix predates agentos-runas (no deploy/agentos-runas.service): upgrade it first;
- isolated terminal or service lanes exist: they need root (namespaces, mounts) and would stop. An
  env database that cannot be read, or a lane spec that cannot be parsed, refuses too: an isolated
  lane could be hidden in it;
- `--user` is an existing login account (a home under /home, or a shell other than nologin or false):
  the vault would be handed to a person's account.
"""

from __future__ import annotations

import contextlib
import grp
import json
import os
import pwd
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from openajar.core.layout import ops_socket
from openajar.core.system import Host
from openajar.runas import PRIVILEGED_GROUPS

HIDDEN_BY_PROTECT_HOME = ("/root/", "/home/", "/run/user/")


class SwitchError(Exception):
    pass


@dataclass
class SwitchPlan:
    prefix: Path
    home: Path
    user: str = "agentos"
    unit: str = "agentosd"
    etc: Path = Path("/etc")
    health_timeout: float = 90.0
    drain_timeout: float = 60.0
    brain_user: str | None = None      # brains settings run_as, read by preflight; the installer gets it

    @property
    def unit_path(self) -> Path:
        return self.etc / "systemd" / "system" / f"{self.unit}.service"

    @property
    def logrotate_path(self) -> Path:
        return self.etc / "logrotate.d" / "agentos"

    @property
    def runas_path(self) -> Path:
        return self.etc / "systemd" / "system" / "agentos-runas.service"

    @property
    def state_path(self) -> Path:
        return self.etc / "systemd" / "system" / f"{self.unit}.service-user-rollback.json"


class Steps(Protocol):
    def preflight(self, plan: SwitchPlan) -> None: ...
    def save_state(self, plan: SwitchPlan) -> dict[str, Any]: ...
    def load_state(self, plan: SwitchPlan) -> dict[str, Any] | None: ...
    def clear_state(self, plan: SwitchPlan) -> None: ...
    def drain(self, plan: SwitchPlan) -> dict[str, Any]: ...
    def stop(self, plan: SwitchPlan) -> None: ...
    def install(self, plan: SwitchPlan) -> None: ...
    def start(self, plan: SwitchPlan) -> None: ...
    def verify(self, plan: SwitchPlan, user: str) -> tuple[bool, str]: ...
    def restore(self, plan: SwitchPlan, state: dict[str, Any]) -> None: ...


@dataclass
class SwitchResult:
    ok: bool = False
    rolled_back: bool = False
    state: str = "unchanged"  # unchanged | switched | rolled_back | broken
    error: str | None = None
    steps: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "rolled_back": self.rolled_back, "state": self.state, "error": self.error,
                "steps": self.steps}


class _Recorder:
    def __init__(self, result: SwitchResult, log: Callable[[str], None]):
        self.result, self.log = result, log

    def record(self, name: str, status: str, detail: str = "") -> None:
        self.result.steps.append({"step": name, "status": status, "detail": detail})
        self.log(f"{name}: {status}{f' ({detail})' if detail else ''}")

    def attempt(self, name: str, fn: Callable[[], Any]) -> Any:
        try:
            out = fn()
        except Exception as e:
            self.record(name, "failed", f"{type(e).__name__}: {e}")
            raise
        self.record(name, "ok")
        return out

    def drain(self, plan: SwitchPlan, steps: Steps) -> None:
        try:
            drained = steps.drain(plan)
            self.record("drain", "ok", ", ".join(drained.get("closed", [])) or drained.get("note", "no warm lanes"))
        except Exception as e:  # noqa: BLE001 - systemctl stop closes lanes gracefully too
            self.record("drain", "skipped", f"{type(e).__name__}: {e}; the stop closes the lanes")


def _back_to_root(plan: SwitchPlan, steps: Steps, state: dict[str, Any], rec: _Recorder, prefix: str) -> bool:
    """Stop, restore the saved unit and owner, start, verify as the old user. True when healthy.
    The start runs even when the restore failed: a root agentosd left stopped is the worst outcome,
    so both errors are raised together and the caller keeps the saved state."""
    with contextlib.suppress(Exception):
        steps.stop(plan)
    restore_error: Exception | None = None
    try:
        rec.attempt(f"{prefix}restore", lambda: steps.restore(plan, state))
    except Exception as e:  # noqa: BLE001 - raised below, once the start has been tried
        restore_error = e
    try:
        rec.attempt(f"{prefix}start", lambda: steps.start(plan))
    except Exception as e:
        if restore_error is None:
            raise
        raise SwitchError(f"{restore_error}; starting {plan.unit} failed too: {e}") from e
    if restore_error is not None:
        raise restore_error
    ok, detail = steps.verify(plan, state.get("user") or "root")
    rec.record(f"{prefix}verify", "ok" if ok else "failed", detail)
    return ok


def run_switch(plan: SwitchPlan, steps: Steps, log: Callable[[str], None] = print) -> SwitchResult:
    result = SwitchResult()
    rec = _Recorder(result, log)
    try:
        rec.attempt("preflight", lambda: steps.preflight(plan))
        state = rec.attempt("save", lambda: steps.save_state(plan))
    except Exception as e:  # noqa: BLE001
        result.error = str(e)
        return result
    rec.drain(plan, steps)
    try:
        rec.attempt("stop", lambda: steps.stop(plan))
    except Exception as e:  # noqa: BLE001
        result.error = f"could not stop {plan.unit}: {e}"
        with contextlib.suppress(Exception):
            steps.start(plan)
        return result

    failure: str | None = None
    try:
        rec.attempt("install", lambda: steps.install(plan))
        rec.attempt("start", lambda: steps.start(plan))
        ok, detail = steps.verify(plan, plan.user)
        rec.record("verify", "ok" if ok else "failed", detail)
        if not ok:
            failure = f"{plan.unit} is not healthy as {plan.user}: {detail}"
    except Exception as e:  # noqa: BLE001
        failure = f"{type(e).__name__}: {e}"
    if failure is None:
        result.ok, result.state = True, "switched"
        return result

    result.error, result.rolled_back = failure, True
    log(f"rolling back: {failure}")
    try:
        result.state = "rolled_back" if _back_to_root(plan, steps, state, rec, "rollback.") else "broken"
    except Exception as e:  # noqa: BLE001
        result.state = "broken"
        result.error = f"{failure}; the rollback failed too: {type(e).__name__}: {e}"
    return result


def run_rollback(plan: SwitchPlan, steps: Steps, log: Callable[[str], None] = print) -> SwitchResult:
    """Undo an earlier switch from its saved state. The state file goes only once it worked."""
    result = SwitchResult()
    rec = _Recorder(result, log)
    try:
        state = steps.load_state(plan)
    except SwitchError as e:
        result.error = str(e)
        return result
    if state is None:
        result.error = f"nothing to roll back: {plan.state_path} does not exist"
        return result
    rec.drain(plan, steps)
    try:
        ok = _back_to_root(plan, steps, state, rec, "")
    except Exception as e:  # noqa: BLE001
        result.state, result.rolled_back = "broken", True
        result.error = f"the rollback failed: {type(e).__name__}: {e}"
        return result
    result.rolled_back = True
    if not ok:
        result.state = "broken"
        result.error = f"{plan.unit} is not healthy after the rollback; the saved state is kept in {plan.state_path}"
        return result
    steps.clear_state(plan)
    result.ok, result.state = True, "rolled_back"
    return result


# ---- the real steps ------------------------------------------------------------------------

def group_names(user: str) -> set[str]:
    """The groups `user` is in: its primary group and every group listing it."""
    out = {g.gr_name for g in grp.getgrall() if user in g.gr_mem}
    with contextlib.suppress(KeyError):
        out.add(grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name)
    return out


def owning_groups(home: Path) -> set[str]:
    """The groups that own OPENAJAR_HOME or anything in it (a brain user in one could read it)."""
    gids: set[int] = set()
    for root, dirs, files in os.walk(home):
        for n in [".", *dirs, *files]:
            with contextlib.suppress(OSError):
                gids.add(os.lstat(os.path.join(root, n)).st_gid)
    out = set()
    for gid in gids:
        try:
            out.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            out.add(str(gid))
    return out


def lookup_user(name: str) -> pwd.struct_passwd | None:
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def _is_service_account(account: Any) -> bool:
    """No login shell and no home under /home: what useradd --system makes, and not a person's account."""
    shell = os.path.basename(account.pw_shell)
    return shell in ("nologin", "false") and not f"{account.pw_dir}/".startswith("/home/")


def isolated_env_lanes(home: Path) -> list[str]:
    """Terminal and service lanes that run in the bubblewrap sandbox (they need root). Fails closed: a
    database it cannot read, or a spec it cannot parse, could hide an isolated lane, so it raises."""
    db = home / "env.db"
    if not db.exists():
        return []
    from openajar.env.models import EnvLane

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT id, spec FROM lanes ORDER BY id").fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        raise SwitchError(f"cannot read {db} to check for isolated lanes ({e}); the switch is refused") from e
    out = []
    for lane_id, spec in rows:
        try:
            lane = EnvLane.model_validate_json(spec)
        except ValueError:  # pydantic's ValidationError is one
            raise SwitchError(f"the spec of env lane {lane_id} cannot be read, so it cannot be checked for "
                              "isolation; the switch is refused") from None
        if lane.isolated:
            out.append(lane.id)
    return out


class SystemSteps:
    def __init__(self, host: Host | None = None, log: Callable[[str], None] = print,
                 users: Callable[[str], Any] = lookup_user, sleep: Callable[[float], None] = time.sleep,
                 groups: Callable[[str], set[str]] = group_names,
                 home_groups: Callable[[Path], set[str]] | None = None):
        self.host = host or Host()
        self.log = log
        self.users = users
        self.sleep = sleep
        self.groups = groups
        self.home_groups = home_groups or owning_groups

    def _run(self, argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 120) -> str:
        rc, out = self.host.run(argv, timeout=timeout, env=env)
        if rc != 0:
            tail = "\n".join(out.strip().splitlines()[-8:])
            raise SwitchError(f"{' '.join(argv[:3])} exited {rc}{f': {tail}' if tail else ''}")
        return out

    def _unit_user(self, plan: SwitchPlan) -> str:
        _, out = self.host.run(["systemctl", "show", plan.unit, "--no-pager", "-pUser", "-pLoadState"])
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if props.get("LoadState") == "not-found":
            raise SwitchError(f"{plan.unit}.service is not installed; install it first with "
                              f"deploy/install-node.sh --systemd --user {plan.user} --hardened")
        return props.get("User") or "root"

    def preflight(self, plan: SwitchPlan) -> None:
        from openajar.brains.settings import load_settings

        if self.host.euid() != 0:
            raise SwitchError("needs root: run it with sudo")
        if not self.host.which("systemctl"):
            raise SwitchError("systemctl not found; a dev install has no service to switch")
        for need in (plan.prefix / ".venv" / "bin" / "openajar", plan.prefix / "deploy" / "install-node.sh",
                     plan.prefix / "deploy" / "agentosd-hardened.service"):
            if not self.host.exists(need):
                raise SwitchError(f"{need} is missing; is {plan.prefix} the install prefix?")
        if not self.host.exists(plan.prefix / "deploy" / "agentos-runas.service"):
            raise SwitchError(f"{plan.prefix} predates agentos-runas (no deploy/agentos-runas.service), which "
                              "starts brains as their own user once agentosd is not root; upgrade it first")
        if any(f"{p}/".startswith(HIDDEN_BY_PROTECT_HOME) for p in (plan.home, plan.home.resolve())):
            raise SwitchError(f"{plan.home} is under /root or /home, which the hardened unit hides (ProtectHome)")
        if not self.host.exists(plan.home / "node.toml"):
            raise SwitchError(f"no node.toml in {plan.home}")
        if plan.user == "root" or not plan.user:
            raise SwitchError("the service user cannot be root")
        account = self.users(plan.user)
        if account is not None and not _is_service_account(account):
            raise SwitchError(f"{plan.user} is a login account (home {account.pw_dir}, shell {account.pw_shell}): "
                              "the vault would be handed to a person's account. Pick a user that does not exist "
                              "yet, or a service account with a nologin shell")
        current = self._unit_user(plan)
        if current == plan.user:
            raise SwitchError(f"{plan.unit} already runs as {plan.user}")
        if current != "root":
            raise SwitchError(f"{plan.unit} runs as {current}, not root; switch from root only")
        settings = load_settings(plan.home)
        self._check_brain_user(plan, settings)
        plan.brain_user = settings.run_as  # the installer creates and the helper serves exactly this user
        envs = isolated_env_lanes(plan.home)
        if envs:
            raise SwitchError(f"isolated terminal or service lanes need root and would stop working: "
                              f"{', '.join(envs)}")

    def _check_brain_user(self, plan: SwitchPlan, settings: Any) -> None:
        """Brains must keep a user of their own under U: agentos-runas starts them as it, and the
        confinement check refuses any brain that could read OPENAJAR_HOME anyway."""
        brain_user = settings.run_as
        if not brain_user:
            raise SwitchError(f"brains have no user of their own (brains settings run_as is empty): under {plan.user} "
                              f"they could run as {plan.user}, next to the vault. Set run_as (default agentos-brain)")
        if settings.confinement == "off":
            raise SwitchError("the brains confinement check is off (brains settings confinement = \"off\"); turn it "
                              "back on (auto) before agentosd leaves root")
        if brain_user in ("root", plan.user):
            raise SwitchError(f"the brain user cannot be {brain_user}: brains must not read {plan.home}")
        account = self.users(brain_user)
        if account is None:
            return  # the installer creates it
        if getattr(account, "pw_uid", None) == 0:
            raise SwitchError(f"the brain user {brain_user} has uid 0")
        if not _is_service_account(account):
            raise SwitchError(f"the brain user {brain_user} is a login account (home {account.pw_dir}, shell "
                              f"{account.pw_shell}); brains need a service account of their own")
        member = self.groups(brain_user)
        if plan.user in member:
            raise SwitchError(f"the brain user {brain_user} is in the {plan.user} group, so it could read "
                              f"group-readable files of {plan.user}; take it out first (gpasswd -d {brain_user} "
                              f"{plan.user})")
        bad = sorted(member & (set(PRIVILEGED_GROUPS) | self.home_groups(plan.home)))
        if bad:
            raise SwitchError(f"the brain user {brain_user} is in the group{'s' if len(bad) > 1 else ''} "
                              f"{', '.join(bad)} (privileged, or owning files in {plan.home}); take it out first "
                              f"(gpasswd -d {brain_user} <group>)")

    def save_state(self, plan: SwitchPlan) -> dict[str, Any]:
        if plan.state_path.exists():
            raise SwitchError(f"{plan.state_path} exists from an earlier switch; run --rollback first or remove it")
        st = self.host.lstat(plan.home)
        if st is None:
            raise SwitchError(f"{plan.home} does not exist")
        if not plan.unit_path.exists():
            raise SwitchError(f"{plan.unit_path} is missing")
        state = {
            "user": "root",
            "owner": f"{st.st_uid}:{st.st_gid}",
            "unit": plan.unit_path.read_text(),
            "logrotate": plan.logrotate_path.read_text() if plan.logrotate_path.exists() else None,
            # the helper the switch installs: none before (removed by a rollback), or the one to put back
            "runas_unit": plan.runas_path.read_text() if plan.runas_path.exists() else None,
            "switched_to": plan.user,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp = plan.state_path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)  # O_EXCL makes a new file: a leftover would keep its old mode
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(json.dumps(state, indent=2))
        tmp.replace(plan.state_path)
        return state

    def load_state(self, plan: SwitchPlan) -> dict[str, Any] | None:
        if not plan.state_path.exists():
            return None
        try:
            state = json.loads(plan.state_path.read_text())
        except (OSError, ValueError) as e:
            raise SwitchError(f"{plan.state_path} is not a readable rollback state ({e}); the file is kept, "
                              f"and the unit cannot be restored from it") from None
        if not isinstance(state, dict) or not {"unit", "owner"} <= state.keys():
            raise SwitchError(f"{plan.state_path} is not a rollback state (no saved unit and owner); the file is "
                              "kept, and the unit cannot be restored from it")
        if not plan.unit_path.exists():
            raise SwitchError(f"{plan.unit_path} is missing: a rollback puts the saved unit back, it does not "
                              "create one")
        return state

    def clear_state(self, plan: SwitchPlan) -> None:
        plan.state_path.unlink(missing_ok=True)

    def drain(self, plan: SwitchPlan) -> dict[str, Any]:
        resp = self.host.unix_request(ops_socket(plan.home), {"op": "drain", "timeout": plan.drain_timeout},
                                      timeout=plan.drain_timeout + 30)
        if resp is None:
            return {"closed": [], "note": "no ops socket; the stop closes the lanes"}
        if not resp.get("ok"):
            raise SwitchError(resp.get("error", {}).get("message", "drain failed"))
        return resp

    def stop(self, plan: SwitchPlan) -> None:
        self._run(["systemctl", "stop", plan.unit])

    def start(self, plan: SwitchPlan) -> None:
        self._run(["systemctl", "start", plan.unit])

    def install(self, plan: SwitchPlan) -> None:
        if not plan.brain_user:
            raise SwitchError("no brain user: preflight reads it from brains settings run_as")
        argv = ["bash", str(plan.prefix / "deploy" / "install-node.sh"), "--systemd", "--no-restart", "--skip-apt",
                "--user", plan.user, "--hardened"]
        # root's own environment: the installer finds uv and root's Camoufox through HOME
        out = self._run(argv, env={**os.environ, "INSTALL_PREFIX": str(plan.prefix), "OPENAJAR_HOME": str(plan.home),
                                   "OPENAJAR_BRAIN_USER": plan.brain_user}, timeout=1800)
        for line in out.splitlines():
            if line.startswith("==>"):
                self.log(f"  {line}")

    def restore(self, plan: SwitchPlan, state: dict[str, Any]) -> None:
        if not plan.unit_path.exists():
            raise SwitchError(f"{plan.unit_path} is missing: a rollback puts the saved unit back, it does not "
                              "create one")
        plan.unit_path.write_text(state["unit"])
        if state.get("logrotate") is not None:
            plan.logrotate_path.write_text(state["logrotate"])
        helper_left = ""
        if "runas_unit" in state:  # a state saved before agentos-runas existed leaves it alone
            if state["runas_unit"] is None:
                if plan.runas_path.exists():
                    rc, out = self.host.run(["systemctl", "disable", "--now", plan.runas_path.name], timeout=60)
                    if rc == 0:
                        plan.runas_path.unlink(missing_ok=True)
                    else:  # still running: keep its unit so systemd can still stop it, and say so
                        helper_left = f"{plan.runas_path.name} would not stop ({rc}): {out.strip()[-200:]}"
            else:
                plan.runas_path.write_text(state["runas_unit"])
        self._run(["chown", "-R", state["owner"], str(plan.home)], timeout=600)
        self._run(["systemctl", "daemon-reload"])
        if helper_left:
            raise SwitchError(f"{helper_left}; it is left installed. Stop it with: systemctl disable --now "
                              f"{plan.runas_path.name}")

    def _verify_brains(self, plan: SwitchPlan, user: str) -> tuple[bool, str]:
        """agentos-runas is up and installed for the brain user brains settings name, and the
        confinement check passes when run as `user` (so through the helper, as the brain user): the
        brain cannot traverse OPENAJAR_HOME or reach what else the probe checks."""
        _, out = self.host.run(["systemctl", "is-active", plan.runas_path.name])
        if out.strip() != "active":
            return False, f"{plan.runas_path.name} is {out.strip() or 'unknown'}"
        if plan.brain_user:
            try:
                served = re.search(r"--brain-user (\S+)", plan.runas_path.read_text())
            except OSError:
                served = None
            if not served or served.group(1) != plan.brain_user:
                return False, (f"{plan.runas_path.name} starts brains as {served.group(1) if served else 'nobody'}, "
                               f"not {plan.brain_user} (brains settings run_as)")
        rc, out = self.host.run(["runuser", "-u", user, "--", str(plan.prefix / ".venv" / "bin" / "openajar"), "brains",
                                 "confinement", "--json"], timeout=120,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
                                     "OPENAJAR_HOME": str(plan.home), "HOME": str(plan.home)})
        try:
            result = json.loads(out[out.index("{"):])
        except ValueError:
            tail = " ".join(out.strip().splitlines()[-3:])
            return False, f"the brains confinement check did not answer ({rc}){f': {tail}' if tail else ''}"
        if not result.get("ok"):
            return False, f"brains are not confined: {result.get('reason')}"
        return True, f"brains confined ({result.get('reason')})"

    def verify(self, plan: SwitchPlan, user: str) -> tuple[bool, str]:
        """The unit is active as `user`, MCP answers, and OPENAJAR_HOME is private to `user`."""
        from openajar.ops.doctor import PASS, DoctorContext, check_mcp, check_permissions

        deadline = time.monotonic() + plan.health_timeout
        detail = ""
        while True:
            _, out = self.host.run(["systemctl", "is-active", plan.unit])
            state = out.strip()
            if state == "active":
                ctx = DoctorContext(home=plan.home, host=self.host, unit=plan.unit)
                running_as = self._unit_user(plan)
                mcp = check_mcp(ctx)
                perms = check_permissions(ctx)
                st = self.host.lstat(plan.home)
                want = self.users(user)
                if running_as != user:
                    detail = f"{plan.unit} runs as {running_as}, not {user}"
                elif want is not None and st is not None and st.st_uid != want.pw_uid:
                    detail = f"{plan.home} is owned by {self.host.user_name(st.st_uid)}, not {user}"
                elif not perms or perms[0].status != PASS:
                    detail = perms[0].detail if perms else "no permissions result"
                elif mcp and mcp[0].status == PASS:
                    if user == "root":
                        return True, f"{plan.unit} active as {user}; {mcp[0].detail}"
                    ok, brains = self._verify_brains(plan, user)
                    if ok:
                        return True, f"{plan.unit} active as {user}; {mcp[0].detail}; {brains}"
                    detail = brains
                else:
                    detail = mcp[0].detail if mcp else "no MCP result"
            else:
                detail = f"{plan.unit} is {state or 'unknown'}"
                if state == "failed":
                    return False, detail
            if time.monotonic() > deadline:
                return False, detail
            self.sleep(2)
