"""
excel_handler.py
================

Two responsibilities for the bulk-load workflow:

  1. write_template(decisions, screen, ...) → bytes
     Build an Excel workbook the user downloads, fills offline, and
     re-uploads. One sheet ("Data"), one column per field marked as
     "from Excel" on the review page. Header row uses the field NAME
     (e.g. POOLGROUPCODE) — that's the canonical key that matches what
     the screen uses internally and what our plan compiler refers to.

     **Per-column formatting is type-aware**:
       - DATE         → cells formatted YYYY-MM-DD
       - CHECKBOX     → list-validation dropdown ("Yes" / "No")
       - DROPDOWN /
         RADIO       → list-validation dropdown of the field's parsed options
       - NUMBER       → number format honouring precision
       - LOV-bound   → free text (user types the value to match)
       - Other (text) → free text

  2. read_uploaded(path) → list[dict[str, Any]]
     Parse a filled XLSX into a list of row dicts keyed by header.
     Trims trailing empty rows. Coerces simple types (numbers, dates) to
     reasonable string forms so the materialiser can drop them into "Enter
     <value>" steps without per-cell formatting logic.

We deliberately don't read the field LABEL or any contextual hint from
the spreadsheet — only the column headers (= field NAMES) matter. Users
can rearrange columns or add comment rows; we look up by header.
"""

from __future__ import annotations

import io
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation


SHEET_NAME = "Data"

# Number of data-row cells to pre-format / pre-validate. Excel still applies
# the validation if the user adds rows beyond this, but pre-applying ensures
# the dropdown arrow appears immediately on row 3.
DATA_ROW_RANGE = 200


def write_template(
    decisions: list[dict],
    screen: dict,
    *,
    placeholder_rows: int = 3,
) -> bytes:
    """Return the XLSX bytes for an empty template. Columns = each decision
    with mode='excel'. See module docstring for per-type formatting."""
    excel_decisions = [d for d in decisions if d.get("mode") == "excel"]
    if not excel_decisions:
        # Stub workbook — at least the user gets *something*.
        wb = Workbook()
        ws = wb.active
        ws.title = SHEET_NAME
        ws["A1"] = "No fields marked 'from Excel' yet. Go back to the Review page."
        return _to_bytes(wb)

    field_lookup = {(f["block_name"], f["name"]): f for f in screen["fields"]}

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME

    name_font   = Font(bold=True, color="FFFFFF")
    label_font  = Font(italic=True, color="666666", size=10)
    header_fill = PatternFill("solid", fgColor="2A3142")
    centered    = Alignment(horizontal="left", vertical="center")

    for idx, d in enumerate(excel_decisions, start=1):
        col_letter = get_column_letter(idx)
        field = field_lookup.get((d.get("block_name"), d["field_name"])) or {}
        name = d["field_name"]
        datatype = (field.get("datatype") or "").upper()
        hint = _format_hint(field, datatype)

        # Row 1: field NAME — this is the key the reader looks up by.
        cell = ws.cell(row=1, column=idx, value=name)
        cell.font = name_font
        cell.fill = header_fill
        cell.alignment = centered

        # Row 2: human cue describing the expected format. Ignored by reader.
        cell = ws.cell(row=2, column=idx, value=hint)
        cell.font = label_font
        cell.alignment = centered

        # Apply per-type formatting & validation to the data rows below.
        _apply_column_format(ws, idx, col_letter, field, datatype)

        ws.column_dimensions[col_letter].width = max(len(name), len(hint), 16) + 4

    # Freeze header rows so the user always sees them while scrolling.
    ws.freeze_panes = "A3"

    # Visible-but-blank example rows (formatting/validation already applied).
    for r in range(3, 3 + placeholder_rows):
        for c in range(1, len(excel_decisions) + 1):
            ws.cell(row=r, column=c, value=None)

    return _to_bytes(wb)


def read_uploaded(path: Path) -> list[dict[str, Any]]:
    """Parse a filled XLSX into row dicts keyed by header (field NAME)."""
    wb = load_workbook(filename=str(path), data_only=True, read_only=True)
    ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.active

    headers: list[str] = []
    rows_iter = ws.iter_rows(values_only=True)

    try:
        first = next(rows_iter)
    except StopIteration:
        return []
    for cell in first:
        headers.append(str(cell).strip() if cell is not None else "")

    # Skip the "(label)" hint row.
    try:
        next(rows_iter)
    except StopIteration:
        return []

    out: list[dict[str, Any]] = []
    for raw_row in rows_iter:
        row = {h: _coerce(raw_row[i] if i < len(raw_row) else None)
               for i, h in enumerate(headers) if h}
        if not any(v not in (None, "") for v in row.values()):
            continue
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Per-column format helpers
# ---------------------------------------------------------------------------

