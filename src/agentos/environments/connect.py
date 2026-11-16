"""Connecting a private repository from the chat (docs/ENVIRONMENTS.md, "Connect a private repository").

environment_open on a repository the node cannot read gives the agent a card instead of a dead end:
an approval_pending result whose `review_url` opens the card in Needs you. Nothing is rented while it
waits. From the card, the owner in person opens the node's own page for a token
(agentos.environments.form: the web app never holds one) and pastes it there, or adds the node's
deploy key at the repository, and the node checks it with the host before it keeps it.
Then the card is approved, so approval_wait returns and the agent calls environment_open again.

A token needs no admin rights on the repository: a collaborator's own token works, where a deploy key
needs a repository admin. The card closes however the repository gets connected: on the card, with
`agentos environments repo add` on the node (the reaper sees it), or by the agent's next open.

The token is a `Secret` from the request body on. It goes to the host's API, to `git ls-remote` (in
the process's own config, as the git gateway sends it) and into the vault, and nowhere else: not to a
card, a tool result, an error, an approval, a trace or a log. No tool and no API route takes a token:
only the node's page does, and its link is made only for the owner in person, which no grant, token
or review link is.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from agentos.core.errors import AgentosError, InvalidRequest, NotFound, ServiceUnavailable
from agentos.core.models import Effect, Principal
from agentos.environments.errors import RepoConnectFailed, RepoNotConnected
from agentos.environments.models import WORKSPACE
from agentos.vault.secret import Secret

log = logging.getLogger("agentos.environments")

REQUEST_TTL_S = 24 * 3600
CONNECTED = "Connected"
GITHUB_API = "https://api.github.com"
EXPIRY_HEADER = "github-authentication-token-expiration"
SSO_HEADER = "x-github-sso"
HTTP_TIMEOUT_S = 15.0
SWEEP_EVERY_S = 60.0
GITHUB_QUOTE = 200  # characters of GitHub's own message a refusal quotes
# How the node reaches the host's API. None: the real network; tests set an httpx.MockTransport.
transport: httpx.AsyncBaseTransport | None = None
_swept: dict[str, float] = {}


# ---------------------------------------------------------------- the card


def _host(slug: str) -> str:
    return slug.split("/", 1)[0]


def links(slug: str) -> dict[str, str]:
    """Where a person makes a token, and the repository's deploy keys page (GitHub only)."""
    host, _, repo = slug.partition("/")
    if host != "github.com":
        return {}
    return {"token_url": "https://github.com/settings/personal-access-tokens/new",
            "classic_token_url": "https://github.com/settings/tokens/new?scopes=repo&description=agentos",
            "deploy_keys_url": f"https://github.com/{repo}/settings/keys/new"}


def card_words(slug: str, *, refused: bool) -> str:
    """Written here, never in an agent's words. It reaches every place a card goes, so no secret, ever."""
    what = "give a new token for" if refused else "connect"
    return (f"An agent asks you to {what} the private repository {slug}, so it can work on it in an "
            f"environment. Open this card to paste a token for it, on a page this computer serves. The node "
            f"checks it with {_host(slug)}, then "
            "keeps it on this computer only. Or at the node, run: "
            f"agentos environments repo add {slug} --token")


def _pending(approvals: Any, approval_id: str) -> bool:
    try:
        return approvals.get(approval_id).state == "pending"
    except NotFound:
        return False


def waiting(store: Any, approvals: Any) -> list[dict[str, str]]:
    """The cards still open. A row whose card closed is forgotten here."""
    out = []
    for ask in store.requests():
        if _pending(approvals, ask["id"]):
            out.append(ask)
        else:
            store.request_drop(ask["id"])
    return out


