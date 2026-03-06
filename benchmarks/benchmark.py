"""
Benchmarks measuring what FastMCP CodeMode actually saves.

Metrics:
  1. Schema tokens   -- tokens in list_tools() response (hub: ~3 tools vs direct: many)
  2. Round-trip count -- tool calls needed per multi-step task (hub: 1 vs direct: N)
  3. Latency         -- wall-clock time per task

Run (fast, in-process stub hub -- no npx required):
    uv run python -m benchmarks.benchmark

Run (live, real stdio servers -- requires npx + GITHUB_PAT):
    uv run python -m benchmarks.benchmark --live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import tiktoken
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.transports.stdio import StdioTransport
from fastmcp.experimental.transforms.code_mode import CodeMode


# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_enc.encode(text))


def tools_to_token_count(tools: list[Any]) -> int:
    serialized = json.dumps(
        [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.inputSchema,
            }
            for t in tools
        ],
        indent=2,
    )
    return count_tokens(serialized)


# ---------------------------------------------------------------------------
# Stub hub (fast mode -- no subprocess)
# ---------------------------------------------------------------------------


def _build_stub_hub() -> FastMCP:
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
        return f"Stub docs for {libraryId} -- topic: {topic or 'general'}"

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


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class SchemaResult:
    label: str
    tool_count: int
    token_count: int


@dataclass
class TaskResult:
    label: str
    task: str
    tool_calls: int
    elapsed_ms: float
    success: bool
    error: str = ""


@dataclass
class BenchmarkReport:
    mode: str
    schema_results: list[SchemaResult] = field(default_factory=list)
    task_results: list[TaskResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Schema overhead benchmark
# ---------------------------------------------------------------------------


async def benchmark_schema_overhead(
    hub_client: Client,
    context7_client: Client | None,
    github_client: Client | None,
) -> list[SchemaResult]:
    results = []

    hub_tools = await hub_client.list_tools()
    results.append(SchemaResult(
        label="Hub (CodeMode)",
        tool_count=len(hub_tools),
        token_count=tools_to_token_count(hub_tools),
    ))

    if context7_client:
        c7_tools = await context7_client.list_tools()
        results.append(SchemaResult(
            label="Context7 (direct)",
            tool_count=len(c7_tools),
            token_count=tools_to_token_count(c7_tools),
        ))

    if github_client:
        gh_tools = await github_client.list_tools()
        results.append(SchemaResult(
            label="GitHub (direct)",
            tool_count=len(gh_tools),
            token_count=tools_to_token_count(gh_tools),
        ))

    if context7_client and github_client:
        combined_tools = (
            await context7_client.list_tools() + await github_client.list_tools()
        )
        results.append(SchemaResult(
            label="Context7 + GitHub combined (direct)",
            tool_count=len(combined_tools),
            token_count=tools_to_token_count(combined_tools),
        ))

    return results


# ---------------------------------------------------------------------------
# Task benchmarks
# ---------------------------------------------------------------------------


async def benchmark_multistep_task(
    hub_client: Client,
    context7_client: Client | None,
    tool_prefix: str = "context7_",
) -> list[TaskResult]:
    """
    Task: resolve library ID then fetch docs.
    Direct: 2 sequential tool calls.
    Hub:    1 execute call (sandbox chains both).
    """
    task = "resolve library ID -> fetch docs"
    results = []

    # Hub: single execute call
    tool_resolve = f"{tool_prefix}resolve_library_id"
    tool_docs = f"{tool_prefix}get_library_docs"

    hub_code = f"""
