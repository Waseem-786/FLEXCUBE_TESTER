"""
claude_md_generator.py
======================

Pure-Python composer that turns

    (screen_dict_from_db,  workflow_mode,  field_decisions)
                                                     │
                                                     ▼
                              CLAUDE.md (markdown string)

The output mirrors the house style in `samples/CLAUDE.md`:
Configuration block → Login → Navigate → Per-block field actions → Save →
Override popup → Authorization Status → Maker-checker → Technical Requirements.

No LLM call. Deterministic. Same inputs always produce the same markdown,
which is exactly what we want for upgrade-regression diffs.

Field-decision shape (one dict per field):
    {
        "block_name": "FST_MASTER",
        "field_name": "POOLGROUPCODE",
        "mode":       "value" | "today" | "option" | "tick" | "untick" |
                      "lov_match" | "skip",
        "value":      <str | None>   # only for value/option/lov_match
    }
"""

from __future__ import annotations

from typing import Iterable


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

WORKFLOW_MODES = {
    "create_new":     "Create New",
    "bulk_load":      "Bulk Load from Excel",
    # Still stubbed.
    "copy_existing":  "Copy Existing  (coming soon)",
    "modify":         "Modify Existing  (coming soon)",
}


def generate_claude_md(
    screen: dict,
    workflow_mode: str,
    decisions: list[dict],
    *,
    excel_rows: list[dict] | None = None,
    grid_rows: dict[str, list[dict]] | None = None,
    excel_grid_rows: dict[str, list[dict]] | None = None,
) -> str:
    """`grid_rows` (Create New): {block_name: [{field_name: value, ...}, ...]}.
    `excel_grid_rows` (Bulk Load): same shape but read from per-grid Excel
    sheets; bulk composer groups by master key."""
    if workflow_mode == "bulk_load":
        return _generate_bulk_load(screen, decisions, excel_rows or [],
                                    excel_grid_rows or {})
    if workflow_mode != "create_new":
        raise ValueError(
            f"workflow_mode={workflow_mode!r} is not implemented; "
            f"supported modes are 'create_new' and 'bulk_load'."
        )
    grid_rows = grid_rows or {}

    fid = screen["function_id"]
    name = screen["name"]
    blocks = screen["blocks"]
    fields = screen["fields"]
    decisions_by_key = {(d.get("block_name"), d["field_name"]): d for d in decisions}

    field_lookup = {(f["block_name"], f["name"]): f for f in fields}

    out: list[str] = []
    step = _StepCounter()

    # ------------------------------------------------------------- header
    out.append(f"# {name} Automation Prompt")
    out.append("")
    out.append("## Objective")
    out.append(_objective_line(name, fid, blocks, fields, decisions_by_key))
    out.append("")
    out.append("---")
    out.append("")

    # ---------------------------------------------------- Configuration
    out.extend(_config_section(fid))
    out.append("")
    out.append("---")
    out.append("")

    # --------------------------------------------------- Workflow body
    out.append("## Automation Workflow")
    out.append("")

    out.append(step.heading("Login"))
    out.extend([
        "- Open browser",
        "- Navigate to base_url",
        "- Enter username and password",
        "- Click **Sign In**",
        "- **If a session-conflict prompt appears** (e.g. *\"User is already logged in. "
          "Re-enter password to clear previous session.\"* or similar), re-enter the same "
          "password and click **OK** / **Continue** / **Yes**. This kicks the prior session "
          "out and proceeds with the new login.",
        "",
    ])

    out.append(step.heading("Post-login Handling"))
    out.extend([
        "- If informational popup appears, click **OK**",
        "",
    ])

    out.append(step.heading(f"Navigate to Screen {fid}"))
    out.extend([
        "- Locate screen ID input (top-right corner)",
        f"- Enter screen_id from config (`{fid}`)",
        f"- Submit to open the {name} screen",
        "",
    ])

    out.append(step.heading("Initiate New Entry"))
    out.extend([
        "- Click **New** button",
        "",
    ])

    # --- Per-block field actions ------------------------------------
    grid_blocks: list[dict] = []
    used_at_least_one_decision = False

    for block in blocks:
        block_fields = [f for f in fields if f["block_name"] == block["name"]]
        if not block_fields:
            continue
        if block["is_grid"]:
            # Render grids in a separate later step so the form-block flow
            # reads naturally first.
            grid_blocks.append(block)
            continue

        block_lines = _render_block_actions(block, block_fields, decisions_by_key)
        if not block_lines:
            continue
        out.append(step.heading(f"Fill {block['label'] or block['name']}"))
        out.extend(block_lines)
        out.append("")
        used_at_least_one_decision = True

    for grid in grid_blocks:
        grid_fields = [f for f in fields if f["block_name"] == grid["name"]]
        if _all_readonly(grid_fields):
            # Pure read-only grid (e.g. FST_HIST "Previous Values"
            # auto-populated by FLEXCUBE) — nothing for the user to fill.
            continue
        rows = grid_rows.get(grid["name"]) or []
        grid_lines = _render_grid_rows(grid, grid_fields, rows)
        if not grid_lines:
            continue
        out.append(step.heading(f"Add Rows to {grid['label'] or grid['name']}"))
        out.extend(grid_lines)
        out.append("")
        used_at_least_one_decision = True

    if not used_at_least_one_decision:
        out.append("<!-- TODO: no field decisions were provided — fill in the Review page. -->")
        out.append("")

    # --- Save / Validate / Authorize --------------------------------
    out.append(step.heading("Save Record"))
    out.extend([
        "- Click **Save**",
        "- If any **Override** popup appears, click **Accept**",
        "",
    ])

    out.append(step.heading("Validation"))
    out.extend([
        "- Confirm success message or UI confirmation",
        "",
    ])

    auth_step_n = step.peek_next()
    out.append(step.heading("Check Authorization Status"))
    out.extend([
        "- After saving, check the **Authorization Status** field on the screen",
        f"- If status is **Unauthorized** (`U`), proceed to step {auth_step_n + 1}",
        "- If status is **Authorized** (`A`), skip authorization",
        "",
    ])

    pk_field = _pick_primary_key(blocks, fields, decisions_by_key)
    out.append(step.heading("Authorize Record (second user)"))
    out.extend([
        "- Open a second browser session and log in as the checker user "
        "(`accorder_auth_username` / `accorder_auth_password`)",
        f"- Navigate to screen **{fid}**",
        "- Click **Enter Query**",
        f"- Enter the **{pk_field['label'] or pk_field['name']}** value in the corresponding field"
        if pk_field
        else "- Enter the unique identifier of the new record into the corresponding field",
        "- Click **Execute Query** to load the record",
        "- Click **Authorize**",
        "- If any **Accept** popup appears, click **Accept**",
        "- Confirm authorization success message",
        "",
    ])

    out.append("---")
    out.append("")

    # ---------------------------------------------- Technical Requirements
    out.extend([
        "## Technical Requirements",
        "- Use Playwright or Selenium",
        "- Avoid hardcoded values",
        "- Use explicit waits (no sleep)",
        "- Include error handling",
        "- Add logging",
        "- Write modular, clean code",
        "",
    ])

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

