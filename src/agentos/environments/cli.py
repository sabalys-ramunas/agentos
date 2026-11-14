"""`agentos environments ...` (alias `agentos envs`): your agents' cloud computers, from the node's shell.

Agents open environments with environment_open (agentosd keeps them, and closes them when they are
done, idle or out of time). Opening one never asks a person; the node's limits bound it. These
commands list them, show one, close one by hand, set those limits, and check what the node needs.
They work while agentosd runs: a close marks the environment, and agentosd's reaper deletes its
machine, browser and tunnels within a minute.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from agentos.core import catalog
from agentos.core.config import home_from_env
from agentos.core.errors import AgentosError, InvalidRequest, NotFound
from agentos.environments.models import SIZES, WORKSPACE

app = typer.Typer(no_args_is_help=True, help="Your agents' cloud computers: list, show, close, allow, set up, "
                                             "connect repositories.")

_state: dict = {"home": None}


@app.callback()
def main(home: Annotated[Path | None, typer.Option(help="AGENTOS_HOME (default: $AGENTOS_HOME)")] = None) -> None:
    """Your agents' cloud computers: list, show, close, allow, set up, connect repositories."""
    _state["home"] = home


def _home() -> Path:
    return Path(_state["home"] or home_from_env())


def _fail(e: AgentosError) -> typer.Exit:
    typer.echo(catalog.cli_error(e.code, e.message), err=True)
    return typer.Exit(1)


def _managers() -> tuple[Any, Any]:
    from agentos.core.runtime import Runtime
    from agentos.environments.manager import EnvironmentManager
    from agentos.vms.manager import VmManager

    rt = Runtime()
    vms = VmManager(_home(), runtime=rt)
    rt.register("vms", vms)
    return EnvironmentManager(_home(), runtime=rt), vms


def _git_state(vms: Any, env: Any) -> list[dict] | None:
    """The environment's repositories (every one under /work), read over its VM's SSH (as agentosd
    reads them)."""
    from agentos.environments.manager import GIT_STATE, parse_git_state
    from agentos.vms import ssh

    if not env.vm_id:
        return None
    try:
        rec = vms.store.get(env.vm_id)
    except NotFound:
        return None
    if rec.state != "ready" or not rec.host:
        return None
    code, out = asyncio.run(ssh.run(vms.remote(rec), GIT_STATE, timeout_s=30))
    return parse_git_state(out) if code == 0 else None


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _unsaved_words(unsaved: dict) -> str:
    """Like "1 changed file and 2 commits not pushed", naming only what there is."""
    parts = []
    if unsaved.get("dirty_files"):
        parts.append(_count(len(unsaved["dirty_files"]), "changed file", "changed files"))
    if unsaved.get("unpushed"):
        parts.append(_count(len(unsaved["unpushed"]), "commit", "commits") + " not pushed")
    if unsaved.get("stashes"):
        parts.append(_count(len(unsaved["stashes"]), "stash", "stashes"))
    if unsaved.get("unreadable"):
        parts.append(_count(len(unsaved["unreadable"]), "repository git cannot read", "repositories git cannot read"))
    return " and ".join(parts) or "nothing unsaved"


def _row(s: dict) -> str:
    cost = f"EUR {s['cost_eur']:.3f}" if s.get("cost_eur") is not None else "EUR ?"
    left = f"{s['minutes_left']} min left" if s.get("minutes_left") is not None else (s.get("closed_reason") or "")
    return (f"{s['id']:<6} {s['state']:<8} {s['title'][:28]:<28} {(s.get('repo') or '-')[:30]:<30} "
            f"{s['machine']['server_type'] or '-':<8} {cost:<11} {left}")


