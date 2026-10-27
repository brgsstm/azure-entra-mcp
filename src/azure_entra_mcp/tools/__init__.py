from fastmcp import FastMCP

from . import arm, graph


def register_all(mcp: FastMCP) -> None:
    graph.register(mcp)
    arm.register(mcp)
