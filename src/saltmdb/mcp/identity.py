"""Per-adapter-process session identity binding (agent API redesign plan §4.5/§4.6).

One MCP adapter process serves exactly one agent session end-to-end (mcp/server.py's
server_lifespan owns exactly one SessionConnection for the process's life), so a module-level
singleton here is the correct primitive -- unlike the DAEMON, which is one shared process
serving many concurrent agent sessions, where a mutable global owner would cross-contaminate
requests from different sessions. That is why this lives in mcp/ (adapter-side) and is wired
only into RpcBackend (§4.5), never into daemon/dispatch.py's DISPATCH_TABLE or
DirectDispatchBackend -- the latter also runs INSIDE the daemon (its own RPC handler) and is
shared across sessions there, exactly the case this must not touch.

The tool boundary performs the first-call hard failure (including its corrected-call guidance),
while this holder remains deliberately small: it only owns the immutable bind/inject state.
Keeping validation in tools.py also means DirectDispatchBackend tests exercise the same contract
without contaminating the shared daemon dispatch path.
"""

from __future__ import annotations

import uuid6


class IdentityRebindRejected(Exception):
    """Raised when a second, different owner_id arrives on an already-bound adapter connection.
    Binding is immutable for the connection's life (§4.5) -- a genuinely different identity
    requires a new MCP connection (a new adapter process), not a mid-session switch."""

    def __init__(self, bound: str, attempted: str) -> None:
        self.bound = bound
        self.attempted = attempted
        super().__init__(
            f"owner_id is already bound to '{bound}' for this session; cannot rebind to "
            f"'{attempted}'. Start a new MCP connection to use a different owner_id."
        )


class _SessionIdentity:
    """Per-adapter-process owner binding. One MCP adapter serves exactly one agent session,
    so process-local state is correct here -- unlike in the daemon, which is shared."""

    def __init__(self) -> None:
        self._owner_id: str | None = None
        self.agent_session_id: str = str(uuid6.uuid7())

    def bind(self, owner_id: str | None) -> None:
        """Binds the session's owner_id from the first call that supplies one. A falsy value
        is a no-op (nothing to bind yet). A later call with a DIFFERENT non-falsy value raises
        IdentityRebindRejected; the identical value again is always a no-op."""
        if not owner_id:
            return
        if self._owner_id is not None and self._owner_id != owner_id:
            raise IdentityRebindRejected(bound=self._owner_id, attempted=owner_id)
        self._owner_id = owner_id

    @property
    def owner_id(self) -> str | None:
        return self._owner_id

    def reset(self) -> None:
        """Test-only: clears bound state so each test starts from a clean adapter. Never called
        by production code -- a real adapter process has exactly one identity for its whole life."""
        self._owner_id = None
        self.agent_session_id = str(uuid6.uuid7())


# One instance per adapter process, by construction (module import happens once per process).
SESSION_IDENTITY = _SessionIdentity()
