"""A typed, bounded dataset specification — the draft before anything exists (P6-F02).

Phase 6 turns a validated statement into a Superset dataset. The risky part is not
the save; it is everything the spec asserts about the data. So this module compiles
the *whole* proposal before anything is stored or created, and it is deliberately
pedantic:

* **a definition is not a preview.** Its SQL is validated by the same guard as any
  other statement — one read-only query, the authorized allowlist, row-level scope —
  but without the preview row cap, because the text becomes the dataset
  (:func:`sql_execution.validate_definition`).
* **types are normalised, not trusted.** A column type decides whether a metric is
  possible and whether a temporal axis is offered, so it is mapped onto a small
  vocabulary and an unknown type becomes ``other`` rather than a guess.
* **grain must exist in the columns.** A declared grain that names a column the
  dataset does not have is a quality bug that would surface as duplicate rows later.
* **names are identifier-shaped.** A dataset or metric name reaches Superset, a URL
  and a SQL expression; it is bounded and character-checked here rather than left to
  the database.
* **the fingerprint is content-free.** It hashes the shape, so "is this the same
  draft / the same source" is answerable without storing the statement in a column.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from xp_prompt import sql_execution, sql_guard
from xp_prompt.schema_slice import SchemaSlice

#: How the dataset reads its source. A virtual dataset is a stored query.
PHYSICAL = "physical"
VIRTUAL = "virtual"
KINDS: tuple[str, ...] = (PHYSICAL, VIRTUAL)

#: The column vocabulary a caller may declare. Anything else becomes ``other``.
COLUMN_TYPES: tuple[str, ...] = (
    "bigint",
    "boolean",
    "date",
    "datetime",
    "decimal",
    "integer",
    "text",
    "other",
)
NUMERIC_TYPES: frozenset[str] = frozenset({"bigint", "decimal", "integer"})
TEMPORAL_TYPES: frozenset[str] = frozenset({"date", "datetime"})

COLUMN_ROLES: tuple[str, ...] = ("dimension", "measure", "temporal")

MAX_NAME_CHARS = 128
MAX_DESCRIPTION_CHARS = 1_000
MAX_COLUMNS = 200
MAX_METRICS = 50
MAX_GRAIN = 10
MAX_OWNERS = 10
MAX_OWNER_CHARS = 128
MAX_EXPRESSION_CHARS = 1_000
MAX_SQL_CHARS = 20_000

_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .\-]{0,127}$")
_COLUMN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

_TYPE_ALIASES: dict[str, str] = {
    "int": "integer",
    "int2": "integer",
    "int4": "integer",
    "int8": "bigint",
    "smallint": "integer",
    "serial": "integer",
    "bigserial": "bigint",
    "smallserial": "integer",
    "tinyint": "integer",
    "mediumint": "integer",
    "integer": "integer",
    "bigint": "bigint",
    "numeric": "decimal",
    "decimal": "decimal",
    "money": "decimal",
    "float": "decimal",
    "float4": "decimal",
    "float8": "decimal",
    "double": "decimal",
    "double precision": "decimal",
    "real": "decimal",
    "text": "text",
    "varchar": "text",
    "character varying": "text",
    "char": "text",
    "character": "text",
    "string": "text",
    "uuid": "text",
    "enum": "text",
    "json": "other",
    "jsonb": "other",
    "bytea": "other",
    "blob": "other",
    "boolean": "boolean",
    "bool": "boolean",
    "date": "date",
    "datetime": "datetime",
    "timestamp": "datetime",
    "timestamp with time zone": "datetime",
    "timestamp without time zone": "datetime",
    "timestamptz": "datetime",
    "time": "other",
}


class DatasetSpecError(ValueError):
    """A refused specification; ``code`` is stable and content-free."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DatasetColumn:
    name: str
    type: str = "other"
    role: str = "dimension"
    description: str = ""
    filterable: bool = True
    groupby: bool = True

    @property
    def is_numeric(self) -> bool:
        return self.type in NUMERIC_TYPES

    @property
    def is_temporal(self) -> bool:
        return self.type in TEMPORAL_TYPES

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "role": self.role,
            "description": self.description,
            "filterable": self.filterable,
            "groupby": self.groupby,
        }