def ask(store: Any, approvals: Any, slug: str, *, reason: str, requester: Principal, grant_id: str | None,
        grant_path: str | None) -> Any:
    """The card for `slug`: the one already open for it, else a new one. `reason`: not_connected, or
    refused (a key or token the vault has was refused, so a new one replaces it)."""
    for row in waiting(store, approvals):
        if row["repo"] == slug:
            if row.get("grant_id") not in ("*", grant_id or ""):
                store.request_shared(row["id"])  # two agents wait on it: neither withdraws it alone
            return approvals.get(row["id"])
    ap = approvals.create_question(question=card_words(slug, refused=reason == "refused"), options=None,
                                   requester=requester, grant_id=grant_id, grant_path=grant_path,
                                   ttl_s=REQUEST_TTL_S, workspace=WORKSPACE)
    store.request_put(ap.id, slug, reason, datetime.now(UTC).isoformat(timespec="seconds"), grant_id or "")
    return ap


def withdraw(store: Any, approvals: Any, grant_id: str | None, *, instead: str) -> list[str]:
    """The agent opened with another repository (`instead`): the cards it alone raised for other ones are
    withdrawn (a name it got wrong), so no person pastes a token for a repository nobody needs. A card
    another agent waits on too stays. Returns the withdrawn cards' ids."""
    if approvals is None or not grant_id:
        return []
    gone = []
    for row in waiting(store, approvals):
        if row.get("grant_id") != grant_id or row["repo"] == instead:
            continue
        try:
            approvals.decide(row["id"], "deny", by="agent", note=f"Withdrawn: the agent asked for {instead} instead.")
            gone.append(row["id"])
        except AgentosError as e:
            log.info("the card for %s was closed already (%s)", row["repo"], e.code)
        store.request_drop(row["id"])
    return gone


def request(store: Any, approvals: Any, approval_id: str) -> dict[str, str]:
    row = next((r for r in waiting(store, approvals) if r["id"] == approval_id), None)
    if row is None:
        raise NotFound("That card is closed, or its repository is connected.")
    return row


def close(store: Any, approvals: Any, slug: str) -> list[str]:
    """The repository is connected: its open cards are approved (approval_wait returns), and forgotten."""
    if approvals is None:
        return []
    closed = []
    for row in store.requests():
        if row["repo"] != slug:
            continue
        try:
            if approvals.get(row["id"]).state == "pending":
                approvals.decide(row["id"], "approve", by="owner", answer=CONNECTED,
                                 note="The repository is connected.")
                closed.append(row["id"])
        except AgentosError as e:
            log.info("the card for %s was closed already (%s)", slug, e.code)
        store.request_drop(row["id"])
    return closed


def decline(store: Any, approvals: Any, approval_id: str, *, by: str) -> dict[str, str]:
    row = request(store, approvals, approval_id)
    approvals.decide(approval_id, "deny", by=by, note="Not now.")
    store.request_drop(approval_id)
    return row


def annotate(store: Any, items: list[dict]) -> list[dict]:
    """Questions as the panel lists them: a repository card says so, with what its card needs."""
    rows = {r["id"]: r for r in store.requests()}
    for item in items:
        row = rows.get(item.get("id"))
        if row is not None:
            item["repo_connect"] = {"repo": row["repo"], "host": _host(row["repo"]), "reason": row["reason"],
                                    **links(row["repo"])}
    return items


async def card_result(ctx: Any, mgr: Any, slug: str, error: RepoNotConnected) -> dict | None:
    """What environment_open returns for a repository the node cannot read: the card, or None on a
    node with no approvals (it then raises repo_not_connected as before)."""
    runtime = ctx.runtime
    approvals = runtime.get("approvals")
    access = getattr(error, "access", None)
    if approvals is None or access is None:
        return None
    from agentos.environments import repos
    from agentos.node.tools.lanes import ssh_host

    grant = getattr(ctx, "grant", None)
    p = ctx.principal
    grant_id = getattr(grant, "id", None) or getattr(p, "grant_id", None)
    await ctx.authorize(Effect.READ, summary=f"Ask a person to connect the repository {slug}")
    reason = "refused" if access.credential is not None else "not_connected"
    ap = ask(mgr.store, approvals, slug, reason=reason,
             requester=Principal(kind=p.kind, id=p.id, owner=getattr(p, "owner", "owner"), grant_id=grant_id),
             grant_id=grant_id, grant_path=getattr(grant, "path", None))
    ctx.trace("repo_connect_card", summary=f"asked a person to connect {slug}", approval_id=ap.id)
    policy = runtime.get("policy")
    notifier = getattr(policy, "notifier", None)
    review = notifier.review_url(ap.id, ap.expires_at) if notifier is not None else None
    return {"status": "approval_pending", "approval_id": ap.id, "kind": "repo_connect", "repo": slug,
            "why": f"{repos.reason_words(access)}. Nothing was rented.", "review_url": review,
            "can_answer_in_chat": False,
            "next": "Give the human review_url (in T3 Code open it beside the chat with preview_open). Wait with "
                    "approval_wait, then call environment_open again with the same arguments. If the repo name "
                    "was wrong, call environment_open with the right one: that withdraws this card.",
            "fallback": f"At the node: ssh {ssh_host(runtime.get('config'))} agentos environments repo add {slug} "
                        f"--token{' --replace' if reason == 'refused' else ''}. A token needs no admin rights on the "
                        "repository."}