CHECKBOX_TRUTHY = {"YES", "Y", "TRUE", "1", "TICK", "TICKED", "CHECKED"}


def _format_hint(field: dict, datatype: str) -> str:
    """Human-readable cue printed in row 2 of each column."""
    label = field.get("label") or field.get("name") or ""
    if datatype == "DATE":
        return f"({label}) — date YYYY-MM-DD"
    if datatype == "CHECKBOX":
        return f"({label}) — Yes (or blank to leave default)"
    if datatype in ("DROPDOWN", "RADIO"):
        opts = field.get("options") or []
        if opts:
            preview = " | ".join(str(o.get("value", "")) for o in opts[:4])
            if len(opts) > 4:
                preview += " | …"
            return f"({label}) — one of: {preview}"
        return f"({label}) — option value"
    if datatype == "NUMBER":
        precision = field.get("precision")
        suffix = f" ({precision} d.p.)" if precision else ""
        return f"({label}) — number{suffix}"
    if field.get("lov"):
        return f"({label}) — value to match (LOV {field['lov']})"
    if datatype in ("VARCHAR2", "VARCHAR", "TEXT", "CHAR"):
        length = field.get("length")
        return f"({label}) — text" + (f" (max {length})" if length else "")
    return f"({label})"


def _apply_column_format(
    ws,
    col_idx: int,
    col_letter: str,
    field: dict,
    datatype: str,
) -> None:
    """Apply Excel cell formatting + data validation for the given field's
    type. Operates on rows 3..(2 + DATA_ROW_RANGE) — enough that the user
    sees the formatting immediately when they start typing."""
    rng_str = f"{col_letter}3:{col_letter}{2 + DATA_ROW_RANGE}"

    # LOV-bound fields are free text. Don't add validation; let the user
    # type the value-to-match.
    if field.get("lov"):
        return

    if datatype == "DATE":
        for r in range(3, 3 + DATA_ROW_RANGE):
            ws.cell(row=r, column=col_idx).number_format = "YYYY-MM-DD"
        return

    if datatype == "CHECKBOX":
        # Excel doesn't have native cell-level checkboxes without ActiveX.
        # The standard pattern is a Yes/No data-validation dropdown.
        dv = DataValidation(
            type="list",
            formula1='"Yes,No"',
            allow_blank=True,
            showErrorMessage=True,
            errorStyle="warning",
            error="Use Yes or No (or leave blank).",
        )
        # showDropDown is inverted in OOXML — `False` makes the arrow visible.
        dv.showDropDown = False
        dv.add(rng_str)
        ws.add_data_validation(dv)
        return

    if datatype in ("DROPDOWN", "RADIO"):
        opts = field.get("options") or []
        if not opts:
            return
        # OOXML literal-list formula1 is capped at ~250 chars including
        # surrounding quotes. Skip data-validation for unusually-long sets;
        # the user can still type a value, and the materialiser still maps
        # it correctly.
        values = [str(o.get("value", "")).replace('"', "'") for o in opts]
        joined = ",".join(v for v in values if v)
        if 0 < len(joined) <= 250:
            dv = DataValidation(
                type="list",
                formula1=f'"{joined}"',
                allow_blank=True,
                showErrorMessage=True,
                errorStyle="warning",
            )
            dv.showDropDown = False
            dv.add(rng_str)
            ws.add_data_validation(dv)
        return

    if datatype == "NUMBER":
        precision = field.get("precision") or 0
        fmt = "0" if not precision else "0." + ("0" * min(precision, 6))
        for r in range(3, 3 + DATA_ROW_RANGE):
            ws.cell(row=r, column=col_idx).number_format = fmt
        return

    # VARCHAR / TEXT / CHAR / unknown → no special formatting (free text).


# ---------------------------------------------------------------------------
# Cell value coercion
# ---------------------------------------------------------------------------

def _coerce(v: Any) -> str | None:
    """Coerce a cell value into the string the materialiser drops into
    `Enter <value>` instructions. The materialiser itself does the
    type-aware mode mapping (e.g. 'Yes' → tick); here we only normalise."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bool):
        # openpyxl can occasionally surface BOOLEANs from formulas; map to
        # the same forms our checkbox materialiser recognises.
        return "Yes" if v else "No"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _to_bytes(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
