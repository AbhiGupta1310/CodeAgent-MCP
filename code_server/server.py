"""
code_server/server.py
=====================
FastMCP server exposing code-intelligence tools.

MCP tool names (exact):
  search_symbols      — find symbols by name
  find_callers        — grep callers of a function
  read_code           — read a file slice with line numbers
  get_imports         — list import statements in a file
  index_repository    — trigger repo indexing

Run:
    python -m code_server.server
    # or
    python code_server/server.py
"""

from __future__ import annotations

import json
import logging
import os

from mcp.server.fastmcp import FastMCP

from code_server.indexer import index_repo
from code_server.tools import (
    find_function,
    get_callers,
    list_imports,
    list_symbols,
    read_file_slice,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("codeagent-code-server")

# ---------------------------------------------------------------------------
# Tool: search_symbols
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_symbols(query: str) -> str:
    """Search the symbol index for functions, classes, and methods whose
    names contain *query* (case-insensitive LIKE match).

    Returns a JSON array of symbol records:
        [{name, kind, file_path, start_line, end_line, parent, docstring}]

    Parameters
    ----------
    query:
        Partial or full symbol name to search for.
    """
    repo = os.environ.get("CODEAGENT_REPO", ".")
    results = await find_function(query, repo_path=repo)
    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Tool: find_callers
# ---------------------------------------------------------------------------


@mcp.tool()
async def find_callers(function_name: str) -> str:
    """Scan the repository for every line that calls *function_name(*.

    Uses the repo path configured via the CODEAGENT_REPO environment variable
    (defaults to the current directory).  Results are capped at 30.

    Returns a JSON array:
        [{file, line, snippet}]

    Parameters
    ----------
    function_name:
        Exact name of the function to search for callers of.
    """
    repo = os.environ.get("CODEAGENT_REPO", ".")
    results = await get_callers(function_name, repo_path=repo)
    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Tool: read_code
# ---------------------------------------------------------------------------


@mcp.tool()
async def read_code(file_path: str, start_line: int, end_line: int) -> str:
    """Return a slice of *file_path* with line numbers prepended.

    Example output line::

        42: def login(user):

    Parameters
    ----------
    file_path:
        Absolute or relative path to the source file.
    start_line:
        First line to read (1-indexed, inclusive).
    end_line:
        Last line to read (1-indexed, inclusive).
    """
    return await read_file_slice(file_path, start_line, end_line)


@mcp.tool()
async def list_all_symbols(kind: str = "all") -> str:
    """List all indexed symbols in the repository, optionally filtered by kind.

    Use this for broad questions like "what classes exist?" or
    "show me all functions". Much more reliable than guessing names.

    Parameters
    ----------
    kind:
        Filter to ``'class'``, ``'function'``, ``'method'``, or ``'all'``
        (default).
    """
    repo = os.environ.get("CODEAGENT_REPO", ".")
    results = await list_symbols(kind=kind, repo_path=repo)
    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_imports
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_imports(file_path: str) -> str:
    """List every import statement found in *file_path*.

    Returns a JSON array:
        [{line: int, statement: str}]

    Parameters
    ----------
    file_path:
        Absolute or relative path to the Python source file.
    """
    results = await list_imports(file_path)
    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Tool: index_repository
# ---------------------------------------------------------------------------


@mcp.tool()
async def index_repository(repo_path: str) -> str:
    """Trigger a full re-index of *repo_path*.

    Walks all .py files (skipping .venv, __pycache__, node_modules, etc.),
    parses them with tree-sitter, and upserts their symbols into the SQLite
    index.  Returns a JSON object with the outcome.

    Parameters
    ----------
    repo_path:
        Absolute or relative path to the root of the repository to index.
    """
    try:
        await index_repo(repo_path)
        return json.dumps({"status": "ok", "repo_path": repo_path})
    except Exception as exc:
        logger.exception("index_repository failed for %s", repo_path)
        return json.dumps({"status": "error", "detail": str(exc)})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from code_server.watcher import start_watcher, stop_watcher
    repo = os.environ.get("CODEAGENT_REPO", ".")
    logger.info("Starting file watcher on repository: %s", repo)
    observer = start_watcher(repo)
    try:
        logger.info("Starting codeagent-code-server (stdio transport)…")
        mcp.run(transport="stdio")
    finally:
        logger.info("Stopping file watcher...")
        stop_watcher(observer)
