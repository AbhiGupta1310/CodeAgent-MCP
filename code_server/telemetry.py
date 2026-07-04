"""
code_server/telemetry.py
========================
OpenTelemetry setup, deep MCP domain metrics collection, OTel tracing spans, and local stats tracker.
Supports OTLP export to Grafana Cloud / APM services as well as a local /stats endpoint.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import time
from typing import Dict, Any, Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# OpenTelemetry imports
try:
    from opentelemetry import trace, metrics
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False

logger = logging.getLogger(__name__)

# In-memory stats counter for quick /stats JSON endpoint
_local_stats: Dict[str, Any] = {
    "start_time": time.time(),
    "total_requests": 0,
    "status_codes": {"2xx": 0, "4xx": 0, "5xx": 0},
    "tool_calls": {},
    "recent_latency_ms": [],
    "domain_metrics": {
        "symbols_indexed_total": {"class": 0, "function": 0, "method": 0},
        "chunks_embedded_total": 0,
        "embedding_batches_processed": 0,
        "avg_treesitter_parse_time_ms": 0.0,
        "avg_embedding_gen_time_ms": 0.0,
        "db_operations": {},
        "recent_similarity_scores": [],
    },
}

# OpenTelemetry meters & instruments
_tracer_provider = None
_meter_provider = None
_tracer = None
_meter = None
_request_counter = None
_request_duration_histogram = None
_tool_counter = None
_tool_duration_histogram = None

# Deep Domain Instruments
_indexed_symbols_counter = None
_indexed_chunks_counter = None
_treesitter_duration_histogram = None
_embedding_duration_histogram = None
_db_query_duration_histogram = None
_similarity_score_histogram = None


import urllib.parse


def setup_telemetry() -> None:
    """Initialize OpenTelemetry tracer and meter providers if OTLP credentials are configured."""
    global _tracer_provider, _meter_provider, _tracer, _meter, _request_counter, _request_duration_histogram, _tool_counter, _tool_duration_histogram
    global _indexed_symbols_counter, _indexed_chunks_counter, _treesitter_duration_histogram
    global _embedding_duration_histogram, _db_query_duration_histogram, _similarity_score_histogram

    if not OTEL_AVAILABLE:
        logger.info("OpenTelemetry packages not fully installed. Running with local metrics fallback.")
        return

    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    headers_raw = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "").strip()

    # Clean up URL percent encoding (%20 -> space) if present in headers
    if "%20" in headers_raw:
        headers_raw = urllib.parse.unquote(headers_raw)

    headers_dict: Dict[str, str] = {}
    if headers_raw:
        for item in headers_raw.split(","):
            if "=" in item:
                k, v = item.split("=", 1)
                headers_dict[k.strip()] = v.strip()

    resource = Resource.create({"service.name": "codeagent-code-server"})

    # Setup Tracing with fast 2-second batch export
    _tracer_provider = TracerProvider(resource=resource)
    if otlp_endpoint:
        try:
            traces_url = otlp_endpoint.rstrip("/")
            if not traces_url.endswith("/v1/traces"):
                traces_url = f"{traces_url}/v1/traces"

            span_exporter = OTLPSpanExporter(endpoint=traces_url, headers=headers_dict) if headers_dict else OTLPSpanExporter(endpoint=traces_url)
            _tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter, scheduled_delay_millis=2000))
            logger.info("OTLP Trace Exporter initialized (target: %s)", traces_url)
        except Exception as e:
            logger.warning("Failed to initialize OTLPSpanExporter: %s", e)

    trace.set_tracer_provider(_tracer_provider)
    _tracer = trace.get_tracer("codeagent-code-server")

    # Send an initial trace span on startup and flush immediately to verify connection in Grafana
    if otlp_endpoint:
        with _tracer.start_as_current_span("mcp.server.startup") as span:
            span.set_attribute("service.status", "initialized")
            span.set_attribute("runtime.env", "render")
        try:
            _tracer_provider.force_flush()
        except Exception:
            pass

    # Setup Metrics
    readers = []
    if otlp_endpoint:
        try:
            metrics_url = otlp_endpoint.rstrip("/")
            if not metrics_url.endswith("/v1/metrics"):
                metrics_url = f"{metrics_url}/v1/metrics"

            metric_exporter = OTLPMetricExporter(endpoint=metrics_url, headers=headers_dict) if headers_dict else OTLPMetricExporter(endpoint=metrics_url)
            readers.append(PeriodicExportingMetricReader(metric_exporter, export_interval_millis=5000))
            logger.info("OTLP Metric Exporter initialized (target: %s)", metrics_url)
        except Exception as e:
            logger.warning("Failed to initialize OTLPMetricExporter: %s", e)

    _meter_provider = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(_meter_provider)
    _meter = metrics.get_meter("codeagent-code-server")

    # Standard HTTP & Tool Instruments
    _request_counter = _meter.create_counter("http_requests_total", description="Total HTTP requests received", unit="1")
    _request_duration_histogram = _meter.create_histogram("http_request_duration_seconds", description="HTTP request latency in seconds", unit="s")
    _tool_counter = _meter.create_counter("mcp_tool_calls_total", description="Total MCP tool invocations", unit="1")
    _tool_duration_histogram = _meter.create_histogram("mcp_tool_duration_seconds", description="MCP tool execution latency in seconds", unit="s")

    # Deep Domain Instruments
    _indexed_symbols_counter = _meter.create_counter("mcp_indexed_symbols_total", description="Number of AST symbols parsed and indexed", unit="1")
    _indexed_chunks_counter = _meter.create_counter("mcp_indexed_chunks_total", description="Number of code vector chunks generated", unit="1")
    _treesitter_duration_histogram = _meter.create_histogram("mcp_treesitter_parse_duration_seconds", description="Tree-Sitter AST parsing latency", unit="s")
    _embedding_duration_histogram = _meter.create_histogram("mcp_embedding_duration_seconds", description="Vector embedding generation latency", unit="s")
    _db_query_duration_histogram = _meter.create_histogram("mcp_db_query_duration_seconds", description="Database query duration in seconds", unit="s")
    _similarity_score_histogram = _meter.create_histogram("mcp_semantic_search_similarity_score", description="Similarity scores for pgvector queries", unit="1")


def shutdown_telemetry() -> None:
    """Flush all remaining traces and metrics and shutdown OTel providers cleanly."""
    global _tracer_provider, _meter_provider
    if _tracer_provider:
        try:
            _tracer_provider.force_flush()
            _tracer_provider.shutdown()
            logger.info("OpenTelemetry tracer provider flushed and shut down cleanly.")
        except Exception as e:
            logger.warning("Error shutting down tracer provider: %s", e)


@contextlib.asynccontextmanager
async def trace_span_async(name: str, attributes: Optional[Dict[str, Any]] = None):
    """Async context manager to record OTel trace spans for internal operations (git clone, embedding, pgvector)."""
    if _tracer and OTEL_AVAILABLE:
        with _tracer.start_as_current_span(name) as span:
            if attributes:
                for k, v in attributes.items():
                    span.set_attribute(k, str(v))
            yield span
    else:
        yield None


@contextlib.contextmanager
def trace_span_sync(name: str, attributes: Optional[Dict[str, Any]] = None):
    """Synchronous context manager to record OTel trace spans."""
    if _tracer and OTEL_AVAILABLE:
        with _tracer.start_as_current_span(name) as span:
            if attributes:
                for k, v in attributes.items():
                    span.set_attribute(k, str(v))
            yield span
    else:
        yield None


class StatsMiddleware(BaseHTTPMiddleware):
    """Starlette middleware to capture request counts, response latencies, and status code distributions."""
    async def dispatch(self, request: Request, call_next) -> Response:
        start_time = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_sec = time.perf_counter() - start_time
            duration_ms = round(duration_sec * 1000, 2)
            path = request.url.path

            # Update in-memory stats
            _local_stats["total_requests"] += 1
            if 200 <= status_code < 400:
                _local_stats["status_codes"]["2xx"] += 1
            elif 400 <= status_code < 500:
                _local_stats["status_codes"]["4xx"] += 1
            else:
                _local_stats["status_codes"]["5xx"] += 1

            # Keep rolling last 50 request latencies
            _local_stats["recent_latency_ms"].append(duration_ms)
            if len(_local_stats["recent_latency_ms"]) > 50:
                _local_stats["recent_latency_ms"].pop(0)

            # Record in OpenTelemetry
            if _request_counter:
                _request_counter.add(1, {"path": path, "status_code": str(status_code)})
            if _request_duration_histogram:
                _request_duration_histogram.record(duration_sec, {"path": path})


def record_tool_metrics(tool_name: str, duration_sec: float, status: str = "success") -> None:
    """Record execution metrics for an MCP tool call."""
    if tool_name not in _local_stats["tool_calls"]:
        _local_stats["tool_calls"][tool_name] = {"count": 0, "errors": 0, "total_duration_ms": 0}

    _local_stats["tool_calls"][tool_name]["count"] += 1
    _local_stats["tool_calls"][tool_name]["total_duration_ms"] += round(duration_sec * 1000, 2)
    if status != "success":
        _local_stats["tool_calls"][tool_name]["errors"] += 1

    if _tool_counter:
        _tool_counter.add(1, {"tool": tool_name, "status": status})
    if _tool_duration_histogram:
        _tool_duration_histogram.record(duration_sec, {"tool": tool_name})


def track_tool(tool_name: str) -> Callable:
    """Decorator to track execution duration and status for MCP tools."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            status = "success"
            async with trace_span_async(f"mcp.tool.{tool_name}"):
                try:
                    res = await func(*args, **kwargs)
                    if isinstance(res, str) and res.startswith("Error:"):
                        status = "error"
                    return res
                except Exception:
                    status = "error"
                    raise
                finally:
                    duration = time.perf_counter() - start
                    record_tool_metrics(tool_name, duration, status)
        return wrapper
    return decorator


