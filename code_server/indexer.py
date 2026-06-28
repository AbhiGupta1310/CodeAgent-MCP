"""
code_server/indexer.py
======================
Tree-sitter AST parser → SQLite symbol index.
Supports Python, JavaScript (JSX), and TypeScript (TSX).

Exports
-------
init_db()          — create/migrate the SQLite schema
index_file(path)   — parse one source file and upsert its symbols
index_repo(path)   — walk all source files in a repo and index them
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import aiosqlite
import tree_sitter_python as tspython
import tree_sitter_javascript as tsjavascript
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Directories that should never be indexed
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".git",
        ".codeagent",
        "dist",
        "build",
        ".next",
        ".svelte-kit",
        "out",
    }
)

# ---------------------------------------------------------------------------
# Language setup (module-level singletons)
# ---------------------------------------------------------------------------

PY_LANGUAGE = Language(tspython.language())
JS_LANGUAGE = Language(tsjavascript.language())
TS_LANGUAGE = Language(tstypescript.language_typescript())
TSX_LANGUAGE = Language(tstypescript.language_tsx())


def _get_language_for_extension(file_path: str) -> Optional[Language]:
    """Map file extension to tree-sitter Language instance."""
    ext = Path(file_path).suffix.lower()
    if ext == ".py":
        return PY_LANGUAGE
    elif ext in (".js", ".jsx", ".mjs", ".cjs"):
        return JS_LANGUAGE
    elif ext in (".ts", ".mts", ".cts"):
        return TS_LANGUAGE
    elif ext == ".tsx":
        return TSX_LANGUAGE
    return None


# ---------------------------------------------------------------------------
# Dynamic DB path — resolved at call time, NOT at import time
# ---------------------------------------------------------------------------


def get_db_path(repo_path: str = None) -> str:
    """Return the absolute path to the SQLite DB for the given repo."""
    base = repo_path or os.environ.get("CODEAGENT_REPO", ".")
    return str(Path(base).resolve() / ".codeagent" / "index.db")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    kind        TEXT    NOT NULL,      -- 'function' | 'class' | 'method'
    file_path   TEXT    NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    parent      TEXT,                  -- class name when kind='method', else NULL
    docstring   TEXT
);
CREATE INDEX IF NOT EXISTS idx_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_file ON symbols(file_path);

CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT NOT NULL,
    module      TEXT NOT NULL,         -- top-level module/package imported
    alias       TEXT,                  -- 'as' alias if present
    symbol      TEXT                   -- specific name imported (from X import Y)
);
CREATE INDEX IF NOT EXISTS idx_import_file ON imports(file_path);
CREATE INDEX IF NOT EXISTS idx_import_module ON imports(module);
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def init_db(repo_path: str = None) -> None:
    """Create the database schema (idempotent)."""
    db_path = get_db_path(repo_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()
    logger.debug("DB initialised at %s", db_path)


async def index_file(file_path: str, repo_path: str = None) -> None:
    """Parse *file_path* using its language parser and upsert its symbols into SQLite."""
    db_path = get_db_path(repo_path)
    path = Path(file_path).resolve()

    lang = _get_language_for_extension(str(path))
    if not lang:
        return  # Unsupported file type

    try:
        source = path.read_bytes()
    except OSError as exc:
        logger.warning("Cannot read %s: %s", file_path, exc)
        return

    # Create local thread-safe parser instance
    parser = Parser(lang)
    tree = parser.parse(source)

    symbols: list[dict] = []
    imports: list[dict] = []

    if lang == PY_LANGUAGE:
        _walk_node_python(
            tree.root_node, source, str(path), symbols, imports, parent_class=None
        )
    else:
        _walk_node_javascript(
            tree.root_node, source, str(path), symbols, imports, parent_class=None
        )

    async with aiosqlite.connect(db_path) as db:
        # Clean-delete all previous data for this file before re-inserting
        await db.execute("DELETE FROM symbols WHERE file_path = ?", (str(path),))
        await db.execute("DELETE FROM imports WHERE file_path = ?", (str(path),))

        await db.executemany(
            """
            INSERT INTO symbols (name, kind, file_path, start_line, end_line, parent, docstring)
            VALUES (:name, :kind, :file_path, :start_line, :end_line, :parent, :docstring)
            """,
            symbols,
        )
        await db.executemany(
            """
            INSERT INTO imports (file_path, module, alias, symbol)
            VALUES (:file_path, :module, :alias, :symbol)
            """,
            imports,
        )
        await db.commit()

    logger.debug(
        "Indexed %s → %d symbols, %d imports", path.name, len(symbols), len(imports)
    )


async def index_repo(repo_path: str) -> None:
    """Walk *repo_path*, find all source files, and index them.

    Skips directories in SKIP_DIRS.
    Always initialises the DB schema first so the database is usable even
    when no source files are present yet.
    """
    root = Path(repo_path).resolve()

    # Always create / migrate the schema first.
    await init_db(str(root))

    files_to_index: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place so os.walk doesn't descend into them
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
                files_to_index.append(Path(dirpath) / fname)

    if not files_to_index:
        logger.info("No source files found under %s — schema created, index is empty.", root)
        return

    logger.info("Indexing %d source files in %s …", len(files_to_index), root)

    # Index concurrently (batched to avoid too many open file handles)
    semaphore = asyncio.Semaphore(16)

    async def _guarded(p: Path) -> None:
        async with semaphore:
            await index_file(str(p), str(root))

    await asyncio.gather(*(_guarded(p) for p in files_to_index))
    logger.info("Done indexing %s", root)


# ---------------------------------------------------------------------------
# AST Traversal helpers (private)
# ---------------------------------------------------------------------------


def _get_node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


# --- Python AST Traverser ---


def _extract_docstring_python(body_node: Node, source: bytes) -> Optional[str]:
    """Return the first string literal in *body_node* if it is a docstring."""
    for child in body_node.children:
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "string":
                    raw = _get_node_text(sub, source)
                    # Strip surrounding quotes
                    for q in ('"""', "'''", '"', "'"):
                        if (
                            raw.startswith(q)
                            and raw.endswith(q)
                            and len(raw) > 2 * len(q) - 1
                        ):
                            return raw[len(q) : -len(q)].strip()
                    return raw.strip()
        # Docstring must be the very first statement
        if child.type not in ("comment", "newline", "indent", "dedent"):
            break
    return None


