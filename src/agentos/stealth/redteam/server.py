"""The detector site's server: serves the pages and the sensor, and records what a site's own
server sees, which no page script can fake: every request's headers in the order the browser sent
them, the navigation headers (Sec-Fetch-*, Referer) of each page load, and the sensor's batches.

A session is one visit, named by the `run` query parameter of its first page (`/?run=lane-1-r0`)
and carried by a cookie after that, as a site's own session cookie would be. `?mode=human` marks a
session a person drives (the baseline), and the pages then show the task.

Stdlib only; one ThreadingHTTPServer on loopback. Also serves the stealth probe
(`/fingerprint.html`, `/headers`) so one server answers both checks.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

SITE = Path(__file__).with_name("site")
PAGES = {"/": "index.html", "/signup": "signup.html", "/feed": "feed.html", "/deep": "deep.html", "/input": "input.html",
         "/judges": "judges.html"}
STATIC = {"/sensor.js": ("sensor.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/vendor/botd.esm.js": ("vendor/botd.esm.js", "text/javascript; charset=utf-8")}
MAX_BODY = 4 * 1024 * 1024
RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
COOKIE = "rt_sid"
BENCH_PAGES = ("/fingerprint.html", "/headers", "/deep", "/judges", "/input")


class Store:
    """Sessions in memory: requests and sensor batches, keyed by session id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self.deep: dict[str, dict[str, Any]] = {}  # engine probe results, by the `who` that loaded it

    def _session(self, sid: str) -> dict[str, Any]:
        s = self._sessions.get(sid)
        if s is None:
            s = self._sessions[sid] = {"id": sid, "human": False, "requests": [], "pages": {}}
        return s

    def request(self, sid: str, entry: dict[str, Any], *, human: bool = False) -> None:
        with self._lock:
            s = self._session(sid)
            s["requests"].append(entry)
            s["human"] = s["human"] or human

    def batch(self, sid: str, data: dict[str, Any]) -> None:
        page_id = str(data.get("page") or "?")
        with self._lock:
            page = self._session(sid)["pages"].setdefault(
                page_id, {"meta": {}, "env": {}, "frames": [], "events": [], "final": False, "elapsed": 0})
            if isinstance(data.get("meta"), dict):
                page["meta"] = data["meta"]
            if isinstance(data.get("env"), dict):
                page["env"].update(data["env"])
            if isinstance(data.get("frames"), list) and len(data["frames"]) >= len(page["frames"]):
                page["frames"] = data["frames"]
            if isinstance(data.get("events"), list):
                page["events"].extend(e for e in data["events"] if isinstance(e, dict))
            page["final"] = page["final"] or bool(data.get("final"))
            page["elapsed"] = max(page["elapsed"], int(data.get("elapsed") or 0))
            page.setdefault("received", time.time())

    def session(self, sid: str) -> dict[str, Any] | None:
        """A copy of the session, its pages in the order they were first seen."""
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            pages = sorted(s["pages"].values(), key=lambda p: p.get("received", 0))
            return json.loads(json.dumps({**s, "pages": pages}))

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._sessions)


class _Handler(BaseHTTPRequestHandler):
    server: DetectorServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        pass

    # ---- helpers ----------------------------------------------------------------------------

    def _cookie_sid(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and RUN_ID.match(value):
                return value
        return None

    def _send(self, status: int, body: bytes = b"", kind: str = "text/plain; charset=utf-8",
              extra: list[tuple[str, str]] = ()) -> None:
        self.send_response(status)
        if body or status == 200:
            self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _record(self, sid: str | None, *, human: bool = False) -> None:
        if sid is None:
            return
        url = urlsplit(self.path)
        self.server.store.request(sid, {
            "t": time.time(), "method": self.command, "path": url.path, "query": url.query,
            "version": self.request_version, "headers": [[k, v] for k, v in self.headers.items()],
        }, human=human)

    # ---- routes ------------------------------------------------------------------------------

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        sid, cookies = self._cookie_sid(), []
        run = (query.get("run") or [None])[0]
        human = (query.get("mode") or [None])[0] == "human"
        if url.path == "/" and run and RUN_ID.match(run):
            sid = run  # a new visit starts here
            cookies.append(("Set-Cookie", f"{COOKIE}={sid}; Path=/; SameSite=Lax"))
            cookies.append(("Set-Cookie", f"rt_mode={'human' if human else 'agent'}; Path=/; SameSite=Lax"))
        if url.path not in BENCH_PAGES:  # the bench's own probes are not part of the visit
            self._record(sid, human=human)
        if url.path in PAGES:
            self._send(200, (SITE / PAGES[url.path]).read_bytes(), "text/html; charset=utf-8", cookies)
        elif url.path in STATIC:
            name, kind = STATIC[url.path]
            self._send(200, (SITE / name).read_bytes(), kind)
        elif url.path == "/fingerprint.html":
            from agentos.stealth import stealth
            self._send(200, stealth.PROBE_PAGE.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/headers":
            body = json.dumps({k.lower(): v for k, v in self.headers.items()
                               if k.lower() not in ("host", "connection")}, sort_keys=True).encode()
            self._send(200, body, "application/json")
        elif url.path == "/favicon.ico":
            self._send(204)
        else:
            self._send(404, b"not found")

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        sid = self._cookie_sid()
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(413)
            self.close_connection = True
            return
        body = self.rfile.read(length) if length else b""
        if url.path == "/collect":
            if sid is not None:
                try:
                    data = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    data = None
                if isinstance(data, dict):
                    self.server.store.batch(sid, data)
            self._send(204)
        elif url.path == "/collect-deep":
            who = (parse_qs(url.query).get("who") or ["?"])[0]
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                data = None
            if isinstance(data, dict) and RUN_ID.match(who):
                with self.server.store._lock:
                    self.server.store.deep[who] = data
            self._send(204)
        elif url.path == "/signup":
            self._record(sid)
            self._send(303, extra=[("Location", "/feed")])
        else:
            self._send(404, b"not found")


class DetectorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.store = Store()
        super().__init__((host, port), _Handler)
        self._thread: threading.Thread | None = None

    @property
    def base(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> DetectorServer:
        self._thread = threading.Thread(target=self.serve_forever, name="redteam-site", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
