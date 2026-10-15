"""Customer attributes only in aggregates, over groups of at least 5 customers.

OpsFleet: city, state, country, age, gender and traffic source are fine, but only
in aggregates; customers may be referenced by user_id.

The rule reads the query as the model wrote it, before the guard swaps tables for
scoped subqueries, qualified against the safe schema (stars expanded, every column
tied to its table).

Free mode. Attributes exist only in `users` and `orders`. A query that never
reaches one runs unchanged: customer lists by user_id with purchase metrics are
allowed.

Attribute mode is an ALLOWLIST, not a list of known attacks: two red-team rounds
showed that arbitrary SQL has too many ways to pick one row out of an aggregate
(windows, rounding, row multiplication, padding, correlated subqueries...). A query
that uses an attribute must have this shape:

  [WITH steps]
  SELECT <dimensions>, <aggregates>
  FROM tables and steps, joined on their key relationships
  WHERE <filters>
  GROUP BY <dimensions>
  [HAVING ...] [ORDER BY ...] [LIMIT n] [UNION ALL another such SELECT]

- Dimensions: any expression of attribute, category, date and amount columns.
  Never an ID (customer, order, order line): those give one row per person.
- Joins: on key relationships (u.id = oi.user_id, o.order_id = oi.order_id,
  oi.product_id = p.id...); an inner join may add filters, an outer join may not.
- Filters: anything but IDs (an ID may only be tested IS NULL).
- Aggregates: COUNT(*), COUNT(DISTINCT any column), SUM / AVG / STDDEV of plain
  amounts (columns combined with + - *, scaled by a constant, or day-level date
  differences), MIN / MAX / one quantile of attributes only, and at most one
  condition per aggregate: COUNTIF(c), SUM(IF(c, x, 0)), COUNT(DISTINCT IF(c, id, NULL)).
  Window functions only over the finished aggregates (shares, ranks).
- Steps (CTEs or derived tables): rows (a filter that keeps user_id), a set of
  customers (SELECT user_id ... GROUP BY user_id [HAVING ...] [ORDER BY ... LIMIT n]),
  or an aggregation across customers that follows these same rules.
- No subqueries in expressions, UNNEST, PIVOT, recursion, ARRAY / STRUCT / JSON
  packing, unknown functions.

Then two checks are injected into the SQL that runs:
- every group needs at least 5 distinct customers from each customer table it
  reads (HAVING COUNT(DISTINCT id) >= 5): smaller groups are dropped;
- every aggregate needs at least 5 distinct customers who actually contribute to
  it (a non-zero, non-NULL value, inside its condition): otherwise that cell is
  NULL, so one person cannot dominate a total that 4 others pad with zeros.

What this does not stop: differencing or reconstruction across several exact
aggregates, especially combined with attribute-free queries. Production adds
BigQuery's aggregation threshold and differential privacy (docs/technical-design.md §4).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, ScopeType, build_scope

from ..knowledge import DataDictionary

MIN_GROUP_CUSTOMERS = 5

# The customer ID of each table whose rows belong to one customer.
CUSTOMER_ID_COLUMN = {"users": "id", "orders": "user_id", "order_items": "user_id"}
# Other IDs that point at one customer's row, by kind (only IDs of the same kind join).
OTHER_IDS = {
    ("orders", "order_id"): "order", ("order_items", "order_id"): "order",
    ("order_items", "id"): "line", ("order_items", "inventory_item_id"): "inventory",
}
ID_KINDS = frozenset({"key", "order", "line", "inventory"})

ALLOWED_AGGREGATES = (
    exp.Count, exp.CountIf, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.AnyValue,
    exp.Stddev, exp.StddevPop, exp.StddevSamp, exp.Variance, exp.VariancePop,
    exp.ApproxDistinct, exp.Corr, exp.CovarPop, exp.CovarSamp,
)
PACKING = (exp.ArrayAgg, exp.GroupConcat, exp.ArrayConcatAgg, exp.Array, exp.Struct, exp.JSONFormat, exp.Format)
# Scalar BigQuery functions sqlglot parses as Anonymous; any other unknown function is rejected.
KNOWN_SCALARS = frozenset({"DATE_BUCKET", "DATETIME_BUCKET", "TIMESTAMP_BUCKET"})
SUMMING = (exp.Sum, exp.Avg, exp.Stddev, exp.StddevPop, exp.StddevSamp, exp.Variance, exp.VariancePop)
SINGLE_VALUE = (exp.Min, exp.Max, exp.AnyValue, exp.ApproxQuantiles)
TWO_ARG = (exp.Corr, exp.CovarPop, exp.CovarSamp)
DIFFS = (exp.DateDiff, exp.TimestampDiff, exp.DatetimeDiff)
TRUNCATIONS = (exp.Date, exp.TsOrDsToDate, exp.DateTrunc, exp.TimestampTrunc, exp.DatetimeTrunc)
NOW = (exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentDatetime)
COARSE_UNITS = frozenset({"DAY", "WEEK", "ISOWEEK", "MONTH", "QUARTER", "YEAR", "ISOYEAR"})
NUMERIC_TYPES = {exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT, exp.DataType.Type.DECIMAL, exp.DataType.Type.BIGDECIMAL}
TIME_TYPES = {"TIMESTAMP", "DATE", "DATETIME"}
NUMBER_TYPES = {"INT64", "FLOAT64", "NUMERIC", "BIGNUMERIC"}

SHAPE_HELP = (
    "With customer attributes (city, state, country, age, gender, traffic_source), write one "
    "aggregation: SELECT attributes / categories / dates, then aggregates, FROM tables joined on their "
    "keys, WHERE filters, GROUP BY. Aggregates: COUNT(*), COUNT(DISTINCT user_id), SUM / AVG of amounts, "
    "MIN / MAX of attributes, one IF / COUNTIF condition per aggregate. Per-customer figures: divide in "
    "the SELECT, e.g. SUM(sale_price) / COUNT(DISTINCT user_id). A CTE may only filter rows (keeping "
    "user_id), pick a set of customers (SELECT user_id ... GROUP BY user_id HAVING ...), or aggregate."
)


class Rejected(Exception):
    def __init__(self, kind: str, message: str):
        self.kind, self.message = kind, message
        super().__init__(f"{kind}: {message}")


class _Untraced(Exception):
    pass


@dataclass
class Step:
    role: str  # "rows" | "segment" | "released"
    classes: dict[str, str]  # output name -> class
    linked: bool  # holds individual customers' rows

    def key(self) -> str | None:
        return next((n for n, c in self.classes.items() if c == "key"), None)


@dataclass
class RuleResult:
    tree: exp.Expression | None  # qualified tree with checks added; None = run the query as written
    notes: tuple[str, ...] = ()


class AggregateOnlyRule:
    def __init__(self, dictionary: DataDictionary, project: str, dataset: str,
                 projection: dict[str, list[str]] | None = None):
        self.project, self.dataset = project, dataset
        columns = projection or {t: tbl.safe_columns for t, tbl in dictionary.tables.items()}
        self.schema = {
            project: {dataset: {t: {c: dictionary.tables[t].columns[c].type for c in cols} for t, cols in columns.items()}}
        }
        self.attributes = frozenset(
            (t, c.name.lower()) for t, tbl in dictionary.tables.items() for c in tbl.columns.values() if c.aggregate_only
        )
        self.attribute_names = frozenset(c for _, c in self.attributes)
        self.attribute_tables = frozenset(t for t, _ in self.attributes)
        self.tables = frozenset(dictionary.tables)
        self.classes: dict[tuple[str, str], str] = {}
        for t, tbl in dictionary.tables.items():
            for c in tbl.columns.values():
                name, kind = c.name.lower(), (c.type or "").upper()
                if CUSTOMER_ID_COLUMN.get(t) == name:
                    cls = "key"
                elif (t, name) in OTHER_IDS:
                    cls = OTHER_IDS[(t, name)]
                elif c.aggregate_only:
                    cls = "attr"
                elif kind in TIME_TYPES:
                    cls = "time"
                elif name == "id" or name.endswith("_id"):
                    cls = "cat"  # product, distribution centre: not people
                elif kind in NUMBER_TYPES:
                    cls = "measure"
                else:
                    cls = "cat"
                self.classes[(t, name)] = cls

    # ------------------------------------------------------------------ entry point

    def check(self, tree: exp.Expression) -> RuleResult:
        """Raises Rejected, or returns the SQL tree to run (tree None: run the query unchanged)."""
        if not self._may_reach_attributes(tree):
            return RuleResult(None)
        work = tree.copy()
        for table in work.find_all(exp.Table):
            if table.name.lower() in self.tables:
                table.set("this", exp.to_identifier(table.name.lower()))
                table.set("db", exp.to_identifier(self.dataset))
                table.set("catalog", exp.to_identifier(self.project))
        try:
            work = qualify(work, schema=self.schema, dialect="bigquery", catalog=self.project, db=self.dataset,
                           validate_qualify_columns=False)
            root = build_scope(work)
        except (OptimizeError, SqlglotError) as e:
            raise Rejected("unverifiable", f"The query could not be checked for customer privacy ({e}). Simplify it.") from e
        if root is None:
            raise Rejected("unverifiable", "The query could not be checked for customer privacy. Simplify it.")
        return _Checker(self, root).run()

    def _may_reach_attributes(self, tree: exp.Expression) -> bool:
        """Cheap over-approximation: only users and orders hold attributes, and an attribute is
        reached by name, through a star or through a whole-row reference."""
        tables = {t.name.lower() for t in tree.find_all(exp.Table)}
        if not tables & self.attribute_tables:
            return False
        if any(i.name.lower() in self.attribute_names for i in tree.find_all(exp.Identifier)):
            return True
        if any(not isinstance(s.parent, exp.Count) for s in tree.find_all(exp.Star)):
            return True
        names = tables | {t.alias_or_name.lower() for t in tree.find_all(exp.Table)} | {
            c.alias_or_name.lower() for c in tree.find_all(exp.CTE)
        }
        return any(not c.table and c.name.lower() in names for c in tree.find_all(exp.Column))


class _Checker:
    def __init__(self, rule: AggregateOnlyRule, root: Scope):
        self.rule, self.root = rule, root
        self.steps: dict[int, Step] = {}
        self.problems: list[tuple[str, str]] = []
        self.releases: list[tuple[exp.Select, list[exp.Column], list[tuple[exp.AggFunc, exp.Expression]]]] = []
        self.leaf_memo: dict[tuple[int, str], frozenset] = {}

    def run(self) -> RuleResult:
        tree = self.root.expression
        whole_rows = sorted({n.name for n in tree.find_all(exp.TableColumn)})
        if whole_rows:
            raise Rejected("table_reference",
                           f"Whole-row references ({', '.join(whole_rows)}) are not allowed; select explicit columns.")
        if any(not isinstance(s.parent, exp.Count) for s in tree.find_all(exp.Star)):
            raise Rejected("unverifiable", "Select explicit columns instead of * so the query can be checked for customer privacy.")
        if not self._reaches_attributes():
            return RuleResult(None)  # no customer attribute after all: free mode

        self._check_constructs(tree)
        if not self.problems:
            try:
                for scope in self.root.traverse():  # inner scopes first: steps before their readers
                    self._check_scope(scope)
            except _Untraced:
                self._add("unverifiable", "Part of the query could not be traced to a table. Qualify every column "
                                          f"with its table alias. {SHAPE_HELP}")
        if self.problems:
            raise Rejected(self.problems[0][0], " ".join(dict.fromkeys(m for _, m in self.problems)))
        for select, keys, cells in self.releases:
            self._inject(select, keys, cells)
        note = (f"Privacy rule: groups with fewer than {MIN_GROUP_CUSTOMERS} distinct customers are left out of this "
                f"result, and a figure that fewer than {MIN_GROUP_CUSTOMERS} customers contribute to is blank (NULL). "
                "Say so if a segment or figure is missing.")
        injected = any(keys for _, keys, _ in self.releases)
        return RuleResult(tree, (note,) if injected else ())

    def _add(self, kind: str, message: str) -> None:
        self.problems.append((kind, message))

    # ------------------------------------------------------------------ does an attribute appear at all?

    def _reaches_attributes(self) -> bool:
        for scope in self.root.traverse():
            for col in scope.columns:
                try:
                    if self._leaves(scope, col) & self.rule.attributes:
                        return True
                except _Untraced:
                    return True  # cannot tell: check it as an attribute query (and fail closed there)
        return False

    def _leaves(self, scope: Scope, col: exp.Column) -> frozenset:
        source = _resolve(scope, col.table) if col.table else None
        if isinstance(source, exp.Table):
            return frozenset({(source.name.lower(), col.name.lower())})
        if isinstance(source, Scope):
            return self._output_leaves(source, col.name.lower())
        if not col.table and isinstance(scope.expression, exp.Select):  # an alias in GROUP BY / ORDER BY
            for proj in scope.expression.selects:
                if proj.alias_or_name.lower() == col.name.lower() and not _contains(proj, col):
                    return self._expr_leaves(scope, proj)
        raise _Untraced

    def _output_leaves(self, scope: Scope, name: str) -> frozenset:
        memo_key = (id(scope), name)
        if memo_key in self.leaf_memo:
            return self.leaf_memo[memo_key]
        self.leaf_memo[memo_key] = frozenset()  # recursion guard (recursive CTEs are rejected later)
        expr = scope.expression
        if isinstance(expr, exp.SetOperation):
            out = frozenset().union(*(self._branch_leaves(b, expr, name) for b in scope.set_operation_scopes))
        elif isinstance(expr, exp.Select):
            projs = [p for p in expr.selects if p.alias_or_name.lower() == name]
            if not projs:
                raise _Untraced
            out = self._expr_leaves(scope, projs[0])
        else:  # UNNEST and friends: everything they read
            out = frozenset().union(*(self._leaves(scope, c) for c in scope.columns))
        self.leaf_memo[memo_key] = out
        return out

    def _branch_leaves(self, branch: Scope, setop: exp.SetOperation, name: str) -> frozenset:
        names = [n.lower() for n in _first_select(setop).named_selects]
        if name not in names or not isinstance(branch.expression, exp.Select):
            return frozenset().union(*(self._leaves(branch, c) for c in branch.columns))
        selects = branch.expression.selects
        index = names.index(name)
        return self._expr_leaves(branch, selects[index]) if index < len(selects) else frozenset()

    def _expr_leaves(self, scope: Scope, node: exp.Expression) -> frozenset:
        out: set = set()
        for col in node.find_all(exp.Column):
            if _owner_select(col) is scope.expression:
                out |= self._leaves(scope, col)
        for sub in scope.subquery_scopes:
            if _contains(node, sub.expression):
                for c in sub.columns:
                    out |= self._leaves(sub, c)
        return frozenset(out)

    # ------------------------------------------------------------------ constructs banned outright

    def _check_constructs(self, tree: exp.Expression) -> None:
        for node in tree.find_all(exp.Func, exp.Select, exp.Pivot, exp.Filter):
            if isinstance(node, exp.Select):
                if node.args.get("kind"):
                    self._add("not_allowed", f"SELECT AS {node.args['kind']} is not allowed with customer attributes.")
                continue
            if isinstance(node, exp.Pivot):
                self._add("unverifiable", f"PIVOT / UNPIVOT cannot be checked for customer privacy. {SHAPE_HELP}")
                continue
            if isinstance(node, exp.Filter):
                self._add("not_allowed", "FILTER (WHERE ...) is not BigQuery syntax; use COUNTIF or IF inside the aggregate.")
                continue
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            if isinstance(node, PACKING):
                self._add("not_allowed",
                          f"{name} is not allowed in a query that uses customer attributes: it can pack many "
                          f"customers into one value. {SHAPE_HELP}")
            elif (isinstance(node, exp.AggFunc) and not isinstance(node, ALLOWED_AGGREGATES) and not _windowed(node)
                  and not _single_quantile(node)):
                self._add("not_allowed",
                          f"Aggregate {name} is not allowed with customer attributes. Use COUNT, COUNTIF, SUM, AVG, "
                          "MIN, MAX, STDDEV, VARIANCE, CORR, APPROX_COUNT_DISTINCT or one quantile, e.g. the "
                          "median APPROX_QUANTILES(x, 2)[OFFSET(1)].")
            elif isinstance(node, exp.Anonymous) and name not in KNOWN_SCALARS:
                self._add("not_allowed", f"Function {name} is not available in a query that uses customer attributes.")

    # ------------------------------------------------------------------ scopes

    def _check_scope(self, scope: Scope) -> None:
        kind = scope.scope_type
        if kind == ScopeType.SUBQUERY:
            self._add("subquery", "Subqueries inside expressions (scalar, IN, EXISTS, ARRAY) are not allowed with "
                                  f"customer attributes: join a CTE instead. {SHAPE_HELP}")
            return
        if kind == ScopeType.UDTF:
            self._add("not_allowed", f"UNNEST is not allowed with customer attributes. {SHAPE_HELP}")
            return
        expr = scope.expression
        if kind in (ScopeType.CTE, ScopeType.DERIVED_TABLE):
            if not isinstance(expr, exp.Select):
                self._add("not_allowed", "A CTE or derived table over customers cannot be a UNION or a recursive "
                                         f"query. {SHAPE_HELP}")
                self.steps[id(scope)] = Step("rows", {}, True)
                return
            self.steps[id(scope)] = self._check_step(scope, expr)
            return
        if kind == ScopeType.SET_OPERATION:  # a branch of the root's UNION
            if isinstance(expr, exp.Select):
                self._check_release(scope, expr)
            return
        if kind == ScopeType.ROOT:
            if isinstance(expr, exp.Select):
                self._check_release(scope, expr)
            elif not isinstance(expr, exp.SetOperation):
                raise _Untraced

    def _class(self, scope: Scope, col: exp.Column) -> str:
        if not col.table:
            raise _Untraced
        source = scope.sources.get(col.table)
        if isinstance(source, exp.Table):
            cls = self.rule.classes.get((source.name.lower(), col.name.lower()))
            if cls is None:
                raise _Untraced
            return cls
        if isinstance(source, Scope):
            step = self.steps.get(id(source))
            if step is None or col.name.lower() not in step.classes:
                raise _Untraced
            return step.classes[col.name.lower()]
        raise _Untraced

    def _linked(self, scope: Scope, alias: str) -> bool:
        source = scope.sources.get(alias)
        if isinstance(source, exp.Table):
            return source.name.lower() in CUSTOMER_ID_COLUMN
        step = self.steps.get(id(source)) if isinstance(source, Scope) else None
        return step is None or step.linked

    def _key_column(self, scope: Scope, alias: str) -> str | None:
        source = scope.sources.get(alias)
        if isinstance(source, exp.Table):
            return CUSTOMER_ID_COLUMN.get(source.name.lower())
        step = self.steps.get(id(source))
        return step.key() if step else None

    # ------------------------------------------------------------------ joins

    def _check_joins(self, scope: Scope, select: exp.Select, one_customer: bool) -> list[str]:
        """Joins on key relationships (inner joins may add filters). Returns the linked aliases.
        one_customer: every row must be one customer's (a step), so linked tables must be joined on IDs."""
        linked = [a for a in scope.selected_sources if self._linked(scope, a)]
        parent = {a: a for a in linked}

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for join in select.args.get("joins") or []:
            if join.args.get("using"):
                raise _Untraced
            outer = bool(join.args.get("side")) or (join.args.get("kind") or "").upper() == "FULL"
            for conjunct in _conjuncts(join.args.get("on")):
                relation = self._relationship(scope, conjunct)
                if relation:
                    a, b = relation
                    if a in parent and b in parent:
                        parent[find(a)] = find(b)
                    continue
                if outer:
                    self._add("join", "An outer join may only use key relationships in ON (e.g. "
                                      "LEFT JOIN order_items oi ON oi.user_id = u.id); put other conditions in WHERE.")
                else:
                    self._check_predicate(scope, conjunct, "join condition")
        where = select.args.get("where")
        for conjunct in _conjuncts(where.this if where else None):
            relation = self._relationship(scope, conjunct)
            if relation and relation[0] in parent and relation[1] in parent:
                parent[find(relation[0])] = find(relation[1])
        if one_customer and len({find(a) for a in linked}) > 1:
            self._add("join", "A CTE may combine customer tables only through their keys (user_id, order_id), so "
                              f"that each row is one customer's. {SHAPE_HELP}")
        return linked

    def _relationship(self, scope: Scope, cond: exp.Expression) -> tuple[str, str] | None:
        """`a.x = b.y` where x and y are IDs of the same kind (or a product key): returns (a, b)."""
        cond = _strip(cond)
        if not isinstance(cond, exp.EQ):
            return None
        left, right = _strip(cond.this), _strip(cond.expression)
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column) and left.table and right.table):
            return None
        lc, rc = self._class(scope, left), self._class(scope, right)
        if lc in ID_KINDS and lc == rc:
            return left.table, right.table
        if lc == rc == "cat" and {left.name.lower(), right.name.lower()} <= {"id", "product_id"}:
            return left.table, right.table  # order_items.product_id = products.id (not a customer link)
        return None

    # ------------------------------------------------------------------ expressions

    def _ids_in(self, scope: Scope, node: exp.Expression) -> list[exp.Column]:
        return [c for c in node.find_all(exp.Column) if self._class(scope, c) in ID_KINDS]

    @staticmethod
    def _scalar(node: exp.Expression) -> bool:
        return not node.find(exp.AggFunc, exp.Window, exp.Subquery, exp.Select)

    def _check_predicate(self, scope: Scope, node: exp.Expression, where: str) -> None:
        if not self._scalar(node):
            self._add("not_allowed", f"A {where} may not contain aggregates, windows or subqueries. {SHAPE_HELP}")
            return
        for col in self._ids_in(scope, node):
            parent = col.parent
            while isinstance(parent, exp.Paren):
                parent = parent.parent
            if not isinstance(parent, exp.Is):
                self._add("customer_id_use",
                          f"'{col.sql('bigquery')}' is a customer or order ID used in a {where}. With customer "
                          "attributes, IDs may only join tables or be counted; never filter on specific customers "
                          "or orders (only `IS NULL` is allowed).")

    def _dimension(self, scope: Scope, node: exp.Expression) -> bool:
        return self._scalar(node) and not self._ids_in(scope, node)

    def _amount(self, scope: Scope, node: exp.Expression, whole: bool = True) -> bool:
        """What SUM / AVG may add up: amounts combined with + - *, scaled by a constant, and day-level
        date differences. No conditions, no rounding, no constant offsets (x + 0.000001 would make every
        padding row count as a contributor): nothing that can pick a row. A lone constant is fine
        (SUM(IF(c, 1, 0)) counts)."""
        node = _strip(node)
        if _number(node):
            return whole
        if isinstance(node, exp.Column):
            return self._class(scope, node) in ("measure", "attr", "released")
        if isinstance(node, (exp.Add, exp.Sub)):
            return self._amount(scope, node.this, False) and self._amount(scope, node.expression, False)
        if isinstance(node, exp.Mul):
            parts = [_strip(node.this), _strip(node.expression)]
            amounts = [p for p in parts if not _number(p)]
            return bool(amounts) and all(self._amount(scope, p, False) for p in amounts)
        if isinstance(node, exp.Div):
            return _number(_strip(node.expression)) and self._amount(scope, node.this, False)
        if isinstance(node, exp.Neg):
            return self._amount(scope, node.this, whole)
        if isinstance(node, (exp.Cast, exp.TryCast)):
            return node.to.this in NUMERIC_TYPES and self._amount(scope, node.this, whole)
        if isinstance(node, DIFFS):
            unit = node.args.get("unit")
            unit_name = (unit.name if unit is not None else "DAY").upper()
            return unit_name in COARSE_UNITS and self._date_arg(scope, node.this) and self._date_arg(scope, node.expression)
        return False

    def _date_arg(self, scope: Scope, node: exp.Expression) -> bool:
        node = _strip(node)
        if isinstance(node, NOW):
            return True
        while isinstance(node, TRUNCATIONS):
            node = _strip(node.this)
        return isinstance(node, exp.Column) and self._class(scope, node) == "time"

    def _attributes_only(self, scope: Scope, node: exp.Expression) -> bool:
        return self._scalar(node) and all(self._class(scope, c) in ("attr", "released") for c in node.find_all(exp.Column))

    # ------------------------------------------------------------------ steps (CTEs, derived tables)

    def _check_step(self, scope: Scope, select: exp.Select) -> Step:
        if select.find(exp.Window):
            self._add("not_allowed", f"Window functions are allowed only over the final aggregates. {SHAPE_HELP}")
        aggregating = bool(select.args.get("group")) or bool(select.args.get("distinct")) or _has_plain_aggregate(select)
        if not aggregating:
            return self._check_rows_step(scope, select)
        keys = _group_keys(select)
        key_cols = [k for k in keys if isinstance(_strip(k), exp.Column) and self._class(scope, _strip(k)) == "key"]
        if key_cols:  # a set of customers
            self._check_joins(scope, select, one_customer=True)
            if select.args.get("where"):
                self._check_predicate(scope, select.args["where"].this, "filter")
            if len(keys) != 1 or len(select.selects) != 1 or _strip(select.selects[0]) != _strip(key_cols[0]):
                self._add("not_allowed",
                          "A per-customer CTE may only pick a set of customers: SELECT user_id ... GROUP BY user_id "
                          "[HAVING ...] [ORDER BY ... LIMIT n]. Per-customer figures cannot be carried into a query "
                          f"that uses customer attributes. {SHAPE_HELP}")
            for part in (select.args.get("having"), select.args.get("order")):
                if part is not None and part.find(exp.Subquery, exp.Select, exp.Window):
                    self._add("not_allowed", "Subqueries and windows are not allowed in a customer-set CTE.")
            return Step("segment", {select.selects[0].alias_or_name.lower(): "key"}, True)
        reads_customers = self._check_release(scope, select)
        # Aggregated across customers (and checked when it read customers' rows): no longer row-level.
        del reads_customers
        return Step("released", {p.alias_or_name.lower(): "released" for p in select.selects}, linked=False)

    def _check_rows_step(self, scope: Scope, select: exp.Select) -> Step:
        linked = self._check_joins(scope, select, one_customer=True)
        if select.args.get("where"):
            self._check_predicate(scope, select.args["where"].this, "filter")
        classes: dict[str, str] = {}
        for proj in select.selects:
            name, value = proj.alias_or_name.lower(), _strip(proj)
            if name in classes:
                raise _Untraced  # t.x would be ambiguous
            if isinstance(value, exp.Column):
                classes[name] = self._class(scope, value)
            elif not self._scalar(value):
                self._add("not_allowed", f"A CTE that filters rows may not compute aggregates, windows or subqueries. {SHAPE_HELP}")
            elif self._ids_in(scope, value):
                self._add("customer_id_use", f"An ID may only pass through a CTE unchanged, not inside `{_short(value)}`.")
            elif self._amount(scope, value):
                classes[name] = "measure"
            elif self._attributes_only(scope, value):
                classes[name] = "attr"
            else:
                classes[name] = "cat"  # usable to group or filter, never to add up
        return Step("rows", classes, linked=bool(linked))

    # ------------------------------------------------------------------ the aggregation (release point)

    def _check_release(self, scope: Scope, select: exp.Select) -> bool:
        """Returns True when the SELECT reads customers' rows (and so gets the checks)."""
        linked = self._check_joins(scope, select, one_customer=False)
        if select.args.get("where"):
            self._check_predicate(scope, select.args["where"].this, "filter")
        if not linked:
            for window in select.find_all(exp.Window):
                if self._ids_in(scope, window):
                    self._add("customer_id_use", "IDs cannot be used in window functions here.")
            return False
        aggregating = bool(select.args.get("group")) or bool(select.args.get("distinct")) or _has_plain_aggregate(select)
        if not aggregating:
            self._add("customer_attributes",
                      "This result lists individual customers' rows (customers, orders, items) and uses customer "
                      f"attributes. {SHAPE_HELP} List customers by ID only in a separate query that does not use "
                      "these attributes at all (not even as a filter).")
            return True
        keys = _group_keys(select)
        for key in keys:
            if not self._dimension(scope, key):
                self._add("customer_attributes",
                          f"GROUP BY `{_short(key)}` gives one row per customer or order and the query uses customer "
                          f"attributes. Group by attributes, categories or dates instead. {SHAPE_HELP}")
        cells: list[tuple[exp.AggFunc, exp.Expression]] = []
        aliases = {p.alias_or_name.lower() for p in select.selects}
        for proj in select.selects:
            self._check_metric(scope, proj.unalias(), keys, aliases, cells, output=proj.alias_or_name)
        for part in (select.args.get("having"), select.args.get("order"), select.args.get("qualify")):
            if part is not None:
                self._check_metric(scope, part, keys, aliases, cells)
        key_columns = []
        for alias in linked:
            name = self._key_column(scope, alias)
            if name is None:
                self._add("no_customer_id",
                          f"'{alias}' holds individual customers' rows but no user_id, so group sizes cannot be "
                          "checked. Keep user_id in its SELECT list.")
            else:
                key_columns.append(exp.column(name, table=alias, quoted=True))
        self.releases.append((select, key_columns, cells))
        return True

    def _check_metric(self, scope: Scope, node: exp.Expression, keys: list[exp.Expression], aliases: set[str],
                      cells: list, output: str | None = None) -> None:
        """Outside aggregates: only group keys, constants and arithmetic on finished aggregates."""
        if any(node == k for k in keys) and self._dimension(scope, node):
            return
        if isinstance(node, exp.AggFunc) and not _windowed(node):
            self._check_aggregate(scope, node, cells)
            return
        if isinstance(node, (exp.Subquery, exp.Select)):
            self._add("subquery", f"Subqueries are not allowed with customer attributes. {SHAPE_HELP}")
            return
        if isinstance(node, exp.Column):
            if not node.table and node.name.lower() in aliases:
                return  # ORDER BY / HAVING an output alias
            self._add("not_aggregated", f"`{output or node.sql('bigquery')}` shows row-level data. {SHAPE_HELP}")
            return
        for child in node.iter_expressions():
            self._check_metric(scope, child, keys, aliases, cells, output)

    def _check_aggregate(self, scope: Scope, agg: exp.AggFunc, cells: list) -> None:
        name = agg.sql_name()
        if isinstance(agg, exp.Count) and isinstance(agg.this, exp.Star):
            return
        cond, value = _conditional_parts(agg)
        if cond is not None:
            self._check_predicate(scope, cond, f"condition inside {name}")
        if isinstance(agg, (exp.Count, exp.ApproxDistinct)):
            if not isinstance(value, (exp.Literal, exp.Column)):
                self._add("not_allowed", f"{name} may count a column or a single IF(condition, column, NULL), not "
                                         f"`{_short(value)}`. {SHAPE_HELP}")
                return
            if cond is None and isinstance(value, exp.Column) and self._class(scope, value) == "key":
                return  # the group's own customer count, already checked by HAVING
            contributes = exp.Not(this=exp.Is(this=value.copy(), expression=exp.Null()))
        elif isinstance(agg, exp.CountIf):
            contributes = None
        elif isinstance(agg, SUMMING):
            if value is None or not self._amount(scope, value):
                self._add("not_allowed",
                          f"{name}(`{_short(agg.this)}`) is not a plain amount. With customer attributes, SUM / AVG "
                          "may add up prices, costs, quantities, ages and day-level date differences (combined with "
                          "+ - *), with at most one IF(condition, amount, 0) around them; filter in WHERE.")
                return
            contributes = exp.NEQ(this=value.copy(), expression=exp.Literal.number(0))
        elif isinstance(agg, SINGLE_VALUE) or _single_quantile(agg):
            if value is None or not self._attributes_only(scope, value):
                self._add("not_allowed",
                          f"{name} of `{_short(agg.this)}` returns one row's exact value, which can be looked up to "
                          "find whose row it is. With customer attributes, use MIN / MAX / quantiles only on the "
                          "attributes themselves; use AVG or SUM for prices, dates and counts.")
                return
            contributes = exp.Not(this=exp.Is(this=value.copy(), expression=exp.Null()))
        elif isinstance(agg, TWO_ARG):
            other = agg.args.get("expression")
            if cond is not None or other is None or not (self._amount(scope, agg.this) and self._amount(scope, other)):
                self._add("not_allowed", f"{name} may only relate two plain amounts, without a condition.")
                return
            contributes = exp.and_(exp.Not(this=exp.Is(this=agg.this.copy(), expression=exp.Null())),
                                   exp.Not(this=exp.Is(this=other.copy(), expression=exp.Null())))
        else:
            self._add("not_allowed", f"Aggregate {name} is not allowed with customer attributes.")
            return
        parts = [p for p in (cond.copy() if cond is not None else None, contributes) if p is not None]
        cells.append((agg, exp.and_(*parts) if parts else exp.true()))

    # ------------------------------------------------------------------ injected checks

    def _inject(self, select: exp.Select, keys: list[exp.Column], cells: list) -> None:
        if not keys:
            return
        for agg, contributes in cells:
            checks = [
                exp.GTE(this=exp.Count(this=exp.Distinct(expressions=[
                    exp.If(this=contributes.copy(), true=key.copy(), false=exp.Null())])),
                    expression=exp.Literal.number(MIN_GROUP_CUSTOMERS))
                for key in keys
            ]
            agg.replace(exp.If(this=exp.and_(*checks), true=agg.copy(), false=exp.Null()))
        if select.args.get("distinct") and not select.args.get("group") and not _has_plain_aggregate(select):
            select.set("distinct", None)
            select.set("group", exp.Group(expressions=[exp.Literal.number(i + 1) for i in range(len(select.selects))]))
        for key in keys:
            count = exp.Count(this=exp.Distinct(expressions=[key.copy()]))
            select.having(exp.GTE(this=count, expression=exp.Literal.number(MIN_GROUP_CUSTOMERS)), copy=False)


