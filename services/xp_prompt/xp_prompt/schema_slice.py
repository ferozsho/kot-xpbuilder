"""Authorized schema slice for SQL generation (P4-F02).

The slice is *built by Superset* (`security_manager.get_user_datasources`, the
same authority that gates Explore) and passed to this service. This module is the
boundary that turns it into something a pipeline can use safely:

* **nothing unvalidated survives.** Names and types are length-bounded and matched
  against an identifier pattern; a dataset with a quote, a newline, or a semicolon
  in a name is dropped rather than escaped, because it is more likely an attack
  than a table.
* **the slice is bounded.** A prompt is not a schema dump: at most
  :data:`MAX_DATASETS` datasets and :data:`MAX_COLUMNS` columns each, and
  :data:`MAX_PROMPT_CHARS` of rendered text.
* **only dialects the guard understands** are kept, normalised to sqlglot names
  (`postgresql` → `postgres`, a MariaDB URI → `mysql`), so P4-F05 can parse what
  P4-F02 hands it.
* **a fingerprint** (SHA-256 over the normalized slice) is carried into prompts and
  cache keys, so a stale slice can be detected instead of silently reused
  (P4-F11) and two turns with different schemas cannot share a cache entry.

The prompt text is produced from the validated slice only, which is what makes
"unauthorized names never enter prompts" true by construction.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from xp_prompt.sql_guard import SUPPORTED_DIALECTS

MAX_DATASETS = 50
MAX_COLUMNS = 60
MAX_NAME_CHARS = 128
MAX_TYPE_CHARS = 64
MAX_PROMPT_CHARS = 12_000

#: Conservative identifier shape for a table/schema/column name. Anything outside
#: it is dropped, not escaped.
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_$#][A-Za-z0-9_$# .-]{0,127}$")

#: Engine aliases -> sqlglot dialect names.
_DIALECT_ALIASES: dict[str, str] = {
    "postgresql": "postgres",
    "postgresql+psycopg2": "postgres",
    "postgres": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "mssql": "tsql",
    "sqlserver": "tsql",
    "tsql": "tsql",
    "bigquery": "bigquery",
    "snowflake": "snowflake",
    "redshift": "redshift",
    "clickhouse": "clickhouse",
    "trino": "trino",
    "spark": "spark",
    "sqlite": "sqlite",
    "duckdb": "duckdb",
}


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str = ""
    temporal: bool = False

    def render(self) -> str:
        suffix = f" {self.type}" if self.type else ""
        marker = " (time)" if self.temporal else ""
        return f"{self.name}{suffix}{marker}"


@dataclass(frozen=True, slots=True)
class Dataset:
    name: str
    database: str = ""
    schema: str = ""
    dialect: str = ""
    dataset_id: int | None = None
    columns: tuple[Column, ...] = ()

    @property
    def qualified_name(self) -> str:
        parts = [part for part in (self.database, self.schema, self.name) if part]
        return ".".join(parts)

    @property
    def sql_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def render(self) -> str:
        """One prompt line: the referencable name first, the label afterwards.

        ``database`` is a Superset connection *name* (``File uploads``), not a SQL
        catalog: putting it in front of the table teaches a model to write a
        cross-database reference that the server then refuses. It is therefore
        reported as metadata, and ``sql_name`` is what the model is told to use.
        """
        columns = ", ".join(column.render() for column in self.columns)
        origin = f"dialect: {self.dialect}" if self.dialect else ""
        if self.database:
            origin = f"{origin}, connection: {self.database}" if origin else (
                f"connection: {self.database}"
            )
        suffix = f" [{origin}]" if origin else ""
        return f"{self.sql_name}{suffix} ({columns})"


@dataclass(frozen=True, slots=True)
class SchemaSlice:
    datasets: tuple[Dataset, ...]
    dropped: int = 0
    fingerprint: str = ""
    truncated: bool = False

    @classmethod
    def from_payload(cls, raw: Any) -> "SchemaSlice":
        """Normalize a slice built by the authorization authority."""
        entries = raw.get("datasets") if isinstance(raw, dict) else raw
        if not isinstance(entries, list | tuple):
            return cls(datasets=(), fingerprint=_fingerprint([]))

        kept: list[Dataset] = []
        seen: set[tuple[str, str]] = set()
        dropped = 0
        for entry in entries:
            dataset = _dataset(entry)
            if dataset is None:
                dropped += 1
                continue
            key = (dataset.dialect, dataset.sql_name.lower())
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            kept.append(dataset)
            if len(kept) >= MAX_DATASETS:
                break

        kept.sort(key=lambda item: (item.database, item.schema, item.name))
        return cls(
            datasets=tuple(kept),
            dropped=dropped,
            fingerprint=_fingerprint(kept),
        )

    def allowed_tables(self, database: str | None = None) -> frozenset[str]:
        """Names the SQL guard may accept, for one database (or all of them).

        Every accepted form is included (``table``, ``schema.table``,
        ``database.schema.table``) because a model writes all three; passing
        ``database`` narrows the set to a single target, which is what execution
        should do.
        """
        names: set[str] = set()
        for dataset in self.datasets:
            if database and dataset.database and dataset.database != database:
                continue
            names.add(dataset.name.lower())
            if dataset.schema:
                names.add(f"{dataset.schema}.{dataset.name}".lower())
            if dataset.database:
                names.add(dataset.qualified_name.lower())
        return frozenset(names)

    def dialects(self) -> tuple[str, ...]:
        return tuple(sorted({dataset.dialect for dataset in self.datasets if dataset.dialect}))

    def connection_names(self) -> tuple[str, ...]:
        """Superset connection labels in this slice — metadata, not SQL catalogs."""
        return tuple(
            sorted({dataset.database for dataset in self.datasets if dataset.database})
        )

    def prompt_text(self) -> str:
        """The schema block a prompt may include — validated names only."""
        lines: list[str] = []
        total = 0
        truncated = False
        for dataset in self.datasets:
            line = dataset.render()
            if total + len(line) > MAX_PROMPT_CHARS:
                truncated = True
                break
            lines.append(line)
            total += len(line) + 1
        if truncated:
            return "\n".join(lines) + "\n(additional tables omitted)"
        return "\n".join(lines)

    def to_public_dict(self, *, include_prompt: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "fingerprint": self.fingerprint,
            "dataset_count": len(self.datasets),
            "dropped": self.dropped,
            "dialects": list(self.dialects()),
            "tables": [
                {
                    "name": dataset.sql_name,
                    "database": dataset.database,
                    "dialect": dataset.dialect,
                    "dataset_id": dataset.dataset_id,
                    "columns": [column.name for column in dataset.columns],
                }
                for dataset in self.datasets
            ],
        }
        if include_prompt:
            payload["prompt"] = self.prompt_text()
        return payload


def _dataset(entry: Any) -> Dataset | None:
    if not isinstance(entry, dict):
        return None
    name = _identifier(entry.get("name"))
    if not name:
        return None
    dialect = _dialect(entry.get("dialect"))
    if dialect is None:
        return None
    schema = _identifier(entry.get("schema")) or ""
    database = _plain(entry.get("database"))
    columns: list[Column] = []
    seen: set[str] = set()
    for raw_column in entry.get("columns") or []:
        if not isinstance(raw_column, dict):
            continue
        column_name = _identifier(raw_column.get("name"))
        if not column_name or column_name.lower() in seen:
            continue
        seen.add(column_name.lower())
        columns.append(
            Column(
                name=column_name,
                type=_plain(raw_column.get("type"), MAX_TYPE_CHARS),
                temporal=bool(raw_column.get("temporal")),
            )
        )
        if len(columns) >= MAX_COLUMNS:
            break
    return Dataset(
        name=name,
        database=database,
        schema=schema,
        dialect=dialect,
        dataset_id=_integer(entry.get("dataset_id")),
        columns=tuple(columns),
    )


def _identifier(value: Any) -> str | None:
    # Whitespace is *not* normalized here: a name containing a newline or a tab is
    # a different name, and collapsing it would silently retarget the query.
    text = str(value or "").strip()
    if not text or len(text) > MAX_NAME_CHARS:
        return None
    if not _SAFE_IDENTIFIER.match(text):
        return None
    return text


def _plain(value: Any, limit: int = 255) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _dialect(value: Any) -> str | None:
    """Normalize an engine name to a sqlglot dialect, or refuse it."""
    raw = str(value or "").strip().lower()
    base = raw.split("://", 1)[0].split("+", 1)[0]
    candidate = _DIALECT_ALIASES.get(base, base)
    return candidate if candidate in SUPPORTED_DIALECTS else None


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fingerprint(datasets: list[Dataset] | tuple[Dataset, ...]) -> str:
    canonical = json.dumps(
        [
            {
                "name": dataset.name,
                "schema": dataset.schema,
                "database": dataset.database,
                "dialect": dataset.dialect,
                "columns": [
                    [column.name, column.type, column.temporal]
                    for column in dataset.columns
                ],
            }
            for dataset in datasets
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
