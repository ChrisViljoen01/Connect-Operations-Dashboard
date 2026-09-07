from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from opus_dashboard.config import BRAND_DIR


NAVY = colors.HexColor("#1C2545")
TEAL = colors.HexColor("#007D6D")
ORANGE = colors.HexColor("#E04403")
RED = colors.HexColor("#B91C1C")
LIGHT_TEAL = colors.HexColor("#EAF5F3")
LIGHT_ORANGE = colors.HexColor("#FFF3ED")
LIGHT_GREY = colors.HexColor("#F4F6F8")
MID_GREY = colors.HexColor("#D8DEE5")
TEXT = colors.HexColor("#263238")
BUSINESS_TIMEZONE = ZoneInfo("Africa/Johannesburg")


def _number(value: Any, *, decimals: int = 3) -> str:
    if value is None:
        return "Not supplied"
    try:
        number = Decimal(str(value))
    except Exception:
        return str(value)
    rendered = f"{number:,.{decimals}f}"
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _text(value: Any) -> str:
    return escape(str(value if value not in (None, "") else "-"))


def _paragraph(value: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_text(value), style)


def _table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    widths: Sequence[float],
    *,
    body_style: ParagraphStyle,
    header_style: ParagraphStyle,
    highlights: Sequence[tuple[int, colors.Color]] = (),
) -> LongTable:
    data: list[list[Any]] = [
        [_paragraph(header, header_style) for header in headers]
    ]
    data.extend(
        [_paragraph(value, body_style) for value in row]
        for row in rows
    )
    table = LongTable(
        data,
        colWidths=list(widths),
        repeatRows=1,
        hAlign="LEFT",
    )
    commands: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, MID_GREY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for row_number in range(1, len(data)):
        if row_number % 2 == 0:
            commands.append(("BACKGROUND", (0, row_number), (-1, row_number), LIGHT_GREY))
    for row_number, colour in highlights:
        commands.append(("BACKGROUND", (0, row_number + 1), (-1, row_number + 1), colour))
    table.setStyle(TableStyle(commands))
    return table


