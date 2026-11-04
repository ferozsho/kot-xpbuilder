"""What the dataset's fields are, before anything is recommended (P7-F02).

A chart recommendation is only as good as the field profile behind it: "is this column a
date, a category, or a measure" decides every rule that follows. So the profile is
explicit, deterministic, and cached by fingerprint — the same dataset columns always
produce the same profile, and a changed schema produces a different one.

Three rules shape it:

* **the declared type wins over the name.** A column typed ``timestamp`` is temporal even
  if it is called ``month_id``; a column typed ``text`` is not temporal even if it is
  called ``created_at``. Name heuristics only fill gaps the type leaves.
* **cardinality comes from the sample, when there is one.** Without a measured
  cardinality a categorical column is *unranked* rather than assumed small, because
  "pie chart over 4 000 values" is exactly the mistake this phase exists to prevent.
* **geography is a hint, not a promise.** A name containing ``country`` makes a map
  candidate plausible; it does not make the values resolvable, so it never outranks a
  safer choice on its own.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from xp_prompt import dataset_spec

#: What a field is, for the purpose of choosing a visualization.
TEMPORAL = "temporal"
CATEGORICAL = "categorical"
NUMERIC = "numeric"
BOOLEAN = "boolean"
OTHER = "other"
KINDS: tuple[str, ...] = (TEMPORAL, CATEGORICAL, NUMERIC, BOOLEAN, OTHER)

#: Cardinality thresholds the rules use. Below ``LOW`` a part-to-whole chart is sane;
#: above ``HIGH`` a table beats an axis.
LOW_CARDINALITY = 12
PART_TO_WHOLE_CARDINALITY = 6
MATRIX_CARDINALITY = 25
HIGH_CARDINALITY = 200

MAX_FIELDS = 200
MAX_NAME_CHARS = 128
MAX_PROFILE_AGE_SECONDS = 3_600

_GEO_HINTS: tuple[str, ...] = (
    "country",
    "state",
    "province",
    "region",
    "city",
    "district",
    "county",
    "zip",
    "postcode",
    "latitude",
    "longitude",
    "lat",
    "lon",
    "lng",
    "geo",
)
_ID_HINTS: tuple[str, ...] = ("id", "uuid", "guid", "key", "pk")


@dataclass(frozen=True, slots=True)
class Field:
    """One dataset column, as the rules need to see it."""

    name: str
    type: str
    kind: str
    cardinality: int | None = None
    null_share: float | None = None
    is_geographic: bool = False
    is_identifier: bool = False
    is_temporal: bool = False
    is_numeric: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "kind": self.kind,
            "cardinality": self.cardinality,
            "null_share": self.null_share,
            "is_geographic": self.is_geographic,
            "is_identifier": self.is_identifier,
        }


@dataclass(frozen=True, slots=True)
class DatasetProfile:
    """The fields of one dataset, plus the aggregates the rules read."""

    fields: tuple[Field, ...] = ()
    row_count: int = 0
    grain: tuple[str, ...] = ()
    fingerprint: str = ""

    def of(self, kind: str) -> tuple[Field, ...]:
        return tuple(field for field in self.fields if field.kind == kind)

    @property
    def temporals(self) -> tuple[Field, ...]:
        return self.of(TEMPORAL)

    @property
    def numerics(self) -> tuple[Field, ...]:
        return self.of(NUMERIC)

    @property
    def categoricals(self) -> tuple[Field, ...]:
        """Categories worth an axis: not identifiers, not constant, not high-cardinality."""
        found = [field for field in self.of(CATEGORICAL) if not field.is_identifier]
        return tuple(
            field
            for field in found
            if field.cardinality is None or field.cardinality >= 2
        )

    def temporal(self) -> Field | None:
        """The best time axis: the first declared temporal column, else a hint match."""
        if self.temporals:
            return self.temporals[0]
        return None

    def measure(self) -> Field | None:
        """The best measure: prefer a non-identifier numeric with the widest signal."""
        candidates = [field for field in self.numerics if not field.is_identifier]
        if not candidates:
            return None
        candidates.sort(key=lambda field: (field.cardinality or 0), reverse=True)
        return candidates[0]

    def is_low_cardinality(self, field: Field) -> bool:
        return field.cardinality is not None and field.cardinality <= LOW_CARDINALITY

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "row_count": self.row_count,
            "grain": list(self.grain),
            "fingerprint": self.fingerprint,
            "fields": [field.to_public_dict() for field in self.fields],
        }


def profile(
    columns: Any,
    *,
    measured: Any = None,
    row_count: int = 0,
    grain: Any = None,
) -> DatasetProfile:
    """Build a field profile from declared columns plus whatever was measured.

    ``measured`` is the per-column aggregate the proxy already collects for the dataset
    quality rules (``{name, null_count, distinct_count, observed_type}``), so this phase
    costs no extra query.
    """
    stats = _stats(measured)
    fields: list[Field] = []
    seen: set[str] = set()
    for entry in columns if isinstance(columns, (list, tuple)) else ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()[:MAX_NAME_CHARS]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        stats_for_column = stats.get(name.lower(), {})
        observed = dataset_spec.normalise_type(
            str(stats_for_column.get("observed_type") or entry.get("type") or "")
        )
        declared = dataset_spec.normalise_type(str(entry.get("type") or ""))
        effective = observed if observed != "other" else declared
        field = _field(
            name=name,
            declared=declared,
            effective=effective,
            role=str(entry.get("role") or "").strip().lower(),
            stats=stats_for_column,
            row_count=row_count,
        )
        fields.append(field)
        if len(fields) >= MAX_FIELDS:
            break
    built = DatasetProfile(
        fields=tuple(fields),
        row_count=max(0, int(row_count or 0)),
        grain=_grain(grain),
    )
    return _with_fingerprint(built)


def fingerprint_of(profile_value: DatasetProfile) -> str:
    """A content-free identity for "this dataset shape", used to cache a profile."""
    material = {
        "fields": [
            [field.name.lower(), field.type, field.kind, field.cardinality]
            for field in profile_value.fields
        ],
        "grain": [name.lower() for name in profile_value.grain],
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _field(
    *,
    name: str,
    declared: str,
    effective: str,
    role: str,
    stats: dict[str, Any],
    row_count: int,
) -> Field:
    lowered = name.lower()
    is_numeric = effective in dataset_spec.NUMERIC_TYPES
    identity = _looks_like_identity(lowered)
    if effective == "date" or effective == "datetime":
        kind = TEMPORAL
    elif role == "temporal":
        kind = TEMPORAL
    elif effective == "boolean":
        kind = BOOLEAN
    elif identity:
        # An identifier can group and filter, but summing it is meaningless — so it is
        # a category, never a measure, whatever its type.
        kind = CATEGORICAL
    elif is_numeric:
        kind = NUMERIC
    elif effective in {"text", "other"}:
        kind = CATEGORICAL
    else:
        kind = OTHER

    cardinality = _cardinality(stats)
    null_share = _null_share(stats, row_count)
    return Field(
        name=name,
        type=effective,
        kind=kind,
        cardinality=cardinality,
        null_share=null_share,
        is_geographic=any(hint in lowered for hint in _GEO_HINTS),
        is_identifier=_looks_like_identity(lowered),
        is_temporal=kind == TEMPORAL,
        is_numeric=kind == NUMERIC,
    )


def _looks_like_identity(lowered: str) -> bool:
    return lowered in _ID_HINTS or lowered.endswith("_id") or lowered.endswith("_key")


def _stats(measured: Any) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for entry in measured if isinstance(measured, (list, tuple)) else ():
        if isinstance(entry, dict) and entry.get("name"):
            found[str(entry["name"]).lower()] = entry
    return found


def _cardinality(stats: dict[str, Any]) -> int | None:
    value = stats.get("distinct_count")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _null_share(stats: dict[str, Any], row_count: int) -> float | None:
    value = stats.get("null_count")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or row_count <= 0:
        return None
    return round(float(value) / float(row_count), 4)


def _grain(value: Any) -> tuple[str, ...]:
    names: list[str] = []
    for entry in value if isinstance(value, (list, tuple)) else ():
        text = str(entry or "").strip()[:MAX_NAME_CHARS]
        if text and text not in names:
            names.append(text)
        if len(names) >= 10:
            break
    return tuple(names)


def _with_fingerprint(value: DatasetProfile) -> DatasetProfile:
    from dataclasses import replace

    return replace(value, fingerprint=fingerprint_of(value))
