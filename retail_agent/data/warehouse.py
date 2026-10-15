"""Warehouse backends behind one interface, with typed errors and resilience.

`QueryError.kind` decides what happens next:

    invalid_sql    the model wrote bad SQL -> sent back to the model to fix
    too_expensive  over the bytes cap      -> sent back to the model to narrow
    permission     credentials / quota     -> terminal, user is told
    unavailable    outage / timeout        -> retried here with backoff, then terminal

`ResilientWarehouse` adds, for any backend: bounded retries with jittered
exponential backoff for transient errors, a circuit breaker (after repeated
outages it fails fast instead of hammering the service and burning the user's
time), an in-process result cache for identical scoped SQL, and optional
fault injection for demos and tests.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

logger = logging.getLogger(__name__)


class QueryError(Exception):
    def __init__(self, kind: str, message: str):
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}")

    @property
    def fixable_by_model(self) -> bool:
        return self.kind in ("invalid_sql", "too_expensive")

    @property
    def transient(self) -> bool:
        return self.kind == "unavailable"


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    total_rows: int
    bytes_processed: int = 0
    cache_hit: bool = False
    elapsed_ms: int = 0
    job_id: str | None = None

    @property
    def truncated(self) -> bool:
        return self.total_rows > len(self.rows)


class Warehouse(Protocol):
    name: str

    def query(self, sql: str, *, labels: dict[str, str], max_rows: int) -> QueryResult: ...

    def live_columns(self) -> dict[str, list[str]] | None: ...


def _plain(value: Any) -> Any:
    """Make a DataFrame cell JSON-friendly (and compact) for the model. ARRAY and STRUCT cells
    arrive as numpy arrays, lists and dicts; their elements are converted too."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if getattr(value, "ndim", 0):  # numpy array
        return [_plain(v) for v in value.tolist()]
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalar
        value = value.item()
    if isinstance(value, float):
        return round(value, 4)
    return value


def _frame_to_rows(df: pd.DataFrame, max_rows: int) -> tuple[list[str], list[list[Any]]]:
    rows = [[_plain(v) for v in record] for record in df.head(max_rows).itertuples(index=False, name=None)]
    return [str(c) for c in df.columns], rows


# --------------------------------------------------------------------------- BigQuery

BIGQUERY_SCOPES = ("https://www.googleapis.com/auth/bigquery", "https://www.googleapis.com/auth/cloud-platform")


def service_account_credentials(raw: str):
    """Credentials from a service-account key held in memory (GCP_SA_KEY_JSON); nothing touches disk.

    A key that is set but unusable is an error, not a silent fall back to another login. The
    message never includes the key's content."""
    from google.oauth2 import service_account

    try:
        info = json.loads(raw)
    except ValueError:
        raise ValueError("GCP_SA_KEY_JSON is set but is not valid JSON. Paste the whole key file, or unset it.") from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ValueError("GCP_SA_KEY_JSON is set but is not a service-account key (\"type\": \"service_account\").")
    try:
        return service_account.Credentials.from_service_account_info(info, scopes=BIGQUERY_SCOPES)
    except (ValueError, KeyError) as e:
        missing = str(e) if "missing fields" in str(e) else type(e).__name__
        raise ValueError(f"GCP_SA_KEY_JSON is set but the key is not usable ({missing}).") from None


