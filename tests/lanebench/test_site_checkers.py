"""The fixture site, its checker and the success checks, without a browser: the site's API is
driven the way its page script drives it, and each checker passes on the right effect and fails
on a wrong one (the same mistakes a deliberately wrong replay makes on the node)."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from lanebench import checkers
from lanebench.sites.app import Site, checker_app, site_app
from lanebench.sites.state import PASSWORD, USERNAME
from lanebench.tasks import load_all

TOKEN = "checker-token"
TASKS = {t.id: t for t in load_all()}


@pytest.fixture
def site():
    s = Site()
    return s, TestClient(site_app(s)), TestClient(checker_app(s, TOKEN), headers={"Authorization": f"Bearer {TOKEN}"})


def start(clients, task_id: str):
    _, web, checker = clients
    conf = checker.post("/configure", json=TASKS[task_id].plan(1)).json()
    if conf["session"]:
        web.cookies.set(conf["cookie"], conf["session"])
    return web, checker


def score(checker, task_id: str, result: dict | None = None) -> bool:
    state = checker.get("/state").json()
    return all(c["passed"] for c in checkers.run_checks(TASKS[task_id].check, state, result or {"status": "done"}))


def test_every_page_renders_under_every_task_plan(site):
    pages = ["/feed", "/compose", "/settings", "/login", "/save-login", "/consent", "/approve-device"]
    for task_id, task in TASKS.items():
        web, _ = start(site, task_id)
        if not task.signed_in:
            web.cookies.set("lb_session", site[0].state.new_session())
        for path in pages:
            r = web.get(path, follow_redirects=False)
            assert r.status_code == 200 and "<!doctype html>" in r.text, (task_id, path)
        assert web.get("/static/site.css").status_code == 200


def test_the_checker_refuses_without_the_token_and_reports_it(site):
    s, web, _ = site
    anon = TestClient(checker_app(s, TOKEN))
    assert anon.get("/state").status_code == 401
    assert web.get("/state").status_code == 404  # the site has no state route
    assert checker_state(site)["intrusions"][0]["path"] == "/state"
    _, checker = start(site, "feed-like")
    assert checker.get("/state").json()["intrusions"] == []  # each task starts clean


def checker_state(clients) -> dict:
    return clients[2].get("/state").json()


def test_signed_out_pages_send_to_login_and_login_follows_the_plan(site):
    web, checker = start(site, "login-consent")
    assert web.get("/feed", follow_redirects=False).headers["location"] == "/login"
    assert web.post("/api/login", json={"username": USERNAME, "password": "nope"}).status_code == 401
    r = web.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    assert r.json()["next"] == "/consent"
    assert web.post("/api/consent", json={"terms": False}).status_code == 400
    web.post("/api/consent", json={"terms": True, "partners": False})
    web.get("/feed")
    assert score(checker, "login-consent")


def test_like_checker_passes_on_the_target_and_fails_on_the_wrong_post(site):
    web, checker = start(site, "feed-like")
    web.post("/api/like", json={"post": "mira-bikes", "on": True})
    assert not score(checker, "feed-like")
    web.post("/api/like", json={"post": "mira-tomatoes", "on": True})
    assert score(checker, "feed-like")


def test_follow_comment_and_settings_checkers(site):
    web, checker = start(site, "feed-follow-moved")
    web.post("/api/follow", json={"user": "tomas", "on": True})
    assert not score(checker, "feed-follow-moved")
    web.post("/api/follow", json={"user": "lina", "on": True})
    assert score(checker, "feed-follow-moved")

    web, checker = start(site, "feed-comment")
    web.post("/api/comment", json={"post": "mira-tomatoes", "text": "Lovely tomato"})
    assert not score(checker, "feed-comment")
    web.post("/api/comment", json={"post": "mira-tomatoes", "text": "Lovely tomatoes!"})
    assert score(checker, "feed-comment")

    web, checker = start(site, "settings-lt")
    web.post("/api/settings", json={"display_name": "Ada Lovelace", "notifications": True})
    assert not score(checker, "settings-lt")
    web.post("/api/settings", json={"display_name": "Ada Lovelace", "notifications": False})
    assert score(checker, "settings-lt")


def test_composer_checkers_tell_a_draft_from_a_publish(site):
    web, checker = start(site, "compose-draft")
    web.post("/api/draft", json={"text": "Autumn menu is here"})
    assert score(checker, "compose-draft")
    web.post("/api/publish", json={"text": "Autumn menu is here", "source": "composer"})
    assert not score(checker, "compose-draft")  # nothing_published

    web, checker = start(site, "compose-publish")
    web.post("/api/publish", json={"text": "Fresh bread on Saturday", "source": "composer"})
    assert not score(checker, "compose-publish")  # no photo
    up = web.post("/api/upload", files={"photo": ("photo.png", b"\x89PNG\r\n\x1a\n...", "image/png")}).json()
    web.post("/api/publish", json={"text": "Fresh bread on Saturday", "upload": up["id"], "source": "composer"})
    assert score(checker, "compose-publish")


def test_login_and_hold_checkers(site):
    web, checker = start(site, "login-cookie-wall")
    assert "cookie-wall" in web.get("/login").text
    web.post("/api/cookies", json={"choice": "all"})
    web.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    web.get("/feed")
    assert not score(checker, "login-cookie-wall")  # accepted every cookie

    web, checker = start(site, "login-device")
    assert web.post("/api/login", json={"username": USERNAME, "password": PASSWORD}).json()["next"] == "/approve-device"
    assert not score(checker, "login-device", {"status": "done"})
    assert score(checker, "login-device", {"status": "held"})


def test_the_answer_checker_reads_the_sites_own_latest_comment(site):
    _, checker = start(site, "read-latest-comment")
    state = checker.get("/state").json()
    latest = state["latest_comment"]["jonas-bread"]
    jonas = next(p for p in state["posts"] if p["id"] == "jonas-bread")
    handle = jonas["comments"][-1]["handle"]
    assert score(checker, "read-latest-comment", {"status": "done", "answer": {"latest_comment": f"@{handle} {latest}"}})
    first = jonas["comments"][0]
    assert not score(checker, "read-latest-comment", {"answer": {"latest_comment": f"@{first['handle']} {first['text']}"}})


def test_the_input_log_is_kept_per_page_view(site):
    web, checker = start(site, "feed-like")
    web.post("/__lb/input", json={"view": "v1", "path": "/feed", "events": [{"k": "move", "t": 1, "x": 1, "y": 2}]})
    web.post("/__lb/input", json={"view": "v1", "path": "/feed", "events": [{"k": "move", "t": 2, "x": 3, "y": 2}]})
    views = checker.get("/state").json()["views"]
    assert len(views) == 1 and len(views[0]["events"]) == 2
