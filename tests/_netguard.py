"""The unit-test network guard that tests/conftest.py installs for every test not marked `node` or
`network`: a socket may connect or send only to this machine (loopback addresses, "localhost",
Unix sockets). Kept out of conftest.py so tests/unit/test_network_guard.py can test it."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import pytest

ATTEMPTS = pytest.StashKey[list[str]]()


class NetworkBlocked(RuntimeError):
    """A unit test tried to reach a host that is not this machine. A RuntimeError, not an OSError,
    so code that handles connection errors does not mistake it for an unreachable host."""


def is_local(address: Any) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX paths and abstract names stay on this machine
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if not isinstance(host, str):
        return False
    host = host.strip("[]").split("%", 1)[0].lower().rstrip(".")
    if host in ("localhost", "") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # a name: connecting would resolve it through DNS
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def _guard(name: str, original: Any, attempts: list[str]) -> Any:
    def guarded(self: socket.socket, *args: Any, **kwargs: Any) -> Any:
        # connect(address), connect_ex(address), sendto(data, [flags,] address)
        if name == "sendto":
            address = args[-1] if len(args) >= 2 else None
        else:
            address = args[0] if args else kwargs.get("address")
        if self.family in (socket.AF_INET, socket.AF_INET6) and address is not None and not is_local(address):
            attempts.append(f"{name}({address!r})")
            raise NetworkBlocked(f"unit tests may not reach {address!r}; mark the test "
                                 "@pytest.mark.network if it really needs the network")
        return original(self, *args, **kwargs)

    guarded.__agentos_netguard__ = True  # type: ignore[attr-defined]
    return guarded


def install(monkeypatch: pytest.MonkeyPatch, attempts: list[str]) -> None:
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _guard(name, getattr(socket.socket, name), attempts))