class BigQueryWarehouse:
    name = "bigquery"

    def __init__(
        self,
        project: str | None,
        dataset: str,
        max_bytes_billed: int,
        timeout_s: float,
        service_account_json: str | None = None,
    ):
        from google.cloud import bigquery  # imported lazily: offline mode does not need credentials

        from .bigquery_runner import BigQueryRunner

        self._bq = bigquery
        credentials = None
        self.credentials_source = "application default credentials"
        if service_account_json:
            credentials = service_account_credentials(service_account_json)
            project = project or credentials.project_id
            self.credentials_source = f"service-account key from GCP_SA_KEY_JSON ({credentials.service_account_email})"
        self.runner = BigQueryRunner(project_id=project, dataset_id=dataset, credentials=credentials)
        self.dataset = dataset
        self.max_bytes_billed = max_bytes_billed
        self.timeout_s = timeout_s
        self._allowed_refs = None  # set by live_columns()

    def live_columns(self) -> dict[str, list[str]] | None:
        out = {}
        for table in ("orders", "order_items", "products", "users"):
            try:
                out[table] = [f["name"] for f in self.runner.get_table_schema(table)]
            except Exception as e:  # schema is an optimisation; the dictionary still works
                logger.warning("could not read live schema for %s: %s", table, e)
                return None
        return out

    def query(self, sql: str, *, labels: dict[str, str], max_rows: int) -> QueryResult:
        from google.api_core import exceptions as gexc

        started = time.monotonic()
        try:
            # 1) Dry run: free, catches syntax/semantic errors, and lets us check
            #    statement type, referenced tables and bytes before paying.
            dry = self.runner.dry_run(sql)
            if (dry.statement_type or "SELECT").upper() != "SELECT":
                raise QueryError("invalid_sql", f"Only SELECT statements may run (got {dry.statement_type}).")
            project, dataset = self.dataset.split(".", 1)
            for ref in dry.referenced_tables or []:
                if (ref.project, ref.dataset_id) != (project, dataset):
                    raise QueryError("invalid_sql", f"Query references a table outside {self.dataset}.")
            if (dry.total_bytes_processed or 0) > self.max_bytes_billed:
                raise QueryError(
                    "too_expensive",
                    f"The query would scan {dry.total_bytes_processed / 1e9:.2f} GB (limit "
                    f"{self.max_bytes_billed / 1e9:.2f} GB). Select fewer columns or filter dates.",
                )
            # 2) Real run, with a hard bytes ceiling enforced by BigQuery itself.
            config = self._bq.QueryJobConfig(
                maximum_bytes_billed=self.max_bytes_billed,
                labels={k: _label(v) for k, v in labels.items()},
                job_timeout_ms=int(self.timeout_s * 1000),
            )
            df = self.runner.execute_query(sql, job_config=config, timeout=self.timeout_s, max_results=max_rows)
        except QueryError:
            raise
        except (gexc.BadRequest, gexc.NotFound) as e:
            message = _bq_message(e)
            if "bytesBilledLimitExceeded" in str(e) or "bytes billed" in message.lower():
                raise QueryError("too_expensive", message) from e
            raise QueryError("invalid_sql", message) from e
        except gexc.Forbidden as e:
            message = _bq_message(e)
            if "rateLimitExceeded" in str(e):
                raise QueryError("unavailable", message) from e
            raise QueryError("permission", message) from e
        except (
            gexc.TooManyRequests,
            gexc.InternalServerError,
            gexc.ServiceUnavailable,
            gexc.GatewayTimeout,
            gexc.DeadlineExceeded,
            gexc.RetryError,
            TimeoutError,
            ConnectionError,
        ) as e:
            raise QueryError("unavailable", _bq_message(e)) from e
        except Exception as e:  # concurrent.futures.TimeoutError, auth errors, ...
            name = type(e).__name__
            if "Timeout" in name:
                raise QueryError("unavailable", f"BigQuery timed out after {self.timeout_s:.0f}s") from e
            if "DefaultCredentialsError" in name or "RefreshError" in name:
                raise QueryError("permission", f"Google Cloud credentials are not configured ({name}).") from e
            raise QueryError("unavailable", f"{name}: {e}") from e
        columns, rows = _frame_to_rows(df, max_rows)
        return QueryResult(
            columns=columns,
            rows=rows,
            total_rows=int(df.attrs.get("total_rows") or len(df)),
            bytes_processed=int(df.attrs.get("bytes_processed") or 0),
            cache_hit=bool(df.attrs.get("cache_hit")),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            job_id=df.attrs.get("job_id"),
        )


def _label(value: str) -> str:
    # BigQuery labels: lowercase letters, digits, '-' and '_', at most 63 chars.
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value.lower())
    return cleaned[:63]


def _bq_message(error: Exception) -> str:
    message = getattr(error, "message", None) or str(error)
    # Strip the job URL and location noise; keep what the model can act on.
    message = message.split("\n")[0]
    for marker in ("; reason:", " Location:", " Job ID:"):
        message = message.split(marker)[0]
    return message[:500]


# --------------------------------------------------------------------------- DuckDB (offline)


