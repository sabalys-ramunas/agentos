"""Local port checks for the tunnel's listening side."""

from __future__ import annotations

import contextlib
import socket
import time

from agentos.client.errors import PortInUse

DEFAULT_LOCAL_PORT = 7400
SEARCH_SPAN = 100


def port_in_use(port: int) -> bool:
    """True when something listens on localhost:port, or ssh could not bind it."""
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        with contextlib.suppress(OSError), socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex((addr, port)) == 0:
                return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # ssh sets it too
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def wait_free(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while port_in_use(port):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def pick_port(preferred: int, reserved: set[int], *, explicit: bool) -> int:
    """`preferred` if it is free; otherwise the next free port (not when the owner chose one)."""
    if explicit:
        if port_in_use(preferred):
            raise PortInUse(f"local port {preferred} is already in use",
                            "stop whatever listens there (an old `ssh -L` tunnel?) or pick another --local-port")
        return preferred
    for port in range(preferred, min(preferred + SEARCH_SPAN, 65536)):
        if port not in reserved and not port_in_use(port):
            return port
    raise PortInUse(f"no free local port between {preferred} and {preferred + SEARCH_SPAN - 1}",
                    "pass --local-port with a free port")