lib = await call_tool("{tool_resolve}", {{"libraryName": "fastmcp"}})
library_id = lib[0]["id"] if isinstance(lib, list) else lib.get("id", "")
docs = await call_tool("{tool_docs}", {{"libraryId": library_id, "topic": "transforms", "tokens": 500}})
return docs
"""
    start = time.perf_counter()
    try:
        await hub_client.call_tool("execute", {"code": hub_code})
        elapsed = (time.perf_counter() - start) * 1000
        results.append(TaskResult(
            label="Hub (CodeMode) -- 1 execute call",
            task=task,
            tool_calls=1,
            elapsed_ms=elapsed,
            success=True,
        ))
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        results.append(TaskResult(
            label="Hub (CodeMode) -- 1 execute call",
            task=task,
            tool_calls=1,
            elapsed_ms=elapsed,
            success=False,
            error=str(e)[:120],
        ))

    if context7_client:
        start = time.perf_counter()
        try:
            lib_result = await context7_client.call_tool(
                "resolve-library-id", {"libraryName": "fastmcp"}
            )
            lib_text = lib_result.content[0].text if lib_result.content else ""
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

            await context7_client.call_tool(
                "get-library-docs",
                {"libraryId": library_id, "topic": "transforms", "tokens": 500},
            )
            elapsed = (time.perf_counter() - start) * 1000
            results.append(TaskResult(
                label="Context7 direct -- 2 sequential calls",
                task=task,
                tool_calls=2,
                elapsed_ms=elapsed,
                success=True,
            ))
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            results.append(TaskResult(
                label="Context7 direct -- 2 sequential calls",
                task=task,
                tool_calls=2,
                elapsed_ms=elapsed,
                success=False,
                error=str(e)[:120],
            ))

    return results


# ---------------------------------------------------------------------------
# Live task benchmark (real context7 v2.1.3 tool names + params)
# ---------------------------------------------------------------------------


async def benchmark_multistep_task_live(
    hub_client: Client,
    context7_client: Client,
) -> list[TaskResult]:
    """
    Real context7 v2.1.3:
      - resolve-library-id  (param: query)
      - query-docs          (param: libraryId)
    Namespaced in hub as: context7_resolve-library-id, context7_query-docs
    """
    task = "resolve library ID -> fetch docs"
    results = []

    # Hub: 1 execute call chains both
    # context7 v2.1.3: resolve-library-id requires {query, libraryName}; query-docs requires {libraryId, query}
    # Monty sandbox: call_tool returns deserialized Python objects, no json import needed
    hub_code = """
