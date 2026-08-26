"""Per-adapter-process identity configured at MCP adapter startup.

One MCP adapter process serves exactly one agent session end-to-end (mcp/server.py's
server_lifespan owns exactly one SessionConnection for the process's life), so a module-level
singleton here is the correct primitive -- unlike the DAEMON, which is one shared process
serving many concurrent agent sessions, where a mutable global owner would cross-contaminate
requests from different sessions. That is why this lives in mcp/ (adapter-side) and is wired
only into RpcBackend (§4.5), never into daemon/dispatch.py's DISPATCH_TABLE or
DirectDispatchBackend -- the latter also runs INSIDE the daemon (its own RPC handler) and is
shared across sessions there, exactly the case this must not touch.

The owner is supplied by ``SALTMDB_OWNER_ID`` in the harness's MCP server configuration, never by
an agent-facing tool argument. This holder owns the immutable configured identity and the minted
SALTMDB session ID.
"""

from __future__ import annotations

import os
import uuid6


class IdentityRebindRejected(Exception):
    """Raised if startup code attempts to configure two identities in one adapter process."""

    def __init__(self, bound: str, attempted: str) -> None:
        self.bound = bound
        self.attempted = attempted
        super().__init__(
            f"SALTMDB_OWNER_ID is already configured as '{bound}'; cannot reconfigure it as "
            f"'{attempted}' in the same MCP process."
        )


class _SessionIdentity:
    """Immutable owner/session identity for one MCP adapter process.

    The owner is configured once during process startup from ``SALTMDB_OWNER_ID``.  There is no
    first-tool-call binding or agent-controlled rebind path: every public MCP tool receives the
    already-configured identity through the private dispatch envelope.
    """

    def __init__(self) -> None:
        self._owner_id: str | None = None
        self.agent_session_id: str = str(uuid6.uuid7())
        self.cwd: str = os.path.realpath(os.getcwd())

    @property
    def owner_id(self) -> str | None:
        return self._owner_id

    def reset(self) -> None:
        """Test-only: clears bound state so each test starts from a clean adapter. Never called
        by production code -- a real adapter process has exactly one identity for its whole life."""
        self._owner_id = None
        self.agent_session_id = str(uuid6.uuid7())
        self.cwd = os.path.realpath(os.getcwd())

    def configure_owner(self, owner_id: str) -> None:
        """Configure the required owner exactly once during adapter startup.

        Validation is repeated here as a defense-in-depth measure for embedding applications and
        tests that call this startup hook directly instead of going through ``__main__``.
        """
        from saltmdb.config import validate_owner_id

        owner_id = validate_owner_id(owner_id)
        if self._owner_id is not None and self._owner_id != owner_id:
            raise IdentityRebindRejected(bound=self._owner_id, attempted=owner_id)
        self._owner_id = owner_id


# One instance per adapter process, by construction (module import happens once per process).
SESSION_IDENTITY = _SessionIdentity()
