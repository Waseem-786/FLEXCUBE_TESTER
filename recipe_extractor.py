"""
recipe_extractor.py
===================

Parses a successful Claude Code stream-json log and extracts a per-screen
recipe — the small set of adaptations the agent had to make against the
team's specific FLEXCUBE deployment that the deterministic runner would not
have figured out from the global `flexcube_selectors.py` profile alone.

What we capture:
  • checkbox_strategy      — for each labelled checkbox, did label-click
                             work, or was a different approach needed?
                             (the IADSKINP run showed input-click times out;
                             label-click works.)
  • lov_popup_titles       — actual `iframe[title="..."]` strings used by
                             each LOV. Usually "List of Values <FieldLabel>"
                             but some screens use slightly different titles.
  • screen_iframe_hint     — the numeric `name=` attribute the agent saw
                             (won't match per-session, but recording it
                             helps debug).
  • saw_save_success_popup — boolean: was an info popup dismissed *after*
                             Save? Some flows don't have it.

What we deliberately don't capture (yet):
  • full per-step selector overrides — that'd require mapping each agent
    tool call back to a step in our compiled plan, which is fragile when
    the agent inserts exploratory snapshot calls. Roadmap.
  • LOV row-match selectors — those depend on user values, not screen
    structure. The deterministic runner already constructs them from input.

Output schema (also documented in db._RUNTIME_COLUMNS):
    {
      "captured_from_run_id": int,
      "captured_at":          str (UTC ISO),
      "function_id":          str,
      "checkbox_strategy":    {"<label>": "label_click" | "input_click"},
      "lov_popup_titles":     {"<field_label>": "<exact iframe title>"},
      "screen_iframe_hint":   "<numeric name attr seen>" | None,
      "saw_save_success_popup": bool,
    }
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


# Regexes for the embedded Playwright JS the agent's tool_result blocks
# include. We only need a handful of patterns; everything else stays
# uncaptured and falls through to global selectors.
_RE_GET_BY_TEXT_LABEL = re.compile(
    r"""getByText\(\s*['"]([^'"]+)['"]\s*\)\s*\.click""",
)
_RE_GET_BY_ROLE_CHECKBOX = re.compile(
    r"""getByRole\(\s*['"]checkbox['"][^)]*name:\s*['"]([^'"]+)['"]""",
)
_RE_LOV_IFRAME_TITLE = re.compile(
    r"""iframe\[title\s*=\s*"((?:List of Values|LOV[^"]*)[^"]*)"\]""",
)
_RE_SCREEN_IFRAME_NAME = re.compile(
    r"""iframe\[name\s*=\s*"(\d+)"\]""",
)
_RE_INFO_POPUP = re.compile(
    r"""iframe\[title="Information Message"\]""",
)


def extract_recipe_from_log(
    log_path: Path,
    function_id: str,
    run_id: int,
) -> dict[str, Any]:
    """Read a JSONL stream-json log, return a recipe dict. The recipe is
    safe to persist as JSON in `screens.recipe_json`. Never raises on a
    malformed line — yields whatever it could extract."""
    recipe: dict[str, Any] = {
        "captured_from_run_id":  run_id,
        "captured_at":           datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "function_id":           function_id,
        "checkbox_strategy":     {},
        "lov_popup_titles":      {},
        "screen_iframe_hint":    None,
        "saw_save_success_popup": False,
    }

    last_assistant_text = ""
    pending_checkbox_label: str | None = None

    if not log_path.exists():
        return recipe

    raw = log_path.read_text(encoding="utf-8", errors="replace")
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")

        # Track the most recent free-text from the agent so we can read its
        # contextual notes (e.g. "Currency LOV opened. Click Fetch then PKR.")
        if etype == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text":
                    last_assistant_text = (block.get("text") or "").strip()
                elif block.get("type") == "tool_use":
                    pending_checkbox_label = _maybe_pending_checkbox(block, pending_checkbox_label)

        # Tool results carry the actual Playwright code the agent ran.
        elif etype == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") != "tool_result":
                    continue
                content = block.get("content")
                text = _content_text(content)
                if not text:
                    continue

                _harvest_lov_titles(text, recipe)
                _harvest_screen_iframe_name(text, recipe)
                _harvest_save_success(text, recipe, last_assistant_text)
                _harvest_checkbox_strategy(text, recipe, pending_checkbox_label,
                                           is_error=block.get("is_error", False))
                # Once we've handled the result for a pending checkbox, clear
                # the label so unrelated subsequent results don't confuse us.
                if pending_checkbox_label:
                    pending_checkbox_label = None

    return recipe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _content_text(content: Any) -> str:
    """Tool-result content is sometimes a string, sometimes a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and "text" in c:
                parts.append(c["text"])
        return "\n".join(parts)
    return ""


def _maybe_pending_checkbox(block: dict, prior: str | None) -> str | None:
    """If the agent's tool_use looks like a checkbox interaction, remember
    the field label so we can attribute the next tool_result outcome to it."""
    if block.get("name") not in {"mcp__playwright__browser_click"}:
        return prior
    inp = block.get("input") or {}
    elem = (inp.get("element") or "")
    # Heuristic — the agent tags clicks like "Fixed checkbox" or
    # "Checkbox parent generic", which we can spot.
    if "checkbox" in elem.lower():
        # Try to extract the field name (first word usually = the label).
        first_token = elem.split()[0]
        return first_token
    return prior


def _harvest_lov_titles(text: str, recipe: dict) -> None:
    """Pick up any `iframe[title="List of Values X"]` references and remember
    them keyed by the label segment. The deterministic runner can then ask
    `recipe.lov_popup_titles[label]` if its default title-pattern misses."""
    for match in _RE_LOV_IFRAME_TITLE.finditer(text):
        title = match.group(1)
        # Title format: "List of Values <Field Label>" → label = trailing.
        if title.startswith("List of Values "):
            label = title[len("List of Values "):].strip()
            if label:
                recipe["lov_popup_titles"][label] = title


def _harvest_screen_iframe_name(text: str, recipe: dict) -> None:
    if recipe["screen_iframe_hint"]:
        return
    m = _RE_SCREEN_IFRAME_NAME.search(text)
    if m:
        recipe["screen_iframe_hint"] = m.group(1)


def _harvest_save_success(text: str, recipe: dict, last_text: str) -> None:
    """If we see an `iframe[title="Information Message"]` reference *after*
    a Save click (heuristic: the agent's last text mentions Save success),
    flag the screen as having a save-success popup."""
    if recipe["saw_save_success_popup"]:
        return
    if not _RE_INFO_POPUP.search(text):
        return
    if "save" in last_text.lower() or "saved" in last_text.lower() or "record" in last_text.lower():
        recipe["saw_save_success_popup"] = True


def _harvest_checkbox_strategy(
    text: str,
    recipe: dict,
    label: str | None,
    is_error: bool,
) -> None:
    """Compare the agent's actual click code against the strategies we
    support. The IADSKINP run showed:
      1) tried getByRole('checkbox', name='Fixed') → timeout / error
      2) fell back to getByText('Fixed') → success
    So we record `Fixed: label_click`."""
    if not label:
        return
    if _RE_GET_BY_TEXT_LABEL.search(text) and not is_error:
        recipe["checkbox_strategy"][label] = "label_click"
    elif _RE_GET_BY_ROLE_CHECKBOX.search(text) and not is_error:
        recipe["checkbox_strategy"][label] = "input_click"
    # If error, don't record a strategy — wait for the successful retry.
