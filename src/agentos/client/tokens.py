"""Where a connection's credentials live on this computer, and the helper that reads the agent's back.

Two credentials per connection, in two separate items:
- the agents' grant token (service `agentos`, account = connection name): what Claude Code and
  other MCP clients send. Connections made before the split hold the node's own token here;
- the owner's device credential (service `agentos-owner`): what `agentos dash`, `agentos
  approvals` and `agentos open` on this computer use. It is a panel session id, refused on /mcp.

macOS: generic passwords in the login Keychain, written with `security -i`, which reads the
command from stdin, so a credential is never on a command line that `ps` could show. Linux, or
`--token-store file`: chmod 600 files in chmod 700 directories (`tokens/` and `owner/`).

Claude Code gets the agent's token through `agentos-mcp-headers <name>`, its `headersHelper`
for the server: a short sh script that reads the `agentos` item and prints the Authorization
header as JSON. It never reads the owner's item. So the token is not on Claude Code's command
line or in ~/.claude.json, and a new grant needs no re-registration.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Protocol

from agentos.client.errors import TokenStoreError, scrub
from agentos.client.remote import TOKEN_RE
from agentos.client.state import Paths, shown, write_private

SERVICE = "agentos"
OWNER_SERVICE = "agentos-owner"
SECURITY_TIMEOUT = 30

DENIED_WORDS = ("user interaction is not allowed", "authorization", "denied", "not permitted",
                "user canceled", "-25293", "-128", "-61")


class TokenStore(Protocol):
    kind: str

    def get(self, name: str) -> str | None: ...

    def put(self, name: str, token: str) -> bool:
        """Store the token; True when it changed."""
        ...

    def delete(self, name: str) -> bool: ...

    def describe(self, name: str) -> str: ...

    def read_command(self, name: str) -> str:
        """A shell expression that prints the token, for lines other clients can use."""
        ...


def check_token(token: str) -> None:
    # The token goes into a `security -i` line and into JSON; only url-safe characters are allowed.
    if not TOKEN_RE.match(token):
        raise TokenStoreError("refusing to store a token with unexpected characters")


class KeychainStore:
    kind = "keychain"

    def __init__(self, security: str, service: str = SERVICE):
        self.security = security
        self.service = service

    def _run(self, args: list[str], *, stdin: str | None = None, secret: str = "") -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self.security, *args], check=False, input=stdin, capture_output=True, text=True,
                                  timeout=SECURITY_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise TokenStoreError("the Keychain did not answer in 30 seconds",
                                  "unlock it (or answer its dialog) and run the command again") from None
        except OSError as exc:
            raise TokenStoreError(scrub(f"could not run security: {exc}", secret)) from None

    def _denied(self, proc: subprocess.CompletedProcess, what: str, secret: str = "") -> TokenStoreError:
        detail = scrub((proc.stderr or proc.stdout).strip(), secret)
        last = detail.splitlines()[-1] if detail else f"exit {proc.returncode}"
        if any(word in detail.lower() for word in DENIED_WORDS):
            return TokenStoreError(
                f"the macOS Keychain refused to {what}: {last}",
                "unlock the login keychain (`security unlock-keychain`) and run the command again, or "
                "pass --token-store file to keep the token in a chmod 600 file instead")
        return TokenStoreError(f"could not {what} in the macOS Keychain: {last}")

    def get(self, name: str) -> str | None:
        proc = self._run(["find-generic-password", "-s", self.service, "-a", name, "-w"])
        if proc.returncode == 44:  # errSecItemNotFound
            return None
        if proc.returncode != 0:
            raise self._denied(proc, "read the token", proc.stdout.strip())
        return proc.stdout.strip() or None

    def put(self, name: str, token: str) -> bool:
        check_token(token)
        if self.get(name) == token:
            return False
        line = f"add-generic-password -U -s {self.service} -a {name} -w {token}\n"
        proc = self._run(["-i"], stdin=line, secret=token)
        if proc.returncode != 0 or "returned" in proc.stderr:
            raise self._denied(proc, "store the token", token)
        return True

    def delete(self, name: str) -> bool:
        proc = self._run(["delete-generic-password", "-s", self.service, "-a", name])
        if proc.returncode == 44:
            return False
        if proc.returncode != 0:
            raise self._denied(proc, "delete the token")
        return True

    def describe(self, name: str) -> str:
        return f"macOS Keychain (service {self.service}, account {name})"

    def read_command(self, name: str) -> str:
        return f"security find-generic-password -s {self.service} -a {name} -w"


class FileStore:
    kind = "file"

    def __init__(self, paths: Paths, *, owner: bool = False):
        self.dir = paths.owner_tokens_dir if owner else paths.tokens_dir

    def path(self, name: str) -> Path:
        return self.dir / name

    def get(self, name: str) -> str | None:
        path = self.path(name)
        try:
            return path.read_text().strip() or None
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TokenStoreError(f"could not read {path}: {exc.strerror}") from None

    def put(self, name: str, token: str) -> bool:
        check_token(token)
        if self.get(name) == token:
            return False
        try:
            write_private(self.path(name), token + "\n")
        except OSError as exc:
            raise TokenStoreError(f"could not write {self.path(name)}: {exc.strerror}") from None
        return True

    def delete(self, name: str) -> bool:
        path = self.path(name)
        if path.exists():
            path.unlink()
            return True
        return False

    def describe(self, name: str) -> str:
        return f"{shown(self.path(name))} (chmod 600)"

    def read_command(self, name: str) -> str:
        return f"cat {shlex.quote(str(self.path(name)))}"


HELPER = """\
#!/bin/sh
# agentos: print the MCP Authorization header for one connection, as JSON.
# Claude Code runs this as the server's headersHelper, so the token never sits
# in a config file or on a command line. Written by `agentos connect`.
name="${{1:-}}"
case "$name" in
  ''|*[!a-z0-9_-]*) echo "usage: agentos-mcp-headers <connection name>" >&2; exit 2 ;;
esac
file={tokens_dir}/"$name"
token=
if [ -f "$file" ]; then
  IFS= read -r token < "$file"
{keychain}fi
if [ -z "${{token:-}}" ]; then
  echo "agentos: no token for $name; run agentos connect again" >&2
  exit 1
fi
printf '{{"Authorization": "Bearer %s"}}\\n' "$token"
"""

KEYCHAIN_BRANCH = """\
else
  token=$({security} find-generic-password -s {service} -a "$name" -w 2>/dev/null)
"""


def helper_script(paths: Paths, security: str | None) -> str:
    keychain = KEYCHAIN_BRANCH.format(security=shlex.quote(security), service=SERVICE) if security else ""
    return HELPER.format(tokens_dir=shlex.quote(str(paths.tokens_dir)), keychain=keychain)


def install_helper(paths: Paths, security: str | None) -> Path:
    path = paths.headers_helper
    content = helper_script(paths, security)
    if not path.exists() or path.read_text() != content:
        write_private(path, content, mode=0o700)
    return path


def helper_command(paths: Paths, name: str) -> str:
    """The headersHelper string for Claude Code (run through a shell)."""
    return f"{shlex.quote(str(paths.headers_helper))} {name}"
