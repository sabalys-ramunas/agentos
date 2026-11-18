"""The pull request shows the proof: environment_pull_request puts the environment's proofs (screenshots,
recordings) in the repository and a Proof section in the pull request's body (agentos.environments.pulls).

Where the files go. In the repository itself, never in the pull request's diff: the branch
`agentos-proofs`, made as an orphan branch (a commit with no parent) through GitHub's Git Data API
(blobs, a tree, a commit, a ref) with the same token that opens the pull request, so it shares no
history with the code and nothing of it is ever merged. Each file is at `<environment id>/<proof
id>.<ext>`, and the body links to `https://github.com/<owner>/<repo>/blob/agentos-proofs/<path>?raw=true`,
which GitHub serves to whoever can see the repository (a private one's viewers, signed in; a public
one's, the world). A file already there is not sent again.

What GitHub shows. A Markdown image of a PNG or a GIF is shown in the body; GitHub plays a video only
when it was uploaded as an attachment by hand, which its API does not offer, so a recording shows as
its GIF preview with the MP4 as a link. GitHub takes at most 100 MB a file (a recording is at most
VIDEO_BYTES_MAX) and shows an image inline up to about 10 MB (INLINE_MAX); a larger one is a link.

The body. agentos's sections sit between markers (PROOF_BEGIN/END, and the network's NET_BEGIN/END):
calling environment_pull_request again for an open pull request replaces only what is between them,
never the agent's or a person's text around them.

Only an environment's own proofs are ever sent: they are pictures of its own browser, which holds no
account and no vault entry. An account lane has no proofs.
"""

from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from typing import Any

from agentos.core.errors import AgentosError, InvalidRequest

BRANCH = "agentos-proofs"
PROOF_BEGIN, PROOF_END = "<!-- agentos:proofs:begin -->", "<!-- agentos:proofs:end -->"
NET_BEGIN, NET_END = "<!-- agentos:network:begin -->", "<!-- agentos:network:end -->"
FILE_MAX = 100 * 1000 * 1000 - (1 << 20)   # GitHub refuses a file of 100 MB or more
INLINE_MAX = 10 << 20                       # an image GitHub shows in the body
UPLOAD_TIMEOUT_S = 300.0
_MD = re.compile(r"([\\`*_\[\]<>|!#])")


class _Race(Exception):
    """The branch moved while this made its commit (another pull request's proofs): try once more."""


class ProofsNotSent(AgentosError):
    """GitHub did not take the proofs (the branch is protected, the token may not write): the pull
    request still opens, and says nothing of them."""
    code = "pull_request_failed"


def md(text: str) -> str:
    """A title as Markdown text: its marks escaped, no HTML comment (it would end a section)."""
    return _MD.sub(r"\\\1", " ".join(str(text).split()).replace("-->", "->"))


def set_section(body: str | None, begin: str, end: str, section: str) -> str:
    """`body` with what lies between `begin` and `end` (the markers too) replaced by `section` in
    markers; appended when the body has no such markers; removed when `section` is empty."""
    body = body or ""
    block = f"{begin}\n{section.strip()}\n{end}" if section.strip() else ""
    i = body.find(begin)
    j = body.find(end, i + len(begin)) if i >= 0 else -1
    if i >= 0 and j >= 0:
        before, after = body[:i].rstrip("\n"), body[j + len(end):]
        return "\n\n".join(p for p in (before, block) if p) + after
    if not block:
        return body
    return f"{body.rstrip()}\n\n{block}" if body.strip() else block


def choose(records: list[dict], wanted: Any) -> list[dict]:
    """The proofs a pull request shows: every one (None or True), none (False), or the ones named by id
    or title."""
    if wanted is None or wanted is True:
        return list(records)
    if wanted is False:
        return []
    if not isinstance(wanted, list) or not all(isinstance(w, str) for w in wanted):
        raise InvalidRequest("proofs is true, false, or a list of proof ids or titles from environment()")
    out = []
    for name in wanted:
        hit = next((r for r in records if name in (r.get("id"), r.get("title"))), None)
        if hit is None:
            have = ", ".join(f"{r['id']} ({r.get('title')})" for r in records[-5:]) or "none yet"
            raise InvalidRequest(f"no proof {name!r} in this environment; its proofs: {have}")
        if hit not in out:
            out.append(hit)
    return out


def files_of(mgr: Any, env_id: str, record: dict) -> list[tuple[str, Path]]:
    """The files of one proof to send: (extension, path). A recording's MP4 and GIF, a shot's PNG."""
    exts = (("mp4", "gif") if record.get("kind") == "video" else ("png",))
    out = []
    for ext in exts:
        path = mgr.proof_path(env_id, record["id"], ext)
        if path is not None:
            out.append((ext, path))
    return out


def link(repo: str, path: str) -> str:
    return f"https://github.com/{repo}/blob/{BRANCH}/{path}?raw=true"


