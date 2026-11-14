"""The unit-test network guard (tests/_netguard.py, installed by tests/conftest.py): loopback and
Unix sockets work, anything else raises and fails the test even when the code swallows the error."""

import asyncio
import contextlib
import socket
import threading

import _netguard
import pytest
from _netguard import NetworkBlocked, is_local


@pytest.fixture
def expect_attempt(request):
    """For the tests below that try a blocked address on purpose: clear the record at teardown."""
    yield
    request.node.stash[_netguard.ATTEMPTS].clear()


def test_loopback_and_unix_addresses_are_allowed():
    for address in [("127.0.0.1", 1), ("127.8.9.10", 1), ("::1", 1, 0, 0), ("localhost", 1),
                    ("api.localhost", 1), ("::ffff:127.0.0.1", 1), ("0.0.0.0", 1), "/tmp/x.sock", b"\0abstract"]:
        assert is_local(address), address


def test_other_hosts_are_not():
    for address in [("10.0.0.1", 1), ("8.8.8.8", 53), ("2001:db8::1", 1, 0, 0), ("example.com", 443),
                    ("::ffff:8.8.8.8", 1), ("169.254.169.254", 80)]:
        assert not is_local(address), address


def test_a_loopback_server_still_answers():
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    threading.Thread(target=lambda: server.accept()[0].sendall(b"hi"), daemon=True).start()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
        assert c.recv(2) == b"hi"
    server.close()


def test_connect_to_a_public_address_is_refused(expect_attempt):
    with socket.socket() as s, pytest.raises(NetworkBlocked):
        s.connect(("192.0.2.1", 80))  # TEST-NET-1: never routed, and never tried


def test_a_swallowed_attempt_is_still_recorded(request, expect_attempt):
    with socket.socket() as s, contextlib.suppress(Exception):  # code under test that hides the error
        s.connect_ex(("192.0.2.1", 80))
    assert request.node.stash[_netguard.ATTEMPTS] == ["connect_ex(('192.0.2.1', 80))"]


async def test_asyncio_connections_are_refused_too(expect_attempt):
    with pytest.raises(NetworkBlocked):
        await asyncio.open_connection("192.0.2.1", 80)


def test_udp_sendto_is_refused(expect_attempt):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s, pytest.raises(NetworkBlocked):
        s.sendto(b"x", ("192.0.2.1", 53))


@pytest.mark.network
def test_the_network_marker_turns_the_guard_off():
    assert not hasattr(socket.socket.connect, "__agentos_netguard__")


def test_without_the_marker_the_guard_is_on():
    assert getattr(socket.socket.connect, "__agentos_netguard__", False)
