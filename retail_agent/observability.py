"""Observability: traces, turn metrics and the data behind /trace and /stats.

Three layers, each answering a different question:

1. OpenTelemetry spans (Pydantic AI's GenAI instrumentation + our own
   `agent_turn` / `sql.query` spans): *what exactly happened in this turn*,
   every model request with its messages and token usage, every tool call
   with arguments and result. Exported here to a local JSONL file; in
   production the same spans go over OTLP to Cloud Trace (or Langfuse).
2. `turns` table: one row per user turn with outcome, latency, tokens, LLM
   calls, SQL attempts/failures/self-corrections, guardrail and PII counters,
   fallback usage. This is what dashboards and alerts are built on.
3. `sql_events` table: one row per query attempt with the scoped SQL, status,
   error kind and message, rows, bytes, latency. The fastest way to see why a
   question failed.

Turn rows carry the OpenTelemetry trace id, so a bad metric links straight to
its full trace.
"""

from __future__ import annotations

import json
import statistics
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from .store import Database, now_iso

_MAX_ATTR = 8000


class JsonlSpanExporter(SpanExporter):
    """Append finished spans to a JSONL file (stand-in for an OTLP exporter)."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        lines = []
        for span in spans:
            ctx = span.get_span_context()
            attrs = {}
            for key, value in (span.attributes or {}).items():
                if key.startswith("logfire."):
                    continue
                text = value if isinstance(value, (int, float, bool)) else str(value)
                if isinstance(text, str) and len(text) > _MAX_ATTR:
                    text = text[:_MAX_ATTR] + "…"
                attrs[key] = text
            lines.append(json.dumps({
                "trace_id": f"{ctx.trace_id:032x}",
                "span_id": f"{ctx.span_id:016x}",
                "parent_id": f"{span.parent.span_id:016x}" if span.parent else None,
                "name": span.name,
                "start": span.start_time,
                "end": span.end_time,
                "duration_ms": round(((span.end_time or 0) - (span.start_time or 0)) / 1e6, 1),
                "status": span.status.status_code.name,
                "attributes": attrs,
                "events": [{"name": e.name, "attributes": dict(e.attributes or {})} for e in span.events],
            }, default=str))
        with self._lock, self.path.open("a") as f:
            for line in lines:
                f.write(line + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


def setup_tracing(path: Path) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(JsonlSpanExporter(path), schedule_delay_millis=500))
    return provider


@dataclass
class TurnStats:
    """Counters that tools increment during one user turn."""

    sql_queries: int = 0
    sql_failures: int = 0
    sql_rejected: int = 0
    consecutive_sql_failures: int = 0
    empty_results: int = 0
    self_corrected: int = 0
    bytes_processed: int = 0
    pii_redactions: int = 0
    precedents: list[str] = field(default_factory=list)
    _failed_since_success: bool = False

    def sql_failed(self) -> None:
        self.sql_failures += 1
        self.consecutive_sql_failures += 1
        self._failed_since_success = True

    def sql_succeeded(self) -> None:
        if self._failed_since_success:
            self.self_corrected += 1
        self._failed_since_success = False
        self.consecutive_sql_failures = 0


class Telemetry:
    def __init__(self, db: Database, provider: TracerProvider | None, spans_path: Path | None):
        self.db = db
        self.provider = provider
        self.spans_path = spans_path
        self.tracer = (provider or trace.get_tracer_provider()).get_tracer("retail_agent")

    def flush(self) -> None:
        if self.provider:
            self.provider.force_flush()

    # ------------------------------------------------------------------ writes

    def record_sql(
        self,
        turn_id: str,
        *,
        purpose: str,
        sql: str,
        status: str,
        error_kind: str | None = None,
        error: str | None = None,
        rows: int | None = None,
        bytes_processed: int | None = None,
        elapsed_ms: int | None = None,
        attempts: int | None = None,
        cache_hit: bool = False,
    ) -> None:
        self.db.execute(
            "INSERT INTO sql_events (turn_id, ts, purpose, sql, status, error_kind, error, rows, bytes, "
            "elapsed_ms, attempts, cache_hit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [turn_id, now_iso(), purpose, sql, status, error_kind, error, rows, bytes_processed, elapsed_ms,
             attempts, int(cache_hit)],
        )

    def record_turn(self, **row: Any) -> None:
        row.setdefault("ts", now_iso())
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        self.db.execute(f"INSERT OR REPLACE INTO turns ({cols}) VALUES ({marks})", list(row.values()))

    # ------------------------------------------------------------------ reads

    def recent_turns(self, limit: int = 10, session_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM turns"
        params: list[Any] = []
        if session_id:
            sql += " WHERE session_id = ?"
            params.append(session_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.query(sql, params)]

    def turn_detail(self, turn_id: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM turns WHERE turn_id LIKE ?", [turn_id + "%"])
        if not rows:
            return None
        turn = dict(rows[0])
        turn["sql_events"] = [
            dict(r) for r in self.db.query("SELECT * FROM sql_events WHERE turn_id = ? ORDER BY id", [turn["turn_id"]])
        ]
        turn["spans"] = self.spans_for(turn.get("trace_id"))
        return turn

    def spans_for(self, trace_id: str | None) -> list[dict[str, Any]]:
        if not trace_id or not self.spans_path:
            return []
        self.flush()  # before the existence check: the first batch may not be written yet
        if not self.spans_path.exists():
            return []
        out = []
        with self.spans_path.open() as f:
            for line in f:
                if trace_id in line:
                    span = json.loads(line)
                    if span["trace_id"] == trace_id:
                        out.append(span)
        out.sort(key=lambda s: s["start"] or 0)
        return out

    def stats(self, since: str | None = None) -> dict[str, Any]:
        where, params = ("WHERE ts >= ?", [since]) if since else ("", [])
        turns = [dict(r) for r in self.db.query(f"SELECT * FROM turns {where}", params)]
        if not turns:
            return {"turns": 0}
        n = len(turns)
        outcomes: dict[str, int] = {}
        for t in turns:
            outcomes[t["outcome"]] = outcomes.get(t["outcome"], 0) + 1
        latencies = sorted(t["latency_ms"] or 0 for t in turns)
        sql_q = sum(t["sql_queries"] or 0 for t in turns)
        sql_f = sum(t["sql_failures"] or 0 for t in turns)
        failed_turns = [t for t in turns if (t["sql_failures"] or 0) > 0]
        recovered = [t for t in failed_turns if (t["self_corrected"] or 0) > 0]
        errors: dict[str, int] = {}
        for t in turns:
            if t["error_type"]:
                errors[t["error_type"]] = errors.get(t["error_type"], 0) + 1
        sql_errors: dict[str, int] = {}
        for r in self.db.query(
            f"SELECT error_kind, COUNT(*) AS n FROM sql_events WHERE status != 'ok' "
            f"{'AND ts >= ?' if since else ''} GROUP BY error_kind",
            params,
        ):
            sql_errors[r["error_kind"] or "unknown"] = r["n"]
        return {
            "turns": n,
            "outcomes": outcomes,
            "answer_rate": round(outcomes.get("answered", 0) / n, 3),
            "latency_ms_p50": int(statistics.median(latencies)),
            "latency_ms_p95": int(latencies[min(n - 1, int(0.95 * n))]),
            "llm_requests_per_turn": round(sum(t["llm_requests"] or 0 for t in turns) / n, 2),
            "tokens_in": sum(t["input_tokens"] or 0 for t in turns),
            "tokens_out": sum(t["output_tokens"] or 0 for t in turns),
            "sql_queries": sql_q,
            "sql_failure_rate": round(sql_f / sql_q, 3) if sql_q else 0.0,
            "sql_errors_by_kind": sql_errors,
            "self_correction_rate": round(len(recovered) / len(failed_turns), 3) if failed_turns else None,
            "empty_results": sum(t["empty_results"] or 0 for t in turns),
            "guardrail_blocks": outcomes.get("blocked", 0),
            "sql_rejected_by_guard": sum(t["sql_rejected"] or 0 for t in turns),
            "pii_redactions": sum(t["pii_redactions"] or 0 for t in turns),
            "fallback_turns": sum(t["fallback_used"] or 0 for t in turns),
            "bytes_processed": sum(t["bytes_processed"] or 0 for t in turns),
            "errors_by_type": errors,
        }
