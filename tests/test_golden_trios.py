"""Every Golden Knowledge trio must pass the guard and run, for every demo user's scope.

This is the CI gate for the knowledge base: a trio whose SQL breaks (schema
change, typo) fails here before it can teach the agent a broken pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from retail_agent.data.sql_guard import SqlGuard
from retail_agent.data.warehouse import DuckDBWarehouse
from retail_agent.identity import load_users
from retail_agent.knowledge import DataDictionary, GoldenKnowledge

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = GoldenKnowledge.load(ROOT / "knowledge" / "golden_trios.yaml")
USERS = load_users(ROOT / "config" / "users.yaml")


@pytest.fixture(scope="module")
def runtime(sample_db):
    warehouse = DuckDBWarehouse(sample_db, "bigquery-public-data.thelook_ecommerce")
    guard = SqlGuard(DataDictionary.load(ROOT / "knowledge" / "data_dictionary.yaml"), warehouse.live_columns())
    return guard, warehouse


@pytest.mark.parametrize("trio", GOLDEN.trios, ids=[t.id for t in GOLDEN.trios])
@pytest.mark.parametrize("user_id", sorted(USERS))
def test_trio_runs_for_every_scope(runtime, trio, user_id):
    guard, warehouse = runtime
    guarded = guard.prepare(trio.sql, USERS[user_id].scope)
    result = warehouse.query(guarded.sql, labels={}, max_rows=5)
    assert result.columns


def test_ids_unique_and_fields_present():
    ids = [t.id for t in GOLDEN.trios]
    assert len(ids) == len(set(ids)) >= 10
    for t in GOLDEN.trios:
        assert t.question and t.sql and t.report and t.tags


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Why are users in Texas underspending vs Florida?", "GK-003"),
        ("why did churn spike last month", "GK-006"),
        ("create a report for Q1 including insights and action items for Q2", "GK-009"),
        ("compare product X and Y and why they perform differently", "GK-004"),
        ("monthly revenue", "GK-001"),
        ("what data is available", "GK-012"),
        ("top customers total spend", "GK-002"),
    ],
)
def test_retrieval_finds_the_right_precedent(question, expected):
    hits = GOLDEN.search(question, k=2)
    assert hits and hits[0][0].id == expected


def test_off_topic_retrieves_nothing():
    assert GOLDEN.search("what's the weather in Paris") == []
