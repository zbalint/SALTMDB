import asyncio
import os
import sys
import atexit
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator
from mcp.server.fastmcp import FastMCP
from saltmdb import config
from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection
from saltmdb.db.schema import init_db
from saltmdb.db.viewer_sessions import (
    count_live_sessions,
    register_session,
    unregister_session,
)
from saltmdb.viewer.server import start_viewer, stop_viewer

# Configure standard logging exclusively to stderr to protect MCP stdio stream
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _cleanup_session_on_exit() -> None:
    try:
        if config.is_viewer_enabled():
            db_path = get_db_path()
            if os.path.exists(db_path):
                conn = get_connection(db_path)
                try:
                    viewer_port = config.get_viewer_port()
                    unregister_session(conn, viewer_port)
                    remaining = count_live_sessions(conn, viewer_port)
                    if remaining == 0:
                        stop_viewer(port=viewer_port)
                finally:
                    conn.close()
    except Exception as e:
        logger.debug("Error in atexit session cleanup: %s", e)


atexit.register(_cleanup_session_on_exit)


def _log_viewer_start_result(task: "asyncio.Task[str]") -> None:
    try:
        result = task.result()
    except asyncio.CancelledError:
        return
    if result.startswith("Error"):
        logger.warning(result)
    else:
        logger.info(result)


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """FastMCP lifespan context manager to initialize database schema and auto-start the web viewer."""
    db_path = get_db_path()
    logger.info("Initializing SALTMDB database schema at: %s", db_path)
    conn = init_db(db_path)

    viewer_port = config.get_viewer_port()
    if config.is_viewer_enabled():
        register_session(conn, viewer_port)
        viewer_task = asyncio.create_task(asyncio.to_thread(start_viewer, viewer_port))
        viewer_task.add_done_callback(_log_viewer_start_result)

    conn.close()
    yield {}

    if config.is_viewer_enabled():
        conn = get_connection(db_path)
        unregister_session(conn, viewer_port)
        remaining = count_live_sessions(conn, viewer_port)
        conn.close()
        if remaining == 0:
            stop_viewer(port=viewer_port)
    logger.info("SALTMDB server shutting down.")


mcp = FastMCP("SALTMDB", lifespan=server_lifespan)

from saltmdb.mcp import tools  # noqa: F401, E402 -- registers @mcp.tool() decorators as a side effect

