"""
code_server/tools.py
====================
Async code-intelligence functions that back the MCP tools.

All functions are pure async — no side effects beyond reads.
All DB-touching functions accept a session_id and query Postgres
via the shared asyncpg pool from db.py.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from code_server.db import get_pool
from code_server.indexer import SKIP_DIRS

# ---------------------------------------------------------------------------
# 1.  find_function — symbol search
# ---------------------------------------------------------------------------


async def find_function(query: str, session_id: str) -> list[dict]:
    """Search the symbol index for names containing *query*.

    SQL: SELECT * FROM symbols WHERE session_id = $1 AND name ILIKE $2 LIMIT 20

    Parameters
    ----------
    query:
        Partial or full symbol name to search for.
    session_id:
        Session ID to scope the query to.

    Returns
    -------
    list of {name, kind, file_path, start_line, end_line, parent, docstring}
    """
    pool = await get_pool()
    pattern = f"%{query}%"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT name, kind, file_path, start_line, end_line, parent, docstring
            FROM   symbols
            WHERE  session_id = $1 AND name ILIKE $2
            ORDER  BY kind, name
            LIMIT  20
            """,
            session_id,
            pattern,
        )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 1b. list_symbols — enumerate all symbols, optionally filtered by kind
# ---------------------------------------------------------------------------


async def list_symbols(kind: str = "all", session_id: str = "") -> list[dict]:
    """Return all indexed symbols, optionally filtered by *kind*.

    Parameters
    ----------
    kind:
        One of ``'class'``, ``'function'``, ``'method'``, or ``'all'``
        (default).  Filtering by kind is far more reliable than guessing
        names for broad questions like "list all classes".
    session_id:
        Session ID to scope the query to.

    Returns
    -------
    list of {name, kind, file_path, start_line, end_line, parent, docstring}
    """
    pool = await get_pool()
    valid_kinds = {"class", "function", "method"}
    async with pool.acquire() as conn:
        if kind in valid_kinds:
            rows = await conn.fetch(
                """
                SELECT name, kind, file_path, start_line, end_line, parent, docstring
                FROM   symbols
                WHERE  session_id = $1 AND kind = $2
                ORDER  BY file_path, start_line
                LIMIT  100
                """,
                session_id,
                kind,
            )
        else:
            # "all" or unrecognised — return everything
            rows = await conn.fetch(
                """
                SELECT name, kind, file_path, start_line, end_line, parent, docstring
                FROM   symbols
                WHERE  session_id = $1
                ORDER  BY kind, file_path, start_line
                LIMIT  100
                """,
                session_id,
            )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 2.  get_callers — grep-style caller search
# ---------------------------------------------------------------------------


async def get_callers(
    function_name: str, session_id: str, repo_path: str
) -> list[dict]:
    """Scan all source files in *repo_path* for lines containing ``function_name(``.

    Uses asyncio to read files concurrently (semaphore-limited).

    Parameters
    ----------
    function_name:
        Exact name of the function to search for callers of.
    session_id:
        Session ID (used for context, not directly for grep).
    repo_path:
        Root of the repository to grep.

    Returns
    -------
    list of {file, line, snippet}   (capped at 30 results)
    """
    root = Path(repo_path).resolve()
    pattern = re.compile(re.escape(function_name) + r"\s*\(")

    results: list[dict] = []
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(16)
    cap = 30

    async def _scan(src_file: Path) -> None:
        nonlocal results
        async with semaphore:
            try:
                content = await asyncio.to_thread(
                    src_file.read_text, encoding="utf-8", errors="ignore"
                )
            except OSError:
                return

            for lineno, line in enumerate(content.splitlines(), start=1):
                if pattern.search(line):
                    async with lock:
                        if len(results) >= cap:
                            return
                        results.append(
                            {
                                "file": str(src_file),
                                "line": lineno,
                                "snippet": line.strip(),
                            }
                        )

    files_to_scan: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext in (
                ".py",
                ".js",
                ".jsx",
                ".ts",
                ".tsx",
                ".mjs",
                ".cjs",
                ".mts",
                ".cts",
            ):
                files_to_scan.append(Path(dirpath) / fname)

    await asyncio.gather(*(_scan(f) for f in files_to_scan))

    # Sort deterministically: by file then line
    results.sort(key=lambda r: (r["file"], r["line"]))
    return results[:cap]


