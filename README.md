# CodeAgent MCP

An autonomous code-reasoning agent built on the **Model Context Protocol (MCP)**. It parses your repository using tree-sitter, indexes all symbols into SQLite, and drives an agentic loop powered by **Claude via OpenRouter** to answer questions about your codebase.

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│  CLI  (typer + rich)                             │
│  codeagent ask "what does Foo.bar() do?"         │
└──────────────┬───────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────┐
│  Agent Server  (agent_server/)                   │
│  • Agentic loop — OpenRouter API (Claude)         │
│  • Tool dispatch ↔ Code Server via MCP           │
│  • Conversation history manager                  │
└──────────────┬───────────────────────────────────┘
               │  MCP (stdio / SSE)
┌──────────────▼───────────────────────────────────┐
│  Code Server  (code_server/)                     │
│  • FastMCP server                                │
│  • tree-sitter AST indexer → SQLite              │
│  • Tools: search_symbols, find_callers,          │
│           read_code, get_imports                 │
│  • watchdog file watcher (auto-reindex)          │
└──────────────────────────────────────────────────┘
```

## Setup Instructions

1. **Clone the repository**:
   ```bash
   git clone <repo-url> && cd codeagent
   ```

2. **Set up virtual environment & install**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

3. **Configure environment variables**:
   ```bash
   cp .env.example .env
   # Open .env and set your OPENROUTER_API_KEY
   ```

4. **Index the repository**:
   ```bash
   codeagent index .
   ```

5. **Ask questions**:
   ```bash
   codeagent ask "How is the AST indexed into SQLite?"
   ```

## Example Queries

- Find all functions:
  ```bash
  codeagent ask "What functions are defined in this codebase?"
  ```
- Trace function execution flow:
  ```bash
  codeagent ask "Which components call start_watcher()?"
  ```
- Explain architectural design:
  ```bash
  codeagent ask "How does the file watching indexing system work?"
  ```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | ✅ | Your OpenRouter API key (`sk-or-...`) |
| `CODEAGENT_REPO` | optional | Default repo path (defaults to `.`) |

## Tech Stack

| Layer | Library |
|---|---|
| LLM | `openai` SDK → OpenRouter (`anthropic/claude-sonnet-4-5`) |
| MCP | `mcp` (`FastMCP`) |
| Code parsing | `tree-sitter` ≥0.22 + `tree-sitter-python` + `tree-sitter-javascript` |
| Database | `aiosqlite` (async SQLite, no ORM) |
| File watching | `watchdog` |
| CLI | `typer` + `rich` |
| Testing | `pytest` + `pytest-asyncio` |

## Development

```bash
# Run all tests
pytest -v

# Run only indexer tests
pytest tests/test_indexer.py -v

# Index a repo manually (Python API)
python3 -c "
import asyncio
from code_server.indexer import index_repo
asyncio.run(index_repo('/path/to/repo'))
"
```

## Project Structure

```
codeagent/
├── code_server/
│   ├── indexer.py      # tree-sitter AST → SQLite (Phase 1 ✅)
│   ├── tools.py        # MCP tools: search_symbols, find_callers, read_code, get_imports
│   ├── server.py       # FastMCP server
│   └── watcher.py      # watchdog file watcher
├── agent_server/
│   ├── loop.py         # agentic loop (OpenRouter + tool use)
│   └── history.py      # conversation history manager
├── cli/
│   └── main.py         # typer CLI
├── tests/
│   ├── test_indexer.py ✅ 6 passing
│   ├── test_tools.py
│   └── test_loop.py
├── .env.example
└── pyproject.toml
```

## Build Phases

- [x] **Phase 0** — Project scaffold, `pyproject.toml`, venv, package installs
- [x] **Phase 1** — AST indexer (`indexer.py`)
- [x] **Phase 2** — Code tools (`tools.py`)
- [x] **Phase 3** — FastMCP server (`server.py`)
- [x] **Phase 4** — Conversation history manager (`history.py`)
- [x] **Phase 5** — Core Agentic loop (`loop.py`)
- [x] **Phase 6** — Rich CLI (`main.py`)
- [x] **Phase 7** — Watchdog file watcher (`watcher.py`)
- [x] **Phase 8** — Verification & Test suite (36/36 tests passing)
- [x] **Phase 9** — Complete README documentation
