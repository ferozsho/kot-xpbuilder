# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Read-only dashboard metadata and complete real-data preview (P8-F06/F07).

The Superset proxy executes every chart through ``ChartDataCommand`` in the caller's
security context.  This module receives those bounded results only after every slot has
rendered, validates them with the same result policy used by chart previews, and builds
the user-readable metadata around them.

Nothing in this module persists a dashboard, chart, dataset, or preview.  A successful
response is therefore an explicit review artifact: ``persisted`` is always false and the
caller must make a separate publish request in a later phase.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from xp_prompt import result_validation

MAX_SLOTS = 8
MAX_TEXT = 240
MAX_DESCRIPTION = 1_000


class DashboardPreviewError(ValueError):
    """A preview is incomplete or structurally unsafe; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Metadata:
    """Readable dashboard metadata derived from the plan and completed runs."""

    title: str
    description: str
    generated_at: str
    run_count: int
    slot_count: int
    dataset_count: int

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "generated_at": self.generated_at,
            "last_generated_at": self.generated_at,
            "run_count": self.run_count,
            "slot_count": self.slot_count,
            "dataset_count": self.dataset_count,
        }


@dataclass(frozen=True, slots=True)
class Preview:
    """A complete dashboard preview; partial previews are never constructed."""

    metadata: Metadata
    slots: tuple[dict[str, Any], ...]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata.to_public_dict(),
            "slots": [dict(slot) for slot in self.slots],
            "count": len(self.slots),
            "complete": True,
            "persisted": False,
            "publish_required": True,
        }


def build(
    *,
    title: str = "",
    goal: str = "",
    audience: str = "unspecified",
    slots: Iterable[Any] = (),
    renders: Iterable[Any] = (),
    generated_at: datetime | None = None,
) -> Preview:
    """Build metadata and accessible contexts after every planned slot rendered."""
    planned = _planned_slots(slots)
    rendered = _rendered_slots(renders)
    planned_ids = [slot["id"] for slot in planned]

    missing = [slot_id for slot_id in planned_ids if slot_id not in rendered]
    extra = [slot_id for slot_id in rendered if slot_id not in set(planned_ids)]
    if missing:
        raise DashboardPreviewError(
            "missing_render", f"no chart-data result for slot {missing[0]}"
        )
    if extra:
        raise DashboardPreviewError(
            "unexpected_render", f"chart-data result has no slot {extra[0]}"
        )

    previews: list[dict[str, Any]] = []
    for slot in planned:
        raw = rendered[slot["id"]]
        try:
            verdict = result_validation.validate_result(
                raw.get("columns"),
                raw.get("rows"),
                row_count=raw.get("row_count")
                if isinstance(raw.get("row_count"), int)
                else None,
                truncated=bool(raw.get("truncated")),
                intent="dashboard",
                max_rows=result_validation.MAX_PREVIEW_ROWS,
            )
        except result_validation.ResultValidationError as exc:
            raise DashboardPreviewError(
                "invalid_render", f"slot {slot['id']} was rejected ({exc.code})"
            ) from exc
        previews.append(_slot_preview(slot, verdict.to_public_dict()))

    stamp = _timestamp(generated_at)
    metadata = Metadata(
        title=_text(title, MAX_TEXT) or "Untitled dashboard",
        description=_description(goal, audience, planned),
        generated_at=stamp,
        # One chart-data execution is required for each slot in this preview.
        run_count=len(previews),
        slot_count=len(previews),
        dataset_count=len({slot["dataset_id"] for slot in planned}),
    )
    return Preview(metadata=metadata, slots=tuple(previews))


def _planned_slots(entries: Iterable[Any]) -> list[dict[str, Any]]:
    if not isinstance(entries, list | tuple):
        raise DashboardPreviewError("invalid_slots", "slots must be a list")
    if not entries:
        raise DashboardPreviewError("empty_preview", "at least one slot is required")
    if len(entries) > MAX_SLOTS:
        raise DashboardPreviewError(
            "slot_budget", f"a preview may contain at most {MAX_SLOTS} slots"
        )

    planned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise DashboardPreviewError("invalid_slot", "each slot must be an object")
        slot_id = _text(entry.get("id"), 64)
        if not slot_id:
            raise DashboardPreviewError("invalid_slot", "each slot needs an id")
        if slot_id in seen:
            raise DashboardPreviewError(
                "duplicate_slot", f"slot {slot_id} appears more than once"
            )
        seen.add(slot_id)
        try:
            dataset_id = int(entry.get("dataset_id"))
        except (TypeError, ValueError) as exc:
            raise DashboardPreviewError(
                "invalid_slot", f"slot {slot_id} needs a dataset id"
            ) from exc
        planned.append(
            {
                "id": slot_id,
                "title": _text(entry.get("title"), MAX_TEXT) or "Untitled chart",
                "question": _text(entry.get("question"), MAX_TEXT),
                "family": _text(entry.get("family"), 32),
                "section": _text(entry.get("section"), 32),
                "dataset_id": dataset_id,
                "dataset_name": _text(entry.get("dataset_name"), MAX_TEXT),
                "viz_type": _text(entry.get("viz_type"), 80),
                "chart_id": _optional_int(entry.get("chart_id")),
                "source": _text(entry.get("source"), 32),
            }
        )
    return planned


def _rendered_slots(entries: Iterable[Any]) -> dict[str, Mapping[str, Any]]:
    if not isinstance(entries, list | tuple):
        raise DashboardPreviewError("invalid_renders", "renders must be a list")
    rendered: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise DashboardPreviewError(
                "invalid_render", "each chart-data result must be an object"
            )
        slot_id = _text(entry.get("slot_id"), 64)
        if not slot_id:
            raise DashboardPreviewError(
                "invalid_render", "each chart-data result needs a slot id"
            )
        if slot_id in rendered:
            raise DashboardPreviewError(
                "duplicate_render", f"slot {slot_id} rendered more than once"
            )
        rendered[slot_id] = entry
    return rendered


def _slot_preview(slot: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    returned = int(verdict.get("returned") or 0)
    row_count = int(verdict.get("row_count") or 0)
    chart_kind = slot["viz_type"].replace("_", " ") or "chart"
    dataset = slot["dataset_name"] or f"dataset {slot['dataset_id']}"
    question = slot["question"] or slot["title"]
    context = (
        f"{slot['title']}. {chart_kind} from {dataset}, answering: {question}. "
        f"Showing {returned} of {row_count} row(s)."
    )[:MAX_DESCRIPTION]
    return {
        **slot,
        "description": context,
        "accessible_context": context,
        "run_count": 1,
        "data": verdict,
    }


def _description(goal: str, audience: str, slots: list[dict[str, Any]]) -> str:
    clean_goal = _text(goal, MAX_DESCRIPTION)
    clean_audience = _text(audience, 80)
    datasets = len({slot["dataset_id"] for slot in slots})
    lead = clean_goal or "Review the proposed dashboard before publishing."
    audience_text = (
        f" for a {clean_audience} audience"
        if clean_audience and clean_audience != "unspecified"
        else ""
    )
    return (
        f"{lead.rstrip('.')} — {len(slots)} chart(s) across {datasets} dataset(s)"
        f"{audience_text}."
    )[:MAX_DESCRIPTION]


def _timestamp(value: datetime | None) -> str:
    stamp = value or datetime.now(UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]
