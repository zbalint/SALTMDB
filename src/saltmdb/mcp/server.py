import sys
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator
from mcp.server.fastmcp import FastMCP
from saltmdb.config import get_db_path
from saltmdb.db.schema import init_db

# Configure standard logging exclusively to stderr to protect MCP stdio stream
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """FastMCP lifespan context manager to initialize database schema once at startup."""
    db_path = get_db_path()
    logger.info("Initializing SALTMDB database schema at: %s", db_path)
    conn = init_db(db_path)
    conn.close()
    yield {}
    logger.info("SALTMDB server shutting down.")

mcp = FastMCP("SALTMDB", lifespan=server_lifespan)