def _get_identifier_python(node: Node, source: bytes) -> str:
    """Return the name identifier child of a function/class definition node."""
    for child in node.children:
        if child.type == "identifier":
            return _get_node_text(child, source)
    return "<unknown>"


def _get_body_python(node: Node) -> Optional[Node]:
    """Return the 'block' child of a function or class definition."""
    for child in node.children:
        if child.type == "block":
            return child
    return None


def _walk_node_python(
    node: Node,
    source: bytes,
    file_path: str,
    symbols: list[dict],
    imports: list[dict],
    parent_class: Optional[str],
) -> None:
    """Recursively walk the Python AST and extract symbols and imports."""

    if node.type == "class_definition":
        class_name = _get_identifier_python(node, source)
        body = _get_body_python(node)
        docstring = _extract_docstring_python(body, source) if body else None

        symbols.append(
            {
                "name": class_name,
                "kind": "class",
                "file_path": file_path,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "parent": parent_class,
                "docstring": docstring,
            }
        )

        if body:
            for child in body.children:
                _walk_node_python(
                    child,
                    source,
                    file_path,
                    symbols,
                    imports,
                    parent_class=class_name,
                )
        return

    if node.type == "function_definition":
        func_name = _get_identifier_python(node, source)
        body = _get_body_python(node)
        docstring = _extract_docstring_python(body, source) if body else None
        kind = "method" if parent_class is not None else "function"

        symbols.append(
            {
                "name": func_name,
                "kind": kind,
                "file_path": file_path,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "parent": parent_class,
                "docstring": docstring,
            }
        )

        if body:
            for child in body.children:
                _walk_node_python(
                    child, source, file_path, symbols, imports, parent_class=None
                )
        return

    if node.type == "import_statement":
        _extract_import_python(node, source, file_path, imports)
        return

    if node.type == "import_from_statement":
        _extract_import_from_python(node, source, file_path, imports)
        return

    for child in node.children:
        _walk_node_python(
            child, source, file_path, symbols, imports, parent_class=parent_class
        )


