"""
cli/main.py
===========
Typer + Rich CLI interface for CodeAgent.

Commands
--------
  codeagent index [repo_path] — Index a repository
  codeagent ask "question"    — Ask a code-reasoning question
"""

from __future__ import annotations

import asyncio
import os
import sys

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

# Load environment variables early so import dependencies can read them
load_dotenv()

app = typer.Typer(help="CodeAgent — Ask questions about any codebase")
console = Console()


@app.command()
def ask(
    question: str = typer.Argument(
        ...,
        help="Question to ask about the codebase",
    ),
    repo: str = typer.Option(
        None,          # None = "not supplied by user"
        "--repo",
        "-r",
        help="Path to the repository to analyse (default: CODEAGENT_REPO env var or '.')",
    ),
    reindex: bool = typer.Option(
        False,
        "--index",
        "-i",
        help="Force a full re-index before running the query",
    ),
) -> None:
    """Ask a natural language question about a codebase."""
    # Priority: --repo CLI flag > CODEAGENT_REPO in .env > current directory
    effective_repo = repo or os.environ.get("CODEAGENT_REPO") or "."
    os.environ["CODEAGENT_REPO"] = effective_repo

    if reindex:
        from code_server.indexer import index_repo

        console.print(f"[yellow]◆ Re-indexing repository: {effective_repo}...[/yellow]")
        asyncio.run(index_repo(effective_repo))
        console.print("[green]✓ Indexing complete.[/green]\n")

    # If the user hasn't set up the API key, let them know clearly
    if not os.environ.get("OPENROUTER_API_KEY"):
        console.print(
            "[red]Error: OPENROUTER_API_KEY environment variable is missing.[/red]\n"
            "Please create a [bold].env[/bold] file or set the environment variable."
        )
        sys.exit(1)

    from agent_server.loop import run_agent_loop

    console.print(f"[dim]◆ CodeAgent — {question}[/dim]\n")

    # Run the main agent loop
    try:
        answer = asyncio.run(run_agent_loop(question))
    except Exception as exc:
        console.print(f"[bold red]System Error running agent loop:[/bold red] {exc}")
        sys.exit(1)

    # Render final output with beautiful formatting
    console.print("\n")
    console.print(
        Panel(
            Markdown(answer),
            title="[bold purple]CodeAgent Response[/bold purple]",
            border_style="purple",
            padding=(1, 2),
        )
    )


@app.command()
def index(
    repo: str = typer.Argument(
        None,
        help="Path to the repository to index (default: CODEAGENT_REPO env var or '.')",
    )
) -> None:
    """Index a repository for analysis (populates SQLite symbol tables)."""
    # Priority: argument > CODEAGENT_REPO in .env > current directory
    effective_repo = repo or os.environ.get("CODEAGENT_REPO") or "."
    # Set env var FIRST — get_db_path() reads this at call time
    os.environ["CODEAGENT_REPO"] = effective_repo
    from code_server.indexer import index_repo

    console.print(f"[yellow]◆ Indexing repository: {effective_repo}...[/yellow]")
    try:
        asyncio.run(index_repo(effective_repo))
        console.print("[green]✓ Done — ready to answer questions![/green]")
    except Exception as exc:
        console.print(f"[bold red]Indexing failed:[/bold red] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    app()

