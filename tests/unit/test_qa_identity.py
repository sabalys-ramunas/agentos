"""QA bug hunt on signed agent identity (Phase 1, docs/design/agent-identity.md, section 13).

Each xfail pins one defect found in the recent merges. The reasons name the finding ids used in the QA review. The fix removes the marker; strict xfail then forces that.
"""

from __future__ import annotations

import http.server
import threading

import pytest
from test_webbotauth_route import (
    AGENT,
    BROWSER_HEADERS,
    LANE,
    SIGNED,
    Context,
    Frame,
    Request,
    Route,
    make_signer,
)
from typer.testing import CliRunner

from agentos.cli import app
from agentos.core.config import load_config
from agentos.core.models import LaneSpec
from agentos.node.store import LaneStore
from agentos.webbotauth import keys, settings
from agentos.webbotauth.route import LaneSigner
from agentos.webbotauth.settings import LaneIdentity

NO_COOKIE = [h for h in BROWSER_HEADERS if h[0] != "Cookie"]


class StopWhileWorking(Context):
    """Stops the signer at the cookie lookup, which is the first await inside `_prepare`: a stop
    (the detector's, on a leak) that lands while a request is still being prepared."""

    signer: LaneSigner | None = None

    async def cookies(self, urls):
        await self.signer.stop("a hop carried the signature")
        return await super().cookies(urls)


async def test_qa_identity_1_a_removed_host_is_not_signed_while_the_lane_is_open(tmp_path):
    secrets = tmp_path / "secrets"
    key = keys.create_key(secrets, LANE)
    ident = LaneIdentity(lane_id=LANE, signature_agent=AGENT)
    settings.add_host(ident, SIGNED, reason="test", by="owner:cli")
    settings.save(secrets, ident)
    signer = LaneSigner(key, ident, proxied=True)
    await signer.install(Context())
    # the owner runs `agentos identity hosts remove` while the lane is open
    settings.remove_host(ident, SIGNED, by="owner:cli")
    settings.save(secrets, ident)
    route = Route()
    await signer._handle(route, Request(SIGNED + "/after-removal"))
    assert route.kinds() == ["fallback"], route.kinds()


async def test_qa_identity_1_a_removal_from_another_process_stops_signing_at_once(tmp_path):
    secrets = tmp_path / "secrets"
    key = keys.create_key(secrets, LANE)
    ident = LaneIdentity(lane_id=LANE, signature_agent=AGENT)
    settings.add_host(ident, SIGNED, reason="test", by="owner:cli")
    settings.save(secrets, ident)
    signer = LaneSigner(key, ident, proxied=True, secrets_dir=secrets)
    await signer.install(Context())
    assert signer.signing_origins() == {SIGNED}
    other = settings.load(secrets, LANE)  # what `agentos identity hosts remove` reads and writes
    settings.remove_host(other, SIGNED, by="owner:cli")
    settings.save(secrets, other)
    route = Route()
    await signer._handle(route, Request(SIGNED + "/after-removal"))
    assert route.kinds() == ["fallback"], route.kinds()
    settings.delete(secrets, LANE)  # a missing file stops all signing
    assert signer.signing_origins() == frozenset() and signer.stopped


class _Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep test output clean
        return None

    def reply(self, status: int, body: bytes, headers: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    home = tmp_path / "agentos-home"
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    assert CliRunner().invoke(app, ["node", "init"]).exit_code == 0
    cfg = load_config(home)
    LaneStore(cfg.db_path).add(LaneSpec(id=LANE))
    cfg.profile_dir(LANE).mkdir(parents=True)
    return home


def identity(*args: str):
    return CliRunner().invoke(app, ["identity", *args])


def test_qa_identity_2_test_sign_send_does_not_forward_the_signature_across_a_redirect(cli_home):
    seen: list[dict[str, str]] = []

    class Other(_Quiet):
        def do_GET(self):
            seen.append(dict(self.headers.items()))
            self.reply(200, b"landed")

    class Listed(_Quiet):
        def do_GET(self):
            self.reply(302, b"", (("Location", f"http://127.0.0.1:{other.server_port}/landing"),))

    other = _serve(Other)
    listed = _serve(Listed)
    try:
        assert identity("init", LANE, "--agent", AGENT).exit_code == 0
        origin = f"http://127.0.0.1:{listed.server_port}"
        assert identity("hosts", "add", LANE, origin, "--reason", "loopback redirect test").exit_code == 0
        result = identity("test-sign", LANE, f"{origin}/start", "--send")
        assert "sent: HTTP 200" in result.output, result.output
        assert seen, "the redirect target was never reached"
        assert not any(k.lower() == "signature" for k in seen[0]), sorted(seen[0])
    finally:
        listed.shutdown()
        other.shutdown()


async def test_qa_identity_3_a_data_frame_is_not_first_party(tmp_path):
    signer, *_ = make_signer(tmp_path)
    await signer.install(Context())
    route = Route()
    frame = Frame("data:text/html,<p>embedded</p>", Frame(SIGNED + "/page"))
    await signer._handle(route, Request(SIGNED + "/api", rtype="fetch", frame=frame))
    assert route.kinds() == ["fallback"], route.kinds()


async def test_qa_identity_4_a_stop_during_preparation_leaves_no_signature(tmp_path):
    signer, *_ = make_signer(tmp_path)
    ctx = StopWhileWorking()
    ctx.signer = signer
    await signer.install(ctx)
    route = Route()
    await signer._handle(route, Request(SIGNED + "/i.png", rtype="image", headers=NO_COOKIE))
    assert route.kinds() == ["fallback"], route.kinds()
