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
"""Banded PDF rendering for the Report Designer.

Crystal-Reports-style output: report header (title, filters, generated-on),
group header bands, detail rows, group footer subtotals, grand-total footer,
and a page footer with page numbers. Rendered with reportlab (pure Python).
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Any

from flask_babel import gettext as _

# reportlab is installed in the runtime image (see Dockerfile); import lazily
# so this module can be imported even when reportlab is unavailable.
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ACCENT = colors.HexColor("#102B6B")
ACCENT_LIGHT = colors.HexColor("#E8EDF7")
GROUP_BG = colors.HexColor("#F2F5FB")
FOOTER_BG = colors.HexColor("#DDE6F3")
BORDER = colors.HexColor("#B9C4D9")
TEXT_GRAY = colors.HexColor("#444444")


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "RPTitle", parent=base["Title"], fontSize=18, textColor=ACCENT,
            spaceAfter=2 * mm,
        ),
        "subtitle": ParagraphStyle(
            "RPSubtitle", parent=base["Normal"], fontSize=9, textColor=TEXT_GRAY,
            spaceAfter=1.5 * mm,
        ),
        "group": ParagraphStyle(
            "RPGroup", parent=base["Normal"], fontSize=10, textColor=ACCENT,
            fontName="Helvetica-Bold", spaceBefore=4 * mm, spaceAfter=1.5 * mm,
        ),
        "total": ParagraphStyle(
            "RPTotal", parent=base["Normal"], fontSize=10, fontName="Helvetica-Bold",
            textColor=ACCENT,
        ),
        "cell": ParagraphStyle(
            "RPCell", parent=base["Normal"], fontSize=8.5, leading=11,
        ),
        "cellb": ParagraphStyle(
            "RPCellB", parent=base["Normal"], fontSize=8.5, leading=11,
            fontName="Helvetica-Bold",
        ),
        "filter": ParagraphStyle(
            "RPFilter", parent=base["Normal"], fontSize=8.5, textColor=TEXT_GRAY,
            leading=11,
        ),
    }


def _cell(value: Any, bold: bool = False) -> Paragraph:
    text = "" if value is None else str(value)
    if len(text) > 120:
        text = text[:117] + "..."
    return Paragraph(text.replace("\n", " "), _styles()["cellb" if bold else "cell"])


def _numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _fmt(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _build_table(
    columns: list[str],
    body_rows: list[list[Paragraph]],
    total_row: list[Paragraph] | None = None,
) -> Table:
    """A data table with a repeating bold header and optional footer band."""
    header = [_cell(col, bold=True) for col in columns]
    data = [header] + body_rows
    style = TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]
    )
    if total_row is not None:
        data.append(total_row)
        style.add("BACKGROUND", (0, -1), (-1, -1), FOOTER_BG)
    table = Table(data, repeatRows=1)
    table.setStyle(style)
    return table


def _grouped_rows(
    columns: list[str], rows: list[dict[str, Any]], group_idx: list[int]
) -> list[list[Paragraph]]:
    """Detail rows with group header + subtotal bands (Crystal style)."""
    styles = _styles()
    table_rows: list[list[Paragraph]] = []
    current_key: tuple | None = None
    count = 0

    def _flush_group() -> None:
        if not table_rows:
            return
        last = str(current_key[-1]) if current_key else ""
        table_rows.append(
            [Paragraph(f"<b>{_('Subtotal')}: {last}</b>", styles["total"])]
            + [Paragraph("", styles["cell"])] * (len(columns) - 1)
        )
        table_rows.append([Paragraph("", styles["cell"]) for _ in columns])

    for row in rows:
        key = tuple(row.get(columns[i], "") for i in group_idx)
        if key != current_key:
            _flush_group()
            current_key = key
            first = str(key[0]) if key else ""
            group_header = [Paragraph("", styles["cell"]) for _ in columns]
            group_header[0] = Paragraph(
                f"<b>{_('Group')}: {first}</b>", styles["group"]
            )
            table_rows.append(group_header)
        table_rows.append([_cell(row.get(col, "")) for col in columns])
        count += 1
    _flush_group()
    table_rows.append(
        [Paragraph(f"<b>{_('Total rows')}: {count}</b>", styles["total"])]
        + [Paragraph("", styles["cell"])] * (len(columns) - 1)
    )
    return table_rows


def _aggregate_rows(
    columns: list[str],
    rows: list[dict[str, Any]],
    metric_idx: list[int],
) -> tuple[list[list[Paragraph]], list[Paragraph]]:
    """Aggregated rows plus a grand-total footer band."""
    styles = _styles()
    body = [[_cell(row.get(col, "")) for col in columns] for row in rows]
    grand: list[float] = [0.0] * len(columns)
    for row in rows:
        for idx in metric_idx:
            value = _numeric(row.get(columns[idx]))
            if value is not None:
                grand[idx] += value
    total: list[Paragraph] = [Paragraph(_("Grand Total"), styles["total"])]
    for idx, _col in enumerate(columns):
        if idx in metric_idx and grand[idx]:
            total.append(Paragraph(_fmt(grand[idx]), styles["total"]))
        else:
            total.append(Paragraph("", styles["cell"]))
    return body, total


def _add_report_header(
    story: list[Any],
    report_name: str,
    description: str,
    definition: dict[str, Any],
    generated_by: str | None,
) -> None:
    """Title, metadata and filter summary bands."""
    styles = _styles()
    story.append(Paragraph(report_name, styles["title"]))
    if description:
        story.append(Paragraph(description, styles["subtitle"]))
    generated_on = datetime.now().strftime("%Y-%m-%d %H:%M")
    meta = _("Generated: %(when)s", when=generated_on)
    if generated_by:
        meta += f"  |  {_('By')}: {generated_by}"
    story.append(Paragraph(meta, styles["subtitle"]))
    filters = definition.get("filters") or []
    if filters:
        parts = []
        for filt in filters:
            value = filt.get("value")
            if isinstance(value, list):
                value = ", ".join(str(item) for item in value)
            parts.append(f"{filt.get('column')} {filt.get('op')} {value}")
        story.append(
            Paragraph(_("Filters") + ": " + " | ".join(parts), styles["filter"])
        )
    story.append(Spacer(1, 4 * mm))


def _render(
    report_name: str,
    description: str,
    definition: dict[str, Any],
    result: dict[str, Any],
    generated_by: str | None,
) -> bytes:
    """Render the report to PDF bytes."""
    buffer = BytesIO()
    columns = result["columns"]
    page_size = landscape(A4) if len(columns) > 5 else A4
    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=14 * mm,
        bottomMargin=16 * mm,
        title=report_name,
    )

    def _page_footer(canvas, _document) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(TEXT_GRAY)
        width, height = page_size
        canvas.drawString(12 * mm, 8 * mm, report_name[:80])
        canvas.drawRightString(
            width - 12 * mm, 8 * mm, str(canvas.getPageNumber())
        )
        canvas.setStrokeColor(BORDER)
        canvas.line(12 * mm, 11 * mm, width - 12 * mm, 11 * mm)
        canvas.restoreState()

    story: list[Any] = []
    _add_report_header(story, report_name, description, definition, generated_by)

    metrics = definition.get("metrics") or []
    group_by = definition.get("group_by") or []
    rows = result["rows"]
    styles = _styles()
    metric_idx = [
        idx
        for idx, col in enumerate(columns)
        if any(
            metric.get("label") == col
            or f"{metric.get('aggregate')}({metric.get('column')})" == col
            for metric in metrics
        )
    ]
    group_idx = [
        idx
        for idx, col in enumerate(columns)
        if any(
            group.get("label") == col or group.get("column") == col
            for group in group_by
        )
    ]

    if not metrics and group_idx:
        story.append(_build_table(columns, _grouped_rows(columns, rows, group_idx)))
    elif metrics:
        body, total = _aggregate_rows(columns, rows, metric_idx)
        story.append(_build_table(columns, body, total_row=total))
        story.append(
            Paragraph(f"<b>{_('Groups')}: {len(rows)}</b>", styles["total"])
        )
    else:
        body = [[_cell(row.get(col, "")) for col in columns] for row in rows]
        story.append(_build_table(columns, body))
        story.append(
            Paragraph(f"<b>{_('Rows')}: {len(rows)}</b>", styles["total"])
        )

    if result.get("truncated"):
        story.append(
            Paragraph(
                _("Report truncated — showing the first %(limit)s rows",
                  limit=definition.get("limit", 1000)),
                styles["filter"],
            )
        )

    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    return buffer.getvalue()


def build_report_pdf(
    report_name: str,
    description: str,
    definition: dict[str, Any],
    result: dict[str, Any],
    generated_by: str | None = None,
) -> bytes:
    """Render a report definition + execution result to a PDF (bytes)."""
    return _render(report_name, description, definition, result, generated_by)