# --------------------------------------------------------------------------- helpers


def _resolve(scope: Scope | None, name: str):
    while scope is not None:
        if name in scope.sources:
            return scope.sources[name]
        scope = scope.parent
    return None


def _strip(node: exp.Expression | None) -> exp.Expression | None:
    while isinstance(node, (exp.Paren, exp.Alias)):
        node = node.this
    return node


def _group_keys(select: exp.Select) -> list[exp.Expression]:
    keys = [p.unalias() for p in select.selects] if select.args.get("distinct") else []
    group = select.args.get("group")
    if group is None:
        return keys
    if group.args.get("all"):
        keys += [p.unalias() for p in select.selects if not _has_plain_aggregate_in(p, select)]
    for g in group.expressions:
        if isinstance(g, exp.Literal) and g.is_int and 0 < int(g.name) <= len(select.selects):
            keys.append(select.selects[int(g.name) - 1].unalias())
        elif isinstance(g, exp.Column) and not g.table:
            match = [p.unalias() for p in select.selects if p.alias_or_name.lower() == g.name.lower()]
            keys.append(match[0] if match else g)
        else:
            keys.append(g)
    for name in ("rollup", "cube", "grouping_sets"):
        for item in group.args.get(name) or []:
            keys += list(item.find_all(exp.Column))
    return keys


