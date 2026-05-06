"""
meta_generator.py
=================

Build the single self-contained `meta.yaml` that gets handed to Claude.

Inputs:
  - screen_name (user-provided)
  - ScreenModel (from flexcube_uixml_parser)
  - JSAnalysisResult (from flexcube_js_parser, optional)

Output: a YAML string. The user uploads this to a Claude chat with a prompt
like "generate a CLAUDE.md test plan for this screen" and Claude has every
field, dependency, validation, and convention it needs — no extra context
required from the human.
"""

from __future__ import annotations

import yaml


def _str_representer(dumper, data):
    """Render multiline strings as YAML `|` literal blocks instead of quoted scalars."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.add_representer(str, _str_representer, Dumper=yaml.SafeDumper)


HEADER = """\
# ============================================================================
# FLEXCUBE Screen Meta — auto-generated.
#
# Hand this file to Claude with a prompt like:
#   "Generate a CLAUDE.md automation test plan for the FLEXCUBE screen
#    described below. Follow the conventions in `instructions_for_claude`."
#
# Every field, block, button, cross-field dependency, and inferred validation
# below was extracted automatically from the uploaded UIXML + JS files.
# Nothing in this file was hand-entered (except the `screen.name` you typed).
# ============================================================================
"""


# Conventions Claude needs to know about that DON'T live in UIXML or JS —
# they're FLEXCUBE-wide product behaviours. Embedded so the user never has
# to type them.
CLAUDE_INSTRUCTIONS = """\
You are generating a CLAUDE.md automation prompt for an Oracle FLEXCUBE
screen. Use the structured data below as ground truth — do not invent fields,
buttons, or tabs that are absent from this file.

House conventions (apply to every screen):

1. Lifecycle is always: New → fill required fields → Save → Authorize.
2. After every Save, include two steps:
     - "If any Override popup appears, click Accept."
     - "Confirm success message or UI confirmation."
3. After every create/modify Save, include a maker-checker step:
     - Check Authorization Status field. If 'U', login as the checker user
       in a second session, query the record on the same function ID, click
       Authorize, and verify the status flips to 'A'.
4. For LOV-bound fields, always use the pattern:
     - Click the LOV button next to the field.
     - In the LOV popup, click Fetch.
     - Find and click the row matching the desired value.
   Never instruct the runner to type the LOV value directly.
5. For grid (multi-row) blocks, use Click + (Add Row) before each row's
   field entry; structure each row as a nested bullet list.
6. Reference fields by their `label` for human readability and include the
   underlying `name` in parentheses on first mention so the runner can
   locate the element. Example:
     "Enter the FUND_ID value into the Fund ID (FUND_ID) field."
7. The Configuration block at the top of the generated CLAUDE.md must list
   at minimum: base_url, username, password, screen_id,
   accorder_auth_username, accorder_auth_password.

Test cases to include (derive from `fields`, `dependencies`, `validations`):
  - lifecycle: happy-path create + maker-checker authorize.
  - lifecycle: save with all required fields empty.
  - negative: each required field left empty.
  - negative: each LOV-bound field given an invalid value.
  - edge: each VARCHAR field at max length and one over.
  - edge: each NUMBER field with zero, negative, and (if precision is set)
    one extra decimal place.
  - edge: each DATE field with a malformed date.
  - dependency: one case per (source_field, kind, target_field) triple
    in `dependencies`.
"""


def _field_to_dict(f) -> dict:
    """Compact dict — drop empty/default fields so the YAML stays readable."""
    out: dict = {"name": f.name, "label": f.label}
    if f.datatype:
        out["datatype"] = f.datatype
    if f.length is not None:
        out["length"] = f.length
    if f.precision is not None:
        out["precision"] = f.precision
    if f.required:
        out["required"] = True
    if f.readonly:
        out["readonly"] = True
    if f.lov:
        out["lov"] = f.lov
    if f.default:
        out["default"] = f.default
    if getattr(f, "hidden", False):
        out["hidden"] = True
    if getattr(f, "options", None):
        out["options"] = list(f.options)
    return out


def _block_to_dict(b) -> dict:
    out: dict = {"name": b.name, "label": b.label}
    if b.is_grid:
        out["is_grid"] = True
    if b.is_tab:
        out["is_tab"] = True
    if b.parent_tab:
        out["parent_tab"] = b.parent_tab
    out["fields"] = [_field_to_dict(f) for f in b.fields]
    return out


def _button_to_dict(btn) -> dict:
    out: dict = {"name": btn.name}
    if btn.label:
        out["label"] = btn.label
    if btn.parent_block:
        out["parent_block"] = btn.parent_block
    return out


def generate_meta_yaml(screen_name: str, screen_model, js_analysis=None) -> str:
    data: dict = {
        "screen": {
            "name": screen_name,
            "function_id": screen_model.function_id,
            "title": screen_model.title,
        },
        "instructions_for_claude": CLAUDE_INSTRUCTIONS,
        "blocks": [_block_to_dict(b) for b in screen_model.blocks],
        "buttons": [_button_to_dict(b) for b in screen_model.buttons],
    }

    if screen_model.tabs:
        data["tabs"] = [
            {"name": t.name, "label": t.label} for t in screen_model.tabs
        ]
    if screen_model.lovs:
        data["lov_definitions"] = sorted(screen_model.lovs.keys())

    if js_analysis is not None:
        if js_analysis.cross_field_dependencies:
            data["dependencies"] = [
                {"source": s, "kind": k, "target": t}
                for s, k, t in js_analysis.cross_field_dependencies
            ]
        validations = []
        for fname, fb in js_analysis.field_behaviours.items():
            if fb.inferred_validations:
                validations.append({"field": fname, "rules": fb.inferred_validations})
        if validations:
            data["validations"] = validations
        if js_analysis.pre_save_hooks:
            data["pre_save_hooks"] = js_analysis.pre_save_hooks

    body = yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    return HEADER + body
