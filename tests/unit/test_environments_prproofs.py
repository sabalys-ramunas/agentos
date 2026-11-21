"""The pull request shows the proof (openajar.environments.prproofs and pulls): the files on an orphan
agentos-proofs branch through the Git Data API, the Proof section made and kept up to date between its
markers, proofs=false and pr_proofs, GitHub's size limits, a refused upload, and no token."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from test_environments_comeback import FakeGateway, cred

from openajar.core.errors import InvalidRequest
from openajar.environments import prproofs, pulls
from openajar.environments.repoconfig import blob_id
from openajar.git import keys as keymod

ENV = "eproofs1234"
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 50
MP4 = b"\0\0\0\x18ftypisom" + b"m" * 200
GIF = b"GIF89a" + b"g" * 80


class GitHub:
    """api.github.com for me/shop: the repository, its pulls, and the Git Data API of its branches."""

    def __init__(self, *, pr_exists: bool = False, body: str = "", refuse_blobs: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict] = {}
        self.ref: str | None = None
        self.pr_exists, self.body, self.refuse_blobs = pr_exists, body, refuse_blobs
        self.posted: dict | None = None
        self.auth: set[str] = set()

    def files(self) -> dict[str, bytes]:
        if self.ref is None:
            return {}
        return {p: self.blobs[s] for p, s in self.trees[self.commits[self.ref]["tree"]].items()}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path.removeprefix("/repos/me/shop"), request.method
        body = json.loads(request.content or b"{}")
        self.calls.append((method, path))
        self.auth.add(request.headers.get("authorization"))
        assert "merge" not in path
        if method == "GET" and path == "":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/pulls" and method == "POST":
            if self.pr_exists:
                return httpx.Response(422, json={"message": "Validation Failed",
                                                 "errors": [{"message": "A pull request already exists"}]})
            self.posted = body
            return httpx.Response(201, json={"html_url": "https://github.com/me/shop/pull/7", "number": 7,
                                             "state": "open", "draft": False, "body": body["body"]})
        if path == "/pulls" and method == "GET":
            return httpx.Response(200, json=[{"html_url": "https://github.com/me/shop/pull/3", "number": 3,
                                              "state": "open", "draft": False, "body": self.body}])
        if path == "/pulls/3" and method == "PATCH":
            self.body = body["body"]
            return httpx.Response(200, json={"number": 3, "body": self.body})
        if path == f"/git/ref/heads/{prproofs.BRANCH}":
            if self.ref is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.ref}})
        if path == f"/contents/{ENV}" and method == "GET":
            assert request.url.params["ref"] == prproofs.BRANCH
            tree = self.trees[self.commits[self.ref]["tree"]]
            rows = [{"name": p.split("/", 1)[1], "size": len(self.blobs[sha]), "sha": sha} for p, sha in tree.items()
                    if p.startswith(ENV + "/")]
            return httpx.Response(200, json=rows) if rows else httpx.Response(404, json={"message": "Not Found"})
        if path == "/git/blobs":
            if self.refuse_blobs:
                return httpx.Response(403, json={"message": "Resource not accessible by integration"})
            data = base64.b64decode(body["content"])
            sha = blob_id(data)  # git's id, as GitHub gives it
            self.blobs[sha] = data
            return httpx.Response(201, json={"sha": sha})
        if path.startswith("/git/commits/") and method == "GET":
            return httpx.Response(200, json={"tree": {"sha": self.commits[path.rsplit("/", 1)[1]]["tree"]}})
        if path == "/git/trees":
            tree = dict(self.trees[body["base_tree"]]) if "base_tree" in body else {}
            tree.update({e["path"]: e["sha"] for e in body["tree"]})
            sha = f"t{len(self.trees)}"
            self.trees[sha] = tree
            return httpx.Response(201, json={"sha": sha})
        if path == "/git/commits" and method == "POST":
            sha = f"c{len(self.commits)}"
            self.commits[sha] = {"tree": body["tree"], "parents": body["parents"]}
            return httpx.Response(201, json={"sha": sha})
        if path == "/git/refs" and method == "POST":
            if self.ref is not None:
                return httpx.Response(422, json={"message": "Reference already exists"})
            assert body["ref"] == f"refs/heads/{prproofs.BRANCH}"
            self.ref = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"]})
        if path == f"/git/refs/heads/{prproofs.BRANCH}" and method == "PATCH":
            assert body["force"] is False and self.commits[body["sha"]]["parents"] == [self.ref]
            self.ref = body["sha"]
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "Not Found"})


def setup(tmp_path, github: GitHub, *, token=True, pr_proofs=True, blocked=()):
    folder = tmp_path / "proofs"
    folder.mkdir(exist_ok=True)
    records = [
        {"id": "pshot000001", "kind": "screenshot", "title": "cart after the fix", "width": 1280, "height": 720},
        {"id": "pvideo00001", "kind": "video", "title": "guest checkout, end [to] end", "duration_s": 27.4,
         "bytes": len(MP4), "gif": {"bytes": len(GIF)}},
    ]
    (folder / "pshot000001.png").write_bytes(PNG)
    (folder / "pvideo00001.mp4").write_bytes(MP4)
    (folder / "pvideo00001.gif").write_bytes(GIF)

    def proof_path(env_id, proof_id, ext):
        p = folder / f"{proof_id}.{ext}"
        return p if env_id == ENV and p.is_file() else None

    gw = FakeGateway([cred("git:shop-token")] if token else [cred("git:k", kind=keymod.SSH_KIND)],
                     {"git:shop-token": "tok-secret"})
    mgr = SimpleNamespace(github_transport=httpx.MockTransport(github.handler), gateway=gw,
                          _get=lambda name: SimpleNamespace(gateway=gw) if name == "git" else None,
                          push_lists=lambda env: {"pushed": [{"branch": "sale"}], "deleted": []},
                          proofs=lambda env, links=False: list(records), proof_path=proof_path,
                          settings=SimpleNamespace(pr_proofs=pr_proofs))
    env = SimpleNamespace(id=ENV, repo="github.com/me/shop", own_branches=["sale"], blocked=list(blocked))
    return mgr, env, records


def link(name: str) -> str:
    return f"https://github.com/me/shop/blob/agentos-proofs/{ENV}/{name}?raw=true"


async def test_a_new_pull_request_shows_every_proof_from_an_orphan_branch(tmp_path):
    gh = GitHub()
    mgr, env, _ = setup(tmp_path, gh)
    got = await pulls.open_pull_request(mgr, env, title="Thirty percent off", body="Tested: npm test")
    assert got["created"] and got["proofs"] == {"in_pull_request": 2, "branch": "agentos-proofs"}
    assert gh.files() == {f"{ENV}/pshot000001.png": PNG, f"{ENV}/pvideo00001.mp4": MP4,
                          f"{ENV}/pvideo00001.gif": GIF}
    [commit] = gh.commits.values()
    assert commit["parents"] == []  # an orphan: it shares no history with the code
    body = gh.posted["body"]
    assert body.startswith("Tested: npm test\n\n" + prproofs.PROOF_BEGIN) and body.endswith(prproofs.PROOF_END)
    assert f"![cart after the fix]({link('pshot000001.png')})" in body
    assert f"![guest checkout, end \\[to\\] end]({link('pvideo00001.gif')})" in body  # the title's marks escaped
    assert f"[Watch the recording (MP4, 0.0 MB)]({link('pvideo00001.mp4')})" in body and "(recording, 0:27)" in body
    assert "anyone who can see the repository" in body
    assert gh.auth == {"Bearer tok-secret"} and "tok-secret" not in json.dumps(got)


async def test_again_on_an_open_one_replaces_only_its_own_section(tmp_path):
    gh = GitHub()
    mgr, env, records = setup(tmp_path, gh)
    await pulls.open_pull_request(mgr, env, title="Fix")
    person = "A person's words.\n\nTested by hand."
    gh.pr_exists = True
    gh.body = "The agent's text.\n\n" + gh.posted["body"] + "\n\n" + person
    records.append({"id": "pshot000002", "kind": "screenshot", "title": "the phone layout"})
    (tmp_path / "proofs" / "pshot000002.png").write_bytes(PNG)
    blobs_before = gh.calls.count(("POST", "/git/blobs"))
    got = await pulls.open_pull_request(mgr, env, title="Fix", body="ignored for an open one")
    assert got["created"] is False and got["url"] == "https://github.com/me/shop/pull/3"
    assert gh.calls.count(("POST", "/git/blobs")) == blobs_before + 1  # only the new file went up
    assert ("PATCH", f"/git/refs/heads/{prproofs.BRANCH}") in gh.calls
    assert gh.body.startswith("The agent's text.\n\n" + prproofs.PROOF_BEGIN) and gh.body.endswith(person)
    assert gh.body.count(prproofs.PROOF_BEGIN) == 1 and "the phone layout" in gh.body
    assert "ignored for an open one" not in gh.body
    patches = [c for c in gh.calls if c == ("PATCH", "/pulls/3")]
    await pulls.open_pull_request(mgr, env, title="Fix")  # nothing new: nothing changes
    assert [c for c in gh.calls if c == ("PATCH", "/pulls/3")] == patches


async def test_proofs_false_and_pr_proofs_off_leave_them_out(tmp_path):
    gh = GitHub()
    mgr, env, _ = setup(tmp_path, gh)
    got = await pulls.open_pull_request(mgr, env, title="Fix", body="Text.", proofs=False)
    assert "proofs" not in got and gh.posted["body"] == "Text." and gh.blobs == {}
    assert not any("/git/" in p for _, p in gh.calls)
    gh2 = GitHub()
    mgr, env, _ = setup(tmp_path, gh2, pr_proofs=False)
    await pulls.open_pull_request(mgr, env, title="Fix", body="Text.")
    assert gh2.posted["body"] == "Text." and gh2.blobs == {}
    gh3 = GitHub()
    mgr, env, _ = setup(tmp_path, gh3, pr_proofs=False)
    got = await pulls.open_pull_request(mgr, env, title="Fix", proofs=["cart after the fix"])  # named: shown
    assert got["proofs"]["in_pull_request"] == 1 and list(gh3.files()) == [f"{ENV}/pshot000001.png"]


async def test_named_proofs_must_be_this_environments(tmp_path):
    gh = GitHub()
    mgr, env, _ = setup(tmp_path, gh)
    with pytest.raises(InvalidRequest, match="no proof 'pnope'.*pshot000001"):
        await pulls.open_pull_request(mgr, env, title="Fix", proofs=["pnope"])
    with pytest.raises(InvalidRequest, match="true, false, or a list"):
        await pulls.open_pull_request(mgr, env, title="Fix", proofs="all")
    assert gh.calls == []


async def test_a_file_github_cannot_take_stays_on_the_node(tmp_path, monkeypatch):
    gh = GitHub()
    mgr, env, _ = setup(tmp_path, gh)
    monkeypatch.setattr(prproofs, "FILE_MAX", 100)  # the MP4 is larger
    monkeypatch.setattr(prproofs, "INLINE_MAX", 40)  # and the PNG too large to show inline
    got = await pulls.open_pull_request(mgr, env, title="Fix")
    assert f"{ENV}/pvideo00001.mp4" not in gh.files() and f"{ENV}/pvideo00001.gif" in gh.files()
    body = gh.posted["body"]
    assert "Watch the recording" not in body and f"[The screenshot (0.0 MB)]({link('pshot000001.png')})" in body
    assert got["proofs"]["in_pull_request"] == 2 and got["proofs"]["too_large"] == ["pvideo00001.mp4"]
    assert "stayed on the node" in got["proofs"]["why"]


async def test_a_refused_upload_still_opens_the_pull_request(tmp_path):
    gh = GitHub(refuse_blobs=True)
    mgr, env, _ = setup(tmp_path, gh, blocked=[{"host": "example.com", "count": 2}])
    got = await pulls.open_pull_request(mgr, env, title="Fix", body="Text.")
    assert got["created"] and got["proofs"]["in_pull_request"] == 0
    assert "403" in got["proofs"]["why"] and "stayed on the node" in got["proofs"]["why"]
    body = gh.posted["body"]
    assert prproofs.PROOF_BEGIN not in body and prproofs.NET_BEGIN in body and "`example.com` (2x)" in body


async def test_no_token_says_the_proofs_stayed_on_the_node(tmp_path):
    gh = GitHub()
    mgr, env, _ = setup(tmp_path, gh, token=False)
    with pytest.raises(pulls.PullRequestFailed) as e:
        await pulls.open_pull_request(mgr, env, title="Fix")
    assert "openajar setup github" in e.value.message and "stayed on the node" in e.value.message
    assert gh.calls == []


async def test_a_branch_that_moved_meanwhile_is_tried_once_more(tmp_path):
    gh = GitHub()
    mgr, env, _ = setup(tmp_path, gh)
    await pulls.open_pull_request(mgr, env, title="Fix", proofs=["pshot000001"])
    real = gh.handler
    raced = []

    def handler(request):
        if request.method == "PATCH" and "/git/refs/" in request.url.path and not raced:
            raced.append(1)
            return httpx.Response(422, json={"message": "Update is not a fast forward"})
        return real(request)

    mgr.github_transport = httpx.MockTransport(handler)
    got = await pulls.open_pull_request(mgr, env, title="Fix")
    assert raced and got["proofs"]["in_pull_request"] == 2 and f"{ENV}/pvideo00001.mp4" in gh.files()


def test_sections_are_replaced_between_their_markers_only():
    b, e = prproofs.PROOF_BEGIN, prproofs.PROOF_END
    assert prproofs.set_section("", b, e, "### Proof") == f"{b}\n### Proof\n{e}"
    assert prproofs.set_section("Mine.", b, e, "one") == f"Mine.\n\n{b}\none\n{e}"
    body = f"Mine.\n\n{b}\nold\n{e}\n\nA reviewer's note."
    assert prproofs.set_section(body, b, e, "new") == f"Mine.\n\n{b}\nnew\n{e}\n\nA reviewer's note."
    assert prproofs.set_section(body, b, e, "") == "Mine.\n\nA reviewer's note."
    assert prproofs.set_section("Mine.", b, e, "") == "Mine."
    assert prproofs.md("a *b* <!-- c -->") == "a \\*b\\* \\<\\!-- c -\\>"  # no comment can end a section