class _StepCounter:
    def __init__(self) -> None:
        self._n = 0

    def heading(self, title: str) -> str:
        self._n += 1
        return f"### {self._n}. {title}"

    def peek_next(self) -> int:
        return self._n + 1


def _config_section(fid: str) -> list[str]:
    return [
        "## Configuration",
        "Store all runtime values in a separate configuration file (`config.json` or `.env`).",
        "",
        "### Required Fields:",
        "- base_url",
        "- username",
        "- password",
        f"- screen_id (default: {fid})",
        "- accorder_auth_username",
        "- accorder_auth_password",
    ]


def _objective_line(
    name: str, fid: str, blocks: list[dict], fields: list[dict],
    decisions_by_key: dict,
) -> str:
    used_fields = [
        f for f in fields
        if (f["block_name"], f["name"]) in decisions_by_key
        and decisions_by_key[(f["block_name"], f["name"])]["mode"] != "skip"
    ]
    if not used_fields:
        return (
            f"Create a new record on the **{fid}** ({name}) screen, "
            f"save it, and complete maker-checker authorization."
        )
    key_labels = ", ".join(
        f["label"] or f["name"]
        for f in used_fields[:3]
    )
    suffix = " and additional fields" if len(used_fields) > 3 else ""
    return (
        f"Create a new record on the **{fid}** ({name}) screen by entering "
        f"{key_labels}{suffix}, then save and complete maker-checker authorization."
    )


