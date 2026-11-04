"""Data-quality rules for a dataset draft (P6-F04).

A dataset that saves successfully and then answers every chart with empty or
duplicated numbers is worse than one that refuses to save. These rules run on the
*preview profile* — what Superset measured about the sample — and produce stable codes
an operator or a policy can act on.

They are deliberately cheap and deterministic, and they never see a row:

* the proxy measures (row count, per-column nulls, distinct counts, observed types);
* this module decides. That keeps the rule set testable without a database, and keeps
  the decision in the service that owns policy.

Severity is the policy surface: ``blocking`` stops the save unless the caller confirms
(``requires_confirmation``), ``warning`` is surfaced and recorded with the draft.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from xp_prompt import dataset_spec

BLOCKING = "blocking"
WARNING = "warning"
INFO = "info"
SEVERITIES: tuple[str, ...] = (BLOCKING, WARNING, INFO)

#: Share of a column's rows that may be null before it is called out.
NULL_SHARE_WARNING = 0.2
#: Share above which a column is "mostly empty" and blocks by default.
NULL_SHARE_BLOCKING = 0.9
#: A column with this many distinct values or fewer is a constant for charting.
CONSTANT_DISTINCT = 1
#: A "duplicate grain" share above this blocks: the grain does not identify a row.
DUPLICATE_GRAIN_BLOCKING = 0.01

MAX_PROFILE_COLUMNS = 200
MAX_WARNINGS = 25


@dataclass(frozen=True, slots=True)
class Warning:
    code: str
    severity: str
    column: str = ""
    message: str = ""
    detail: float | int | str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "column": self.column,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class QualityReport:
    """The verdict on a draft's sample, and why."""

    ok: bool
    blocking: bool
    requires_confirmation: bool
    row_count: int
    sampled: bool
    checked_columns: int
    warnings: tuple[Warning, ...] = field(default_factory=tuple)
    duplicate_grain: int = 0

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blocking": self.blocking,
            "requires_confirmation": self.requires_confirmation,
            "row_count": self.row_count,
            "sampled": self.sampled,
            "checked_columns": self.checked_columns,
            "duplicate_grain": self.duplicate_grain,
            "codes": [warning.code for warning in self.warnings],
            "warnings": [warning.to_public_dict() for warning in self.warnings],
        }


