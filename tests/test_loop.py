"""
tests/test_loop.py
==================
Tests for the agent_server package, including:
1. ConversationHistory tests
2. run_agent_loop mock testing (using unittest.mock to avoid real API calls)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_server.history import ConversationHistory
from agent_server.loop import run_agent_loop

# ---------------------------------------------------------------------------
# ConversationHistory Tests
# ---------------------------------------------------------------------------

SYSTEM = "You are a code reasoning agent."
TOOL_CALL = {
    "id": "call_abc123",
    "type": "function",
    "function": {
        "name": "search_symbols",
        "arguments": json.dumps({"query": "login"}),
    },
}


def test_empty_history_has_no_messages():
    h = ConversationHistory(SYSTEM)
    assert len(h) == 0


def test_get_messages_always_starts_with_system():
    h = ConversationHistory(SYSTEM)
    msgs = h.get_messages()
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == SYSTEM


def test_get_messages_system_only_when_no_turns():
    h = ConversationHistory(SYSTEM)
    msgs = h.get_messages()
    assert len(msgs) == 1


def test_add_user_string():
    h = ConversationHistory(SYSTEM)
    h.add_user("Hello!")
    msgs = h.get_messages()
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "Hello!"


def test_add_user_list_content():
    h = ConversationHistory(SYSTEM)
    parts = [{"type": "text", "text": "Explain foo()"}]
    h.add_user(parts)
    msgs = h.get_messages()
    assert msgs[1]["content"] == parts


def test_add_assistant_text_only():
    h = ConversationHistory(SYSTEM)
    h.add_user("Hi")
    h.add_assistant("Hello back")
    msgs = h.get_messages()
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "Hello back"
    assert "tool_calls" not in msgs[2]


def test_add_assistant_with_tool_calls():
    h = ConversationHistory(SYSTEM)
    h.add_user("Find login")
    h.add_assistant(None, tool_calls=[TOOL_CALL])
    msgs = h.get_messages()
    asst = msgs[2]
    assert asst["role"] == "assistant"
    assert asst["content"] is None
    assert asst["tool_calls"] == [TOOL_CALL]


def test_add_tool_result_format():
    h = ConversationHistory(SYSTEM)
    h.add_user("Find login")
    h.add_assistant(None, tool_calls=[TOOL_CALL])
    h.add_tool_result("call_abc123", '[{"name": "login", "kind": "function"}]')

    msgs = h.get_messages()
    tool_msg = msgs[3]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_abc123"
    assert '"login"' in tool_msg["content"]


def test_three_turn_conversation_structure():
    h = ConversationHistory(SYSTEM)
    h.add_user("Where is the login function defined?")
    h.add_assistant(None, tool_calls=[TOOL_CALL])
    h.add_tool_result(
        "call_abc123",
        json.dumps([{"name": "login", "file_path": "auth.py", "start_line": 42}]),
    )
    h.add_assistant("The `login` function is defined in `auth.py` at line 42.")

    msgs = h.get_messages()
    assert len(msgs) == 5
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]


def test_len_counts_non_system_messages():
    h = ConversationHistory(SYSTEM)
    h.add_user("A")
    h.add_assistant("B")
    assert len(h) == 2


def test_clear_resets_messages():
    h = ConversationHistory(SYSTEM)
    h.add_user("Hello")
    h.clear()
    assert len(h) == 0
    msgs = h.get_messages()
    assert msgs[0]["content"] == SYSTEM


# ---------------------------------------------------------------------------
# run_agent_loop Mock Tests
# ---------------------------------------------------------------------------


class MockToolCall:
    """Mock structure matching the structure of OpenAI's Choice.Message.ToolCall."""

    def __init__(self, call_id: str, name: str, arguments: str):
        self.id = call_id
        self.type = "function"
        self.function = MagicMock()
        self.function.name = name
        self.function.arguments = arguments

    def model_dump(self):
        return {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


@pytest.mark.asyncio
@patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-or-testkey"})
@patch("agent_server.loop.AsyncOpenAI")
async def test_run_agent_loop_stops_on_plain_text(mock_client_cls):
    """If the LLM responds with plain text and no tool calls, return the answer directly."""
    # Set up mock completion response
    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message = MagicMock()
    mock_choice.message.content = "This is the final answer."
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_client = MagicMock()
    mock_client.chat.returns_val = mock_response
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    mock_client_cls.return_value = mock_client

    ans = await run_agent_loop("Hello task", max_iterations=1)
    assert ans == "This is the final answer."
    assert mock_client.chat.completions.create.await_count == 1


@pytest.mark.asyncio
@patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-or-testkey"})
@patch("agent_server.loop.AsyncOpenAI")
@patch("agent_server.loop._execute_tool")
async def test_run_agent_loop_runs_tool_then_final_answer(mock_exec_tool, mock_client_cls):
    """Test a two-step completion where step 1 calls a tool, and step 2 calls final_answer."""
    mock_exec_tool.return_value = '{"status": "indexed"}'

    # Setup step 1 response (call tool search_symbols)
    call_1 = MockToolCall("c1", "search_symbols", '{"query": "myfunc"}')
    choice_1 = MagicMock()
    choice_1.finish_reason = "tool_calls"
    choice_1.message = MagicMock()
    choice_1.message.content = None
    choice_1.message.tool_calls = [call_1]

    resp_1 = MagicMock()
    resp_1.choices = [choice_1]

    # Setup step 2 response (call tool final_answer)
    call_2 = MockToolCall("c2", "final_answer", '{"answer": "Function exists at line 10."}')
    choice_2 = MagicMock()
    choice_2.finish_reason = "tool_calls"
    choice_2.message = MagicMock()
    choice_2.message.content = None
    choice_2.message.tool_calls = [call_2]

    resp_2 = MagicMock()
    resp_2.choices = [choice_2]

    # Mock completions.create to return resp_1 then resp_2
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=[resp_1, resp_2])
    mock_client_cls.return_value = mock_client

    ans = await run_agent_loop("Find myfunc", max_iterations=5)

    assert ans == "Function exists at line 10."
    assert mock_client.chat.completions.create.await_count == 2
    mock_exec_tool.assert_called_once_with("search_symbols", {"query": "myfunc"})


@pytest.mark.asyncio
@patch.dict("os.environ", {}, clear=True)
async def test_run_agent_loop_fails_if_key_missing():
    ans = await run_agent_loop("Find myfunc")
    assert "ERROR: OPENROUTER_API_KEY env variable is not set" in ans


@pytest.mark.asyncio
@patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-or-testkey"})
@patch("agent_server.loop.AsyncOpenAI")
async def test_run_agent_loop_returns_final_answer_immediately(mock_client_cls):
    """Test that the loop terminates immediately and returns the answer when the model returns a final_answer tool call in the first step."""
    call = MockToolCall("c1", "final_answer", '{"answer": "Immediate answer."}')
    choice = MagicMock()
    choice.finish_reason = "tool_calls"
    choice.message = MagicMock()
    choice.message.content = None
    choice.message.tool_calls = [call]

    resp = MagicMock()
    resp.choices = [choice]

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=resp)
    mock_client_cls.return_value = mock_client

    ans = await run_agent_loop("Find immediate", max_iterations=5)
    assert ans == "Immediate answer."
    assert mock_client.chat.completions.create.await_count == 1

