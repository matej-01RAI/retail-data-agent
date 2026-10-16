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
        "SELECT status FROM thelook_ecommerce.orders",  # dataset-qualified only
        "SELECT status FROM orders",  # bare name
        f"SELECT 'a' AS x UNION ALL SELECT status FROM {ORDERS}",
    ],
)
def test_allowed(guard, sql):
    guarded = guard.prepare(sql, TOM)
    assert "bigquery-public-data" in guarded.sql or "orders" not in sql


def test_rewrite_projects_no_pii_and_applies_scope(guard):
    guarded = guard.prepare(f"SELECT COUNT(*) FROM {USERS}", TOM)
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


def test_cte_may_not_take_a_table_name(guard):
    sql = f"WITH orders AS (SELECT order_id FROM {ORDERS}) SELECT COUNT(*) FROM orders"
    with pytest.raises(SqlRejected) as err:
        guard.prepare(sql, TOM)
    assert err.value.kind == "forbidden_table"


@pytest.mark.parametrize(
    "sql",
    [
        # review C1: the bare table inside a same-named CTE was never scoped (full PII leak offline)
        "WITH users AS (SELECT * EXCEPT (id, age, gender, state, city, country, traffic_source, created_at) FROM users) "
        "SELECT * FROM users",
        "SELECT COUNT(*) FROM users WHERE EXISTS (WITH users AS (SELECT 1 AS x) SELECT x FROM users)",
        "SELECT p.brand, SUM(oi.sale_price) FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "WHERE EXISTS (WITH order_items AS (SELECT 1 AS x), products AS (SELECT 1 AS x) SELECT 1) GROUP BY 1",
        "WITH Products AS (SELECT 1 AS id) SELECT COUNT(*) FROM products",
    ],
)
def test_shadowing_ctes_rejected(guard, sql):
    with pytest.raises(SqlRejected) as err:
        guard.prepare(sql, TOM)
    assert err.value.kind == "forbidden_table"


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT `attacker-proj.ds.exfil`(brand) FROM {PRODUCTS}",
        f"SELECT attacker.exfil(brand) FROM {PRODUCTS}",
        f"SELECT HLL_COUNT.MERGE(x) FROM (SELECT 1 AS x)",
    ],
)
def test_qualified_functions_rejected(guard, sql):
    with pytest.raises(SqlRejected) as err:
        guard.prepare(sql, TOM)
    assert err.value.kind == "forbidden_function"


def test_safe_and_net_prefixes_still_work(guard):
    guard.prepare(f"SELECT SAFE.PARSE_DATE('%Y', '2024') AS d, NET.HOST('https://x.io') AS h FROM {PRODUCTS} LIMIT 1", TOM)


def test_scope_values_are_escaped(guard):
    evil = ProductScope({"brand": ("Levi's' OR '1'='1",)})
    guarded = guard.prepare(f"SELECT COUNT(*) FROM {PRODUCTS}", evil)
    assert "OR '1'='1'" not in guarded.sql.replace("\\'", "")


# --------------------------------------------------------------------------- customer attributes only in aggregates

BRANDS = ProductScope({"brand": ("Levi's", "Carhartt")})
U, I, O = USERS, ITEMS, ORDERS


