# Project: mcp-experiment

## Purpose
Building a **FastMCP 3.1** server using **CodeMode** architecture — where clients compose tool calls as Python scripts executed in a sandbox instead of making sequential round-trip MCP calls. This eliminates context bloat (no schema dumps) and round-trip overhead.

## Stack
- Python 3.11+
- `fastmcp` 3.1 (MCP server framework)
- Pydantic v2 (schema validation, models)
- `uv` (package manager — prefer over pip/poetry)
- `pytest` + `pytest-asyncio` (testing)
- `black` (formatting) + `ruff` (linting)

## CodeMode Architecture

```
Client → Search (BM25) → GetSchemas → Execute (Monty sandbox Python)
```

Three-stage flow:
1. **Search** — LLM queries available tools; receives lightweight names + descriptions
2. **GetSchemas** — LLM selects relevant tools; receives parameter schemas
3. **Execute** — LLM writes a Python script calling `await call_tool(name, args)`; script runs isolated in sandbox; intermediate results never pollute context

Sandbox constraints: configurable timeout/memory/recursion. Only `call_tool()` + Python stdlib available. No project module imports, no file I/O.

## Key FastMCP Patterns

**Define a tool:**
```python
from fastmcp import FastMCP

mcp = FastMCP("MyServer")

@mcp.tool
async def search_products(query: str, limit: int = 10) -> list[dict]:
    """Search product catalog by keyword. Returns id, name, price."""
    ...
```

**Enable CodeMode (as a transform):**
```python
from fastmcp.experimental.transforms.code_mode import CodeMode

app = mcp.wrap_transform(CodeMode())
```

Note: `CodeMode` lives in `fastmcp.experimental` — the blog post showed a future API (`fastmcp.contrib`). Use the experimental path until it stabilizes.

**CodeMode constructor params:**
```python
CodeMode(
    sandbox_provider=None,        # Default: MontySandboxProvider
    discovery_tools=None,         # Default: [Search, GetSchemas]
    execute_tool_name="execute",  # Name of the execute meta-tool
    execute_description=None,     # Override execute tool docstring
)
```

**Stack transforms:**
```python
# Each transform wraps the previous via wrap_transform
# Order: outermost applied last
prefixed = mcp.wrap_transform(PrefixTransform(prefix="prod_"))
app = prefixed.wrap_transform(CodeMode())
```

## Commands
- `uv run fastmcp dev server.py` — Dev server with MCP inspector
- `uv run fastmcp run server.py` — Production run
- `uv run pytest` — Run all tests
- `uv run pytest --cov --cov-report=term-missing` — Coverage
- `uv run black .` — Format
- `uv run ruff check . --fix` — Lint and auto-fix

## Verification
After every change: `uv run black . && uv run ruff check . && uv run pytest`

## Task Approach
1. **Test tools without CodeMode first.** Validate each tool via direct FastMCP before enabling the CodeMode transform.
2. **Discovery docstrings are the product.** Tool docstrings are what the LLM sees during Search — write them as precise, <100 char summaries.
3. **Sandbox is a black box.** Tools must return self-contained data. Don't rely on shared state between `call_tool()` invocations in the same script.
4. **Start with `"brief"` discovery.** Only upgrade to `"detailed"` if client benchmarks show it's needed.
5. **Transforms compose, not override.** Each transform wraps the previous — order matters. Apply innermost transforms first.

## Context7
Use `use context7` in prompts for live FastMCP docs. Library: `fastmcp`.

## Common Mistakes
- **Sync tool in async server**: Always `async def` — sync tools block the event loop.
- **Importing project modules in sandbox scripts**: Sandbox has no access to project files; pass all data through `call_tool()` args and return values.
- **Fat discovery responses**: Returning full schemas in Search defeats the entire point of CodeMode's token savings.
- **Wrong import path**: The blog uses `fastmcp.contrib.code_mode` — the actual path is `fastmcp.experimental.transforms.code_mode`.
- **Wrong instantiation**: Don't do `CodeMode(mcp)` — use `mcp.wrap_transform(CodeMode())`.
- **Transform order confusion**: Use chained `.wrap_transform()` calls — outermost transform applied last.
- **Missing return type annotations**: FastMCP uses return types for schema generation; missing types produce incomplete schemas.