def record_ast_metrics(symbol_counts: Dict[str, int], parse_duration_sec: float) -> None:
    """Record Tree-Sitter AST parsing metrics."""
    domain = _local_stats["domain_metrics"]
    for kind, count in symbol_counts.items():
        if kind in domain["symbols_indexed_total"]:
            domain["symbols_indexed_total"][kind] += count
        else:
            domain["symbols_indexed_total"][kind] = count

        if _indexed_symbols_counter:
            _indexed_symbols_counter.add(count, {"kind": kind})

    if _treesitter_duration_histogram:
        _treesitter_duration_histogram.record(parse_duration_sec)


def record_embedding_metrics(chunk_count: int, duration_sec: float) -> None:
    """Record vector embedding batch generation metrics."""
    domain = _local_stats["domain_metrics"]
    domain["chunks_embedded_total"] += chunk_count
    domain["embedding_batches_processed"] += 1
    duration_ms = round(duration_sec * 1000, 2)
    domain["avg_embedding_gen_time_ms"] = duration_ms

    if _indexed_chunks_counter:
        _indexed_chunks_counter.add(chunk_count)
    if _embedding_duration_histogram:
        _embedding_duration_histogram.record(duration_sec)


def record_db_operation(operation: str, duration_sec: float) -> None:
    """Record database operation durations."""
    domain = _local_stats["domain_metrics"]
    op_stats = domain["db_operations"].setdefault(operation, {"count": 0, "total_duration_ms": 0.0})
    op_stats["count"] += 1
    op_stats["total_duration_ms"] += round(duration_sec * 1000, 2)

    if _db_query_duration_histogram:
        _db_query_duration_histogram.record(duration_sec, {"operation": operation})


