"""
code_server/indexer.py
======================
Tree-sitter AST parser → Postgres symbol index.
Supports Python, JavaScript (JSX), and TypeScript (TSX).

Exports
-------
index_file(file_path, session_id)  — parse one source file and upsert its symbols
index_repo(repo_path, session_id)  — walk all source files in a repo and index them
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import tree_sitter_python as tspython
import tree_sitter_javascript as tsjavascript
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

from code_server.db import get_pool

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
# Public API
# ---------------------------------------------------------------------------


async def index_file(file_path: str, session_id: str) -> None:
    """Parse *file_path* using its language parser and upsert its symbols into Postgres."""
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

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Clean-delete all previous data for this file before re-inserting
        await conn.execute(
            "DELETE FROM symbols WHERE session_id = $1 AND file_path = $2",
            session_id,
            str(path),
        )
        await conn.execute(
            "DELETE FROM imports WHERE session_id = $1 AND file_path = $2",
            session_id,
            str(path),
        )

        # Batch insert symbols
        if symbols:
            await conn.executemany(
                """
                INSERT INTO symbols (session_id, name, kind, file_path, start_line, end_line, parent, docstring)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                [
                    (
                        session_id,
                        s["name"],
                        s["kind"],
                        s["file_path"],
                        s["start_line"],
                        s["end_line"],
                        s["parent"],
                        s["docstring"],
                    )
                    for s in symbols
                ],
            )

        # Batch insert imports
        if imports:
            await conn.executemany(
                """
                INSERT INTO imports (session_id, file_path, module, alias, symbol)
                VALUES ($1, $2, $3, $4, $5)
                """,
                [
                    (
                        session_id,
                        imp["file_path"],
                        imp["module"],
                        imp["alias"],
                        imp["symbol"],
                    )
                    for imp in imports
                ],
            )

    logger.debug(
        "Indexed %s → %d symbols, %d imports", path.name, len(symbols), len(imports)
    )


async def index_repo(repo_path: str, session_id: str) -> None:
    """Walk *repo_path*, find all source files, and index them.

    Skips directories in SKIP_DIRS.
    """
    root = Path(repo_path).resolve()

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
        logger.info("No source files found under %s — index is empty.", root)
        return

    logger.info("Indexing %d source files in %s …", len(files_to_index), root)

    # Index concurrently (batched to avoid too many open file handles)
    semaphore = asyncio.Semaphore(16)

    async def _guarded(p: Path) -> None:
        async with semaphore:
            await index_file(str(p), session_id)

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