class DuckDBWarehouse:
    """Runs the same guarded BigQuery SQL on a local synthetic copy of the dataset.

    For development, tests and demos without Google Cloud. The SQL is
    transpiled from BigQuery to DuckDB with sqlglot. It is not a faithful
    BigQuery emulator: a few functions differ, and errors read differently.
    """

    name = "duckdb"

    def __init__(self, path: Path, dataset: str):
        import duckdb

        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found. Build it with: python -m retail_agent sample-db"
            )
        self._duckdb = duckdb
        self.conn = duckdb.connect(str(path), read_only=True)
        self.project, self.dataset = dataset.split(".", 1)
        self._lock = threading.Lock()

    def live_columns(self) -> dict[str, list[str]] | None:
        out: dict[str, list[str]] = {}
        with self._lock:
            for (table,) in self.conn.execute("SELECT table_name FROM information_schema.tables").fetchall():
                cols = self.conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position",
                    [table],
                ).fetchall()
                out[table] = [c for (c,) in cols]
        return out

    def to_duckdb_sql(self, sql: str) -> str:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read="bigquery")
        for table in tree.find_all(exp.Table):
            if (table.catalog or "").lower() == self.project.lower() and (table.db or "").lower() == self.dataset.lower():
                table.set("catalog", None)
                table.set("db", None)
        return tree.sql(dialect="duckdb")

    def query(self, sql: str, *, labels: dict[str, str], max_rows: int) -> QueryResult:
        started = time.monotonic()
        duckdb = self._duckdb
        try:
            local_sql = self.to_duckdb_sql(sql)
            with self._lock:
                df = self.conn.execute(local_sql).fetchdf()
        except duckdb.Error as e:  # offline: every engine error is a SQL error the model can fix
            raise QueryError("invalid_sql", str(e).split("\n")[0][:500]) from e
        except Exception as e:
            if type(e).__module__.startswith("sqlglot"):
                raise QueryError("invalid_sql", str(e)[:500]) from e
            raise QueryError("unavailable", f"{type(e).__name__}: {e}") from e
        columns, rows = _frame_to_rows(df, max_rows)
        return QueryResult(
            columns=columns,
            rows=rows,
            total_rows=len(df),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


# --------------------------------------------------------------------------- resilience


@dataclass
class CircuitBreaker:
    """Opens after `threshold` consecutive outages; half-opens after `cooldown_s`."""

    threshold: int = 3
    cooldown_s: float = 30.0
    failures: int = 0
    opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown_s:
            return True  # half-open: let one request probe the service
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "open" if not self.allow() else "half-open"


@dataclass
class FaultInjector:
    """Simulate outages: the next `remaining` queries fail as `kind`."""

    remaining: int = 0
    kind: str = "unavailable"

    def maybe_fail(self) -> None:
        if self.remaining > 0:
            self.remaining -= 1
            raise QueryError(self.kind, "Simulated outage (fault injection): 503 Service Unavailable")


@dataclass
class ResilientWarehouse:
    inner: Warehouse
    attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 4.0
    cache_size: int = 128
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    faults: FaultInjector = field(default_factory=FaultInjector)
    sleep: Any = time.sleep
    _cache: OrderedDict = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def name(self) -> str:
        return self.inner.name

    def live_columns(self) -> dict[str, list[str]] | None:
        return self.inner.live_columns()

    def run(self, sql: str, *, labels: dict[str, str], max_rows: int) -> tuple[QueryResult, int]:
        """Returns (result, attempts used; 0 = served from cache). Raises QueryError."""
        key = (sql, max_rows)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                cached: QueryResult = self._cache[key]
                return QueryResult(**{**cached.__dict__, "cache_hit": True, "elapsed_ms": 0}), 0
        if not self.breaker.allow():
            raise QueryError(
                "unavailable",
                "The data warehouse is temporarily unavailable (circuit open after repeated failures).",
            )
        last: QueryError | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                self.faults.maybe_fail()
                result = self.inner.query(sql, labels=labels, max_rows=max_rows)
            except QueryError as e:
                if not e.transient:
                    raise  # the model's to fix, or terminal; not an outage
                last = e
                self.breaker.record_failure()
                if attempt == self.attempts or not self.breaker.allow():
                    break
                delay = min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1))
                self.sleep(delay * (0.5 + random.random() / 2))
                continue
            self.breaker.record_success()
            with self._lock:
                self._cache[key] = result
                if len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            return result, attempt
        assert last is not None
        raise last