@app.command("list")
def list_cmd(all_: Annotated[bool, typer.Option("--all", help="Closed and failed ones too.")] = False,
             json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Environments of this node: state, title, repository, machine, cost so far, time left."""
    mgr, _vms = _managers()
    envs = mgr.store.recent(100) if all_ else mgr.store.open()
    rows = [mgr.summary(e) for e in envs]
    if json_out:
        typer.echo(json.dumps({"environments": rows}, indent=2, default=str))
        return
    if not rows:
        typer.echo("No open environments." if not all_ else "No environments yet.")
        return
    for s in rows:
        typer.echo(_row(s))
    typer.echo(f"\nThis month about EUR {mgr.month_spend():.2f} on environments.")


@app.command()
def show(env_id: Annotated[str, typer.Argument(metavar="ID", help="An environment id, like e7k2.")],
         json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """One environment: its machine, lanes, the calls its agent made, and its unsaved work."""
    mgr, vms = _managers()
    try:
        env = mgr.get(env_id)
    except (NotFound, ValueError):
        raise _fail(NotFound(f"no environment {env_id}")) from None
    out = mgr.summary(env)
    repos = _git_state(vms, env) if env.open else None
    out["unsaved"] = mgr.unsaved_of(repos)
    out["git"] = repos
    out["pushed"] = mgr.pushes(env)
    out["calls"] = [{"at": c.at.isoformat(), "tool": c.tool, "summary": c.summary, "ok": c.ok}
                    for c in reversed(env.calls[-20:])]
    if json_out:
        typer.echo(json.dumps(out, indent=2, default=str))
        return
    typer.echo(_row(out))
    typer.echo(f"  agent {out['agent']['name']}, lanes {out['lanes']['terminal']} and {out['lanes']['browser']}, "
               f"workdir {out['workdir']}")
    if out["unsaved"]:
        typer.echo(f"  not saved: {_unsaved_words(out['unsaved'])}")
    for p in out["pushed"]:
        typer.echo(f"  pushed {p['branch']} {p['commit']} at {p['at']}")
    for c in out["calls"][:10]:
        typer.echo(f"  {c['at'][:19]} {c['tool']:<18} {'ok' if c['ok'] else 'failed':<6} {c['summary'][:80]}")


@app.command()
def close(env_id: Annotated[str, typer.Argument(metavar="ID", help="An environment id, like e7k2.")],
          discard: Annotated[bool, typer.Option("--discard", help="Close it even with work that is not pushed.")]
          = False) -> None:
    """Close an environment; refused while its work is not pushed, unless --discard.

    agentosd deletes its machine, browser and tunnels within a minute."""
    mgr, vms = _managers()
    try:
        env = mgr.get(env_id)
    except (NotFound, ValueError):
        raise _fail(NotFound(f"no environment {env_id}")) from None
    if not env.open:
        typer.echo(f"Environment {env.id} is already {env.state}.")
        return
    if not discard and env.state == "ready":
        repos = _git_state(vms, env)
        unsaved = mgr.unsaved_of(repos) if repos is not None else {"dirty_files": [], "unpushed": []}
        if repos is None or unsaved:
            from agentos.environments.errors import UnsavedWork

            why = ("its machine does not answer, so its work cannot be checked" if repos is None else
                   f"it has {_unsaved_words(unsaved)}")
            raise _fail(UnsavedWork(f"environment {env.id}: {why}; run again with --discard to delete it anyway"))
    reason = "closed by the owner (agentos environments close)" + (" (discarded)" if discard else "")

    def closing(e: Any) -> bool:
        if not e.open:
            return False
        e.state, e.closed_reason = "closing", reason
        return True

    # one write transaction: agentosd records calls on the same row meanwhile, and neither loses the other's
    env = mgr.store.update(env.id, closing)
    if env.state != "closing":
        typer.echo(f"Environment {env.id} is already {env.state}.")
        return
    typer.echo(f"Closing {env.id} ({env.title}). agentosd deletes its machine on its next pass (within a minute).")


repo_app = typer.Typer(no_args_is_help=True, help=(
    "Connect private repositories: a key or token in the vault, and the declaration for env."))
app.add_typer(repo_app, name="repo")


def _remote_or_fail(remote: str) -> Any:
    from agentos.git.wire import parse_remote

    try:
        r = parse_remote(remote)
    except AgentosError as e:
        raise _fail(e) from None
    if r.repo.endswith("/*"):
        raise _fail(InvalidRequest("name one repository, like github.com/me/shop (for owner/*, use `agentos vault "
                                   "git key add` and `agentos vault git remote allow env`)"))
    return r


def _env_rules(home: Path) -> list[Any]:
    from agentos.git.remotes import Remotes

    return [r for r in Remotes(home / "git" / "remotes.json").list() if r.scope.split("/")[0] == WORKSPACE]


@repo_app.command("add")
def repo_add(remote: Annotated[str, typer.Argument(help="The repository, like github.com/me/shop.")],
             token: Annotated[bool, typer.Option("--token", help="Store a fine-grained token (read from a prompt) "
                                                 "instead of making a deploy key.")] = False,
             stdin: Annotated[bool, typer.Option("--stdin", help="Read the token from stdin.")] = False,
             replace: Annotated[bool, typer.Option("--replace", help="Make a new key or token even if the vault has "
                                                   "one for this repository.")] = False,
             read_only: Annotated[bool, typer.Option("--read-only", help="Clone only, no pushes.")] = False) -> None:
    """Connect a private repository: a deploy key (printed, to paste) or a token, and the declaration.

    The same as `agentos vault git key add --host H --repo O/N` and `agentos vault git remote allow
    env H/O/N`. Run it on the node, as the owner; an agent never does."""
    from agentos.git import cli as gitcli
    from agentos.git import keys as keymod
    from agentos.git.remotes import Remotes

    r = _remote_or_fail(remote)
    have = None if replace else keymod.pick(gitcli.vault_credentials(), r)
    if have is None:
        out = gitcli.make_credential(r.host, [r.repo], token=token, stdin=stdin, replace=replace)
        gitcli.echo_credential(out, pushers="agents")
        key = out["type"] == "ssh"
    else:
        key = have.type == "ssh"
        typer.echo(f"The vault has the {'deploy key' if key else 'token'} {have.name} for "
                   f"{r.host}/{', '.join(have.repos)} already (--replace makes a new one).")
        if have.public_key:
            typer.echo(f"If it is not added yet, add it as a deploy key, with write access: "
                       f"{gitcli.deploy_key_hint(r.host, [r.repo])}")
            typer.echo("")
            typer.echo(have.public_key)
            typer.echo("")
    try:
        rule = Remotes(_home() / "git" / "remotes.json").allow(WORKSPACE, r.slug, push=not read_only)
    except AgentosError as e:
        raise _fail(e) from None
    typer.echo(f"Declared {r.slug} for environments ({'clone only' if not rule.push else 'clone and push'}).")
    typer.echo(("Once the key is added there, agents" if key else "Agents") + " can open it: "
               f"environment_open(title=..., repo=\"{r.slug}\").")


@repo_app.command("list")
def repo_list(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """The repositories declared for environments, and the key or token each one uses."""
    from agentos.git import keys as keymod
    from agentos.git.wire import Remote

    rules = _env_rules(_home())
    creds = _creds_or_none()
    rows = []
    for rule in rules:
        if creds is None:
            cred = None
        elif rule.repo.endswith("/*"):  # owner/*: a credential made for owner/* too
            cred = next((c for c in creds if c.host == rule.host and rule.repo in c.repos), None)
        else:
            cred = keymod.pick(creds, Remote(rule.host, rule.repo))
        rows.append({"repo": rule.slug, "scope": rule.scope, "push": rule.push,
                     "credential": cred.public() if cred else None})
    if json_out:
        typer.echo(json.dumps({"repos": rows}, indent=2))
        return
    if not rows:
        typer.echo("No repository is connected for environments. Connect one: "
                   "agentos environments repo add github.com/<owner>/<name>")
        return
    for row in rows:
        c = row["credential"]
        used = (f"{'deploy key' if c['type'] == 'ssh' else 'token'} {c['name']}" if c else
                "no key or token in the vault: public only" if creds is not None else "the vault is not set up")
        typer.echo(f"{row['repo']:<40} {'clone and push' if row['push'] else 'clone only':<15} {used}")


def _creds_or_none() -> list[Any] | None:
    from agentos.cli import cli_runtime
    from agentos.git import cli as gitcli

    if cli_runtime().get("vault") is None:
        return None
    return gitcli.vault_credentials()


@repo_app.command("remove")
def repo_remove(remote: Annotated[str, typer.Argument(help="The repository, like github.com/me/shop.")],
                keep_key: Annotated[bool, typer.Option("--keep-key", help="Keep its key or token in the vault.")]
                = False) -> None:
    """Disconnect a repository from environments: its declaration, and the key or token made for it alone."""
    from agentos.git import cli as gitcli
    from agentos.git.remotes import Remotes

    r = _remote_or_fail(remote)
    remotes = Remotes(_home() / "git" / "remotes.json")
    try:
        remotes.deny(WORKSPACE, r.slug)
    except NotFound:
        raise _fail(NotFound(f"{r.slug} is not connected for environments; `agentos environments repo list` "
                             "shows what is")) from None
    typer.echo(f"Removed {r.slug} from environments: new ones can no longer clone it through the node.")
    if keep_key:
        return
    still = [rule for rule in remotes.list() if rule.matches(r)]
    creds = _creds_or_none() or []
    for cred in (c for c in creds if c.host == r.host and c.repos == (r.repo,)):
        if still:
            typer.echo(f"Kept {cred.name}: {', '.join(rule.scope for rule in still)} still may use {r.slug}.")
            continue
        gitcli.remove_credential(cred)
        typer.echo(f"Removed {cred.name} from the vault. Remove it at {r.host} too "
                   f"({'the deploy key' if cred.type == 'ssh' else 'revoke the token'}).")


def _write_settings(updates: dict[str, Any]) -> Path:
    import tomllib

    import tomli_w

    from agentos.core.config import write_private
    from agentos.environments.models import SETTINGS_FILE, EnvSettings

    path = _home() / SETTINGS_FILE
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    data.update({k: v for k, v in updates.items() if v is not None})
    EnvSettings(**data)  # refuse what agentosd would not start with
    write_private(path, tomli_w.dumps(data))
    return path


@app.command()
def allow(max_live: Annotated[int | None, typer.Option("--max-live", help="Open at once, at most (default 3).")
                               ] = None,
          max_hours: Annotated[float | None, typer.Option("--max-hours", help="How long one lives, extend included, "
                                                          "at most (default 8).")] = None,
          monthly_eur: Annotated[float | None, typer.Option("--monthly-eur", help="What environments may book in a "
                                                            "month, in EUR (default 50; 0: no budget).")] = None,
          max_size: Annotated[str | None, typer.Option("--max-size", help="The biggest machine an agent may ask "
                                                       "for: small, medium, large or xlarge (default).")] = None,
          days: Annotated[int, typer.Option(help="No longer used: opening one never asks, so there is no yes "
                                                 "to last.", hidden=True)] = 30,
          workspace: Annotated[str, typer.Option(help="No longer used.", hidden=True)] = "env") -> None:
    """Set the limits that bound the environments agents open.

    Opening an environment never asks you. These limits bound it instead, and they hold without this command. Past one, the node refuses an open or an extend with environment_limit, whatever the agent read. The VM limits in vms.toml hold too."""
    from agentos.environments.models import load_settings

    del days, workspace  # kept so older scripts still run; nothing is granted any more
    if max_size is not None and max_size not in SIZES:
        raise _fail(InvalidRequest(f"--max-size is one of {', '.join(SIZES)}"))
    path = _write_settings({"max_live": max_live, "max_hours": max_hours, "monthly_eur": monthly_eur,
                            "max_size": max_size})
    typer.echo(f"Environments: {_limit_words(load_settings(_home()))}. Limits in {path}.")
    typer.echo("Opening one never asks you; the node refuses one past these limits.")


def _limit_words(s: Any) -> str:
    """The limits that bound environment_open, in words."""
    live = "the VM limit (vms.toml max_live)" if s.max_live is None else str(s.max_live)
    budget = f"EUR {s.monthly_eur:g}" if s.monthly_eur else "no budget"
    cpu, memory = SIZES[s.max_size]
    return (f"at most {live} open at once, {s.max_hours:g} hours each, {budget} a month, machines up to "
            f"{s.max_size} ({cpu} CPU, {memory} GB)")


@app.command()
def setup(build: Annotated[bool, typer.Option("--build", help="Build the docker provider's image now.")] = False
          ) -> None:
    """Check what environments need on this node, and say what is missing.

    The provider (a Hetzner token, or Docker and its image), the image machines boot from, and the
    repositories declared for workspace env in the git gateway."""
    from agentos.vms.manager import VmManager

    home = _home()
    vms = VmManager(home)
    s = vms.settings
    lines: list[tuple[str, str, str]] = []

    async def check_provider() -> str | None:
        try:
            await vms.provider().check()
        except AgentosError as e:
            return e.message
        return None

    if s.provider == "docker" and build:
        _build_image(s.docker_image, s.docker_bin)
    problem = asyncio.run(check_provider()) if vms.configured else (
        "no Hetzner token: run `agentos vm setup`, or set provider = \"docker\" in vms.toml to use Docker here")
    if problem is None:
        lines.append(("ok", f"provider {s.provider}", "answers"))
    else:
        lines.append(("missing", f"provider {s.provider}", problem))
    if problem is None:
        try:
            asyncio.run(vms.provider().resolve_image("snapshot:dev"))
            lines.append(("ok", "image", "the dev snapshot: machines boot ready"))
        except AgentosError:
            lines.append(("ok", "image", "Ubuntu 24.04, set up by cloud-init on each boot (a few minutes); "
                                        "`agentos vm image build dev` makes them boot ready"
                          if s.provider != "docker" else s.docker_image))
    from agentos.git.remotes import Remotes

    declared = [r for r in Remotes(home / "git" / "remotes.json").list() if r.scope.split("/")[0] == "env"]
    if declared:
        lines.append(("ok", "git", "connected for env: " + ", ".join(r.slug for r in declared)
                      + " (`agentos environments repo list` shows their keys)"))
    else:
        lines.append(("missing", "git", ("no repository is connected for environments, so only public repositories "
                                         "clone and nothing can be pushed. Connect a private one (a deploy key to "
                                         "paste, or --token): `agentos environments repo add "
                                         "github.com/<owner>/<name>`")))
    if s.provider == "docker":
        lines.append(("note", "isolation", docker_isolation_words()))
    lines.append(("note", "grants", ("agents connected with `agentos connect` or `agentos dev up` may open one (the "
                                     "environments special: vm and exec on env/* lanes only). Nothing in one asks "
                                     "you, opening and pushing included; the limits bound it")))
    from agentos.environments.models import load_settings

    lines.append(("note", "limits", _limit_words(load_settings(home)) + " (`agentos environments allow` changes them)"))
    for status, what, words in lines:
        typer.echo(f"{status:<8} {what:<18} {words}")
    if any(st == "missing" for st, _w, _x in lines if _w.startswith("provider")):
        raise typer.Exit(1)


def docker_isolation_words(platform: str | None = None) -> str:
    """What the docker provider keeps a machine from, and what it does not, said plainly."""
    import sys

    mac = (platform or sys.platform) == "darwin"
    words = ("each machine has its own network, CPU, memory and process limits (vms.toml docker_cpus, "
             "docker_memory_gb, docker_pids) and a firewall that keeps it off this computer and your network: it "
             "reaches the internet only. It still shares a kernel ")
    words += ("with Docker Desktop's Linux VM, which can read the folders you share with Docker (your home folder "
              "by default)" if mac else "with this computer")
    words += (", so a machine that breaks out of its container is on this computer. Use the docker provider for "
              "your own code; the cloud provider (hetzner) is the isolated one.")
    if mac:
        words += (" macOS cannot tell which account opened a local connection, so another account on this Mac "
                  "could use an environment's browser tunnel port.")
    return words


def _build_image(image: str, docker: str) -> None:
    """docker build of deploy/environments/Dockerfile with the package's setup script, in a temp folder."""
    import shutil
    import subprocess
    import tempfile

    here = Path(__file__).resolve()
    deploy = here.parents[3] / "deploy" / "environments"
    if not (deploy / "Dockerfile").exists():
        raise _fail(NotFound("deploy/environments/Dockerfile is not here (an install without the checkout): "
                             "build the image from a checkout of agentos"))
    with tempfile.TemporaryDirectory(prefix="agentos-env-") as tmp:
        for f in deploy.iterdir():
            if f.is_file():
                shutil.copy(f, tmp)
        shutil.copy(here.parent / "setup.sh", tmp)
        typer.echo(f"Building {image} (a few minutes the first time)...")
        done = subprocess.run([docker, "build", "-q", "-t", image, tmp], check=False)
        if done.returncode != 0:
            raise typer.Exit(done.returncode)
    typer.echo(f"Built {image}.")
