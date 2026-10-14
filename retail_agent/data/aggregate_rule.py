"""Quasi-identifiers only in aggregates (OpsFleet: city, state, country, age, gender and
traffic source are fine, but only in aggregates; customers may be referenced by user_id).

Runs on the guard's *rewritten* SQL, after it is qualified against the schema,
and traces every column back to its base table column through CTEs, derived
tables, set operations and scalar subqueries (sqlglot scopes). Three rules:

R1  A customer-level result (an output that is an un-aggregated customer key:
    users.id, orders.user_id, order_items.user_id) may not use a demographic
    column anywhere in the query, not in the output and not as a filter.
    "Customer IDs in Texas aged 67" identifies people as well as a label does.
R2  Every demographic column in the output must be aggregated across customers
    somewhere on its path: inside an aggregate function, or in a SELECT that
    groups (GROUP BY / DISTINCT / aggregates) without grouping by a customer key.
R3  Whole-row references (TO_JSON_STRING(u), u used as a value) are rejected:
    their content cannot be traced.

Anything the resolver cannot trace in a query that touches demographics fails
closed. Small groups (k-anonymity) are enforced in production by BigQuery
aggregation-threshold policies (docs/technical-design.md §4).
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope

from ..knowledge import DataDictionary

Leaf = tuple[str, str]  # (base table, column)

CUSTOMER_KEYS: frozenset[Leaf] = frozenset({("users", "id"), ("orders", "user_id"), ("order_items", "user_id")})


@dataclass(frozen=True)
class Origin:
    leaf: Leaf | None  # None: could not be traced
    aggregated: bool  # inside an aggregate function somewhere on the path
    across_customers: bool  # aggregated by a SELECT that does not group by customer


def _has_aggregate(node: exp.Expression) -> bool:
    return any(agg.find_ancestor(exp.Window) is None for agg in node.find_all(exp.AggFunc))


class AggregateOnlyRule:
    def __init__(self, dictionary: DataDictionary, project: str, dataset: str):
        self.schema = {
            project: {dataset: {t: {c.name: c.type for c in tbl.columns.values()} for t, tbl in dictionary.tables.items()}}
        }
        self.quasi = frozenset(
            (t, c.name) for t, tbl in dictionary.tables.items() for c in tbl.columns.values() if c.aggregate_only
        )

    # ------------------------------------------------------------------ entry point

    def violations(self, rewritten_sql: str) -> list[tuple[str, str]]:
        """[(kind, message)]; empty when the query is allowed."""
        tree = sqlglot.parse_one(rewritten_sql, read="bigquery")
        if not any(c.name.lower() in {q[1] for q in self.quasi} for c in tree.find_all(exp.Column)) and not tree.find(exp.Star):
            # No demographic column can be reached (names checked before qualifying, stars expand later).
            return []
        try:
            tree = qualify(tree, schema=self.schema, dialect="bigquery", validate_qualify_columns=False)
            root = build_scope(tree)
        except (OptimizeError, SqlglotError) as e:
            return [("unverifiable", f"The query could not be checked for customer privacy ({e}). Simplify it.")]
        if root is None:
            return []

        problems: list[tuple[str, str]] = []
        whole_row = self._whole_row_references(root)
        if whole_row:
            problems.append((
                "table_reference",
                f"Whole-row references ({', '.join(sorted(whole_row))}) are not allowed; select explicit columns.",
            ))

        outputs = self._trace_outputs(root)
        output_refs = [o for origins in outputs.values() for o in origins]
        uses_demographics = sorted({o.leaf[1] for o in output_refs + self._clause_origins(root) if o.leaf in self.quasi})
        customer_outputs = sorted({name for name, origins in outputs.items()
                                   for o in origins if o.leaf in CUSTOMER_KEYS and not o.aggregated})
        if customer_outputs and uses_demographics:
            problems.append((
                "customer_attributes",
                f"This result has one row per customer ({', '.join(customer_outputs)}), so it cannot use "
                f"{', '.join(uses_demographics)} (not as a column and not as a filter). Customer attributes "
                "(city, state, country, age, gender, traffic_source) may only appear as aggregates across "
                "customers. List customers by ID with purchase metrics only, and analyse demographics in a "
                "separate aggregated query.",
            ))
        not_aggregated = sorted({name for name, origins in outputs.items()
                                 for o in origins if o.leaf in self.quasi and not o.across_customers})
        if not_aggregated:
            problems.append((
                "not_aggregated",
                f"Output column(s) {', '.join(not_aggregated)} show customer attributes row by row. These "
                "attributes may only be shown as aggregates across customers: GROUP BY them with "
                "COUNT(DISTINCT user_id), SUM, AVG..., or use them inside an aggregate function.",
            ))
        if (customer_outputs or uses_demographics) and any(o.leaf is None for os in outputs.values() for o in os):
            if not problems:
                problems.append((
                    "unverifiable",
                    "Part of this query's output could not be traced, so it cannot be checked for customer "
                    "privacy. Use explicit columns from the tables (no table-wide expressions).",
                ))
        return problems

    # ------------------------------------------------------------------ tracing

    def _trace_outputs(self, scope: Scope) -> dict[str, list[Origin]]:
        expr = scope.expression
        if isinstance(expr, exp.SetOperation):
            branches = scope.set_operation_scopes
            names = branches[0].expression.named_selects if branches else []
            out: dict[str, list[Origin]] = {n: [] for n in names}
            for branch in branches:
                for i, name in enumerate(names):
                    for origin_list in self._trace_output_at(branch, i).values():
                        out[name].extend(origin_list)
            return out
        if isinstance(expr, exp.Select):
            return {proj.alias_or_name: self._trace_projection(scope, proj) for proj in expr.selects}
        return {}

    def _trace_output_at(self, scope: Scope, index: int) -> dict[str, list[Origin]]:
        outputs = self._trace_outputs(scope)
        names = list(outputs)
        return {names[index]: outputs[names[index]]} if index < len(names) else {}

    def _trace_projection(self, scope: Scope, proj: exp.Expression) -> list[Origin]:
        select = scope.expression
        agg_here = _has_aggregate(proj)
        groups_here = isinstance(select, exp.Select) and (
            agg_here
            or bool(select.args.get("group"))
            or bool(select.args.get("distinct"))
            or any(_has_aggregate(p) for p in select.selects)
        )
        across_here = groups_here and not self._grouped_by_customer(scope)
        origins: list[Origin] = []
        for col in self._own_columns(proj):
            for o in self._trace_column(scope, col):
                origins.append(Origin(o.leaf, o.aggregated or agg_here, o.across_customers or across_here))
        for sub in self._scalar_subquery_scopes(scope, proj):
            for sub_origins in self._trace_outputs(sub).values():
                for o in sub_origins:
                    origins.append(Origin(o.leaf, o.aggregated or agg_here, o.across_customers or across_here))
        return origins

    def _trace_column(self, scope: Scope, col: exp.Column) -> list[Origin]:
        source = scope.sources.get(col.table)
        if isinstance(source, exp.Table):
            return [Origin((source.name.lower(), col.name.lower()), False, False)]
        if isinstance(source, Scope):
            traced = self._trace_outputs(source)
            for name, origins in traced.items():
                if name.lower() == col.name.lower():
                    return origins
            return [Origin(None, False, False)]
        if not col.table:
            # A bare name: a SELECT alias (ORDER BY revenue) or an UNNEST value. Neither reaches a table.
            return []
        return [Origin(None, False, False)]

    def _grouped_by_customer(self, scope: Scope) -> bool:
        select = scope.expression
        group = select.args.get("group") if isinstance(select, exp.Select) else None
        if not group:
            return False
        keys: list[exp.Expression] = []
        for g in group.expressions:
            if isinstance(g, exp.Literal) and g.is_int and 0 < int(g.name) <= len(select.selects):
                keys.append(select.selects[int(g.name) - 1])
            else:
                keys.append(g)
        for key in keys:
            for col in key.find_all(exp.Column):
                for o in self._trace_column(scope, col):
                    if o.leaf in CUSTOMER_KEYS and not o.aggregated:
                        return True
        return False

    def _clause_origins(self, root: Scope) -> list[Origin]:
        """Origins of columns used outside SELECT lists (WHERE, JOIN, GROUP BY, HAVING, ORDER BY...).

        Projections are covered by output tracing; skipping them also skips the guard's scoped
        subqueries, which project every non-PII column whether or not the query uses it."""
        out: list[Origin] = []
        for scope in root.traverse():
            select = scope.expression
            projected = (
                {id(c) for p in select.selects for c in p.find_all(exp.Column)} if isinstance(select, exp.Select) else set()
            )
            for col in scope.columns:
                if id(col) not in projected:
                    out.extend(self._trace_column(scope, col))
        return out

    def _whole_row_references(self, root: Scope) -> set[str]:
        # After qualification a table alias used as a value becomes a TableColumn (a STRUCT of the row).
        found = {node.name for node in root.expression.find_all(exp.TableColumn)}
        for scope in root.traverse():
            for col in scope.columns:
                if not col.table and col.name in scope.sources and not isinstance(scope.sources[col.name], exp.Unnest):
                    source = scope.sources[col.name]
                    if isinstance(source, (exp.Table, Scope)):
                        found.add(col.name)
        return found

    @staticmethod
    def _own_columns(node: exp.Expression) -> list[exp.Column]:
        """Columns of this projection, excluding those inside nested (scalar) subqueries."""
        return [c for c in node.find_all(exp.Column) if not _inside_subquery(c, node)]

    @staticmethod
    def _scalar_subquery_scopes(scope: Scope, proj: exp.Expression) -> list[Scope]:
        subs = [s.this for s in proj.find_all(exp.Subquery)]
        return [child for child in scope.subquery_scopes if any(child.expression is s for s in subs)]


def _inside_subquery(col: exp.Column, root: exp.Expression) -> bool:
    node = col.parent
    while node is not None and node is not root:
        if isinstance(node, (exp.Subquery, exp.Select)):
            return True
        node = node.parent
    return False
