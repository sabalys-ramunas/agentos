"""A stand-in for GitHub's pull request API, on 127.0.0.1, so environment_pull_request works offline.

The node's environments service sends its GitHub calls here (tests/env_eval/node_main.py sets
`github_api`). It answers the three calls agentos.environments.pulls makes, from the run's own bare
repositories (`repos`: "owner/name" -> its bare repository's path):

- GET  /repos/{owner}/{name}           the repository, with its default branch (the bare repo's HEAD)
- POST /repos/{owner}/{name}/pulls     a pull request: 201, or 422 as GitHub answers when the head
                                       branch is not in the repository or one is open for it already
- GET  /repos/{owner}/{name}/pulls     the open ones, `head=owner:branch` narrows it
- PATCH /repos/{owner}/{name}/pulls/N  a new body (agentos's proof section in an open one)

and, for the proofs a pull request shows (agentos.environments.prproofs), the Git Data API calls it
makes, in memory: blobs, trees, commits, the `agentos-proofs` ref, and the contents of a folder there.

Every request must carry a bearer token (any): the node sends the vault's. What was opened is kept
in `pulls`, and the proof files sent in `files(repo)`, for the checkers.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qs, urlsplit


def git_out(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "--git-dir", str(repo), *args], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


@dataclass
class GitHubStub:
    port: int
    repos: dict[str, Path] = field(default_factory=dict)
    pulls: list[dict[str, Any]] = field(default_factory=list)
    blobs: dict[str, bytes] = field(default_factory=dict)
    trees: dict[str, dict[str, str]] = field(default_factory=dict)         # sha -> {path: blob sha}
    commits: dict[str, dict[str, Any]] = field(default_factory=dict)       # sha -> {tree, parents}
    refs: dict[tuple[str, str], str] = field(default_factory=dict)         # (repo, branch) -> commit sha

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                stub._answer(self, "GET")

            def do_POST(self) -> None:
                stub._answer(self, "POST")

            def do_PATCH(self) -> None:
                stub._answer(self, "PATCH")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    def reset(self) -> None:
        with self._lock:
            self.repos.clear()
            self.pulls.clear()
            self.blobs.clear()
            self.trees.clear()
            self.commits.clear()
            self.refs.clear()

    def files(self, repo: str, branch: str = "agentos-proofs") -> dict[str, bytes]:
        """The files on a branch the Git Data API made: path -> bytes."""
        with self._lock:
            head = self.refs.get((repo, branch))
            tree = self.trees.get(self.commits[head]["tree"], {}) if head else {}
            return {path: self.blobs.get(sha, b"") for path, sha in tree.items()}

    def opened(self, repo: str) -> list[dict[str, Any]]:
        with self._lock:
            return [p for p in self.pulls if p["repo"] == repo]

    # ---- the API ----

    def _answer(self, req: http.server.BaseHTTPRequestHandler, method: str) -> None:
        if not (req.headers.get("Authorization") or "").startswith("Bearer "):
            return _send(req, 401, {"message": "Requires authentication"})
        parts = urlsplit(req.path)
        bits = [b for b in parts.path.split("/") if b]
        if len(bits) < 3 or bits[0] != "repos":
            return _send(req, 404, {"message": "Not Found"})
        repo = f"{bits[1]}/{bits[2]}"
        bare = self.repos.get(repo)
        if bare is None:
            return _send(req, 404, {"message": "Not Found"})
        default = git_out(bare, "symbolic-ref", "--short", "HEAD") or "main"
        if len(bits) == 3 and method == "GET":
            return _send(req, 200, {"full_name": repo, "default_branch": default, "private": True})
        if len(bits) > 3 and bits[3] in ("git", "contents"):
            return self._git(req, method, repo, bits[3:], parse_qs(parts.query))
        if len(bits) == 5 and bits[3] == "pulls" and method == "PATCH":
            return self._edit(req, repo, int(bits[4]) if bits[4].isdigit() else 0, _body(req))
        if len(bits) == 4 and bits[3] == "pulls":
            if method == "GET":
                head = (parse_qs(parts.query).get("head") or [""])[0].partition(":")[2]
                with self._lock:
                    rows = [p for p in self.pulls if p["repo"] == repo and (not head or p["head"]["ref"] == head)]
                return _send(req, 200, rows)
            length = int(req.headers.get("Content-Length") or 0)
            try:
                body = json.loads(req.rfile.read(length) or b"{}")
            except ValueError:
                return _send(req, 400, {"message": "Problems parsing JSON"})
            return self._open(req, repo, bare, body)
        return _send(req, 404, {"message": "Not Found"})

    def _edit(self, req: http.server.BaseHTTPRequestHandler, repo: str, number: int, body: dict) -> None:
        with self._lock:
            pr = next((p for p in self.pulls if p["repo"] == repo and p["number"] == number), None)
            if pr is not None and "body" in body:
                pr["body"] = body["body"]
        return _send(req, 200, pr) if pr is not None else _send(req, 404, {"message": "Not Found"})

    def _git(self, req: http.server.BaseHTTPRequestHandler, method: str, repo: str, bits: list[str],
             query: dict[str, list[str]]) -> None:
        """The Git Data API, as far as agentos's proofs use it."""
        body = _body(req) if method in ("POST", "PATCH") else {}
        what = "/".join(bits[1:])
        with self._lock:
            if bits[0] == "contents" and method == "GET":
                head = self.refs.get((repo, (query.get("ref") or [""])[0]))
                tree = self.trees.get(self.commits[head]["tree"], {}) if head else {}
                rows = [{"name": p.split("/", 1)[1], "size": len(self.blobs.get(sha, b"")), "sha": sha}
                        for p, sha in tree.items() if p.startswith(what + "/")]
                return _send(req, 200, rows) if rows else _send(req, 404, {"message": "Not Found"})
            if what == "blobs" and method == "POST":
                data = base64.b64decode(str(body.get("content") or ""))
                sha = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()  # git's blob id, as GitHub's
                self.blobs[sha] = data
                return _send(req, 201, {"sha": sha})
            if what == "trees" and method == "POST":
                tree = dict(self.trees.get(str(body.get("base_tree") or ""), {}))
                tree.update({str(e["path"]): str(e["sha"]) for e in body.get("tree") or []})
                sha = hashlib.sha1(json.dumps(tree, sort_keys=True).encode()).hexdigest()
                self.trees[sha] = tree
                return _send(req, 201, {"sha": sha})
            if what == "commits" and method == "POST":
                commit = {"tree": str(body.get("tree")), "parents": list(body.get("parents") or [])}
                sha = hashlib.sha1(json.dumps(commit, sort_keys=True).encode() + str(len(self.commits)).encode()).hexdigest()
                self.commits[sha] = commit
                return _send(req, 201, {"sha": sha})
            if what.startswith("commits/") and method == "GET":
                commit = self.commits.get(what.split("/", 1)[1])
                if commit is None:
                    return _send(req, 404, {"message": "Not Found"})
                return _send(req, 200, {"sha": what.split("/", 1)[1], "tree": {"sha": commit["tree"]}})
            if what.startswith(("ref/heads/", "refs/heads/")):
                branch = what.split("/", 2)[2]
                if method == "GET":
                    head = self.refs.get((repo, branch))
                    return _send(req, 200, {"object": {"sha": head}}) if head else _send(req, 404, {"message": "Not Found"})
                if method == "PATCH":
                    self.refs[(repo, branch)] = str(body.get("sha"))
                    return _send(req, 200, {"object": {"sha": body.get("sha")}})
            if what == "refs" and method == "POST":
                branch = str(body.get("ref") or "").removeprefix("refs/heads/")
                if (repo, branch) in self.refs:
                    return _send(req, 422, {"message": "Reference already exists"})
                self.refs[(repo, branch)] = str(body.get("sha"))
                return _send(req, 201, {"object": {"sha": body.get("sha")}})
        return _send(req, 404, {"message": "Not Found"})

    def _open(self, req: http.server.BaseHTTPRequestHandler, repo: str, bare: Path, body: dict) -> None:
        head, base = str(body.get("head") or ""), str(body.get("base") or "")
        sha = git_out(bare, "rev-parse", "--verify", f"refs/heads/{head}")
        if not sha:
            return _send(req, 422, {"message": "Validation Failed", "errors": [
                {"resource": "PullRequest", "field": "head", "code": "invalid"}]})
        if not git_out(bare, "rev-parse", "--verify", f"refs/heads/{base}"):
            return _send(req, 422, {"message": "Validation Failed", "errors": [
                {"resource": "PullRequest", "field": "base", "code": "invalid"}]})
        with self._lock:
            if any(p["repo"] == repo and p["head"]["ref"] == head for p in self.pulls):
                return _send(req, 422, {"message": "Validation Failed", "errors": [
                    {"message": f"A pull request already exists for {repo.split('/')[0]}:{head}."}]})
            number = len(self.pulls) + 1
            pr = {"repo": repo, "number": number, "html_url": f"{self.url}/{repo}/pull/{number}", "state": "open",
                  "title": body.get("title"), "body": body.get("body"), "draft": bool(body.get("draft")),
                  "head": {"ref": head, "sha": sha}, "base": {"ref": base}}
            self.pulls.append(pr)
        _send(req, 201, pr)


def _body(req: http.server.BaseHTTPRequestHandler) -> dict:
    length = int(req.headers.get("Content-Length") or 0)
    try:
        data = json.loads(req.rfile.read(length) or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _send(req: http.server.BaseHTTPRequestHandler, status: int, data: Any) -> None:
    raw = json.dumps(data).encode()
    req.send_response(status)
    req.send_header("Content-Type", "application/json")
    req.send_header("Content-Length", str(len(raw)))
    req.end_headers()
    req.wfile.write(raw)
