"""A real pull request for a branch an environment pushed (environment_pull_request).

The VM has no gh and no credential, and git.compare only opens GitHub's form. The node opens the
pull request itself, through GitHub's REST API, with the token the git gateway already pushes with:
a token declared for the repository (or its owner) first, else the GitHub login of local mode
(agentos.git.login, `agentos setup github`). A deploy key cannot open one, so it is skipped.

What it may do, and no more:

- only for a branch this environment created on the remote (Environment.own_branches, as the git
  gateway records each push), into the base it names or the repository's default branch;
- open one (or return the open one for that branch); never merge, close or change anything else.

The token stays on the node: it goes in the request's header and is scrubbed from every message.
"""

from __future__ import annotations

import logging
from typing import Any

from agentos.core.errors import AgentosError, InvalidRequest, PolicyDenied

log = logging.getLogger("agentos.environments")

API = "https://api.github.com"
TIMEOUT_S = 20.0
TITLE_MAX = 256
BODY_MAX = 60_000


class PullRequestFailed(AgentosError):
    """GitHub did not open the pull request, or the node holds no token that may."""
    code = "pull_request_failed"


NO_TOKEN = ("the node holds no GitHub token for {repo}, so it cannot open a pull request (a deploy key only "
            "pushes). Tell the user: on agentos local, `agentos setup github` stores a login; on a server node, "
            "`agentos git key add --host github.com --repo {repo} --token <token>` stores one. Meanwhile give "
            "them git.compare from environment(), which opens GitHub's form")


def github_repo(env: Any) -> str:
    host, _, repo = (env.repo or "").partition("/")
    if host != "github.com" or repo.count("/") != 1:
        raise InvalidRequest(f"environment_pull_request opens pull requests on github.com, and this environment's "
                             f"repository is {env.repo or 'none'}. Give the human the branch name instead")
    return repo


def pick_branch(mgr: Any, env: Any, branch: str | None) -> str:
    """The branch to open it for: the one named, else the newest this environment pushed. Either way
    one the environment created (own_branches)."""
    own = list(env.own_branches)
    if branch:
        if branch not in own:
            raise PolicyDenied(f"{branch} is not a branch this environment pushed first, so it opens no pull request "
                               f"for it. Its own branches: {', '.join(own[-5:]) or 'none yet'}. Push your work to a "
                               "new branch (git push origin <branch>), then call this again")
        return branch
    for p in mgr.push_lists(env)["pushed"]:
        if p.get("branch") in own:
            return str(p["branch"])
    raise PolicyDenied("this environment has pushed no branch of its own yet. Commit, push to a new branch "
                       "(git push origin <branch>), then call environment_pull_request again")


async def token_for(mgr: Any, repo: str) -> str | None:
    """The token the git gateway would push to github.com/<repo> with, as a value, or None."""
    from agentos.git import keys as keymod
    from agentos.git import login
    from agentos.git.wire import parse_remote

    git = mgr._get("git")
    gateway = getattr(git, "gateway", None)
    if gateway is None:
        return None
    remote = parse_remote(f"github.com/{repo}")
    creds = [c for c in await gateway.credentials() if c.kind != keymod.SSH_KIND]
    found = keymod.pick(creds, remote) or next(
        (c for c in creds if c.any_repo and c.ref == login.REF and c.host == remote.host), None)
    if found is None:
        return None
    try:
        return await gateway._read_secret(found.ref, f"open a pull request on {remote.slug}")
    except AgentosError as e:
        raise PullRequestFailed(f"the vault did not give the GitHub token: {e.message}") from None


def _blocked(mgr: Any, env: Any) -> str:
    """The "Blocked during this work" section, or "" when the environment's network refused no host."""
    from agentos.environments.network import blocked_section

    view = getattr(mgr, "network_view", None)
    blocked = view(env)["blocked"] if callable(view) else list(getattr(env, "blocked", None) or [])
    return blocked_section(blocked)


def _with_blocked(mgr: Any, env: Any, body: str | None) -> str:
    """The body, with "Blocked during this work" (between its markers) when the network refused hosts."""
    from agentos.environments import prproofs

    return prproofs.set_section(body, prproofs.NET_BEGIN, prproofs.NET_END, _blocked(mgr, env))


def _client(mgr: Any) -> Any:
    import httpx

    return httpx.AsyncClient(base_url=getattr(mgr, "github_api", None) or API, timeout=TIMEOUT_S,
                             transport=getattr(mgr, "github_transport", None),
                             headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                                      "User-Agent": "agentos"})


def _why(resp: Any, token: str) -> str:
    try:
        data = resp.json()
        msg = str(data.get("message") or "")
        errs = "; ".join(str(e.get("message") or e.get("code") or "") for e in data.get("errors") or []
                         if isinstance(e, dict))
        text = f"{msg} ({errs})" if errs else msg
    except ValueError:
        text = resp.text[:300]
    return (text or f"HTTP {resp.status_code}").replace(token, "***")[:400]


