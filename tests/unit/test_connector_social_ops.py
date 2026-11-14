"""The social connector operations (connectors/reddit and connectors/linkedin): they compile
strictly, their commit requests are the ones the verified egress rules know, they stay unverified
until a canary, and they run end to end on fake pages built from the notes of a canary run."""

from __future__ import annotations

import json
import re
import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agentos.core.models import RunStatus, Signal
from agentos.node.egress import EFFECT_RULES, classify, request_op
from agentos.runner.connectors.compile import ConnectorInvalid, compile_connector, compile_path
from agentos.runner.connectors.marks import MarkLedger
from agentos.runner.connectors.models import ActingAs, Operation
from agentos.runner.connectors.service import ConnectorService
from agentos.runner.connectors.verify import bump_patch, mark_installed, mark_verified
from agentos.runner.fakes import OWNER, El, FakeClock, FakeLanes, FakeNotifier, FakeSite, Harness

# (flow id, the verified egress rule its commit request matches): every commit operation of the two connectors
OPS = (
    ("connector.reddit.post_text", "reddit.post"), ("connector.reddit.comment", "reddit.comment"),
    ("connector.reddit.join", "reddit.join"), ("connector.linkedin.post", "linkedin.post"),
    ("connector.linkedin.comment", "linkedin.comment"), ("connector.linkedin.react", "linkedin.like"),
    ("connector.linkedin.follow", "linkedin.follow"),
)
REPO = Path(__file__).resolve().parents[2]
REDDIT = REPO / "connectors" / "reddit"
LINKEDIN = REPO / "connectors" / "linkedin"
RSC = "https://www.linkedin.com/flagship-web/rsc-action/actions/server-request"
GQL = "https://www.reddit.com/svc/shreddit/graphql"
JSON_CT = {"Content-Type": "application/json"}
FORM_CT = {"Content-Type": "application/x-www-form-urlencoded"}


def _li(request_id: str) -> bytes:
    return json.dumps({"requestId": request_id, "serverRequest": {"requestId": request_id}}).encode()


def _gql(operation: str) -> bytes:
    return json.dumps({"operation": operation, "variables": {"input": {}}}).encode()


# what the canary recorded on 2026-11-04 for each kind, and the
# neighbouring requests on the same URL that must never be taken for it
RECORDED = {
    ("reddit", "comment"): ("https://www.reddit.com/svc/shreddit/t3_1demo02/create-comment", FORM_CT,
                            b"content=x&mode=richText&csrf_token=t", []),
    ("reddit", "join"): (GQL, JSON_CT, _gql("UpdateSubredditSubscriptions"),
                         [_gql("UpdatePostVoteState"), _gql("CreatePost"), _gql("EvaluateCommentAutomationsByPostId")]),
    ("reddit", "post_text"): (GQL, JSON_CT, _gql("CreatePost"), []),
    ("linkedin", "post"): (RSC, JSON_CT, _li("com.linkedin.sdui.requests.sharing.CreatePost"),
                           [_li("com.linkedin.sdui.comments.createComment"), _li("com.linkedin.sdui.reactions.create")]),
    ("linkedin", "comment"): (RSC, JSON_CT, _li("com.linkedin.sdui.comments.createComment"),
                              [_li("com.linkedin.sdui.comments.fetchCommentPrompts"),
                               _li("com.linkedin.sdui.reactions.create")]),
    ("linkedin", "react"): (RSC, JSON_CT, _li("com.linkedin.sdui.reactions.create"),
                            [_li("com.linkedin.sdui.reactions.delete"), _li("com.linkedin.sdui.hovercard.fetchHovercard"),
                             _li("com.linkedin.sdui.comments.createComment")]),
    ("linkedin", "follow"): (RSC, JSON_CT, _li("com.linkedin.sdui.requests.mynetwork.addaUpdateFollowState"),
                             [_li("com.linkedin.sdui.requests.mynetwork.sendInvitation"),
                              _li("com.linkedin.sdui.hovercard.fetchHovercard")]),
}


# ---------------------------------------------------------------- compile and lint


@pytest.mark.parametrize("root", [REDDIT, LINKEDIN], ids=["reddit", "linkedin"])
def test_the_connectors_compile_strictly_with_fixtures_for_every_operation(root):
    cc = compile_path(root, commit_gate=True)
    assert not [i for i in cc.issues if i.level == "error"]
    assert all(op.installable for op in cc.operations.values())
    for name in cc.operations:
        fixture = root / "fixtures" / name / "en.html"
        assert fixture.exists()
        if name != "post_text":                       # the hand-written ones say so first
            assert fixture.read_text().startswith("<!-- SYNTHESIZED, NOT CAPTURED.")
    assert {n for n, op in cc.operations.items() if op.effect == "read"} == (
        {"browse_subreddit"} if root == REDDIT else {"feed"})


def test_no_operation_carries_a_live_mark_and_post_texts_old_canary_is_stale():
    """reddit.post_text's 2026-11-04 canary ran 0.1.0; its public check and author check came later,
    so the mark it carries names no implementation: it compiles stale, with no verified text."""
    for root in (REDDIT, LINKEDIN):
        cc = compile_path(root)
        for name, op in cc.connector.operations.items():
            compiled = cc.operations[name]
            assert compiled.definition["connector"]["verified"] == ""
            assert compiled.proof.startswith("sha256:") and len(compiled.proof) == 71
            if (cc.key, name) == ("reddit", "post_text"):
                assert op.verified.startswith("2026-11-04 reddit.com canary:") and op.verified_hash == ""
                assert compiled.mark == "stale"
            else:
                assert op.verified == "" and compiled.mark == "" and op.description.startswith("Not verified yet")


def test_every_listed_operation_is_a_commit_of_a_connector_in_the_repo():
    ops = {f"connector.{cc.key}.{n}": cc for cc in (compile_path(REDDIT), compile_path(LINKEDIN))
           for n in cc.operations}
    for flow_id, _rule in OPS:
        assert flow_id in ops, flow_id
        assert ops[flow_id].operations[flow_id.rsplit(".", 1)[1]].effect == "commit"


# ---------------------------------------------------------------- commit requests


