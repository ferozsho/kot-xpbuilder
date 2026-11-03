"""Structured response blocks for the XP Prompt workspace (P2-F06).

A chat answer is prose plus zero or more *blocks*. The block vocabulary is fixed
here so the workspace can render a plan, assumptions, a SQL preview, a result
preview, a dataset profile, a chart recommendation, a dashboard proposal,
warnings, a repair outcome, or a clarification question — and so Phases 4–8 have
one contract to emit into instead of inventing a shape each time.

Two rules shape this module:

* **normalization never raises.** Provider- or phase-supplied blocks are
  untrusted: an unknown type is dropped, a string is truncated, a row list is
  capped. A malformed block must never fail a turn that already cost tokens.
* **previews are bounded and honest.** ``sql_preview`` is truncated with
  ``truncated: true``, ``result_preview`` keeps at most :data:`MAX_ROWS` rows and
  marks the real ``row_count``, so a rendered preview can never be mistaken for
  the complete result — which is also what keeps unrestricted results out of the
  stored transcript (P2-F10).

Phase 2 emits only what it can honestly know: a plan for dataset/chart/dashboard
intents, the assumptions behind a query answer, and a warning that nothing has
been executed or saved. The remaining types are contract + renderer only until
their producers land.
"""

from __future__ import annotations

import re
from typing import Any

from xp_prompt import intents

CLARIFICATION = "clarification"
PLAN = "plan"
ASSUMPTIONS = "assumptions"
SQL_PREVIEW = "sql_preview"
RESULT_PREVIEW = "result_preview"
DATASET_PROFILE = "dataset_profile"
CHART_RECOMMENDATION = "chart_recommendation"
DASHBOARD_PROPOSAL = "dashboard_proposal"
WARNINGS = "warnings"
REPAIR = "repair"
LINKS = "links"

BLOCK_TYPES: tuple[str, ...] = (
    CLARIFICATION,
    PLAN,
    ASSUMPTIONS,
    SQL_PREVIEW,
    RESULT_PREVIEW,
    DATASET_PROFILE,
    CHART_RECOMMENDATION,
    DASHBOARD_PROPOSAL,
    WARNINGS,
    REPAIR,
    LINKS,
)

MAX_BLOCKS = 12
MAX_ITEMS = 20
MAX_TEXT_CHARS = 600
MAX_SQL_CHARS = 4000
MAX_ROWS = 20
MAX_CELLS = 12
MAX_CELL_CHARS = 200
MAX_LINKS = 6

#: Superset objects the workspace may link to. The frontend owns the actual
#: paths, so a route change never leaves a stale href in a stored transcript.
LINK_ENTITIES: tuple[str, ...] = ("dataset", "chart", "dashboard")
_SAFE_ENTITY_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_REPAIR_OUTCOMES = frozenset({"fixed", "failed", "unchanged", "skipped"})
_PERIOD_CUE = re.compile(
    r"\b(today|yesterday|this|last|previous|next|since|between|ytd|mtd|q[1-4]|"
    r"week|month|quarter|year|term|semester|days?)\b",
    re.IGNORECASE,
)

_PLANS: dict[str, tuple[str, ...]] = {
    intents.DATASET: (
        "Pick the source table and the rows to keep",
        "Define the joins, columns, and filters",
        "Preview the result before anything is saved",
        "Save the dataset only after you confirm",
    ),
    intents.CHART: (
        "Confirm the metric and the dimensions to group by",
        "Preview the aggregate the chart would use",
        "Recommend a chart type for that shape",
        "Create the chart only after you confirm",
    ),
    intents.DASHBOARD: (
        "List the metrics the dashboard should show",
        "Propose one chart per metric",
        "Preview each chart before publishing",
        "Publish only after you confirm",
    ),
}


def normalize_blocks(raw: Any) -> list[dict[str, Any]]:
    """Keep the well-formed blocks, in order, bounded in count and size."""
    if not isinstance(raw, list):
        return []
    kept: list[dict[str, Any]] = []
    for item in raw:
        if len(kept) >= MAX_BLOCKS:
            break
        block = _normalize_block(item)
        if block is not None:
            kept.append(block)
    return kept


