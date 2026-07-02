"""
agent_server/loop.py
===================
Core agentic loop executing against the OpenRouter API (Claude 3.5 Sonnet).

Exposes:
  run_agent_loop(task, max_iterations=20)
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai import AsyncOpenAI
from rich.console import Console

from agent_server.history import ConversationHistory

# ---------------------------------------------------------------------------
# OpenAI/OpenRouter Config
# ---------------------------------------------------------------------------

# Use OpenRouter endpoint as specified by user
_BASE_URL = "https://openrouter.ai/api/v1"
_MODEL_NAME = "anthropic/claude-3-haiku"

# ---------------------------------------------------------------------------
# Tool Definitions (OpenAI Function Schema)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_symbols",
            "description": (
                "Find functions, classes, or methods by PARTIAL NAME in the indexed "
                "codebase. Searches name LIKE '%query%'. "
                "Use this when you know part of a symbol's name. "
                "For broad questions like 'list all classes', use list_symbols instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Partial or full symbol NAME to search for (not a keyword like 'class')",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_symbols",
            "description": (
                "List all indexed symbols in the codebase, filtered by kind. "
                "Use this for broad questions: 'what classes exist?', 'list all functions'. "
                "Much more reliable than guessing names with search_symbols."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["class", "function", "method", "all"],
                        "description": "Filter by symbol kind. Use 'all' to see everything.",
                    }
                },
                "required": ["kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_callers",
            "description": "Find all places in the codebase that call a given function by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "function_name": {
                        "type": "string",
                        "description": "Exact function name to search for callers of",
                    }
                },
                "required": ["function_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_code",
            "description": "Read specific lines from a source file. Use start_line and end_line from search_symbols results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["file_path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_imports",
            "description": "List all imports in a file. Use to understand dependencies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"}
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": (
                "Search for concepts or natural language queries in the indexed codebase "
                "using vector similarity search (pgvector)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language concept to search for (e.g. 'user login logic')",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_architecture",
            "description": (
                "Generate a Mermaid.js class diagram illustrating the object-oriented "
                "architecture and class inheritance relationships in the codebase."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "final_answer",
            "description": (
                "Call this ONLY when you have a complete, thorough answer. "
                "Do not call this until you have read enough code to be confident."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": (
                            "Your complete, detailed answer with file references and line numbers"
                        ),
                    }
                },
                "required": ["answer"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------

AGENT_SYSTEM_PROMPT = """You are CodeAgent, an expert code analysis AI.

You have tools to explore a codebase. Use them methodically to answer the user's question.

STRATEGY:
1. Start with search_symbols or semantic_search to find relevant functions/classes
2. Use read_code to read the actual implementation (use start_line and end_line from search results)
3. Use find_callers to trace who calls a function
4. Use get_imports to understand a file's dependencies
5. Use generate_architecture for high-level class diagrams
6. Keep exploring until you fully understand the flow
7. Call final_answer with a detailed, well-structured answer that includes file paths and line numbers