def record_search_similarity(scores: list[float]) -> None:
    """Record pgvector similarity scores from semantic search results."""
    if not scores:
        return
    domain = _local_stats["domain_metrics"]
    domain["recent_similarity_scores"].extend(scores)
    if len(domain["recent_similarity_scores"]) > 50:
        domain["recent_similarity_scores"] = domain["recent_similarity_scores"][-50:]

    if _similarity_score_histogram:
        for s in scores:
            _similarity_score_histogram.record(s)


def get_local_stats() -> Dict[str, Any]:
    """Return current snapshot of captured server stats."""
    uptime_sec = round(time.time() - _local_stats["start_time"], 1)
    recent_latencies = _local_stats["recent_latency_ms"]
    avg_latency = round(sum(recent_latencies) / len(recent_latencies), 2) if recent_latencies else 0.0

    domain = _local_stats["domain_metrics"]
    recent_scores = domain["recent_similarity_scores"]
    avg_similarity = round(sum(recent_scores) / len(recent_scores), 3) if recent_scores else 0.0

    return {
        "status": "ok",
        "uptime_seconds": uptime_sec,
        "total_requests": _local_stats["total_requests"],
        "status_codes": _local_stats["status_codes"],
        "avg_latency_ms": avg_latency,
        "tool_calls": _local_stats["tool_calls"],
        "deep_mcp_metrics": {
            "symbols_indexed": domain["symbols_indexed_total"],
            "total_chunks_embedded": domain["chunks_embedded_total"],
            "embedding_batches": domain["embedding_batches_processed"],
            "avg_similarity_score": avg_similarity,
            "db_operations": domain["db_operations"],
        },
        "otlp_export_active": bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")),
    }
