"""Read-only SQL guard (P4-F05).

Every candidate SQL string produced for a user passes through :func:`check`
before it can reach a database. The guard is deliberately paranoid and cheap:

* **one statement, and it must be a query.** ``sqlglot`` parses the text into an
  AST; anything that is not a ``SELECT``/``UNION`` (DDL, DML, ``COPY``, a vendor
  ``Command``, ``SELECT … INTO``) is rejected. A trailing ``; DROP …`` produces a
  second statement and is rejected before any node walk.
* **no escape hatches.** Functions that read files, run shells, sleep, reach the
  network, or advance sequences are denied by name, and system schemas
  (``information_schema``, ``pg_catalog``, ``mysql``, …) are refused outright.
* **only allowlisted tables.** When the caller knows the authorized schema slice
  (P4-F02), every referenced table must be in it — CTE names are not tables and
  are excluded.
* **a row cap the model cannot raise.** A missing or oversized ``LIMIT`` is
  replaced with :data:`MAX_ROWS`; only a non-literal limit (an expression or a
  bind) forces a subquery wrap, because its value cannot be checked here.

The guard never executes anything and never talks to a database: it is pure, so
the adversarial corpus in ``tests/test_sql_guard.py`` runs in milliseconds.

Refusals raise :class:`SqlPolicyError` with a stable ``code``. The code — not the
SQL text — is what gets stored and logged downstream (P4-F12).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

#: sqlglot logs the offending text ("<sql> contains unsupported syntax") when it
#: falls back to an opaque Command node. That would put candidate SQL — which may
#: contain user data — into the container log, so only real errors are kept.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

#: Dialects this guard has been exercised against. Anything else is refused
#: rather than parsed optimistically with the wrong grammar.
SUPPORTED_DIALECTS: tuple[str, ...] = (
    "bigquery",
    "clickhouse",
    "duckdb",
    "mysql",
    "postgres",
    "redshift",
    "snowflake",
    "spark",
    "sqlite",
    "trino",
    "tsql",
)

MAX_SQL_CHARS = 20_000
MAX_ROWS = 1_000
MIN_ROWS = 1

#: Functions that read files, execute commands, reach the network, waste time, or
#: mutate state. Compared upper-cased against ``sql_name()``/anonymous names.
DENIED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # filesystem / server-side readers
        "PG_READ_FILE",
        "PG_READ_BINARY_FILE",
        "PG_LS_DIR",
        "PG_STAT_FILE",
        "PG_LS_LOGDIR",
        "PG_LS_WALDIR",
        "PG_LS_TMPDIR",
        "LO_IMPORT",
        "LO_EXPORT",
        "LOAD_FILE",
        "READ_CSV",
        "READ_CSV_AUTO",
        "READ_PARQUET",
        "READ_JSON",
        "READ_JSON_AUTO",
        "READ_TEXT",
        "READ_BLOB",
        "DELTA_SCAN",
        "ICEBERG_SCAN",
        # network / cross-database
        "DBLINK",
        "DBLINK_EXEC",
        "POSTGRES_FDW",
        "OPENROWSET",
        "OPENDATASOURCE",
        "OPENDATASET",
        "URL",
        "S3",
        "GCS",
        "AZURE_BLOB_STORAGE",
        # command execution
        "XP_CMDSHELL",
        "SYS_EVAL",
        "SYS_EXEC",
        "SYSTEM",
        "SHELL",
        "EXEC",
        "EXECUTE",
        "EXECUTE_IMMEDIATE",
        "PYTHON",
        "RUN_PYTHON",
        # time / resource abuse
        "PG_SLEEP",
        "SLEEP",
        "BENCHMARK",
        "GET_LOCK",
        "RELEASE_LOCK",
        "PG_TERMINATE_BACKEND",
        "PG_CANCEL_BACKEND",
        "PG_RELOAD_CONF",
        # state mutation through a read-shaped call
        "NEXTVAL",
        "SETVAL",
        "SET_CONFIG",
        "LOAD_EXTENSION",
        "PG_CREATE_RESTORE_POINT",
        "PG_DROP_REPLICATION_SLOT",
    }
)

#: Schemas that describe the server rather than the tenant's data.
DENIED_SCHEMAS: frozenset[str] = frozenset(
    {
        "information_schema",
        "pg_catalog",
        "pg_toast",
        "pg_temp",
        "performance_schema",
        "mysql",
        "sys",
        "sqlite_master",
        "sqlite_schema",
        "system",
        "duckdb_internal",
        "iceberg",
        "snowflake",  # account-level INFORMATION_SCHEMA lives here too
        "dbms",
    }
)

#: MySQL/MariaDB executable comments hide a statement from naive scanners.
_EXECUTABLE_COMMENT = "/*!"

#: Node types that mutate, even if they appear inside a query-shaped statement.
_MUTATING_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Merge,
    exp.Grant,
    exp.Command,
)

_QUERY_NODES: tuple[type[exp.Expression], ...] = (exp.Select, exp.Union, exp.Intersect, exp.Except)


class SqlPolicyError(RuntimeError):
    """A refusal with a stable, content-free ``code`` (safe to log and store)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class GuardResult:
    """What the guard hands to the executor.

    ``sql`` is the rewritten statement (limit enforced, normalized) — always
    re-rendered from the AST, never the caller's original text.
    """

    sql: str
    dialect: str
    tables: tuple[str, ...]
    limit: int
    wrapped: bool
    read_only: bool = True

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "dialect": self.dialect,
            "tables": list(self.tables),
            "limit": self.limit,
            "wrapped": self.wrapped,
            "read_only": self.read_only,
        }