def check(
    spec: dataset_spec.DatasetSpec,
    profile: Any,
    *,
    confirmed: bool = False,
) -> QualityReport:
    """Judge a draft against its preview profile.

    ``profile`` is what the proxy measured:
    ``{row_count, sampled, duplicate_grain, columns: [{name, null_count,
    distinct_count, observed_type, invalid_dates}]}``. A missing value is *unknown*,
    never zero — an unmeasured check is skipped instead of passing silently.
    """
    body = profile if isinstance(profile, dict) else {}
    row_count = _number(body.get("row_count"))
    sampled = bool(body.get("sampled"))
    duplicate_grain = _number(body.get("duplicate_grain"))
    measured = {entry.get("name"): entry for entry in _entries(body.get("columns"))}

    warnings: list[Warning] = []
    if row_count == 0:
        warnings.append(
            Warning(
                "empty_result",
                BLOCKING,
                message="the definition returns no rows, so a dataset from it would "
                "answer nothing",
            )
        )
    if spec.grain:
        declared = {name.lower() for name in spec.column_names}
        missing = [name for name in spec.grain if name.lower() not in declared]
        if missing:
            warnings.append(
                Warning(
                    "grain_not_in_columns",
                    BLOCKING,
                    ",".join(missing),
                    "the declared grain is not part of the dataset",
                )
            )
        if duplicate_grain and row_count:
            share = duplicate_grain / row_count
            if share > DUPLICATE_GRAIN_BLOCKING:
                warnings.append(
                    Warning(
                        "duplicate_grain",
                        BLOCKING,
                        ",".join(spec.grain),
                        "the grain does not identify a row in the sample",
                        round(share, 4),
                    )
                )
    elif row_count:
        warnings.append(
            Warning(
                "no_grain",
                WARNING,
                message="no grain is declared, so duplicate detection is skipped",
            )
        )

    for column in spec.columns:
        entry = measured.get(column.name)
        if not isinstance(entry, dict):
            continue
        null_count = _number(entry.get("null_count"))
        if null_count is not None and row_count:
            share = null_count / row_count
            if share >= NULL_SHARE_BLOCKING:
                warnings.append(
                    Warning(
                        "column_mostly_null",
                        BLOCKING,
                        column.name,
                        "almost every value in the sample is null",
                        round(share, 4),
                    )
                )
            elif share > NULL_SHARE_WARNING:
                warnings.append(
                    Warning(
                        "column_null_heavy",
                        WARNING,
                        column.name,
                        "a large share of the sample is null",
                        round(share, 4),
                    )
                )
        distinct = _number(entry.get("distinct_count"))
        if distinct is not None and distinct <= CONSTANT_DISTINCT and row_count > 1:
            warnings.append(
                Warning(
                    "column_constant",
                    WARNING,
                    column.name,
                    "the column has a single distinct value in the sample",
                    distinct,
                )
            )
        observed = dataset_spec.normalise_type(str(entry.get("observed_type") or ""))
        if observed != "other" and column.type != "other" and observed != column.type:
            warnings.append(
                Warning(
                    "type_drift",
                    WARNING,
                    column.name,
                    f"declared {column.type} but the sample reads as {observed}",
                )
            )
        invalid_dates = _number(entry.get("invalid_dates"))
        if invalid_dates:
            severity = BLOCKING if column.name == spec.temporal_column else WARNING
            warnings.append(
                Warning(
                    "invalid_dates",
                    severity,
                    column.name,
                    "some values are not valid dates",
                    invalid_dates,
                )
            )

    for name in spec.grain or ():
        entry = measured.get(name)
        if isinstance(entry, dict) and _number(entry.get("null_count")):
            warnings.append(
                Warning(
                    "null_in_grain",
                    BLOCKING,
                    name,
                    "the grain column contains nulls, so rows cannot be identified",
                    _number(entry.get("null_count")),
                )
            )

    for metric in spec.metrics:
        if not _metric_numeric(metric, spec):
            warnings.append(
                Warning(
                    "metric_not_numeric",
                    WARNING,
                    metric.name,
                    "the metric does not aggregate a numeric column",
                )
            )

    blocking = any(warning.severity == BLOCKING for warning in warnings)
    capped = tuple(warnings[:MAX_WARNINGS])
    return QualityReport(
        ok=not blocking,
        blocking=blocking,
        requires_confirmation=bool(warnings),
        row_count=int(row_count or 0),
        sampled=sampled,
        checked_columns=len(spec.columns),
        warnings=capped,
        duplicate_grain=int(duplicate_grain or 0),
    )


def _metric_numeric(metric: dataset_spec.DatasetMetric, spec: dataset_spec.DatasetSpec) -> bool:
    """A metric is plausible when it aggregates a numeric column, or is arithmetic.

    Counting is always plausible (``COUNT(*)``), and a ratio of two metrics is, so the
    rule only fires on an aggregate applied to a column this dataset knows to be
    non-numeric.
    """
    expression = (metric.expression or "").strip().lower()
    if not expression:
        return False
    if "count(" in expression:
        return True
    for column in spec.columns:
        if column.name.lower() not in expression:
            continue
        if column.is_numeric or column.is_temporal:
            return True
    return not any(
        column.name.lower() in expression and not column.is_numeric
        for column in spec.columns
    )


def _entries(raw: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, (list, tuple)) else ():
        if isinstance(entry, dict) and entry.get("name"):
            entries.append(entry)
        if len(entries) >= MAX_PROFILE_COLUMNS:
            break
    return entries


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None