def _extract_import_python(
    node: Node, source: bytes, file_path: str, imports: list[dict]
) -> None:
    i = 0
    children = node.children
    while i < len(children):
        child = children[i]
        if child.type == "dotted_name" or child.type == "identifier":
            module = _get_node_text(child, source)
            alias = None
            if (
                i + 2 < len(children)
                and children[i + 1].type == "as"
                and children[i + 2].type == "identifier"
            ):
                alias = _get_node_text(children[i + 2], source)
                i += 2
            imports.append(
                {
                    "file_path": file_path,
                    "module": module,
                    "alias": alias,
                    "symbol": None,
                }
            )
        elif child.type == "aliased_import":
            parts = [
                c for c in child.children if c.type in ("dotted_name", "identifier")
            ]
            if len(parts) >= 1:
                module = _get_node_text(parts[0], source)
                alias = _get_node_text(parts[-1], source) if len(parts) >= 2 else None
                imports.append(
                    {
                        "file_path": file_path,
                        "module": module,
                        "alias": alias,
                        "symbol": None,
                    }
                )
        i += 1


def _extract_import_from_python(
    node: Node, source: bytes, file_path: str, imports: list[dict]
) -> None:
    module: Optional[str] = None
    for child in node.children:
        if child.type in ("dotted_name", "relative_import"):
            module = _get_node_text(child, source)
            break

    if module is None:
        return

    for child in node.children:
        if child.type == "import_from_as_name":
            parts = [c for c in child.children if c.type == "identifier"]
            sym = _get_node_text(parts[0], source) if parts else None
            alias = _get_node_text(parts[1], source) if len(parts) > 1 else None
            imports.append(
                {
                    "file_path": file_path,
                    "module": module,
                    "alias": alias,
                    "symbol": sym,
                }
            )
        elif child.type == "import_from_as_names" or child.type == "wildcard_import":
            for sub in child.children:
                if sub.type == "import_from_as_name":
                    parts = [c for c in sub.children if c.type == "identifier"]
                    sym = _get_node_text(parts[0], source) if parts else None
                    alias = _get_node_text(parts[1], source) if len(parts) > 1 else None
                    imports.append(
                        {
                            "file_path": file_path,
                            "module": module,
                            "alias": alias,
                            "symbol": sym,
                        }
                    )
                elif sub.type == "identifier":
                    imports.append(
                        {
                            "file_path": file_path,
                            "module": module,
                            "alias": None,
                            "symbol": _get_node_text(sub, source),
                        }
                    )
                elif sub.type == "wildcard_import":
                    imports.append(
                        {
                            "file_path": file_path,
                            "module": module,
                            "alias": None,
                            "symbol": "*",
                        }
                    )


# --- JavaScript / TypeScript AST Traverser ---


def _extract_jsdoc(node: Node, source: bytes) -> Optional[str]:
    """Extract and parse preceding JSDoc style comments for *node*."""
    prev = node.prev_sibling
    if prev and prev.type == "comment":
        text = _get_node_text(prev, source)
        if text.startswith("/**"):
            lines = []
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("/**"):
                    line = line[3:]
                elif line.endswith("*/"):
                    line = line[:-2]
                if line.startswith("*"):
                    line = line[1:]
                lines.append(line.strip())
            return "\n".join(lines).strip()
        return text.strip()
    return None


def _get_js_identifier(node: Node, source: bytes) -> str:
    """Find the first 'identifier' or 'type_identifier' child in the node."""
    for child in node.children:
        if child.type in ("identifier", "type_identifier"):
            return _get_node_text(child, source)
    return "<unknown>"


def _get_js_property_identifier(node: Node, source: bytes) -> str:
    """Find the property identifier or regular identifier name (for methods)."""
    for child in node.children:
        if child.type in ("property_identifier", "identifier"):
            return _get_node_text(child, source)
    return "<unknown>"


