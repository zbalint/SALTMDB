"""Real stdio transport fixture; arguments select observable lifecycle behaviours."""

import asyncio
import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("viewer-tools-test")


@mcp.tool()
async def search_tags(mode: str = "normal", delay: float = 0.0) -> str:
    """Report execution context, or exercise a slow, failing, or large response."""
    journal = Path(os.environ["VIEWER_TOOLS_JOURNAL"])
    with journal.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"tool": "search_tags", "mode": mode, "pid": os.getpid()}) + "\n")
    if delay:
        await asyncio.sleep(delay)
    if mode == "raise":
        raise ValueError("fixture rejection")
    if mode == "large":
        return "x" * 262145
    return json.dumps(
        {
            "cwd": os.getcwd(),
            "pid": os.getpid(),
            "agent_id": os.environ.get("SALTMDB_AGENT_ID"),
            "tags": ["fixture"],
        }
    )


@mcp.tool()
def store_memory(content: str) -> str:
    """Write-classified tool: execution is forbidden through the viewer."""
    with Path(os.environ["VIEWER_TOOLS_JOURNAL"]).open("a", encoding="utf-8") as stream:
        stream.write("store_memory\n")
    return content


@mcp.tool()
def mystery_tool() -> str:
    """Unclassified tool: execution is forbidden through the viewer."""
    with Path(os.environ["VIEWER_TOOLS_JOURNAL"]).open("a", encoding="utf-8") as stream:
        stream.write("mystery_tool\n")
    return "unknown"


if __name__ == "__main__":
    mcp.run(transport="stdio")
