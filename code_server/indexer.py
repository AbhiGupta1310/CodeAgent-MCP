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
from fastembed import TextEmbedding

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

_embedding_model: Optional[TextEmbedding] = None

def get_embedding_model() -> TextEmbedding:
    global _embedding_model
    if _embedding_model is None:
        logger.info("Loading FastEmbed model (BAAI/bge-small-en-v1.5) ...")
        _embedding_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    return _embedding_model



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


def parse_file_ast(file_path: str) -> tuple[list[dict], list[dict]]:
    """Parse source file into AST and extract symbols and imports in-memory."""
    path = Path(file_path).resolve()
    lang = _get_language_for_extension(str(path))
    if not lang:
        return [], []

    try:
        source = path.read_bytes()
    except OSError as exc:
        logger.warning("Cannot read %s: %s", file_path, exc)
        return [], []

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

    return symbols, imports


async def generate_embeddings_batch(symbols: list[dict]) -> None:
    """Generate 384-dim vector embeddings via Hugging Face API or OpenRouter API.

    Uses cloud API embeddings to eliminate CPU/RAM usage on cloud servers (Render/Railway).
    Falls back to local FastEmbed with low batch size if no API key is available.
    """
    if not symbols:
        return

    texts = [
        f"{s['kind']} {s['name']} " + (s['docstring'] if s['docstring'] else "")
        for s in symbols
    ]

    hf_token = os.environ.get("HF_TOKEN", "").strip()
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()

    # Tier 1: Hugging Face Inference API
    if hf_token:
        try:
            import httpx

            logger.info(
                "Generating embeddings via Hugging Face API (%d symbols, 0%% CPU, 0 MB RAM) ...",
                len(symbols),
            )
            headers = {"Authorization": f"Bearer {hf_token}"}
            url = "https://router.huggingface.co/hf-inference/models/BAAI/bge-small-en-v1.5"

            batch_size = 32
            all_embeddings: list[list[float]] = []

            async with httpx.AsyncClient(timeout=30.0) as client:
                for i in range(0, len(texts), batch_size):
                    chunk_texts = texts[i : i + batch_size]
                    payload = {
                        "inputs": chunk_texts,
                        "options": {"wait_for_model": True},
                    }
                    resp = await client.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    chunk_embs = resp.json()
                    all_embeddings.extend(chunk_embs)

            for s, emb in zip(symbols, all_embeddings):
                s["embedding"] = str(emb)
            return
        except Exception as exc:
            logger.warning(
                "Hugging Face API request failed, trying OpenRouter fallback: %s", exc
            )

    # Tier 2: OpenRouter API
    if openrouter_key:
        try:
            import httpx

            logger.info(
                "Generating embeddings via OpenRouter API (%d symbols, 0%% CPU, 0 MB RAM) ...",
                len(symbols),
            )
            headers = {
                "Authorization": f"Bearer {openrouter_key}",
                "Content-Type": "application/json",
            }
            batch_size = 64
            all_embeddings = []

            async with httpx.AsyncClient(timeout=30.0) as client:
                for i in range(0, len(texts), batch_size):
                    chunk_texts = texts[i : i + batch_size]
                    payload = {
                        "model": "openai/text-embedding-3-small",
                        "input": chunk_texts,
                        "dimensions": 384,
                    }
                    resp = await client.post(
                        "https://openrouter.ai/api/v1/embeddings",
                        headers=headers,
                        json=payload,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    chunk_embs = [item["embedding"] for item in data["data"]]
                    all_embeddings.extend(chunk_embs)

            for s, emb in zip(symbols, all_embeddings):
                s["embedding"] = str(emb)
            return
        except Exception as exc:
            logger.warning(
                "OpenRouter API request failed, falling back to local FastEmbed: %s", exc
            )

    # Tier 3: Local FastEmbed fallback
    logger.info(
        "No HF_TOKEN or OPENROUTER_API_KEY set. Generating embeddings locally via FastEmbed (batch_size=16) ..."
    )
    model = get_embedding_model()
    embeddings_gen = await asyncio.to_thread(model.embed, texts, batch_size=16)
    embeddings = await asyncio.to_thread(list, embeddings_gen)
    for s, emb in zip(symbols, embeddings):
        s["embedding"] = str(emb.tolist())


async def index_file(file_path: str, session_id: str) -> None:
    """Parse *file_path* using its language parser and upsert its symbols into Postgres."""
    symbols, imports = parse_file_ast(file_path)
    if not symbols and not imports:
        return

    if symbols:
        await generate_embeddings_batch(symbols)

    path_str = str(Path(file_path).resolve())
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM symbols WHERE session_id = $1 AND file_path = $2",
                session_id,
                path_str,
            )
            await conn.execute(
                "DELETE FROM imports WHERE session_id = $1 AND file_path = $2",
                session_id,
                path_str,
            )

            if symbols:
                await conn.executemany(
                    """
                    INSERT INTO symbols (session_id, name, kind, file_path, start_line, end_line, parent, docstring, embedding)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
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
                            s.get("embedding"),
                        )
                        for s in symbols
                    ],
                )

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


async def index_repo(repo_path: str, session_id: str) -> None:
    """Walk *repo_path*, parse all source files into symbols & imports,
    generate vector embeddings in a single repository-wide batch, and bulk-insert
    all data into Postgres inside a single transaction.
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

    logger.info("Parsing %d source files in %s …", len(files_to_index), root)

    # 1. Parse all ASTs in parallel in-memory
    def _parse_all() -> tuple[list[dict], list[dict]]:
        syms: list[dict] = []
        imps: list[dict] = []
        for p in files_to_index:
            s_list, i_list = parse_file_ast(str(p))
            syms.extend(s_list)
            imps.extend(i_list)
        return syms, imps

    all_symbols, all_imports = await asyncio.to_thread(_parse_all)

    logger.info(
        "Extracted %d symbols and %d imports across %d files.",
        len(all_symbols),
        len(all_imports),
        len(files_to_index),
    )

    # 2. Vectorized Batch Embeddings for all symbols across the repository
    if all_symbols:
        logger.info("Generating embeddings for %d symbols in vectorized batch pass...", len(all_symbols))
        await generate_embeddings_batch(all_symbols)

    # 3. Single Bulk Database Transaction over 1 Connection
    logger.info("Writing symbols and imports to database in a single transaction...")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM symbols WHERE session_id = $1", session_id)
            await conn.execute("DELETE FROM imports WHERE session_id = $1", session_id)

            if all_symbols:
                await conn.executemany(
                    """
                    INSERT INTO symbols (session_id, name, kind, file_path, start_line, end_line, parent, docstring, embedding)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
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
                            s.get("embedding"),
                        )
                        for s in all_symbols
                    ],
                )

            if all_imports:
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
                        for imp in all_imports
                    ],
                )

    logger.info("Done indexing %s in single bulk pass.", root)


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