@pytest.mark.parametrize(
    "sql, kind",
    [
        # one row per customer next to an attribute, in any form
        (f"SELECT * FROM {U} LIMIT 5", "customer_attributes"),
        (f"SELECT * FROM {O} LIMIT 5", "customer_attributes"),  # orders.gender next to user_id
        (f"SELECT oi.user_id, u.state, SUM(oi.sale_price) FROM {I} oi JOIN {U} u ON u.id = oi.user_id GROUP BY 1, 2", "customer_attributes"),
        (f"SELECT oi.user_id, SUM(oi.sale_price) FROM {I} oi JOIN {U} u ON u.id = oi.user_id WHERE u.state = 'Texas' GROUP BY 1", "customer_attributes"),
        (f"WITH c AS (SELECT id AS cid, city AS place FROM {U}) SELECT cid, place FROM c", "customer_attributes"),
        (f"SELECT id, COUNT(*) OVER (PARTITION BY state) FROM {U}", "customer_attributes"),
        (f"SELECT u.id FROM {U} u QUALIFY u.state = 'Texas'", "customer_attributes"),
        (f"SELECT oi.user_id FROM {I} oi JOIN {U} u ON u.id = oi.user_id ORDER BY u.age DESC", "customer_attributes"),
        (f"SELECT oi.user_id, SUM(oi.sale_price) FROM {I} oi JOIN {U} u ON u.id = oi.user_id GROUP BY 1 HAVING MAX(u.age) > 60", "customer_attributes"),
        (f"SELECT u.id FROM {U} u UNION ALL SELECT u.age FROM {U} u", "customer_attributes"),
        (f"SELECT oi.user_id AS state FROM {I} oi JOIN {U} u ON u.id = oi.user_id WHERE state = 'Texas'", "customer_attributes"),
        (f"SELECT oi.order_id, oi.sale_price FROM {I} oi JOIN {U} u ON u.id = oi.user_id WHERE u.state = 'Texas'", "customer_attributes"),
        (f"SELECT o.user_id FROM {O} o WHERE o.gender = 'F'", "customer_attributes"),
        (f"SELECT oi.user_id, `u.state` FROM {I} oi JOIN {U} u ON u.id = oi.user_id", "customer_attributes"),
        (f"SELECT o.user_id FROM {O} o JOIN {I} oi USING (user_id) WHERE o.gender = 'F'", "customer_attributes"),
        (f"SELECT user_id FROM {O} JOIN {I} USING (user_id) WHERE gender = 'F'", "customer_id_use"),  # COALESCE(ids)
        (f"SELECT city FROM {U}", "not_aggregated"),
        (f"SELECT state, age FROM (SELECT id, state, age FROM {U} GROUP BY 1, 2, 3)", "not_aggregated"),
        (f"SELECT MAX(age) FROM {U} GROUP BY id", "not_aggregated"),
        (f"SELECT state FROM {U} UNION ALL SELECT 'x'", "not_aggregated"),
        # review C2: values that pack many customers or a whole record
        (f"SELECT x.id, x.state FROM UNNEST(ARRAY(SELECT AS STRUCT u.id, u.state, u.age FROM {U} u)) AS x", "not_allowed"),
        (f"SELECT ARRAY(SELECT AS STRUCT u.id, u.state, u.city, u.age FROM {U} u) AS dump", "not_allowed"),
        (f"SELECT s FROM UNNEST(ARRAY(SELECT u.state FROM {U} u)) AS s", "not_allowed"),
        (f"SELECT (SELECT AS STRUCT u.id, u.state, u.age) AS r FROM {U} u", "not_allowed"),
        (f"SELECT STRUCT(u.id, u.city) AS s FROM {U} u", "not_allowed"),
        (f"SELECT o.user_id, x FROM {O} o, UNNEST([o.gender]) AS x", "not_allowed"),
        (f"SELECT FORMAT('%d:%s', u.id, u.state) AS x FROM {U} u", "not_allowed"),
        (f"SELECT TO_JSON_STRING(u) FROM {U} u", "table_reference"),
        (f"SELECT ARRAY_AGG(u) FROM {U} u", "table_reference"),
        # review C3: a customer ID inside an aggregate still lists individuals
        (f"SELECT u.state, ARRAY_AGG(u.id) AS ids FROM {U} u GROUP BY u.state", "not_allowed"),
        (f"SELECT STRING_AGG(CAST(u.id AS STRING), ',') AS ids FROM {U} u WHERE u.gender = 'F' AND u.age BETWEEN 60 AND 70", "not_allowed"),
        (f"SELECT TO_JSON_STRING(ARRAY_AGG(STRUCT(u.id, u.state, u.city, u.age, u.gender))) AS dump FROM {U} u", "not_allowed"),
        (f"SELECT (SELECT STRING_AGG(FORMAT('%d:%s:%d', u.id, u.state, u.age)) FROM {U} u) AS dump", "not_allowed"),
        (f"SELECT u.state, MAX(u.id) AS cid, COUNT(*) AS n FROM {U} u GROUP BY u.state HAVING COUNT(*) = 1", "customer_id_use"),
        (f"SELECT MAX(oi.user_id) AS customer, SUM(oi.sale_price) FROM {I} oi JOIN {U} u ON u.id = oi.user_id WHERE u.state = 'Texas' GROUP BY oi.user_id", "customer_id_use"),
        (f"SELECT u.state, APPROX_TOP_COUNT(u.city, 100) FROM {U} u GROUP BY 1", "not_allowed"),
        (f"SELECT u.state, APPROX_QUANTILES(u.age, 100) AS ages FROM {U} u GROUP BY 1", "not_allowed"),
        (f"SELECT CONCAT(CAST(u.id AS STRING), u.state) AS x FROM {U} u", "customer_id_use"),
        # review C4: grouping by a per-customer column
        (f"SELECT MAX(o.user_id) AS uid, MAX(o.gender) AS g FROM {O} o GROUP BY o.order_id", "customer_id_use"),
        (f"SELECT MIN(u.id) AS uid, MAX(u.state) AS st, MAX(u.age) AS a FROM {U} u GROUP BY u.created_at", "customer_id_use"),
        # review C5: picking customers by ID
        (f"SELECT u.state, u.city, u.age, u.gender, COUNT(*) AS n FROM {U} u WHERE u.id = 35 GROUP BY 1, 2, 3, 4", "customer_id_use"),
        (f"SELECT u.state, COUNT(*) FROM {U} u WHERE u.id IN (1, 2, 3, 4, 5) GROUP BY 1", "customer_id_use"),
        (f"SELECT u.gender, COUNT(DISTINCT IF(u.id = 35, u.id, NULL)) FROM {U} u GROUP BY 1", "customer_id_use"),
        (f"SELECT u.state, COUNT(*) FROM {U} u WHERE u.id IN (SELECT 35) GROUP BY 1", "customer_id_use"),
        # review C6: correlated subqueries
        (f"SELECT oi.user_id, SUM(oi.sale_price) AS spend FROM {I} oi WHERE (SELECT u.state FROM {U} u WHERE u.id = oi.user_id) = 'Texas' GROUP BY 1", "subquery"),
        (f"SELECT oi.user_id, (SELECT u.city FROM {U} u WHERE u.id = oi.user_id) FROM {I} oi", "subquery"),
        (f"SELECT oi.user_id FROM {I} oi WHERE EXISTS (SELECT 1 FROM {U} u WHERE u.id = oi.user_id AND u.age > 60)", "subquery"),
        (f"SELECT COUNT(*) FROM {PRODUCTS} p WHERE EXISTS (SELECT 1 FROM {I} oi JOIN {U} u ON u.id = oi.user_id WHERE oi.product_id = p.id AND u.age = 93)", "subquery"),
        # counting one set of customers while showing another's attributes
        (f"WITH x AS (SELECT a.id, b.state FROM {U} a CROSS JOIN {U} b WHERE b.age = 93) SELECT state, COUNT(*) FROM x GROUP BY state", "no_customer_id"),
        (f"WITH x AS (SELECT a.id, b.age, a.gender FROM {U} a LEFT JOIN {U} b ON a.id = b.id AND b.city = 'Juneau') SELECT gender, MAX(age) FROM x GROUP BY gender", "no_customer_id"),
        (f"SELECT u.state, COUNT(*) FROM {U} u WHERE u.id IN (SELECT a.id FROM {U} a CROSS JOIN {U} b WHERE b.age = 93 AND a.state = b.state) GROUP BY 1", "customer_id_use"),
        (f"SELECT state, COUNT(*) FROM (SELECT id, state FROM {U} WHERE age = 93 UNION ALL SELECT 1, 'Texas' UNION ALL SELECT 2, 'Texas') GROUP BY state", "no_customer_id"),
        (f"WITH tx AS (SELECT oi.sale_price FROM {I} oi JOIN {U} u ON u.id = oi.user_id WHERE u.age = 93) SELECT SUM(sale_price) FROM tx", "no_customer_id"),
        # second review: an aggregate that reads one row of a large group
        (f"SELECT MAX(IF(o.order_id = 1, o.gender, NULL)) g, MAX(IF(o.order_id = 1, u.state, NULL)) st FROM {O} o JOIN {U} u ON u.id = o.user_id", "customer_id_use"),
        (f"WITH r AS (SELECT o.user_id, u.state, ROW_NUMBER() OVER (ORDER BY o.created_at) rn FROM {O} o JOIN {U} u ON u.id = o.user_id) "
         f"SELECT DIV(rn, 8) b, COUNTIF(state = 'Texas') FROM r GROUP BY 1", "customer_id_use"),
        (f"SELECT SUM(u.age * IF(o.created_at = TIMESTAMP '2024-01-01 10:00:00', 1000000, 1)) FROM {O} o JOIN {U} u ON u.id = o.user_id", "not_allowed"),
        (f"SELECT u.state, SUM(1 / (ABS(oi.sale_price - 99.99) * 1000 + 1)) FROM {I} oi JOIN {U} u ON u.id = oi.user_id GROUP BY 1", "not_allowed"),
        (f"SELECT u.state, MAX(oi.sale_price) FROM {I} oi JOIN {U} u ON u.id = oi.user_id GROUP BY 1", "not_allowed"),
        (f"WITH c AS (SELECT u.id, u.state, COUNTIF(o.created_at = TIMESTAMP '2024-01-01 10:00:00') x FROM {O} o "
         f"JOIN {U} u ON u.id = o.user_id GROUP BY u.id, u.state) SELECT state, SUM(x) FROM c GROUP BY state", "not_allowed"),
        # second review: UNION pads a group with other customers' IDs
        (f"WITH t AS (SELECT u.id p, u.state st, u.id q FROM {U} u UNION ALL SELECT oi.user_id, 'Tokyo', oi.user_id FROM {I} oi) "
         f"SELECT st, COUNT(*) FROM t GROUP BY st", "no_customer_id"),
        (f"WITH t AS (SELECT u.id p, u.state st FROM {U} u UNION ALL SELECT oi.user_id, NULL FROM {I} oi) SELECT t.p, MAX(t.st) FROM t GROUP BY t.p", "customer_attributes"),
        (f"SELECT state, COUNT(*) FROM (SELECT id, state FROM {U} UNION ALL SELECT id, state FROM {U}) GROUP BY state", "no_customer_id"),
        # second review: IN (customer subquery) with something else on the left
        (f"SELECT n FROM UNNEST(GENERATE_ARRAY(1, 1000)) n WHERE n IN (SELECT u.id FROM {U} u WHERE u.state = 'Texas')", "subquery"),
        (f"SELECT n customer_id, k age FROM UNNEST(GENERATE_ARRAY(1, 1000)) n CROSS JOIN UNNEST(GENERATE_ARRAY(0, 120)) k "
         f"WHERE n IN (SELECT u.id FROM {U} u WHERE u.age = k)", "subquery"),
        # a per-order table without the customer ID cannot be counted (it used to return 0 rows silently)
        (f"WITH po AS (SELECT oi.order_id, u.gender, SUM(oi.sale_price) total FROM {I} oi JOIN {U} u ON u.id = oi.user_id "
         f"GROUP BY oi.order_id, u.gender) SELECT gender, AVG(total) FROM po GROUP BY 1", "no_customer_id"),
        (f"SELECT oi.user_id, MAX(u.state) FROM {I} oi JOIN {U} u ON u.id = oi.user_id GROUP BY ALL", "customer_attributes"),
        # third review: windows copying one row onto every row, attribute-only selectors, correlated
        # subqueries, rounding tricks, row multiplication, FILTER
        (f"WITH r AS (SELECT u.id, FIRST_VALUE(u.state) OVER (ORDER BY u.id) s, FIRST_VALUE(u.age) OVER (ORDER BY u.id) a "
         f"FROM {U} u) SELECT MAX(r.s), MAX(r.a) FROM r", "not_allowed"),
        (f"WITH g AS (SELECT o.user_id, o.gender, DATE_DIFF(DATE(o.created_at), LAG(DATE(o.created_at)) OVER "
         f"(PARTITION BY o.user_id ORDER BY o.created_at), DAY) gap FROM {O} o) SELECT g.gender, AVG(g.gap) FROM g GROUP BY 1", "not_allowed"),
        (f"SELECT SUM(IF(u.state = 'Tokyo' AND u.age = 14, TIMESTAMP_DIFF(oi.created_at, TIMESTAMP '1970-01-01', MICROSECOND), 0)) "
         f"FROM {I} oi JOIN {U} u ON u.id = oi.user_id", "not_allowed"),
        (f"SELECT MAX(IF((SELECT COUNT(*) FROM {PRODUCTS} p WHERE p.id = oi.product_id AND oi.created_at = TIMESTAMP '2024-01-01') = 1, "
         f"u.state, NULL)) FROM {I} oi JOIN {U} u ON u.id = oi.user_id", "subquery"),
        (f"SELECT SUM(u.age * DATE_DIFF(DATE(TIMESTAMP_ADD(u.created_at, INTERVAL 5 MICROSECOND)), "
         f"DATE(TIMESTAMP_ADD(u.created_at, INTERVAL 4 MICROSECOND)), DAY)) FROM {U} u", "not_allowed"),
        (f"SELECT AVG(u.age) FROM {U} u CROSS JOIN UNNEST(GENERATE_ARRAY(1, IF(u.created_at = TIMESTAMP '2024-01-01', 1000000, 1))) w", "not_allowed"),
        (f"SELECT AVG(u.age) FROM {U} u LEFT JOIN {PRODUCTS} p ON u.created_at = TIMESTAMP '2024-01-01'", "join"),
        (f"SELECT MAX(u.state) FILTER (WHERE u.created_at = TIMESTAMP '2024-01-01') FROM {U} u", "not_allowed"),
        (f"WITH c AS (SELECT u.id FROM {U} u LEFT JOIN {O} o ON o.user_id = u.id AND o.created_at = TIMESTAMP '2024-01-01') "
         f"SELECT u.state, COUNT(*) FROM c JOIN {U} u ON u.id = c.id GROUP BY 1", "join"),
        (f"SELECT u.state, SUM(oi.sale_price - 99.99 + 0.000001) FROM {I} oi JOIN {U} u ON u.id = oi.user_id GROUP BY 1", "not_allowed"),
        (f"WITH f AS (SELECT o.user_id, o.gender, ROW_NUMBER() OVER (PARTITION BY o.user_id ORDER BY o.created_at) rn FROM {O} o) "
         f"SELECT f.gender, COUNTIF(f.rn = 1) AS new_orders FROM f GROUP BY 1", "not_allowed"),
        # fourth review: a CTE mixing two customers per row through a product (or renamed) key,
        # picking a customer by ID in a customer-set CTE, padding a MIN with known extremes
        (f"WITH base AS (SELECT oi.user_id, oi.status AS product_id, oi.created_at, oi.sale_price, u.state, u.city, u.age "
         f"FROM {I} oi JOIN {U} u ON u.id = oi.user_id), mix AS (SELECT a.user_id, b.created_at, b.sale_price, b.state, "
         f"b.city, b.age FROM base a JOIN base b ON a.product_id = b.product_id) "
         f"SELECT m.created_at, m.sale_price, m.state, m.city, m.age, COUNT(*) n FROM mix m GROUP BY 1, 2, 3, 4, 5", "join"),
        (f"WITH mix AS (SELECT a.user_id, u.state, u.age FROM {I} a JOIN {I} b ON a.product_id = b.product_id "
         f"JOIN {U} u ON u.id = b.user_id) SELECT m.state, m.age, COUNT(DISTINCT m.user_id) n FROM mix m GROUP BY 1, 2", "join"),
        (f"WITH target AS (SELECT oi.user_id FROM {I} oi GROUP BY oi.user_id HAVING oi.user_id = 377) "
         f"SELECT u.state, COUNT(*) FROM target t JOIN {U} u ON u.id = t.user_id GROUP BY 1", "customer_id_use"),
        (f"WITH t AS (SELECT oi.user_id FROM {I} oi JOIN {U} u ON u.id = oi.user_id GROUP BY oi.user_id "
         f"HAVING MIN(u.age) = 14 OR MIN(u.state) > 'S') SELECT MIN(u.state) FROM t JOIN {U} u ON u.id = t.user_id", "not_allowed"),
        (f"SELECT MIN(u.age) FROM {U} u WHERE u.created_at = TIMESTAMP '2025-04-06 07:25:34' OR u.age > 60", "not_allowed"),
        (f"SELECT u.state, COUNT(*) - COUNTIF(u.age = 60) FROM {U} u WHERE u.created_at = TIMESTAMP '2025-04-06 07:25:34' "
         f"OR u.age = 60 GROUP BY 1", "not_allowed"),
        (f"SELECT u.state, COUNTIF(NOT (u.created_at != TIMESTAMP '2025-04-06 07:25:34' AND u.age != 60)) FROM {U} u GROUP BY 1", "not_allowed"),
        (f"SELECT IF(u.created_at = TIMESTAMP '2025-04-06 07:25:34' OR u.age = 60, 'x', 'y') g, AVG(u.age) FROM {U} u GROUP BY 1", "customer_attributes"),
        # what cannot be traced is rejected
        (f"SELECT t.x, COUNT(*) FROM (SELECT u.id AS x, u.state AS x FROM {U} u) t GROUP BY t.x", "unverifiable"),
        (f"WITH RECURSIVE r AS (SELECT u.id, u.state FROM {U} u UNION ALL SELECT r.id, r.state FROM r WHERE FALSE) SELECT state, COUNT(*) FROM r GROUP BY 1", "unverifiable"),
        (f"SELECT * FROM (SELECT id, state, city FROM {U}) UNPIVOT(v FOR k IN (state, city))", "unverifiable"),
    ],
)
def test_customer_attributes_rejected(guard, sql, kind):
    """Every attack is rejected. `kind` names the concern it tests; the rule may report another
    reason first (the allowlist usually catches an attack before the specific check would)."""
    with pytest.raises(SqlRejected) as err:
        guard.prepare(sql, BRANDS)
    assert err.value.kind in REJECTION_KINDS
    assert err.value.message