@pytest.mark.parametrize(("flow_id", "rule_id"), OPS, ids=[f for f, _ in OPS])
def test_the_commit_request_is_the_one_its_verified_rule_knows(flow_id, rule_id):
    site, name = flow_id.split(".")[1:]
    cc = compile_path(REDDIT if site == "reddit" else LINKEDIN)
    req = cc.connector.operations[name].commit.request
    url, headers, body, neighbours = RECORDED[(site, name)]
    rules = {r.id: r for r in EFFECT_RULES}
    assert rules[rule_id].verified
    rule = classify(url, "POST", headers, body=body)
    assert rule is not None and rule.id == rule_id
    assert req.method == "POST" and re.match(req.url, url)
    if req.op is not None:
        assert re.match(req.op, request_op(headers, body) or "")
    for other in neighbours:
        name_other = request_op(headers, other) or ""
        assert req.op is not None and not re.match(req.op, name_other), name_other


def test_a_like_never_matches_its_undo_though_the_rule_takes_both():
    req = compile_path(LINKEDIN).connector.operations["react"].commit.request
    delete = request_op(JSON_CT, _li("com.linkedin.sdui.reactions.delete"))
    assert classify(RSC, "POST", JSON_CT, body=_li("com.linkedin.sdui.reactions.delete")).id == "linkedin.like"
    assert not re.match(req.op, delete)


def test_a_request_op_is_anchored_and_does_not_change_an_old_contract():
    with pytest.raises(ValidationError, match="anchored"):
        Operation.model_validate({**_op_doc(), "commit": {"step": "s", "request": {
            "method": "POST", "url": "^https://x\\.example/$", "op": "CreatePost"}}})
    # post_text has no op: its contract hash is the one installed nodes and approvals know
    assert compile_path(REDDIT).operations["post_text"].contract == (
        "sha256:2d600c698196636ef216f84016fb38dfca8b7f38c121e333d3a6cb9375c79f7f")


# ---------------------------------------------------------------- the verified mark


def _op_doc() -> dict:
    return {"title": "t", "effect": "read", "acting_as": {"kind": "account"},
            "steps": [{"type": "navigate", "url": "https://x.example/"}]}


def test_the_verified_mark_starts_with_a_date_and_where():
    assert Operation.model_validate({**_op_doc(), "verified": " 2026-11-06 reddit.com canary: ok "}).verified == (
        "2026-11-06 reddit.com canary: ok")
    assert Operation.model_validate(_op_doc()).verified == ""
    with pytest.raises(ValidationError, match="date"):
        Operation.model_validate({**_op_doc(), "verified": "yes"})


def test_marking_verified_records_the_proof_bumps_the_version_and_can_be_revoked(tmp_path):
    d = tmp_path / "reddit"
    shutil.copytree(REDDIT, d)
    ledger = MarkLedger(tmp_path / "home")
    before = compile_path(d)
    doc, version, proof = mark_verified(d, "comment", note="u/Throwaway, comment 1abc read back on its permalink",
                                        day=date(2026, 11, 6), ledger=ledger)
    assert version == "0.3.1" and json.loads((d / "connector.json").read_text()) == doc
    assert list(doc["operations"]["comment"])[:4] == ["title", "description", "verified", "verified_hash"]
    after = compile_path(d)
    op = after.connector.operations["comment"]
    assert op.verified == "2026-11-06 reddit.com canary: u/Throwaway, comment 1abc read back on its permalink"
    assert op.verified_hash == proof == after.operations["comment"].proof == before.operations["comment"].proof
    assert after.operations["comment"].mark == "verified"
    assert after.operations["comment"].definition["connector"]["verified"] == op.verified
    assert after.operations["comment"].contract == before.operations["comment"].contract
    assert ledger.get("reddit.comment")["proof"] == proof
    state = ledger.state("connector.reddit.comment", after.operations["comment"].definition)
    assert state is not None and not state.stale and state.text == op.verified
    with pytest.raises(ConnectorInvalid, match="note"):
        mark_verified(d, "join", note="  ")
    with pytest.raises(ConnectorInvalid, match="no operation"):
        mark_verified(d, "vote", note="x")
    doc, version, _ = mark_verified(d, "comment", revoke=True, ledger=ledger)
    assert version == "0.3.2" and "verified" not in doc["operations"]["comment"]
    assert ledger.get("reddit.comment") is None
    with pytest.raises(ConnectorInvalid, match="not marked"):
        mark_verified(d, "comment", revoke=True, ledger=ledger)


def _edit(d: Path, fn) -> None:
    doc = json.loads((d / "connector.json").read_text())
    fn(doc["operations"]["comment"])
    (d / "connector.json").write_text(json.dumps(doc, indent=2) + "\n")


@pytest.mark.parametrize("edit", [
    lambda op: op["steps"][-2]["locator"].update(nth=1),                                   # a locator
    lambda op: op["steps"][-1]["condition"].update(matches="^https://www\\.reddit\\.com/"),  # a readback
    lambda op: op["commit"]["preflight"].pop(),                                              # a preflight
    lambda op: op["commit"]["request"].update(text="form:other"),                            # the commit request
    lambda op: op["pace"].update(per_day=3),                                                 # pace
], ids=["locator", "readback", "preflight", "request", "pace"])
def test_a_mark_goes_stale_when_what_it_certified_changes(tmp_path, edit):
    d = tmp_path / "reddit"
    shutil.copytree(REDDIT, d)
    ledger = MarkLedger(tmp_path / "home")
    mark_verified(d, "comment", note="u/Throwaway, comment read back", day=date(2026, 11, 6), ledger=ledger)
    _edit(d, lambda op: op.update(description="Reworded."))   # words and a version bump keep the mark
    doc = json.loads((d / "connector.json").read_text())
    doc["version"] = "0.3.9"
    (d / "connector.json").write_text(json.dumps(doc, indent=2) + "\n")
    kept = compile_path(d).operations["comment"]
    assert kept.mark == "verified" and not ledger.state("reddit.comment", kept.definition).stale
    _edit(d, edit)
    cc = compile_path(d).operations["comment"]
    assert cc.mark == "stale" and cc.definition["connector"]["verified"] == ""
    state = ledger.state("reddit.comment", cc.definition)
    assert state is not None and state.stale and str(state) == "" and state.reason == "verified_stale"


