"""Valid Superset position metadata and provable native filters (P8-F04/F05).

Two guarantees, both checkable without a database:

* **the layout validates before anything is saved.** Superset's ``position`` object is a
  graph, not a tree: every component lists its parents as well as its children, and a row
  may not exceed the twelve columns the grid renders. :func:`validate_position` walks the
  graph and reports every inconsistency, so a malformed layout fails here instead of in a
  browser with a blank dashboard.
* **a native filter is only proposed where compatibility is proven.** The proxy tells us
  which columns each dataset really exposes. A filter is emitted only for a column that at
  least two in-scope charts share — a filter over one chart is a control nobody can use, and
  a filter naming a column the dataset does not have is a broken dashboard.

Component ids are derived from the plan's deterministic order, so regenerating the same
plan produces the same ids and a diff shows only real changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

DASHBOARD_VERSION = "v2"
GRID_COLUMNS = 12
DEFAULT_CHART_WIDTH = 6
ROW_HEIGHT = 50
#: A table or heatmap is unreadable at half width, so it claims a whole row.
FULL_WIDTH_FAMILIES = frozenset({"detail", "matrix"})
#: Two charts is the minimum scope where a filter means anything.
MIN_FILTER_SCOPE = 2
MAX_FILTERS = 3
#: Filters apply to the whole grid unless a caller narrows them.
FILTER_SCOPE: tuple[str, ...] = ("ROOT_ID", "GRID_ID")

FILTER_SELECT = "filter_select"
FILTER_TIME = "filter_time"

_TEMPORAL_KINDS = frozenset({"temporal", "time", "date", "datetime", "timestamp"})

_ROW_ID = re.compile(r"^ROW-\d+$")
_CHART_ID = re.compile(r"^CHART-\d+$")
_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class RejectedFilter:
    """A filter that was considered and refused, with the reason."""

    dataset_id: int
    column: str
    reason: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "column": self.column,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class FilterPlan:
    """Native filters that are safe to add, and the ones that are not."""

    filters: tuple[dict[str, Any], ...] = ()
    rejected: tuple[RejectedFilter, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "native_filters": [dict(entry) for entry in self.filters],
            "rejected": [entry.to_public_dict() for entry in self.rejected],
            "count": len(self.filters),
        }


def build_position(
    slots: Iterable[Any],
    *,
    title: str = "",
    row_height: int = ROW_HEIGHT,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Build position metadata from plan slots, reporting what could not be placed.

    A slot without a saved chart id cannot be drawn, so it is dropped with a problem
    instead of producing a component that renders an empty frame.
    """
    problems: list[str] = []
    items: list[dict[str, Any]] = []
    seen_charts: set[int] = set()
    for slot in slots or ():
        slot_id = _text(_entry(slot, "id"), 64)
        try:
            chart_id = int(_entry(slot, "chart_id"))
        except (TypeError, ValueError):
            problems.append(f"slot {slot_id or '(unnamed)'} has no saved chart id")
            continue
        if chart_id in seen_charts:
            problems.append(f"chart {chart_id} was proposed twice and kept once")
            continue
        seen_charts.add(chart_id)
        family = _text(_entry(slot, "family"), 32)
        items.append(
            {
                "slot_id": slot_id,
                "chart_id": chart_id,
                "title": _text(_entry(slot, "title"), 160),
                "family": family,
                "section": _text(_entry(slot, "section"), 32) or "detail",
                "width": _width(family, _entry(slot, "width")),
            }
        )

    position: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": DASHBOARD_VERSION,
        "ROOT_ID": {
            "type": "ROOT",
            "id": "ROOT_ID",
            "children": ["GRID_ID"],
            "parents": [],
        },
        "GRID_ID": {
            "type": "GRID",
            "id": "GRID_ID",
            "children": [],
            "parents": ["ROOT_ID"],
        },
        "HEADER_ID": {
            "type": "HEADER",
            "id": "HEADER_ID",
            "meta": {"text": _text(title, 160)},
        },
    }

    grid_children: list[str] = []
    row_number = 0
    chart_number = 0
    row_id = ""
    used_width = 0
    for section, group in _by_section(items):
        for item in group:
            if not row_id or used_width + item["width"] > GRID_COLUMNS:
                row_number += 1
                row_id = f"ROW-{row_number}"
                position[row_id] = {
                    "type": "ROW",
                    "id": row_id,
                    "children": [],
                    "parents": ["ROOT_ID", "GRID_ID"],
                    "meta": {
                        "background": "BACKGROUND_TRANSPARENT",
                        "section": section,
                    },
                }
                grid_children.append(row_id)
                used_width = 0
            chart_number += 1
            component = f"CHART-{chart_number}"
            position[component] = {
                "type": "CHART",
                "id": component,
                "children": [],
                "parents": ["ROOT_ID", "GRID_ID", row_id],
                "meta": {
                    "chartId": item["chart_id"],
                    "width": item["width"],
                    "height": row_height,
                    "sliceName": item["title"],
                    "sliceNameOverride": item["title"],
                },
            }
            position[row_id]["children"].append(component)
            used_width += item["width"]

    position["GRID_ID"]["children"] = grid_children
    return position, tuple(problems)


