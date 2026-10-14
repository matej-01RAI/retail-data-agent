"""Wiring: build every service once from Settings."""

from __future__ import annotations

import logging
from dataclasses import replace

from .agent import PersonaSource, Services
from .data.sql_guard import SqlGuard
from .data.warehouse import BigQueryWarehouse, DuckDBWarehouse, ResilientWarehouse, Warehouse
from .knowledge import DataDictionary, GoldenKnowledge
from .observability import Telemetry, setup_tracing
from .settings import Settings
from .store import Database, PreferenceStore, ReportStore

logger = logging.getLogger(__name__)


def build_warehouse(settings: Settings) -> Warehouse:
    if settings.warehouse == "duckdb":
        return DuckDBWarehouse(settings.duckdb_path, settings.dataset)
    if settings.warehouse == "bigquery":
        return BigQueryWarehouse(settings.gcp_project, settings.dataset, settings.max_bytes_billed, settings.query_timeout_s)
    raise ValueError(f"WAREHOUSE must be 'bigquery' or 'duckdb', not {settings.warehouse!r}")


def build_services(settings: Settings, *, warehouse: Warehouse | None = None, tracing: bool = True) -> Services:
    db = Database(settings.db_path)
    provider = setup_tracing(settings.traces_path) if tracing else None
    telemetry = Telemetry(db, provider, settings.traces_path if tracing else None)
    dictionary = DataDictionary.load(settings.knowledge_dir / "data_dictionary.yaml")
    golden = GoldenKnowledge.load(settings.knowledge_dir / "golden_trios.yaml")
    inner = warehouse or build_warehouse(settings)
    live = inner.live_columns()
    if live:
        for table in dictionary.tables:
            missing = set(dictionary.tables[table].columns) - set(live.get(table, []))
            if missing:
                logger.warning("columns in the data dictionary but not in %s.%s: %s", settings.dataset, table, sorted(missing))
    return Services(
        settings=settings,
        dictionary=dictionary,
        golden=golden,
        guard=SqlGuard(dictionary, live),
        warehouse=ResilientWarehouse(inner),
        reports=ReportStore(db, settings.reports_dir),
        prefs=PreferenceStore(db),
        telemetry=telemetry,
        persona=PersonaSource(settings.persona_path),
    )


def offline(settings: Settings) -> Settings:
    return replace(settings, warehouse="duckdb")
