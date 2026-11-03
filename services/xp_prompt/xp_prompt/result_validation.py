"""Result validation and preview blocks (P4-F10).

"Success" for an analytics answer is not "the statement executed". A result is
usable only if it has columns, has a shape the caller asked for, and does not leak
material it should not show. This module is the single place that decides that,
and the single place that turns a raw result into a ``result_preview`` block
(ADR-0006), so the proxy that executed the SQL never invents a shape.

Rules, and why each exists:

* **no columns** — a payload with no header is not an answer; it is refused.
* **wide results** — the first :data:`MAX_COLUMNS` columns are shown; the rest are
  dropped with a warning rather than silently truncated mid-row.
* **empty results** — zero rows is a valid answer ("none"), surfaced with a
  warning so a reader does not think the query failed.
* **all-null columns** — flagged per column; usually a wrong join or a wrong
  column, which a reader should see.
* **sensitive headers** — a column whose name looks like a credential is masked
  in the preview and reported. The value never reaches the browser or a stored
  transcript, even if a model selected it.
* **no measure** — a chart/dashboard intent with no numeric column cannot be
  charted; that is reported instead of drawn.
* **row/truncation honesty** — ``row_count`` is the real count and
  ``returned`` the preview size, so a capped preview is never mistaken for the
  complete result.

Blocking failures raise :class:`ResultValidationError` with a stable code;
everything else is reported as an issue and a warning block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from xp_prompt import responses

MAX_PREVIEW_ROWS = 200
MAX_COLUMNS = 30
MAX_CELL_CHARS = 200
MIN_CELL_CHARS = 1

COLUMN = "column"
ROW = "row"

#: Column names that mean "do not show this to anyone".
SENSITIVE_HEADER = re.compile(
    r"(?i)\b(pass(word)?|passwd|secret|token|api[_-]?key|credential|"
    r"authorization|private[_-]?key|salt)\b"
)
REDACTED = "[redacted]"
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


class ResultValidationError(ValueError):
    """The result cannot be shown at all; ``code`` is stable and content-free."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ResultVerdict:
    ok: bool
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    row_count: int
    returned: int
    truncated: bool
    issues: tuple[str, ...] = ()
    blocks: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "columns": list(self.columns),
            "rows": [list(row) for row in self.rows],
            "row_count": self.row_count,
            "returned": self.returned,
            "truncated": self.truncated,
            "issues": list(self.issues),
            "blocks": list(self.blocks),
        }


def validate_result(
    columns: Any,
    rows: Any,
    *,
    row_count: int | None = None,
    truncated: bool = False,
    intent: str | None = None,
    max_rows: int = MAX_PREVIEW_ROWS,
    max_columns: int = MAX_COLUMNS,
) -> ResultVerdict:
    """Normalize a raw result, decide whether it is usable, and build its blocks."""
    clean_columns = _columns(columns)
    if not clean_columns:
        raise ResultValidationError("no_columns", "the result has no columns")

    issues: list[str] = []
    warnings: list[str] = []

    if len(clean_columns) > max_columns:
        dropped = len(clean_columns) - max_columns
        clean_columns = clean_columns[:max_columns]
        issues.append("wide_result")
        warnings.append(f"{dropped} additional column(s) were left out of the preview.")

    preview, input_total = _rows(rows, len(clean_columns), max(1, int(max_rows)))
    real_count = int(row_count) if isinstance(row_count, int) else 0
    real_count = max(real_count, input_total, len(preview))
    capped = truncated or real_count > len(preview)

    masked = [
        index
        for index, name in enumerate(clean_columns)
        if SENSITIVE_HEADER.search(name)
    ]
    if masked:
        preview = [
            tuple(REDACTED if index in masked else value for index, value in enumerate(row))
            for row in preview
        ]
        issues.append("masked_columns")
        for index in masked:
            warnings.append(f"Column '{clean_columns[index]}' was masked as sensitive.")

    if not preview:
        issues.append("empty_result")
        warnings.append("The query returned no rows.")

    for index, name in enumerate(clean_columns):
        if index in masked:
            continue
        if preview and all(row[index] == "" for row in preview):
            issues.append(f"all_null:{name}")
            warnings.append(f"Column '{name}' is entirely empty.")

    if intent in {"chart", "dashboard"} and not _has_measure(clean_columns, preview):
        issues.append("no_measure")
        warnings.append("No numeric column was found, so this result cannot be charted.")

    if capped:
        issues.append("truncated")
        warnings.append(
            f"Showing {len(preview)} of {real_count} row(s); the rest is not included."
        )

    blocks = responses.normalize_blocks(
        [
            {
                "type": responses.RESULT_PREVIEW,
                "columns": list(clean_columns),
                "rows": [list(row) for row in preview],
                "row_count": real_count,
                "truncated": capped,
            },
            {"type": responses.WARNINGS, "items": warnings},
        ]
    )

    return ResultVerdict(
        ok=True,
        columns=tuple(clean_columns),
        rows=tuple(preview),
        row_count=real_count,
        returned=len(preview),
        truncated=capped,
        issues=tuple(issues),
        blocks=tuple(blocks),
    )


def _columns(columns: Any) -> list[str]:
    if not isinstance(columns, list | tuple):
        return []
    cleaned: list[str] = []
    for column in columns:
        name = " ".join(str(column or "").split())[:MAX_CELL_CHARS]
        if name:
            cleaned.append(name)
    return cleaned


def _rows(rows: Any, width: int, limit: int) -> tuple[list[tuple[str, ...]], int]:
    """Return the preview rows and how many well-formed rows arrived."""
    if not isinstance(rows, list | tuple):
        return [], 0
    usable = [row for row in rows if isinstance(row, list | tuple)]
    cleaned: list[tuple[str, ...]] = []
    for row in usable[:limit]:
        values = [_cell(value) for value in row[:width]]
        values.extend([""] * (width - len(values)))
        cleaned.append(tuple(values))
    return cleaned, len(usable)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if text in {"nan", "NaN", "NaT", "None"}:
        return ""
    return text[:MAX_CELL_CHARS]


def _has_measure(columns: list[str], rows: list[tuple[str, ...]]) -> bool:
    """Is there at least one column of numbers to plot on?"""
    for index in range(len(columns)):
        values = [row[index] for row in rows if row[index] != ""]
        if values and all(_NUMERIC.match(value) for value in values):
            return True
    return False