REJECTION_KINDS = {
    "customer_attributes", "not_aggregated", "customer_id_use", "not_allowed", "subquery", "join",
    "no_customer_id", "unverifiable", "table_reference", "invalid_sql",
}


@pytest.mark.parametrize(
    "sql, kind, says",
    [
        (f"SELECT oi.user_id, u.state, SUM(oi.sale_price) FROM {I} oi JOIN {U} u ON u.id = oi.user_id GROUP BY 1, 2",
         "customer_attributes", "one row per customer"),
        (f"SELECT * FROM {U} LIMIT 5", "customer_attributes", "individual customers' rows"),
        (f"SELECT u.state, COUNT(*) FROM {U} u WHERE u.id = 35 GROUP BY 1", "customer_id_use", "never filter on specific"),
        (f"SELECT u.state, ARRAY_AGG(u.id) FROM {U} u GROUP BY 1", "not_allowed", "pack many customers"),
        (f"SELECT oi.user_id FROM {I} oi WHERE EXISTS (SELECT 1 FROM {U} u WHERE u.id = oi.user_id AND u.age > 60)",
         "subquery", "join a CTE instead"),
        (f"WITH tx AS (SELECT oi.sale_price FROM {I} oi JOIN {U} u ON u.id = oi.user_id WHERE u.age = 93) "
         f"SELECT SUM(sale_price) FROM tx", "no_customer_id", "Keep user_id"),
        (f"SELECT u.state, MAX(oi.sale_price) FROM {I} oi JOIN {U} u ON u.id = oi.user_id GROUP BY 1",
         "not_allowed", "looked up"),
        (f"WITH c AS (SELECT oi.user_id, SUM(oi.sale_price) spend FROM {I} oi GROUP BY 1) "
         f"SELECT u.state, AVG(c.spend) FROM c JOIN {U} u ON u.id = c.user_id GROUP BY 1",
         "not_allowed", "SUM(sale_price) / COUNT(DISTINCT user_id)"),
    ],
)
def test_rejections_tell_the_model_what_to_do(guard, sql, kind, says):
    with pytest.raises(SqlRejected) as err:
        guard.prepare(sql, BRANDS)
    assert err.value.kind == kind
    assert says in err.value.message


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT u.state, COUNT(DISTINCT u.id) AS customers FROM {U} u GROUP BY u.state",
        f"SELECT DISTINCT country FROM {U}",
        f"SELECT AVG(age) AS avg_age FROM {U}",
        f"SELECT u.gender, SUM(oi.sale_price) FROM {U} u JOIN {I} oi ON oi.user_id = u.id GROUP BY 1",
        f"SELECT state, COUNT(*) FROM {U} GROUP BY 1 UNION ALL SELECT 'total', COUNT(*) FROM {U}",
        # profile the top customers in aggregate: no customer ID in the output
        f"WITH top AS (SELECT user_id FROM {I} GROUP BY 1 ORDER BY SUM(sale_price) DESC LIMIT 100) "
        f"SELECT u.country, COUNT(*) AS top_customers FROM top JOIN {U} u ON u.id = top.user_id GROUP BY 1",
        f"SELECT COUNT(DISTINCT user_id) FROM {O} WHERE gender = 'F'",
        f"SELECT o.gender, COUNT(*) FROM {O} o GROUP BY 1",
        # conditional counts, shares and semi-joins on customer ID
        f"SELECT u.gender, COUNT(DISTINCT IF(u.state = 'Texas', u.id, NULL)) AS texans FROM {U} u GROUP BY 1",
        f"SELECT u.state, COUNT(*) AS n, SUM(COUNT(*)) OVER () AS total FROM {U} u GROUP BY 1",
        f"SELECT u.state, APPROX_QUANTILES(u.age, 2)[OFFSET(1)] AS median_age FROM {U} u GROUP BY 1",
        # conditional aggregates (each gets its own 5-customer check), windows per customer, per-order tables
        f"SELECT u.gender, SAFE_DIVIDE(COUNTIF(oi.status = 'Returned'), COUNTIF(oi.status IN ('Complete', 'Returned'))) AS rr "
        f"FROM {I} oi JOIN {U} u ON u.id = oi.user_id GROUP BY 1",
        # a set of customers picked per customer, then profiled
        f"WITH first AS (SELECT o.user_id FROM {O} o GROUP BY 1 HAVING MIN(DATE(o.created_at)) >= DATE '2024-01-01') "
        f"SELECT u.gender, COUNT(*) AS new_customers FROM first JOIN {U} u ON u.id = first.user_id GROUP BY 1",
        # per-customer and per-order figures as ratios of aggregates
        f"SELECT u.gender, SUM(oi.sale_price) / COUNT(DISTINCT oi.order_id) AS aov, "
        f"SUM(oi.sale_price) / COUNT(DISTINCT oi.user_id) AS spend_per_customer FROM {I} oi JOIN {U} u ON u.id = oi.user_id GROUP BY 1",
        f"SELECT u.state, AVG(DATE_DIFF(CURRENT_DATE(), DATE(o.created_at), DAY)) AS order_age FROM {O} o "
        f"JOIN {U} u ON u.id = o.user_id GROUP BY 1",
        # a CTE that filters rows, keeping user_id
        f"WITH done AS (SELECT oi.user_id, oi.sale_price FROM {I} oi WHERE oi.status = 'Complete') "
        f"SELECT u.country, SUM(done.sale_price) FROM done JOIN {U} u ON u.id = done.user_id GROUP BY 1",
        f"(SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY 1)",
        f"(SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY 1) ORDER BY n DESC LIMIT 3",
        # comma join with the key in WHERE, ROLLUP, hour-level durations, NOT IN and one-column OR
        f"SELECT u.state, COUNT(*) FROM {U} u, {I} oi WHERE u.id = oi.user_id AND oi.status NOT IN ('Cancelled') GROUP BY 1",
        f"SELECT u.country, u.gender, COUNT(*) AS n FROM {U} u GROUP BY ROLLUP (u.country, u.gender)",
        f"SELECT u.country, AVG(TIMESTAMP_DIFF(o.shipped_at, o.created_at, HOUR)) AS ship_hours FROM {O} o "
        f"JOIN {U} u ON u.id = o.user_id WHERE o.status = 'Complete' OR o.status = 'Shipped' GROUP BY 1",
        f"SELECT p.category, COUNT(*) FROM {I} oi JOIN {PRODUCTS} p ON p.id = oi.product_id "
        f"JOIN {U} u ON u.id = oi.user_id WHERE u.age > 60 GROUP BY 1",
        # an already-aggregated table can be read row by row
        f"SELECT x.state, x.n FROM (SELECT state, COUNT(*) AS n FROM {U} GROUP BY 1) x ORDER BY x.n DESC",
    ],
)
def test_customer_attributes_allowed_with_group_size_check(guard, sql):
    guarded = guard.prepare(sql, BRANDS)
    assert "HAVING COUNT(DISTINCT" in guarded.sql
    assert guarded.notes and "fewer than 5" in guarded.notes[0]


