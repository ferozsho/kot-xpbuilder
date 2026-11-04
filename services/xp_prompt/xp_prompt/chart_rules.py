"""Rule-based chart candidates, and the reasons the others were dropped (P7-F03).

The recommendation baseline in the plan is a set of *guards*: a rule only fires when the
profile can actually support the encoding, and every rule that fires says why. Two
properties matter more than cleverness:

* **nothing is recommended that cannot render.** A candidate is only produced when the
  viz type is available in this deployment *and* its encoded controls pass the contract
  check (P7-F01/P7-F05). Elimination is therefore a first-class output, not a silence.
* **the deterministic answer comes first.** The ordered candidates are usable with no
  model at all; AI ranking (P7-F04) may only reorder them and must never add one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from xp_prompt import chart_catalog, chart_profile

#: Order in which families are considered; earlier wins when scores tie.
FAMILY_PRIORITY: tuple[str, ...] = (
    "trend",
    "comparison",
    "part_to_whole",
    "matrix",
    "correlation",
    "single_value",
    "detail",
)

MAX_CANDIDATES = 5
MAX_ELIMINATED = 12
DEFAULT_ROW_LIMIT = 1_000
#: More distinct periods than this and a line is unreadable at chart width.
MAX_TREND_POINTS = 400
#: Fewer than this and "over time" is not a trend, it is two dots.
MIN_TREND_POINTS = 3


@dataclass(frozen=True, slots=True)
class ChartCandidate:
    """One proposed visualization: type, encoded controls, and why."""

    id: str
    viz_type: str
    family: str
    title: str
    form_data: dict[str, Any]
    rationale: str
    score: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)
    round_trip: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "viz_type": self.viz_type,
            "family": self.family,
            "title": self.title,
            "form_data": dict(self.form_data),
            "rationale": self.rationale,
            "score": round(self.score, 4),
            "factors": {key: round(value, 4) for key, value in self.factors.items()},
            "round_trip": self.round_trip,
        }


@dataclass(frozen=True, slots=True)
class Elimination:
    """A visualization that was considered and refused, with the reason."""

    viz_type: str
    family: str
    reason: str

    def to_public_dict(self) -> dict[str, Any]:
        return {"viz_type": self.viz_type, "family": self.family, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class Recommendation:
    """What the rules concluded, in a shape a caller can render or explain."""

    candidates: tuple[ChartCandidate, ...] = ()
    eliminated: tuple[Elimination, ...] = ()
    profile_fingerprint: str = ""

    @property
    def empty(self) -> bool:
        return not self.candidates

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "candidates": [candidate.to_public_dict() for candidate in self.candidates],
            "eliminated": [entry.to_public_dict() for entry in self.eliminated],
            "profile_fingerprint": self.profile_fingerprint,
            "count": len(self.candidates),
        }


def recommend(
    profile: chart_profile.DatasetProfile,
    catalog: chart_catalog.Catalog,
    *,
    row_limit: int = DEFAULT_ROW_LIMIT,
) -> Recommendation:
    """Rank the visualizations this profile supports, deterministically."""
    if not profile.fields:
        return Recommendation(profile_fingerprint=profile.fingerprint)

    found: list[ChartCandidate] = []
    eliminated: list[Elimination] = []
    temporal = profile.temporal()
    measure = profile.measure()
    categories = profile.categoricals
    numerics = [field for field in profile.numerics if not field.is_identifier]

    _trend(found, eliminated, catalog, profile, temporal, measure, row_limit=row_limit)
    _comparison(found, eliminated, catalog, profile, categories, measure, row_limit=row_limit)
    _part_to_whole(found, eliminated, catalog, profile, categories, measure, row_limit=row_limit)
    _matrix(found, eliminated, catalog, profile, categories, measure, row_limit=row_limit)
    _correlation(found, eliminated, catalog, profile, numerics, row_limit=row_limit)
    _single_value(found, eliminated, catalog, profile, measure, row_limit=row_limit)
    _detail(found, eliminated, catalog, profile, categories, numerics, row_limit=row_limit)

    found.sort(
        key=lambda candidate: (
            -candidate.score,
            _priority(candidate.family),
            candidate.viz_type,
        )
    )
    top = found[:MAX_CANDIDATES]
    # The table is the safe fallback for any shape, so the cap never drops it: a
    # caller that dislikes every recommendation still has something that renders.
    fallback = next(
        (candidate for candidate in found if candidate.family == "detail"), None
    )
    if fallback is not None and fallback not in top:
        top = [*top, fallback]
    return Recommendation(
        candidates=tuple(top),
        eliminated=tuple(eliminated[:MAX_ELIMINATED]),
        profile_fingerprint=profile.fingerprint,
    )


def adhoc_metric(column: str, aggregate: str = "SUM") -> dict[str, Any]:
    """A metric Superset can actually run: an aggregate over a named column.

    A bare column name in ``metrics`` would be looked up as a *saved* metric and fail, so
    every candidate carries an adjacency-free adhoc definition instead.
    """
    label = f"{aggregate}({column})"
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": column},
        "aggregate": aggregate,
        "label": label,
        "hasCustomLabel": True,
    }


# --- the rules -------------------------------------------------------------


def _trend(
    found: list[ChartCandidate],
    eliminated: list[Elimination],
    catalog: chart_catalog.Catalog,
    profile: chart_profile.DatasetProfile,
    temporal: chart_profile.Field | None,
    measure: chart_profile.Field | None,
    *,
    row_limit: int,
) -> None:
    if temporal is None or measure is None:
        return
    points = temporal.cardinality
    if points is not None and points < MIN_TREND_POINTS:
        eliminated.append(Elimination("echarts_timeseries_line", "trend", "too_few_periods"))
        return
    if points is not None and points > MAX_TREND_POINTS:
        eliminated.append(Elimination("echarts_timeseries_line", "trend", "too_many_periods"))
        return
    form = {
        "x_axis": temporal.name,
        "metrics": [adhoc_metric(measure.name)],
        "time_grain_sqla": _grain_for(temporal),
        "row_limit": row_limit,
    }
    continuity = 1.0 if points is None else min(1.0, max(0.0, points / 24.0))
    _add(
        found,
        catalog,
        chart_catalog.VIZ_CONTRACTS["echarts_timeseries_line"].viz_type,
        family="trend",
        title=f"{measure.name} over {temporal.name}",
        form_data=form,
        rationale=(
            f"{temporal.name} is a time axis with {_points(points)}, and {measure.name} "
            "is a measure, so the change over time is the story."
        ),
        score=0.55 + 0.25 * continuity + 0.1 * _freshness(measure),
        factors={"continuity": continuity, "measure": _freshness(measure)},
        eliminated=eliminated,
    )
    if _cumulative(measure):
        _add(
            found,
            catalog,
            "echarts_timeseries_area",
            family="trend",
            title=f"{measure.name} accumulated over {temporal.name}",
            form_data=dict(form),
            rationale=(
                f"{measure.name} accumulates, so an area makes the running total "
                "visible where a line only shows the slope."
            ),
            score=0.5 + 0.2 * continuity,
            factors={"continuity": continuity},
            eliminated=eliminated,
        )


def _comparison(
    found: list[ChartCandidate],
    eliminated: list[Elimination],
    catalog: chart_catalog.Catalog,
    profile: chart_profile.DatasetProfile,
    categories: tuple[chart_profile.Field, ...],
    measure: chart_profile.Field | None,
    *,
    row_limit: int,
) -> None:
    if measure is None:
        return
    if not categories:
        eliminated.append(Elimination("echarts_timeseries_bar", "comparison", "no_category"))
        return
    category = categories[0]
    if category.cardinality is not None and category.cardinality > chart_profile.HIGH_CARDINALITY:
        eliminated.append(
            Elimination("echarts_timeseries_bar", "comparison", "category_too_high_cardinality")
        )
        return
    readable = (
        1.0
        if category.cardinality is None
        else max(0.0, 1.0 - (category.cardinality / (chart_profile.MATRIX_CARDINALITY * 2)))
    )
    _add(
        found,
        catalog,
        "echarts_timeseries_bar",
        family="comparison",
        title=f"{measure.name} by {category.name}",
        form_data={
            "x_axis": category.name,
            "metrics": [adhoc_metric(measure.name)],
            "row_limit": row_limit,
        },
        rationale=(
            f"{category.name} is categorical with {_points(category.cardinality)}, so "
            f"bars compare {measure.name} across it directly."
        ),
        score=0.5 + 0.2 * readable,
        factors={"readability": readable},
        eliminated=eliminated,
    )


def _part_to_whole(
    found: list[ChartCandidate],
    eliminated: list[Elimination],
    catalog: chart_catalog.Catalog,
    profile: chart_profile.DatasetProfile,
    categories: tuple[chart_profile.Field, ...],
    measure: chart_profile.Field | None,
    *,
    row_limit: int,
) -> None:
    if measure is None or not categories:
        return
    category = min(
        (field for field in categories if field.cardinality is not None),
        key=lambda field: field.cardinality or 0,
        default=None,
    )
    if category is None:
        eliminated.append(Elimination("pie", "part_to_whole", "cardinality_unknown"))
        return
    if (category.cardinality or 0) > chart_profile.PART_TO_WHOLE_CARDINALITY:
        eliminated.append(Elimination("pie", "part_to_whole", "too_many_slices"))
        return
    _add(
        found,
        catalog,
        "pie",
        family="part_to_whole",
        title=f"{measure.name} share by {category.name}",
        form_data={
            "groupby": [category.name],
            "metric": adhoc_metric(measure.name),
            "row_limit": row_limit,
            "show_legend": True,
        },
        rationale=(
            f"{category.name} has only {category.cardinality} values, so the parts of "
            f"{measure.name} add up to a whole worth showing."
        ),
        score=0.45,
        factors={"slices": 1.0 - ((category.cardinality or 0) / 7.0)},
        eliminated=eliminated,
    )


def _matrix(
    found: list[ChartCandidate],
    eliminated: list[Elimination],
    catalog: chart_catalog.Catalog,
    profile: chart_profile.DatasetProfile,
    categories: tuple[chart_profile.Field, ...],
    measure: chart_profile.Field | None,
    *,
    row_limit: int,
) -> None:
    sized = [
        field
        for field in categories
        if field.cardinality is not None
        and 1 < (field.cardinality or 0) <= chart_profile.MATRIX_CARDINALITY
    ]
    if measure is None or len(sized) < 2:
        return
    first, second = sized[0], sized[1]
    _add(
        found,
        catalog,
        "heatmap_v2",
        family="matrix",
        title=f"{measure.name} across {first.name} and {second.name}",
        form_data={
            "x_axis": first.name,
            "groupby": [second.name],
            "metric": adhoc_metric(measure.name),
            "row_limit": row_limit,
        },
        rationale=(
            f"Two categories with {first.cardinality} and {second.cardinality} values "
            f"form a matrix where {measure.name} can be read as intensity."
        ),
        score=0.4,
        factors={"shape": 0.5},
        eliminated=eliminated,
    )


def _correlation(
    found: list[ChartCandidate],
    eliminated: list[Elimination],
    catalog: chart_catalog.Catalog,
    profile: chart_profile.DatasetProfile,
    numerics: list[chart_profile.Field],
    *,
    row_limit: int,
) -> None:
    if len(numerics) < 2:
        return
    first, second = numerics[0], numerics[1]
    _add(
        found,
        catalog,
        "echarts_timeseries_scatter",
        family="correlation",
        title=f"{first.name} against {second.name}",
        form_data={"x_axis": first.name, "metrics": [adhoc_metric(second.name)], "row_limit": row_limit},
        rationale=(
            f"Two measures ({first.name}, {second.name}) are better compared as points "
            "than as two series on one axis."
        ),
        score=0.35,
        factors={"measures": 1.0},
        eliminated=eliminated,
    )


def _single_value(
    found: list[ChartCandidate],
    eliminated: list[Elimination],
    catalog: chart_catalog.Catalog,
    profile: chart_profile.DatasetProfile,
    measure: chart_profile.Field | None,
    *,
    row_limit: int,
) -> None:
    if measure is None:
        return
    _add(
        found,
        catalog,
        "big_number_total",
        family="single_value",
        title=str(measure.name),
        form_data={"metric": adhoc_metric(measure.name)},
        rationale=(
            f"A single measure is most legible as one number; {measure.name} is the "
            "only measure in this dataset."
        ),
        score=0.3,
        factors={"measures": 1.0},
        eliminated=eliminated,
    )


def _detail(
    found: list[ChartCandidate],
    eliminated: list[Elimination],
    catalog: chart_catalog.Catalog,
    profile: chart_profile.DatasetProfile,
    categories: tuple[chart_profile.Field, ...],
    numerics: list[chart_profile.Field],
    *,
    row_limit: int,
) -> None:
    """The table is always safe, and it is the honest answer for wide or detailed data."""
    groupby = [field.name for field in categories[:4]]
    metrics = [adhoc_metric(field.name) for field in numerics[:4]]
    high = any(
        field.cardinality is not None and field.cardinality > chart_profile.HIGH_CARDINALITY
        for field in categories
    )
    form: dict[str, Any] = {"row_limit": row_limit}
    if groupby:
        form["groupby"] = groupby
    if metrics:
        form["metrics"] = metrics
    _add(
        found,
        catalog,
        "table",
        family="detail",
        title="Detail table",
        form_data=form,
        rationale=(
            "This dataset has many distinct values, so a table keeps every row legible "
            "where an axis would collapse them."
            if high
            else "A table is the safe default: it renders any combination of fields."
        ),
        score=0.6 if high else 0.2,
        factors={"detail": 1.0 if high else 0.2},
        eliminated=eliminated,
    )


# --- helpers ---------------------------------------------------------------


def _add(
    found: list[ChartCandidate],
    catalog: chart_catalog.Catalog,
    viz_type: str,
    *,
    family: str,
    title: str,
    form_data: dict[str, Any],
    rationale: str,
    score: float,
    factors: dict[str, float],
    eliminated: list[Elimination],
) -> None:
    """Add a candidate only if this deployment can actually render it (P7-F01/F05)."""
    ok, problems = catalog.validate(viz_type, form_data)
    if not ok:
        eliminated.append(
            Elimination(viz_type, family, ",".join(problems) or "unsupported_controls")
        )
        return
    found.append(
        ChartCandidate(
            id=f"{family}:{viz_type}",
            viz_type=viz_type,
            family=family,
            title=title,
            form_data={**form_data, "viz_type": viz_type},
            rationale=rationale,
            score=round(min(1.0, max(0.0, score)), 4),
            factors=dict(factors),
        )
    )


def _priority(family: str) -> int:
    return FAMILY_PRIORITY.index(family) if family in FAMILY_PRIORITY else len(
        FAMILY_PRIORITY
    )


def _grain_for(field: chart_profile.Field) -> str:
    """A time grain that suits the declared type: dates by day, timestamps by month."""
    return "P1D" if field.type == "date" else "P1M"


def _cumulative(field: chart_profile.Field) -> bool:
    lowered = field.name.lower()
    return any(hint in lowered for hint in ("total", "cumulative", "running", "count"))


def _freshness(field: chart_profile.Field) -> float:
    return 0.0 if field.null_share is None else round(1.0 - min(1.0, field.null_share), 4)


def _points(cardinality: int | None) -> str:
    return "an unknown number of values" if cardinality is None else f"{cardinality} values"