def _extract_import_js(
    node: Node, source: bytes, file_path: str, imports: list[dict]
) -> None:
    """Extract modules and symbols from standard ES import statements."""
    module = None
    for child in node.children:
        if child.type == "string":
            raw = _get_node_text(child, source)
            if (raw.startswith('"') and raw.endswith('"')) or (
                raw.startswith("'") and raw.endswith("'")
            ):
                module = raw[1:-1]
            else:
                module = raw
            break

    if not module:
        return

    clause = None
    for child in node.children:
        if child.type == "import_clause":
            clause = child
            break

    if not clause:
        # e.g. import "module-name";
        imports.append(
            {
                "file_path": file_path,
                "module": module,
                "alias": None,
                "symbol": None,
            }
        )
        return

    # namespace import: import * as name
    ns = None
    for child in clause.children:
        if child.type == "namespace_import":
            ns = child
            break
    if ns:
        alias = None
        for child in ns.children:
            if child.type == "identifier":
                alias = _get_node_text(child, source)
        imports.append(
            {
                "file_path": file_path,
                "module": module,
                "alias": alias,
                "symbol": "*",
            }
        )
        return

    # named imports: import { x, y as z }
    named = None
    for child in clause.children:
        if child.type == "named_imports":
            named = child
            break
    if named:
        for child in named.children:
            if child.type == "import_specifier":
                idents = [c for c in child.children if c.type == "identifier"]
                if len(idents) == 1:
                    symbol = _get_node_text(idents[0], source)
                    imports.append(
                        {
                            "file_path": file_path,
                            "module": module,
                            "alias": None,
                            "symbol": symbol,
                        }
                    )
                elif len(idents) >= 2:
                    symbol = _get_node_text(idents[0], source)
                    alias = _get_node_text(idents[1], source)
                    imports.append(
                        {
                            "file_path": file_path,
                            "module": module,
                            "alias": alias,
                            "symbol": symbol,
                        }
                    )
        return

    # default import: import defaultExport from "module"
    for child in clause.children:
        if child.type == "identifier":
            symbol = _get_node_text(child, source)
            imports.append(
                {
                    "file_path": file_path,
                    "module": module,
                    "alias": None,
                    "symbol": symbol,
                }
            )
            return

    imports.append(
        {
            "file_path": file_path,
            "module": module,
            "alias": None,
            "symbol": None,
        }
    )


def _walk_node_javascript(
    node: Node,
    source: bytes,
    file_path: str,
    symbols: list[dict],
    imports: list[dict],
    parent_class: Optional[str] = None,
) -> None:
    """Recursively walk JavaScript/TypeScript AST and extract declarations."""

    if node.type == "class_declaration":
        class_name = _get_js_identifier(node, source)
        docstring = _extract_jsdoc(node, source)
        symbols.append(
            {
                "name": class_name,
                "kind": "class",
                "file_path": file_path,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "parent": parent_class,
                "docstring": docstring,
            }
        )
        for child in node.children:
            if child.type == "class_body":
                for sub in child.children:
                    _walk_node_javascript(
                        sub,
                        source,
                        file_path,
                        symbols,
                        imports,
                        parent_class=class_name,
                    )
        return

    elif node.type == "method_definition":
        method_name = _get_js_property_identifier(node, source)
        docstring = _extract_jsdoc(node, source)
        symbols.append(
            {
                "name": method_name,
                "kind": "method",
                "file_path": file_path,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "parent": parent_class,
                "docstring": docstring,
            }
        )
        for child in node.children:
            if child.type == "statement_block":
                for sub in child.children:
                    _walk_node_javascript(
                        sub, source, file_path, symbols, imports, parent_class=None
                    )
        return

    elif node.type in ("function_declaration", "generator_function_declaration"):
        func_name = _get_js_identifier(node, source)
        docstring = _extract_jsdoc(node, source)
        kind = "method" if parent_class is not None else "function"
        symbols.append(
            {
                "name": func_name,
                "kind": kind,
                "file_path": file_path,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "parent": parent_class,
                "docstring": docstring,
            }
        )
        for child in node.children:
            if child.type == "statement_block":
                for sub in child.children:
                    _walk_node_javascript(
                        sub, source, file_path, symbols, imports, parent_class=None
                    )
        return

    elif node.type in ("lexical_declaration", "variable_declaration"):
        docstring = _extract_jsdoc(node, source)
        for child in node.children:
            if child.type == "variable_declarator":
                var_name = None
                var_val = None
                for sub in child.children:
                    if sub.type == "identifier":
                        var_name = _get_node_text(sub, source)
                    elif sub.type in (
                        "arrow_function",
                        "function",
                        "function_expression",
                    ):
                        var_val = sub

                if var_name and var_val:
                    kind = "method" if parent_class is not None else "function"
                    symbols.append(
                        {
                            "name": var_name,
                            "kind": kind,
                            "file_path": file_path,
                            "start_line": node.start_point[0] + 1,
                            "end_line": node.end_point[0] + 1,
                            "parent": parent_class,
                            "docstring": docstring,
                        }
                    )
                    for sub in var_val.children:
                        if sub.type in (
                            "statement_block",
                            "binary_expression",
                            "call_expression",
                        ):
                            _walk_node_javascript(
                                sub,
                                source,
                                file_path,
                                symbols,
                                imports,
                                parent_class=None,
                            )
        return

    elif node.type == "import_statement":
        _extract_import_js(node, source, file_path, imports)
        return

    for child in node.children:
        _walk_node_javascript(
            child, source, file_path, symbols, imports, parent_class=parent_class
        )