def _conditional_parts(agg: exp.AggFunc) -> tuple[exp.Expression | None, exp.Expression | None]:
    """(condition, value): COUNTIF(c) -> (c, None); SUM(IF(c, x, 0)) -> (c, x);
    COUNT(DISTINCT IF(c, id, NULL)) -> (c, id); SUM(x) -> (None, x)."""
    if isinstance(agg, exp.CountIf):
        return agg.this, None
    first = agg.this
    inner = first.expressions[0] if isinstance(first, exp.Distinct) and len(first.expressions) == 1 else first
    inner = _strip(inner)
    if isinstance(inner, exp.If) and _blank(inner.args.get("false")):
        return inner.this, _strip(inner.args.get("true"))
    if (isinstance(inner, exp.Case) and not inner.this and len(inner.args.get("ifs") or []) == 1
            and _blank(inner.args.get("default"))):
        return inner.args["ifs"][0].this, _strip(inner.args["ifs"][0].args.get("true"))
    return None, inner


def _number(node: exp.Expression | None) -> bool:
    while isinstance(node, exp.Neg):
        node = node.this
    return isinstance(node, exp.Literal) and node.is_number


def _blank(e: exp.Expression | None) -> bool:
    return e is None or isinstance(e, exp.Null) or (isinstance(e, exp.Literal) and e.is_number and float(e.name) == 0)


