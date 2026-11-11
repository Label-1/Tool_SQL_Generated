"""Small, serializable query plan. AI may propose edits to this, never raw SQL."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any

SEP = "\x1f"


def table_key(schema: str, table: str, catalog: str = "") -> str:
    """Stable identity: schema.table or catalog.schema.table (SQL Server).

    MySQL calls databases 'schemas', so the two-part form already addresses
    cross-database joins: database_name.table_name.
    """
    names = (catalog, schema, table) if catalog else (schema, table)
    if any(not isinstance(name, str) or not name or SEP in name for name in names):
        raise ValueError("Invalid database/schema/table name")
    return SEP.join(names)


def split_key(key: str) -> tuple[str, ...]:
    """Return exact identifier parts for fully qualified SQL rendering."""
    parts = key.split(SEP)
    if len(parts) not in (2, 3) or any(not part for part in parts):
        raise ValueError("Invalid table reference")
    return tuple(parts)


@dataclass
class Column:
    name: str
    dtype: str = "TEXT"
    nullable: bool = True
    primary: bool = False


@dataclass
class Table:
    schema: str
    name: str
    columns: list[Column]
    catalog: str = ""   # SQL Server database; for MySQL schema already means database

    @property
    def key(self) -> str:
        return table_key(self.schema, self.name, self.catalog)

    @property
    def qualified_name(self) -> str:
        return ".".join((self.catalog, self.schema, self.name) if self.catalog
                        else (self.schema, self.name))

    def has_column(self, name: str) -> bool:
        return any(c.name == name for c in self.columns)


@dataclass
class TableNode:
    key: str
    alias: str
    x: int = 40
    y: int = 50


@dataclass
class Projection:
    table: str
    column: str
    aggregate: str = ""     # COUNT, COUNT_DISTINCT, SUM, AVG, MIN, MAX
    alias: str = ""


@dataclass
class Relation:
    left: str
    left_column: str
    right: str
    right_column: str
    kind: str = "INNER"    # INNER, LEFT, RIGHT, EXISTS
    extras: list[tuple[str, str]] = field(default_factory=list)  # additional ON equality pairs


@dataclass
class Filter:
    table: str
    column: str
    operator: str = "="
    value: str = ""
    logical: str = "AND"   # connector with previous filter


@dataclass
class Sort:
    table: str
    column: str
    direction: str = "ASC"


@dataclass
class QueryPlan:
    name: str = "Untitled Query"
    dialect: str = "sqlite"
    base: str = ""
    nodes: list[TableNode] = field(default_factory=list)
    joins: list[Relation] = field(default_factory=list)
    projections: list[Projection] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    group_by: list[tuple[str, str]] = field(default_factory=list)
    sort: Sort | None = None
    distinct: bool = False
    # Query context intentionally belongs only to this plan, not a global AI chat.
    user_context: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueryPlan":
        if not isinstance(data, dict):
            raise ValueError("Workspace must contain an object")
        plan = cls(
            name=str(data.get("name", "Untitled Query"))[:120],
            dialect=str(data.get("dialect", "sqlite")),
            base=str(data.get("base", "")),
            nodes=[TableNode(**n) for n in data.get("nodes", [])],
            joins=[Relation(**j) for j in data.get("joins", [])],
            projections=[Projection(**p) for p in data.get("projections", [])],
            filters=[Filter(**f) for f in data.get("filters", [])],
            group_by=[tuple(g) for g in data.get("group_by", [])],
            sort=Sort(**data["sort"]) if data.get("sort") else None,
            distinct=bool(data.get("distinct", False)),
            user_context=[str(x) for x in data.get("user_context", [])][-10:],
        )
        if len(plan.nodes) > 40 or len(plan.joins) > 60 or len(plan.filters) > 50:
            raise ValueError("Workspace exceeds safety limits")
        return plan

    def alias_for(self, key: str) -> str:
        node = next((n for n in self.nodes if n.key == key), None)
        if not node:
            raise ValueError("Table is not on canvas")
        return node.alias

    def add_node(self, table: Table, x: int = 40, y: int = 50) -> None:
        if any(n.key == table.key for n in self.nodes):
            return
        alias = "t" + str(len(self.nodes) + 1)
        self.nodes.append(TableNode(table.key, alias, x, y))
        if not self.base:
            self.base = table.key
            self.projections.append(Projection(table.key, "*"))

    def clear_context(self) -> None:
        self.user_context.clear()
