"""
code_server/sessions.py
=======================
Session management for multi-tenant GitHub repo analysis.

Each session represents one cloned GitHub repository with:
- A unique UUID session_id
- A directory on disk: /tmp/codeagent_sessions/{session_id}/repo/
- A row in the Postgres sessions table
- TTL of 24 hours (cleaned up by background task)
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from code_server.db import get_pool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SESSIONS_BASE_DIR = Path("/tmp/codeagent_sessions")
SESSION_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_session(github_url: str) -> tuple[str, str]:
    """Create a new session for a GitHub repository.

    Inserts a row into the sessions table and creates the disk directory.

    Parameters
    ----------
    github_url:
        The public GitHub URL to clone.

    Returns
    -------
    tuple of (session_id, repo_path)
    """
    session_id = str(uuid.uuid4())
    repo_path = str(SESSIONS_BASE_DIR / session_id / "repo")

    # Create disk directory
    Path(repo_path).mkdir(parents=True, exist_ok=True)

    # Insert into Postgres
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (id, github_url, repo_path, status)
            VALUES ($1, $2, $3, 'indexing')
            """,
            session_id,
            github_url,
            repo_path,
        )

    logger.info("Created session %s for %s", session_id, github_url)
    return session_id, repo_path


async def get_repo_path(session_id: str) -> str | None:
    """Return the disk path to the cloned repo for a session.

    Returns None if the session does not exist.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT repo_path FROM sessions WHERE id = $1",
            session_id,
        )
    return row["repo_path"] if row else None


async def get_session_status(session_id: str) -> str | None:
    """Return the status of a session ('indexing', 'ready', 'error').

    Returns None if the session does not exist.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM sessions WHERE id = $1",
            session_id,
        )
    return row["status"] if row else None


async def session_exists(session_id: str) -> bool:
    """Check if a session exists in the database."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT 1 FROM sessions WHERE id = $1",
            session_id,
        )
    return row is not None


async def update_session_status(session_id: str, status: str) -> None:
    """Update the status of a session."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET status = $1 WHERE id = $2",
            status,
            session_id,
        )


async def update_last_active(session_id: str) -> None:
    """Update the last_active timestamp for a session."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET last_active = NOW() WHERE id = $1",
            session_id,
        )


async def cleanup_expired_sessions() -> int:
    """Delete sessions older than SESSION_TTL_HOURS from Postgres and disk.

    Returns the number of sessions deleted.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Fetch expired sessions
        rows = await conn.fetch(
            f"""
            SELECT id, repo_path FROM sessions
            WHERE last_active < NOW() - INTERVAL '{SESSION_TTL_HOURS} hours'
            """,
        )

        if not rows:
            return 0

        expired_ids = [row["id"] for row in rows]

        # Delete from database (cascade: symbols and imports too)
        await conn.execute(
            "DELETE FROM imports WHERE session_id = ANY($1::text[])",
            expired_ids,
        )
        await conn.execute(
            "DELETE FROM symbols WHERE session_id = ANY($1::text[])",
            expired_ids,
        )
        await conn.execute(
            "DELETE FROM sessions WHERE id = ANY($1::text[])",
            expired_ids,
        )

    # Clean up disk directories
    for row in rows:
        session_dir = Path(row["repo_path"]).parent  # /tmp/.../session_id/
        if session_dir.exists():
            try:
                shutil.rmtree(session_dir)
                logger.info("Removed expired session directory: %s", session_dir)
            except OSError as exc:
                logger.warning("Failed to remove %s: %s", session_dir, exc)

    logger.info("Cleaned up %d expired sessions", len(rows))
    return len(rows)