RULES:
- Never guess — read the actual code before drawing conclusions
- Always include file:line references in your final answer
- Be thorough but efficient — don't read the same file twice
- If you search and find nothing, try a different query
- Call final_answer when you are confident, not before"""

# ---------------------------------------------------------------------------
# Core Agent Loop
# ---------------------------------------------------------------------------


async def run_agent_loop(
    task: str, session_id: str = "", max_iterations: int = 30
) -> str:
    """Run the main agentic code-reasoning loop against OpenRouter.

    Loops up to *max_iterations* times, driving Claude via OpenRouter and
    calling local repository analysis tools as requested.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key or api_key == "sk-or-...":
        return (
            "ERROR: OPENROUTER_API_KEY env variable is not set. "
            "Please check your .env file."
        )

    client = AsyncOpenAI(
        base_url=_BASE_URL,
        api_key=api_key,
    )

    history = ConversationHistory(AGENT_SYSTEM_PROMPT)
    history.add_user(task)

    console = Console()
    console.print(f"\n[bold purple]◆ CodeAgent[/bold purple] — {task}\n")

    for i in range(max_iterations):
        console.print(f"[dim]── step {i+1} ──[/dim]")

        # Call OpenAI completions
        # Messages from history.get_messages() already has the prepended system prompt
        response = await client.chat.completions.create(
            model=_MODEL_NAME,
            messages=history.get_messages(),
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=4096,
        )

        msg = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        # Serialise tool_calls safely for history
        tcalls_dump = None
        if msg.tool_calls:
            tcalls_dump = []
            for tc in msg.tool_calls:
                if hasattr(tc, "model_dump"):
                    tcalls_dump.append(tc.model_dump())
                else:
                    tcalls_dump.append(
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                    )

        history.add_assistant(content=msg.content or "", tool_calls=tcalls_dump)

        # No tool calls — model gave a plain text answer
        if finish_reason == "stop" or not msg.tool_calls:
            return msg.content or "No answer generated."

        final_answer_text = None

        # Execute any tool calls emitted by the model
        for tool_call in msg.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError as exc:
                console.print(f"[red]! Error decoding tool arguments:[/red] {exc}")
                history.add_tool_result(tool_call.id, f"ERROR: Invalid JSON arguments: {exc}")
                continue

            console.print(
                f"[green]→[/green] [bold]{fn_name}[/bold]({json.dumps(fn_args)})"
            )

            if fn_name == "final_answer":
                final_answer_text = fn_args.get("answer")
                history.add_tool_result(tool_call.id, "Answer recorded.")
                break

            # Execute the tool on the local filesystem / database index
            result = await _execute_tool(fn_name, fn_args, session_id=session_id)

            # Safeguard context size by truncating extremely long tool outputs
            if len(result) > 6000:
                result = (
                    result[:6000]
                    + "\n... [truncated — use more specific queries]"
                )

            preview = result[:150].replace("\n", " ")
            console.print(f"   [dim]{preview}...[/dim]")

            history.add_tool_result(tool_call.id, result)

        if final_answer_text is not None:
            console.print("\n[bold green]✓ Complete[/bold green]\n")
            return final_answer_text

    return "Reached max iterations. Partial context may be incomplete."


# ---------------------------------------------------------------------------
# Tool Execution Router
# ---------------------------------------------------------------------------


async def _execute_tool(
    name: str, args: dict[str, Any], session_id: str = ""
) -> str:
    """Route tool call execution to the underlying code server tools."""
    from code_server import tools as ct

    repo = os.environ.get("CODEAGENT_REPO", ".")

    try:
        if name == "search_symbols":
            query = args.get("query", "")
            result = await ct.find_function(query, session_id=session_id)
            return json.dumps(result, indent=2)

        elif name == "list_symbols":
            kind = args.get("kind", "all")
            result = await ct.list_symbols(kind=kind, session_id=session_id)
            return json.dumps(result, indent=2)

        elif name == "find_callers":
            func_name = args.get("function_name", "")
            result = await ct.get_callers(func_name, session_id, repo)
            return json.dumps(result, indent=2)

        elif name == "read_code":
            file_path = args.get("file_path", "")
            start = int(args.get("start_line", 1))
            end = int(args.get("end_line", 1))
            return await ct.read_file_slice(file_path, start, end)

        elif name == "get_imports":
            file_path = args.get("file_path", "")
            result = await ct.list_imports(file_path, session_id=session_id)
            return json.dumps(result, indent=2)

        elif name == "generate_architecture":
            return await ct.generate_architecture(session_id=session_id)

        elif name == "semantic_search":
            query = args.get("query", "")
            result = await ct.semantic_search(query, session_id=session_id)
            return json.dumps(result, indent=2)

        else:
            return f"Unknown tool: {name}"

    except Exception as exc:
        return f"ERROR: Execution of tool '{name}' failed: {exc}"