@pytest.mark.parametrize(
    "sql",
    [
        # no customer attribute anywhere: customer lists by ID are fine and run as written
        f"SELECT oi.user_id AS customer_id, SUM(oi.sale_price) AS spend FROM {I} oi GROUP BY 1 ORDER BY spend DESC LIMIT 10",
        f"SELECT u.id, COUNT(DISTINCT oi.order_id) FROM {U} u JOIN {I} oi ON oi.user_id = u.id GROUP BY u.id",
        f"SELECT COUNT(*) AS state FROM {U}",
        f"SELECT COUNT(*) FROM {I} oi WHERE oi.user_id IN (SELECT 35)",
    ],
)
def test_queries_without_attributes_run_unchanged(guard, sql):
    guarded = guard.prepare(sql, BRANDS)
    assert "HAVING COUNT(DISTINCT" not in guarded.sql and not guarded.notes


def test_small_groups_are_left_out(guard, sample_db):
    from retail_agent.data.warehouse import DuckDBWarehouse

    warehouse = DuckDBWarehouse(sample_db, "bigquery-public-data.thelook_ecommerce")
    fine = f"SELECT state, city, gender, COUNT(DISTINCT id) AS customers FROM {U} GROUP BY 1, 2, 3"
    guarded = warehouse.query(guard.prepare(fine, ALL).sql, labels={}, max_rows=10_000)
    unguarded = warehouse.conn.execute(
        "SELECT COUNT(*) FROM (SELECT state, city, gender, COUNT(DISTINCT id) AS n FROM users GROUP BY 1, 2, 3) WHERE n < 5"
    ).fetchone()[0]
    assert unguarded > 0  # the sample has groups an unguarded query would show
    assert guarded.rows and all(row[-1] >= 5 for row in guarded.rows)
    # one row per person (DISTINCT on a near-unique column) comes back empty, not as a profile
    per_person = guard.prepare(f"SELECT DISTINCT u.created_at, u.state, u.city, u.age, u.gender FROM {U} u", ALL)
    assert warehouse.query(per_person.sql, labels={}, max_rows=10).total_rows == 0


