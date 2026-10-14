"""SQL guard: the deterministic boundary between model-written SQL and the warehouse.

Every query the model writes goes through `SqlGuard.prepare()` before it can
reach BigQuery. It does not trust the model, the prompt or the user:

1. Parse (BigQuery dialect). Exactly one statement, and it must be a query
   (SELECT / WITH / set operation). DML, DDL, scripting, EXPORT DATA -> rejected.
2. Allowlist. Every table must be one of the dataset's analysis tables (or a
   CTE defined in the query). INFORMATION_SCHEMA, other datasets, wildcard
   tables and table-valued functions -> rejected. A small denylist of
   functions that reach outside the dataset (EXTERNAL_QUERY, ML.*, AI.*, ...)
   -> rejected.
3. PII. Any reference to a PII column (by name, anywhere in the query) is
   rejected with a message that tells the model what to use instead.
4. Rewrite. Every table reference is replaced by a *scoped subquery* that
   (a) projects only the non-PII columns and (b) filters rows to the user's
   product entitlement. So even a query the checks above missed cannot see
   PII or another team's products: those rows and columns are not in its FROM.

The rewritten SQL is what runs. In production the same guarantees are also
enforced by BigQuery itself (authorized views + row-level access policies +
column-level policy tags), so the guard is defence in depth, not the only wall.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from ..identity import ProductScope
from ..knowledge import DataDictionary

DENIED_FUNCTION_PREFIXES = (
    "EXTERNAL_QUERY",
    "ML.",
    "AI.",
    "VECTOR_SEARCH",
    "APPENDS",
    "CHANGES",
    "KEYS.",
    "AEAD.",
    "DETERMINISTIC_",
    "OBJ.",
    "EXTERNAL_OBJECT_TRANSFORM",
    "SESSION_USER",
)


class SqlRejected(ValueError):
    """The query is not allowed. `message` is written for the model to act on."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}")


@dataclass(frozen=True)
class GuardedQuery:
    original_sql: str
    sql: str  # rewritten, scoped SQL (BigQuery dialect)
    tables: tuple[str, ...]  # analysis tables referenced


