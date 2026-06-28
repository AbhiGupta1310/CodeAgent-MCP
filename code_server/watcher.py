"""
code_server/watcher.py
======================
Watchdog-based file watcher that detects modifications to Python source files
and automatically schedules re-indexing in the background database.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

from rich.console import Console
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from code_server.indexer import SKIP_DIRS, index_file

console = Console()


class _Handler(FileSystemEventHandler):
    """File modification event handler scheduling indexing in a background event loop."""

    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="CodeAgentWatcherLoop",
            daemon=True,
        )
        self._thread.start()

    def on_modified(self, event) -> None:
        """Handle watchdog on_modified callback."""
        if event.is_directory:
            return

        src_path = event.src_path
        ext = Path(src_path).suffix.lower()
        if ext not in (
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
            return

        # Path-filtering: Skip if file lies within any of the excluded directories
        path_parts = Path(src_path).parts
        if any(d in SKIP_DIRS for d in path_parts):
            return

        # Schedule the re-indexing coroutine to run on the background loop
        asyncio.run_coroutine_threadsafe(self._reindex(src_path), self._loop)

    async def _reindex(self, path: str) -> None:
        """Coroutine that executes file indexing and prints status."""
        try:
            await index_file(path)
            console.print(f"[dim]↺ re-indexed {os.path.basename(path)}[/dim]")
        except Exception as exc:
            console.print(
                f"[red]! Failed to re-index {os.path.basename(path)}: {exc}[/red]"
            )

    def shutdown(self) -> None:
        """Shut down the background loop and stop the thread."""
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)


# A module-level reference to the active handler to allow clean shutdown
_active_handler: _Handler | None = None


def start_watcher(repo_path: str) -> Observer:
    """Schedule and start watching *repo_path* for Python file edits.

    Returns the watchdog Observer instance.
    """
    global _active_handler
    _active_handler = _Handler()

    observer = Observer()
    observer.schedule(_active_handler, repo_path, recursive=True)
    observer.start()
    return observer


def stop_watcher(observer: Observer) -> None:
    """Stop the watchdog observer and shut down the background indexer loop."""
    global _active_handler
    observer.stop()
    observer.join()

    if _active_handler:
        _active_handler.shutdown()
        _active_handler = None
