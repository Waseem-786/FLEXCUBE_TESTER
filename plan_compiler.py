"""
plan_compiler.py
================

Compiles the same `(screen, decisions, workflow_mode)` triple that the
markdown generator consumes into a list of structured step dicts.

The deterministic runner reads this output and dispatches each step kind
to a Playwright API call. Keeping the data model separate from the runner
makes it easy to:
  - inspect the plan without spawning Playwright
  - unit-test the compiler in isolation
  - regenerate the plan after a Review-page edit without re-running the
    markdown composer

Step shape:
    {
      "kind":  "<one of STEP_KINDS>",
      "title": "<human-readable, used for STEP N: log lines>",
      "args":  { ... kind-specific ... }
    }

Step kinds (v1.2):
  navigate              args: { url }
  login                 args: { username, password }
  dismiss_info_popup    args: { scope: 'page' | 'screen' }
  fast_path             args: { function_id }
  click_screen_action   args: { action: NEW|SAVE|... }
  fill_field            args: { label, value }
  select_dropdown       args: { label, value }
  tick_checkbox         args: { label }
  untick_checkbox       args: { label }
  enter_date            args: { label, value: 'today' | 'YYYY-MM-DD' }
  select_lov            args: { label, lov_index, row_match }
  screenshot            args: { name }

A whole maker run on Create-New for IADSKINP compiles to ~25 steps.
"""

from __future__ import annotations

from datetime import date
from typing import Any


BULK_LOAD_ROW_CAP = 50


def compile_plan(
    screen: dict,
    workflow_mode: str,
    decisions: list[dict],
    *,
    excel_rows: list[dict] | None = None,
) -> list[dict[str, Any]]:
    if workflow_mode == "bulk_load":
        return _compile_bulk_load(screen, decisions, excel_rows or [])
    if workflow_mode != "create_new":
        raise ValueError(
            f"workflow_mode={workflow_mode!r} is not implemented; "
            f"deterministic runner supports 'create_new' and 'bulk_load'."
        )

    fid = screen["function_id"]
    blocks = screen["blocks"]
    fields = screen["fields"]
    decisions_by_key = {(d.get("block_name"), d["field_name"]): d for d in decisions}

    # Build LOV-index map: for each (block, field) that's LOV-bound, what
    # index does its LOV button have on the screen? FLEXCUBE renders fields
    # in UIXML declaration order, so we walk that order and number LOV-bound
    # fields starting at 0. This matches the `.first()` / `.nth(1)` indexing
    # the agent's successful run used.
    lov_index_map: dict[tuple[str | None, str], int] = {}
    next_idx = 0
    for f in fields:
        if f.get("lov"):
            lov_index_map[(f["block_name"], f["name"])] = next_idx
            next_idx += 1

    steps: list[dict[str, Any]] = []
    n = _Numbering()

    # ----- 1. Login --------------------------------------------------------
    steps.append({
        "kind":  "navigate",
        "title": n.title("Login"),
        "args":  {"url": "$BASE_URL"},  # runner substitutes from .env
    })
    steps.append({
        "kind":  "login",
        "title": n.same_title(),
        "args":  {"username": "$USERNAME", "password": "$PASSWORD"},
    })
    steps.append({"kind": "screenshot", "title": n.same_title(),
                  "args": {"name": f"step_{n.idx:02d}_login.png"}})

    # ----- 2. Post-login info popup ---------------------------------------
    steps.append({
        "kind":  "dismiss_info_popup",
        "title": n.title("Post-login Handling"),
        "args":  {"scope": "page"},
    })
    steps.append({"kind": "screenshot", "title": n.same_title(),
                  "args": {"name": f"step_{n.idx:02d}_post_login.png"}})

    # ----- 3. Navigate to function via Fast Path --------------------------
    steps.append({
        "kind":  "fast_path",
        "title": n.title(f"Navigate to Screen {fid}"),
        "args":  {"function_id": fid},
    })
    steps.append({"kind": "screenshot", "title": n.same_title(),
                  "args": {"name": f"step_{n.idx:02d}_navigate_{fid.lower()}.png"}})

    # ----- 4. New entry ---------------------------------------------------
    steps.append({
        "kind":  "click_screen_action",
        "title": n.title("Initiate New Entry"),
        "args":  {"action": "NEW"},
    })
    steps.append({"kind": "screenshot", "title": n.same_title(),
                  "args": {"name": f"step_{n.idx:02d}_new_entry.png"}})

    # ----- 5..N. Fill blocks, then grids ----------------------------------
    grid_blocks: list[dict] = []
    for block in blocks:
        block_fields = [f for f in fields if f["block_name"] == block["name"]]
        if not block_fields:
            continue
        if block.get("is_grid"):
            grid_blocks.append(block)
            continue

        block_steps = _block_steps(block, block_fields, decisions_by_key, lov_index_map)
        if not block_steps:
            continue
        title = n.title(f"Fill {block.get('label') or block['name']}")
        for s in block_steps:
            s["title"] = title
            steps.append(s)
        steps.append({"kind": "screenshot", "title": title,
                      "args": {"name": f"step_{n.idx:02d}_fill_{block['name'].lower()}.png"}})

    for grid in grid_blocks:
        gfields = [f for f in fields if f["block_name"] == grid["name"]]
        # v1.2: emit one Add-Row sequence; Excel-loop is roadmap.
        # No structural steps for Add Row yet — would need a row-add screen
        # action selector we haven't validated. For now we skip grids in the
        # deterministic runner and emit a marker step the runner reports.
        title = n.title(f"Add Rows to {grid.get('label') or grid['name']}")
        steps.append({
            "kind":  "todo",
            "title": title,
            "args":  {"reason": "grid Add-Row not yet supported by the deterministic runner"},
        })

    # ----- N+1. Save ------------------------------------------------------
    steps.append({
        "kind":  "click_screen_action",
        "title": n.title("Save Record"),
        "args":  {"action": "SAVE"},
    })
    # FLEXCUBE shows an info popup ("Record Successfully Saved") nested
    # inside the screen frame. Dismiss it.
    steps.append({
        "kind":  "dismiss_info_popup",
        "title": n.same_title(),
        "args":  {"scope": "screen"},
    })
    steps.append({"kind": "screenshot", "title": n.same_title(),
                  "args": {"name": f"step_{n.idx:02d}_save.png"}})

    # ----- N+2. Validation screenshot ------------------------------------
    steps.append({
        "kind":  "screenshot",
        "title": n.title("Validation"),
        "args":  {"name": f"step_{n.idx:02d}_validation.png"},
    })

    # Maker-checker authorize is intentionally NOT compiled. v1.1 runner
    # rule: stop after the maker save. v1.2 deterministic runner mirrors
    # that until a separate Authorize button (or a checker config option)
    # is added.

    return steps


