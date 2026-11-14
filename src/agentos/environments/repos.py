"""The repository an environment clones: checked before anything is rented, and what a clone that
still fails says.

environment_open with `repo` asks the node first (agentos.git.service.GitService.access): a
repository declared for workspace env is checked with the vault's key or token for it, as the
git gateway will fetch it; any other must be readable with no credential at all (a public one). A
repository that is neither is `repo_not_connected`, and no machine is rented. The fix in that error
is for the human, on the node (`agentos environments repo add`), never for the agent: a repository
is the owner's to connect, as a login is.
"""

from __future__ import annotations

from typing import Any

from agentos.environments.errors import EnvironmentFailed, RepoNotConnected

# An MCP error's message is cut at 500 characters (agentos.policy.wrap): the reason and the fix both
# fit in it, for a repository name of up to REPO_WORDS_FIT characters.
MESSAGE_MAX = 500
REPO_WORDS_FIT = 100


def connect_fix(slug: str, *, replace: bool = False) -> str:
    """The words an agent hands the human, word for word: how to connect `slug` on the node
    (`replace`: its key or token was refused, so a new one replaces it). One command, so the whole
    fix fits in an MCP error for a long repository name too (docs/ENVIRONMENTS.md has the two
    `agentos vault git` commands it runs)."""
    again = " --replace" if replace else ""
    return (f"Tell the human, word for word: \"Run on the node: `agentos environments repo add {slug}{again}` "
            "(it prints a deploy key to add to the repository; --token stores a token instead).\" Only the "
            "human runs it, never you.")


def refused_words(access: Any) -> str:
    """What went wrong with the node's credential, from git's own message (upstream.explain): which
    one the host refused (a deploy key not added, a token expired or revoked), else git's words."""
    why = access.why or ""
    name = (access.credential or "")[:36]
    if "refused" in why and access.kind in ("ssh", "token"):
        return f"its {'deploy key' if access.kind == 'ssh' else 'token'} {name} was refused"
    return why[:60] or f"its credential {name} did not work"


def not_connected(access: Any) -> RepoNotConnected:
    """repo_not_connected for what GitService.access found (nothing rented yet)."""
    slug = access.slug
    if not access.declared:
        why = f"{slug} is not connected: not declared for environments, and not public"
    elif access.credential is None:
        why = f"{slug} is declared for env, but has no key or token in the vault and is not public"
    else:
        why = f"{slug}: {refused_words(access)}"
    return RepoNotConnected(f"{why}. Nothing was rented. {connect_fix(slug, replace=access.credential is not None)}")


def unreachable(access: Any) -> EnvironmentFailed:
    host = access.slug.split("/", 1)[0]
    return EnvironmentFailed(f"the node could not reach {host} to check {access.slug}, so nothing was rented; "
                             "try again in a minute")


# What git on the machine prints when it cannot read a repository with no credential (or the host
# refuses the one it has): the repository is not connected, whatever else is right.
_AUTH_WORDS = ("could not read username", "authentication failed", "repository not found", "returned error: 403",
               "returned error: 401", "permission denied (publickey", "could not read from remote repository",
               "terminal prompts disabled")
_GATEWAY = "remote error: agentos:"


def clone_failure(slug: str, output: str, *, via_gateway: bool) -> Exception:
    """What a failed clone on the machine says. The gateway's own refusal (a deploy key the host
    refused, a revoked token) is quoted from git's output; a repository the machine cannot read
    with no credential is not connected. Anything else is environment_failed with git's last lines."""
    text = (output or "").strip()
    low = text.lower()
    at = low.find(_GATEWAY)
    if at >= 0:
        said = text[at + len(_GATEWAY):].strip().splitlines()[0].strip().rstrip(".")
        return RepoNotConnected(f"cloning {slug} through the node's git gateway failed: {said[:300]}. The machine "
                                f"was deleted. {connect_fix(slug, replace=True)}")
    if any(w in low for w in _AUTH_WORDS):
        last = [ln.strip() for ln in text.splitlines() if ln.strip()][-1:] or [""]
        where = "through the node's git gateway" if via_gateway else "with no credential"
        return RepoNotConnected(f"cloning {slug} {where} failed (git said: {last[0][:200]}). The machine was "
                                f"deleted. {connect_fix(slug, replace=via_gateway)}")
    return EnvironmentFailed(f"cloning {slug} failed: {text[-500:]}")
