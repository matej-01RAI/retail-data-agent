"""SQL guard: the deterministic boundary between model-written SQL and the warehouse.

Every query the model writes goes through `SqlGuard.prepare()` before it can
reach BigQuery. It does not trust the model, the prompt or the user:

1. Parse (BigQuery dialect). Exactly one statement, and it must be a query
   (SELECT / WITH / set operation). DML, DDL, scripting, EXPORT DATA -> rejected.
2. Allowlist. Every table must be one of the dataset's analysis tables (or a
   CTE defined in the query; a CTE may not reuse a table's name).
   INFORMATION_SCHEMA, other datasets, wildcard tables and table-valued
   functions -> rejected. Functions that reach outside the dataset
   (EXTERNAL_QUERY, ML.*, AI.*, ...) and user-defined or qualified functions
   (`project.dataset.fn`) -> rejected.
3. PII. Any reference to a PII column (by name, anywhere in the query) is
   rejected with a message that tells the model what to use instead.
4. Rewrite. Every table reference is replaced by a *scoped subquery* that
   (a) projects only the non-PII columns and (b) filters rows to the user's
   product entitlement. So even a query the checks above missed cannot see
   PII or another team's products: those rows and columns are not in its FROM.
   Customers and orders are visible only through purchases of in-scope
   products, and an order's num_of_item counts only its in-scope items.
5. Customer attributes only in aggregates (aggregate_rule.py): city, state,
   country, age, gender and traffic source never sit next to an individual
   customer, never filter a list of customers, and are only shown aggregated,
   over groups of at least 5 customers (a HAVING clause the guard adds).

The rewritten SQL is what runs. In production the same guarantees are also
enforced by BigQuery itself (authorized views + row-level access policies +
column-level policy tags), so the guard is defence in depth, not the only wall.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from ..identity import ProductScope
from ..knowledge import DataDictionary
from .aggregate_rule import AggregateOnlyRule, Rejected

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
    notes: tuple[str, ...] = ()  # for the model, e.g. that small groups were left out


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
        self.aggregate_rule = AggregateOnlyRule(dictionary, project, dataset, self.projection)

    # ------------------------------------------------------------------ public

    def prepare(self, sql: str, scope: ProductScope) -> GuardedQuery:
        try:
            return self._prepare(sql, scope)
        except RecursionError:
            raise SqlRejected("invalid_sql", "The query is nested too deeply to check. Use CTEs instead of deep nesting.") from None

    def _prepare(self, sql: str, scope: ProductScope) -> GuardedQuery:
        tree = self._parse_single_query(sql)
        cte_names = self._check_ctes(tree)
        self._check_functions(tree)
        self._check_pii(tree)
        tables = self._check_and_collect_tables(tree, cte_names)
        try:
            checked = self.aggregate_rule.check(tree)
        except Rejected as e:
            raise SqlRejected(e.kind, e.message) from e
        out = checked.tree if checked.tree is not None else tree
        rewritten = self._rewrite(out, scope).sql(dialect="bigquery")
        names = tuple(sorted({self._table_key(t) for t in tables}))
        return GuardedQuery(original_sql=sql, sql=rewritten, tables=names, notes=checked.notes)

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
        except TokenError as e:  # an unterminated string, quoted name or comment
            raise SqlRejected("invalid_sql", f"SQL syntax error: {_short(str(e))}") from e
        if len(statements) != 1:
            raise SqlRejected("not_read_only", "Send exactly one SELECT statement (no ';'-separated scripts).")
        tree = statements[0]
        # (SELECT ...) on its own, or followed by ORDER BY / LIMIT: the parentheses add nothing.
        while isinstance(tree, exp.Subquery) and not tree.alias:
            extra = {k: v for k, v in tree.args.items() if v and k != "this"}
            inner = tree.this
            if not set(extra) <= {"order", "limit", "offset"} or any(inner.args.get(k) for k in extra):
                break
            for k, v in extra.items():
                inner.set(k, v)
            tree = inner.pop()
        if not isinstance(tree, exp.Query):
            raise SqlRejected(
                "not_read_only",
                f"Only read-only SELECT queries are allowed; got {tree.key.upper()}. "
                "The database is read-only and saved reports are managed with the report tools.",
            )
        for node in tree.find_all(exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command):
            raise SqlRejected("not_read_only", f"{node.key.upper()} is not allowed inside a query.")
        return tree

    def _check_ctes(self, tree: exp.Expression) -> set[str]:
        names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        shadowing = sorted(names & self.allowed_tables)
        if shadowing:
            raise SqlRejected(
                "forbidden_table",
                f"A CTE may not reuse a table's name ({', '.join(shadowing)}). Name it something else, e.g. "
                f"{shadowing[0]}_filtered.",
            )
        return names

    def _check_functions(self, tree: exp.Expression) -> None:
        for func in tree.find_all(exp.Anonymous):
            if "." in func.name or (isinstance(func.parent, exp.Dot) and func.parent.expression is func):
                raise SqlRejected(
                    "forbidden_function",
                    f"User-defined or qualified functions ({func.sql('bigquery').split('(')[0]}) are not available; "
                    "use BigQuery built-in functions.",
                )
        if tree.find(exp.Filter):
            raise SqlRejected("invalid_sql", "FILTER (WHERE ...) is not BigQuery syntax; use COUNTIF or IF inside the aggregate.")
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
            if table.args.get("joins") or table.args.get("sample"):
                # The scope rewrite replaces the table node; joins nested in parentheses or a sample would be lost.
                raise SqlRejected("invalid_sql", "Write joins without parentheses around them, and do not use TABLESAMPLE.")
            name = table.name.lower()
            db = (table.db or "").lower()
            catalog = (table.catalog or "").lower()
            if not db and not catalog and name in cte_names:
                continue  # reference to a CTE defined in this query (never a table name: see _check_ctes)
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

    def _rewrite(self, tree: exp.Expression, scope: ProductScope) -> exp.Expression:
        # Every reference to an analysis table: CTEs cannot take a table's name (_check_ctes).
        for table in list(tree.find_all(exp.Table)):
            name = self._table_key(table)
            if name not in self.allowed_tables:
                continue
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

    def _scoped_select(self, table: str, scope: ProductScope) -> exp.Select:
        cols = self.projection[table]
        if scope.unrestricted:
            return exp.select(*cols).from_(self._raw(table))
        raw = {t: self._raw(t).sql(dialect="bigquery") for t in self.allowed_tables}
        predicate = self._product_predicate(scope).sql(dialect="bigquery")  # literals escaped by sqlglot
        in_scope_products = f"SELECT id FROM {raw['products']} WHERE {predicate}"
        if table == "products":
            sql = f"SELECT {', '.join(cols)} FROM {raw['products']} WHERE {predicate}"
        elif table == "order_items":
            sql = f"SELECT {', '.join(cols)} FROM {raw['order_items']} WHERE product_id IN ({in_scope_products})"
        elif table == "orders":
            # Orders that contain in-scope items; num_of_item counts only those items, so an
            # order does not reveal how many other brands' items it held.
            projection = ", ".join("s.num_of_item AS num_of_item" if c == "num_of_item" else f"o.{c}" for c in cols)
            sql = (
                f"SELECT {projection} FROM {raw['orders']} AS o JOIN (SELECT order_id, COUNT(*) AS num_of_item "
                f"FROM {raw['order_items']} WHERE product_id IN ({in_scope_products}) GROUP BY order_id) AS s "
                "ON s.order_id = o.order_id"
            )
        elif table == "users":
            # Customers who bought in-scope products.
            sql = (
                f"SELECT {', '.join(cols)} FROM {raw['users']} WHERE id IN "
                f"(SELECT user_id FROM {raw['order_items']} WHERE product_id IN ({in_scope_products}))"
            )
        else:
            raise SqlRejected("forbidden_table", f"No entitlement rule for table {table}.")
        return sqlglot.parse_one(sql, read="bigquery")


def _describe_parse_error(error: ParseError) -> str:
    if not error.errors:
        return _short(str(error))
    first = error.errors[0]
    near = f"{first.get('start_context', '')}{first.get('highlight', '')}{first.get('end_context', '')}"
    return _short(f"{first.get('description')} at line {first.get('line')}, column {first.get('col')}, near: {near.strip()}")


def _short(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