def _clock(seconds: Any) -> str:
    s = round(float(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


def _mb(n: Any) -> str:
    return f"{(int(n or 0) / (1 << 20)):.1f} MB"


def section(repo: str, env_id: str, records: list[dict], sent: dict[str, dict[str, int]]) -> str:
    """The Proof section: each proof's title, its picture (a recording's GIF) and a recording's MP4 as
    a link. `sent`: proof id -> {extension: bytes} of the files on the branch."""
    lines = ["### Proof", ""]
    for r in records:
        files = sent.get(r["id"]) or {}
        if not files:
            continue
        title = md(r.get("title") or r["id"])
        path = f"{env_id}/{r['id']}"
        if r.get("kind") == "video":
            lines.append(f"**{title}** (recording, {_clock(r.get('duration_s'))})")
            if "gif" in files and files["gif"] <= INLINE_MAX:
                lines += ["", f"![{title}]({link(repo, path + '.gif')})"]
            if "mp4" in files:
                lines += ["", f"[Watch the recording (MP4, {_mb(files['mp4'])})]({link(repo, path + '.mp4')})"]
        else:
            lines.append(f"**{title}**")
            if files.get("png", 0) <= INLINE_MAX:
                lines += ["", f"![{title}]({link(repo, path + '.png')})"]
            else:
                lines += ["", f"[The screenshot ({_mb(files['png'])})]({link(repo, path + '.png')})"]
        lines.append("")
    if len(lines) == 2:
        return ""
    lines.append(f"<sub>Kept by agentos on this repository's `{BRANCH}` branch: anyone who can see the repository "
                 "can open them.</sub>")
    return "\n".join(lines)


async def _json(resp: Any, what: str, token: str, ok: tuple[int, ...] = (200, 201)) -> Any:
    from agentos.environments.pulls import _why

    if resp.status_code not in ok:
        raise ProofsNotSent(f"GitHub did not {what} ({resp.status_code}: {_why(resp, token)})")
    return resp.json()


async def _blob(http: Any, repo: str, auth: dict, token: str, path: Path) -> str:
    data = await asyncio.to_thread(lambda: base64.b64encode(path.read_bytes()).decode())
    resp = await http.post(f"/repos/{repo}/git/blobs", headers=auth, json={"content": data, "encoding": "base64"},
                           timeout=UPLOAD_TIMEOUT_S)
    return str((await _json(resp, "take a proof's file", token))["sha"])


async def _on_branch(http: Any, repo: str, auth: dict, token: str, env_id: str) -> tuple[str | None, dict[str, int]]:
    """The branch's head commit (None when there is no branch yet) and the files of this environment
    already on it (name -> bytes)."""
    got = await http.get(f"/repos/{repo}/git/ref/heads/{BRANCH}", headers=auth)
    if got.status_code == 404:
        return None, {}
    head = str((await _json(got, f"show the {BRANCH} branch", token))["object"]["sha"])
    listed = await http.get(f"/repos/{repo}/contents/{env_id}", headers=auth, params={"ref": BRANCH})
    if listed.status_code == 404:
        return head, {}
    rows = await _json(listed, f"list the {BRANCH} branch", token)
    return head, {str(r.get("name")): int(r.get("size") or 0) for r in rows if isinstance(r, dict)} \
        if isinstance(rows, list) else {}


async def _commit(http: Any, repo: str, auth: dict, token: str, head: str | None, tree: list[dict],
                  env_id: str) -> None:
    """One commit with the new files on the branch: on its head, or the first, with no parent (orphan)."""
    body: dict[str, Any] = {"tree": tree}
    if head is not None:
        parent = await _json(await http.get(f"/repos/{repo}/git/commits/{head}", headers=auth),
                             "show the proofs branch's commit", token)
        body["base_tree"] = parent["tree"]["sha"]
    made = await _json(await http.post(f"/repos/{repo}/git/trees", headers=auth, json=body), "make a tree", token)
    commit = await _json(await http.post(f"/repos/{repo}/git/commits", headers=auth, json={
        "message": f"agentos: proofs of environment {env_id}", "tree": made["sha"],
        "parents": [head] if head is not None else []}), "make a commit", token)
    if head is None:
        resp = await http.post(f"/repos/{repo}/git/refs", headers=auth,
                               json={"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    else:
        resp = await http.patch(f"/repos/{repo}/git/refs/heads/{BRANCH}", headers=auth,
                                json={"sha": commit["sha"], "force": False})
    if resp.status_code == 422:
        raise _Race()
    await _json(resp, f"move the {BRANCH} branch", token)


async def send(mgr: Any, http: Any, repo: str, auth: dict, token: str, env_id: str,
               records: list[dict]) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Put the proofs' files on the branch (the ones not there yet). Returns proof id -> {extension:
    bytes} of its files there, and the files too large for GitHub. Raises ProofsNotSent; a race with
    another push is tried once more."""
    for attempt in (1, 2):
        head, there = await _on_branch(http, repo, auth, token, env_id)
        sent: dict[str, dict[str, int]] = {}
        skipped: list[str] = []
        tree = []
        for r in records:
            for ext, path in files_of(mgr, env_id, r):
                name, size = f"{r['id']}.{ext}", path.stat().st_size
                if size > FILE_MAX:
                    skipped.append(name)  # GitHub takes no file this large: the link on the node stays
                    continue
                if name not in there:
                    tree.append({"path": f"{env_id}/{name}", "mode": "100644", "type": "blob",
                                 "sha": await _blob(http, repo, auth, token, path)})
                sent.setdefault(r["id"], {})[ext] = size
        if not tree:
            return sent, skipped
        try:
            await _commit(http, repo, auth, token, head, tree, env_id)
            return sent, skipped
        except _Race:
            if attempt == 2:
                raise ProofsNotSent(f"the {BRANCH} branch kept moving while the proofs were sent; call "
                                    "environment_pull_request again") from None
    return {}, []