class SqlGuard:
    def __init__(self, dictionary: DataDictionary, live_columns: dict[str, list[str]] | None = None):
        self.dictionary = dictionary
        project, dataset = dictionary.dataset.split(".", 1)
        self.project, self.dataset_name = project, dataset
        self.allowed_tables = set(dictionary.tables)
        self.pii_columns = {c.lower() for c in dictionary.pii_columns}
        # Columns each scoped subquery exposes: non-PII columns from the
        # dictionary, intersected with the live schema when we know it (so a
        # column the dataset dropped cannot break every query).
        self.projection: dict[str, list[str]] = {}
        for name, table in dictionary.tables.items():
            cols = table.safe_columns
            if live_columns and name in live_columns:
                live = {c.lower() for c in live_columns[name]}
                cols = [c for c in cols if c.lower() in live]
            self.projection[name] = cols

    # ------------------------------------------------------------------ public

    def prepare(self, sql: str, scope: ProductScope) -> GuardedQuery:
        tree = self._parse_single_query(sql)
        cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        self._check_functions(tree)
        self._check_pii(tree)
        tables = self._check_and_collect_tables(tree, cte_names)
        rewritten = self._rewrite(tree, tables, cte_names, scope)
        names = tuple(sorted({self._table_key(t) for t in tables}))
        return GuardedQuery(original_sql=sql, sql=rewritten.sql(dialect="bigquery"), tables=names)

    def fq_name(self, table: str) -> str:
        return f"`{self.project}.{self.dataset_name}.{table}`"

    # ------------------------------------------------------------------ checks

    def _parse_single_query(self, sql: str) -> exp.Expression:
        if not sql or not sql.strip():
            raise SqlRejected("invalid_sql", "The query is empty.")
        try:
            statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
        except ParseError as e:
            raise SqlRejected("invalid_sql", f"SQL syntax error: {_describe_parse_error(e)}") from e
        if len(statements) != 1:
            raise SqlRejected("not_read_only", "Send exactly one SELECT statement (no ';'-separated scripts).")
        tree = statements[0]
        if not isinstance(tree, exp.Query):
            raise SqlRejected(
                "not_read_only",
                f"Only read-only SELECT queries are allowed; got {tree.key.upper()}. "
                "The database is read-only and saved reports are managed with the report tools.",
            )
        for node in tree.find_all(exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command):
            raise SqlRejected("not_read_only", f"{node.key.upper()} is not allowed inside a query.")
        return tree

    def _check_functions(self, tree: exp.Expression) -> None:
        for func in tree.find_all(exp.Func):
            name = (func.sql_name() if not isinstance(func, exp.Anonymous) else func.name).upper()
            parent = func.parent
            if isinstance(parent, exp.Dot):  # e.g. ML.PREDICT parses as Dot(ML, PREDICT(...))
                name = parent.sql(dialect="bigquery").upper()
            if any(name.startswith(p) for p in DENIED_FUNCTION_PREFIXES):
                raise SqlRejected("forbidden_function", f"Function {name.split('(')[0]} is not allowed.")

    def _check_pii(self, tree: exp.Expression) -> None:
        for col in tree.find_all(exp.Column):
            if col.name.lower() in self.pii_columns:
                raise SqlRejected(
                    "pii_column",
                    f"Column '{col.name}' is personal data (PII) and cannot be queried, filtered on or "
                    "returned. Identify customers by users.id / user_id and describe them with non-PII "
                    "attributes (age band, gender, city, state, country, traffic_source).",
                )
        # JOIN ... USING (email) names the column as a bare identifier.
        for ident in tree.find_all(exp.Identifier):
            if ident.name.lower() in self.pii_columns and isinstance(ident.parent, exp.Join):
                raise SqlRejected("pii_column", f"Column '{ident.name}' is personal data (PII) and cannot be used.")
        # Table-wide references such as TO_JSON_STRING(u) or SELECT u.* are
        # safe by construction: the scoped subquery has no PII columns.

    def _table_key(self, table: exp.Table) -> str:
        return table.name.lower()

    def _check_and_collect_tables(self, tree: exp.Expression, cte_names: set[str]) -> list[exp.Table]:
        found: list[exp.Table] = []
        for table in tree.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                raise SqlRejected("forbidden_table", f"Table expression {table.sql('bigquery')!r} is not allowed.")
            name = table.name.lower()
            db = (table.db or "").lower()
            catalog = (table.catalog or "").lower()
            if not db and not catalog and name in cte_names:
                continue  # reference to a CTE defined in this query
            if "*" in name or name.endswith("information_schema") or db.endswith("information_schema"):
                raise SqlRejected("forbidden_table", "Metadata and wildcard tables are not available. Use describe_data.")
            if name not in self.allowed_tables:
                raise SqlRejected(
                    "forbidden_table",
                    f"Table '{table.sql('bigquery')}' is not available. Allowed tables: "
                    + ", ".join(self.fq_name(t) for t in sorted(self.allowed_tables)),
                )
            if db and db != self.dataset_name.lower():
                raise SqlRejected("forbidden_table", f"Only the {self.dataset_name} dataset is available.")
            if catalog and catalog != self.project.lower():
                raise SqlRejected("forbidden_table", f"Only the {self.project} project is available.")
            found.append(table)
        return found

    # ------------------------------------------------------------------ rewrite

    def _rewrite(
        self, tree: exp.Expression, tables: list[exp.Table], cte_names: set[str], scope: ProductScope
    ) -> exp.Expression:
        for table in tables:
            name = self._table_key(table)
            alias = table.alias or table.name
            scoped = self._scoped_select(name, scope)
            table.replace(exp.Subquery(this=scoped, alias=exp.TableAlias(this=exp.to_identifier(alias))))
        return tree

    def _raw(self, table: str) -> exp.Table:
        return exp.Table(
            this=exp.to_identifier(table),
            db=exp.to_identifier(self.dataset_name),
            catalog=exp.to_identifier(self.project, quoted=True),
        )

    def _product_predicate(self, scope: ProductScope) -> exp.Expression | None:
        if scope.unrestricted:
            return None
        conds = [
            exp.In(this=exp.column(dim), expressions=[exp.Literal.string(v) for v in values])
            for dim, values in scope.filters.items()
        ]
        return exp.and_(*conds)

    def _entitled_product_ids(self, scope: ProductScope) -> exp.Select:
        return exp.select("id").from_(self._raw("products")).where(self._product_predicate(scope))

    def _scoped_select(self, table: str, scope: ProductScope) -> exp.Select:
        select = exp.select(*self.projection[table]).from_(self._raw(table))
        if scope.unrestricted:
            return select
        product_ids = self._entitled_product_ids(scope)
        if table == "products":
            return select.where(self._product_predicate(scope))
        if table == "order_items":
            return select.where(exp.column("product_id").isin(query=product_ids))
        if table == "orders":
            orders_in_scope = (
                exp.select("order_id")
                .from_(self._raw("order_items"))
                .where(exp.column("product_id").isin(query=product_ids))
            )
            return select.where(exp.column("order_id").isin(query=orders_in_scope))
        if table == "users":
            buyers_in_scope = (
                exp.select("user_id")
                .from_(self._raw("order_items"))
                .where(exp.column("product_id").isin(query=product_ids))
            )
            return select.where(exp.column("id").isin(query=buyers_in_scope))
        raise SqlRejected("forbidden_table", f"No entitlement rule for table {table}.")


def _describe_parse_error(error: ParseError) -> str:
    if not error.errors:
        return _short(str(error))
    first = error.errors[0]
    near = f"{first.get('start_context', '')}{first.get('highlight', '')}{first.get('end_context', '')}"
    return _short(f"{first.get('description')} at line {first.get('line')}, column {first.get('col')}, near: {near.strip()}")


def _short(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