def test_apostrophes_in_brand_scope_are_escaped(guard):
    guarded = guard.prepare(f"SELECT COUNT(*) FROM {PRODUCTS}", BRANDS)
    assert "Levi\\'s" in guarded.sql


def test_a_conditional_cell_over_few_customers_is_blank(guard, sample_db):
    from retail_agent.data.warehouse import DuckDBWarehouse

    warehouse = DuckDBWarehouse(sample_db, "bigquery-public-data.thelook_ecommerce")
    first = warehouse.conn.execute("SELECT CAST(created_at AS VARCHAR) FROM orders ORDER BY order_id LIMIT 1").fetchone()[0]
    one_order = (f"SELECT MAX(IF(o.created_at = TIMESTAMP '{first}', u.state, NULL)) AS st, "
                 f"COUNTIF(o.created_at = TIMESTAMP '{first}') AS hits, COUNT(*) AS n FROM {O} o JOIN {U} u ON u.id = o.user_id")
    row = warehouse.query(guard.prepare(one_order, ALL).sql, labels={}, max_rows=1).rows[0]
    assert row[0] is None and row[1] is None and row[2] > 100  # the whole table is there; the one-order cells are not
    returns = (f"SELECT u.gender, COUNTIF(oi.status = 'Returned') AS returned FROM {I} oi "
               f"JOIN {U} u ON u.id = oi.user_id GROUP BY 1")
    assert all(r[1] is not None for r in warehouse.query(guard.prepare(returns, ALL).sql, labels={}, max_rows=5).rows)