# ---------------------------------------------------------------- checking a token with the host


def _expiry(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            when = datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
        return (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone(UTC).isoformat(timespec="seconds")
    return None


def _said(response: httpx.Response, token: Secret) -> str:
    """GitHub's own message, short, and never with the token in it."""
    try:
        message = str((response.json() or {}).get("message") or "")
    except ValueError:
        message = ""
    return message.replace(token.reveal(), "[token]")[:GITHUB_QUOTE].strip()


def _sso_url(header: str) -> str | None:
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == "url" and value.startswith("https://github.com/"):
            return value
    return None


def refusal(response: httpx.Response, slug: str, token: Secret) -> RepoConnectFailed:
    """What a GitHub answer other than 200 means, in words a person acts on."""
    owner = slug.split("/")[1]
    status = response.status_code
    said = _said(response, token)
    low = said.lower()
    if status == 401:
        return RepoConnectFailed("GitHub did not accept this token: it is wrong, expired or revoked. Make a new "
                                 "one and paste it here.", reason="bad_token")
    if status == 403:
        sso = _sso_url(response.headers.get(SSO_HEADER) or "")
        if sso:
            return RepoConnectFailed(f"This token must be authorized for the {owner} organization's single sign-on "
                                     f"(SSO) first. Open {sso}, authorize it, then paste it again.", reason="sso")
        if response.headers.get("x-ratelimit-remaining") == "0":
            return RepoConnectFailed("GitHub's rate limit for this token is used up. Try again in a few minutes.",
                                     reason="rate_limited")
        if "forbids access via" in low or "policy" in low:
            return RepoConnectFailed(f"The {owner} organization does not allow this kind of token. GitHub says: "
                                     f"\"{said}\". Make a token the organization allows (often a fine-grained "
                                     "one), or ask an admin of it.", reason="org_policy")
        if "approv" in low or "pending" in low:
            return RepoConnectFailed(f"This fine-grained token waits for the {owner} organization's approval. An "
                                     "admin of the organization approves it in its settings (Personal access "
                                     "tokens, Pending requests). Then paste it again.", reason="org_approval")
        return RepoConnectFailed(f"GitHub refused this token for {slug}. GitHub says: \"{said}\".",
                                 reason="forbidden")
    if status == 404:
        return RepoConnectFailed(f"This token cannot see {slug}. Check that you are a collaborator on it. A "
                                 "classic token needs the repo scope. For a fine-grained token: its resource owner "
                                 f"is {owner}, and its repository access includes {slug.split('/')[2]}. If {owner} "
                                 "is an organization, it may also still have to approve the token.",
                                 reason="cannot_see")
    return RepoConnectFailed(f"GitHub answered {status}, so the token could not be checked. Try again in a minute.",
                             reason="unreachable")


async def _github(slug: str, token: Secret) -> str | None:
    """GET /repos/{owner}/{name} with the token: GitHub's answer, and its expiry when it says one."""
    _, owner, name = slug.split("/")
    headers = {"Authorization": f"Bearer {token.reveal()}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "agentos-node"}
    try:
        async with httpx.AsyncClient(transport=transport, timeout=HTTP_TIMEOUT_S, follow_redirects=False) as c:
            response = await c.get(f"{GITHUB_API}/repos/{owner}/{name}", headers=headers)
    except httpx.HTTPError:
        raise RepoConnectFailed("The node could not reach GitHub to check the token. Try again in a minute.",
                                reason="unreachable") from None
    if response.status_code != 200:
        raise refusal(response, slug, token)
    return _expiry(response.headers.get(EXPIRY_HEADER))


async def _ls_remote(git: Any, home: Any, slug: str, token: Secret, *, probe: Any = None) -> None:
    """`git ls-remote` with the token, the way the git gateway fetches with it."""
    from agentos.git import keys as keymod
    from agentos.git.upstream import Target, credential_env, ls_remote, target_for, unreachable
    from agentos.git.wire import parse_remote

    remote = parse_remote(slug)
    cred = keymod.Credential(ref=keymod.REF_PREFIX + "card-check", kind=keymod.TOKEN_KIND, host=remote.host,
                             repos=(remote.repo,), label="")
    rule = git.remotes.find(remote, [WORKSPACE]) if git is not None else None
    target = target_for(rule, remote, cred) if rule is not None else \
        Target(remote, f"https://{remote.host}/{remote.repo}.git", cred)

    async def read(_ref: str, _purpose: str) -> str:
        return token.reveal()

    allow_file = bool(getattr(getattr(git, "gateway", None), "allow_file", False))
    async with credential_env(target, home=home, agent_dir=home, read_secret=read,
                              purpose=f"check a token for {slug}") as auth:
        ok, err = await (probe or ls_remote)(target.url, auth.env, timeout_s=20.0, allow_file=allow_file)
    if ok:
        return
    if unreachable(err):
        raise RepoConnectFailed(f"The node could not reach {remote.host} to check the token. Try again in a minute.",
                                reason="unreachable")
    if remote.host == "github.com":
        raise RepoConnectFailed(f"GitHub accepts this token, but git cannot read the code of {slug} with it. Give "
                                "it read access to the code: a fine-grained token needs Contents: Read (Read and "
                                "write to push), a classic token the repo scope.", reason="no_contents")
    raise RepoConnectFailed(f"{remote.host} did not let git read {slug} with this token: it may be wrong, "
                            "expired, or lack read access to the code.", reason="bad_token")


# ---------------------------------------------------------------- keeping it


def _vault(runtime: Any) -> Any:
    vault = runtime.get("vault")
    if vault is None:
        raise ServiceUnavailable("This node has no vault to keep a token in. At the node, run: agentos vault init")
    return vault


def _remotes(mgr: Any) -> Any:
    git = mgr.runtime.get("git")
    if git is not None:
        return git.remotes
    from agentos.git.remotes import Remotes

    return Remotes(mgr.home / "git" / "remotes.json")


async def connect_with_token(mgr: Any, approval_id: str, token: Secret) -> dict[str, Any]:
    """Check the token with the host and with git, keep it in the vault for this one repository, declare
    the repository for env, and close its cards. Nothing is kept when a check fails."""
    from agentos.git import keys as keymod
    from agentos.git.credentials import git_credentials, put_token
    from agentos.git.wire import parse_remote

    runtime = mgr.runtime
    approvals = runtime.get("approvals")
    row = request(mgr.store, approvals, approval_id)
    remote = parse_remote(row["repo"])
    try:
        token = keymod.check_token(token.reveal())
    except InvalidRequest:
        raise InvalidRequest("A token is one line of letters and digits, at least 8 of them. Copy it again from "
                             "GitHub.") from None
    vault = _vault(runtime)
    ensure = getattr(vault, "ensure_unsealed", None)
    if callable(ensure):
        await ensure()  # a sealed vault says so now, before the token is sent anywhere
    expires = await _github(remote.slug, token) if remote.host == "github.com" else None
    await _ls_remote(runtime.get("git"), mgr.home, remote.slug, token, probe=mgr.repo_probe)
    # the repository's own key or token was refused or missing: this one replaces them
    old = [c for c in await git_credentials(vault) if c.host == remote.host and c.repos == (remote.repo,)]
    kept = await put_token(vault, remote.host, [remote.repo], token, replace=True)
    for c in old:
        if c.name != kept["name"]:
            await vault.delete_secret(c.ref)
    remotes = _remotes(mgr)
    if remotes.find(remote, [WORKSPACE]) is None:
        remotes.allow(WORKSPACE, remote.slug, push=True)
    closed = close(mgr.store, approvals, remote.slug)
    log.info("repository %s is connected with a token from its card", remote.slug)
    return {"repo": remote.slug, "connected": True, "credential": kept["name"], "expires_at": expires,
            "closed": closed}


async def deploy_key(mgr: Any, approval_id: str) -> dict[str, Any]:
    """The node's deploy key for the card's repository: the one the vault has, or a new one (as
    `agentos environments repo add` makes it). The repository is declared for env, and a repository
    admin adds the key at the host."""
    from agentos.git import keys as keymod
    from agentos.git.credentials import deploy_key_hint, git_credentials, put_deploy_key
    from agentos.git.wire import parse_remote

    runtime = mgr.runtime
    row = request(mgr.store, runtime.get("approvals"), approval_id)
    remote = parse_remote(row["repo"])
    vault = _vault(runtime)
    have = keymod.pick(await git_credentials(vault), remote)
    if have is not None and have.type == "ssh" and have.public_key:
        key = {"name": have.name, "public_key": have.public_key, "fingerprint": have.fingerprint}
    elif remote.host not in keymod.KNOWN_HOSTS:
        raise InvalidRequest(f"A deploy key for {remote.host} is made at the node, which checks the host's key. "
                             f"Run: agentos environments repo add {remote.slug}")
    else:
        key = await put_deploy_key(vault, remote.host, [remote.repo], replace=True)
    remotes = _remotes(mgr)
    if remotes.find(remote, [WORKSPACE]) is None:
        remotes.allow(WORKSPACE, remote.slug, push=True)
    return {"repo": remote.slug, "name": key["name"], "public_key": key["public_key"],
            "fingerprint": key["fingerprint"],
            "settings_url": links(remote.slug).get("deploy_keys_url") or deploy_key_hint(remote.host, [remote.repo])}


async def check(mgr: Any, approval_id: str) -> dict[str, Any]:
    """"I added it, check": can a machine clone the repository now? Closes the card when it can."""
    from agentos.environments import repos

    approvals = mgr.runtime.get("approvals")
    row = request(mgr.store, approvals, approval_id)
    access = await mgr.repo_access(row["repo"])
    if access.ok:
        return {"repo": row["repo"], "connected": True, "closed": close(mgr.store, approvals, row["repo"])}
    if access.unreachable:
        raise RepoConnectFailed(f"The node could not reach {_host(row['repo'])}. Try again in a minute.",
                                reason="unreachable")
    raise RepoConnectFailed(f"Not yet: {repos.reason_words(access)}. Add the deploy key to the repository (with "
                            "write access to push), then check again.", reason="not_yet")


async def sweep(mgr: Any) -> list[str]:
    """The reaper's look at open cards: a repository connected some other way (the CLI) closes its cards.
    Only one that is declared with a credential is checked, at most once a minute."""
    approvals = mgr.runtime.get("approvals") if mgr.runtime is not None else None
    if approvals is None:
        return []
    closed: list[str] = []
    now = time.monotonic()
    rows = waiting(mgr.store, approvals)
    for gone in set(_swept) - {r["id"] for r in rows}:
        del _swept[gone]
    for row in rows:
        if now - _swept.get(row["id"], 0.0) < SWEEP_EVERY_S:
            continue
        _swept[row["id"]] = now
        if not await _declared_with_credential(mgr, row["repo"]):
            continue
        access = await mgr.repo_access(row["repo"])
        if access.ok:
            closed += close(mgr.store, approvals, row["repo"])
    return closed


async def _declared_with_credential(mgr: Any, slug: str) -> bool:
    from agentos.git import keys as keymod
    from agentos.git.credentials import git_credentials
    from agentos.git.wire import parse_remote

    vault = mgr.runtime.get("vault")
    remote = parse_remote(slug)
    if vault is None or _remotes(mgr).find(remote, [WORKSPACE]) is None:
        return False
    return keymod.pick(await git_credentials(vault), remote) is not None
