"""
agent_server/history.py
=======================
Clean, serialisable conversation history for the OpenAI/OpenRouter chat
completions format.

Usage
-----
    hist = ConversationHistory(system_prompt="You are a helpful assistant.")
    hist.add_user("What does foo() do?")
    hist.add_assistant("It does X.", tool_calls=[...])
    hist.add_tool_result("call_abc", "result string")
    messages = hist.get_messages()  # pass to client.chat.completions.create()
"""

from __future__ import annotations

import copy
from typing import Any


class ConversationHistory:
    """Manages multi-turn message history in OpenAI chat-completions format.

    The *system prompt* is stored separately and prepended by
    :meth:`get_messages` — it is never mutated by turn operations.

    Message roles used:
    - ``"user"``      — human turn (string or list of content parts)
    - ``"assistant"`` — model turn (may carry ``tool_calls``)
    - ``"tool"``      — result of a tool call (identified by ``tool_call_id``)
    """

    def __init__(self, system_prompt: str) -> None:
        self.system_prompt: str = system_prompt
        self.messages: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def add_user(self, content: str | list) -> None:
        """Append a user turn.

        Parameters
        ----------
        content:
            Either a plain string or a list of OpenAI content-part dicts
            (e.g. ``[{"type": "text", "text": "..."}]``).
        """
        self.messages.append({"role": "user", "content": content})

    def add_assistant(
        self,
        content: str | None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        """Append an assistant turn.

        Parameters
        ----------
        content:
            The assistant's text reply.  May be ``None`` when the model
            only emits tool calls (OpenAI spec allows ``null`` content).
        tool_calls:
            List of tool-call objects in OpenAI format::

                [{
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "search_symbols",
                        "arguments": "{\"query\": \"login\"}"
                    }
                }]
        """
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        """Append the result of a tool call.

        Parameters
        ----------
        tool_call_id:
            The ``id`` that was present in the corresponding
            ``assistant.tool_calls`` entry (e.g. ``"call_abc"``).
        content:
            The tool's string output (JSON-stringified or plain text).
        """
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
            }
        )

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_messages(self) -> list[dict[str, Any]]:
        """Return the full message list in OpenAI format.

        The system prompt is prepended as the first message with
        ``role="system"``.  A deep-copy is returned so callers cannot
        accidentally mutate internal state.

        Returns
        -------
        list[dict]
            Ready to pass as the ``messages`` argument to
            ``client.chat.completions.create()``.
        """
        system_msg = {"role": "system", "content": self.system_prompt}
        return [system_msg] + copy.deepcopy(self.messages)

    def token_estimate(self) -> int:
        """Rough token count (len(str(messages)) // 4).

        Not exact — use only for budget-checking before hitting the API.
        """
        return sum(len(str(m)) for m in self.get_messages()) // 4

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Reset the conversation (keep the system prompt)."""
        self.messages = []

    def __len__(self) -> int:
        """Return the number of non-system messages."""
        return len(self.messages)

    def __repr__(self) -> str:
        return (
            f"ConversationHistory("
            f"turns={len(self)}, "
            f"tokens≈{self.token_estimate()})"
        )
