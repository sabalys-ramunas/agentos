#!/usr/bin/env python3
"""SPIKE, NOT PRODUCT CODE. Measurements behind docs/design/egress-guard.md.

Answers, with numbers, on a throwaway Camoufox profile and local pages only:

  cache    what a Playwright context.route (narrow or catch-all) does to the HTTP cache of other
           URLs, and what it costs in requests and bytes on a re-visit and a reload
  window   whether a route installed for a few seconds (an agent action) leaves the cache as it
           was: are responses loaded during the window stored, are earlier entries still used
  timing   page load medians with and without a route
  detect   whether `request` events see every kind of document navigation (link, form POST,
           meta refresh, JS location, popup, iframe, every redirect hop), whether the request
           reaches the server before the driver hears of it, and whether stopping it keeps the
           page where it was

Run it on a node dev slot only (slot 41: display :410, ports 6410 and 6411):

  DISPLAY_NUM=410 BASE_PORT=6410 .venv/bin/python scripts/spikes/egress_guard.py --out /tmp/egress.json

Two loopback servers stand in for the lane's site (127.0.0.1) and a foreign host (127.0.0.2).
Nothing here contacts any other host. Every process it starts is stopped at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DISPLAY_NUM = int(os.environ.get("DISPLAY_NUM", "410"))
BASE_PORT = int(os.environ.get("BASE_PORT", "6410"))
SITE = f"http://127.0.0.1:{BASE_PORT}"
FOREIGN = f"http://127.0.0.2:{BASE_PORT}"
ASSET = bytes(range(256)) * 80  # 20 kB per static asset

LOG: list[dict[str, Any]] = []
LOCK = threading.Lock()

PAGES = {  # page name -> image numbers it shows
    "p1": range(1, 6), "p2": range(1, 6), "p3": range(6, 11), "p4": range(6, 11),
}


def page_html(name: str) -> bytes:
    imgs = "".join(f'<img src="/s/img{i}.png" width=10 height=10>' for i in PAGES.get(name, ()))
    return (f'<!doctype html><html><head><title>{name}</title><link rel="stylesheet" href="/s/a.css">'
            f'<script src="/s/b.js"></script><script src="/s/rev.js"></script></head><body><h1>{name}</h1>'
            f'{imgs}<a id="off" href="{FOREIGN}/slow/link">off</a>'
            f'<form id="f" method="post" action="{FOREIGN}/slow/form"><input name="q" value="secret">'
            f'<button id="send">send</button></form></body></html>').encode()


NAV_PAGES = {
    "meta": f'<meta http-equiv="refresh" content="0;url={FOREIGN}/slow/meta">',
    "iframe": f'<iframe src="{FOREIGN}/slow/iframe"></iframe>',
    "redirect-host": "",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a: Any) -> None:
        pass

    def _send(self, code: int, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        with LOCK:
            LOG.append({"t": time.monotonic(), "host": self.headers.get("Host", ""), "method": self.command,
                        "path": self.path, "status": code, "bytes": len(body),
                        "inm": self.headers.get("If-None-Match"), "cc": self.headers.get("Cache-Control"),
                        "pragma": self.headers.get("Pragma")})

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.do_GET()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/page/"):
            name = path.removeprefix("/page/")
            body = page_html(name)
            etag = '"' + hashlib.sha1(body).hexdigest()[:12] + '"'
            if self.headers.get("If-None-Match") == etag:
                self._send(304, b"", {"ETag": etag, "Cache-Control": "no-cache"})
                return
            self._send(200, body, {"Content-Type": "text/html", "Cache-Control": "no-cache", "ETag": etag})
        elif path.startswith("/nav/"):
            name = path.removeprefix("/nav/")
            self._send(200, f"<!doctype html><html><head><title>{name}</title>{NAV_PAGES.get(name, '')}"
                            f"</head><body>{name}</body></html>".encode(), {"Content-Type": "text/html"})
        elif path == "/s/rev.js":
            etag = '"rev1"'
            if self.headers.get("If-None-Match") == etag:
                self._send(304, b"", {"ETag": etag, "Cache-Control": "no-cache"})
                return
            self._send(200, b"/*" + ASSET[:2000].hex().encode() + b"*/", {
                "Content-Type": "text/javascript", "Cache-Control": "no-cache", "ETag": etag})
        elif path.startswith("/s/"):
            ctype = {"css": "text/css", "js": "text/javascript", "png": "image/png"}[path.rsplit(".", 1)[1]]
            body = ASSET if ctype == "image/png" else b"/*" + ASSET[:4000].hex().encode() + b"*/"
            self._send(200, body, {"Content-Type": ctype, "Cache-Control": "public, max-age=3600",
                                   "ETag": '"' + path + '"', "Last-Modified": "Mon, 02 Nov 2026 10:00:00 GMT"})
        elif path.startswith("/slow/"):
            with LOCK:  # the request arrived (logged before the answer, which a stop may cut off)
                LOG.append({"t": time.monotonic(), "host": self.headers.get("Host", ""), "method": self.command,
                            "path": self.path, "status": 0, "bytes": 0, "recv": True,
                            "inm": None, "cc": None, "pragma": None})
            time.sleep(2.0)
            self._send(200, f"<!doctype html><title>foreign {path}</title><body>foreign</body>".encode(),
                       {"Content-Type": "text/html"})
        elif path == "/redir":
            to = self.path.split("to=", 1)[1] if "to=" in self.path else "/page/p1"
            from urllib.parse import unquote

            self._send(302, b"", {"Location": unquote(to)})
        else:
            self._send(404, b"nf", {"Content-Type": "text/plain"})


def serve() -> list[ThreadingHTTPServer]:
    out = []
    for host in ("127.0.0.1", "127.0.0.2"):
        s = ThreadingHTTPServer((host, BASE_PORT), Handler)
        s.daemon_threads = True
        threading.Thread(target=s.serve_forever, daemon=True).start()
        out.append(s)
    return out


def mark() -> int:
    with LOCK:
        return len(LOG)


def since(i: int) -> list[dict[str, Any]]:
    with LOCK:
        return list(LOG[i:])


def tally(rows: list[dict[str, Any]]) -> dict[str, Any]:
    static = [r for r in rows if r["path"].startswith("/s/")]
    return {"requests": len(rows), "static_requests": len(static),
            "static_200": sum(1 for r in static if r["status"] == 200),
            "static_304": sum(1 for r in static if r["status"] == 304),
            "bytes": sum(r["bytes"] for r in rows),
            "pragma_no_cache": sum(1 for r in rows if r["pragma"]),
            "cache_control_sent": sorted({r["cc"] for r in rows if r["cc"]})}


async def launch(profile: Path) -> Any:
    from camoufox.async_api import AsyncCamoufox

    env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
    env["DISPLAY"] = f":{DISPLAY_NUM}"
    cm = AsyncCamoufox(persistent_context=True, user_data_dir=str(profile), headless=False, geoip=False,
                       humanize=False, enable_cache=True, i_know_what_im_doing=True, env=env,
                       firefox_user_prefs={"browser.cache.disk.smart_size.enabled": False,
                                           "browser.cache.disk.capacity": 262144})
    ctx = await cm.__aenter__()
    return cm, ctx


async def goto(page: Any, name: str) -> dict[str, Any]:
    i = mark()
    t0 = time.monotonic()
    await page.goto(f"{SITE}/page/{name}", wait_until="load")
    ms = (time.monotonic() - t0) * 1000
    await asyncio.sleep(0.3)
    perf = await page.evaluate("performance.getEntriesByType('resource').map(e => [e.name, e.transferSize])")
    return {"page": name, "ms": round(ms, 1), "server": tally(since(i)),
            "perf_from_cache": sum(1 for _, size in perf if size == 0), "perf_entries": len(perf)}


async def reload(page: Any) -> dict[str, Any]:
    i = mark()
    await page.reload(wait_until="load")
    await asyncio.sleep(0.3)
    return {"page": "reload", "server": tally(since(i))}


async def noop_route(route: Any) -> None:
    await route.fallback()


async def docs_only(route: Any) -> None:
    await route.fallback()  # a real guard would judge documents here; every other type falls back


async def scenario(work: Path, name: str) -> dict[str, Any]:
    profile = work / f"profile-{name}"
    cm, ctx = await launch(profile)
    steps: list[dict[str, Any]] = []
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if name == "narrow":
            await ctx.route("http://route-never.invalid/**", noop_route)
        if name == "catchall_docs":
            await ctx.route("**/*", docs_only)
        if name == "listeners":
            ctx.on("request", lambda r: None)
            page.on("framenavigated", lambda f: None)
        if name == "window":
            steps.append(await goto(page, "p1"))           # warm, no route
            await ctx.route("http://route-never.invalid/**", noop_route)
            steps.append(await goto(page, "p2"))           # p1's assets: still read from the cache?
            steps.append(await goto(page, "p3"))           # new assets loaded while the route exists
            await ctx.unroute("http://route-never.invalid/**", noop_route)
            steps.append(await goto(page, "p4"))           # p3's assets: were they stored?
            steps.append(await goto(page, "p4"))
            return {"scenario": name, "steps": steps}
        steps.append(await goto(page, "p1"))
        steps.append(await goto(page, "p2"))
        steps.append(await reload(page))
        steps.append(await goto(page, "p1"))
        times = []
        for k in range(12):
            r = await goto(page, "p2" if k % 2 else "p1")
            times.append(r["ms"])
        return {"scenario": name, "steps": steps, "median_ms": statistics.median(times[2:]),
                "times": times}
    finally:
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)


async def detect(work: Path) -> dict[str, Any]:
    """Which document navigations `request` events see, how late, and whether stopping keeps the page."""
    cm, ctx = await launch(work / "profile-detect")
    events: list[dict[str, Any]] = []
    stop_mode = {"how": "none"}
    tasks: set[asyncio.Task] = set()

    async def stop(req: Any) -> None:
        try:
            frame = req.frame
            if stop_mode["how"] == "window.stop":
                await frame.evaluate("window.stop()")
        except Exception as e:  # noqa: BLE001
            events.append({"stop_error": type(e).__name__ + ": " + str(e)[:120]})

    def on_request(req: Any) -> None:
        try:
            frame = req.frame
            main = frame.parent_frame is None
            furl = frame.url
        except Exception:  # noqa: BLE001
            main, furl = None, None
        events.append({"t": time.monotonic(), "url": req.url, "nav": req.is_navigation_request(),
                       "type": req.resource_type, "method": req.method, "main": main, "frame_url": furl,
                       "from": req.redirected_from.url if req.redirected_from else None})
        if req.is_navigation_request() and req.url.startswith(FOREIGN):
            t = asyncio.get_running_loop().create_task(stop(req))
            tasks.add(t)
            t.add_done_callback(tasks.discard)

    ctx.on("request", on_request)
    results: dict[str, Any] = {}
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def case(label: str, how: str, trigger: Any, settle: float = 3.0) -> None:
            stop_mode["how"] = how
            await page.goto(f"{SITE}/page/p1", wait_until="load")
            pages_before = len(ctx.pages)
            events.clear()
            i = mark()
            t0 = time.monotonic()
            with contextlib.suppress(Exception):
                await trigger()
            await asyncio.sleep(settle)
            rows = [r for r in since(i) if "127.0.0.2" in r["host"]]
            srv = [r for r in rows if not r.get("recv")]
            recv = [r for r in rows if r.get("recv")]
            ev = [e for e in events if "url" in e and e["url"].startswith(FOREIGN)]
            seen = ev[0]["t"] - t0 if ev else None
            got = srv[0]["t"] - t0 if srv else None
            # the server logs after it answers (2 s); a request arrived ~2 s before its log line
            results[label] = {
                "stop": how, "event_seen": bool(ev), "nav": ev[0]["nav"] if ev else None,
                "main_frame": ev[0]["main"] if ev else None, "method": ev[0]["method"] if ev else None,
                "redirect_hops": [e["from"] for e in events if e.get("from")],
                "event_after_ms": round(seen * 1000) if seen is not None else None,
                "server_answered_after_ms": round(got * 1000) if got is not None else None,
                "server_hits": len(srv), "server_received": len(recv),
                "received_after_ms": round((recv[0]["t"] - t0) * 1000) if recv else None, "url_after": page.url, "new_pages": len(ctx.pages) - pages_before,
                "errors": [e["stop_error"] for e in events if "stop_error" in e]}
            for p in ctx.pages[pages_before:]:
                with contextlib.suppress(Exception):
                    await p.close()

        for how in ("none", "window.stop"):
            await case(f"js_location/{how}", how,
                       lambda: page.evaluate(f"location.href = '{FOREIGN}/slow/js?d=x'"))
            await case(f"link_click/{how}", how, lambda: page.click("#off"))
            await case(f"form_post/{how}", how, lambda: page.click("#send"))
            await case(f"meta_refresh/{how}", how, lambda: page.goto(f"{SITE}/nav/meta", wait_until="commit"))
            await case(f"redirect/{how}", how,
                       lambda: page.evaluate(f"location.href = '{SITE}/redir?to={FOREIGN}/slow/redir'"))
            await case(f"iframe/{how}", how, lambda: page.goto(f"{SITE}/nav/iframe", wait_until="commit"))
            await case(f"popup/{how}", how, lambda: page.evaluate(f"window.open('{FOREIGN}/slow/popup')"))
        return results
    finally:
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)


async def main_async(out: Path, only: list[str]) -> None:
    work = Path(tempfile.mkdtemp(prefix="egress-spike-"))
    xvfb = subprocess.Popen(  # noqa: ASYNC220 - a spike, started once["Xvfb", f":{DISPLAY_NUM}", "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    servers = serve()
    report: dict[str, Any] = {"camoufox": None}
    try:
        await asyncio.sleep(1.0)
        with contextlib.suppress(Exception):
            from importlib.metadata import version

            report["camoufox"] = version("camoufox")
            report["playwright"] = version("playwright")
        if not only or "cache" in only:
            report["cache"] = [await scenario(work, n) for n in ("none", "narrow", "catchall_docs", "listeners")]
        if not only or "window" in only:
            report["window"] = await scenario(work, "window")
        if not only or "detect" in only:
            report["detect"] = await detect(work)
    finally:
        for s in servers:
            s.shutdown()
        xvfb.terminate()
        with contextlib.suppress(Exception):
            xvfb.wait(5)
        shutil.rmtree(work, ignore_errors=True)
    out.write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("/tmp/egress-spike.json"))
    ap.add_argument("parts", nargs="*", help="cache, window, detect (default all)")
    a = ap.parse_args()
    asyncio.run(main_async(a.out, a.parts))


if __name__ == "__main__":
    main()