def create_order_study_pdf(path: Path, study: dict[str, Any]) -> dict[str, int]:
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=21,
        textColor=NAVY,
        spaceAfter=3,
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle",
        parent=styles["Normal"],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#52616B"),
    )
    section_style = ParagraphStyle(
        "Section",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=NAVY,
        spaceBefore=8,
        spaceAfter=5,
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontSize=7.4,
        leading=9.2,
        textColor=TEXT,
    )
    small_style = ParagraphStyle(
        "Small",
        parent=body_style,
        fontSize=6.7,
        leading=8.2,
    )
    header_style = ParagraphStyle(
        "Header",
        parent=body_style,
        fontName="Helvetica-Bold",
        fontSize=6.8,
        leading=8,
        textColor=colors.white,
        alignment=TA_CENTER,
    )
    value_style = ParagraphStyle(
        "KpiValue",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=15,
        textColor=NAVY,
        alignment=TA_CENTER,
    )
    label_style = ParagraphStyle(
        "KpiLabel",
        parent=small_style,
        fontName="Helvetica-Bold",
        textColor=TEAL,
        alignment=TA_CENTER,
    )
    right_style = ParagraphStyle(
        "Right",
        parent=subtitle_style,
        alignment=TA_RIGHT,
    )

    generated_at = datetime.now(BUSINESS_TIMEZONE)
    page_count = {"value": 0}
    document = SimpleDocTemplate(
        str(path),
        pagesize=landscape(A4),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=13 * mm,
        title=f"{study.get('order_reference')} operational flow report",
        author="Connect Logistics",
        subject="Order stock, physical movement leg and bay-flow reconciliation",
    )

    def footer(canvas: Any, doc: Any) -> None:
        page_count["value"] = max(page_count["value"], int(doc.page))
        canvas.saveState()
        canvas.setStrokeColor(MID_GREY)
        canvas.setLineWidth(0.4)
        canvas.line(10 * mm, 9 * mm, landscape(A4)[0] - 10 * mm, 9 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#667680"))
        canvas.drawString(
            10 * mm,
            5.5 * mm,
            "Connect Logistics | Internal management report | OPUS source data",
        )
        canvas.drawRightString(
            landscape(A4)[0] - 10 * mm,
            5.5 * mm,
            f"Page {doc.page}",
        )
        canvas.restoreState()

    story: list[Any] = []
    logo_path = BRAND_DIR / "Connect-Logistics-Logo.png"
    logo: Any = ""
    if logo_path.exists():
        logo = Image(str(logo_path), width=48 * mm, height=12 * mm, kind="proportional")
    report_meta = Paragraph(
        (
            f"<b>Client:</b> {_text(study.get('client_name'))}<br/>"
            f"<b>Root period:</b> {_text(study.get('date_from'))} to "
            f"{_text(study.get('date_to'))}<br/>"
            f"<b>Generated:</b> {generated_at:%d %b %Y %H:%M} SAST"
        ),
        right_style,
    )
    story.append(Table([[logo, report_meta]], colWidths=[120 * mm, 147 * mm]))
    story.append(Spacer(1, 3 * mm))
    story.append(
        Paragraph(
            f"{_text(study.get('order_reference'))} Operational Flow Report",
            title_style,
        )
    )
    story.append(
        Paragraph(
            (
                "Concise reconciliation of signed-off loading and offloading nett "
                "weights by physical leg, stock at BCF and BC, bay balances, route "
                "alignment, and source exceptions."
            ),
            subtitle_style,
        )
    )

    coverage_message = study.get("coverage_warning") or study.get("coverage_note")
    if coverage_message:
        note = Table(
            [[Paragraph(f"<b>Source coverage:</b> {_text(coverage_message)}", body_style)]],
            colWidths=[267 * mm],
        )
        note.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), LIGHT_TEAL),
                    ("BOX", (0, 0), (-1, -1), 0.6, TEAL),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.extend((Spacer(1, 2 * mm), note))

    metrics = study.get("metrics") or {}
    kpis = (
        ("Parcel allocation", f"{_number(metrics.get('parcel_tonnes'))} t"),
        ("Opening stock", f"{_number(metrics.get('opening_stock_tonnes'))} t"),
        ("Current BCF stock", f"{_number(metrics.get('bcf_stock_tonnes'))} t"),
        ("Current BC stock", f"{_number(metrics.get('bc_stock_tonnes'))} t"),
        (
            "Stock at recorded locations",
            f"{_number(metrics.get('stock_at_recorded_locations_tonnes'))} t",
        ),
        ("In transit", f"{_number(metrics.get('in_transit_tonnes'))} t"),
    )
    kpi_cells = [
        Table(
            [
                [Paragraph(label, label_style)],
                [Paragraph(value, value_style)],
            ],
            colWidths=[64 * mm],
        )
        for label, value in kpis
    ]
    kpi_table = Table(
        [kpi_cells[:3], kpi_cells[3:]],
        colWidths=[89 * mm] * 3,
        rowHeights=[19 * mm, 19 * mm],
    )
    kpi_table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.5, MID_GREY),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, MID_GREY),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ]
        )
    )
    story.extend((Paragraph("Order stock overview", section_style), kpi_table))

    leg_rows = study.get("legs") or []
    leg_table = _table(
        (
            "Physical leg",
            "Dispatched t",
            "Received t",
            "Difference t",
            "Awaiting refs",
            "Awaiting t",
        ),
        (
            (
                row.get("route_name"),
                _number(row.get("loaded_tonnes")),
                _number(row.get("offloaded_tonnes")),
                _number(row.get("movement_difference_tonnes")),
                _number(row.get("pending_references"), decimals=0),
                _number(row.get("pending_loaded_tonnes")),
            )
            for row in leg_rows
        ),
        (37 * mm, 25 * mm, 25 * mm, 25 * mm, 22 * mm, 25 * mm),
        body_style=body_style,
        header_style=header_style,
    )

    audit_lines = [
        f"{int(metrics.get('review_references') or 0):,} unique references have one or more review reasons.",
        f"{int(metrics.get('allocations_without_loading') or 0):,} Transport Allocation roots have no signed-off loading.",
        f"{int(metrics.get('pending_offloads') or 0):,} loaded movements have no signed-off offloading.",
        f"{int(metrics.get('unexpected_bay_movements') or 0):,} movements use bays outside the expected study list.",
        f"{int(metrics.get('unlisted_route_movements') or 0):,} movements use route/bay combinations outside the supplied route matrix.",
        f"{int(metrics.get('weight_variance_exceptions') or 0):,} completed movements exceed the 0.250% absolute weight-variance review threshold.",
    ]
    closing = metrics.get("bcf_closing_tonnes")
    if closing is not None and Decimal(str(closing)) < 0:
        audit_lines.insert(
            0,
            (
                f"BCF closing is {_number(closing)} t: recorded dispatches exceed "
                "opening stock plus recorded mine receipts. The source values are "
                "shown without adjustment."
            ),
        )
    audit_content = "<br/>".join(f"&#8226; {_text(line)}" for line in audit_lines)
    audit_box = Table(
        [[Paragraph(audit_content, body_style)]],
        colWidths=[104 * mm],
    )
    audit_box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT_ORANGE),
                ("BOX", (0, 0), (-1, -1), 0.6, ORANGE),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend(
        (
            Paragraph("Physical movement legs and management attention", section_style),
            Table(
                [[leg_table, audit_box]],
                colWidths=[160 * mm, 107 * mm],
                style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]),
            ),
        )
    )

    route_rows = study.get("route_plan") or []
    route_highlights = [
        (index, LIGHT_ORANGE)
        for index, row in enumerate(route_rows)
        if row.get("plan_status")
        == "Observed in OPUS but not in route plan - review"
    ]
    story.append(PageBreak())
    story.extend(
        (
            Paragraph("Route plan alignment", section_style),
            _table(
                (
                    "Route classification",
                    "Route",
                    "Origin",
                    "From bay",
                    "Destination",
                    "To bay",
                    "Refs",
                    "Loaded t",
                    "Offloaded t",
                ),
                (
                    (
                        row.get("plan_status"),
                        row.get("route_name"),
                        row.get("origin"),
                        row.get("from_bay"),
                        row.get("destination"),
                        row.get("to_bay"),
                        _number(row.get("movement_references"), decimals=0),
                        _number(row.get("loaded_tonnes")),
                        _number(row.get("offloaded_tonnes")),
                    )
                    for row in route_rows
                ),
                (
                    45 * mm,
                    27 * mm,
                    20 * mm,
                    31 * mm,
                    22 * mm,
                    31 * mm,
                    14 * mm,
                    28 * mm,
                    30 * mm,
                ),
                body_style=small_style,
                header_style=header_style,
                highlights=route_highlights,
            ),
        )
    )

    staging_rows = study.get("staging") or []
    staging_highlights = [
        (index, LIGHT_ORANGE)
        for index, row in enumerate(staging_rows)
        if row.get("closing_tonnes") is not None
        and Decimal(str(row["closing_tonnes"])) < 0
    ]
    story.extend(
        (
            Paragraph("Bay flow analysis - BCF", section_style),
            _table(
                (
                    "BCF bay / area",
                    "Opening date",
                    "Opening source",
                    "Opening t",
                    "Mine receipts t",
                    "Other receipts t",
                    "Loaded to BC t",
                    "Current SOH t",
                ),
                (
                    (
                        row.get("bay"),
                        row.get("effective_date"),
                        row.get("opening_status"),
                        _number(row.get("opening_tonnes")),
                        _number(row.get("mine_receipts_tonnes")),
                        _number(row.get("other_receipts_tonnes")),
                        _number(row.get("bcf_dispatch_tonnes")),
                        _number(row.get("closing_tonnes")),
                    )
                    for row in staging_rows
                ),
                (
                    32 * mm,
                    22 * mm,
                    48 * mm,
                    24 * mm,
                    29 * mm,
                    28 * mm,
                    31 * mm,
                    28 * mm,
                ),
                body_style=small_style,
                header_style=header_style,
                highlights=staging_highlights,
            ),
        )
    )

    bc_rows = study.get("bc_areas") or []
    bc_highlights = [
        (index, LIGHT_ORANGE)
        for index, row in enumerate(bc_rows)
        if row.get("closing_tonnes") is not None
        and Decimal(str(row["closing_tonnes"])) < 0
    ]
    story.extend(
        (
            Paragraph("Bay flow analysis - BC", section_style),
            _table(
                (
                    "BC bay / area",
                    "Opening source",
                    "Opening t",
                    "From BCF t",
                    "Direct Mine t",
                    "Total received t",
                    "Current SOH t",
                ),
                (
                    (
                        row.get("area"),
                        row.get("opening_status"),
                        _number(row.get("opening_tonnes")),
                        _number(row.get("received_from_bcf_tonnes")),
                        _number(row.get("received_direct_from_mine_tonnes")),
                        _number(row.get("total_received_tonnes")),
                        _number(row.get("closing_tonnes")),
                    )
                    for row in bc_rows
                ),
                (
                    41 * mm,
                    58 * mm,
                    27 * mm,
                    31 * mm,
                    31 * mm,
                    37 * mm,
                    32 * mm,
                ),
                body_style=small_style,
                header_style=header_style,
                highlights=bc_highlights,
            ),
        )
    )

    methodology = (
        "<b>Methodology.</b> The study is isolated by exact order reference and "
        "qualifying Transport Allocation root date. Latest signed-off Loading and "
        "Exit and Offloading and Exit facts supply nett weights. BCF is treated as "
        "staging: opening stock + mine receipts - BCF dispatches. BC stock is its "
        "opening plus BCF and direct-Mine receipts. Stock is counted once at BCF or "
        "BC; Mine-to-BCF, BCF-to-BC and direct-Mine movements remain separate physical "
        "legs and are not added as an order total. Missing openings display as zero. "
        "Source exceptions remain visible and source weights are never altered."
    )
    story.extend(
        (
            Paragraph("Interpretation and method", section_style),
            KeepTogether(
                [
                    Table(
                        [[Paragraph(methodology, body_style)]],
                        colWidths=[267 * mm],
                        style=TableStyle(
                            [
                                ("BACKGROUND", (0, 0), (-1, -1), LIGHT_TEAL),
                                ("BOX", (0, 0), (-1, -1), 0.6, TEAL),
                                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                                ("TOPPADDING", (0, 0), (-1, -1), 6),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                            ]
                        ),
                    )
                ]
            ),
        )
    )

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return {
        "pages": page_count["value"],
        "leg_rows": len(leg_rows),
        "route_rows": len(route_rows),
        "staging_rows": len(staging_rows),
        "bc_rows": len(bc_rows),
        "bay_rows": len(staging_rows) + len(bc_rows),
    }


def _study_location_for_report(value: Any) -> str:
    text = str(value or "").strip()
    normalized = text.casefold()
    if "kookfontein" in normalized:
        return "Kookfontein"
    if "base chrome fields" in normalized:
        return "BCF"
    if "bulk connection" in normalized:
        return "BC"
    return text or "Unknown"
