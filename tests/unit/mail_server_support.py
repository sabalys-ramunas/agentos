"""A real IMAP and SMTP server in Docker for the tests that need one (tests/mail_server).

Container `openajar-mailtest`, on ports Docker picks, removed when the session ends. Nothing else is
touched. Without a working Docker the tests skip.
"""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

NAME = "openajar-mailtest"
IMAGE = "openajar-mailtest:latest"
CONTEXT = Path(__file__).parent.parent / "mail_server"
HOST = "127.0.0.1"
OWNER, PASSWORD = "owner@example.test", "right-password-1"
FRIEND, FRIEND_PASSWORD = "friend@example.test", "friend-password-2"
UTF8, UTF8_PASSWORD = "utf8@example.test", "pässwörd-3"


def docker(*args: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=timeout)


def docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return docker("version", "--format", "{{.Server.Version}}", check=False, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass
class MailServer:
    ports: dict[int, int]
    ca: Path

    def tls(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.ca))

    def exec(self, *args: str) -> str:
        return docker("exec", NAME, *args, timeout=60).stdout

    def set_uidvalidity(self, user: str, value: int) -> None:
        self.exec("doveadm", "mailbox", "update", "-u", user, "--uid-validity", str(value), "INBOX")


def _port(container_port: int) -> int:
    return int(docker("port", NAME, f"{container_port}/tcp").stdout.strip().splitlines()[0].rsplit(":", 1)[1])


def _answers(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), 1):
            return True
    except OSError:
        return False


def start(ca_dir: Path) -> Iterator[MailServer]:
    if not docker_works():
        pytest.skip("Docker is not available here")
    docker("rm", "-f", NAME, check=False)
    docker("build", "-q", "-t", IMAGE, str(CONTEXT))
    docker("run", "-d", "--name", NAME, "-p", f"{HOST}::143", "-p", f"{HOST}::993", "-p", f"{HOST}::465",
           "-p", f"{HOST}::587", IMAGE)
    try:
        ports = {p: _port(p) for p in (143, 993, 465, 587)}
        for _ in range(120):
            if docker("exec", NAME, "test", "-S", "/var/spool/postfix/private/auth", check=False).returncode == 0 and \
                    docker("exec", NAME, "postfix", "status", check=False).returncode == 0 and \
                    all(_answers(p) for p in ports.values()):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(f"the mail server did not start: {docker('logs', NAME, check=False).stderr[-800:]}")
        ca = ca_dir / "ca.crt"
        docker("cp", f"{NAME}:/etc/ssl/mailtest/ca.crt", str(ca))
        yield MailServer(ports, ca)
    finally:
        docker("rm", "-f", NAME, check=False)


class StrippingProxy:
    """Someone between the node and the server who deletes STARTTLS from what the server says, and
    keeps what the client sends, to show what would have reached them."""

    def __init__(self, port: int):
        self.target = port
        self.seen = bytearray()
        self.listener = socket.create_server((HOST, 0))
        self.port = self.listener.getsockname()[1]
        self.running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while self.running:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            server = socket.create_connection((HOST, self.target), 5)
            threading.Thread(target=self._pipe, args=(server, client, True), daemon=True).start()
            threading.Thread(target=self._pipe, args=(client, server, False), daemon=True).start()

    def _pipe(self, src: socket.socket, dst: socket.socket, strip: bool) -> None:
        try:
            while data := src.recv(4096):
                if strip:
                    data = data.replace(b"STARTTLS", b"XSTARTLS")
                else:
                    self.seen += data
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.close()
                except OSError:
                    pass

    def close(self) -> None:
        self.running = False
        self.listener.close()