async def open_pull_request(mgr: Any, env: Any, *, title: str, body: str | None = None, base: str | None = None,
                            draft: bool = False, branch: str | None = None, proofs: Any = None) -> dict:
    """Open (or find the open) pull request for the environment's branch, with its proofs on the
    repository's agentos-proofs branch and a Proof section in the body (agentos.environments.prproofs).
    An open one gets only agentos's own sections replaced. Returns url, number, branch, base, draft,
    state, created (False when one was open already), repo and proofs."""
    title = " ".join(str(title or "").split())
    if not title:
        raise InvalidRequest("title is the pull request's title, like \"Fix the typo in the docs\"")
    repo = github_repo(env)
    head = pick_branch(mgr, env, branch)
    chosen = _chosen(mgr, env, proofs)
    token = await token_for(mgr, repo)
    if not token:
        raise PullRequestFailed(NO_TOKEN.format(repo=repo) + (KEPT_ON_NODE if chosen else ""))
    auth = {"Authorization": f"Bearer {token}"}
    async with _client(mgr) as http:
        try:
            if not base:
                got = await http.get(f"/repos/{repo}", headers=auth)
                if got.status_code != 200:
                    raise PullRequestFailed(f"GitHub did not show {repo} ({got.status_code}: {_why(got, token)})")
                base = str(got.json().get("default_branch") or "main")
            if base == head:
                raise InvalidRequest(f"{head} is the base branch itself; push your work to a new branch first")
            shown, sections = await _proof_sections(mgr, http, env, repo, auth, token, chosen)
            pr, created = await _open_or_update(http, repo, auth, token, head, base, sections, {
                "title": title[:TITLE_MAX], "head": head, "base": base, "draft": bool(draft),
                "body": _compose(body, sections)})
        except AgentosError:
            raise
        except Exception as e:  # noqa: BLE001 - a network failure is the call's failure, in words
            raise PullRequestFailed(f"GitHub did not answer ({type(e).__name__}); nothing was opened. Try again "
                                    "in a minute") from None
    return {"url": pr.get("html_url"), "number": pr.get("number"), "branch": head, "base": base,
            "draft": bool(pr.get("draft", draft)), "state": pr.get("state", "open"), "created": created,
            "repo": f"github.com/{repo}", **({"proofs": shown} if shown else {})}


KEPT_ON_NODE = ". The proofs stayed on the node: environment() lists them with their links, to give the human"


def _chosen(mgr: Any, env: Any, proofs: Any) -> list[dict]:
    """The proofs this pull request shows: none with pr_proofs = false (unless the call names some)."""
    from agentos.environments import prproofs

    listed = getattr(mgr, "proofs", None)
    if not callable(listed):
        return []
    settings = getattr(mgr, "settings", None)
    if proofs is None and not getattr(settings, "pr_proofs", True):
        return []
    return prproofs.choose(listed(env, links=False), proofs)


async def _proof_sections(mgr: Any, http: Any, env: Any, repo: str, auth: dict, token: str,
                          chosen: list[dict]) -> tuple[dict | None, dict[tuple[str, str], str | None]]:
    """The proofs sent to the repository, as the result says them, and agentos's body sections
    (markers -> text; None: leave that section as it is)."""
    from agentos.environments import prproofs

    sections: dict[tuple[str, str], str | None] = {(prproofs.NET_BEGIN, prproofs.NET_END): _blocked(mgr, env)}
    if not chosen:
        sections[(prproofs.PROOF_BEGIN, prproofs.PROOF_END)] = None
        return None, sections
    try:
        sent, skipped = await prproofs.send(mgr, http, repo, auth, token, env.id, chosen)
    except AgentosError as e:
        sections[(prproofs.PROOF_BEGIN, prproofs.PROOF_END)] = None
        log.warning("environment %s: its proofs did not go to %s: %s", env.id, repo, e.message)
        return {"in_pull_request": 0, "why": e.message + KEPT_ON_NODE}, sections
    sections[(prproofs.PROOF_BEGIN, prproofs.PROOF_END)] = prproofs.section(repo, env.id, chosen, sent)
    return {"in_pull_request": len(sent), "branch": prproofs.BRANCH,
            **({"too_large": skipped, "why": "GitHub takes no file of 100 MB or more" + KEPT_ON_NODE}
               if skipped else {})}, sections


def _compose(body: str | None, sections: dict[tuple[str, str], str | None]) -> str:
    """A new pull request's body: the agent's text (cut to fit), then agentos's sections in their markers."""
    tail = "\n\n".join(f"{begin}\n{text.strip()}\n{end}" for (begin, end), text in sections.items() if text)
    text = (body or "").rstrip()[:max(BODY_MAX - len(tail) - 2, 0)]
    return "\n\n".join(p for p in (text, tail) if p)


async def _open_or_update(http: Any, repo: str, auth: dict, token: str, head: str, base: str,
                          sections: dict[tuple[str, str], str | None], new: dict) -> tuple[dict, bool]:
    """Open the pull request; when one is open for the branch, replace agentos's sections of its body
    (never the text around them). Returns (the pull request, created)."""
    from agentos.environments import prproofs

    made = await http.post(f"/repos/{repo}/pulls", headers=auth, json=new)
    if made.status_code == 201:
        return made.json(), True
    if not (made.status_code == 422 and "already exists" in made.text):
        raise PullRequestFailed(f"GitHub did not open the pull request ({made.status_code}: {_why(made, token)})")
    found = await http.get(f"/repos/{repo}/pulls", headers=auth,
                           params={"head": f"{repo.split('/')[0]}:{head}", "state": "open"})
    rows = found.json() if found.status_code == 200 else []
    if not rows:
        raise PullRequestFailed(f"GitHub says a pull request for {head} exists, and lists none open")
    pr = rows[0]
    old = str(pr.get("body") or "")
    body = old
    for (begin, end), text in sections.items():
        if text is not None and (text or begin in body):
            body = prproofs.set_section(body, begin, end, text)
    if body != old and len(body) <= BODY_MAX:
        changed = await http.patch(f"/repos/{repo}/pulls/{pr.get('number')}", headers=auth, json={"body": body})
        if changed.status_code != 200:
            raise PullRequestFailed(f"GitHub did not update the pull request's proof ({changed.status_code}: "
                                    f"{_why(changed, token)})")
        pr = {**pr, **changed.json()}
    return pr, False