def _render_block_actions(
    block: dict,
    block_fields: list[dict],
    decisions_by_key: dict,
) -> list[str]:
    """Form-block (single-entry) field actions, in declaration order."""
    lines: list[str] = []
    for f in block_fields:
        action = _field_action_lines(f, decisions_by_key.get((block["name"], f["name"])))
        if action:
            lines.extend(action)
    return lines


def _all_readonly(fields: list[dict]) -> bool:
    """A grid block where every field is read-only (FCJNeoWeb auto-populates
    them) — exclude entirely from the workflow, the review form, and the
    Excel template."""
    return bool(fields) and all(f.get("readonly") for f in fields)


def _render_grid_rows(
    grid: dict,
    grid_fields: list[dict],
    rows: list[dict],
) -> list[str]:
    """Render N rows of an editable grid as one Add-Row block per row.
    `rows` = [{field_name: value, ...}, ...]. Empty list → no output (the
    grid is skipped). Read-only grid columns are silently dropped."""
    if not rows:
        return []
    editable = [f for f in grid_fields if not f.get("readonly")]
    out: list[str] = []
    for i, row in enumerate(rows, start=1):
        # If every value in this row is empty, skip the row entirely so the
        # user doesn't have to manually trim trailing-empty grid editors.
        if not any((row.get(f["name"]) or "").strip()
                   if isinstance(row.get(f["name"]), str)
                   else row.get(f["name"]) not in (None, "")
                   for f in editable):
            continue
        out.append(f"- Click the **+** (Add Row) button on the grid (row {i}).")
        out.append("- In the new row:")
        for f in editable:
            cell = row.get(f["name"])
            if cell in (None, ""):
                continue
            decision = _cell_to_decision(f, cell)
            for line in _field_action_lines(f, decision):
                out.append("  " + line)
    return out


def _cell_to_decision(field: dict, cell) -> dict:
    """Turn a single cell value into the decision shape `_field_action_lines`
    expects. Same type-aware mapping as the bulk-load materialiser."""
    cell_str = str(cell).strip()
    datatype = (field.get("datatype") or "").upper()
    if field.get("lov"):
        return {"mode": "lov_match", "value": cell_str}
    if datatype == "CHECKBOX":
        if cell_str.upper() in _CHECKBOX_TRUTHY:
            return {"mode": "tick", "value": None}
        return {"mode": "skip", "value": None}
    if datatype in ("DROPDOWN", "RADIO"):
        return {"mode": "option", "value": cell_str}
    return {"mode": "value", "value": cell_str}