def test_a_mark_in_a_file_or_bundle_is_never_honoured_only_the_nodes_ledger(tmp_path):
    """Provenance: a connector whose file says verified (with the right hash, as a bundle could)
    is still unmarked on a node whose own ledger has no mark for it."""
    d = tmp_path / "reddit"
    shutil.copytree(REDDIT, d)
    mark_verified(d, "comment", note="made up somewhere else", day=date(2026, 11, 6))   # no ledger
    h = Harness(tmp_path / "node")
    ConnectorService(h.flows).install(compile_path(d), by=OWNER)
    home = tmp_path / "node-home"
    fid = "connector.reddit.comment"
    fv = h.flows.store.get_flow(fid)
    assert MarkLedger(home).state(fid, fv.definition.canonical()) is None
    mark_installed(MarkLedger(home), "reddit.comment", fv.definition.canonical(), domain="reddit.com",
                   note="test/reddit: comment read back", day=date(2026, 11, 6))
    state = MarkLedger(home).state(fid, fv.definition.canonical())
    assert state is not None and not state.stale and str(state).startswith("2026-11-06")
    with pytest.raises(ConnectorInvalid, match="not installed"):
        mark_installed(MarkLedger(home), "reddit.join", None, domain="reddit.com", note="x")
    mark_installed(MarkLedger(home), "reddit.comment", None, domain="reddit.com", revoke=True)
    assert MarkLedger(home).state(fid, fv.definition.canonical()) is None


def test_the_ledger_is_the_owners_and_unreadable_means_no_marks(tmp_path):
    ledger = MarkLedger(tmp_path)
    ledger.put("connector.reddit.comment", proof="sha256:" + "0" * 64, text="2026-11-06 reddit.com canary: x")
    assert ledger.path.stat().st_mode & 0o777 == 0o600
    assert set(ledger.all()) == {"reddit.comment"}
    ledger.path.write_text("{not json")
    assert ledger.all() == {} and ledger.state("reddit.comment", {"id": "x"}) is None
    with pytest.raises(ConnectorInvalid, match="site.op"):
        ledger.get("reddit")


def test_the_cli_marks_an_operation_from_a_directory_into_the_ledger(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentos.runner.connectors.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path / "home"))
    d = tmp_path / "linkedin"
    shutil.copytree(LINKEDIN, d)
    out = CliRunner().invoke(app, ["verify", "linkedin.react", "--dir", str(d), "--date", "2026-11-06",
                                   "--note", "test/linkedin liked a post, seen on the reactions activity",
                                   "--no-install"])
    assert out.exit_code == 0, out.output
    assert "linkedin.react: 2026-11-06 linkedin.com canary: test/linkedin liked" in out.output
    assert "linkedin 0.3.1" in out.output
    assert compile_path(d).operations["react"].mark == "verified"
    assert MarkLedger(tmp_path / "home").get("linkedin.react")["proof"] == compile_path(d).operations["react"].proof
    bad = CliRunner().invoke(app, ["verify", "linkedin", "--dir", str(d), "--note", "x", "--no-install"])
    assert bad.exit_code == 1 and "site.op" in bad.output


def test_the_cli_never_marks_the_connector_in_the_working_directory(tmp_path, monkeypatch):
    """Without --dir it marks the copy installed on this node; connectors/ under the shell's
    directory is never picked up."""
    from typer.testing import CliRunner

    from agentos.runner.connectors.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    shutil.copytree(LINKEDIN, work / "connectors" / "linkedin")
    monkeypatch.chdir(work)
    before = (work / "connectors" / "linkedin" / "connector.json").read_text()
    out = CliRunner().invoke(app, ["verify", "linkedin.react", "--note", "x"])
    assert out.exit_code == 1 and "not installed on this node" in out.output
    assert (work / "connectors" / "linkedin" / "connector.json").read_text() == before
    assert MarkLedger(tmp_path / "home").all() == {}


def test_a_version_that_is_not_semver_is_a_clean_error(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentos.runner.connectors.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path / "home"))
    d = tmp_path / "reddit"
    shutil.copytree(REDDIT, d)
    doc = json.loads((d / "connector.json").read_text())
    doc["version"] = "0.3.x"
    (d / "connector.json").write_text(json.dumps(doc))
    with pytest.raises(ConnectorInvalid, match="MAJOR.MINOR.PATCH"):
        bump_patch("0.3.x")
    out = CliRunner().invoke(app, ["verify", "reddit.comment", "--dir", str(d), "--note", "x", "--no-install"])
    assert out.exit_code == 1 and "MAJOR.MINOR.PATCH" in out.output
    assert isinstance(out.exception, SystemExit)          # a clean exit, not a traceback


# ---------------------------------------------------------------- acting as without an identity cookie


def test_an_account_can_be_read_from_the_url_on_a_site_without_an_identity_cookie():
    a = ActingAs.model_validate({"kind": "account", "source": "url", "pattern": "^https://x/in/([a-z]+)/"})
    assert a.source == "url"
    with pytest.raises(ValidationError, match="identity cookie"):
        ActingAs.model_validate({"kind": "account", "source": "url"})
    with pytest.raises(ValidationError, match="identity cookie"):
        ActingAs.model_validate({"kind": "account", "pattern": "^(x)$"})
    li = compile_path(LINKEDIN).connector.operations["react"]
    assert li.acting_as.kind == "account" and li.acting_as.source == "url"


# ---------------------------------------------------------------- end to end on fake pages


RD_LANE = "test/reddit"
LI_LANE = "test/linkedin"
MAIN_HOST = "https://www.reddit.com"


def _harness(tmp_path, site: FakeSite, root: Path, lane: str, acts_as: dict) -> Harness:
    h = Harness(tmp_path, site=site, services={"lanes": FakeLanes([lane]), "notifier": FakeNotifier()})
    h.driver.after_hooks.append(lambda op, target, d: d._render() if op in ("type", "fill") else None)
    ConnectorService(h.flows).install(compile_path(root), by=OWNER)
    h.runner.surfaces.put({"id": lane, "site": root.name, "lane": lane, "acts_as": acts_as,
                           "account_language": "en"}, by=OWNER)
    return h


