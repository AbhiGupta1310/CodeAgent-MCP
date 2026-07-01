"""
code_server/server.py
=====================
FastMCP server exposing code-intelligence tools over SSE transport.

MCP tool names (exact):
  index_github_repo  — clone + index a public GitHub repo
  search_symbols     — find symbols by name
  list_all_symbols   — enumerate all symbols by kind
  find_callers       — grep callers of a function
  read_code          — read a file slice with line numbers
  get_imports        — list import statements in a file
  get_session_status — check if a session is ready

Run:
    python -m code_server.server
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from code_server.db import init_schema
from code_server.indexer import index_repo
from code_server.sessions import (
    cleanup_expired_sessions,
    create_session,
    get_repo_path,
    session_exists,
    update_last_active,
    update_session_status,
)
from code_server.tools import (
    find_function,
    get_callers,
    list_imports,
    list_symbols,
    read_file_slice,
)

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("codeagent-code-server", host="0.0.0.0", port=8000)

# ---------------------------------------------------------------------------
# Helper: validate session
# ---------------------------------------------------------------------------


async def _validate_session(session_id: str) -> str | None:
    """Return an error message if session_id is invalid, else None."""
    if not session_id:
        return "Error: session_id is required. Run index_github_repo first."
    exists = await session_exists(session_id)
    if not exists:
        return "Session not found. Please run index_github_repo first."
    return None


# ---------------------------------------------------------------------------
# Tool: index_github_repo
# ---------------------------------------------------------------------------


@mcp.tool()
async def index_github_repo(github_url: str) -> str:
    """Clone a public GitHub repository and index it for analysis.
    Returns a session_id that must be passed to all subsequent tool calls.
    Only public GitHub URLs are supported.

    Parameters
    ----------
    github_url:
        Full GitHub URL (e.g. https://github.com/user/repo).
    """
    # 1. Validate URL
    if "github.com" not in github_url:
        return "Error: Only public GitHub URLs are supported. URL must contain 'github.com'."

    try:
        # 2. Create session
        session_id, repo_path = await create_session(github_url)

        # 3. Clone with timeout
        try:
            result = subprocess.run(
                ["git", "clone", "--depth=1", github_url, repo_path],
                timeout=120,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                await update_session_status(session_id, "error")
                return f"Failed to clone repo: {result.stderr.strip()}"
        except subprocess.TimeoutExpired:
            await update_session_status(session_id, "error")
            return "Error: Clone timed out after 120 seconds. Repo may be too large."

        # 4. Check repo size (200MB limit)
        total_size = sum(
            f.stat().st_size
            for f in Path(repo_path).rglob("*")
            if f.is_file()
        )
        if total_size > 200 * 1024 * 1024:
            # Clean up the clone
            import shutil
            shutil.rmtree(Path(repo_path).parent, ignore_errors=True)
            await update_session_status(session_id, "error")
            return "Error: Repo exceeds 200MB limit for free tier."

        # 5. Index the repo
        await index_repo(repo_path, session_id)

        # 6. Update status
        await update_session_status(session_id, "ready")

        # 7. Return session info
        return (
            f"Session ID: {session_id}\n"
            f"Repository cloned and indexed successfully.\n"
            f"You can now ask questions. Pass this session_id to all tool calls:\n"
            f"* search_symbols(query, session_id)\n"
            f"* list_all_symbols(kind, session_id)\n"
            f"* find_callers(function_name, session_id)\n"
            f"* read_code(file_path, start_line, end_line, session_id)\n"
            f"* get_imports(file_path, session_id)"
        )

    except Exception as exc:
        logger.exception("index_github_repo failed for %s", github_url)
        return f"Error: Failed to index repository: {exc}"


# ---------------------------------------------------------------------------
# Tool: search_symbols
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_symbols(query: str, session_id: str) -> str:
    """Search the symbol index for functions, classes, and methods whose
    names contain *query* (case-insensitive ILIKE match).

    Returns a JSON array of symbol records:
        [{name, kind, file_path, start_line, end_line, parent, docstring}]

    Parameters
    ----------
    query:
        Partial or full symbol name to search for.
    session_id:
        Session ID returned by index_github_repo.
    """
    error = await _validate_session(session_id)
    if error:
        return error

    await update_last_active(session_id)
    results = await find_function(query, session_id)
    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Tool: list_all_symbols
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_all_symbols(kind: str, session_id: str) -> str:
    """List all indexed symbols in the repository, optionally filtered by kind.

    Use this for broad questions like "what classes exist?" or
    "show me all functions". Much more reliable than guessing names.

    Parameters
    ----------
    kind:
        Filter to ``'class'``, ``'function'``, ``'method'``, or ``'all'``.
    session_id:
        Session ID returned by index_github_repo.
    """
    error = await _validate_session(session_id)
    if error:
        return error

    await update_last_active(session_id)
    results = await list_symbols(kind=kind, session_id=session_id)
    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Tool: find_callers
# ---------------------------------------------------------------------------


@mcp.tool()
async def find_callers_tool(function_name: str, session_id: str) -> str:
    """Find all places in the codebase that call a given function.

    Scans all source files in the cloned repository for lines
    containing ``function_name(``.

    Parameters
    ----------
    function_name:
        Exact name of the function to search for callers of.
    session_id:
        Session ID returned by index_github_repo.
    """
    error = await _validate_session(session_id)
    if error:
        return error

    await update_last_active(session_id)
    repo_path = await get_repo_path(session_id)
    if not repo_path:
        return "Error: Could not find repo path for this session."
    results = await get_callers(function_name, session_id, repo_path)
    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Tool: read_code
# ---------------------------------------------------------------------------


@mcp.tool()
async def read_code(
    file_path: str, start_line: int, end_line: int, session_id: str
) -> str:
    """Read specific lines from a source file with line numbers prepended.

    Example output line::

        42: def login(user):

    Parameters
    ----------
    file_path:
        Absolute path to the source file (from search_symbols results).
    start_line:
        First line to read (1-indexed, inclusive).
    end_line:
        Last line to read (1-indexed, inclusive).
    session_id:
        Session ID returned by index_github_repo.
    """
    error = await _validate_session(session_id)
    if error:
        return error

    await update_last_active(session_id)
    return await read_file_slice(file_path, start_line, end_line)


# ---------------------------------------------------------------------------
# Tool: get_imports
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_imports(file_path: str, session_id: str) -> str:
    """List all imports recorded for a file in the index.

    Returns a JSON array:
        [{file_path, module, alias, symbol}]

    Parameters
    ----------
    file_path:
        Absolute path to the source file.
    session_id:
        Session ID returned by index_github_repo.
    """
    error = await _validate_session(session_id)
    if error:
        return error

    await update_last_active(session_id)
    results = await list_imports(file_path, session_id)
    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_session_status
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_session_status(session_id: str) -> str:
    """Check if a session exists and is ready for questions.

    Parameters
    ----------
    session_id:
        Session ID returned by index_github_repo.
    """
    exists = await session_exists(session_id)
    if not exists:
        return "Session not found. Please run index_github_repo first."
    return "Session is ready. You can ask questions about this codebase."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Initialize schema before starting the server
    asyncio.run(init_schema())
    logger.info("Postgres schema initialized at startup")

    logger.info("Starting codeagent-code-server (SSE transport on 0.0.0.0:8000)…")
    mcp.run(transport="sse")

