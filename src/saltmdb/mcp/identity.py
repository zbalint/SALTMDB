"""Per-adapter-process session identity binding (agent API redesign plan §4.5/§4.6).

One MCP adapter process serves exactly one agent session end-to-end (mcp/server.py's
server_lifespan owns exactly one SessionConnection for the process's life), so a module-level
singleton here is the correct primitive -- unlike the DAEMON, which is one shared process
serving many concurrent agent sessions, where a mutable global owner would cross-contaminate
requests from different sessions. That is why this lives in mcp/ (adapter-side) and is wired
only into RpcBackend (§4.5), never into daemon/dispatch.py's DISPATCH_TABLE or
DirectDispatchBackend -- the latter also runs INSIDE the daemon (its own RPC handler) and is
shared across sessions there, exactly the case this must not touch.

Phase 1 scope: bind and inject only. The hard failure on a first call with no owner_id at all
(§4.5's "this error text is load-bearing") lands in Phase 2 alongside the tools.py signature
rewrite -- landing it here would break every existing test that calls a tool without owner_id
(tests/test_mcp_tools.py, tests/test_tag_merge_tool.py), per the plan's explicit Phase 1/2 split.
"""

from __future__ import annotations


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
        self._host_session_id: str | None = None

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

    def bind_host_session_id(self, host_session_id: str | None) -> None:
        """Binds the host harness's own session id (§4.6), an opaque, advisory, may-dangle
        pointer -- SALTMDB stores it and never resolves it (transcript layouts are host-
        specific; resolving them is a bootstrap/CLI/hook concern per memory 9be9b3b0, not this
        module's). First non-empty value wins, same as owner_id, but mismatches are NOT an
        error here -- unlike owner_id there is no correctness invariant a rebind would violate
        (it's advisory-only), so silently keeping the first value is sufficient."""
        if host_session_id and self._host_session_id is None:
            self._host_session_id = host_session_id

    @property
    def host_session_id(self) -> str | None:
        return self._host_session_id

    def reset(self) -> None:
        """Test-only: clears bound state so each test starts from a clean adapter. Never called
        by production code -- a real adapter process has exactly one identity for its whole life."""
        self._owner_id = None
        self._host_session_id = None


# One instance per adapter process, by construction (module import happens once per process).
SESSION_IDENTITY = _SessionIdentity()
