"""
code_server/tools.py
====================
Four async code-intelligence functions that back the MCP tools.

All functions are pure async — no side effects beyond reads.
All DB-touching functions accept an optional repo_path; the DB path is
resolved at call time via get_db_path(), NEVER at import time.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import aiosqlite

from code_server.indexer import SKIP_DIRS, get_db_path

# ---------------------------------------------------------------------------
# 1.  find_function — symbol search
# ---------------------------------------------------------------------------


async def find_function(query: str, repo_path: str = None) -> list[dict]:
    """Search the symbol index for names containing *query*.

    SQL: SELECT * FROM symbols WHERE name LIKE '%query%' LIMIT 20

    Parameters
    ----------
    query:
        Partial or full symbol name to search for.
    repo_path:
        Root of the repository whose index to query. Resolved at call time
        via get_db_path(); defaults to CODEAGENT_REPO env var or ".".

    Returns
    -------
    list of {name, kind, file_path, start_line, end_line, parent, docstring}
    """
    db_path = get_db_path(repo_path)
    pattern = f"%{query}%"
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT name, kind, file_path, start_line, end_line, parent, docstring
            FROM   symbols
            WHERE  name LIKE ?
            ORDER  BY kind, name
            LIMIT  20
            """,
            (pattern,),
        )
        rows = await cur.fetchall()

    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 1b. list_symbols — enumerate all symbols, optionally filtered by kind
# ---------------------------------------------------------------------------


async def list_symbols(kind: str = "all", repo_path: str = None) -> list[dict]:
    """Return all indexed symbols, optionally filtered by *kind*.

    Parameters
    ----------
    kind:
        One of ``'class'``, ``'function'``, ``'method'``, or ``'all'``
        (default).  Filtering by kind is far more reliable than guessing
        names for broad questions like "list all classes".
    repo_path:
        Root of the repository whose index to query.

    Returns
    -------
    list of {name, kind, file_path, start_line, end_line, parent, docstring}
    """
    db_path = get_db_path(repo_path)
    valid_kinds = {"class", "function", "method"}
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        if kind in valid_kinds:
            cur = await db.execute(
                """
                SELECT name, kind, file_path, start_line, end_line, parent, docstring
                FROM   symbols
                WHERE  kind = ?
                ORDER  BY file_path, start_line
                LIMIT  100
                """,
                (kind,),
            )
        else:
            # "all" or unrecognised — return everything
            cur = await db.execute(
                """
                SELECT name, kind, file_path, start_line, end_line, parent, docstring
                FROM   symbols
                ORDER  BY kind, file_path, start_line
                LIMIT  100
                """
            )
        rows = await cur.fetchall()

    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 2.  get_callers — grep-style caller search
# ---------------------------------------------------------------------------


async def get_callers(function_name: str, repo_path: str = None) -> list[dict]:
    """Scan all source files in *repo_path* for lines containing ``function_name(``.

    Uses asyncio to read files concurrently (semaphore-limited).

    Parameters
    ----------
    function_name:
        Exact name of the function to search for callers of.
    repo_path:
        Root of the repository to grep. Defaults to CODEAGENT_REPO env var
        or "." — resolved at call time.

    Returns
    -------
    list of {file, line, snippet}   (capped at 30 results)
    """
    root = Path(repo_path or os.environ.get("CODEAGENT_REPO", ".")).resolve()
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
# 4.  list_imports — import statement extractor
# ---------------------------------------------------------------------------

_PY_IMPORT_RE = re.compile(r"^\s*(from\s+\S+\s+import\s+.+|import\s+.+)")
_JS_IMPORT_RE = re.compile(
    r"^\s*(import\s+.+|const\s+\S+\s*=\s*require\(.+|let\s+\S+\s*=\s*require\(.+|var\s+\S+\s*=\s*require\(.+)"
)


async def list_imports(file_path: str) -> list[dict]:
    """Return every import statement found in *file_path*.

    Scans line-by-line with a regex; does NOT parse the AST (fast, works on
    files not yet indexed).

    Returns
    -------
    list of {line: int, statement: str}
    """
    path = Path(file_path).resolve()
    ext = path.suffix.lower()
    is_js_ts = ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")
    import_re = _JS_IMPORT_RE if is_js_ts else _PY_IMPORT_RE

    try:
        content = await asyncio.to_thread(
            path.read_text, encoding="utf-8", errors="ignore"
        )
    except OSError as exc:
        return [{"line": 0, "statement": f"ERROR: cannot read {file_path}: {exc}"}]

    results: list[dict] = []
    for lineno, raw in enumerate(content.splitlines(), start=1):
        m = import_re.match(raw)
        if m:
            results.append({"line": lineno, "statement": raw.strip()})

    return results