@dataclass(frozen=True, slots=True)
class DatasetMetric:
    name: str
    expression: str
    description: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expression": self.expression,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """One complete, validated proposal. Nothing in Superset refers to it yet."""

    name: str
    kind: str
    database: str
    columns: tuple[DatasetColumn, ...]
    schema: str = ""
    table: str = ""
    sql: str = ""
    dialect: str = ""
    description: str = ""
    grain: tuple[str, ...] = ()
    metrics: tuple[DatasetMetric, ...] = ()
    temporal_column: str = ""
    owners: tuple[str, ...] = ()
    certified_by: str = ""
    certification_details: str = ""
    tables: tuple[str, ...] = ()
    inlined: tuple[str, ...] = ()
    rls_applied: tuple[str, ...] = ()
    schema_fingerprint: str = ""
    fingerprint: str = ""
    warnings: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def is_virtual(self) -> bool:
        return self.kind == VIRTUAL

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def column(self, name: str) -> DatasetColumn | None:
        lowered = (name or "").strip().lower()
        for column in self.columns:
            if column.name.lower() == lowered:
                return column
        return None

    def to_public_dict(self, *, include_sql: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "database": self.database,
            "schema": self.schema,
            "table": self.table,
            "dialect": self.dialect,
            "description": self.description,
            "grain": list(self.grain),
            "columns": [column.to_public_dict() for column in self.columns],
            "metrics": [metric.to_public_dict() for metric in self.metrics],
            "temporal_column": self.temporal_column,
            "owners": list(self.owners),
            "certified_by": self.certified_by,
            "certification_details": self.certification_details,
            "tables": list(self.tables),
            "inlined": list(self.inlined),
            "rls_applied": list(self.rls_applied),
            "schema_fingerprint": self.schema_fingerprint,
            "fingerprint": self.fingerprint,
            "warnings": list(self.warnings),
            "sql_hidden": not include_sql,
        }
        if include_sql:
            payload["sql"] = self.sql
        return payload


def normalise_type(value: str) -> str:
    """Map a driver type onto the small vocabulary a spec may declare."""
    text = " ".join((value or "").strip().lower().split())
    if not text:
        return "other"
    base = text.split("(")[0].strip()
    return _TYPE_ALIASES.get(base, _TYPE_ALIASES.get(text, "other"))


def from_payload(
    payload: dict[str, Any] | None, *, schema: SchemaSlice | None = None
) -> DatasetSpec:
    """Compile and validate one dataset proposal, or refuse it with a stable code."""
    body = payload if isinstance(payload, dict) else {}
    name = " ".join(str(body.get("name") or "").split())
    if not name:
        raise DatasetSpecError("name_required", "a dataset name is required")
    if len(name) > MAX_NAME_CHARS or not _NAME.match(name):
        raise DatasetSpecError(
            "invalid_name",
            "a dataset name must be identifier-shaped and at most "
            f"{MAX_NAME_CHARS} characters",
        )
    kind = str(body.get("kind") or VIRTUAL).strip().lower()
    if kind not in KINDS:
        raise DatasetSpecError("unsupported_kind", f"unsupported dataset kind: {kind}")
    database = str(body.get("database") or "").strip()
    if not database:
        raise DatasetSpecError("database_required", "a source database is required")
    schema_name = str(body.get("schema") or "").strip()
    table = str(body.get("table") or "").strip()
    raw_sql = str(body.get("sql") or "").strip()
    dialect = str(body.get("dialect") or "").strip().lower()

    warnings: list[dict[str, Any]] = []
    tables: list[str] = []
    inlined: list[str] = []
    rls_applied: list[str] = []
    schema_fingerprint = ""

    if kind == PHYSICAL:
        if not table:
            raise DatasetSpecError(
                "table_required", "a physical dataset needs a table"
            )
        if raw_sql:
            raise DatasetSpecError(
                "sql_not_allowed", "a physical dataset must not carry SQL"
            )
        statement = ""
        if schema is not None:
            allowed = {name.lower() for name in schema.allowed_tables(database=database)}
            reference = f"{schema_name}.{table}".lower() if schema_name else table.lower()
            if allowed and reference not in allowed:
                raise DatasetSpecError(
                    "table_not_authorized", "the table is not in your authorized slice"
                )
            schema_fingerprint = schema.fingerprint
            tables = [reference]
            if dialect:
                warning = _dialect_warning(dialect, schema, database)
                if warning:
                    warnings.append(warning)
    else:
        if not raw_sql:
            raise DatasetSpecError(
                "sql_required", "a virtual dataset needs its definition"
            )
        if len(raw_sql) > MAX_SQL_CHARS:
            raise DatasetSpecError("sql_too_large", "the definition is too long")
        if table:
            raise DatasetSpecError(
                "table_not_allowed", "a virtual dataset is defined by SQL, not a table"
            )
        if schema is None:
            raise DatasetSpecError(
                "schema_required", "the definition cannot be validated without a slice"
            )
        try:
            validated = sql_execution.validate_definition(
                raw_sql, schema=schema, database=database, dialect=dialect or None
            )
        except sql_guard.SqlPolicyError as exc:
            raise DatasetSpecError(
                f"sql_blocked:{exc.code}", f"the definition was refused ({exc.code})"
            ) from exc
        statement = str(validated["sql"])
        dialect = str(validated.get("dialect") or dialect)
        tables = list(validated.get("tables") or [])
        inlined = list(validated.get("inlined") or [])
        rls_applied = list(validated.get("rls_applied") or [])
        schema_fingerprint = str(validated.get("schema_fingerprint") or "")

    columns = _columns(body.get("columns"))
    grain = _grain(body.get("grain"), columns)
    metrics = _metrics(body.get("metrics"))
    temporal = _temporal(body.get("temporal_column"), columns, warnings)
    owners = _owners(body.get("owners"))
    description = _text(body.get("description"), MAX_DESCRIPTION_CHARS)
    certified_by = _text(body.get("certified_by"), MAX_NAME_CHARS)
    certification_details = _text(
        body.get("certification_details"), MAX_DESCRIPTION_CHARS
    )
    if not columns:
        warnings.append(
            _warning("no_columns", "warning", "", "no columns were declared")
        )
    if kind == VIRTUAL and not grain and not metrics:
        warnings.append(
            _warning(
                "no_grain_or_metric",
                "warning",
                "",
                "a virtual dataset without a grain or a metric cannot be charted "
                "reliably",
            )
        )

    spec = DatasetSpec(
        name=name,
        kind=kind,
        database=database,
        columns=columns,
        schema=schema_name,
        table=table,
        sql=statement,
        dialect=dialect,
        description=description,
        grain=grain,
        metrics=metrics,
        temporal_column=temporal,
        owners=owners,
        certified_by=certified_by,
        certification_details=certification_details,
        tables=tuple(tables),
        inlined=tuple(inlined),
        rls_applied=tuple(rls_applied),
        schema_fingerprint=schema_fingerprint,
        warnings=tuple(warnings),
    )
    return _with_fingerprint(spec)


def fingerprint_of(spec: DatasetSpec) -> str:
    """A content-free identity for "this dataset shape" (no SQL, no data)."""
    material = {
        "name": spec.name.lower(),
        "kind": spec.kind,
        "database": spec.database.lower(),
        "schema": spec.schema.lower(),
        "table": spec.table.lower(),
        "dialect": spec.dialect,
        "sql_sha256": hashlib.sha256(spec.sql.encode("utf-8")).hexdigest()
        if spec.sql
        else "",
        "columns": [
            [column.name.lower(), column.type, column.role] for column in spec.columns
        ],
        "grain": [name.lower() for name in spec.grain],
        "metrics": [
            [metric.name.lower(), metric.expression] for metric in spec.metrics
        ],
        "temporal": spec.temporal_column.lower(),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _with_fingerprint(spec: DatasetSpec) -> DatasetSpec:
    from dataclasses import replace

    return replace(spec, fingerprint=fingerprint_of(spec))


def _columns(raw: Any) -> tuple[DatasetColumn, ...]:
    entries: list[DatasetColumn] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, (list, tuple)) else ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name or not _COLUMN_NAME.match(name) or name.lower() in seen:
            continue
        seen.add(name.lower())
        declared = normalise_type(str(entry.get("type") or ""))
        role = str(entry.get("role") or "").strip().lower()
        if role not in COLUMN_ROLES:
            role = "temporal" if declared in TEMPORAL_TYPES else "dimension"
        entries.append(
            DatasetColumn(
                name=name,
                type=declared,
                role=role,
                description=_text(entry.get("description"), 200),
                filterable=bool(entry.get("filterable", True)),
                groupby=bool(entry.get("groupby", True)),
            )
        )
        if len(entries) >= MAX_COLUMNS:
            break
    return tuple(entries)


def _grain(raw: Any, columns: tuple[DatasetColumn, ...]) -> tuple[str, ...]:
    """The grain must name columns the dataset actually has, in declared spelling."""
    names: list[str] = []
    known = {column.name.lower(): column.name for column in columns}
    for entry in raw if isinstance(raw, (list, tuple)) else ():
        text = str(entry or "").strip()
        canonical = known.get(text.lower())
        if not text or canonical is None or canonical in names:
            continue
        names.append(canonical)
        if len(names) >= MAX_GRAIN:
            break
    return tuple(names)


def _metrics(raw: Any) -> tuple[DatasetMetric, ...]:
    entries: list[DatasetMetric] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, (list, tuple)) else ():
        if not isinstance(entry, dict):
            continue
        name = " ".join(str(entry.get("name") or "").split())
        expression = " ".join(str(entry.get("expression") or "").split())
        if not name or not expression or name.lower() in seen:
            continue
        if len(expression) > MAX_EXPRESSION_CHARS:
            continue
        seen.add(name.lower())
        entries.append(
            DatasetMetric(
                name=name,
                expression=expression,
                description=_text(entry.get("description"), 200),
            )
        )
        if len(entries) >= MAX_METRICS:
            break
    return tuple(entries)


def _temporal(
    raw: Any, columns: tuple[DatasetColumn, ...], warnings: list[dict[str, Any]]
) -> str:
    """The temporal axis, or an honest warning when there is none."""
    requested = str(raw or "").strip()
    if requested:
        column = next(
            (item for item in columns if item.name.lower() == requested.lower()), None
        )
        if column is None:
            warnings.append(
                _warning(
                    "unknown_temporal",
                    "warning",
                    requested,
                    "the declared temporal column is not part of the dataset",
                )
            )
            return ""
        if not column.is_temporal:
            warnings.append(
                _warning(
                    "temporal_type_mismatch",
                    "warning",
                    column.name,
                    f"the temporal column is typed {column.type}",
                )
            )
        return column.name
    inferred = next((column for column in columns if column.is_temporal), None)
    if inferred is not None:
        return inferred.name
    warnings.append(
        _warning(
            "no_temporal_column",
            "warning",
            "",
            "no temporal column was found, so time-series charts are unavailable",
        )
    )
    return ""


def _owners(raw: Any) -> tuple[str, ...]:
    owners: list[str] = []
    for entry in raw if isinstance(raw, (list, tuple)) else ():
        text = str(entry or "").strip()[:MAX_OWNER_CHARS]
        if text and text not in owners:
            owners.append(text)
        if len(owners) >= MAX_OWNERS:
            break
    return tuple(owners)


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _warning(code: str, severity: str, column: str, message: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "column": column, "message": message}


def _dialect_warning(
    dialect: str, schema: SchemaSlice, database: str
) -> dict[str, Any] | None:
    try:
        resolved = sql_execution.resolve_dialect(
            schema, database=database, dialect=dialect
        )
    except sql_guard.SqlPolicyError as exc:
        return _warning(exc.code, "blocking", "", str(exc))
    if resolved != dialect:
        return _warning(
            "dialect_mismatch",
            "blocking",
            "",
            f"the database speaks {resolved}, not {dialect}",
        )
    return None