lib = await call_tool("context7_resolve-library-id", {"query": "transforms", "libraryName": "fastmcp"})
library_id = lib[0]["id"] if isinstance(lib, list) and lib else "/jlowin/fastmcp/latest"
docs = await call_tool("context7_query-docs", {"libraryId": library_id, "query": "transforms"})
return docs
"""
    start = time.perf_counter()
    try:
        await hub_client.call_tool("execute", {"code": hub_code})
        elapsed = (time.perf_counter() - start) * 1000
        results.append(TaskResult(
            label="Hub (CodeMode) -- 1 execute call",
            task=task, tool_calls=1, elapsed_ms=elapsed, success=True,
        ))
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        results.append(TaskResult(
            label="Hub (CodeMode) -- 1 execute call",
            task=task, tool_calls=1, elapsed_ms=elapsed, success=False,
            error=str(e)[:120],
        ))

    # Direct: 2 sequential calls
    start = time.perf_counter()
    try:
        lib_result = await context7_client.call_tool(
            "resolve-library-id", {"query": "transforms", "libraryName": "fastmcp"}
        )
        lib_text = lib_result.content[0].text if lib_result.content else ""
        try:
            lib_data = json.loads(lib_text)
            library_id = lib_data[0]["id"] if isinstance(lib_data, list) and lib_data else ""
        except (json.JSONDecodeError, KeyError):
            library_id = ""

        if library_id:
            await context7_client.call_tool("query-docs", {"libraryId": library_id, "query": "transforms"})

        elapsed = (time.perf_counter() - start) * 1000
        results.append(TaskResult(
            label="Context7 direct -- 2 sequential calls",
            task=task, tool_calls=2, elapsed_ms=elapsed, success=True,
        ))
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        results.append(TaskResult(
            label="Context7 direct -- 2 sequential calls",
            task=task, tool_calls=2, elapsed_ms=elapsed, success=False,
            error=str(e)[:120],
        ))

    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_report(report: BenchmarkReport) -> None:
    width = 72
    mode_label = "LIVE (real servers)" if report.mode == "live" else "FAST (stub servers, no subprocess)"

    print(f"\n{'=' * width}")
    print(f"  FastMCP CodeMode Hub -- Benchmark Report [{mode_label}]")
    print(f"{'=' * width}")

    # Schema overhead
    print("\n-- Schema Overhead (tokens Claude pays before reading your message) --\n")
    print(f"  {'Server':<40} {'Tools':>6} {'Tokens':>8}")
    print(f"  {'-'*40} {'-'*6} {'-'*8}")
    for r in report.schema_results:
        print(f"  {r.label:<40} {r.tool_count:>6} {r.token_count:>8,}")

    hub = next((r for r in report.schema_results if "Hub" in r.label), None)
    combined = next((r for r in report.schema_results if "combined" in r.label), None)
    if hub and combined:
        saved = combined.token_count - hub.token_count
        pct = (saved / combined.token_count) * 100
        print(f"\n  Savings: {saved:,} tokens ({pct:.0f}% reduction) per session start")

    # Task benchmarks
    if report.task_results:
        print("\n-- Round-Trip & Latency --\n")
        print(f"  {'Approach':<44} {'Calls':>5} {'Time (ms)':>10} {'Status':>8}")
        print(f"  {'-'*44} {'-'*5} {'-'*10} {'-'*8}")
        for r in report.task_results:
            status = "OK" if r.success else "FAIL"
            print(f"  {r.label:<44} {r.tool_calls:>5} {r.elapsed_ms:>10.0f} {status:>8}")
            if not r.success:
                print(f"    Error: {r.error}")

        hub_task = next((r for r in report.task_results if "Hub" in r.label and r.success), None)
        direct_task = next((r for r in report.task_results if "direct" in r.label and r.success), None)
        if hub_task and direct_task:
            call_reduction = direct_task.tool_calls - hub_task.tool_calls
            print(f"\n  Round-trips eliminated: {call_reduction} per multi-step task")
            print("  (Each eliminated round-trip = 1 fewer LLM turn + context accumulation)")

    print(f"\n{'=' * width}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main(live: bool) -> None:
    if live:
        import server  # noqa: PLC0415
        hub = server.mcp
        print("Running benchmarks (live mode -- real stdio servers)...")
    else:
        hub = _build_stub_hub()
        print("Running benchmarks (fast mode -- stub hub, no subprocess)...")
        print("Note: schema overhead uses stub (4 tools). Use --live for real token counts + latency.")

    async with Client(hub) as hub_client:
        if live:
            token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
            context7_transport = StdioTransport("cmd", ["/c", "npx", "-y", "@upstash/context7-mcp"])
            github_transport = StdioTransport(
                "cmd",
                ["/c", "npx", "-y", "@modelcontextprotocol/server-github"],
                env={**os.environ, "GITHUB_PERSONAL_ACCESS_TOKEN": token},
            )
            async with (
                Client(context7_transport) as context7_client,
                Client(github_transport) as github_client,
            ):
                schema_results = await benchmark_schema_overhead(hub_client, context7_client, github_client)
                # Live: use real namespaced tool names (hyphens preserved) and correct params
                task_results = await benchmark_multistep_task_live(hub_client, context7_client)
        else:
            schema_results = await benchmark_schema_overhead(hub_client, None, None)
            task_results = await benchmark_multistep_task(hub_client, None, tool_prefix="context7_")

        mode = "live" if live else "fast"
        report = BenchmarkReport(mode=mode, schema_results=schema_results, task_results=task_results)
        print_report(report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FastMCP CodeMode benchmarks")
    parser.add_argument("--live", action="store_true", help="Use real stdio servers (requires npx)")
    args = parser.parse_args()
    asyncio.run(main(args.live))
