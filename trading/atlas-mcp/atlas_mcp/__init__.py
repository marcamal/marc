"""atlas-mcp: the bridge between ATLAS and any MCP client.

Runs as its own process, talks to the ATLAS backend over localhost HTTP, and
exposes a deliberately asymmetric tool surface: read everything, evaluate a
hypothetical trade, engage the kill switch - and nothing else.

Read `atlas_mcp.permissions` first. That module is the security model.
"""

__version__ = "0.1.0"
