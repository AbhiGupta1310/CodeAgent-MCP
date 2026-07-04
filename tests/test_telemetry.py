import asyncio
import pytest
from code_server.telemetry import (
    setup_telemetry,
    record_tool_metrics,
    track_tool,
    record_ast_metrics,
    record_embedding_metrics,
    record_db_operation,
    record_search_similarity,
    get_local_stats,
)


def test_telemetry_recording():
    setup_telemetry()
    record_tool_metrics("search_symbols", 0.05, "success")
    record_tool_metrics("search_symbols", 0.10, "error")

    stats = get_local_stats()
    assert stats["status"] == "ok"
    assert "search_symbols" in stats["tool_calls"]
    assert stats["tool_calls"]["search_symbols"]["count"] >= 2
    assert stats["tool_calls"]["search_symbols"]["errors"] >= 1


@pytest.mark.asyncio
async def test_track_tool_decorator():
    @track_tool("test_dummy_tool")
    async def dummy_tool():
        await asyncio.sleep(0.01)
        return "success_result"

    res = await dummy_tool()
    assert res == "success_result"

    stats = get_local_stats()
    assert "test_dummy_tool" in stats["tool_calls"]
    assert stats["tool_calls"]["test_dummy_tool"]["count"] >= 1


def test_deep_domain_metrics():
    setup_telemetry()
    record_ast_metrics({"class": 5, "function": 12}, 0.15)
    record_embedding_metrics(17, 0.45)
    record_db_operation("test_op", 0.08)
    record_search_similarity([0.89, 0.76])

    stats = get_local_stats()
    assert "deep_mcp_metrics" in stats
    deep = stats["deep_mcp_metrics"]
    assert deep["symbols_indexed"]["class"] >= 5
    assert deep["symbols_indexed"]["function"] >= 12
    assert deep["total_chunks_embedded"] >= 17
    assert deep["avg_similarity_score"] > 0.0
    assert "test_op" in deep["db_operations"]
