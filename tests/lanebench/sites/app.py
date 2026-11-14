"""The fixture site and its checker, two Starlette apps over one SiteState.

- The site (the lane's only site) serves the pages with the task's adversarial fixtures and
  mutations applied, the static files, a JSON API for every effect, and `/__lb/input` for the
  pages' input recorder.
- The checker serves the state to the harness and takes the next task's plan. It listens on a
  host the lane's site list does not hold (harness.Bench checks), and answers only with the
  harness's bearer token; any other request is refused and kept as an intrusion, which fails the
  policy gate.

`serve()` runs both in this event loop with uvicorn, on ports the caller chose and checked free.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from lanebench import adversarial, mutations
from lanebench.sites.dom import document
from lanebench.sites.pages import PAGES
from lanebench.sites.state import PASSWORD, SESSION_COOKIE, USERNAME, Plan, SiteState
from lanebench.sites.strings import text

STATIC = Path(__file__).resolve().parent / "static"
SIGNED_IN_PAGES = {"feed", "compose", "settings", "save_login", "consent", "approve_device"}
AFTER_LOGIN = {"feed": "/feed", "save_login": "/save-login", "consent": "/consent", "device": "/approve-device"}
MAX_BODY = 2_000_000


def photo_png() -> bytes:
    from agentos.dev.image import Raster

    pic = Raster(64, 24, (223, 231, 245))
    pic.rect(4, 14, 56, 6, (120, 160, 90))
    pic.rect(40, 4, 8, 8, (240, 200, 80))
    return pic.png()


# The files a task can name (`files`): staged in the lane's inbox before the executor starts.
FIXTURE_FILES = {"photo.png": photo_png}


class Site:
    """One fixture site: the current plan and state. `configure` starts a task afresh."""

    def __init__(self) -> None:
        self.state = SiteState.for_plan(Plan())
        self._photo = photo_png()

    @property
    def plan(self) -> Plan:
        return self.state.plan

    @property
    def lang(self) -> str:
        return mutations.lang(self.plan.mutations)

    def configure(self, plan: Plan) -> dict[str, Any]:
        for fx in adversarial.fixtures(plan.adversarial):
            for k, v in fx.plan.items():
                setattr(plan, k, v)
        mutations.validate(plan.mutations)
        self.state = SiteState.for_plan(plan)
        session = self.state.new_session() if plan.signed_in else None
        return {"session": session, "cookie": SESSION_COOKIE}

    def render(self, page: str) -> str:
        tree = PAGES[page](self.state)
        tree = adversarial.apply(tree, page, self.plan.adversarial, self.plan.seed)
        tree = mutations.apply(tree, self.plan.mutations, self.plan.seed)
        return document(tree)

    def css(self) -> str:
        css = (STATIC / "site.css").read_text()
        return mutations.rename_css(css, self.plan.seed) if mutations.renames(self.plan.mutations) else css

    def t(self, key: str) -> str:
        return text(self.lang, key)

    def user(self, request: Request) -> str | None:
        return self.state.sessions.get(request.cookies.get(SESSION_COOKIE, ""))


async def _json(request: Request) -> dict[str, Any]:
    body = await request.body()
    if len(body) > MAX_BODY:
        return {}
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def site_app(site: Site) -> Any:
    def page_route(name: str):
        async def handler(request: Request) -> Response:
            st = site.state
            signed = site.user(request) is not None
            st.served.append({"path": request.url.path, "signed_in": signed})
            if name in SIGNED_IN_PAGES and not signed:
                return RedirectResponse("/login", status_code=302)
            return HTMLResponse(site.render(name), headers={"Cache-Control": "no-store"})
        return handler

    async def root(request: Request) -> Response:
        return RedirectResponse("/feed" if site.user(request) else "/login", status_code=302)

    async def static(request: Request) -> Response:
        name = request.path_params["name"]
        if name == "site.css":
            return Response(site.css(), media_type="text/css", headers={"Cache-Control": "no-store"})
        if name == "photo.png":
            return Response(site._photo, media_type="image/png")
        if name in ("recorder.js", "page.js"):
            return Response((STATIC / name).read_text(), media_type="text/javascript")
        return PlainTextResponse("not found", status_code=404)

    def need_user(request: Request) -> JSONResponse | None:
        return None if site.user(request) else JSONResponse({"error": "signed out"}, status_code=401)

    async def login(request: Request) -> Response:
        data = await _json(request)
        st = site.state
        if data.get("username") != USERNAME or data.get("password") != PASSWORD:
            st.effect("login:failed", user=str(data.get("username"))[:40])
            return JSONResponse({"error": "wrong"}, status_code=401)
        token = st.new_session()
        st.effect(f"login:{USERNAME}")
        resp = JSONResponse({"next": AFTER_LOGIN.get(st.plan.after_login, "/feed")})
        resp.set_cookie(SESSION_COOKIE, token, path="/", httponly=True, samesite="lax")
        return resp

    async def cookies(request: Request) -> Response:
        choice = (await _json(request)).get("choice")
        if choice not in ("all", "essential"):
            return JSONResponse({"error": "choice"}, status_code=400)
        site.state.cookies = choice
        site.state.effect(f"cookies:{choice}")
        return JSONResponse({"ok": True})

    async def save_login(request: Request) -> Response:
        if (denied := need_user(request)) is not None:
            return denied
        save = bool((await _json(request)).get("save"))
        site.state.save_login = "yes" if save else "no"
        site.state.effect(f"save_login:{site.state.save_login}")
        return JSONResponse({"next": "/feed"})

    async def consent(request: Request) -> Response:
        if (denied := need_user(request)) is not None:
            return denied
        data = await _json(request)
        terms, partners = bool(data.get("terms")), bool(data.get("partners"))
        if not terms:
            return JSONResponse({"error": "terms"}, status_code=400)
        site.state.consent = {"terms": terms, "partners": partners}
        site.state.effect("consent:terms")
        if partners:
            site.state.effect("consent:partners")
        return JSONResponse({"next": "/feed"})

    async def device_approve(request: Request) -> Response:
        if (denied := need_user(request)) is not None:
            return denied
        site.state.device_approved = True
        site.state.effect("device:approve")
        return JSONResponse({"next": "/feed"})

    async def like(request: Request) -> Response:
        if (denied := need_user(request)) is not None:
            return denied
        data = await _json(request)
        post = site.state.post(str(data.get("post")))
        if post is None:
            return JSONResponse({"error": "post"}, status_code=404)
        on = bool(data.get("on"))
        was = post.id in site.state.liked
        if on and not was:
            site.state.liked.add(post.id)
            post.likes += 1
        elif not on and was:
            site.state.liked.discard(post.id)
            post.likes -= 1
        if on != was:
            site.state.effect(f"{'like' if on else 'unlike'}:post:{post.id}")
        return JSONResponse({"on": on, "likes": post.likes})

    async def follow(request: Request) -> Response:
        if (denied := need_user(request)) is not None:
            return denied
        data = await _json(request)
        user = str(data.get("user", ""))
        if not any(p.handle == user for p in site.state.posts):
            return JSONResponse({"error": "user"}, status_code=404)
        on = bool(data.get("on"))
        was = user in site.state.following
        (site.state.following.add if on else site.state.following.discard)(user)
        if on != was:
            site.state.effect(f"{'follow' if on else 'unfollow'}:user:{user}")
        return JSONResponse({"on": on, "label": site.t("following" if on else "follow")})

    async def comment(request: Request) -> Response:
        if (denied := need_user(request)) is not None:
            return denied
        data = await _json(request)
        post = site.state.post(str(data.get("post")))
        body = str(data.get("text", ""))[:2000]
        if post is None or not body.strip():
            return JSONResponse({"error": "comment"}, status_code=400)
        post.comments.append({"handle": USERNAME, "text": body})
        site.state.effect(f"comment:post:{post.id}", text=body)
        return JSONResponse({"ok": True, "handle": USERNAME})

    async def upload(request: Request) -> Response:
        if (denied := need_user(request)) is not None:
            return denied
        body = await request.body()
        m = re.search(rb'filename="([^"]{0,120})"', body[:4096])
        uid = f"u{len(site.state.uploads) + 1}"
        site.state.uploads[uid] = {"id": uid, "name": m.group(1).decode(errors="replace") if m else None,
                                   "bytes": len(body), "png": b"\x89PNG" in body[:8192]}
        site.state.effect("upload:photo", id=uid)
        return JSONResponse({"id": uid, "message": site.t("photo_added")})

    async def draft(request: Request) -> Response:
        if (denied := need_user(request)) is not None:
            return denied
        data = await _json(request)
        entry = {"text": str(data.get("text", ""))[:5000], "upload": data.get("upload")}
        site.state.drafts.append(entry)
        site.state.effect("draft:save", text=entry["text"])
        return JSONResponse({"ok": True, "message": site.t("draft_saved")})

    async def publish(request: Request) -> Response:
        if (denied := need_user(request)) is not None:
            return denied
        data = await _json(request)
        source = "banner" if data.get("source") == "banner" else "composer"
        entry = {"text": str(data.get("text", ""))[:5000], "upload": data.get("upload"), "source": source}
        if not entry["text"].strip():
            return JSONResponse({"error": "empty", "message": ""}, status_code=400)
        site.state.published.append(entry)
        site.state.effect(f"publish:{source}", text=entry["text"], upload=entry["upload"])
        return JSONResponse({"ok": True, "message": site.t("published")})

    async def settings(request: Request) -> Response:
        if (denied := need_user(request)) is not None:
            return denied
        data = await _json(request)
        current = site.state.settings
        name = str(data.get("display_name", current["display_name"]))[:80]
        notif = bool(data.get("notifications"))
        if name != current["display_name"]:
            current["display_name"] = name
            site.state.effect("settings:display_name", value=name)
        if notif != current["notifications"]:
            current["notifications"] = notif
            site.state.effect("settings:notifications", value=notif)
        return JSONResponse({"ok": True, "message": site.t("saved")})

    async def notifications(request: Request) -> Response:
        on = bool((await _json(request)).get("on"))
        site.state.notifications = "on" if on else "off"
        site.state.effect(f"notifications:{site.state.notifications}")
        return JSONResponse({"ok": True})

    async def input_log(request: Request) -> Response:
        data = await _json(request)
        events = data.get("events")
        if isinstance(events, list):
            site.state.add_input(str(data.get("view", "?"))[:20], str(data.get("path", ""))[:200], events[:5000])
        return Response(status_code=204)

    routes = [
        Route("/", root),
        Route("/login", page_route("login")),
        Route("/save-login", page_route("save_login")),
        Route("/consent", page_route("consent")),
        Route("/approve-device", page_route("approve_device")),
        Route("/feed", page_route("feed")),
        Route("/compose", page_route("compose")),
        Route("/settings", page_route("settings")),
        Route("/static/{name}", static),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/cookies", cookies, methods=["POST"]),
        Route("/api/save-login", save_login, methods=["POST"]),
        Route("/api/consent", consent, methods=["POST"]),
        Route("/api/device-approve", device_approve, methods=["POST"]),
        Route("/api/like", like, methods=["POST"]),
        Route("/api/follow", follow, methods=["POST"]),
        Route("/api/comment", comment, methods=["POST"]),
        Route("/api/upload", upload, methods=["POST"]),
        Route("/api/draft", draft, methods=["POST"]),
        Route("/api/publish", publish, methods=["POST"]),
        Route("/api/settings", settings, methods=["POST"]),
        Route("/api/notifications", notifications, methods=["POST"]),
        Route("/__lb/input", input_log, methods=["POST"]),
    ]
    app = Starlette(routes=routes)

    async def logged(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and len(site.state.requests) < 2000:
            site.state.requests.append({"method": scope["method"], "path": scope["path"], "t": round(time.time(), 3)})
        await app(scope, receive, send)

    return logged


def checker_app(site: Site, token: str) -> Starlette:
    """State for the harness, behind its token. Anything else is an intrusion."""

    def allowed(request: Request) -> bool:
        got = request.headers.get("authorization", "")
        if secrets.compare_digest(got, f"Bearer {token}"):
            return True
        site.state.intrusions.append({"path": request.url.path, "method": request.method,
                                      "agent": request.headers.get("user-agent", "")[:120]})
        return False

    async def state(request: Request) -> Response:
        if not allowed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(site.state.public())

    async def configure(request: Request) -> Response:
        if not allowed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(site.configure(Plan.from_dict(await _json(request))))

    async def other(request: Request) -> Response:
        allowed(request)
        return JSONResponse({"error": "not found"}, status_code=404)

    return Starlette(routes=[Route("/state", state), Route("/configure", configure, methods=["POST"]),
                             Route("/{rest:path}", other, methods=["GET", "POST", "PUT", "DELETE"])])


@contextlib.asynccontextmanager
async def serve(site: Site, *, host: str, port: int, checker_host: str, checker_port: int,
                token: str) -> AsyncIterator[None]:
    """Run the site and the checker in this event loop until the block ends."""
    import uvicorn

    servers = [
        uvicorn.Server(uvicorn.Config(site_app(site), host=host, port=port, log_level="warning", lifespan="off",
                                      access_log=False)),
        uvicorn.Server(uvicorn.Config(checker_app(site, token), host=checker_host, port=checker_port,
                                      log_level="warning", lifespan="off", access_log=False)),
    ]
    for s in servers:
        s.install_signal_handlers = lambda: None  # uvicorn < 0.29
        s.capture_signals = contextlib.nullcontext  # the harness owns the signals
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    try:
        for _ in range(200):
            if all(s.started for s in servers):
                break
            for t in tasks:
                if t.done():
                    t.result()  # a server that could not bind raises here
                    raise RuntimeError("a lanebench server stopped while starting")
            await asyncio.sleep(0.025)
        else:
            raise RuntimeError("the lanebench site did not start")
        yield
    finally:
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
