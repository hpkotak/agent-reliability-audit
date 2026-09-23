"""Exposes the shop tools to the agent over MCP (stdio).

The login session is set by the harness through environment variables, never by the model:
SHOP_DB (database path), SHOP_VERSION (v1 or v2), SHOP_CUSTOMER (logged-in customer id).
"""
import functools
import os
import traceback

from mcp.server.mcpserver import MCPServer

from shop.tools import TOOL_NAMES, VERSIONS

version = os.environ.get("SHOP_VERSION", "v1")
tools = VERSIONS[version](os.environ["SHOP_DB"], os.environ.get("SHOP_CUSTOMER", "C1"))
server = MCPServer("shop")


def recorded(fn):
    """A crashing tool is a bug in the test harness, not the agent. Record it so the run is marked
    as an error instead of being graded."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            tools.con.execute("INSERT INTO harness_errors (error) VALUES (?)", (traceback.format_exc(),))
            tools.con.commit()
            raise
    return wrapper


for name in TOOL_NAMES[version]:
    server.tool()(recorded(getattr(tools, name)))

if __name__ == "__main__":
    server.run()
