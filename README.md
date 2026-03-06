# mcp-experiment

A FastMCP 3.1 **CodeMode hub** that aggregates multiple MCP servers behind a single, token-efficient interface.

Instead of exposing every tool schema to the LLM on session start, CodeMode collapses all downstream tools into three meta-tools: `search`, `get_schema`, and `execute`. The LLM discovers tools on demand and chains calls in a single sandbox execution — no intermediate results polluting the context window.

## Benchmark results

Measured against Context7 + GitHub MCP (28 tools combined):

| | Tools | Schema tokens |
|---|---|---|
| Hub (CodeMode) | 3 | 635 |
| Context7 + GitHub direct | 28 | 6,950 |
| **Savings** | | **6,315 tokens (91%)** |

Round-trips for a 2-step task (resolve library ID → fetch docs):

| Approach | LLM turns | Tool calls | Time |
|---|---|---|---|
| Hub (CodeMode) | 1 | 1 execute | 1,339ms |
| Direct (sequential) | 2 | 2 calls | 785ms |

The hub adds wall-clock latency (sandbox overhead) but eliminates one full LLM round-trip per multi-step task. The token savings are realized on every session start.

## Architecture

```
Claude Code
    │
    ▼
FastMCP Hub  (CodeMode transform)
    │   └── search      ← BM25 over tool names + descriptions
    │   └── get_schema  ← fetch parameter schemas on demand
    │   └── execute     ← run Python in Monty sandbox
    │
    ├── context7 proxy  ──► @upstash/context7-mcp (stdio)
    └── github proxy    ──► @modelcontextprotocol/server-github (stdio)
```

**CodeMode flow:**
1. `search("library docs")` → returns matching tool names, descriptions only
2. `get_schema(["context7_resolve-library-id"])` → returns parameter schema
3. `execute("""...""")` → runs Python script in sandbox; intermediate results never touch context

## Project structure

```
mcp-experiment/
├── server.py                     # Hub entry point
├── src/tools/                    # Add your own tools here
├── tests/
│   ├── conftest.py               # Stub + live fixtures
│   └── test_correctness.py       # Hub mechanics + live comparison tests
├── benchmarks/
│   └── benchmark.py              # Schema token + latency benchmarks
└── pyproject.toml
```

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Node.js + npx (for downstream MCP servers)
- `GITHUB_PERSONAL_ACCESS_TOKEN` env var (for GitHub MCP)

## Quickstart

```bash
# Install
git clone https://github.com/MCKRUZ/mcp-experiment
cd mcp-experiment
uv sync

# Dev server (MCP inspector)
uv run fastmcp dev server.py

# Run tests (fast, no subprocess)
uv run pytest tests/

# Run tests against real servers
GITHUB_PERSONAL_ACCESS_TOKEN=... uv run pytest tests/ --live

# Run benchmarks (fast)
uv run python -m benchmarks.benchmark

# Run benchmarks against real servers
GITHUB_PERSONAL_ACCESS_TOKEN=... uv run python -m benchmarks.benchmark --live
```

## Connect to Claude Code

Add to `~/.claude/settings.json` or `~/.claude.json` under `mcpServers`:

```json
"mcp-hub": {
  "command": "uv",
  "args": [
    "run",
    "--project", "/path/to/mcp-experiment",
    "fastmcp", "run",
    "/path/to/mcp-experiment/server.py"
  ],
  "env": {
    "GITHUB_PERSONAL_ACCESS_TOKEN": "your_token_here"
  }
}
```

## Adding tools

Define tools in `src/tools/`, register them on `mcp` before `add_transform`:

```python
# src/tools/my_tools.py
def register(mcp):
    @mcp.tool
    async def my_tool(query: str) -> str:
        """One-line description used for BM25 search discovery."""
        ...

# server.py
from src.tools.my_tools import register
register(mcp)
mcp.add_transform(CodeMode())
```

Docstrings are the BM25 search surface — keep them accurate and under 100 characters.

## Adding more MCP servers

```python
# server.py
from fastmcp.server import create_proxy

firecrawl = create_proxy(StdioTransport("cmd", ["/c", "npx", "-y", "firecrawl-mcp"]))
mcp.mount(firecrawl, namespace="firecrawl")
```

## Notes

- `CodeMode` lives in `fastmcp.experimental.transforms.code_mode` (not `fastmcp.contrib` as shown in the original blog post)
- Apply transforms with `mcp.add_transform(CodeMode())`, not `mcp.wrap_transform()` — the latter returns a `_WrappedProvider` which the `fastmcp` CLI cannot run
- The Monty sandbox has restricted stdlib — `json`, `os`, and most modules are unavailable in `execute` scripts; use only `call_tool()` and basic Python
- The hub adds ~500ms latency overhead vs direct calls; worthwhile when task complexity exceeds 2 tool calls

## References

- [FastMCP 3.1 CodeMode](https://www.jlowin.dev/blog/fastmcp-3-1-code-mode) — original architecture post
- [FastMCP docs](https://github.com/jlowin/fastmcp)
- [Model Context Protocol](https://modelcontextprotocol.io)
