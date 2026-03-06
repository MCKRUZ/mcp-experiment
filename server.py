"""
FastMCP 3.1 CodeMode hub.

Aggregates Context7 and GitHub MCP behind a single CodeMode interface.
Claude Code connects here instead of maintaining separate connections to each server.

Flow: Claude Code → search/get_schemas/execute → downstream tools (namespaced)
  - context7_resolve-library-id, context7_get-library-docs
  - github_create_issue, github_list_prs, github_search_code, ...

Note: add_transform() mutates FastMCP in-place (makes hub itself the runnable server).
      wrap_transform() returns a _WrappedProvider which is NOT runnable by fastmcp CLI.
"""

import os

from fastmcp import FastMCP
from fastmcp.client.transports.stdio import StdioTransport
from fastmcp.experimental.transforms.code_mode import CodeMode
from fastmcp.server import create_proxy

# --- Downstream proxies ---

context7 = create_proxy(
    StdioTransport(
        command="cmd",
        args=["/c", "npx", "-y", "@upstash/context7-mcp"],
    )
)

github = create_proxy(
    StdioTransport(
        command="cmd",
        args=["/c", "npx", "-y", "@modelcontextprotocol/server-github"],
        env={
            **os.environ,
            "GITHUB_PERSONAL_ACCESS_TOKEN": os.environ.get(
                "GITHUB_PERSONAL_ACCESS_TOKEN", ""
            ),
        },
    )
)

# --- Hub (FastMCP instance — runnable by fastmcp CLI) ---

mcp = FastMCP("mcp-hub")
mcp.mount(context7, namespace="context7")
mcp.mount(github, namespace="github")

# Mutates mcp in-place — mcp stays a FastMCP instance, runnable by fastmcp CLI
mcp.add_transform(CodeMode())

if __name__ == "__main__":
    mcp.run()