def _field_action_lines(field: dict, decision: dict | None) -> list[str]:
    """Translate one (field, decision) pair into 1+ markdown bullet lines.
    Returns an empty list if the field should not appear in the workflow
    (skipped, or no decision given for an optional field)."""
    if decision is None or decision["mode"] == "skip":
        # Required fields with no decision get a TODO so the user notices.
        if field.get("required"):
            return [
                f"- <!-- TODO: required field **{field.get('label') or field['name']}** "
                f"({field['name']}) has no value. Update the Review page. -->"
            ]
        return []

    label = field.get("label") or field["name"]
    name = field["name"]
    dtype = (field.get("datatype") or "").upper()
    mode = decision["mode"]
    value = decision.get("value") or ""

    if mode == "lov_match" or (mode == "value" and field.get("lov")):
        return [
            f"- Click the **LOV button** next to the **{label}** ({name}) field.",
            "- In the LOV popup, click **Fetch**.",
            f"- Find and click the row matching `{value}`.",
        ]

    if mode == "option" or (mode == "value" and dtype == "DROPDOWN"):
        return [
            f"- In the **{label}** ({name}) dropdown, select `{value}`.",
        ]

    if mode == "tick":
        return [f"- **Tick** the **{label}** ({name}) checkbox."]
    if mode == "untick":
        return [f"- **Untick** the **{label}** ({name}) checkbox."]

    if mode == "today":
        return [f"- Enter today's date into the **{label}** ({name}) field."]

    # Plain text/number value
    return [f"- Enter `{value}` into the **{label}** ({name}) field."]


def _pick_primary_key(
    blocks: list[dict], fields: list[dict], decisions_by_key: dict,
) -> dict | None:
    """Heuristic: the value the checker queries by. Prefer the first required
    text-like field on the first non-grid block that the user has provided
    a value for. Fall back to any required text field."""
    non_grid_blocks = [b for b in blocks if not b["is_grid"]]
    for b in non_grid_blocks:
        for f in fields:
            if f["block_name"] != b["name"]:
                continue
            if not f["required"]:
                continue
            d = decisions_by_key.get((b["name"], f["name"]))
            if d and d["mode"] != "skip" and d.get("value"):
                return f
    for f in fields:
        if f["required"] and (f["datatype"] or "").upper() in {"VARCHAR2", "VARCHAR", "CHAR", "TEXT"}:
            return f
    return None


# ---------------------------------------------------------------------------
# Form-decision parsing helper used by the Flask route
# ---------------------------------------------------------------------------

BULK_LOAD_ROW_CAP = 50  # safety cap; can be raised once we've battle-tested
                        # longer plans against real FLEXCUBE deployments.


