import sys
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator
from mcp.server.fastmcp import FastMCP
from saltmdb.config import get_db_path
from saltmdb.daemon.client import SessionConnection

# Configure standard logging exclusively to stderr to protect MCP stdio stream
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """FastMCP lifespan context manager. Track B (scratch/plans/track_b_daemon_detailed.md §5/§8,
    5 rounds of Codex review): owns ONLY the SessionConnection's open/close -- a narrower, more
    accurate scope than the pre-Track-B version, which also handled direct DB/viewer-lifecycle
    calls (register_session/start_viewer/unregister_session/stop_viewer, all removed -- the
    daemon owns the Viewer and session bookkeeping now). Backend configuration
    (tools.configure_backend) is deliberately NOT done here -- it's a whole-process, startup-time
    concern handled once by __main__.py before mcp.run() is even called, not a per-lifespan one
    (round-3/round-4 correction: a contextvars.ContextVar tried to solve this here first and was
    the wrong primitive)."""
    session = SessionConnection(get_db_path())
    session.open()
    try:
        yield {}
    finally:
        # Codex round-1 finding: without try/finally, an exception raised anywhere during the
        # server's active lifetime (propagated back into this generator via athrow) would skip
        # session.close() entirely, leaking the persistent hello connection.
        session.close()
        logger.info("SALTMDB server shutting down.")


mcp = FastMCP("SALTMDB", lifespan=server_lifespan)

from saltmdb.mcp import tools  # noqa: F401, E402 -- registers @mcp.tool() decorators as a side effect
