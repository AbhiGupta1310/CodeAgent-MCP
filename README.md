# CodeAgent MCP

A hosted **MCP server** that clones any public GitHub repository, indexes it using tree-sitter, and exposes code-intelligence tools via **SSE transport**. Connect from Claude Desktop, Cursor, or any MCP-compatible client.

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  MCP Client (Claude Desktop / Cursor / GPT)          │
│  "index_github_repo('https://github.com/user/repo')"│
└──────────────┬───────────────────────────────────────┘
               │  SSE (Server-Sent Events)
┌──────────────▼───────────────────────────────────────┐
│  Code Server  (code_server/)                         │
│  • FastMCP server (SSE on port 8000)                 │
│  • GitHub clone → /tmp/codeagent_sessions/           │
│  • tree-sitter AST indexer → Postgres (Neon)         │
│  • Tools: index_github_repo, search_symbols,         │
│           find_callers, read_code, get_imports        │
│  • Session management (24h TTL, auto-cleanup)        │
└───────────┬──────────────────────┬───────────────────┘
            │                      │
    ┌───────▼────────┐    ┌───────▼────────┐
    │  Postgres      │    │  Disk          │
    │  (Neon)        │    │  /tmp/sessions │
    │  • sessions    │    │  • cloned repos│
    │  • symbols     │    │  • source files│
    │  • imports     │    │                │
    └────────────────┘    └────────────────┘
```

## Quick Start (Connect via Claude Desktop)

### 1. Add to Claude Desktop config

```json
{
  "mcpServers": {
    "codeagent": {
      "url": "https://your-app.railway.app/sse",
      "type": "sse"
    }
  }
}
```

### 2. Workflow

```
1. "Index this repo: https://github.com/pallets/flask"
   → Returns session_id

2. "Search for symbols named 'route' in session {session_id}"
   → Returns matching functions/classes

3. "Read the code at /path/to/file.py lines 10-50 for session {session_id}"
   → Returns actual source lines

4. "Find all callers of 'login' in session {session_id}"
   → Returns all call sites
```

## MCP Tools

| Tool | Description |
|---|---|
| `index_github_repo(github_url)` | Clone + index a public repo. Returns `session_id` |
| `search_symbols(query, session_id)` | Find symbols by partial name (ILIKE) |
| `list_all_symbols(kind, session_id)` | List all symbols filtered by kind |
| `find_callers(function_name, session_id)` | Grep for call sites |
| `read_code(file_path, start_line, end_line, session_id)` | Read source lines |
| `get_imports(file_path, session_id)` | List imports in a file |
| `get_session_status(session_id)` | Check if session is ready |

## Self-Hosting

### Prerequisites

- Python 3.11+
- PostgreSQL database (e.g., [Neon](https://neon.tech) free tier)
- Git

### Setup

```bash
# Clone and install
git clone <repo-url> && cd codeagent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Configure
cp .env.example .env
# Set DATABASE_URL in .env
```

### Run Locally

```bash
python -m code_server.server
# → SSE server at http://localhost:8000/sse
```

### Deploy with Docker

```bash
docker build -t codeagent .
docker run -p 8000:8000 \
  -e DATABASE_URL="postgresql://..." \
  codeagent
```

### Deploy to Railway

```bash
# Push to GitHub, then in Railway:
# 1. New Project → Deploy from GitHub
# 2. Add DATABASE_URL env var
# 3. Railway auto-detects Dockerfile
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | Postgres connection string (Neon recommended) |
| `OPENROUTER_API_KEY` | For CLI only | OpenRouter API key for agentic loop |

## Tech Stack

| Layer | Library |
|---|---|
| MCP | `mcp` (`FastMCP`) + SSE transport |
| Code parsing | `tree-sitter` ≥0.22 + language grammars |
| Database | `asyncpg` (async Postgres, connection pool) |
| Git | `subprocess` (shallow clones) |
| CLI | `typer` + `rich` |
| Testing | `pytest` + `pytest-asyncio` |

## Project Structure

```
codeagent/
├── code_server/
│   ├── db.py          # Postgres connection pool + schema
│   ├── sessions.py    # Session CRUD (create, cleanup, TTL)
│   ├── indexer.py     # tree-sitter AST → Postgres
│   ├── tools.py       # MCP tool implementations
│   ├── server.py      # FastMCP SSE server
│   └── watcher.py     # watchdog file watcher (local dev)
├── agent_server/
│   ├── loop.py        # agentic loop (OpenRouter + tool use)
│   └── history.py     # conversation history manager
├── cli/
│   └── main.py        # typer CLI (local usage)
├── Dockerfile
├── railway.toml
├── .env.example
└── pyproject.toml
```

## CLI (Local Development)

The CLI is preserved for local development:

```bash
# Index a local repo
codeagent index /path/to/repo

# Ask questions about indexed code
codeagent ask "How does the authentication system work?"
```

---

**License**: MIT
