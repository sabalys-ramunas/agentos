"""A stand-in for GitHub's pull request API, on 127.0.0.1, so environment_pull_request works offline.

The node's environments service sends its GitHub calls here (tests/env_eval/node_main.py sets
`github_api`). It answers the three calls agentos.environments.pulls makes, from the run's own bare
repositories (`repos`: "owner/name" -> its bare repository's path):

- GET  /repos/{owner}/{name}           the repository, with its default branch (the bare repo's HEAD)
- POST /repos/{owner}/{name}/pulls     a pull request: 201, or 422 as GitHub answers when the head
                                       branch is not in the repository or one is open for it already
- GET  /repos/{owner}/{name}/pulls     the open ones, `head=owner:branch` narrows it

Every request must carry a bearer token (any): the node sends the vault's. What was opened is kept
in `pulls`, for the checkers.
"""

from __future__ import annotations

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

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802 - http.server's name
                stub._answer(self, "GET")

            def do_POST(self) -> None:  # noqa: N802
                stub._answer(self, "POST")

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


def _send(req: http.server.BaseHTTPRequestHandler, status: int, data: Any) -> None:
    raw = json.dumps(data).encode()
    req.send_response(status)
    req.send_header("Content-Type", "application/json")
    req.send_header("Content-Length", str(len(raw)))
    req.end_headers()
    req.wfile.write(raw)