def reddit_site(*, sends: bool = True, older: list[dict] | None = None) -> FakeSite:
    """Reddit as the canary saw it: a post with its comment form, the account's comments page
    linking to each comment (newest first), a subreddit header with Join, and the New list's post
    elements. `sends` False: the Comment button clears the box but nothing is posted."""
    site = FakeSite()
    site.identity = {"site": "reddit", "user_id": "t2_examplecat0", "source": "cookie reddit_session"}
    site.state = {"comments": list(older or []), "joined": False}

    def post(s, d):
        def send(s, el, d):
            text = d.values.pop("comment", "")
            if sends:
                s.state["comments"].insert(0, {"post": "1demo02", "id": f"19s4nze6wq4y{len(s.state['comments'])}",
                                               "text": text})
        return El("document", "post").add(El("main").add(
            El("article", "A post", attrs={"id": "t3_1demo02"}),
            El("form").add(El("textbox", "Join the conversation", key="comment"),
                           El("button", "Cancel"), El("button", "Comment", on_click=send))))

    def mine(s, d):
        main = El("main")
        for c in s.state["comments"]:
            link = f"/r/test/comments/{c['post']}/comment/{c['id']}/"
            main.add(El("link", c["text"], text=c["text"], attrs={"href": link},
                        on_click=lambda s, el, d, link=link: MAIN_HOST + link))
        return El("document", "comments").add(main)

    def sub(s, d):
        def join(s, el, d):
            s.state["joined"] = True
        main = El("main").add(El("heading", "r/test"),
                              El("button", "Joined" if s.state["joined"] else "Join", on_click=join))
        return El("document", "r/test").add(main)

    def new(s, d):
        main = El("main")
        for pid, author in (("1demo01", "MiraKestrelDemo"), ("1xnnwk1", "someone")):
            main.add(El("article", f"post {pid}", attrs={"id": f"t3_{pid}", "author": author,
                                                         "permalink": f"/r/test/comments/{pid}/a_title/"}))
        return El("document", "new").add(main)

    site.route(f"{MAIN_HOST}/r/test/comments/1demo02/", post)
    site.route(f"{MAIN_HOST}/user/me/comments/", mine)
    site.route(f"{MAIN_HOST}/r/test/comments/1demo02/comment/*", lambda s, d: El("document", "comment").add(El("main")))
    site.route(f"{MAIN_HOST}/r/test/", sub)
    site.route(f"{MAIN_HOST}/r/test/new/", new)
    return site


RD_AS = {"kind": "account", "user_id": "t2_examplecat0", "name": "MiraKestrelDemo"}
COMMENT = {"subreddit": "test", "post_id": "1demo02", "text": "Nice one"}