def _generate_bulk_load(
    screen: dict,
    decisions: list[dict],
    excel_rows: list[dict],
    excel_grid_rows: dict[str, list[dict]] | None = None,
) -> str:
    excel_grid_rows = excel_grid_rows or {}
    """Compose a CLAUDE.md that creates ONE record per row in the uploaded
    Excel file. Each row's body reuses the create_new rendering — login,
    Save, validation, and authorize steps appear ONCE at the top/bottom and
    the per-record block (New → fill → Save) is repeated for every row.

    Decisions schema for bulk_load:
      mode='excel'  → value is the Excel column name (= field NAME). The
                      composer looks up `excel_row[value]` for each row.
      mode='value'/'today'/etc. → constant across all rows (same as
                      create_new).
      mode='skip'   → field omitted.

    Grids are deliberately not bulk-handled in this iteration; we emit a
    TODO marker per grid block so the user can fill those manually if they
    care.
    """
    fid = screen["function_id"]
    name = screen["name"]
    blocks = screen["blocks"]
    fields = screen["fields"]

    if not excel_rows:
        # Plan still composes — handy for previewing structure — but the
        # body is just a TODO so the user notices they haven't uploaded.
        return _bulk_empty_plan(fid, name, decisions)

    rows = excel_rows[:BULK_LOAD_ROW_CAP]
    capped = len(excel_rows) > BULK_LOAD_ROW_CAP

    out: list[str] = []
    out.append(f"# {name} Bulk Load Automation Prompt")
    out.append("")
    out.append("## Objective")
    out.append(
        f"Create {len(rows)} new record(s) on the **{fid}** ({name}) screen "
        f"by iterating an Excel data file. For each row, run a New / fill / "
        f"Save sequence; complete maker-checker authorization at the end."
    )
    out.append("")
    out.append("---")
    out.append("")
    out.extend(_config_section(fid))
    out.append("")
    out.append("---")
    out.append("")
    out.append("## Automation Workflow")
    out.append("")

    step = _StepCounter()

    out.append(step.heading("Login"))
    out.extend([
        "- Open browser",
        "- Navigate to base_url",
        "- Enter username and password",
        "- Click **Sign In**",
        "- **If a session-conflict prompt appears** (e.g. *\"User is already logged in. "
          "Re-enter password to clear previous session.\"* or similar), re-enter the same "
          "password and click **OK** / **Continue** / **Yes**. This kicks the prior session "
          "out and proceeds with the new login.",
        "",
    ])
    out.append(step.heading("Post-login Handling"))
    out.extend(["- If informational popup appears, click **OK**", ""])
    out.append(step.heading(f"Navigate to Screen {fid}"))
    out.extend([
        "- Locate screen ID input (top-right corner)",
        f"- Enter screen_id from config (`{fid}`)",
        f"- Submit to open the {name} screen",
        "",
    ])

    # Per-row body: repeat New → fill → Save for each row.
    for i, excel_row in enumerate(rows, start=1):
        title = f"Process row {i} of {len(rows)}"
        # Try to surface a useful identifier in the heading.
        ident_field = _row_identifier(fields, excel_row)
        if ident_field:
            ident_val = excel_row.get(ident_field["name"])
            title += f"  ({ident_field['label'] or ident_field['name']} = {ident_val})"

        out.append(step.heading(title))
        out.append("- Click **New** button.")

        row_decisions = _materialise_for_row(decisions, excel_row, fields)
        decisions_by_key = {(d.get("block_name"), d["field_name"]): d for d in row_decisions}

        for block in blocks:
            block_fields = [f for f in fields if f["block_name"] == block["name"]]
            if not block_fields:
                continue
            if block["is_grid"]:
                if _all_readonly(block_fields):
                    continue
                grid_for_master = _filter_grid_rows_for_master(
                    excel_grid_rows.get(block["name"]) or [], i,
                )
                if not grid_for_master:
                    continue
                grid_lines = _render_grid_rows(block, block_fields, grid_for_master)
                if grid_lines:
                    out.append(f"  Grid: {block['label'] or block['name']}")
                    for line in grid_lines:
                        # nest under the row's bullet structure
                        out.append("  " + line if line.startswith("- ") else line)
                continue

            for f in block_fields:
                action_lines = _field_action_lines(f, decisions_by_key.get((block["name"], f["name"])))
                out.extend(action_lines)

        out.append("- Click **Save**.")
        out.append("- If any **Override** popup appears, click **Accept**.")
        out.append("- Confirm success message or UI confirmation.")
        out.append("")

    if capped:
        out.append(
            f"<!-- TODO: Excel had more rows than the v1 cap "
            f"({BULK_LOAD_ROW_CAP}). Process the remaining "
            f"{len(excel_rows) - BULK_LOAD_ROW_CAP} rows in a follow-up run. -->"
        )
        out.append("")

    auth_step_n = step.peek_next()
    out.append(step.heading("Check Authorization Status"))
    out.extend([
        "- After saving each record, check the **Authorization Status** field.",
        f"- If status is **Unauthorized** (`U`), proceed to step {auth_step_n + 1}.",
        "- If status is **Authorized** (`A`), skip authorization for that record.",
        "",
    ])

    pk_field = _pick_primary_key(blocks, fields, {})
    out.append(step.heading("Authorize each unauthorised record (second user)"))
    out.extend([
        "- Open a second browser session and log in as the checker user "
        "(`accorder_auth_username` / `accorder_auth_password`)",
        f"- Navigate to screen **{fid}**",
        "- For each record that is still Unauthorized:",
        "  - Click **Enter Query**",
        f"  - Enter the **{pk_field['label'] or pk_field['name']}** value in the corresponding field"
        if pk_field
        else "  - Enter the unique identifier of the record into the corresponding field",
        "  - Click **Execute Query** to load the record",
        "  - Click **Authorize**",
        "  - If any **Accept** popup appears, click **Accept**",
        "  - Confirm authorization success message",
        "",
    ])

    out.append("---")
    out.append("")
    out.extend([
        "## Technical Requirements",
        "- Use Playwright or Selenium",
        "- Avoid hardcoded values",
        "- Use explicit waits (no sleep)",
        "- Include error handling",
        "- Add logging",
        "- Write modular, clean code",
        "",
    ])

    return "\n".join(out)


