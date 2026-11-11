"""SQL produced only from a constrained, independently checked query plan.

No SQL from the model or editor is ever executed. All preview parameters are bound.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from .models import QueryPlan, Table, Projection, Filter, Relation, split_key

OPERATORS = frozenset(("=", "!=", "<>", ">", ">=", "<", "<=", "LIKE", "NOT LIKE", "IN", "NOT IN", "BETWEEN", "IS NULL", "IS NOT NULL"))
AGGREGATES = frozenset(("", "COUNT", "COUNT_DISTINCT", "SUM", "AVG", "MIN", "MAX"))
JOINS = frozenset(("INNER", "LEFT", "RIGHT", "EXISTS"))
DIALECTS = frozenset(("sqlite", "mysql", "postgres", "tsql"))


@dataclass
class BuiltSQL:
    sql: str
    params: tuple


def quote_identifier(identifier: str, dialect: str) -> str:
    if not isinstance(identifier, str) or not identifier or "\x00" in identifier:
        raise ValueError("Invalid SQL identifier")
    if dialect == "mysql":
        return "`" + identifier.replace("`", "``") + "`"
    if dialect == "tsql":
        return "[" + identifier.replace("]", "]]" ) + "]"
    return '"' + identifier.replace('"', '""') + '"'


def quote_table(key: str, dialect: str) -> str:
    parts = split_key(key)
    if dialect == "sqlite":
        return quote_identifier(parts[-1], dialect)
    # Catalog-qualified names are supported for SQL Server only.  MySQL's
    # two-part name is database.table; PostgreSQL's is schema.table.
    if len(parts) == 3 and dialect != "tsql":
        raise ValueError("Three-part catalog-qualified SQL is only supported for SQL Server")
    return ".".join(quote_identifier(part, dialect) for part in parts)


def literal(value: object, dialect: str) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return str(value)
    s = str(value).replace("'", "''")
    if dialect == "mysql":
        s = s.replace("\\", "\\\\")
    if dialect == "tsql":
        return "N'" + s + "'"
    return "'" + s + "'"


def numeric_value(value: str, dtype: str):
    dt = dtype.lower()
    if any(t in dt for t in ("int", "decimal", "numeric", "float", "double", "real", "number", "money")):
        try:
            d = Decimal(str(value).strip())
            if not d.is_finite():
                raise ValueError("Non-finite numeric value")
            return int(d) if d == d.to_integral_value() else d
        except (InvalidOperation, ValueError):
            raise ValueError(f"Invalid numeric filter: {value}")
    return str(value)


def validate_plan(plan: QueryPlan, tables: dict[str, Table]) -> None:
    if plan.dialect not in DIALECTS:
        raise ValueError("Unsupported SQL dialect")
    if not plan.base or plan.base not in tables:
        raise ValueError("Select a source table before generating SQL")
    node_keys = [n.key for n in plan.nodes]
    if len(node_keys) != len(set(node_keys)) or not node_keys or plan.base not in node_keys:
        raise ValueError("Invalid or duplicate table node")
    if len(plan.nodes) > 25 or len(plan.joins) > 35 or len(plan.projections) > 90 or len(plan.filters) > 35:
        raise ValueError("Query exceeds configured safety limits")
    aliases = [n.alias for n in plan.nodes]
    if len(aliases) != len(set(aliases)) or any(not a.isascii() or not a.replace("_", "").isalnum() or not a[0].isalpha() for a in aliases):
        raise ValueError("Invalid table aliases")
    for key in node_keys:
        if key not in tables:
            raise ValueError("Unknown table in workspace")
    active = {plan.base}
    for j in plan.joins:
        if j.kind not in JOINS or j.left not in node_keys or j.right not in node_keys or j.left == j.right:
            raise ValueError("Invalid JOIN relationship")
        if not tables[j.left].has_column(j.left_column) or not tables[j.right].has_column(j.right_column):
            raise ValueError("JOIN references a non-existent column")
        if len(j.extras) > 8:
            raise ValueError("Too many extra JOIN conditions")
        for pair in j.extras:
            if len(pair) != 2 or not tables[j.left].has_column(pair[0]) or not tables[j.right].has_column(pair[1]):
                raise ValueError("Additional JOIN condition references an invalid column")
        if j.kind == "EXISTS":
            if j.right not in active and j.left not in active:
                raise ValueError("EXISTS must link to an active table")
        else:
            if j.left in active and j.right not in active:
                active.add(j.right)
            elif j.right in active and j.left not in active:
                # SQL rendering can reverse relation endpoints but preserves semantics
                active.add(j.left)
            else:
                raise ValueError("JOIN order must connect a new table to the query")
    if not plan.projections:
        raise ValueError("Select at least one result column")
    for p in plan.projections:
        if p.table not in active or p.aggregate not in AGGREGATES:
            raise ValueError("Invalid result column or aggregation")
        if p.column != "*" and not tables[p.table].has_column(p.column):
            raise ValueError("Selected column does not exist")
        if p.column == "*" and p.aggregate not in ("", "COUNT"):
            raise ValueError("Only COUNT can aggregate all columns")
        if p.alias and (len(p.alias) > 100 or "\x00" in p.alias):
            raise ValueError("Invalid result column alias")
    for f in plan.filters:
        if f.table not in active or f.operator not in OPERATORS or f.logical not in ("AND", "OR"):
            raise ValueError("Invalid filter")
        if not tables[f.table].has_column(f.column):
            raise ValueError("Filter column does not exist")
        if len(f.value) > 1000:
            raise ValueError("Filter value is too long")
    for key, col in plan.group_by:
        if key not in active or not tables[key].has_column(col):
            raise ValueError("Invalid GROUP BY")
    if plan.sort and (plan.sort.table not in active or not tables[plan.sort.table].has_column(plan.sort.column) or plan.sort.direction not in ("ASC", "DESC")):
        raise ValueError("Invalid ORDER BY")


class SQLBuilder:
    def __init__(self, plan: QueryPlan, tables: dict[str, Table]):
        validate_plan(plan, tables)
        self.plan, self.tables = plan, tables

    def _ref(self, table: str, column: str) -> str:
        return quote_identifier(self.plan.alias_for(table), self.plan.dialect) + "." + ("*" if column == "*" else quote_identifier(column, self.plan.dialect))

    def _value(self, table: str, column: str, raw: str):
        c = next(c for c in self.tables[table].columns if c.name == column)
        return numeric_value(raw, c.dtype)

    def _predicate(self, f: Filter, params: list, parameterized: bool) -> str:
        left = self._ref(f.table, f.column)
        if f.operator in ("IS NULL", "IS NOT NULL"):
            return left + " " + f.operator
        if f.operator in ("IN", "NOT IN"):
            parts = [s.strip() for s in f.value.split(",") if s.strip()]
            if not parts or len(parts) > 80:
                raise ValueError("IN filter needs 1-80 comma-separated values")
            values = [self._value(f.table, f.column, v) for v in parts]
            return f"{left} {f.operator} (" + ", ".join(self._bind(v, params, parameterized) for v in values) + ")"
        if f.operator == "BETWEEN":
            parts = [s.strip() for s in f.value.split(",")]
            if len(parts) != 2:
                raise ValueError("BETWEEN requires two comma-separated values")
            v1 = self._bind(self._value(f.table, f.column, parts[0]), params, parameterized)
            v2 = self._bind(self._value(f.table, f.column, parts[1]), params, parameterized)
            return f"{left} BETWEEN {v1} AND {v2}"
        val = self._bind(self._value(f.table, f.column, f.value), params, parameterized)
        return f"{left} {f.operator} {val}"

    def _bind(self, value, params: list, parameterized: bool) -> str:
        if not parameterized:
            return literal(value, self.plan.dialect)
        params.append(value)
        return "?" if self.plan.dialect in ("sqlite", "tsql") else "%s"

    def build(self, limit: int | None = None, parameterized: bool = False) -> BuiltSQL:
        plan, dialect = self.plan, self.plan.dialect
        if limit is not None and (not isinstance(limit, int) or not 1 <= limit <= 500):
            raise ValueError("Preview must be limited to 1-500 rows")
        params: list = []
        exprs = []
        for p in plan.projections:
            expr = self._ref(p.table, p.column)
            if p.aggregate:
                if p.aggregate == "COUNT_DISTINCT":
                    expr = "COUNT(DISTINCT " + expr + ")"
                else:
                    expr = p.aggregate + "(" + expr + ")"
            if p.alias:
                expr += " AS " + quote_identifier(p.alias, dialect)
            exprs.append(expr)
        select = "SELECT " + ("DISTINCT " if plan.distinct else "")
        if dialect == "tsql" and limit is not None:
            select += f"TOP ({limit}) "
        result = select + "\n    " + ",\n    ".join(exprs)
        result += "\nFROM " + quote_table(plan.base, dialect) + " AS " + quote_identifier(plan.alias_for(plan.base), dialect)
        active = {plan.base}
        exists_predicates = []
        for j in plan.joins:
            if j.kind == "EXISTS":
                if j.left in active and j.right not in active:
                    existing_key, existing_col, other_key, other_col = j.left, j.left_column, j.right, j.right_column
                elif j.right in active and j.left not in active:
                    existing_key, existing_col, other_key, other_col = j.right, j.right_column, j.left, j.left_column
                else:
                    raise ValueError("EXISTS relationship is ambiguous")
                extra_terms = [self._ref(j.left, c1) + " = " + self._ref(j.right, c2) for c1, c2 in j.extras]
                exists_predicates.append(
                    "EXISTS (SELECT 1 FROM " + quote_table(other_key, dialect) + " AS " + quote_identifier(plan.alias_for(other_key), dialect)
                    + " WHERE " + self._ref(other_key, other_col) + " = " + self._ref(existing_key, existing_col)
                    + (" AND " + " AND ".join(extra_terms) if extra_terms else "") + ")"
                )
                continue
            kind = j.kind
            if j.left in active and j.right not in active:
                nxt = j.right
            elif j.right in active and j.left not in active:
                nxt = j.left
                # Preserve the direction chosen on the visual relation.
                if kind == "LEFT":
                    kind = "RIGHT"
                elif kind == "RIGHT":
                    kind = "LEFT"
            else:
                raise ValueError("Disconnected JOIN graph")
            result += "\n" + kind + " JOIN " + quote_table(nxt, dialect)
            result += " AS " + quote_identifier(plan.alias_for(nxt), dialect)
            result += " ON " + self._ref(j.left, j.left_column) + " = " + self._ref(j.right, j.right_column)
            for left_col, right_col in j.extras:
                result += " AND " + self._ref(j.left, left_col) + " = " + self._ref(j.right, right_col)
            active.add(nxt)
        filters: list[str] = []
        for f in plan.filters:
            filters.append(self._predicate(f, params, parameterized))
        if filters or exists_predicates:
            grouped = ""
            if filters:
                grouped = "(" + filters[0]
                for i, f in enumerate(plan.filters[1:], 1):
                    grouped += " " + f.logical + " " + filters[i]
                grouped += ")"
            predicates = [grouped] if grouped else []
            predicates.extend(exists_predicates)
            result += "\nWHERE " + "\n  AND ".join(predicates)
        if plan.group_by:
            result += "\nGROUP BY " + ", ".join(self._ref(t, c) for t, c in plan.group_by)
        if plan.sort:
            result += "\nORDER BY " + self._ref(plan.sort.table, plan.sort.column) + " " + plan.sort.direction
        if dialect != "tsql" and limit is not None:
            result += f"\nLIMIT {limit}"
        return BuiltSQL(result + ";", tuple(params))
