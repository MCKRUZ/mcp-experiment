"""
Test fixtures.

Two fixture tiers:
  - Stub (default): hub with fake tools, no subprocess. Tests CodeMode mechanics.
  - Live (--live flag): real stdio subprocesses for context7 and github.
    Requires npx on PATH and a valid GITHUB_PERSONAL_ACCESS_TOKEN.
"""

import os

import pytest
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.transports.stdio import StdioTransport
from fastmcp.experimental.transforms.code_mode import CodeMode


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run tests against live stdio MCP servers (slow, requires npx)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: requires --live flag and npx")


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if not config.getoption("--live"):
        skip = pytest.mark.skip(reason="requires --live flag")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip)


# ---------------------------------------------------------------------------
# Stub hub fixture (fast, no subprocess)
# Tests CodeMode mechanics: search, get_schemas, execute all work correctly.
# ---------------------------------------------------------------------------


def _build_stub_hub() -> FastMCP:
    """FastMCP server with fake context7 + github tools for unit testing."""
    stub = FastMCP("stub-hub")

    @stub.tool
    async def context7_resolve_library_id(libraryName: str) -> list[dict]:
        """Resolve a library name to its Context7 library ID."""
        return [{"id": f"/stub/{libraryName}/latest", "name": libraryName}]

    @stub.tool
    async def context7_get_library_docs(
        libraryId: str, topic: str = "", tokens: int = 1000
    ) -> str:
        """Fetch documentation for a library from Context7."""
        return f"Stub docs for {libraryId} — topic: {topic or 'general'}"

    @stub.tool
    async def github_search_repositories(query: str, perPage: int = 5) -> list[dict]:
        """Search GitHub repositories by keyword."""
        return [{"name": f"stub-{query}", "full_name": f"stub/{query}", "stars": 42}]

    @stub.tool
    async def github_list_pull_requests(
        owner: str, repo: str, state: str = "open"
    ) -> list[dict]:
        """List pull requests for a GitHub repository."""
        return [{"number": 1, "title": "Stub PR", "state": state}]

    stub.add_transform(CodeMode())
    return stub


@pytest.fixture(scope="function")
async def hub_client() -> Client:
    """Stub hub in-process. No subprocess startup. Tests CodeMode mechanics."""
    stub = _build_stub_hub()
    async with Client(stub) as client:
        yield client


# ---------------------------------------------------------------------------
# Live fixtures (slow, require npx + GITHUB_PAT)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
async def live_hub_client():
    """Real hub with context7 + github proxies. Requires npx."""
    import server  # noqa: PLC0415

    async with Client(server.mcp) as client:
        yield client


@pytest.fixture(scope="function")
async def context7_client():
    """Direct connection to context7 via stdio subprocess."""
    transport = StdioTransport(
        command="cmd",
        args=["/c", "npx", "-y", "@upstash/context7-mcp"],
    )
    async with Client(transport) as client:
        yield client


@pytest.fixture(scope="function")
async def github_client():
    """Direct connection to GitHub MCP via stdio subprocess."""
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    transport = StdioTransport(
        command="cmd",
        args=["/c", "npx", "-y", "@modelcontextprotocol/server-github"],
        env={**os.environ, "GITHUB_PERSONAL_ACCESS_TOKEN": token},
    )
    async with Client(transport) as client:
        yield client