# ---------------------------------------------------------------------------
# Block-level compilation
# ---------------------------------------------------------------------------

def _block_steps(
    block: dict,
    block_fields: list[dict],
    decisions_by_key: dict,
    lov_index_map: dict[tuple[str | None, str], int],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in block_fields:
        decision = decisions_by_key.get((block["name"], f["name"]))
        out.extend(_field_steps(block, f, decision, lov_index_map))
    return out


def _field_steps(
    block: dict,
    field: dict,
    decision: dict | None,
    lov_index_map: dict,
) -> list[dict[str, Any]]:
    if decision is None or decision["mode"] == "skip":
        return []

    label = field["label"] or field["name"]
    dtype = (field.get("datatype") or "").upper()
    mode = decision["mode"]
    value = decision.get("value") or ""

    # LOV: the runner needs both the visible label AND the positional index
    # so it can pick the right "List of Values" button on screen.
    if mode == "lov_match" or (mode == "value" and field.get("lov")):
        idx = lov_index_map.get((block["name"], field["name"]))
        if idx is None:
            # Shouldn't happen — the field is LOV-bound but missing from the
            # index map. Emit a TODO step the runner can report.
            return [{"kind": "todo", "title": "",
                     "args": {"reason": f"LOV index not found for {field['name']}"}}]
        return [{"kind": "select_lov", "title": "",
                 "args": {"label": label, "lov_index": idx, "row_match": value}}]

    if mode == "option" or (mode == "value" and dtype == "DROPDOWN"):
        return [{"kind": "select_dropdown", "title": "",
                 "args": {"label": label, "value": value}}]

    if mode == "tick":
        return [{"kind": "tick_checkbox", "title": "",
                 "args": {"label": label}}]
    if mode == "untick":
        return [{"kind": "untick_checkbox", "title": "",
                 "args": {"label": label}}]

    if mode == "today":
        today = date.today().isoformat()
        return [{"kind": "enter_date", "title": "",
                 "args": {"label": label, "value": today}}]

    if dtype == "DATE":
        return [{"kind": "enter_date", "title": "",
                 "args": {"label": label, "value": value}}]

    # Plain text/number — same step kind, runner just fills it.
    return [{"kind": "fill_field", "title": "",
             "args": {"label": label, "value": value}}]


# ---------------------------------------------------------------------------
# Numbering helper — produces "Step N" titles, one per logical group
# ---------------------------------------------------------------------------

def _compile_bulk_load(
    screen: dict,
    decisions: list[dict],
    excel_rows: list[dict],
) -> list[dict[str, Any]]:
    """Bulk-load: login + navigate once, then per-row [New, fill, Save]."""
    fid = screen["function_id"]
    blocks = screen["blocks"]
    fields = screen["fields"]
    rows = excel_rows[:BULK_LOAD_ROW_CAP]

    # Reuse the same LOV-index map; LOV positions don't change per row.
    lov_index_map: dict[tuple[str | None, str], int] = {}
    next_idx = 0
    for f in fields:
        if f.get("lov"):
            lov_index_map[(f["block_name"], f["name"])] = next_idx
            next_idx += 1

    steps: list[dict[str, Any]] = []
    n = _Numbering()

    # ----- Once-only: login + navigate ------------------------------------
    steps.append({"kind": "navigate", "title": n.title("Login"),
                  "args": {"url": "$BASE_URL"}})
    steps.append({"kind": "login", "title": n.same_title(),
                  "args": {"username": "$USERNAME", "password": "$PASSWORD"}})
    steps.append({"kind": "screenshot", "title": n.same_title(),
                  "args": {"name": f"step_{n.idx:02d}_login.png"}})

    steps.append({"kind": "dismiss_info_popup", "title": n.title("Post-login Handling"),
                  "args": {"scope": "page"}})
    steps.append({"kind": "fast_path", "title": n.title(f"Navigate to Screen {fid}"),
                  "args": {"function_id": fid}})

    # ----- Per-row body ----------------------------------------------------
    if not rows:
        steps.append({"kind": "todo", "title": n.title("Process rows"),
                      "args": {"reason": "no Excel rows to process"}})
        return steps

    for i, excel_row in enumerate(rows, start=1):
        title = f"Process row {i} of {len(rows)}"
        ident = _row_identifier(fields, excel_row)
        if ident:
            title += f"  ({ident['label'] or ident['name']} = {excel_row.get(ident['name'])})"

        row_decisions = _materialise_for_row(decisions, excel_row, fields)
        decisions_by_key = {(d.get("block_name"), d["field_name"]): d for d in row_decisions}

        steps.append({"kind": "click_screen_action", "title": n.title(title),
                      "args": {"action": "NEW"}})

        for block in blocks:
            block_fields = [f for f in fields if f["block_name"] == block["name"]]
            if not block_fields or block.get("is_grid"):
                continue
            for f in block_fields:
                d = decisions_by_key.get((block["name"], f["name"]))
                steps.extend(_field_step_objs(block, f, d, lov_index_map, title))

        steps.append({"kind": "click_screen_action", "title": n.same_title(),
                      "args": {"action": "SAVE"}})
        steps.append({"kind": "dismiss_info_popup", "title": n.same_title(),
                      "args": {"scope": "screen"}})
        steps.append({"kind": "screenshot", "title": n.same_title(),
                      "args": {"name": f"step_{n.idx:02d}_row_{i:02d}.png"}})

    return steps


_CHECKBOX_TRUTHY = {"YES", "Y", "TRUE", "1", "TICK", "TICKED", "CHECKED"}


def _materialise_for_row(
    decisions: list[dict],
    excel_row: dict,
    fields: list[dict],
) -> list[dict]:
    """Substitute Excel cell values into 'excel'-mode decisions, mapping
    each cell to the correct mode based on the field's datatype. Mirror
    of `claude_md_generator._materialise_for_row` — keep the two in sync.
    See that function's docstring for the mode-mapping table."""
    field_lookup = {(f["block_name"], f["name"]): f for f in fields}
    out: list[dict] = []
    for d in decisions:
        if d["mode"] != "excel":
            out.append(d)
            continue

        field = field_lookup.get((d.get("block_name"), d["field_name"])) or {}
        cell = excel_row.get(d.get("value") or d["field_name"])

        if cell in (None, ""):
            out.append({**d, "mode": "skip", "value": None})
            continue

        cell_str = str(cell).strip()
        datatype = (field.get("datatype") or "").upper()

        if field.get("lov"):
            out.append({**d, "mode": "lov_match", "value": cell_str})
        elif datatype == "CHECKBOX":
            if cell_str.upper() in _CHECKBOX_TRUTHY:
                out.append({**d, "mode": "tick", "value": None})
            else:
                out.append({**d, "mode": "skip", "value": None})
        elif datatype in ("DROPDOWN", "RADIO"):
            out.append({**d, "mode": "option", "value": cell_str})
        else:
            out.append({**d, "mode": "value", "value": cell_str})
    return out


def _row_identifier(fields: list[dict], row: dict) -> dict | None:
    for f in fields:
        if f.get("required") and f["name"] in row and row[f["name"]] not in (None, ""):
            return f
    return None


def _field_step_objs(block, field, decision, lov_index_map, title) -> list[dict]:
    """Same as _field_steps but pre-titles the steps so they're attributable
    to the correct row in the live log."""
    base = _field_steps(block, field, decision, lov_index_map)
    for s in base:
        s["title"] = title
    return base


class _Numbering:
    """Tracks the current logical step number. `title()` increments it and
    returns the prefixed title; `same_title()` returns the title without
    incrementing — used so multiple step records (the action + its
    screenshot) share the same step number, matching the markdown's
    numbering."""

    def __init__(self) -> None:
        self.idx = 0
        self._last = ""

    def title(self, t: str) -> str:
        self.idx += 1
        self._last = f"Step {self.idx}: {t}"
        return self._last

    def same_title(self) -> str:
        return self._last
