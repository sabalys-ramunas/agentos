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
            "(a deploy key for a repo admin to add; --token: a token, no admin rights needed).\" Only the "
            "human runs it, never you.")


def fit(text: str, room: int) -> str:
    """`text` cut at a word end to at most `room` characters, with "..." when it was cut (never mid-word)."""
    text = text.strip().rstrip(".")
    if len(text) <= room:
        return text
    return text[:max(0, room - 3)].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def refused_words(access: Any) -> str:
    """What went wrong with the node's credential, from git's own message (upstream.explain): which
    one the host refused (a deploy key not added, a token expired or revoked), else git's words."""
    why = access.why or ""
    name = (access.credential or "")[:36]
    if "refused" in why and access.kind in ("ssh", "token"):
        return f"its {'deploy key' if access.kind == 'ssh' else 'token'} {name} was refused"
    return why.strip().rstrip(".") or f"its credential {name} did not work"


def reason_words(access: Any) -> str:
    """Why the node cannot clone it, in a few words (the start of repo_not_connected)."""
    slug = access.slug
    if not access.declared:
        return f"{slug} is not connected: not declared for environments, and not public"
    if access.credential is None:
        return f"{slug} is declared for env, but has no key or token in the vault and is not public"
    why = refused_words(access)
    return why if why.startswith(slug) else f"{slug}: {why}"  # git's words often name the repository already


def not_connected(access: Any) -> RepoNotConnected:
    """repo_not_connected for what GitService.access found (nothing rented yet). `access` rides on the
    error, so environment_open can offer a card instead (agentos.environments.connect). The reason is
    cut at a word end so the whole fix still fits in an MCP error (MESSAGE_MAX)."""
    tail = f". Nothing was rented. {connect_fix(access.slug, replace=access.credential is not None)}"
    e = RepoNotConnected(fit(reason_words(access), MESSAGE_MAX - len(tail)) + tail)
    e.access = access
    return e


def not_found(access: Any) -> Exception:
    """repo_not_found: GitHub answered 404 to a login or token that should see the repository (the
    node's GitHub login with any_repo, or a token for its owner), so it does not exist, or that login
    cannot see it. No card: a token would not help. Nothing was rented."""
    from agentos.environments.errors import RepoNotFound

    who = f"your GitHub login {access.login}" if access.login else f"the token {access.credential}"
    return RepoNotFound(f"{access.slug} does not exist, or {who} cannot see it, so nothing was rented. Check the "
                        "name with the person (a typo, or another owner); a token would not help")


NETWORK_RETRY = "try again in 2 minutes"


def unreachable(access: Any, *, tries: int = 1) -> EnvironmentFailed:
    """The host did not answer the check (after `tries` over about 15 s): the network of the computer
    agentos runs on, not the repository. Nothing was rented."""
    host = access.slug.split("/", 1)[0]
    times = f" ({tries} tries over about 15 s)" if tries > 1 else ""
    return EnvironmentFailed(f"the computer agentos runs on could not reach {host} to check {access.slug}{times}, "
                             f"so its network is down or slow; nothing was rented. {NETWORK_RETRY.capitalize()}",
                             reason="network")


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
    return EnvironmentFailed(f"cloning {slug} failed: {text[-500:]}", reason="clone")
