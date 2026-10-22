"""The platform's MCP server (`docs/superpowers/specs/2026-10-09-mcp-server-design.md`)."""

from app.mcp.invoke import InvokeOutcome
from app.mcp.server import McpMount, McpPrincipal, build_mcp

__all__ = ["InvokeOutcome", "McpMount", "McpPrincipal", "build_mcp"]