def validate_position(position: Any) -> tuple[str, ...]:
    """Every reason this position object would render incorrectly; empty means valid."""
    if not isinstance(position, Mapping):
        return ("position is not an object",)
    problems: list[str] = []
    for required, kind in (("ROOT_ID", "ROOT"), ("GRID_ID", "GRID")):
        node = position.get(required)
        if not isinstance(node, Mapping):
            problems.append(f"{required} is missing")
        elif node.get("type") != kind:
            problems.append(f"{required} is not a {kind}")

    seen: set[str] = set()
    chart_ids: set[int] = set()
    for node_id, node in position.items():
        if node_id == "DASHBOARD_VERSION_KEY" or not isinstance(node, Mapping):
            continue
        if node_id in seen:
            problems.append(f"{node_id} appears twice")
        seen.add(node_id)
        for child in _list(node.get("children")):
            if child not in position:
                problems.append(f"{node_id} references missing child {child}")
                continue
            expected = _expected_parents(position, child)
            if expected is None:
                continue
            actual = tuple(_list(position[child].get("parents")))
            if actual != expected:
                problems.append(
                    f"{child} has parents {list(actual)} instead of {list(expected)}"
                )
        if node.get("type") == "ROW":
            widths = [
                _meta_width(position.get(child))
                for child in _list(node.get("children"))
            ]
            total = sum(widths)
            if total > GRID_COLUMNS:
                problems.append(f"{node_id} is {total} columns wide, over {GRID_COLUMNS}")
            if any(width < 1 or width > GRID_COLUMNS for width in widths):
                problems.append(f"{node_id} has a width outside 1..{GRID_COLUMNS}")
        if node.get("type") == "CHART":
            raw = _meta_chart_id(node)
            if raw is None:
                problems.append(f"{node_id} has no chartId")
            elif raw in chart_ids:
                problems.append(f"chart {raw} is placed more than once")
            else:
                chart_ids.add(raw)
    return tuple(problems)


def plan_filters(
    slots: Iterable[Any],
    *,
    columns: Mapping[Any, Mapping[str, Any]] | None = None,
    max_filters: int = MAX_FILTERS,
) -> FilterPlan:
    """Emit native filters only where a column is shared by enough in-scope charts."""
    counts: dict[int, int] = {}
    for slot in slots or ():
        try:
            dataset_id = int(_entry(slot, "dataset_id"))
        except (TypeError, ValueError):
            continue
        if _entry(slot, "chart_id") is None:
            continue
        counts[dataset_id] = counts.get(dataset_id, 0) + 1

    declared = _declared_columns(columns)
    filters: list[dict[str, Any]] = []
    rejected: list[RejectedFilter] = []
    for dataset_id, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        available = declared.get(dataset_id)
        if not available:
            if count >= MIN_FILTER_SCOPE:
                rejected.append(
                    RejectedFilter(dataset_id, "", "columns_not_declared")
                )
            continue
        if count < MIN_FILTER_SCOPE:
            rejected.append(RejectedFilter(dataset_id, "", "single_chart_scope"))
            continue
        if len(filters) >= max_filters:
            rejected.append(RejectedFilter(dataset_id, "", "filter_budget"))
            continue
        # A time range is the filter people reach for first, so it wins the budget.
        for column in _ordered_columns(available):
            if len(filters) >= max_filters:
                # One summary per dataset: the per-column list is noise.
                rejected.append(RejectedFilter(dataset_id, "", "filter_budget"))
                break
            filters.append(_filter_payload(dataset_id, column, available[column]))
    return FilterPlan(filters=tuple(filters), rejected=tuple(rejected))