def _windowed(agg: exp.Expression) -> bool:
    node = agg
    while isinstance(node.parent, (exp.IgnoreNulls, exp.RespectNulls)):
        node = node.parent
    return isinstance(node.parent, exp.Window) and node.parent.this is node


def _single_quantile(agg: exp.Expression) -> bool:
    """APPROX_QUANTILES(x, n)[OFFSET(k)]: one order statistic, like MIN or MAX (the whole array is not)."""
    parent = agg.parent
    return (
        isinstance(agg, exp.ApproxQuantiles)
        and isinstance(parent, exp.Bracket)
        and parent.this is agg
        and len(parent.expressions) == 1
        and isinstance(parent.expressions[0], exp.Literal)
    )


def _has_plain_aggregate(select: exp.Select) -> bool:
    parts = [*select.selects, select.args.get("having"), select.args.get("order")]
    return any(part is not None and _has_plain_aggregate_in(part, select) for part in parts)


def _has_plain_aggregate_in(node: exp.Expression, select: exp.Select) -> bool:
    return any(not _windowed(a) and _owner_select(a) is select for a in node.find_all(exp.AggFunc))


def _owner_select(node: exp.Expression) -> exp.Expression | None:
    parent = node.parent
    while parent is not None and not isinstance(parent, (exp.Select, exp.SetOperation)):
        parent = parent.parent
    return parent


def _contains(root: exp.Expression, node: exp.Expression | None) -> bool:
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False


def _first_select(expr: exp.Expression) -> exp.Select:
    while isinstance(expr, (exp.SetOperation, exp.Subquery)):
        expr = expr.this
    return expr


def _conjuncts(cond: exp.Expression | None) -> list[exp.Expression]:
    if cond is None:
        return []
    while isinstance(cond, exp.Paren):
        cond = cond.this
    if isinstance(cond, exp.And):
        return _conjuncts(cond.this) + _conjuncts(cond.expression)
    return [cond]


def _short(node: exp.Expression | None, limit: int = 80) -> str:
    text = node.sql("bigquery") if node is not None else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
