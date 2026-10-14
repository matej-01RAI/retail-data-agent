"""Red-team and correctness cases for the SQL guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from retail_agent.data.sql_guard import SqlGuard, SqlRejected
from retail_agent.identity import ProductScope
from retail_agent.knowledge import DataDictionary

from .conftest import ITEMS, ORDERS, PRODUCTS, USERS

ROOT = Path(__file__).resolve().parent.parent
ALL = ProductScope()
TOM = ProductScope({"category": ("Jeans", "Outerwear & Coats")})


@pytest.fixture(scope="module")
def guard() -> SqlGuard:
    return SqlGuard(DataDictionary.load(ROOT / "knowledge" / "data_dictionary.yaml"))


@pytest.mark.parametrize(
    "sql, kind",
    [
        # writes and scripts
        (f"DELETE FROM {ORDERS} WHERE TRUE", "not_read_only"),
        (f"UPDATE {ORDERS} SET status = 'x' WHERE TRUE", "not_read_only"),
        (f"INSERT INTO {ORDERS} (order_id) VALUES (1)", "not_read_only"),
        ("DROP TABLE `bigquery-public-data.thelook_ecommerce.orders`", "not_read_only"),
        (f"CREATE TABLE my.t AS SELECT * FROM {ORDERS}", "not_read_only"),
        (f"MERGE {ORDERS} t USING {ORDERS} s ON FALSE WHEN NOT MATCHED THEN INSERT ROW", "not_read_only"),
        (f"SELECT 1; DELETE FROM {ORDERS} WHERE TRUE", "not_read_only"),
        (f"EXPORT DATA OPTIONS(uri='gs://bucket/*.csv', format='CSV') AS SELECT * FROM {USERS}", "not_read_only"),
        ("DECLARE x INT64 DEFAULT 1", "not_read_only"),
        # PII, however it is referenced
        (f"SELECT email FROM {USERS}", "pii_column"),
        (f"SELECT u.first_name, u.last_name FROM {USERS} u", "pii_column"),
        (f"SELECT COUNT(*) FROM {USERS} WHERE email LIKE '%@gmail.com'", "pii_column"),
        (f"SELECT id FROM {USERS} ORDER BY street_address", "pii_column"),
        (f"SELECT ST_ASTEXT(user_geom) FROM {USERS}", "pii_column"),
        (f"SELECT x.e FROM (SELECT email AS e FROM {USERS}) x", "pii_column"),
        (f"SELECT AVG(latitude), AVG(longitude) FROM {USERS}", "pii_column"),
        (f"SELECT * FROM {USERS} a JOIN {USERS} b USING (postal_code)", "pii_column"),
        # tables outside the allowlist
        ("SELECT * FROM `bigquery-public-data.thelook_ecommerce.distribution_centers`", "forbidden_table"),
        ("SELECT * FROM `bigquery-public-data.thelook_ecommerce.events`", "forbidden_table"),
        ("SELECT * FROM `bigquery-public-data.thelook_ecommerce.INFORMATION_SCHEMA.COLUMNS`", "forbidden_table"),
        ("SELECT * FROM `bigquery-public-data.thelook_ecommerce.*`", "forbidden_table"),
        ("SELECT * FROM `my-project.private.orders`", "forbidden_table"),
        ("SELECT * FROM `bigquery-public-data.other_dataset.orders`", "forbidden_table"),
        # functions that reach outside the dataset
        ("SELECT * FROM EXTERNAL_QUERY('us.conn', 'SELECT * FROM secrets')", "forbidden_function"),
        (f"SELECT * FROM ML.PREDICT(MODEL `p.d.m`, TABLE {ORDERS})", "forbidden_table"),
        ("SELECT SESSION_USER()", "forbidden_function"),
        # garbage
        ("SELEC status FRM orders", "invalid_sql"),
        ("", "invalid_sql"),
    ],
)
def test_rejected(guard, sql, kind):
    with pytest.raises(SqlRejected) as err:
        guard.prepare(sql, ALL)
    assert err.value.kind == kind
    assert err.value.message  # always something the model can act on


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT status, COUNT(*) FROM {ORDERS} GROUP BY status",
        f"SELECT p.brand, SUM(oi.sale_price) FROM {ITEMS} oi JOIN {PRODUCTS} p ON p.id = oi.product_id GROUP BY 1",
        f"WITH c AS (SELECT user_id, COUNT(*) n FROM {ORDERS} GROUP BY user_id) SELECT n, COUNT(*) FROM c GROUP BY n",
        f"SELECT u.state, AVG(u.age) FROM {USERS} u GROUP BY 1",
        f"SELECT * FROM {USERS} LIMIT 5",  # allowed: the scoped subquery has no PII columns
        f"SELECT TO_JSON_STRING(u) FROM {USERS} u LIMIT 1",  # likewise
        "SELECT status FROM thelook_ecommerce.orders",  # dataset-qualified only
        "SELECT status FROM orders",  # bare name
        f"SELECT 'a' AS x UNION ALL SELECT status FROM {ORDERS}",
    ],
)
def test_allowed(guard, sql):
    guarded = guard.prepare(sql, TOM)
    assert "bigquery-public-data" in guarded.sql or "orders" not in sql


def test_rewrite_projects_no_pii_and_applies_scope(guard):
    guarded = guard.prepare(f"SELECT * FROM {USERS}", TOM)
    for column in ("first_name", "last_name", "email", "street_address", "postal_code", "latitude", "longitude", "user_geom"):
        assert column not in guarded.sql
    assert "category IN ('Jeans', 'Outerwear & Coats')" in guarded.sql


def test_every_table_reference_is_scoped(guard):
    sql = f"SELECT COUNT(*) FROM {ORDERS} o JOIN {ITEMS} oi ON o.order_id = oi.order_id JOIN {PRODUCTS} p ON p.id = oi.product_id"
    guarded = guard.prepare(sql, TOM)
    # three scoped subqueries, each carrying the entitlement predicate
    assert guarded.sql.count("category IN ('Jeans', 'Outerwear & Coats')") >= 3
    assert guarded.tables == ("order_items", "orders", "products")


def test_unrestricted_scope_adds_no_filter(guard):
    guarded = guard.prepare(f"SELECT COUNT(*) FROM {PRODUCTS}", ALL)
    assert "WHERE" not in guarded.sql


def test_cte_named_like_a_table_is_not_confused(guard):
    sql = f"WITH orders AS (SELECT order_id FROM {ORDERS}) SELECT COUNT(*) FROM orders"
    guarded = guard.prepare(sql, TOM)
    assert guarded.sql.count("category IN") == 1  # only the real table inside the CTE


def test_scope_values_are_escaped(guard):
    evil = ProductScope({"brand": ("Levi's' OR '1'='1",)})
    guarded = guard.prepare(f"SELECT COUNT(*) FROM {PRODUCTS}", evil)
    assert "OR '1'='1'" not in guarded.sql.replace("\\'", "")