async def test_reddit_comment_runs_and_reads_the_comment_back_from_the_accounts_own_list(tmp_path):
    older = [{"post": "1demo02", "id": "0ld0000000001", "text": "An earlier one"}]
    h = _harness(tmp_path, reddit_site(older=older), REDDIT, RD_LANE, RD_AS)
    run = await h.run("connector.reddit.comment", COMMENT, lane=RD_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert h.site.state["comments"][0] == {"post": "1demo02", "id": "19s4nze6wq4y1", "text": "Nice one"}
    assert run.output["data"] == {
        "comment_url": "https://www.reddit.com/r/test/comments/1demo02/comment/19s4nze6wq4y1/"}


async def test_reddit_comment_never_takes_an_older_comment_with_the_same_words_for_proof(tmp_path):
    """The comment did not go out, and the account commented the same words on that post before:
    the newest comment on the post is the one noted before the run, so nothing proves it."""
    older = [{"post": "1demo02", "id": "0ld0000000001", "text": "Nice one"}]
    h = _harness(tmp_path, reddit_site(sends=False, older=older), REDDIT, RD_LANE, RD_AS)
    run = await h.run("connector.reddit.comment", COMMENT, lane=RD_LANE)
    assert run.status == RunStatus.PARTIAL and run.error["code"] == "readback_failed"
    assert "differs from the one saved as before" in run.error["message"]
    assert run.output is None


async def test_reddit_comment_is_not_proved_by_a_new_comment_with_other_words(tmp_path):
    site = reddit_site()
    h = _harness(tmp_path, site, REDDIT, RD_LANE, RD_AS)
    h.driver.after_hooks.append(lambda op, target, d: site.state["comments"][0].update(text="Nice one!")
                                if op == "click" and site.state["comments"] else None)
    run = await h.run("connector.reddit.comment", COMMENT, lane=RD_LANE)
    assert run.status == RunStatus.PARTIAL and run.error["code"] == "readback_failed"
    assert "text equals" in run.error["message"]


async def test_reddit_join_runs_and_refuses_a_subreddit_already_joined(tmp_path):
    h = _harness(tmp_path, reddit_site(), REDDIT, RD_LANE, RD_AS)
    run = await h.run("connector.reddit.join", {"subreddit": "test"}, lane=RD_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert h.site.state["joined"] and run.output["data"] == {"subreddit_url": "https://www.reddit.com/r/test/"}
    h.clock.advance(2 * 3600)
    again = await h.run("connector.reddit.join", {"subreddit": "test"}, lane=RD_LANE)
    assert again.status == RunStatus.FAILED and again.error["code"] == "preflight_mismatch"   # never leaves


async def test_reddit_browse_returns_links_ids_and_authors_only(tmp_path):
    h = _harness(tmp_path, reddit_site(), REDDIT, RD_LANE, RD_AS)
    run = await h.run("connector.reddit.browse_subreddit", {"subreddit": "test"}, lane=RD_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert run.output["data"] == {"posts": [
        {"url": "https://www.reddit.com/r/test/comments/1demo01/a_title/", "id": "t3_1demo01", "author": "MiraKestrelDemo"},
        {"url": "https://www.reddit.com/r/test/comments/1xnnwk1/a_title/", "id": "t3_1xnnwk1", "author": "someone"}]}
    assert run.output["untrusted_fields"] == ["posts[].author"]


LI = "https://www.linkedin.com"
URN = "urn:li:activity:7392000000000000001"
ME_URL = f"{LI}/in/arlo-fenwick-demo/"


def linkedin_site(*, reaction: str | None = None, audience: str = "Anyone", publishes: bool = True,
                  posts: list[tuple[str, str]] | None = None, top_card: str = "Follow") -> FakeSite:
    """LinkedIn as the canary saw it: /in/me/ leads to the member's own profile; a post page whose
    container (role article, data-urn) holds its reaction toggle, its comment form and nested
    comments with their own Like; the activity pages; a company page whose top card has Follow, a
    suggestion list with its own Follow buttons, and a profile whose top card offers only Connect."""
    site = FakeSite()
    site.identity = None                                   # no identity cookie on LinkedIn
    site.state = {"reaction": reaction, "comment_likes": 0, "comments": [], "following": False,
                  "suggested": 0, "connected": 0, "composing": False, "posts": list(posts or []), "n": 0}

    def post(s, d):
        def like(s, el, d):
            s.state["reaction"] = "Like"

        def like_comment(s, el, d):
            s.state["comment_likes"] += 1

        def send(s, el, d):
            s.state["comments"].insert(0, (URN, d.values.pop("comment", "")))
        r = s.state["reaction"]
        toggle = El("button", f"Unreact {r}" if r else "React Like", attrs={"aria-pressed": "true" if r else "false"},
                    on_click=None if r else like)
        return El("document", "post").add(El("main").add(
            El("article", "update", attrs={"data-urn": URN}).add(
                El("link", "Mira Kestrel", attrs={"href": f"{LI}/in/mira-kestrel/"}), toggle,
                El("form").add(El("textbox", "Add a comment", key="comment"), El("button", "Comment", on_click=send)),
                El("article", "comment").add(
                    El("button", "React Like to Someone's comment", attrs={"aria-pressed": "false"},
                       on_click=like_comment)))))

    def reactions(s, d):
        main = El("main")
        if s.state["reaction"]:
            main.add(El("article", "update", attrs={"data-urn": URN}))
        return El("document", "reactions").add(main)

    def comments(s, d):
        main = El("main")
        for urn, text in s.state["comments"]:
            main.add(El("generic", "entry", attrs={"data-urn": urn}).add(El("article", "comment").add(
                El("generic", "", text=text, css=(".comments-comment-item__main-content",)))))
        return El("document", "comments").add(main)

    def activity(s, d):
        main = El("main")
        for urn, text in s.state["posts"]:
            main.add(El("generic", "update", attrs={"data-urn": urn}).add(
                El("generic", "", text=text, css=(".update-components-text",))))
        return El("document", "activity").add(main)

    def feed(s, d):
        main = El("main")
        if d.url == f"{LI}/feed/":
            def start(s, el, d):
                s.state["composing"] = True
            main.add(El("button", "Start a post", on_click=start), El("button", "Photo"))
        for n, author in ((1, f"{LI}/in/mira-kestrel/"), (2, f"{LI}/company/example-robotics/"),
                          (3, f"{LI}/in/arlo-fenwick-demo/")):
            main.add(El("article", "update", attrs={"data-urn": f"urn:li:activity:739200000000000000{n}"}).add(
                El("link", "author", attrs={"href": author}), El("link", "a commenter", attrs={"href": f"{LI}/in/x/"})))
        doc = El("document", "feed").add(main)
        if s.state["composing"]:
            def publish(s, el, d):
                s.state["composing"] = False
                if publishes:
                    s.state["n"] += 1
                    s.state["posts"].insert(0, (f"urn:li:activity:73930000000000000{s.state['n']:02d}",
                                                d.values.pop("post", "")))
            doc.add(El("dialog", "Create post modal").add(
                El("button", f"Arlo Fenwick, Post to {audience}"),
                El("textbox", "Text editor for creating content", key="post"),
                El("button", "Post", on_click=publish)))
        return doc

    def company(s, d):
        def follow(s, el, d):
            s.state["following"] = True

        def suggested(s, el, d):
            s.state["suggested"] += 1
        top = El("generic", "top card", css=(".org-top-card",)).add(
            El("heading", "Example Robotics"),
            El("button", "Following" if s.state["following"] else "Follow Example Robotics", on_click=follow))
        return El("document", "company").add(El("main").add(top, El("generic", "Pages people also viewed").add(
            El("button", "Follow Other Company", on_click=suggested))))

    def profile(s, d):
        def connect(s, el, d):
            s.state["connected"] += 1

        def suggested(s, el, d):
            s.state["suggested"] += 1
        top = El("generic", "top card", css=(".pv-top-card",)).add(
            El("heading", "Mira Kestrel"), El("button", top_card, on_click=connect), El("button", "More"))
        return El("document", "profile").add(El("main").add(top, El("generic", "People you may know").add(
            El("button", "Follow Someone Else", on_click=suggested))))

    site.route(ME_URL, lambda s, d: El("document", "me").add(El("main")))
    site.route(f"{LI}/feed/update/{URN.replace(':', '%3A')}/", post)   # params in a URL are encoded
    site.route(f"{ME_URL}recent-activity/reactions/", reactions)
    site.route(f"{ME_URL}recent-activity/comments/", comments)
    site.route(f"{ME_URL}recent-activity/all/", activity)
    site.route(f"{LI}/company/example-robotics/", company)
    site.route(f"{LI}/in/mira-kestrel/", profile)
    site.route(f"{LI}/feed/", feed)
    return site


def _me_redirects(d, *, after: int = 0) -> None:
    """/in/me/... answers with the member's own profile URL, as LinkedIn redirects it; with `after`,
    the first time the URL still reads /in/me/ for that many reads of it (the redirect has not
    resolved yet)."""
    pending = {"reads": 0, "first": True}
    real = d.current_url

    def resolve() -> None:
        d.url = d.url.replace("/in/me/", "/in/arlo-fenwick-demo/")
        d._render()

    def hook(op, target, d):
        if op == "navigate" and "/in/me/" in d.url:
            if after and pending["first"]:
                pending["reads"], pending["first"] = after, False
            else:
                resolve()

    async def current_url():
        if "/in/me/" in d.url:
            if pending["reads"] <= 0:
                resolve()
            pending["reads"] -= 1
        return await real()
    d.after_hooks.append(hook)
    d.current_url = current_url


LI_AS = {"kind": "account", "user_id": "arlo-fenwick-demo", "name": "Arlo Fenwick"}


async def _li_harness(tmp_path, **site_kw) -> Harness:
    h = _harness(tmp_path, linkedin_site(**site_kw), LINKEDIN, LI_LANE, LI_AS)
    _me_redirects(h.driver)
    return h


async def test_linkedin_react_likes_the_post_itself_and_reads_its_own_state_back(tmp_path):
    h = await _li_harness(tmp_path)
    run = await h.run("connector.linkedin.react", {"post_urn": URN}, lane=LI_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert h.site.state["reaction"] == "Like" and h.site.state["comment_likes"] == 0
    assert run.output["data"]["proof_url"].endswith("/in/arlo-fenwick-demo/recent-activity/reactions/")
    checks = [rb["check_text"] for rb in run.proof["readbacks"]]
    assert any('aria-pressed="true"' in c and "data-urn" in c for c in checks), checks   # the post's own state
    h.clock.advance(600)
    again = await h.run("connector.linkedin.react", {"post_urn": URN}, lane=LI_LANE, idempotency_key="again")
    assert again.status == RunStatus.FAILED and again.error["code"] == "preflight_mismatch"   # never an unlike


@pytest.mark.parametrize("reaction", ["Celebrate", "Love", "Like"])
async def test_linkedin_react_refuses_a_post_already_reacted_and_never_likes_a_comment(tmp_path, reaction):
    """A post reacted with Celebrate has no pressed Like; the first Like in main is a comment's.
    The preflight sees the post's own reaction and refuses; no comment is liked."""
    h = await _li_harness(tmp_path, reaction=reaction)
    run = await h.run("connector.linkedin.react", {"post_urn": URN}, lane=LI_LANE)
    assert run.status == RunStatus.FAILED and run.error["code"] == "preflight_mismatch"
    assert h.site.state["reaction"] == reaction and h.site.state["comment_likes"] == 0


async def test_linkedin_comment_reads_back_the_members_own_comment_on_that_post(tmp_path):
    h = await _li_harness(tmp_path)
    run = await h.run("connector.linkedin.comment", {"post_urn": URN, "text": "Impressive range."}, lane=LI_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert h.site.state["comments"] == [(URN, "Impressive range.")]


async def test_linkedin_comment_is_not_proved_by_the_words_somewhere_else_on_the_page(tmp_path):
    """The comment went out with other words (or not at all), while the text sits in an entry for
    another post and inside a longer comment: only the newest entry for this post, exactly, proves it."""
    site = linkedin_site()
    site.state["comments"] = [("urn:li:activity:7392000000000000009", "Impressive range.")]
    h = _harness(tmp_path, site, LINKEDIN, LI_LANE, LI_AS)
    _me_redirects(h.driver)
    h.driver.after_hooks.append(lambda op, target, d: site.state["comments"].__setitem__(
        0, (URN, "Impressive range. Truly.")) if op == "click" and site.state["comments"][0][0] == URN else None)
    run = await h.run("connector.linkedin.comment", {"post_urn": URN, "text": "Impressive range."}, lane=LI_LANE)
    assert run.status == RunStatus.PARTIAL and run.error["code"] == "readback_failed"


TEXT = "Shipping the rover's new arm today."


async def test_linkedin_post_proves_a_new_update_with_exactly_the_text(tmp_path):
    h = await _li_harness(tmp_path, posts=[("urn:li:activity:7390000000000000001", "An older post")])
    run = await h.run("connector.linkedin.post", {"text": TEXT}, lane=LI_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert run.output["data"] == {"post_urn": "urn:li:activity:7393000000000000001"}


async def test_linkedin_post_with_no_earlier_update_is_proved_too(tmp_path):
    h = await _li_harness(tmp_path)
    run = await h.run("connector.linkedin.post", {"text": TEXT}, lane=LI_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error


async def test_linkedin_post_is_not_proved_by_an_older_update_with_the_same_text(tmp_path):
    h = await _li_harness(tmp_path, publishes=False, posts=[("urn:li:activity:7390000000000000001", TEXT)])
    run = await h.run("connector.linkedin.post", {"text": TEXT}, lane=LI_LANE)
    assert run.status == RunStatus.PARTIAL and run.error["code"] == "readback_failed"
    assert run.output is None


async def test_linkedin_post_text_must_be_exactly_the_text_not_contain_it(tmp_path):
    site = linkedin_site()
    h = _harness(tmp_path, site, LINKEDIN, LI_LANE, LI_AS)
    _me_redirects(h.driver)
    h.driver.after_hooks.append(lambda op, target, d: site.state["posts"].__setitem__(
        0, (site.state["posts"][0][0], TEXT + " #hiring")) if op == "click" and site.state["posts"] else None)
    run = await h.run("connector.linkedin.post", {"text": TEXT}, lane=LI_LANE)
    assert run.status == RunStatus.PARTIAL and "text equals" in run.error["message"]


async def test_linkedin_post_refuses_before_posting_unless_the_audience_is_anyone(tmp_path):
    h = await _li_harness(tmp_path, audience="Connections only")
    run = await h.run("connector.linkedin.post", {"text": TEXT}, lane=LI_LANE)
    assert run.status == RunStatus.FAILED and run.error["code"] == "preflight_mismatch"
    assert h.site.state["posts"] == []


async def test_linkedin_follow_presses_the_top_cards_follow(tmp_path):
    h = await _li_harness(tmp_path)
    run = await h.run("connector.linkedin.follow", {"profile_kind": "company", "name": "example-robotics"},
                      lane=LI_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert run.output["data"] == {"proof_url": "https://www.linkedin.com/company/example-robotics/"}
    assert h.site.state["following"] and h.site.state["suggested"] == 0
    h.clock.advance(3600)
    again = await h.run("connector.linkedin.follow", {"profile_kind": "company", "name": "example-robotics"},
                        lane=LI_LANE, idempotency_key="again")
    assert again.status == RunStatus.FAILED and again.error["code"] == "preflight_mismatch"   # never an unfollow


async def test_linkedin_follow_refuses_a_profile_that_offers_only_connect(tmp_path):
    """The top card has Connect, not Follow; a suggestion list further down has Follow buttons.
    Nothing is pressed: never Connect, never a suggested person."""
    h = await _li_harness(tmp_path, top_card="Connect")
    run = await h.run("connector.linkedin.follow", {"profile_kind": "in", "name": "mira-kestrel"}, lane=LI_LANE)
    assert run.status == RunStatus.FAILED and run.error["code"] == "preflight_mismatch"
    assert h.site.state["connected"] == 0 and h.site.state["suggested"] == 0


async def test_linkedin_refuses_another_member(tmp_path):
    h = _harness(tmp_path, linkedin_site(), LINKEDIN, LI_LANE, {**LI_AS, "user_id": "someone-else"})
    _me_redirects(h.driver)
    run = await h.run("connector.linkedin.react", {"post_urn": URN}, lane=LI_LANE)
    assert run.status == RunStatus.FAILED and run.error["code"] == "wrong_account"
    assert not h.site.state["reaction"]


async def test_linkedin_waits_for_the_me_redirect_before_reading_who_it_acts_as(tmp_path):
    """The URL still reads /in/me/ for a while after the navigation: `me` is not an id yet, so
    it is never a wrong account; the run reads the id once the redirect resolved."""
    h = _harness(tmp_path, linkedin_site(), LINKEDIN, LI_LANE, LI_AS)
    _me_redirects(h.driver, after=3)
    run = await h.run("connector.linkedin.react", {"post_urn": URN}, lane=LI_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error


async def test_an_unresolved_me_is_unknown_never_a_wrong_account(tmp_path):
    h = _harness(tmp_path, linkedin_site(), LINKEDIN, LI_LANE, LI_AS)
    _me_redirects(h.driver, after=10_000)                       # it never resolves
    run = await h.run("connector.linkedin.react", {"post_urn": URN}, lane=LI_LANE)
    assert run.status == RunStatus.FAILED and run.error["code"] == "identity_unknown"
    assert not h.site.state["reaction"]


async def test_linkedin_feed_returns_urns_and_author_links(tmp_path):
    h = await _li_harness(tmp_path)
    run = await h.run("connector.linkedin.feed", {}, lane=LI_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    posts = run.output["data"]["posts"]
    assert [p["urn"] for p in posts] == [f"urn:li:activity:739200000000000000{n}" for n in (1, 2, 3)]
    assert posts[1]["author_url"] == "https://www.linkedin.com/company/example-robotics/"


def test_an_extract_field_may_pick_the_first_match_inside_its_item():
    doc = json.loads((LINKEDIN / "connector.json").read_text())
    compile_connector(doc)                            # nth inside an item is anchored by the item
    doc["operations"]["feed"]["steps"][-1]["items"].pop("within")
    doc["operations"]["feed"]["steps"][-1]["items"]["nth"] = 0
    with pytest.raises(ConnectorInvalid, match="nth without an anchor"):
        compile_connector(doc)


def post_text_site(author_id: str = "t2_examplecat0") -> tuple[FakeSite, str]:
    site = FakeSite()
    site.identity = {"site": "reddit", "user_id": "t2_examplecat0", "source": "cookie reddit_session"}
    site.state = {"posts": []}
    permalink = f"{MAIN_HOST}/r/test/comments/1xq0001/hello/"

    def submit(s, d):
        def post(s, el, d):
            s.state["posts"].append((d.values.get("title"), d.values.get("body")))
            return f"{MAIN_HOST}/r/test/"
        return El("document", "submit").add(El("main").add(
            El("group").add(El("textbox", "Title", key="title")),
            El("group").add(El("textbox", "Post body text field", key="body")),
            El("button", "Post", on_click=post)))

    def new(s, d):
        return El("document", "new").add(El("main").add(
            *[El("link", t, on_click=lambda s, el, d: permalink) for t, _ in s.state["posts"]]))

    site.route(f"{MAIN_HOST}/r/test/submit/?type=TEXT", submit)
    site.route(f"{MAIN_HOST}/r/test/", lambda s, d: El("document", "r/test").add(El("main")))
    site.route(f"{MAIN_HOST}/r/test/new/", new)
    site.route(permalink, lambda s, d: El("document", "post").add(El("main").add(
        El("generic", "", attrs={"author-id": author_id, "author": "MiraKestrelDemo"}, css=("shreddit-post",)).add(
            El("heading", "Post Title: Hello")))))
    return site, permalink


POST = {"subreddit": "test", "title": "Hello", "body": "World"}


async def test_reddit_post_text_returns_the_permalink_its_url_readback_saved(tmp_path):
    """0.1.1's readback saves the permalink with a url readback; the runner now saves such a
    readback's page URL (before, a verified post ended output_contract_violation)."""
    site, permalink = post_text_site()
    h = _harness(tmp_path, site, REDDIT, RD_LANE, RD_AS)
    run = await h.run("connector.reddit.post_text", POST, lane=RD_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, (run.error, run.output, run.proof)
    assert site.state["posts"] == [("Hello", "World")] and run.output["data"] == {"post_url": permalink}


async def test_reddit_post_text_is_not_proved_by_a_post_of_someone_else(tmp_path):
    """Another account's post with the same title is the newest on /new/: its author is not the account."""
    site, _ = post_text_site(author_id="t2_someoneelse")
    h = _harness(tmp_path, site, REDDIT, RD_LANE, RD_AS)
    run = await h.run("connector.reddit.post_text", POST, lane=RD_LANE)
    assert run.status == RunStatus.PARTIAL and run.error["code"] == "readback_failed"
    assert "author-id" in run.error["message"]


async def test_a_url_readback_that_could_not_be_checked_never_reads_as_proof(tmp_path):
    """A captcha lands on the permalink: the url readback cannot be checked (passed None). The run
    is not verified, and the page it saved is marked not found."""
    site, permalink = post_text_site()
    h = _harness(tmp_path, site, REDDIT, RD_LANE, RD_AS)

    def captcha(op, target, d):
        if op == "click" and d.url == permalink:
            site.signal = Signal.CAPTCHA
    h.driver.after_hooks.append(captcha)
    run = await h.run("connector.reddit.post_text", POST, lane=RD_LANE)
    assert run.status != RunStatus.SUCCEEDED_VERIFIED
    assert run.status in (RunStatus.SUCCEEDED_UNVERIFIED, RunStatus.PARTIAL), run.error
    saved = [rb for rb in run.proof["readbacks"] if rb.get("passed") is None]
    assert saved, run.proof


async def test_url_readback_saves_found_only_when_it_passed(tmp_path):
    from agentos.runner.executor import FlowExecutor

    calls = {}

    class Driver:
        async def current_url(self):
            return "https://www.reddit.com/r/test/comments/1xq0001/hello/"

        async def signal(self):
            return Signal.CAPTCHA

    from agentos.runner.models import ReadbackStep
    step = ReadbackStep.model_validate({"type": "readback", "save_as": "post", "timeout_ms": 0, "condition": {
        "kind": "url", "matches": "^https://www\\.reddit\\.com/r/"}})
    ex = FlowExecutor.__new__(FlowExecutor)
    ex.clock, ex.poll_s = FakeClock(), 0.0
    state = SimpleNamespace(driver=Driver(), scope={"vars": {}}, scrub=lambda x: x,
                            flow=SimpleNamespace(domain="reddit.com", connector={}))
    out = await ex._run_step(step, "0", state, None)
    calls.update(out["vars"])
    assert calls["post"]["found"] is False and calls["post"]["count"] == 0


async def test_a_connector_run_scopes_its_lane_to_the_commit_request_it_declares(tmp_path):
    """The executor tells the driver which request the run may commit with (the egress guard
    refuses any other: agentos.node.egress.FlowCommit), and a read declares none."""
    h = await _li_harness(tmp_path)
    seen: list[tuple[str, dict | None]] = []

    async def scope_commit(flow_id, request):
        seen.append((flow_id, request))
    h.driver.scope_commit = scope_commit
    await h.run("connector.linkedin.react", {"post_urn": URN}, lane=LI_LANE)
    await h.run("connector.linkedin.feed", {}, lane=LI_LANE)
    assert seen[0][0] == "connector.linkedin.react"
    assert seen[0][1]["op"] == "^com\\.linkedin\\.sdui\\.reactions\\.create$"
    assert seen[1] == ("connector.linkedin.feed", None)


async def test_a_run_whose_commit_scope_cannot_be_bound_does_not_start(tmp_path):
    from agentos.core.errors import EngineError

    h = await _li_harness(tmp_path)

    async def scope_commit(flow_id, request):
        raise EngineError("the egress guard could not bind the connector run's commit route")
    h.driver.scope_commit = scope_commit
    run = await h.run("connector.linkedin.react", {"post_urn": URN}, lane=LI_LANE)
    assert run.status != RunStatus.SUCCEEDED_VERIFIED and not h.site.state["reaction"]


def test_changed_from_is_an_attribute_check_against_a_saved_element():
    from agentos.core.locator import Condition

    c = Condition.model_validate({"kind": "attribute", "name": "data-urn", "changed_from": "before"})
    fresh = c.render({"vars": {"before": {"found": True, "attributes": {"data-urn": "urn:li:activity:1"}}}})
    assert fresh.baseline_value == "urn:li:activity:1"
    assert c.render({"vars": {"before": {"found": False, "attributes": {}}}}).baseline_value is None
    with pytest.raises(ValueError, match="not saved"):
        c.render({"vars": {}})
    with pytest.raises(ValidationError, match="attribute"):
        Condition.model_validate({"kind": "text", "changed_from": "before"})
    with pytest.raises(ValidationError, match="exactly one"):
        Condition.model_validate({"kind": "attribute", "name": "x", "equals": "a", "changed_from": "before"})


def test_changed_from_never_passes_on_a_missing_baseline():
    """A baseline that was not there (no element, no attribute, ambiguous) fails the check, unless
    the condition allows an empty list, and then only when the saved read found nothing at all."""
    from agentos.core.driver import ElementState
    from agentos.core.locator import Condition
    from agentos.runner.proof import evaluate

    old = "https://media.licdn.com/dms/image/v2/D4D03AQOLD/profile-displayphoto-shrink_200_200/0/1?e=1&t=a"
    shown = ElementState(count=1, found=True, attributes={"src": old})
    c = Condition.model_validate({"kind": "attribute", "name": "src", "changed_from": "before"})
    for before in (ElementState(count=0, found=False), ElementState(count=2, found=False, ambiguous=True),
                   ElementState(count=1, found=True, attributes={}),
                   ElementState(count=1, found=True, attributes={"src": " "})):
        assert evaluate(c.render({"vars": {"before": before.model_dump()}}), shown)[0] is False, before
    empty = Condition.model_validate({"kind": "attribute", "name": "src", "changed_from": "before",
                                      "empty_before_ok": True})
    assert evaluate(empty.render({"vars": {"before": ElementState(count=0).model_dump()}}), shown)[0] is True
    ambiguous = ElementState(count=2, found=False, ambiguous=True).model_dump()
    assert evaluate(empty.render({"vars": {"before": ambiguous}}), shown)[0] is False
    no_attr = ElementState(count=1, found=True, attributes={}).model_dump()
    assert evaluate(empty.render({"vars": {"before": no_attr}}), shown)[0] is False
    with pytest.raises(ValidationError, match="changed_from"):
        Condition.model_validate({"kind": "attribute", "name": "src", "equals": "x", "empty_before_ok": True})


def test_changed_from_compares_the_identity_not_the_raw_value():
    from agentos.core.driver import ElementState
    from agentos.core.locator import Condition
    from agentos.runner.proof import evaluate

    ident = r"^https://media\.licdn\.com/dms/image/(?:v2/)?([A-Za-z0-9_-]+)/"
    c = Condition.model_validate({"kind": "attribute", "name": "src", "changed_from": "before", "identity": ident})
    old = "https://media.licdn.com/dms/image/v2/D4D03AQOLD/profile-displayphoto-shrink_200_200/0/1?e=1&t=a"
    before = {"vars": {"before": ElementState(count=1, found=True, attributes={"src": old}).model_dump()}}
    rc = c.render(before)

    def src(v):
        return ElementState(count=1, found=True, attributes={"src": v})
    assert evaluate(rc, src(old.replace("200_200", "800_800").replace("t=a", "t=b")))[0] is False   # resized
    assert evaluate(rc, src(old.replace("D4D03AQOLD", "D4D03AQNEW")))[0] is True
    assert evaluate(rc, src("https://cdn.example.com/dms/image/v2/D4D03AQNEW/x"))[0] is False     # not the host
    ghost = {"vars": {"before": ElementState(count=1, found=True,
                                             attributes={"src": "https://static.licdn.com/ghost.svg"}).model_dump()}}
    assert evaluate(c.render(ghost), src(old))[0] is True          # a first photo after the placeholder
    for bad in ("(a)(b)", "no group", "("):
        with pytest.raises(ValidationError):
            Condition.model_validate({"kind": "attribute", "name": "src", "changed_from": "b", "identity": bad})


def test_a_lines_text_condition_keeps_line_breaks():
    from agentos.core.driver import ElementState
    from agentos.core.locator import Condition
    from agentos.runner.proof import evaluate

    c = Condition.model_validate({"kind": "text", "equals": "One.\nTwo.", "lines": True})
    st = ElementState(count=1, found=True, text="One. Two.", lines="  One.\t \n Two.  \n")
    assert evaluate(c, st)[0] is True
    assert evaluate(c, st.model_copy(update={"lines": "One. Two."}))[0] is False
    assert evaluate(c, st.model_copy(update={"lines": None}))[0] is False      # no lines read: not proved
    plain = Condition.model_validate({"kind": "text", "equals": "One. Two."})
    assert evaluate(plain, st)[0] is True
    with pytest.raises(ValidationError, match="lines"):
        Condition.model_validate({"kind": "attribute", "name": "x", "equals": "a", "lines": True})


def test_pace_counts_a_week_too():
    from datetime import UTC, datetime, timedelta

    from agentos.runner.connectors.pace import pace_problem

    now = datetime(2026, 11, 10, 12, tzinfo=UTC)
    runs = [SimpleNamespace(id=f"r{i}", surface="test/linkedin", status=RunStatus.SUCCEEDED_VERIFIED,
                            started_at=now - timedelta(days=i + 1), created_at=now - timedelta(days=i + 1))
            for i in range(3)]
    store = SimpleNamespace(list_runs=lambda flow_id, limit: runs)
    pace = {"per_day": 5, "per_week": 3}
    why, when = pace_problem(store, "connector.linkedin.follow", "test/linkedin", pace, now)
    assert "at most 3 times in a week" in why and when == runs[-1].started_at + timedelta(days=7)
    assert pace_problem(store, "connector.linkedin.follow", "test/linkedin", {"per_week": 4}, now) is None