def test_deep_nesting_is_rejected_not_crashed(guard):
    with pytest.raises(SqlRejected) as err:
        guard.prepare("SELECT " + "(" * 200 + "1" + ")" * 200, ALL)
    assert err.value.kind == "invalid_sql"


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT COUNT(*) FROM ({USERS} u JOIN {ITEMS} oi ON oi.user_id = u.id)",
        f"SELECT COUNT(*) FROM {USERS} u TABLESAMPLE SYSTEM (10 PERCENT)",
        f"SELECT COUNT(*) FILTER (WHERE status = 'Complete') FROM {ORDERS}",
        "SELECT 'abc",
        "SELECT 1 /* never closed",
    ],
)
def test_syntax_the_rewrite_cannot_keep_is_rejected(guard, sql):
    with pytest.raises(SqlRejected) as err:
        guard.prepare(sql, ALL)
    assert err.value.kind == "invalid_sql"


def test_integer_division_keeps_its_grouping_offline(sample_db):
    from retail_agent.data.warehouse import DuckDBWarehouse

    warehouse = DuckDBWarehouse(sample_db, "bigquery-public-data.thelook_ecommerce")
    assert warehouse.query("SELECT DIV(11 - 1, 5) AS x", labels={}, max_rows=1).rows == [[2]]