def clarification_blocks(question: str) -> list[dict[str, Any]]:
    """One clarification block, normalized like every other block."""
    return normalize_blocks([{"type": CLARIFICATION, "question": question}])


def answer_blocks(
    intent: str,
    *,
    content: str = "",
    has_prior_answer: bool = False,
) -> list[dict[str, Any]]:
    """The blocks Phase 2 can produce honestly for one turn."""
    blocks: list[dict[str, Any]] = []

    plan = _PLANS.get(intent)
    if plan:
        blocks.append({"type": PLAN, "title": "What I would do", "items": list(plan)})
        blocks.append(
            {
                "type": WARNINGS,
                "items": ["Nothing has been saved or published yet."],
            }
        )

    if intent == intents.QUERY:
        assumptions = []
        if not _PERIOD_CUE.search(content or ""):
            assumptions.append(
                "No time range was given, so this assumes the current period."
            )
        assumptions.append("No query has been run against your data yet.")
        blocks.append({"type": ASSUMPTIONS, "items": assumptions})

    if intent == intents.MODIFICATION and has_prior_answer:
        blocks.append(
            {
                "type": ASSUMPTIONS,
                "items": ["Applied to the most recent answer in this conversation."],
            }
        )

    return normalize_blocks(blocks)


def _normalize_block(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    block_type = str(item.get("type") or "").strip().lower()
    if block_type not in BLOCK_TYPES:
        return None
    try:
        return _NORMALIZERS[block_type](item)
    except (TypeError, ValueError, AttributeError):
        return None


def _clarification(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": CLARIFICATION,
        "question": _text(item.get("question")) or "Could you clarify that?",
    }


def _plan(item: dict[str, Any]) -> dict[str, Any]:
    items = _items(item.get("items"))
    if not items:
        raise ValueError("plan needs items")
    return {
        "type": PLAN,
        "title": _text(item.get("title")) or "Plan",
        "items": items,
    }


def _assumptions(item: dict[str, Any]) -> dict[str, Any]:
    items = _items(item.get("items"))
    if not items:
        raise ValueError("assumptions need items")
    return {"type": ASSUMPTIONS, "items": items}


def _warnings(item: dict[str, Any]) -> dict[str, Any]:
    items = _items(item.get("items"))
    if not items:
        raise ValueError("warnings need items")
    return {"type": WARNINGS, "items": items}


def _sql_preview(item: dict[str, Any]) -> dict[str, Any]:
    sql = str(item.get("sql") or "").strip()
    if not sql:
        raise ValueError("sql_preview needs sql")
    truncated = len(sql) > MAX_SQL_CHARS
    return {
        "type": SQL_PREVIEW,
        "sql": sql[:MAX_SQL_CHARS],
        "dialect": _text(item.get("dialect")) or "",
        "truncated": truncated,
    }


def _result_preview(item: dict[str, Any]) -> dict[str, Any]:
    columns = [str(column)[:MAX_CELL_CHARS] for column in _list(item.get("columns"))]
    if not columns:
        raise ValueError("result_preview needs columns")
    columns = columns[:MAX_CELLS]
    rows: list[list[str]] = []
    for row in _list(item.get("rows"))[:MAX_ROWS]:
        if not isinstance(row, list | tuple):
            continue
        rows.append([_cell(cell) for cell in row[: len(columns)]])
    try:
        row_count = int(item.get("row_count"))
    except (TypeError, ValueError):
        row_count = len(rows)
    return {
        "type": RESULT_PREVIEW,
        "columns": columns,
        "rows": rows,
        "row_count": max(row_count, len(rows)),
        "truncated": bool(item.get("truncated")) or row_count > len(rows),
    }


def _dataset_profile(item: dict[str, Any]) -> dict[str, Any]:
    columns = []
    for column in _list(item.get("columns")):
        if not isinstance(column, dict):
            continue
        name = _text(column.get("name"))
        if not name:
            continue
        columns.append(
            {
                "name": name,
                "type": _text(column.get("type")),
                "nullable": bool(column.get("nullable", True)),
            }
        )
    if not columns:
        raise ValueError("dataset_profile needs columns")
    return {
        "type": DATASET_PROFILE,
        "table": _text(item.get("table")),
        "columns": columns[:MAX_CELLS],
        "row_count": _optional_int(item.get("row_count")),
    }


def _chart_recommendation(item: dict[str, Any]) -> dict[str, Any]:
    chart_type = _text(item.get("chart_type"))
    if not chart_type:
        raise ValueError("chart_recommendation needs chart_type")
    return {
        "type": CHART_RECOMMENDATION,
        "chart_type": chart_type,
        "x": _text(item.get("x")),
        "y": [value[:MAX_CELL_CHARS] for value in _list(item.get("y"))][:MAX_CELLS],
        "reason": _text(item.get("reason")),
    }


def _dashboard_proposal(item: dict[str, Any]) -> dict[str, Any]:
    sections = []
    for section in _list(item.get("sections")):
        if not isinstance(section, dict):
            continue
        title = _text(section.get("title"))
        if not title:
            continue
        sections.append(
            {
                "title": title,
                "items": [value[:MAX_CELL_CHARS] for value in _list(section.get("items"))],
            }
        )
    if not sections:
        raise ValueError("dashboard_proposal needs sections")
    return {
        "type": DASHBOARD_PROPOSAL,
        "title": _text(item.get("title")) or "Proposed dashboard",
        "sections": sections[:MAX_ITEMS],
    }


def _repair(item: dict[str, Any]) -> dict[str, Any]:
    outcome = str(item.get("outcome") or "").strip().lower()
    if outcome not in _REPAIR_OUTCOMES:
        outcome = "unchanged"
    return {
        "type": REPAIR,
        "outcome": outcome,
        "attempts": _optional_int(item.get("attempts")) or 0,
        "notes": _text(item.get("notes")),
    }


def _links(item: dict[str, Any]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for entry in _list(item.get("items")):
        if not isinstance(entry, dict):
            continue
        entity_type = str(entry.get("entity_type") or "").strip().lower()
        entity_id = str(entry.get("entity_id") or "").strip()
        if entity_type not in LINK_ENTITIES or not _SAFE_ENTITY_ID.match(entity_id):
            continue
        entries.append(
            {
                "label": _text(entry.get("label")) or f"Open {entity_type}",
                "entity_type": entity_type,
                "entity_id": entity_id,
            }
        )
    if not entries:
        raise ValueError("links needs at least one valid item")
    return {"type": LINKS, "items": entries[:MAX_LINKS]}


def native_link(entity_type: str, entity_id: Any, label: str | None = None) -> dict:
    """Build one link entry for a producer (Phases 7–8).

    Only the object identity is stored: the frontend maps it to the deployment's
    own Superset route, so a stored transcript can never carry a stale or foreign
    URL.
    """
    entity = str(entity_type or "").strip().lower()
    identifier = str(entity_id or "").strip()
    if entity not in LINK_ENTITIES:
        raise ValueError(f"unsupported entity_type {entity_type!r}")
    if not _SAFE_ENTITY_ID.match(identifier):
        raise ValueError("entity_id must be alphanumeric, dash, or underscore")
    entry = {"entity_type": entity, "entity_id": identifier}
    if label:
        entry["label"] = _text(label)
    return entry


_NORMALIZERS = {
    CLARIFICATION: _clarification,
    PLAN: _plan,
    ASSUMPTIONS: _assumptions,
    SQL_PREVIEW: _sql_preview,
    RESULT_PREVIEW: _result_preview,
    DATASET_PROFILE: _dataset_profile,
    CHART_RECOMMENDATION: _chart_recommendation,
    DASHBOARD_PROPOSAL: _dashboard_proposal,
    WARNINGS: _warnings,
    REPAIR: _repair,
    LINKS: _links,
}


def _text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _list(value: Any) -> list[Any]:
    if isinstance(value, list | tuple):
        return list(value)
    return []


def _items(value: Any) -> list[str]:
    return [text for text in (_text(item) for item in _list(value)) if text][:MAX_ITEMS]


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)[:MAX_CELL_CHARS]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
