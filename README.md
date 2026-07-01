# 🚀 CodeAgent MCP

A powerful, hosted **Model Context Protocol (MCP)** server that gives LLMs (like Claude, Cursor, and Windsurf) the ability to understand, navigate, and query any public GitHub repository.

CodeAgent clones the repository, indexes it using **tree-sitter**, and exposes a suite of code-intelligence tools via **Server-Sent Events (SSE)**.

---

## 🌟 Vision & What It Is

**The Problem:** LLMs are great at writing code, but they struggle to explore large, unseen codebases. You can't just paste a 10,000-line repository into a chat window.

**The Solution:** CodeAgent acts as the "eyes and hands" of the LLM. 
1. The user tells the LLM: *"Index this GitHub repo"*
2. The LLM calls our `index_github_repo` tool.
3. CodeAgent clones the repo to disk, parses every file into an Abstract Syntax Tree (AST) using tree-sitter, and stores all functions, classes, and imports in a **PostgreSQL database**.
4. The LLM can now use tools like `search_symbols`, `find_callers`, and `read_code` to navigate the codebase precisely, fetching only the exact lines of code it needs.

---

## 🏗 Architecture

Here is exactly how CodeAgent works under the hood:

```mermaid
graph TD
    subgraph Client [MCP Client]
        LLM[Claude Desktop / Cursor / Web]
    end

    subgraph CodeAgent [CodeAgent Server]
        FastMCP[FastMCP SSE Server]
        SessionMgr[Session Manager]
        Indexer[Tree-Sitter Indexer]
        Tools[Code Intelligence Tools]
    end

    subgraph Storage [Storage Layer]
        DB[(PostgreSQL)]
        Disk[(Local Disk /tmp)]
    end

    LLM <-->|1. SSE Transport| FastMCP
    FastMCP -->|2. Route Request| SessionMgr
    FastMCP -->|4. Execute Tool| Tools
    
    SessionMgr -->|3a. Git Clone| Disk
    SessionMgr -->|3b. Trigger Indexing| Indexer
    
    Indexer -->|Parse AST| Disk
    Indexer -->|Store Symbols & Imports| DB
    
    Tools -->|Query Metadata| DB
    Tools -->|Read File Slices| Disk
```

### Components
- **FastMCP (SSE):** The transport layer. Listens for HTTP requests and maintains a streaming connection with the LLM.
- **Session Manager:** Manages multi-tenant workspaces. Clones repos to `/tmp/codeagent_sessions/{id}` and cleans them up after 24 hours.
- **Tree-Sitter Indexer:** Scans the codebase, understands the language syntax (Python, JS, TS, etc.), and extracts symbols.
- **Postgres:** Stores relational metadata (`symbols`, `imports`) for lightning-fast querying across massive codebases.

---

## 🛠 Available MCP Tools

CodeAgent equips the LLM with these exact capabilities:

| Tool | Description |
|---|---|
| `index_github_repo(github_url)` | Clones and indexes a public repo. Returns a unique `session_id`. |
| `search_symbols(query, session_id)` | Finds functions/classes by partial name using `ILIKE` search. |
| `list_all_symbols(kind, session_id)` | Lists all symbols filtered by kind (e.g., all `class`es). |
| `find_callers_tool(function_name, session_id)` | Greps the codebase for places where a function is called. |
| `read_code(file_path, start_line, end_line, session_id)`| Reads exact source code lines directly from disk. |
| `get_imports(file_path, session_id)` | Lists all imports recorded for a specific file. |
| `get_session_status(session_id)` | Checks if a repository session is ready for querying. |

---

## 🚀 Quick Start (Using the Hosted Version)

Want to try it immediately? Add our public hosted endpoint to your MCP client!

### Using Claude Desktop
1. Open Claude Desktop.
2. Go to **Settings (Gear Icon) → Integrations** (or **Connectors**).
3. Click **Add Integration**.
4. Enter the Server URL: `https://codeagent-mcp.onrender.com/sse`
5. Click **Connect**.

*Note: Claude Web (Chrome/Safari) does not support MCP yet. You must use the Claude Desktop macOS/Windows app.*

### Using Cursor
1. Go to **Cursor Settings → Features → MCP**.
2. Click **Add New MCP Server**.
3. Choose **SSE** as the transport.
4. Enter URL: `https://codeagent-mcp.onrender.com/sse`

**Example Prompt:**
> "Index this repo: https://github.com/pallets/flask. Then tell me what the main application class looks like."

---

## 💻 Self-Hosting & Deployment

CodeAgent is fully open-source and easy to host yourself.

### Prerequisites
- Python 3.11+
- PostgreSQL database (e.g., [Neon](https://neon.tech) free tier)
- Git installed on the system

### 1. Local Development Setup

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/CodeAgent-MCP.git
cd CodeAgent-MCP

# Create virtual environment and install
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Configure environment
cp .env.example .env
# Edit .env and set your DATABASE_URL
```

Run the server locally:
```bash
python -m code_server.server
# Server will start at http://0.0.0.0:8000/sse
```

### 2. Deploy to Railway or Render (Free Tier)
1. Push your code to GitHub.
2. Go to [Railway](https://railway.app) or [Render](https://render.com).
3. Create a new service from your GitHub repo.
4. Set the `DATABASE_URL` environment variable to your Postgres connection string.
5. Deploy! The platforms will automatically detect the `Dockerfile` and expose port `8000`.

---

## 🤝 Instructions for Contributors

We love contributions! If you want to make CodeAgent smarter, faster, or add support for more languages, here is how you can help:

### How to Contribute
1. **Fork the repo** and create your branch from `main`.
2. **Set up locally** using the *Local Development Setup* instructions above.
3. **Make your changes**. Ensure your code is well-commented and clean.
4. **Test your changes** by running the server locally and connecting your own Claude Desktop to `http://localhost:8000/sse`.
5. **Issue a Pull Request**.

### Areas to Improve (Ideas for PRs)
- **Add Language Support:** Currently, we parse Python, JS, and TS. Help us add Go, Rust, Java, or C++ by updating the `indexer.py` tree-sitter grammars!
- **Smarter Chunking:** Improve how `read_code` returns large files so the LLM context window doesn't get flooded.
- **Vector Search:** Introduce `pgvector` to allow semantic searching (e.g., "Find the authentication logic") instead of just exact symbol matching.
- **File Tree Tool:** Add a tool that returns the directory structure of the cloned repository.

### Development Guidelines
- All DB-touching functions live in `code_server/tools.py` and must accept a `session_id`.
- Tool wrappers for FastMCP live in `code_server/server.py`.
- We use `asyncpg` for database pooling. **Never** use blocking synchronous calls in the async event loop.
- Use the `lifespan` hook in `server.py` for any startup/shutdown logic to avoid `event loop is closed` errors.

---

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
