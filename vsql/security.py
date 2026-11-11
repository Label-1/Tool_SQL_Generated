"""Defensive checks for restricted builder-generated SQL.

CRITICAL: this is NOT a general-purpose SQL execution validator. Users cannot
execute arbitrary SQL. The sole executable SQL source is SQLBuilder's typed
plan, checked against live metadata, plus database READ-ONLY credentials.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from .models import QueryPlan, Table
from .sql_builder import SQLBuilder, validate_plan

BAD_WORDS = frozenset(
    "INSERT UPDATE DELETE REPLACE UPSERT MERGE CREATE ALTER DROP TRUNCATE GRANT REVOKE "
    "EXEC EXECUTE CALL DO COPY LOAD UNLOAD INTO OUTFILE DUMPFILE FOR SHARE LOCK "
    "PROCEDURE FUNCTION TRIGGER COMMIT ROLLBACK BEGIN SAVEPOINT ATTACH DETACH "
    "PRAGMA VACUUM ANALYZE EXPLAIN SET USE KILL SHUTDOWN HANDLER".split()
)


@dataclass
class SafetyReport:
    safe: bool
    message: str


def scrub_sql(sql: str) -> str:
    """Mask quoted strings/identifiers; reject unterminated quotes & comments."""
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i) or sql.startswith("/*", i) or sql.startswith("#", i):
            raise ValueError("Comments are not accepted in generated SQL")
        if c in ("'", '"', "`", "["):
            end = "]" if c == "[" else c
            i += 1
            while i < n:
                if sql[i] == end:
                    if i + 1 < n and sql[i + 1] == end:
                        i += 2
                        continue
                    i += 1
                    break
                if c == "'" and sql[i] == "\\":
                    # MySQL string escaping, accepted only from our own compiler.
                    i += 2
                else:
                    i += 1
            else:
                raise ValueError("Unterminated SQL string or identifier")
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def check_generated_sql(sql: str) -> SafetyReport:
    """Inspect the full SQL text as a second line of defense for our compiler.

    Not intended to certify arbitrary SQL pasted from other applications.
    """
    try:
        normalized = scrub_sql(sql)
        if normalized.count(";") != 1 or not normalized.strip().endswith(";"):
            return SafetyReport(False, "SQL must consist of exactly one statement")
        body = normalized.strip()[:-1]
        if not re.match(r"^SELECT\b", body, re.I):
            return SafetyReport(False, "Only SELECT statements are permitted")
        if body.count("(") != body.count(")"):
            return SafetyReport(False, "SQL parentheses are not balanced")
        tokens = re.findall(r"[A-Za-z_][A-Za-z_0-9]*", body.upper())
        blocked = BAD_WORDS.intersection(tokens)
        if blocked:
            return SafetyReport(False, "Non-read-only token detected: " + ", ".join(sorted(blocked)))
        if not re.search(r"\bFROM\b", body, re.I):
            return SafetyReport(False, "SELECT has no source table")
        return SafetyReport(True, "Built from restricted query plan; full statement checked")
    except ValueError as exc:
        return SafetyReport(False, str(exc))


def validate_compiled_plan(plan: QueryPlan, tables: dict[str, Table]) -> tuple[str, SafetyReport]:
    try:
        validate_plan(plan, tables)
        sql = SQLBuilder(plan, tables).build().sql
        return sql, check_generated_sql(sql)
    except (ValueError, KeyError) as exc:
        return "", SafetyReport(False, str(exc))


def check_sql_ast_if_available(sql: str, dialect: str, required: bool = False) -> SafetyReport:
    """Optional SQLGlot AST verification of entire compiler output.

    Production database preview requires this dependency. If unavailable or
    unsure, the app refuses live preview rather than interpreting arbitrary SQL.
    """
    try:
        import sqlglot
        from sqlglot.errors import ErrorLevel
        from sqlglot import exp
    except ImportError:
        if required:
            return SafetyReport(False, "Install sqlglot using install_windows.bat before live DB Preview")
        return SafetyReport(True, "Offline compiler checks only (sqlglot not installed)")
    try:
        mapping = {"mysql": "mysql", "postgres": "postgres", "tsql": "tsql", "sqlite": "sqlite"}
        expressions = sqlglot.parse(sql, read=mapping[dialect], error_level=ErrorLevel.RAISE)
        statements = [x for x in expressions if x is not None]
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            return SafetyReport(False, "AST: expected exactly one SELECT statement")
        denied = []
        for cname in ("Insert", "Update", "Delete", "Merge", "Create", "Alter", "Drop", "Command", "Into",
                      "Lock", "Copy", "Transaction", "Commit", "Rollback", "Set", "Grant", "Revoke"):
            cls = getattr(exp, cname, None)
            if cls is not None:
                denied.append(cls)
        allowed_functions = tuple(getattr(exp, cname) for cname in ("Count", "Sum", "Avg", "Min", "Max") if hasattr(exp, cname))
        for node in statements[0].walk():
            if denied and isinstance(node, tuple(denied)):
                return SafetyReport(False, "AST: query contains a non-read-only construct")
            if isinstance(node, exp.Func) and not isinstance(node, allowed_functions):
                return SafetyReport(False, "AST: unsupported SQL function")
        return SafetyReport(True, "Full SQL AST: one SELECT, only approved built-in aggregates")
    except Exception as exc:
        return SafetyReport(False, "SQL AST parse failed: " + str(exc)[:250])
