"""Turn a goal into an explainable dashboard plan (P8-F01), reusing what exists (P8-F02).

The plan is a *proposal*, and every part of it has to be defensible without a model:

* **a chart is only proposed when it can actually render.** Reused charts already render;
  generated charts come from Phase 7's candidates, which were contract-checked against the
  deployed catalog. A candidate this deployment cannot draw is dropped here as well.
* **reuse beats regeneration.** An existing chart on a relevant dataset wins its family
  slot, so the plan never duplicates an artifact that is already saved (P8-F02).
* **silence is not an answer.** Datasets that matched the goal but have nothing to show
  produce an explicit :class:`ArtifactNeed` — that is the instruction to invoke Phases 6–7
  rather than a quietly thinner dashboard.
* **quality is measured, not asserted.** Diversity, coverage, redundancy and hierarchy are
  computed from the slots that survived, so a chart-heavy plan cannot claim breadth.

Keyword matching is the only goal-understanding available while the provider is
unavailable; when nothing matches, the plan says so in :attr:`Plan.notes` instead of
inventing intent.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from xp_prompt import chart_catalog

MAX_SLOTS = 8
MAX_DATASETS = 20
MAX_COLUMNS = 120
MAX_CANDIDATES_PER_DATASET = 8
MAX_TEXT = 160
#: Two charts from one dataset is a story; five is a dump.
MAX_CHARTS_PER_DATASET = 2
#: An existing chart is preferred on ties, but a strong recommendation may beat it.
REUSE_SCORE = 0.5

SECTION_ORDER: tuple[str, ...] = (
    "headline",
    "trend",
    "comparison",
    "matrix",
    "detail",
)

SECTION_TITLES: dict[str, str] = {
    "headline": "Headline",
    "trend": "Trends over time",
    "comparison": "Comparisons",
    "matrix": "Patterns and matrices",
    "detail": "Detail",
}

#: Which section a chart family belongs to; also the reading order of the dashboard.
FAMILY_SECTION: dict[str, str] = {
    "single_value": "headline",
    "trend": "trend",
    "correlation": "trend",
    "comparison": "comparison",
    "part_to_whole": "comparison",
    "matrix": "matrix",
    "detail": "detail",
}

FAMILY_LABELS: dict[str, str] = {
    "trend": "trend",
    "comparison": "comparison",
    "part_to_whole": "part-to-whole",
    "matrix": "matrix",
    "correlation": "correlation",
    "single_value": "single value",
    "detail": "detail",
}

#: The family each known viz type belongs to, so a saved chart can win a slot.
VIZ_FAMILY: dict[str, str] = {
    "echarts_timeseries_line": "trend",
    "echarts_timeseries_area": "trend",
    "echarts_timeseries_scatter": "correlation",
    "echarts_timeseries_bar": "comparison",
    "pie": "part_to_whole",
    "heatmap_v2": "matrix",
    "big_number_total": "single_value",
    "table": "detail",
}

_GRAIN_WORDS: tuple[tuple[str, str], ...] = (
    ("daily", "P1D"),
    ("weekly", "P1W"),
    ("monthly", "P1M"),
    ("quarterly", "P3M"),
    ("yearly", "P1Y"),
    ("annual", "P1Y"),
)

_AUDIENCE_WORDS: tuple[str, ...] = (
    "executive",
    "management",
    "operations",
    "finance",
    "sales",
    "teacher",
    "trainer",
    "student",
    "learner",
    "parent",
    "admin",
)

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "over",
        "across",
        "show",
        "give",
        "list",
        "want",
        "need",
        "please",
        "this",
        "that",
        "these",
        "those",
        "each",
        "per",
        "how",
        "what",
        "which",
        "when",
        "where",
        "all",
        "any",
        "top",
        "most",
        "dashboard",
        "report",
        "chart",
        "charts",
        "view",
        "see",
        "help",
        "make",
        "create",
        "build",
        "generate",
        "between",
        "about",
        "their",
        "there",
        "have",
    }
)

_WORD = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class Dataset:
    """A dataset the proxy says the principal may query."""

    id: int
    name: str
    columns: tuple[str, ...] = ()
    measures: tuple[str, ...] = ()
    relevance: float = 0.0

    @classmethod
    def from_payload(cls, entry: Any) -> "Dataset | None":
        if not isinstance(entry, Mapping):
            return None
        try:
            dataset_id = int(entry.get("id"))
        except (TypeError, ValueError):
            return None
        name = _text(entry.get("name"), MAX_TEXT) or f"dataset {dataset_id}"
        return cls(
            id=dataset_id,
            name=name,
            columns=_column_names(entry.get("columns")),
            measures=_column_names(entry.get("measures")),
        )


@dataclass(frozen=True, slots=True)
class Chart:
    """An existing saved chart, worth reusing when it fits the plan."""

    id: int
    name: str
    viz_type: str
    dataset_id: int

    @property
    def family(self) -> str:
        return VIZ_FAMILY.get(self.viz_type, "detail")

    @classmethod
    def from_payload(cls, entry: Any) -> "Chart | None":
        if not isinstance(entry, Mapping):
            return None
        raw_dataset = entry.get("dataset_id", entry.get("datasource_id"))
        try:
            chart_id = int(entry.get("id"))
            dataset_id = int(raw_dataset)
        except (TypeError, ValueError):
            return None
        viz_type = _text(entry.get("viz_type"), 64).lower().replace("-", "_")
        name = _text(entry.get("name") or entry.get("slice_name"), MAX_TEXT)
        return cls(
            id=chart_id,
            name=name or f"chart {chart_id}",
            viz_type=viz_type,
            dataset_id=dataset_id,
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    """A Phase 7 recommendation, already contract-checked by the proxy."""

    viz_type: str
    family: str
    title: str
    form_data: dict[str, Any]
    score: float
    rationale: str

    @classmethod
    def from_payload(cls, entry: Any) -> "Candidate | None":
        if not isinstance(entry, Mapping):
            return None
        viz_type = _text(entry.get("viz_type"), 64).lower().replace("-", "_")
        if not viz_type:
            return None
        family = _text(entry.get("family"), 32).lower()
        if not family:
            family = VIZ_FAMILY.get(viz_type, "detail")
        form_data = entry.get("form_data")
        try:
            score = float(entry.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        return cls(
            viz_type=viz_type,
            family=family,
            title=_text(entry.get("title"), MAX_TEXT),
            form_data=dict(form_data) if isinstance(form_data, Mapping) else {},
            score=max(0.0, min(score, 1.0)),
            rationale=_text(entry.get("rationale"), MAX_TEXT),
        )


@dataclass(frozen=True, slots=True)
class Slot:
    """One planned chart, and whether it exists or must be generated."""

    id: str
    title: str
    question: str
    family: str
    section: str
    dataset_id: int
    dataset_name: str
    viz_type: str
    form_data: dict[str, Any]
    source: str
    chart_id: int | None
    score: float
    rationale: str

    @property
    def needs_generation(self) -> bool:
        return self.chart_id is None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "question": self.question,
            "family": self.family,
            "section": self.section,
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "viz_type": self.viz_type,
            "form_data": dict(self.form_data),
            "source": self.source,
            "chart_id": self.chart_id,
            "score": round(self.score, 4),
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class Section:
    """A narrated block of the dashboard."""

    key: str
    title: str
    slots: tuple[Slot, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "slots": [slot.to_public_dict() for slot in self.slots],
        }


@dataclass(frozen=True, slots=True)
class ArtifactNeed:
    """A dataset that belongs in the dashboard but has nothing to show yet."""

    dataset_id: int
    dataset_name: str
    family: str
    reason: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "family": self.family,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Excluded:
    """A dataset that was considered and left out, with the reason."""

    dataset_id: int
    dataset_name: str
    reason: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """The proposed dashboard, in reading order, with its measured quality."""

    title: str
    goal: str = ""
    audience: str = "unspecified"
    sections: tuple[Section, ...] = ()
    needs: tuple[ArtifactNeed, ...] = ()
    excluded: tuple[Excluded, ...] = ()
    quality: Mapping[str, float] = dataclasses.field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def slots(self) -> tuple[Slot, ...]:
        return tuple(slot for section in self.sections for slot in section.slots)

    @property
    def empty(self) -> bool:
        return not self.slots

    @property
    def reuse_count(self) -> int:
        return sum(1 for slot in self.slots if not slot.needs_generation)

    @property
    def generate_count(self) -> int:
        return sum(1 for slot in self.slots if slot.needs_generation)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "goal": self.goal,
            "audience": self.audience,
            "sections": [section.to_public_dict() for section in self.sections],
            "slots": [slot.to_public_dict() for slot in self.slots],
            "needs": [need.to_public_dict() for need in self.needs],
            "excluded": [entry.to_public_dict() for entry in self.excluded],
            "quality": {key: round(value, 4) for key, value in self.quality.items()},
            "notes": list(self.notes),
            "count": len(self.slots),
            "reuse_count": self.reuse_count,
            "generate_count": self.generate_count,
        }


@dataclass(frozen=True, slots=True)
class _Option:
    """A way to fill a slot: an existing chart or a generated candidate."""

    family: str
    viz_type: str
    title: str
    form_data: dict[str, Any]
    score: float
    rationale: str
    source: str
    chart_id: int | None


def build(
    *,
    goal: str = "",
    datasets: Iterable[Any] = (),
    charts: Iterable[Any] = (),
    candidates: Mapping[Any, Iterable[Any]] | None = None,
    catalog: chart_catalog.Catalog | None = None,
    time_grain: str | None = None,
    max_slots: int = MAX_SLOTS,
) -> Plan:
    """Plan a dashboard from what the principal can already query."""
    goal_text = _text(goal, MAX_TEXT * 2)
    tokens = _tokens(goal_text)
    grain = _grain(goal_text) or _text(time_grain, 8) or None
    known = _datasets(datasets)
    ranked = sorted(
        (
            dataclasses.replace(dataset, relevance=_relevance(tokens, dataset))
            for dataset in known
        ),
        key=lambda dataset: (-dataset.relevance, dataset.name.lower(), dataset.id),
    )
    existing = _charts(charts, {dataset.id for dataset in known})
    proposed = _candidates(candidates, catalog)

    slots: list[Slot] = []
    needs: list[ArtifactNeed] = []
    excluded: list[Excluded] = []
    used_families: set[str] = set()
    taken_pairs: set[tuple[int, str]] = set()
    relevant: set[int] = set()

    for dataset in ranked:
        if len(slots) >= max_slots:
            excluded.append(Excluded(dataset.id, dataset.name, "slot_budget"))
            continue
        if dataset.relevance > 0:
            relevant.add(dataset.id)
        elif slots:
            # The goal named something else; adding this dataset would be padding.
            excluded.append(Excluded(dataset.id, dataset.name, "no_goal_match"))
            continue
        options = _options(
            dataset,
            existing.get(dataset.id, ()),
            proposed.get(dataset.id, ()),
            used_families,
            catalog,
            grain,
        )
        if not options:
            if dataset.relevance > 0:
                needs.append(
                    ArtifactNeed(
                        dataset_id=dataset.id,
                        dataset_name=dataset.name,
                        family="chart",
                        reason="no_existing_chart_and_no_recommendation",
                    )
                )
            else:
                excluded.append(Excluded(dataset.id, dataset.name, "no_chart_available"))
            continue
        taken = 0
        for option in options:
            if len(slots) >= max_slots or taken >= MAX_CHARTS_PER_DATASET:
                break
            pair = (dataset.id, option.family)
            if pair in taken_pairs:
                continue
            slots.append(_slot(dataset, option))
            taken_pairs.add(pair)
            used_families.add(option.family)
            taken += 1

    sections = _sections(slots)
    quality = _quality(slots, relevant, {dataset.id for dataset in known})
    notes = _notes(tokens, known, slots, needs, excluded, quality)
    return Plan(
        title=_plan_title(goal_text, ranked),
        goal=goal_text,
        audience=_audience(tokens),
        sections=sections,
        needs=tuple(needs),
        excluded=tuple(excluded),
        quality=quality,
        notes=notes,
    )


def _slot(dataset: Dataset, option: _Option) -> Slot:
    family = option.family if option.family in FAMILY_SECTION else "detail"
    title = option.title or f"{FAMILY_LABELS.get(family, family)} of {dataset.name}"
    return Slot(
        id=f"{dataset.id}:{family}",
        title=title,
        question=title,
        family=family,
        section=FAMILY_SECTION[family],
        dataset_id=dataset.id,
        dataset_name=dataset.name,
        viz_type=option.viz_type,
        form_data=dict(option.form_data),
        source=option.source,
        chart_id=option.chart_id,
        score=option.score,
        rationale=option.rationale,
    )


def _options(
    dataset: Dataset,
    existing: Iterable[Chart],
    proposed: Iterable[Candidate],
    used_families: set[str],
    catalog: chart_catalog.Catalog | None,
    grain: str | None,
) -> list[_Option]:
    """Every way to fill a slot for this dataset, best-first.

    Unused families come first: the point of the sort is a readable dashboard, not
    five variations of the same comparison.
    """
    options: list[_Option] = []
    for chart in existing:
        options.append(
            _Option(
                family=chart.family,
                viz_type=chart.viz_type,
                title=chart.name,
                form_data={},
                score=REUSE_SCORE,
                rationale="reuses a saved chart on this dataset",
                source="existing",
                chart_id=chart.id,
            )
        )
    for candidate in proposed:
        form_data = dict(candidate.form_data)
        if grain and _allows_grain(catalog, candidate.viz_type):
            form_data["time_grain_sqla"] = grain
        options.append(
            _Option(
                family=candidate.family,
                viz_type=candidate.viz_type,
                title=candidate.title,
                form_data=form_data,
                score=candidate.score,
                rationale=candidate.rationale,
                source="generate",
                chart_id=None,
            )
        )
    options.sort(
        key=lambda option: (
            option.family in used_families,
            -option.score,
            option.source != "existing",
            option.family,
            option.viz_type,
        )
    )
    return options


def _allows_grain(catalog: chart_catalog.Catalog | None, viz_type: str) -> bool:
    if catalog is None:
        return False
    contract = catalog.contract(viz_type)
    return contract is not None and "time_grain_sqla" in contract.allows


def _candidates(
    source: Mapping[Any, Iterable[Any]] | None,
    catalog: chart_catalog.Catalog | None,
) -> dict[int, tuple[Candidate, ...]]:
    """Group Phase 7 candidates by dataset, dropping what this deployment cannot draw."""
    grouped: dict[int, list[Candidate]] = {}
    for key, entries in (source or {}).items():
        try:
            dataset_id = int(key)
        except (TypeError, ValueError):
            continue
        kept: list[Candidate] = []
        for entry in entries or ():
            candidate = Candidate.from_payload(entry)
            if candidate is None:
                continue
            if catalog is not None:
                accepted, _ = catalog.validate(candidate.viz_type, candidate.form_data)
                if not accepted:
                    continue
            if len(kept) >= MAX_CANDIDATES_PER_DATASET:
                break
            kept.append(candidate)
        if kept:
            kept.sort(key=lambda item: (-item.score, item.family, item.viz_type))
            grouped[dataset_id] = tuple(kept)
    return grouped


def _datasets(entries: Iterable[Any]) -> tuple[Dataset, ...]:
    seen: set[int] = set()
    kept: list[Dataset] = []
    for entry in entries or ():
        dataset = Dataset.from_payload(entry)
        if dataset is None or dataset.id in seen:
            continue
        seen.add(dataset.id)
        kept.append(dataset)
        if len(kept) >= MAX_DATASETS:
            break
    return tuple(kept)


def _charts(entries: Iterable[Any], known: set[int]) -> dict[int, tuple[Chart, ...]]:
    """Index saved charts by dataset, keeping one per family so nothing is duplicated."""
    grouped: dict[int, dict[str, Chart]] = {}
    for entry in entries or ():
        chart = Chart.from_payload(entry)
        if chart is None or chart.dataset_id not in known:
            continue
        bucket = grouped.setdefault(chart.dataset_id, {})
        current = bucket.get(chart.family)
        if current is None or chart.id < current.id:
            bucket[chart.family] = chart
    return {
        dataset_id: tuple(sorted(bucket.values(), key=lambda item: (item.family, item.id)))
        for dataset_id, bucket in grouped.items()
    }


def _sections(slots: Iterable[Slot]) -> tuple[Section, ...]:
    grouped: dict[str, list[Slot]] = {key: [] for key in SECTION_ORDER}
    for slot in slots:
        grouped[slot.section].append(slot)
    return tuple(
        Section(key=key, title=SECTION_TITLES[key], slots=tuple(grouped[key]))
        for key in SECTION_ORDER
        if grouped[key]
    )


def _quality(
    slots: list[Slot],
    relevant: set[int],
    known: set[int],
) -> dict[str, float]:
    """Four numbers a reviewer can argue with, rather than a vague "looks good"."""
    if not slots:
        return {
            "diversity": 0.0,
            "coverage": 0.0,
            "redundancy": 1.0,
            "hierarchy": 0.0,
        }
    families = {slot.family for slot in slots}
    denominator = min(len(slots), len(FAMILY_LABELS))
    diversity = len(families) / denominator if denominator else 0.0
    used = {slot.dataset_id for slot in slots}
    scope = relevant or known
    coverage = len(used & scope) / len(scope) if scope else 0.0
    pairs = [(slot.dataset_id, slot.family) for slot in slots]
    duplicates = len(pairs) - len(set(pairs))
    redundancy = 1.0 - duplicates / len(pairs)
    has_headline = any(slot.section == "headline" for slot in slots)
    if len(slots) == 1:
        hierarchy = 0.4
    else:
        hierarchy = 1.0 if has_headline else 0.5
    return {
        "diversity": round(diversity, 4),
        "coverage": round(coverage, 4),
        "redundancy": round(redundancy, 4),
        "hierarchy": round(hierarchy, 4),
    }


def _notes(
    tokens: frozenset[str],
    known: tuple[Dataset, ...],
    slots: list[Slot],
    needs: list[ArtifactNeed],
    excluded: list[Excluded],
    quality: Mapping[str, float],
) -> tuple[str, ...]:
    notes: list[str] = []
    if not tokens and known:
        notes.append(
            "The goal named no dataset or column, so the plan was built from the "
            "artifacts that are available; confirm the intent before publishing."
        )
    if needs:
        notes.append(
            f"{len(needs)} goal-matched dataset(s) have no chart yet; generate them "
            "before the dashboard can be complete."
        )
    if any(entry.reason == "slot_budget" for entry in excluded):
        notes.append("The chart budget was reached; lower-priority datasets were left out.")
    if not slots and known:
        notes.append("No dataset could fill a slot, so this plan is empty by evidence.")
    if quality.get("diversity", 0.0) < 0.5 and len(slots) > 1:
        notes.append(
            "Most slots use the same chart family; the dashboard repeats one idea."
        )
    return tuple(notes)


def _plan_title(goal_text: str, ranked: tuple[Dataset, ...]) -> str:
    if goal_text:
        return goal_text[:1].upper() + goal_text[1:MAX_TEXT]
    if ranked:
        return f"{ranked[0].name} overview"
    return "Untitled dashboard"


def _audience(tokens: frozenset[str]) -> str:
    for word in _AUDIENCE_WORDS:
        if word in tokens:
            return word
    return "unspecified"


def _relevance(goal_tokens: frozenset[str], dataset: Dataset) -> float:
    if not goal_tokens:
        return 0.0
    haystack = _tokens(" ".join((dataset.name, *dataset.columns, *dataset.measures)))
    overlap = goal_tokens & haystack
    return round(len(overlap) / len(goal_tokens), 4)


def _grain(goal_text: str) -> str:
    text = goal_text.lower()
    for word, grain in _GRAIN_WORDS:
        if word in text:
            return grain
    return ""


def _tokens(text: str) -> frozenset[str]:
    words = _WORD.findall(str(text or "").lower())
    return frozenset(
        word for word in words if len(word) >= 3 and word not in _STOPWORDS
    )


def _column_names(value: Any) -> tuple[str, ...]:
    names: list[str] = []
    for entry in value if isinstance(value, (list, tuple)) else ():
        if isinstance(entry, Mapping):
            name = _text(entry.get("name") or entry.get("column_name"), MAX_TEXT)
        else:
            name = _text(entry, MAX_TEXT)
        if name and name not in names:
            names.append(name)
        if len(names) >= MAX_COLUMNS:
            break
    return tuple(names)


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit]
