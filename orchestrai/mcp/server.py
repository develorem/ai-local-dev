"""Stdio entry point for the OrchestrAi MCP server.

The tools live in orchestrai_mcp.py (shared with the hub-hosted HTTP mount).
This entry point talks to the hub over HTTP, so it can run anywhere with
Python 3.10+ and only needs `mcp`.

Run (Claude Code):
  claude mcp add orchestrai -- uv run --with mcp python /abs/path/to/server.py
Env:
  ORCHESTRAI_HUB_URL    default http://localhost:6724
  ORCHESTRAI_PROJECT_SLUG / _NAME   optional default project (else call use_project)
  ORCHESTRAI_TOKEN      optional bearer token (reserved; hub is unauthenticated today)

Prefer the hub-hosted HTTP endpoint when available — connect with just a URL:
  claude mcp add --transport http orchestrai http://localhost:6724/mcp
"""

from orchestrai_mcp import mcp

if __name__ == "__main__":
    mcp.run()