def check(
    sql: str,
    *,
    dialect: str = "postgres",
    allowed_tables: Any = None,
    max_rows: int = MAX_ROWS,
) -> GuardResult:
    """Validate one candidate statement and return a safe, limit-enforced SQL.

    ``allowed_tables`` is an optional iterable of authorized ``table`` or
    ``schema.table`` names (lower-cased here). When supplied, an unknown reference
    is refused — that is how the authorized schema slice of P4-F02 is enforced.
    """
    text = (sql or "").strip()
    if not text:
        raise SqlPolicyError("empty", "SQL is empty")
    if len(text) > MAX_SQL_CHARS:
        raise SqlPolicyError("too_large", "SQL is too long")
    if _EXECUTABLE_COMMENT in text:
        raise SqlPolicyError("obfuscation", "executable comments are not allowed")

    normalised_dialect = (dialect or "").strip().lower()
    if normalised_dialect not in SUPPORTED_DIALECTS:
        raise SqlPolicyError(
            "unsupported_dialect", f"unsupported SQL dialect: {dialect}"
        )

    statements = _parse(text, normalised_dialect)
    if len(statements) != 1:
        raise SqlPolicyError(
            "multiple_statements", "exactly one statement is allowed"
        )
    statement = statements[0]
    if not isinstance(statement, _QUERY_NODES):
        raise SqlPolicyError("not_read_only", "only SELECT queries are allowed")

    _reject_mutating_nodes(statement)
    _reject_denied_functions(statement)
    tables = _collect_tables(statement)
    _reject_system_schemas(tables)
    if allowed_tables is not None:
        _reject_unlisted_tables(tables, allowed_tables)

    capped, limit, wrapped = _enforce_limit(statement, max_rows)

    return GuardResult(
        sql=capped.sql(dialect=normalised_dialect),
        dialect=normalised_dialect,
        tables=tuple(sorted(selector for selector in tables)),
        limit=limit,
        wrapped=wrapped,
    )


