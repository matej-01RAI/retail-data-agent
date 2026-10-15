"""Customer attributes only in aggregates, over groups of at least 5 customers.

OpsFleet: city, state, country, age, gender and traffic source are fine, but only
in aggregates; customers may be referenced by user_id.

The rule reads the query as the model wrote it, before the guard swaps tables for
scoped subqueries. It qualifies it against the safe schema, so stars expand and
every column has a source, and follows each column through CTEs, derived tables,
set operations, UNNEST and subqueries (sqlglot scopes). It fails closed: anything
it cannot trace or does not recognise, in a query that uses customer attributes,
is rejected.

Free mode. Customer attributes exist only in `users` and `orders`. A query that
never reaches one runs unchanged: customer lists by user_id with purchase metrics
are allowed.

Attribute mode, when any customer attribute is used anywhere (output, filter, join,
grouping, ordering):

A1  No construct that can pack many customers or a whole record into one value:
    ARRAY_AGG, STRING_AGG, ARRAY(...), [...], STRUCT, TO_JSON(_STRING), FORMAT,
    SELECT AS STRUCT / VALUE, aggregates outside a small allowlist, functions
    sqlglot does not recognise. Whole-row references are rejected in every mode.
A2  Customer IDs only join (a.id = b.user_id, id IN (SELECT user_id ...)), are
    counted (COUNT, COUNT(DISTINCT), APPROX_COUNT_DISTINCT), group or pass through
    a per-customer subquery. Never compared with a value, computed with or listed.
A3  A per-customer subquery is used as a table (FROM, JOIN, CTE) or on the right of
    `customer_id IN (...)`, never as a scalar or EXISTS subquery: only a table can
    be counted.
A4  The result leaves only through aggregation across customers. A SELECT that
    aggregates without grouping by a customer ID is a release point; the final
    output may not be one row per customer (or per order, or per anything finer).
A5  Each release point gets HAVING COUNT(DISTINCT <customer id>) >= 5 for every
    source of customer rows it reads, injected into the SQL that runs. A derived
    table is counted once per set of customers it carries (tables joined on
    customer ID by an inner join or WHERE are one set); a set with no customer ID
    column to count is rejected. Smaller groups are dropped.

What this does not stop: differencing two large aggregates, or groups chosen to
isolate someone by other attributes. Production adds BigQuery's aggregation
threshold or differential privacy (docs/technical-design.md §4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, ScopeType, build_scope

from ..knowledge import DataDictionary

MIN_GROUP_CUSTOMERS = 5

# The customer ID of each table whose rows belong to one customer.
CUSTOMER_ID_COLUMN = {"users": "id", "orders": "user_id", "order_items": "user_id"}

ALLOWED_AGGREGATES = (
    exp.Count, exp.CountIf, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.AnyValue,
    exp.Stddev, exp.StddevPop, exp.StddevSamp, exp.Variance, exp.VariancePop,
    exp.ApproxDistinct, exp.Corr, exp.CovarPop, exp.CovarSamp, exp.LogicalAnd, exp.LogicalOr,
)
PACKING = (exp.ArrayAgg, exp.GroupConcat, exp.ArrayConcatAgg, exp.Array, exp.Struct, exp.JSONFormat, exp.Format)
# Scalar BigQuery functions sqlglot parses as Anonymous; any other unknown function is rejected.
KNOWN_SCALARS = frozenset({"DATE_BUCKET", "DATETIME_BUCKET", "TIMESTAMP_BUCKET"})

ATTRIBUTES_HELP = (
    "Customer attributes (city, state, country, age, gender, traffic_source) may only be used in "
    "aggregates across customers: GROUP BY the attribute (or an age band) and aggregate with "
    "COUNT(DISTINCT user_id), SUM, AVG..."
)


class Rejected(Exception):
    def __init__(self, kind: str, message: str):
        self.kind, self.message = kind, message
        super().__init__(f"{kind}: {message}")


class _Untraced(Exception):
    pass


@dataclass(frozen=True)
class Flow:
    """What reaches a value at customer level, not yet released by an aggregate."""

    attrs: frozenset[str] = frozenset()  # customer attribute columns
    key: bool = False  # a customer ID, possibly inside an expression
    pure_key: bool = False  # the value IS a customer ID

    def __or__(self, other: Flow) -> Flow:
        return Flow(self.attrs | other.attrs, self.key or other.key, False)

    @property
    def tainted(self) -> bool:
        return bool(self.attrs) or self.key


EMPTY = Flow()


@dataclass
class ScopeInfo:
    outputs: dict[str, Flow]  # in order; names lower-cased
    linked: bool = False  # rows are individual customers' data
    release: bool = False  # aggregates across customers
    linked_sources: list[str] = field(default_factory=list)
    # Linked scopes: one entry per set of customers the rows carry, naming the output
    # columns that hold that set's customer ID. None: some set has no ID column.
    id_groups: list[list[str]] | None = None


@dataclass
class RuleResult:
    tree: exp.Expression | None  # qualified tree with HAVING added; None = run the query as written
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
        return _Analysis(self, root).run()

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


class _Analysis:
    def __init__(self, rule: AggregateOnlyRule, root: Scope):
        self.rule, self.root = rule, root
        self.infos: dict[int, ScopeInfo] = {}
        self.in_progress: set[int] = set()
        self.problems: list[tuple[str, str]] = []

    def run(self) -> RuleResult:
        tree = self.root.expression
        whole_rows = sorted({n.name for n in tree.find_all(exp.TableColumn)})
        if whole_rows:
            raise Rejected("table_reference",
                           f"Whole-row references ({', '.join(whole_rows)}) are not allowed; select explicit columns.")
        if any(not isinstance(s.parent, exp.Count) for s in tree.find_all(exp.Star)):
            raise Rejected("unverifiable", "Select explicit columns instead of * so the query can be checked for customer privacy.")

        # traverse() yields inner scopes first. A correlated column is also listed by the outer scope;
        # it belongs to the innermost one.
        scopes = list(self.root.traverse())
        columns: dict[int, tuple[Scope, exp.Column]] = {}
        for scope in scopes:
            for col in scope.columns:
                columns.setdefault(id(col), (scope, col))
        flows: dict[int, Flow] = {}
        untraced: list[str] = []
        for scope, col in columns.values():
            try:
                flows[id(col)] = self._trace(scope, col)
            except _Untraced:
                untraced.append(col.sql("bigquery"))
        try:
            for scope in scopes:
                self._info(scope)
        except _Untraced:
            untraced.append("(a derived table)")
        if untraced:
            raise Rejected("unverifiable",
                           f"{', '.join(sorted(set(untraced)))} could not be traced to a table, so the query cannot be "
                           "checked for customer privacy. Qualify every column with its table alias and avoid "
                           "recursive CTEs and VALUES lists.")
        if not any(f.attrs for f in flows.values()):
            return RuleResult(None)  # no customer attribute after all: free mode

        self._check_constructs(tree)
        for scope, col in columns.values():
            if flows[id(col)].key and not self._key_use_allowed(scope, col):
                self._add("customer_id_use",
                          f"'{col.sql('bigquery')}' is a customer ID used in `{_context(col)}`. In a query that "
                          "uses customer attributes, customer IDs may only join tables, be counted "
                          "(COUNT(DISTINCT ...)) or group a per-customer subquery; never filter on, compute with "
                          "or list specific customers.")
        for scope in scopes:
            if scope.scope_type == ScopeType.SUBQUERY and self._info(scope).linked and not self._semi_join(scope):
                self._add("subquery",
                          "A subquery over individual customers' rows is used as a value or EXISTS test. JOIN it as "
                          "a table, or use `user_id IN (SELECT user_id ...)`, so group sizes can be counted.")
        self._check_output(self.root)
        injected = 0 if self.problems else self._inject(scopes)
        if self.problems:
            raise Rejected(self.problems[0][0], " ".join(dict.fromkeys(m for _, m in self.problems)))
        note = (f"Privacy rule: groups with fewer than {MIN_GROUP_CUSTOMERS} distinct customers are left out of this "
                "result. Say so if a segment is missing.")
        return RuleResult(tree, (note,) if injected else ())

    def _add(self, kind: str, message: str) -> None:
        self.problems.append((kind, message))

    # ------------------------------------------------------------------ A1 constructs

    def _check_constructs(self, tree: exp.Expression) -> None:
        for node in tree.find_all(exp.Func, exp.Select):
            if isinstance(node, exp.Select):
                if node.args.get("kind"):
                    self._add("not_allowed", f"SELECT AS {node.args['kind']} is not allowed with customer attributes.")
                continue
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            if isinstance(node, PACKING):
                self._add("not_allowed",
                          f"{name} is not allowed in a query that uses customer attributes: it can pack many "
                          f"customers into one value. {ATTRIBUTES_HELP}")
            elif isinstance(node, exp.AggFunc) and not isinstance(node, ALLOWED_AGGREGATES) and not _windowed(node):
                self._add("not_allowed",
                          f"Aggregate {name} is not allowed with customer attributes. Use COUNT, COUNTIF, SUM, AVG, "
                          "MIN, MAX, STDDEV, VARIANCE, CORR or APPROX_COUNT_DISTINCT.")
            elif isinstance(node, exp.Anonymous) and name not in KNOWN_SCALARS:
                self._add("not_allowed", f"Function {name} is not available in a query that uses customer attributes.")

    # ------------------------------------------------------------------ tracing

    def _trace(self, scope: Scope, col: exp.Column) -> Flow:
        name = col.name.lower()
        if not col.table:
            return self._alias_flow(scope, col)
        source = _resolve(scope, col.table)
        if isinstance(source, exp.Table):
            table = source.name.lower()
            if table not in self.rule.tables:  # a recursive CTE reading itself
                raise _Untraced
            is_key = CUSTOMER_ID_COLUMN.get(table) == name
            return Flow(frozenset({name}) if (table, name) in self.rule.attributes else frozenset(), is_key, is_key)
        if isinstance(source, Scope):
            outputs = self._info(source).outputs
            if source.scope_type == ScopeType.UDTF:
                return next(iter(outputs.values()), EMPTY)
            if name in outputs:
                return outputs[name]
        raise _Untraced

    def _alias_flow(self, scope: Scope, col: exp.Column) -> Flow:
        """A bare name: a SELECT alias in GROUP BY / HAVING / ORDER BY / QUALIFY. Anywhere else it is untraced."""
        clause = col.parent
        while clause is not None and not isinstance(clause, (exp.Group, exp.Having, exp.Order, exp.Qualify, exp.Select, exp.SetOperation)):
            clause = clause.parent
        if not isinstance(clause, (exp.Group, exp.Having, exp.Order, exp.Qualify)):
            raise _Untraced
        name, expr = col.name.lower(), scope.expression
        if isinstance(expr, exp.SetOperation):
            outputs = self._info(scope).outputs
            if name in outputs:
                return outputs[name]
        elif isinstance(expr, exp.Select):
            for proj in expr.selects:
                if proj.alias_or_name.lower() == name:
                    return self._expression_flow(scope, proj)
        raise _Untraced

    def _expression_flow(self, scope: Scope, node: exp.Expression) -> Flow:
        inner = node.unalias() if isinstance(node, exp.Alias) else node
        if isinstance(inner, exp.Column):
            return self._trace(scope, inner)
        flow = EMPTY
        for col in _own_columns(node):
            flow = flow | self._trace(scope, col)
        for sub in _subquery_scopes_in(scope, node):
            for out in self._info(sub).outputs.values():
                flow = flow | out
        return flow

    def _info(self, scope: Scope) -> ScopeInfo:
        cached = self.infos.get(id(scope))
        if cached is not None:
            return cached
        if id(scope) in self.in_progress:  # a recursive CTE
            raise _Untraced
        self.in_progress.add(id(scope))
        try:
            expr = scope.expression
            if isinstance(expr, exp.SetOperation):
                info = self._set_operation_info(scope, expr)
            elif isinstance(expr, exp.Select):
                info = self._select_info(scope, expr)
            elif scope.scope_type == ScopeType.UDTF:
                flow = EMPTY
                for col in scope.columns:
                    flow = flow | self._trace(scope, col)
                info = ScopeInfo({"*": flow}, linked=flow.tainted)
            else:
                raise _Untraced
        finally:
            self.in_progress.discard(id(scope))
        self.infos[id(scope)] = info
        return info

    def _set_operation_info(self, scope: Scope, expr: exp.SetOperation) -> ScopeInfo:
        branches = [self._info(b) for b in scope.set_operation_scopes]
        names = [n.lower() for n in _first_select(expr).named_selects]
        outputs = {}
        for i, name in enumerate(names):
            flows = [list(b.outputs.values())[i] for b in branches if i < len(b.outputs)]
            merged = EMPTY
            for f in flows:
                merged = merged | f
            pure = len(flows) == len(branches) and all(f.pure_key for f in flows)
            outputs[name] = Flow(merged.attrs, merged.key or pure, pure)
        linked = any(b.linked for b in branches)
        # Countable when every branch carries one set of customers, with its ID at the same position.
        id_groups = None
        if linked and all(b.id_groups is not None and len(b.id_groups) == 1 for b in branches):
            for i, name in enumerate(names):
                if all(i < len(b.outputs) and list(b.outputs)[i] in b.id_groups[0] for b in branches):
                    id_groups = [[name]]
                    break
        return ScopeInfo(outputs, linked=linked, id_groups=id_groups)

    def _select_info(self, scope: Scope, select: exp.Select) -> ScopeInfo:
        linked_sources = [name for name, (_, source) in scope.selected_sources.items() if self._source_linked(source)]
        group_keys = list(select.selects) if select.args.get("distinct") else []
        group = select.args.get("group")
        for g in group.expressions if group else []:
            if isinstance(g, exp.Literal) and g.is_int and 0 < int(g.name) <= len(select.selects):
                group_keys.append(select.selects[int(g.name) - 1])
            else:
                group_keys.append(g)
        aggregating = bool(group_keys) or any(
            not _windowed(agg) and not _inside_subquery(agg, select)
            for part in (*select.selects, select.args.get("having"), select.args.get("order"))
            if part is not None
            for agg in part.find_all(exp.AggFunc)
        )
        by_customer = any(self._expression_flow(scope, k).pure_key for k in group_keys)
        release = aggregating and not by_customer
        outputs = {p.alias_or_name.lower(): EMPTY if release else self._expression_flow(scope, p) for p in select.selects}
        linked = not release and bool(linked_sources)
        info = ScopeInfo(outputs, linked=linked, release=release, linked_sources=linked_sources)
        if linked:
            info.id_groups = self._id_groups(scope, select, linked_sources)
        return info

    def _id_groups(self, scope: Scope, select: exp.Select, linked_sources: list[str]) -> list[list[str]] | None:
        """Which output columns count the customers of each set of linked sources joined on customer ID."""
        node_of: dict[tuple[str, str], tuple[str, int]] = {}  # (source alias, column) -> (source alias, set index)
        parent: dict[tuple[str, int], tuple[str, int]] = {}
        for alias in linked_sources:
            _, source = scope.selected_sources[alias]
            groups = [[CUSTOMER_ID_COLUMN[source.name.lower()]]] if isinstance(source, exp.Table) else self._info(source).id_groups
            if groups is None:
                return None
            for i, columns in enumerate(groups):
                parent[(alias, i)] = (alias, i)
                for c in columns:
                    node_of[(alias, c)] = (alias, i)

        def find(n):
            while parent[n] != n:
                parent[n] = parent[parent[n]]
                n = parent[n]
            return n

        def node(e: exp.Expression):
            while isinstance(e, exp.Paren):
                e = e.this
            if isinstance(e, exp.Column) and e.table:
                return node_of.get((e.table, e.name.lower()))
            return None

        # Equalities that hold on every row: WHERE conjuncts and the ON of inner joins.
        conditions = [select.args.get("where")] + [
            j.args.get("on") for j in select.args.get("joins") or [] if not j.args.get("side") and j.args.get("kind") != "CROSS"
        ]
        for cond in conditions:
            for conjunct in _conjuncts(cond.this if isinstance(cond, exp.Where) else cond):
                if isinstance(conjunct, exp.EQ):
                    a, b = node(conjunct.this), node(conjunct.expression)
                    if a and b:
                        parent[find(a)] = find(b)

        out: dict[tuple[str, int], list[str]] = {find(n): [] for n in parent}
        for proj in select.selects:
            n = node(proj.unalias() if isinstance(proj, exp.Alias) else proj)
            if n:
                out[find(n)].append(proj.alias_or_name.lower())
        return None if any(not cols for cols in out.values()) else list(out.values())

    def _source_linked(self, source: exp.Table | Scope) -> bool:
        if isinstance(source, exp.Table):
            return source.name.lower() in CUSTOMER_ID_COLUMN
        return self._info(source).linked

    # ------------------------------------------------------------------ A2 / A3

    def _key_use_allowed(self, scope: Scope, col: exp.Column) -> bool:
        node, parent = col, col.parent
        while isinstance(parent, exp.Paren):
            node, parent = parent, parent.parent
        if isinstance(parent, exp.Alias) and isinstance(parent.parent, exp.Select) and parent in parent.parent.selects:
            return True
        if isinstance(parent, exp.Select) and node in parent.selects:
            return True
        if isinstance(parent, (exp.Group, exp.Window, exp.Is)):
            return True
        if isinstance(parent, exp.Ordered) and isinstance(parent.parent, (exp.Order, exp.Window)):
            return True
        if isinstance(parent, (exp.EQ, exp.NEQ)):
            other = parent.expression if parent.this is node else parent.this
            while isinstance(other, exp.Paren):
                other = other.this
            try:
                return isinstance(other, exp.Column) and self._trace(scope, other).pure_key
            except _Untraced:
                return False
        if isinstance(parent, exp.In) and parent.this is node and parent.args.get("query") is not None:
            sub = next((s for s in scope.subquery_scopes if s.expression is parent.args["query"].this), None)
            return sub is not None and self._one_customer_id(sub)
        # The value of IF / CASE directly inside a count: COUNT(DISTINCT IF(cond, user_id, NULL)).
        while isinstance(parent, (exp.If, exp.Case, exp.Paren)) and _is_value_branch(parent, node):
            node, parent = parent, parent.parent
        if isinstance(parent, exp.Distinct) and isinstance(parent.parent, exp.Count):
            return True
        return isinstance(parent, (exp.Count, exp.ApproxDistinct)) and parent.this is node

    def _one_customer_id(self, sub: Scope) -> bool:
        """The subquery returns one column: the ID of the one set of customers it reads."""
        info = self._info(sub)
        return (
            info.linked
            and len(info.outputs) == 1
            and info.id_groups is not None
            and len(info.id_groups) == 1
            and next(iter(info.outputs)) in info.id_groups[0]
        )

    def _semi_join(self, scope: Scope) -> bool:
        """`customer_id IN (SELECT customer_id ...)`."""
        wrapper = scope.expression.parent
        return (
            isinstance(wrapper, exp.Subquery)
            and isinstance(wrapper.parent, exp.In)
            and wrapper.parent.args.get("query") is wrapper
            and self._one_customer_id(scope)
        )

    # ------------------------------------------------------------------ A4 / A5

    def _check_output(self, scope: Scope) -> None:
        if isinstance(scope.expression, exp.SetOperation):
            for branch in scope.set_operation_scopes:
                self._check_output(branch)
            return
        info = self._info(scope)
        keys = sorted(n for n, f in info.outputs.items() if f.key)
        attrs = sorted({a for f in info.outputs.values() for a in f.attrs})
        if keys:
            self._add("customer_attributes",
                      f"This result has one row per customer ({', '.join(keys)}) and uses customer attributes. "
                      f"{ATTRIBUTES_HELP} List customers by ID only in a separate query that does not use these "
                      "attributes at all (not even as a filter).")
        elif attrs:
            self._add("not_aggregated", f"Output column(s) show {', '.join(attrs)} row by row. {ATTRIBUTES_HELP}")
        elif info.linked:
            self._add("customer_attributes",
                      "This result lists individual customers' rows (orders, items...) selected or ordered by "
                      f"customer attributes. {ATTRIBUTES_HELP}")

    def _inject(self, scopes: list[Scope]) -> int:
        """HAVING COUNT(DISTINCT id) >= k for every set of customers each release point reads."""
        injected = 0
        for scope in scopes:
            info = self._info(scope)
            if not info.release or not info.linked_sources:
                continue
            select = scope.expression
            ids: list[exp.Column] = []
            for alias in info.linked_sources:
                _, source = scope.selected_sources[alias]
                if isinstance(source, exp.Table):
                    groups = [[CUSTOMER_ID_COLUMN[source.name.lower()]]]
                else:
                    groups = self._info(source).id_groups
                if groups is None:
                    self._add("no_customer_id",
                              f"'{alias}' holds individual customers' rows but not the customer ID of every table "
                              "it reads, so group sizes cannot be checked. Add user_id to its SELECT list (and GROUP "
                              "BY), and join its tables on customer ID.")
                    continue
                ids += [exp.column(cols[0], table=alias, quoted=True) for cols in groups]
            if self.problems:
                continue
            if select.args.get("distinct") and not select.args.get("group") and not _has_plain_aggregate(select):
                if any(p.find(exp.Window) for p in select.selects):
                    self._add("not_allowed", "Use GROUP BY instead of SELECT DISTINCT with window functions here.")
                    continue
                select.set("distinct", None)
                select.set("group", exp.Group(expressions=[exp.Literal.number(i + 1) for i in range(len(select.selects))]))
            for column in ids:
                count = exp.Count(this=exp.Distinct(expressions=[column]))
                select.having(exp.GTE(this=count, expression=exp.Literal.number(MIN_GROUP_CUSTOMERS)), copy=False)
            injected += 1
        return injected


# --------------------------------------------------------------------------- helpers


def _resolve(scope: Scope | None, name: str):
    while scope is not None:
        if name in scope.sources:
            return scope.sources[name]
        scope = scope.parent
    return None


def _windowed(agg: exp.Expression) -> bool:
    node = agg
    while isinstance(node.parent, (exp.IgnoreNulls, exp.RespectNulls)):
        node = node.parent
    return isinstance(node.parent, exp.Window) and node.parent.this is node


def _has_plain_aggregate(select: exp.Select) -> bool:
    return any(not _windowed(a) and not _inside_subquery(a, select) for p in select.selects for a in p.find_all(exp.AggFunc))


def _inside_subquery(node: exp.Expression, root: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None and parent is not root:
        if isinstance(parent, (exp.Subquery, exp.Select)):
            return True
        parent = parent.parent
    return False


def _own_columns(node: exp.Expression) -> list[exp.Column]:
    return [c for c in node.find_all(exp.Column) if not _inside_subquery(c, node)]


def _subquery_scopes_in(scope: Scope, node: exp.Expression) -> list[Scope]:
    inner = {id(s.this) for s in node.find_all(exp.Subquery)}
    return [s for s in scope.subquery_scopes if id(s.expression) in inner]


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


def _is_value_branch(parent: exp.Expression, child: exp.Expression) -> bool:
    if isinstance(parent, exp.Paren):
        return True
    if isinstance(parent, exp.If):
        return child is parent.args.get("true") or child is parent.args.get("false")
    if isinstance(parent, exp.Case):
        return child is parent.args.get("default") or any(child is i for i in parent.args.get("ifs") or [])
    return False


def _context(col: exp.Column) -> str:
    node = col
    while node.parent is not None and not isinstance(node.parent, (exp.Select, exp.Where, exp.Join, exp.Having, exp.Group)):
        node = node.parent
    text = node.sql("bigquery")
    return text if len(text) <= 120 else text[:119] + "…"
