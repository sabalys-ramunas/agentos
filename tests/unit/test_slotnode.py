"""The bench and soak harness without a node: the stand-in proxy, and how a regression is flagged."""

from __future__ import annotations

import http.client
import importlib.util
import json
import socket
from pathlib import Path

import pytest

from openajar.dev.slotnode import IP_HOST, SITE, SITE_HOST, StandInProxy

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def proxy():
    p = StandInProxy(free_port(), username="u", password="secret-pw").start()
    yield p
    p.stop()


def get(proxy: StandInProxy, url: str, *, auth: bool = True, method: str = "GET",
        conn: http.client.HTTPConnection | None = None):
    conn = conn or http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
    conn.request(method, url, headers={"Proxy-Authorization": proxy.auth} if auth else {})
    response = conn.getresponse()
    return response.status, response.read(), dict(response.getheaders())


def test_it_serves_the_fixture_site_only_to_its_own_users(proxy):
    status, body, _ = get(proxy, f"{SITE}/feed.html")
    assert status == 200 and b"Post by Ada Lovelace" in body
    assert get(proxy, f"{SITE}/feed.html", auth=False)[0] == 407
    assert get(proxy, f"{SITE}/../../pyproject.toml")[0] == 404
    assert get(proxy, f"{SITE}/%2e%2e/%2e%2e/pyproject.toml")[0] == 404
    assert get(proxy, "http://example.com/")[0] == 404  # never forwarded
    assert proxy.refused == 1 and proxy.requests == [(SITE_HOST, "/feed.html"), (SITE_HOST, "/../../pyproject.toml"),
                                                     (SITE_HOST, "/%2e%2e/%2e%2e/pyproject.toml")]


def test_the_exit_check_reads_the_exit_it_is_told(proxy):
    status, body, headers = get(proxy, f"http://{IP_HOST}/ip")
    assert (status, body) == (200, b"203.0.113.10") and headers["Access-Control-Allow-Origin"] == "*"
    proxy.exit_ip = "203.0.113.99"
    assert get(proxy, f"http://{IP_HOST}/ip")[1] == b"203.0.113.99"


def test_no_tunnels(proxy):
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
    conn.request("CONNECT", "example.com:443", headers={"Proxy-Authorization": proxy.auth})
    assert conn.getresponse().status == 403


def test_stopping_it_cuts_open_connections(proxy):
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
    assert get(proxy, f"{SITE}/index.html", conn=conn)[0] == 200  # a keep-alive connection stays open
    proxy.stop()
    with pytest.raises((OSError, http.client.HTTPException)):
        get(proxy, f"{SITE}/index.html", conn=conn)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", proxy.port), timeout=1).close()
    proxy.start()
    assert get(proxy, f"{SITE}/index.html")[0] == 200


def load_compare():
    spec = importlib.util.spec_from_file_location("bench_compare", ROOT / "scripts" / "bench_compare.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def result(commit: str, host: str = "node", **metrics) -> dict:
    return {"commit": commit, "host": host, "metrics": metrics}


def test_a_regression_is_over_the_threshold_and_over_the_noise(tmp_path, capsys):
    bench = load_compare()
    rows = {r[0]: r for r in bench.compare(result("a", wake_cold_s=2.0, act_ms=40.0, mcp_overhead_ms=4.0),
                                           result("b", wake_cold_s=2.6, act_ms=44.0, mcp_overhead_ms=8.0), 0.2)}
    assert rows["wake_cold_s"][4] is True                      # +30% and 0.6 s
    assert rows["act_ms"][4] is False                          # +10%
    assert rows["mcp_overhead_ms"][4] is False                 # +100%, but 4 ms is noise

    path = tmp_path / "results.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in (
        result("a", wake_cold_s=2.0), result("x", host="laptop", wake_cold_s=9.0), result("b", wake_cold_s=2.6))))
    assert bench.main(["--file", str(path)]) == 1
    assert "b against a on node" in capsys.readouterr().out    # the laptop's result is not the baseline
    assert bench.main(["--file", str(path), "--threshold", "0.5"]) == 0
    assert bench.main(["--file", str(tmp_path / "none.jsonl")]) == 0
