"""Entry point, so the server can be launched as `python -m atlas_mcp`.

That is the form an MCP client's configuration should use: it works without
installing a console script and without guessing where pip put one.
"""

from atlas_mcp.server import main

if __name__ == "__main__":
    main()
