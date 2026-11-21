"""The credentials `openajar connect` sets up with the node token (in memory only): the owner's device
credential, then the connection's agent grant, kept, issued or replaced. A mixin of Client."""

from __future__ import annotations

from dataclasses import dataclass

from openajar.client.choices import (
    DEFAULT_AGENT_TTL,
    DEFAULT_CEILING,
    DEFAULT_CLIENT,
    DEFAULT_SPECIALS,
    NEWER_SPECIALS,
    AgentOptions,
    ConnectResult,
    computer_name,
)
from openajar.client.errors import ClientError, VerifyError
from openajar.client.mcp_http import PROBE_TIMEOUT, McpError, probe
from openajar.client.owner import NodePanel, PanelError
from openajar.client.remote import NodeSettings
from openajar.client.state import (
    Connection,
)
from openajar.client.tokens import (
    TokenStore,
)


class _quiet:
    """Ignore a PanelError in a clean-up step that must not fail the command."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind: type[BaseException] | None, exc: BaseException | None, tb: object) -> bool:
        return kind is not None and issubclass(kind, PanelError)


@dataclass
class _Connecting:
    """What one `openajar connect` works with while it sets up the connection's credentials."""
    conn: Connection
    previous: Connection | None
    node: NodeSettings
    panel: NodePanel
    old_agent: str | None  # the agent token this computer held for the connection
    old_device: str | None  # and the owner's device credential

    @property
    def was_grant(self) -> bool:
        return self.previous is not None and self.previous.credential == "agent_grant"


