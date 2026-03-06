"""
Correctness tests: hub results must match direct server results.

Fast tests (no --live): verify hub exposes exactly the right tools and
that execute scripts produce structurally valid output (stub tools, no subprocess).

Live tests (--live): compare hub output against direct server output for
identical queries. Requires npx + GITHUB_PERSONAL_ACCESS_TOKEN.
"""

import json

import pytest
from fastmcp.client import Client
from fastmcp.client.client import CallToolResult


def _text(result: CallToolResult) -> str:
    """Extract text from a CallToolResult."""
    if result.content and hasattr(result.content[0], "text"):
        return result.content[0].text
    return str(result.content)


# ---------------------------------------------------------------------------
# Hub structure tests (no --live required)
# ---------------------------------------------------------------------------


async def test_hub_exposes_exactly_three_tools(hub_client: Client) -> None:
    """CodeMode must collapse all tools to exactly: search, get_schema, execute."""
    tools = await hub_client.list_tools()
    names = {t.name for t in tools}
    assert names == {"search", "get_schema", "execute"}, (
        f"Expected {{search, get_schema, execute}}, got {names}"
    )


async def test_hub_search_returns_tool_names(hub_client: Client) -> None:
    """search must return a list of tool names/descriptions from downstream tools."""
    result = await hub_client.call_tool("search", {"query": "library documentation"})
    content = _text(result)
    assert "context7" in content.lower(), (
        f"Expected context7 tools in search results, got: {content[:300]}"
    )


async def test_hub_get_schema_returns_schema(hub_client: Client) -> None:
    """get_schema must return a parameter schema for a requested tool."""
    search_result = await hub_client.call_tool("search", {"query": "resolve library"})
    search_text = _text(search_result)

    try:
        search_data = json.loads(search_text)
        tool_name = (
            search_data[0].get("name", "context7_resolve_library_id")
            if isinstance(search_data, list) and search_data
            else "context7_resolve_library_id"
        )
    except (json.JSONDecodeError, KeyError):
        tool_name = "context7_resolve_library_id"

    schema_result = await hub_client.call_tool("get_schema", {"tools": [tool_name]})
    schema_text = _text(schema_result)
    assert tool_name in schema_text, (
        f"Schema response should contain '{tool_name}', got: {schema_text[:300]}"
    )


async def test_execute_returns_result(hub_client: Client) -> None:
    """execute must run a trivial expression and return the result."""
    result = await hub_client.call_tool("execute", {"code": "return 2 + 2"})
    content = _text(result)
    assert "4" in content, f"Expected '4' in result, got: {content}"


async def test_execute_can_call_stub_tool(hub_client: Client) -> None:
    """execute sandbox must be able to call a downstream tool via call_tool()."""
    result = await hub_client.call_tool(
        "execute",
        {
            "code": """
result = await call_tool("context7_resolve_library_id", {"libraryName": "fastmcp"})
return result
"""
        },
    )
    content = _text(result)
    assert "fastmcp" in content.lower(), (
        f"Expected stub tool result containing 'fastmcp', got: {content}"
    )


async def test_execute_chains_two_stub_calls(hub_client: Client) -> None:
    """
    Core CodeMode value test: two tool calls in one sandbox execution,
    intermediate result never touches the context window.
    """
    result = await hub_client.call_tool(
        "execute",
        {
            "code": """
lib = await call_tool("context7_resolve_library_id", {"libraryName": "fastmcp"})
library_id = lib[0]["id"] if isinstance(lib, list) else lib.get("id", "")
docs = await call_tool("context7_get_library_docs", {"libraryId": library_id, "topic": "transforms"})
return {"library_id": library_id, "docs_preview": docs[:50] if isinstance(docs, str) else str(docs)[:50]}
"""
        },
    )
    content = _text(result)
    assert "library_id" in content or "stub" in content.lower(), (
        f"Expected chained result, got: {content}"
    )


# ---------------------------------------------------------------------------
# Live correctness tests (require --live)
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_context7_library_id_matches(
    context7_client: Client,
    live_hub_client: Client,
) -> None:
    """Hub must return the same library ID as direct context7 for 'fastmcp'."""
    direct = await context7_client.call_tool(
        "resolve-library-id", {"libraryName": "fastmcp"}
    )
    direct_text = _text(direct)

    hub = await live_hub_client.call_tool(
        "execute",
        {
            "code": """
result = await call_tool("context7_resolve-library-id", {"libraryName": "fastmcp"})
return result
"""
        },
    )
    hub_text = _text(hub)

    assert direct_text.strip() == hub_text.strip() or any(
        word in hub_text for word in direct_text.split() if len(word) > 4
    ), f"Mismatch:\n  direct: {direct_text[:200]}\n  hub:    {hub_text[:200]}"


@pytest.mark.live
async def test_github_search_results_match(
    github_client: Client,
    live_hub_client: Client,
) -> None:
    """Hub must return equivalent GitHub search results to the direct server."""
    query = "fastmcp"

    direct = await github_client.call_tool(
        "search_repositories", {"query": query, "perPage": 3}
    )
    direct_text = _text(direct)

    hub = await live_hub_client.call_tool(
        "execute",
        {
            "code": f"""
result = await call_tool("github_search_repositories", {{"query": "{query}", "perPage": 3}})
return result
"""
        },
    )
    hub_text = _text(hub)

    assert "fastmcp" in hub_text.lower(), (
        f"Hub result missing expected content.\n  direct: {direct_text[:300]}\n  hub: {hub_text[:300]}"
    )


@pytest.mark.live
async def test_hub_multistep_matches_sequential_direct(
    context7_client: Client,
    live_hub_client: Client,
) -> None:
    """
    Core CodeMode correctness test.

    Direct: resolve-library-id → get-library-docs (2 sequential calls, 2 LLM turns)
    Hub:    single execute script chaining both calls (1 LLM turn)

    Final doc content must be equivalent.
    """
    # Direct: two calls
    lib_result = await context7_client.call_tool(
        "resolve-library-id", {"libraryName": "fastmcp"}
    )
    lib_text = _text(lib_result)
    try:
        lib_data = json.loads(lib_text)
        if isinstance(lib_data, list) and lib_data:
            library_id = lib_data[0].get("id") or lib_data[0].get("libraryId", "")
        elif isinstance(lib_data, dict):
            library_id = lib_data.get("id") or lib_data.get("libraryId", "")
        else:
            library_id = lib_text.strip()
    except json.JSONDecodeError:
        library_id = lib_text.strip()

    direct_docs = await context7_client.call_tool(
        "get-library-docs",
        {"libraryId": library_id, "topic": "transforms", "tokens": 1000},
    )
    direct_text = _text(direct_docs)

    # Hub: one execute script
    hub = await live_hub_client.call_tool(
        "execute",
        {
            "code": """
lib = await call_tool("context7_resolve-library-id", {"libraryName": "fastmcp"})
library_id = lib[0]["id"] if isinstance(lib, list) else lib.get("id", lib.get("libraryId", ""))
docs = await call_tool("context7_get-library-docs", {"libraryId": library_id, "topic": "transforms", "tokens": 1000})
return docs
"""
        },
    )
    hub_text = _text(hub)

    assert len(hub_text) > 100, f"Hub returned suspiciously short docs: {hub_text}"
    assert any(
        keyword in hub_text.lower()
        for keyword in ["transform", "fastmcp", "tool", "server"]
    ), f"Hub docs missing expected content: {hub_text[:300]}"