# ---------------------------------------------------------------------------
# 3.  read_file_slice — read exact lines with line-number prefix
# ---------------------------------------------------------------------------


async def read_file_slice(
    file_path: str,
    start_line: int,
    end_line: int,
) -> str:
    """Read lines *start_line*–*end_line* (1-indexed, inclusive) from *file_path*.

    Each line is prefixed with its line number::

        42: def login(user):
        43:     ...

    Returns
    -------
    str — the annotated slice, or an error message if the file can't be read.
    """
    path = Path(file_path).resolve()
    try:
        content = await asyncio.to_thread(
            path.read_text, encoding="utf-8", errors="ignore"
        )
    except OSError as exc:
        return f"ERROR: cannot read {file_path}: {exc}"

    lines = content.splitlines()
    total = len(lines)

    # Clamp to valid range
    s = max(1, start_line)
    e = min(total, end_line)

    if s > total:
        return f"ERROR: start_line {start_line} exceeds file length ({total} lines)."

    selected = lines[s - 1 : e]  # 0-indexed slice
    width = len(str(e))           # pad line numbers to same width
    annotated = "\n".join(f"{s + i:>{width}}: {line}" for i, line in enumerate(selected))
    return annotated


# ---------------------------------------------------------------------------
# 4.  list_imports — import query from Postgres
# ---------------------------------------------------------------------------


async def list_imports(file_path: str, session_id: str) -> list[dict]:
    """Return every import record for *file_path* from the Postgres index.

    Parameters
    ----------
    file_path:
        Absolute or relative path to the source file.
    session_id:
        Session ID to scope the query to.

    Returns
    -------
    list of {file_path, module, alias, symbol}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT file_path, module, alias, symbol
            FROM   imports
            WHERE  session_id = $1 AND file_path = $2
            """,
            session_id,
            file_path,
        )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 5.  generate_architecture — Mermaid.js class diagram
# ---------------------------------------------------------------------------


async def generate_architecture(session_id: str) -> str:
    """Generate a Mermaid.js class diagram of the repository architecture.

    Queries all classes and their inheritance relationships.

    Parameters
    ----------
    session_id:
        Session ID to scope the query to.

    Returns
    -------
    str — Mermaid.js code block
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT name, parent
            FROM   symbols
            WHERE  session_id = $1 AND kind = 'class'
            ORDER  BY name
            """,
            session_id,
        )

    if not rows:
        return "No classes found in the repository to generate an architecture diagram."

    lines = ["```mermaid", "classDiagram"]
    for row in rows:
        name = row["name"]
        parent = row["parent"]
        if parent:
            lines.append(f"  {parent} <|-- {name}")
        else:
            lines.append(f"  class {name}")
    
    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6.  semantic_search — Vector similarity search
# ---------------------------------------------------------------------------


async def semantic_search(query: str, session_id: str) -> list[dict]:
    """Search for concepts using pgvector cosine similarity."""
    hf_token = os.environ.get("HF_TOKEN")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    query_vector = None

    if hf_token:
        try:
            import httpx

            headers = {"Authorization": f"Bearer {hf_token}"}
            url = "https://router.huggingface.co/hf-inference/models/BAAI/bge-small-en-v1.5"
            payload = {"inputs": [query], "options": {"wait_for_model": True}}
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                embs = resp.json()
                query_vector = embs[0]
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("HF API query embedding failed: %s", exc)

    elif api_key:
        try:
            import httpx

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": "openai/text-embedding-3-small",
                "input": [query],
                "dimensions": 384,
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/embeddings",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                query_vector = data["data"][0]["embedding"]
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("OpenRouter API query embedding failed: %s", exc)

    if query_vector is None:
        from code_server.indexer import get_embedding_model

        model = get_embedding_model()
        embeddings_gen = await asyncio.to_thread(model.embed, [query])
        embeddings = await asyncio.to_thread(list, embeddings_gen)
        query_vector = embeddings[0].tolist()

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT name, kind, file_path, start_line, end_line, docstring,
                   1 - (embedding <=> $2::vector) AS similarity
            FROM   symbols
            WHERE  session_id = $1 AND embedding IS NOT NULL
            ORDER  BY embedding <=> $2::vector
            LIMIT  10
            """,
            session_id,
            str(query_vector),
        )
    return [dict(row) for row in rows]

