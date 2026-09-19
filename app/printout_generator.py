"""
Printout generator — Excel export for physical event overviews.

Generates a workbook with two sheets:
  - Podpisy: one row per (event, spot), wide signature column for handwriting
  - Přehled: one row per event, spots side-by-side in columns

Public entry point::

    wb = generate_printout(events, date_range, me_name)
    # caller saves to a BytesIO buffer and serves as a download
"""

from typing import TYPE_CHECKING

from openpyxl import Workbook
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.utils import get_app_tz
from app.xlsx import HEADER_FILL, HEADER_FONT, STD_FONT, THIN, cell, title_block

if TYPE_CHECKING:
    from app.models.event import Event
    from app.staffing import RequirementCoverage


def _participant_rows(event: Event) -> list[tuple[str, str, str]]:
    if event.staffing_mode == "CONDITIONS":
        return [
            (a.user.name, ", ".join(q.name for q in a.user.qualifications if not q.is_deleted), "")
            for a in event.assignments
        ]
    return [
        (
            s.assignment.user.name if s.assignment else "",
            ", ".join(q.name for q in s.required_qualifications if not q.is_deleted),
            s.description or "",
        )
        for s in sorted(event.spots, key=lambda s: s.id)
    ]


# ── Sheet 1: Podpisy (Signatures) ─────────────────────────────────────────────


def _build_signature_sheet(
    ws: Worksheet,
    events: list[Event],
    date_range: str,
    me_name: str | None,
) -> None:
    headers = ["Datum", "Název akce", "Jméno", "Kvalifikace", "Popis pozice", "Podpis"]
    widths = [12, 34, 26, 22, 22, 38]

    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    n_cols = len(headers)
    subtitle = _subtitle(date_range, me_name)
    hdr_row = title_block(ws, "Sestava pro tisk — Podpisy", subtitle, n_cols)

    # Column headers
    centre = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, h in enumerate(headers, 1):
        cell(ws, hdr_row, i, h, font=HEADER_FONT, fill=HEADER_FILL, alignment=centre, border=THIN)
    ws.row_dimensions[hdr_row].height = 18

    # Data — one row per (event, spot)
    tz = get_app_tz()
    row = hdr_row + 1
    left = Alignment(horizontal="left", vertical="center")

    for event in events:
        participants = _participant_rows(event)
        if not participants:
            continue
        date_str = event.start_datetime.astimezone(tz).strftime("%d.%m.%Y")

        for person, quals, desc in participants:

            for col, val in enumerate([date_str, event.name, person, quals, desc, ""], 1):
                cell(ws, row, col, val, font=STD_FONT, alignment=left, border=THIN)

            ws.row_dimensions[row].height = 28  # room for handwriting
            row += 1


# ── Sheet 2: Přehled (Overview) ───────────────────────────────────────────────


def _build_overview_sheet(
    ws: Worksheet,
    events: list[Event],
    date_range: str,
    me_name: str | None,
) -> None:
    max_spots = max((len(_participant_rows(e)) for e in events), default=1)

    fixed_headers = ["Datum", "Název akce", "Stav"]
    fixed_widths = [12, 34, 18]
    spot_width = 28

    for i, w in enumerate(fixed_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for j in range(max_spots):
        ws.column_dimensions[get_column_letter(len(fixed_headers) + j + 1)].width = spot_width

    n_cols = len(fixed_headers) + max_spots
    subtitle = _subtitle(date_range, me_name)
    hdr_row = title_block(ws, "Sestava pro tisk — Přehled", subtitle, n_cols)

    # Column headers
    centre = Alignment(horizontal="center", vertical="center")
    for i, h in enumerate(fixed_headers, 1):
        cell(ws, hdr_row, i, h, font=HEADER_FONT, fill=HEADER_FILL, alignment=centre, border=THIN)
    for j in range(max_spots):
        cell(
            ws,
            hdr_row,
            len(fixed_headers) + j + 1,
            f"Pozice {j + 1}",
            font=HEADER_FONT,
            fill=HEADER_FILL,
            alignment=centre,
            border=THIN,
        )
    ws.row_dimensions[hdr_row].height = 18

    # Data — one row per event
    tz = get_app_tz()
    row = hdr_row + 1
    left = Alignment(horizontal="left", vertical="center", wrap_text=True)

    for event in events:
        participants = _participant_rows(event)
        date_str = event.start_datetime.astimezone(tz).strftime("%d.%m.%Y")

        for col, val in enumerate([date_str, event.name, event.status.value], 1):
            cell(
                ws,
                row,
                col,
                val,
                font=STD_FONT,
                alignment=Alignment(horizontal="left", vertical="center"),
                border=THIN,
            )

        for j in range(max_spots):
            if j < len(participants):
                cell_val = participants[j][0]
            else:
                cell_val = ""
            cell(ws, row, len(fixed_headers) + j + 1, cell_val, font=STD_FONT, alignment=left, border=THIN)

        ws.row_dimensions[row].height = 18
        row += 1


# ── Public entry point ────────────────────────────────────────────────────────


def _subtitle(date_range: str, me_name: str | None) -> str:
    s = f"Období: {date_range}"
    if me_name:
        s += f"  |  Nadřazená akce: {me_name}"
    return s


def generate_printout(
    events: list[Event],
    date_range: str,
    me_name: str | None,
) -> Workbook:
    """Build the printout workbook. Caller is responsible for saving/streaming."""
    wb = Workbook()

    ws_sig = wb.active
    ws_sig.title = "Podpisy"
    _build_signature_sheet(ws_sig, events, date_range, me_name)

    ws_overview = wb.create_sheet(title="Přehled")
    _build_overview_sheet(ws_overview, events, date_range, me_name)

    condition_events = [event for event in events if event.staffing_mode == "CONDITIONS"]
    if condition_events:
        ws_conditions = wb.create_sheet("Podmínky")
        headers = [
            "Akce",
            "Účastníci",
            "Minimum",
            "Maximum",
            "Kvalifikace",
            "Pokryto",
            "Požadováno",
            "Zodpovědná osoba",
            "Účastníci (jména)",
        ]
        for column, header in enumerate(headers, 1):
            cell(ws_conditions, 1, column, header, font=HEADER_FONT, fill=HEADER_FILL)
            ws_conditions.column_dimensions[get_column_letter(column)].width = 24
        row = 2
        for event in condition_events:
            summary = event.staffing_summary
            coverage_rows: list[RequirementCoverage | None] = list(summary.requirements)
            if not coverage_rows:
                coverage_rows.append(None)
            for requirement in coverage_rows:
                values = [
                    event.name,
                    summary.participant_count,
                    summary.minimum,
                    summary.maximum,
                    requirement.qualification.name if requirement else "",
                    requirement.covered if requirement else "",
                    requirement.minimum_count if requirement else "",
                    event.responsible_person.name if summary.rp_valid else "Chybí",
                    ", ".join(a.user.name for a in event.assignments),
                ]
                for column, value in enumerate(values, 1):
                    cell(ws_conditions, row, column, value)
                row += 1
    return wb