_CHECKBOX_TRUTHY = {"YES", "Y", "TRUE", "1", "TICK", "TICKED", "CHECKED"}


def _filter_grid_rows_for_master(
    grid_rows: list[dict],
    master_index_1based: int,
) -> list[dict]:
    """For bulk-load: pick the grid rows belonging to master record #N.

    Three behaviours, in priority order:

      • No `MASTER_KEY` column on the grid sheet at all → every row
        applies to every master (template / single-master case).

      • Row's `MASTER_KEY` is **blank** → that row is a template row and
        applies to every master record. This is the recommended default
        for screens like IADASFNL where the same fund-allocation grid
        is repeated for each asset.

      • Row's `MASTER_KEY` is filled with `1`/`2`/… → that row is linked
        ONLY to the master record at that 1-based row number. Use this
        when different masters need different grid rows.
    """
    if not grid_rows:
        return []
    if "MASTER_KEY" not in (grid_rows[0] if grid_rows else {}):
        return grid_rows
    target = str(master_index_1based)
    out: list[dict] = []
    for g in grid_rows:
        key = _normalize_master_key(g.get("MASTER_KEY"))
        if not key:
            # Blank → broadcast to every master.
            out.append(g)
        elif key == target:
            out.append(g)
    return out


def _normalize_master_key(v) -> str:
    """Excel sometimes coerces '1' to 1.0 in number-format columns; openpyxl
    then stringifies as '1' via _coerce. We re-normalise here so '1', '1.0',
    1, and 1.0 all match each other."""
    if v in (None, ""):
        return ""
    s = str(v).strip()
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return s


def _materialise_for_row(
    decisions: list[dict],
    excel_row: dict,
    fields: list[dict],
) -> list[dict]:
    """Substitute Excel cell values into 'excel'-mode decisions, mapping the
    cell to the correct decision mode based on the field's datatype:

      LOV-bound       → mode='lov_match'   (cell value = the row to match)
      CHECKBOX        → mode='tick' if cell is Yes/Y/TRUE/1/etc, else 'skip'
      DROPDOWN/RADIO  → mode='option'
      DATE            → mode='value'  (already YYYY-MM-DD via _coerce)
      NUMBER / TEXT   → mode='value'

    Empty cells become 'skip' regardless of field type so an unfilled cell
    doesn't fabricate an "Enter `` into …" line.
    """
    field_lookup = {(f["block_name"], f["name"]): f for f in fields}
    out: list[dict] = []
    for d in decisions:
        if d["mode"] != "excel":
            out.append(d)
            continue

        field = field_lookup.get((d.get("block_name"), d["field_name"])) or {}
        cell = excel_row.get(d.get("value") or d["field_name"])
        out.append(_excel_cell_to_decision(d, cell, field))
    return out