class Credentials:
    """Client's credential steps (it reads self.out and self._answers)."""

    @staticmethod
    def _forget_agent(conn: Connection, credential: str) -> None:
        conn.credential = credential  # type: ignore[assignment]
        for key in ("agent_grant_id", "agent_subject", "agent_ceiling", "agent_expires_at", "owner_device_id",
                    "owner_device_expires_at"):
            setattr(conn, key, None)
        conn.agent_specials = []

    def _credentials(self, conn: Connection, previous: Connection | None, node: NodeSettings, tokens: TokenStore,
                     owner: TokenStore, held: tuple[str | None, str | None], agent: AgentOptions,
                     result: ConnectResult) -> None:
        """With the node token (in memory only): the owner's device credential, then the agent grant.
        A node that has neither route (older openajar, or no policy) keeps the node token, with a note."""
        c = _Connecting(conn, previous, node, NodePanel(conn.local_port), *held)
        device, minted = self._held_device(c), None
        if device is None:
            try:
                minted = c.panel.mint_device(node.token, label=computer_name())
            except PanelError as exc:
                if not exc.missing:
                    raise
                return self._keep_node_token(conn, node, tokens, result, exc)
            device = minted["session"]
            conn.owner_device_id, conn.owner_device_expires_at = minted.get("id"), minted.get("expires_at")
        subject, ceiling, specials = self._agent_terms(c, agent)
        if self._grant_kept(c, agent, subject, ceiling, specials):
            token = c.old_agent
            self.out(f"Agent grant {conn.agent_grant_id} kept: {subject}, every lane, up to {ceiling}")
        else:
            try:
                token = self._issue_grant(c, agent, subject, ceiling, specials, result)
            except PanelError as exc:
                if minted:
                    with _quiet():
                        c.panel.end_device(device)
                if not exc.missing:
                    raise
                return self._keep_node_token(conn, node, tokens, result, exc)
        self._store_and_verify(c, tokens, owner, token, device, minted, result)
        self._after_grant(c, device, result)

    @staticmethod
    def _held_device(c: _Connecting) -> str | None:
        """The owner's device credential this computer held, while the node still knows it."""
        if not (c.old_device and c.was_grant):
            return None
        try:
            c.panel.whoami(c.old_device)
        except PanelError as exc:
            if exc.status not in (401, 403):
                raise
            return None
        return c.old_device

    @staticmethod
    def _agent_terms(c: _Connecting, agent: AgentOptions) -> tuple[str, str, list[str]]:
        """Who the agent grant is for, its ceiling and its specials: what was asked, else what the
        connection had, else the defaults."""
        before = c.previous if c.was_grant else None
        was_client = (before.agent_subject or "").split("@")[0] if before else ""
        subject = f"{agent.client or was_client or DEFAULT_CLIENT}@{computer_name()}"
        ceiling = agent.ceiling or (before.agent_ceiling if before else None) or DEFAULT_CEILING
        specials = sorted(agent.specials if agent.specials is not None else
                          (before.agent_specials if before else DEFAULT_SPECIALS))
        return subject, ceiling, specials

    def _grant_kept(self, c: _Connecting, agent: AgentOptions, subject: str, ceiling: str,
                    specials: list[str]) -> bool:
        """Whether the grant this computer holds already is the one asked for, and the node takes it."""
        before = c.previous
        if not (c.was_grant and c.old_agent and before is not None) or agent.new_grant or agent.ttl is not None:
            return False
        same = (before.agent_subject == subject and before.agent_ceiling == ceiling
                and sorted(before.agent_specials) == specials)
        return same and self._answers(c.conn.local_port, c.old_agent)

    def _issue_grant(self, c: _Connecting, agent: AgentOptions, subject: str, ceiling: str, specials: list[str],
                     result: ConnectResult) -> str:
        """A new agent grant from the node, recorded on the connection; its token."""
        ttl = agent.ttl or DEFAULT_AGENT_TTL
        try:
            issued = c.panel.issue_agent_grant(c.node.token, subject=subject, ceiling=ceiling, specials=specials,
                                               ttl=ttl)
        except PanelError as exc:
            # a node older than these names refuses them: issue without them, as before
            refused = [s for s in NEWER_SPECIALS if s in specials and s in exc.message]
            if agent.specials is not None or not refused:
                raise
            specials = [s for s in specials if s not in refused]
            issued = c.panel.issue_agent_grant(c.node.token, subject=subject, ceiling=ceiling, specials=specials,
                                               ttl=ttl)
        conn = c.conn
        conn.agent_grant_id = issued.get("id")
        conn.agent_expires_at = (issued.get("grant") or {}).get("expires_at")
        conn.agent_subject, conn.agent_ceiling, conn.agent_specials = subject, ceiling, specials
        result.grant_issued = True
        until = f", until {conn.agent_expires_at[:10]}" if conn.agent_expires_at else ""
        extra = f", specials {', '.join(specials)}" if specials else ", no specials"
        self.out(f"Agent grant {conn.agent_grant_id} issued for {subject}: every lane, up to {ceiling}{extra}{until}")
        return issued["token"]

    def _store_and_verify(self, c: _Connecting, tokens: TokenStore, owner: TokenStore, token: str, device: str,
                          minted: dict | None, result: ConnectResult) -> None:
        """Keep both credentials on this computer and check the node takes the agent's. When that
        fails, nothing new stays behind on the node."""
        conn, name = c.conn, c.conn.name
        try:
            result.token_changed = tokens.put(name, token)
            self.out(f"Token {'stored in' if result.token_changed else 'already in'} {tokens.describe(name)}")
            stored = owner.put(name, device)
            self.out(f"Owner credential {'stored in' if stored else 'already in'} {owner.describe(name)}")
            self.out("  openajar dash, openajar approvals and openajar open use it; MCP refuses it")
            try:
                probe(conn.local_port, token, timeout=PROBE_TIMEOUT)
            except McpError as exc:
                raise VerifyError(f"the node did not accept the agent grant it issued ({exc.detail})",
                                  f"run `openajar connect {conn.ssh_host} --name {name} --new-grant`") from None
        except ClientError:
            if result.grant_issued and conn.agent_grant_id:
                with _quiet():
                    c.panel.call("POST", f"/api/grants/{conn.agent_grant_id}/revoke", {}, bearer=c.node.token)
            if minted:
                with _quiet():
                    c.panel.end_device(device)
            raise

    def _after_grant(self, c: _Connecting, device: str, result: ConnectResult) -> None:
        """The connection holds its own grant now: the node learns the route back here, and the
        grant and device credential this computer held before are ended."""
        conn, previous, panel = c.conn, c.previous, c.panel
        conn.credential = "agent_grant"
        if conn.agent_grant_id:
            # browser_view then gives this computer's agents a link and tunnel that work here (best effort:
            # a node older than this answers 404 and keeps its own words)
            with _quiet():
                panel.record_route(c.node.token, conn.agent_grant_id, ssh_host=conn.ssh_host,
                                   local_port=conn.local_port)
        self.out("  the node accepted the agent grant; its own token was used only for these calls "
                 "and is not stored here")
        if result.grant_issued and c.was_grant and previous and previous.agent_grant_id \
                and previous.agent_grant_id != conn.agent_grant_id:
            self._revoke_previous(panel, device, previous.agent_grant_id, result)
        if c.old_device and c.old_device != device:
            with _quiet():
                panel.end_device(c.old_device)
        if previous is not None and not c.was_grant:
            self.out("  moved from the node token to its own grant; rotate the node token once every "
                     f"connection is moved: `ssh {conn.ssh_host} openajar node rotate-token`")

    def _revoke_previous(self, panel: NodePanel, device: str, grant_id: str, result: ConnectResult) -> None:
        try:
            panel.revoke_grant(device, grant_id)
            self.out(f"  revoked the previous agent grant {grant_id}")
        except PanelError as exc:
            if exc.status not in (404, 409):
                result.notes.append(f"could not revoke the previous agent grant {grant_id}: {exc.message}; "
                                    f"revoke it on the node with `openajar grants revoke {grant_id}`")

    def _keep_node_token(self, conn: Connection, node: NodeSettings, tokens: TokenStore, result: ConnectResult,
                         why: PanelError) -> None:
        result.token_changed = tokens.put(conn.name, node.token)
        self._forget_agent(conn, "node_token")
        self.out(f"Token {'stored in' if result.token_changed else 'already in'} {tokens.describe(conn.name)}")
        result.notes.append(
            f"{conn.ssh_host} cannot issue agent grants yet ({why.message}), so this connection holds the node "
            "token and you cannot approve from here what its agents ask. Update openajar on the node, then run "
            "`openajar connect --upgrade`.")