def _parse(text: str, dialect: str) -> list[exp.Expression]:
    try:
        parsed = sqlglot.parse(text, read=dialect)
    except (SqlglotError, ValueError) as exc:
        raise SqlPolicyError("parse_error", "SQL could not be parsed") from exc
    # ``parse`` yields ``None`` for empty or comment-only fragments.
    statements = [item for item in parsed if item is not None]
    if not statements:
        raise SqlPolicyError("empty", "SQL is empty")
    return statements


def _reject_mutating_nodes(statement: exp.Expression) -> None:
    for node in statement.walk():
        if isinstance(node, _MUTATING_NODES):
            raise SqlPolicyError("not_read_only", "only SELECT queries are allowed")
        # ``SELECT … INTO new_table`` is a data-writing query in query clothing.
        if isinstance(node, exp.Into):
            raise SqlPolicyError("not_read_only", "SELECT INTO is not allowed")


def _reject_denied_functions(statement: exp.Expression) -> None:
    for node in statement.walk():
        if isinstance(node, exp.Func):
            name = _function_name(node)
            if name and name in DENIED_FUNCTIONS:
                raise SqlPolicyError(
                    "unsafe_function", "the query uses a forbidden function"
                )


def _function_name(node: exp.Expression) -> str:
    """Normalized function name, for the typed and the anonymous cases alike."""
    try:
        name = node.sql_name()
    except (AttributeError, ValueError):  # pragma: no cover - defensive
        name = ""
    if not name or name.upper() == "ANONYMOUS":
        raw = node.args.get("this")
        name = raw if isinstance(raw, str) else ""
    return name.strip().upper()


def _collect_tables(statement: exp.Expression) -> set[str]:
    """Referenced table names, excluding CTE aliases (they are not tables).

    A catalog-qualified reference keeps its catalog (``proj.ds.table`` in
    BigQuery), so a cross-project read cannot pass as the same dataset name.
    """
    cte_names = {
        cte.alias_or_name.lower()
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }
    names: set[str] = set()
    for table in statement.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name or name in cte_names:
            continue
        parts = [
            str(part).lower()
            for part in (table.catalog, table.db, name)
            if part
        ]
        names.add(".".join(parts))
    return names


def _reject_system_schemas(tables: set[str]) -> None:
    for table in tables:
        schema = table.split(".", 1)[0] if "." in table else ""
        if schema and schema in DENIED_SCHEMAS:
            raise SqlPolicyError(
                "system_schema", "metadata schemas are not available to analytics"
            )


def _reject_unlisted_tables(tables: set[str], allowed_tables: Any) -> None:
    allowed = {str(name).strip().lower() for name in allowed_tables}
    if not allowed:
        raise SqlPolicyError("unknown_table", "no tables are authorized")
    unknown = sorted(table for table in tables if table not in allowed)
    if unknown:
        raise SqlPolicyError("unknown_table", "the query references an unauthorized table")


def _enforce_limit(
    statement: exp.Expression, max_rows: int
) -> tuple[exp.Expression, int, bool]:
    """Guarantee a row cap the model cannot raise.

    Returns the rewritten statement, the effective cap, and whether a subquery
    wrap was needed (only when the existing limit is not a literal).
    """
    cap = max(MIN_ROWS, int(max_rows))
    existing = statement.args.get("limit")
    if existing is None:
        return statement.limit(cap), cap, False

    value = existing.expression
    if isinstance(value, exp.Literal) and not value.is_string:
        try:
            rows = int(value.this)
        except (TypeError, ValueError):
            rows = cap + 1
        if rows <= cap:
            return statement, rows, False
        return statement.limit(cap), cap, False

    # A bind or an expression: its value cannot be verified here, so contain the
    # whole statement instead of trusting it.
    wrapped = (
        exp.select("*")
        .from_(
            exp.Subquery(
                this=statement,
                alias=exp.TableAlias(this=exp.to_identifier("xp_capped")),
            )
        )
        .limit(cap)
    )
    return wrapped, cap, True