def _excel_cell_to_decision(d: dict, cell, field: dict) -> dict:
    if cell in (None, ""):
        return {**d, "mode": "skip", "value": None}

    cell_str = str(cell).strip()
    datatype = (field.get("datatype") or "").upper()

    if field.get("lov"):
        return {**d, "mode": "lov_match", "value": cell_str}
    if datatype == "CHECKBOX":
        if cell_str.upper() in _CHECKBOX_TRUTHY:
            return {**d, "mode": "tick", "value": None}
        # "No" / "False" / blank-after-strip → skip rather than untick: an
        # untick is a click, which would TOGGLE a checkbox that was already
        # unchecked. Only emit clicks when the user explicitly says Yes.
        return {**d, "mode": "skip", "value": None}
    if datatype in ("DROPDOWN", "RADIO"):
        return {**d, "mode": "option", "value": cell_str}
    return {**d, "mode": "value", "value": cell_str}


def _row_identifier(fields: list[dict], row: dict) -> dict | None:
    """Pick a sensible field whose value to surface in row headings."""
    for f in fields:
        if f.get("required") and f["name"] in row and row[f["name"]] not in (None, ""):
            return f
    return None


def _bulk_empty_plan(fid: str, name: str, decisions: list[dict]) -> str:
    excel_count = sum(1 for d in decisions if d["mode"] == "excel")
    return "\n".join([
        f"# {name} Bulk Load Automation Prompt",
        "",
        "## Objective",
        f"Create N records on the **{fid}** ({name}) screen by iterating "
        f"an Excel data file.",
        "",
        f"<!-- TODO: no Excel file uploaded yet. Upload one with {excel_count} "
        f"column(s) corresponding to the fields marked 'from Excel' on the "
        f"Review page, then click Generate again. -->",
        "",
    ])


GRID_MAX_ROWS = 20  # cap rows per grid editor in the Review UI


def parse_grid_decisions_from_form(
    blocks: list[dict],
    fields: list[dict],
    form,
    *,
    max_rows: int = GRID_MAX_ROWS,
) -> dict[str, list[dict]]:
    """Translate the grid mini-editor's form keys into structured rows:

        grid_<BLOCK>_<ROW_IDX>_<FIELD>=value
            ↓
        { block_name: [ { field_name: value, ... }, ... ] }

    Read-only-only grids are skipped entirely (no editor was rendered).
    Rows where every cell is empty are dropped, so trailing-empty editor
    rows don't manufacture phantom Add-Row steps in the plan."""
    out: dict[str, list[dict]] = {}
    for block in blocks:
        if not block.get("is_grid"):
            continue
        gfields = [f for f in fields
                   if f["block_name"] == block["name"] and not f.get("readonly")]
        if not gfields:
            continue
        rows: list[dict] = []
        for row_idx in range(max_rows):
            row = {}
            for f in gfields:
                key = f"grid_{block['name']}_{row_idx}_{f['name']}"
                v = (form.get(key) or "").strip()
                if v:
                    row[f["name"]] = v
            if row:
                rows.append(row)
        out[block["name"]] = rows
    return out


def parse_decisions_from_form(fields: list[dict], form) -> list[dict]:
    """Translate the multipart form posted by review.html into the decision
    list the generator consumes. The form encoding is per-field with these
    keys (see review.html for the rendering side):

      mode_<block>__<name>     mandatory: skip | value | today | option |
                                          tick | untick | lov_match
      value_<block>__<name>    optional, used when mode requires a value
    """
    decisions: list[dict] = []
    for f in fields:
        key = f"{f['block_name']}__{f['name']}"
        mode = (form.get(f"mode_{key}") or "skip").strip()
        value = (form.get(f"value_{key}") or "").strip() or None

        # 'excel' mode means "this field's value is sourced from the Excel
        # column whose header equals this field's NAME." We don't need a
        # `value` for that — store the field name in `value` so the bulk
        # composer's lookup is uniform with other modes.
        if mode == "excel":
            value = f["name"]

        # If the user picked a value-bearing mode but didn't supply one,
        # fall back to skip rather than emitting an empty quoted string.
        if mode in {"value", "option", "lov_match"} and not value:
            mode = "skip"

        decisions.append({
            "block_name": f["block_name"],
            "field_name": f["name"],
            "mode": mode,
            "value": value,
        })
    return decisions
