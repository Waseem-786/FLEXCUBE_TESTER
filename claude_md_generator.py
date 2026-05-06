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
    # Stubbed for v1 — surfaced in the UI as disabled options so we know
    # they're on the roadmap but not yet supported.
    "copy_existing":  "Copy Existing  (coming soon)",
    "modify":         "Modify Existing  (coming soon)",
    "bulk_load":      "Bulk Load from Excel  (coming soon)",
}


def generate_claude_md(
    screen: dict,
    workflow_mode: str,
    decisions: list[dict],
) -> str:
    if workflow_mode != "create_new":
        raise ValueError(
            f"workflow_mode={workflow_mode!r} is not implemented in v1; "
            f"use 'create_new'."
        )

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
        grid_lines = _render_grid_actions(grid, grid_fields, decisions_by_key)
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


def _render_grid_actions(
    grid: dict,
    grid_fields: list[dict],
    decisions_by_key: dict,
) -> list[str]:
    """Multi-entry grid: one Add-Row block, with each field action nested.
    For v1 this describes ONE row; the closing line tells the runner to
    repeat for each additional input row."""
    inner: list[str] = []
    for f in grid_fields:
        decision = decisions_by_key.get((grid["name"], f["name"]))
        if not decision or decision["mode"] == "skip":
            continue
        # Each grid action is rendered as a sub-bullet under "In the new row:"
        inner.extend("  " + line for line in _field_action_lines(f, decision))
    if not inner:
        return []
    return [
        "- Click the **+** (Add Row) button on the grid.",
        "- In the new row:",
        *inner,
        "- Repeat the Add-Row sequence for each additional input row.",
    ]


def _field_action_lines(field: dict, decision: dict | None) -> list[str]:
    """Translate one (field, decision) pair into 1+ markdown bullet lines.
    Returns an empty list if the field should not appear in the workflow
    (skipped, or no decision given for an optional field)."""
    if decision is None or decision["mode"] == "skip":
        # Required fields with no decision get a TODO so the user notices.
        if field["required"]:
            return [
                f"- <!-- TODO: required field **{field['label'] or field['name']}** "
                f"({field['name']}) has no value. Update the Review page. -->"
            ]
        return []

    label = field["label"] or field["name"]
    name = field["name"]
    dtype = (field["datatype"] or "").upper()
    mode = decision["mode"]
    value = decision.get("value") or ""

    if mode == "lov_match" or (mode == "value" and field["lov"]):
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
