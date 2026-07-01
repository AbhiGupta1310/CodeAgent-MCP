"""
code_server/db.py
=================
Centralized Postgres connection pool and schema initialization.

All database access in the project goes through get_pool().
Schema is created once at server startup via init_schema().
"""

from __future__ import annotations

import logging
import os

import asyncpg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection Pool (lazy singleton)
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Return the shared asyncpg connection pool.

    Creates the pool on first call using the DATABASE_URL environment
    variable.  Subsequent calls return the same pool instance.

    Raises
    ------
    RuntimeError
        If DATABASE_URL is not set.
    """
    global _pool
    if _pool is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL environment variable is not set. "
                "Please add it to your .env file."
            )
        _pool = await asyncpg.create_pool(
            url.strip(),
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        logger.info("Postgres connection pool created (min=2, max=10)")
    return _pool


async def close_pool() -> None:
    """Gracefully close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Postgres connection pool closed")


# ---------------------------------------------------------------------------
# Schema Initialization
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
-- Sessions table: one row per GitHub repo clone
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    github_url   TEXT NOT NULL,
    repo_path    TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    last_active  TIMESTAMPTZ DEFAULT NOW(),
    status       TEXT DEFAULT 'indexing'
);

-- Symbols table: extracted functions, classes, methods
CREATE TABLE IF NOT EXISTS symbols (
    id          SERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    parent      TEXT,
    docstring   TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_name
    ON symbols(session_id, name);
CREATE INDEX IF NOT EXISTS idx_session_file
    ON symbols(session_id, file_path);

-- Imports table: extracted import statements
CREATE TABLE IF NOT EXISTS imports (
    id          SERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    module      TEXT NOT NULL,
    alias       TEXT,
    symbol      TEXT
);
CREATE INDEX IF NOT EXISTS idx_import_session
    ON imports(session_id, file_path);
"""


async def init_schema() -> None:
    """Create all database tables and indexes (idempotent).

    Call once at server startup.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    logger.info("Postgres schema initialized (sessions, symbols, imports)")