def _ordered_columns(available: dict[str, str]) -> list[str]:
    """Temporal columns first, then alphabetical — stable, and defensible."""
    return sorted(
        available,
        key=lambda column: (available[column] not in _TEMPORAL_KINDS, column),
    )


def _filter_payload(dataset_id: int, column: str, kind: str) -> dict[str, Any]:
    temporal = kind in _TEMPORAL_KINDS
    payload: dict[str, Any] = {
        "id": f"NATIVE_FILTER-{dataset_id}-{_slug(column)}",
        "name": column,
        "filterType": FILTER_TIME if temporal else FILTER_SELECT,
        "targets": [{"datasetId": dataset_id, "column": {"name": column}}],
        "scope": {"rootPath": list(FILTER_SCOPE), "excluded": []},
        "description": "",
        "cascadeParentIds": [],
        "controlValues": {},
    }
    if not temporal:
        payload["controlValues"] = {
            "enableEmptyFilter": False,
            "multiSelect": True,
            "searchAllOptions": False,
            "inverseSelection": False,
        }
        payload["defaultDataMask"] = {
            "extraFormData": {},
            "filterState": {"value": None},
            "ownState": {},
        }
    return payload


def _declared_columns(
    columns: Mapping[Any, Mapping[str, Any]] | None,
) -> dict[int, dict[str, str]]:
    declared: dict[int, dict[str, str]] = {}
    for key, entries in (columns or {}).items():
        try:
            dataset_id = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(entries, Mapping):
            continue
        cleaned: dict[str, str] = {}
        for name, kind in entries.items():
            column = _text(name, 160)
            if column:
                cleaned[column] = _text(kind, 32).lower()
        if cleaned:
            declared[dataset_id] = cleaned
    return declared


def _by_section(
    items: list[dict[str, Any]],
) -> Iterable[tuple[str, list[dict[str, Any]]]]:
    """Group consecutive slots by section, preserving the plan's reading order."""
    block: list[dict[str, Any]] = []
    current = ""
    for item in items:
        if block and item["section"] != current:
            yield current, block
            block = []
        current = item["section"]
        block.append(item)
    if block:
        yield current, block


def _width(family: str, declared: Any) -> int:
    if family in FULL_WIDTH_FAMILIES:
        return GRID_COLUMNS
    try:
        width = int(declared)
    except (TypeError, ValueError):
        return DEFAULT_CHART_WIDTH
    return max(1, min(width, GRID_COLUMNS))


def _expected_parents(
    position: Mapping[str, Any], child: str
) -> tuple[str, ...] | None:
    """The exact parent chain Superset expects for a component of this type."""
    node = position.get(child)
    if not isinstance(node, Mapping):
        return None
    node_type = node.get("type")
    if node_type == "GRID":
        return ("ROOT_ID",)
    if node_type == "ROW":
        return ("ROOT_ID", "GRID_ID")
    if node_type == "CHART":
        for row_id, row in position.items():
            if not _ROW_ID.match(str(row_id)) or not isinstance(row, Mapping):
                continue
            if child in _list(row.get("children")):
                return ("ROOT_ID", "GRID_ID", str(row_id))
        return ("ROOT_ID", "GRID_ID")
    return None


def _meta_width(node: Any) -> int:
    if not isinstance(node, Mapping):
        return 0
    meta = node.get("meta")
    raw = meta.get("width") if isinstance(meta, Mapping) else None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _meta_chart_id(node: Any) -> int | None:
    if not isinstance(node, Mapping):
        return None
    meta = node.get("meta")
    raw = meta.get("chartId") if isinstance(meta, Mapping) else None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _entry(slot: Any, key: str, default: Any = None) -> Any:
    if isinstance(slot, Mapping):
        return slot.get(key, default)
    return getattr(slot, key, default)


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _slug(value: str) -> str:
    return _SLUG.sub("-", value.lower()).strip("-")[:24] or "column"


def _text(value: Any, limit: int = 160) -> str:
    return " ".join(str(value or "").split())[:limit]
