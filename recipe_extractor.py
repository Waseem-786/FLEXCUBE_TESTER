"""
recipe_extractor.py
===================

Parses a successful Claude Code stream-json log and extracts a per-screen
recipe the deterministic runner uses to reproduce the run.

Two layers of capture:

1. **Selector overrides** (existing): a sparse dict of adaptations the
   agent had to make — checkbox click strategies, LOV iframe titles, the
   screen iframe's numeric name. Used by the deterministic runner's
   selector helpers.

2. **Step recordings** (new in v1.7): the *full sequence of Playwright
   actions* the agent executed, grouped by `STEP N: title` anchors and
   parameterised against the runtime config (URL/USERNAME/PASSWORD/etc.)
   so subsequent runs can substitute new values. This is what lets the
   deterministic runner reproduce **anything** the agent did — tab
   switching, multi-grid Add-Row sequences, conditional dialogs — without
   us having to anticipate it in plan_compiler.

Recipe schema:
    {
      "captured_from_run_id": int,
      "captured_at":          str (UTC ISO),
      "function_id":          str,

      # — selector overrides (narrow) —
      "checkbox_strategy":    {"<label>": "label_click" | "input_click"},
      "lov_popup_titles":     {"<field_label>": "<exact iframe title>"},
      "screen_iframe_hint":   "<numeric name attr seen>" | None,
      "saw_save_success_popup": bool,

      # — step recordings (full replay) —
      "step_recordings": {
        "<step_title>": [
          {"op":"navigate", "url":"$BASE_URL"},
          {"op":"fill", "frame_chain":[], "locator":{"role":"textbox","name":"User ID"}, "value":"$USERNAME"},
          {"op":"click","frame_chain":[], "locator":{"role":"button","name":"Sign In"}},
          {"op":"click","frame_chain":[
              {"selector":"iframe[name=:numeric:]"},
              {"selector":"iframe[title=\\"List of Values GL Code\\"]"}
            ], "locator":{"role":"button","name":"Fetch"}},
          ...
        ]
      }
    }

Placeholders used in recorded values:
    $BASE_URL, $USERNAME, $PASSWORD, $ACCORDER_AUTH_USERNAME,
    $ACCORDER_AUTH_PASSWORD, $FUNCTION_ID
The runner substitutes these from runtime_config / current plan at replay.
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


# ---------------------------------------------------------------------------
# Playwright JS parser — turns "await page.X.Y.Z(...)" into a struct
# ---------------------------------------------------------------------------

# A handful of focused regexes. Order matters because some patterns are
# subsets of others (e.g. .click() must be detected before .first().click()).
_RE_PW_BLOCK    = re.compile(r"```js\s*\n([\s\S]*?)```")
_RE_GOTO        = re.compile(r"""page\.goto\(\s*(?:'([^']*)'|"([^"]*)")""")
# CSS selectors routinely contain BOTH quote types — e.g.
# `.locator('iframe[name="21154"]')`. A simple `[^'\"]+` character class
# would terminate at the first inner quote and corrupt the selector. Match
# either quoting style and accept any content (other than the matching
# outer quote) inside.
_RE_LOC_CSS     = re.compile(r"""\.locator\(\s*(?:'([^']*)'|"([^"]*)")\s*\)""")
_RE_CONTENT_FR  = re.compile(r"\.contentFrame\(\)")
_RE_GET_BY_ROLE = re.compile(
    r"""\.getByRole\(\s*['"](\w+)['"]            # role
        (?:\s*,\s*\{\s*([^}]*?)\s*\})?            # optional {name, exact, ...}
        \s*\)""",
    re.VERBOSE,
)
_RE_GET_BY_TEXT = re.compile(r"\.getByText\(\s*['\"]([^'\"]+)['\"](?:\s*,\s*\{\s*exact:\s*(true|false)\s*\})?")
_RE_GET_BY_PLACEHOLDER = re.compile(r"\.getByPlaceholder\(\s*['\"]([^'\"]+)['\"]")
_RE_NAME_ARG    = re.compile(r"name:\s*['\"]([^'\"]*)['\"]")
_RE_EXACT_ARG   = re.compile(r"exact:\s*(true|false)")
_RE_NTH         = re.compile(r"\.nth\(\s*(\d+)\s*\)")
_RE_FIRST_LAST  = re.compile(r"\.(first|last)\(\s*\)")
_RE_FILL        = re.compile(r"\.fill\(\s*['\"]([\s\S]*?)['\"]\s*\)\s*;?\s*$")
_RE_TYPE        = re.compile(r"\.type\(\s*['\"]([\s\S]*?)['\"]")
_RE_PRESS       = re.compile(r"\.press\(\s*['\"]([^'\"]+)['\"]\s*\)")
_RE_SELECT_OPT  = re.compile(r"\.selectOption\(\s*['\"]([^'\"]+)['\"]")
_RE_CLICK       = re.compile(r"\.click\(\s*\)")
_RE_IFRAME_NAME_NUMERIC = re.compile(r'iframe\[name="(\d+)"\]')


def parse_playwright_js(js: str) -> dict | None:
    """Parse a single ```js block from a tool_result into a structured
    action. Returns None for unrecognised forms (the runner just skips
    those during replay)."""
    js = js.strip().rstrip(";").strip()
    if not js.startswith("await "):
        return None
    body = js[len("await "):]

    # 1. page.goto
    if m := _RE_GOTO.search(body):
        return {"op": "navigate", "url": m.group(1)}

    # 2. Build the frame chain — every `.locator('iframe[…]').contentFrame()`
    #    pair before the terminal locator is a frame hop. Drop the leading
    #    `page` so cursor starts with `.locator(...)` and the loop's regex
    #    anchors cleanly.
    frame_chain: list[dict] = []
    cursor = body
    if cursor.startswith("page."):
        cursor = cursor[len("page"):]
    # We rely on the .locator('iframe[…]').contentFrame() pattern. Strip
    # those iteratively from the front.
    while True:
        m = _RE_LOC_CSS.match(cursor)
        if not m:
            break
        sel = m.group(1) or m.group(2)
        rest_idx = m.end()
        cf = _RE_CONTENT_FR.match(cursor, rest_idx)
        if not cf:
            break
        # Mark dynamic iframe[name="<numeric>"] for re-resolution at replay
        if _RE_IFRAME_NAME_NUMERIC.search(sel):
            sel = _RE_IFRAME_NAME_NUMERIC.sub('iframe[name=":numeric:"]', sel)
        frame_chain.append({"selector": sel})
        cursor = cursor[cf.end():]

    # `cursor` now starts with the page-level call (after frame hops).
    # `_parse_terminal_locator`'s regexes anchor on `\.getByRole(`,
    # `\.getByText(` etc. — they expect the leading dot. Don't strip it.
    locator = _parse_terminal_locator(cursor)
    if locator is None:
        return None

    # 3. The action is whatever .click() / .fill() / .type() / etc. comes
    #    last. Detect the trailing call.
    if m := _RE_FILL.search(body):
        return {"op": "fill", "frame_chain": frame_chain, "locator": locator, "value": m.group(1)}
    if m := _RE_TYPE.search(body):
        return {"op": "fill", "frame_chain": frame_chain, "locator": locator, "value": m.group(1)}
    if m := _RE_SELECT_OPT.search(body):
        return {"op": "select_option", "frame_chain": frame_chain, "locator": locator, "value": m.group(1)}
    if m := _RE_PRESS.search(body):
        return {"op": "press", "frame_chain": frame_chain, "locator": locator, "key": m.group(1)}
    if _RE_CLICK.search(body):
        return {"op": "click", "frame_chain": frame_chain, "locator": locator}
    return None


def _parse_terminal_locator(s: str) -> dict | None:
    """Parse the remainder after the frame chain (e.g.
    `getByRole('textbox', { name: 'User ID' })` or
    `getByText('Fixed', { exact: true }).last()` or
    `locator('input[name="X"]').first()`) into a structured locator."""
    nth: int | str | None = None
    if m := _RE_NTH.search(s):
        nth = int(m.group(1))
    elif m := _RE_FIRST_LAST.search(s):
        nth = m.group(1)

    if m := _RE_GET_BY_ROLE.search(s):
        role = m.group(1)
        opts = m.group(2) or ""
        name = None
        exact = False
        if nm := _RE_NAME_ARG.search(opts):
            name = nm.group(1)
        if ex := _RE_EXACT_ARG.search(opts):
            exact = ex.group(1) == "true"
        return {"kind": "role", "role": role, "name": name, "exact": exact, "nth": nth}
    if m := _RE_GET_BY_TEXT.search(s):
        return {"kind": "text", "text": m.group(1),
                "exact": (m.group(2) == "true") if m.lastindex == 2 else False, "nth": nth}
    if m := _RE_GET_BY_PLACEHOLDER.search(s):
        return {"kind": "placeholder", "placeholder": m.group(1), "nth": nth}
    if m := _RE_LOC_CSS.search(s):
        return {"kind": "css", "selector": m.group(1) or m.group(2), "nth": nth}
    return None


# ---------------------------------------------------------------------------
# Step-grouped recordings — captured per `STEP N: title` anchor
# ---------------------------------------------------------------------------

# Runtime-config values get masked into placeholders so a replay against a
# different deployment swaps them in cleanly. Keys are placeholders, values
# are the actual values seen during recording.
_PLACEHOLDER_VARS = {
    "$BASE_URL":                "FLEXCUBE_BASE_URL",
    "$USERNAME":                "FLEXCUBE_USERNAME",
    "$PASSWORD":                "FLEXCUBE_PASSWORD",
    "$ACCORDER_AUTH_USERNAME":  "FLEXCUBE_ACCORDER_AUTH_USERNAME",
    "$ACCORDER_AUTH_PASSWORD":  "FLEXCUBE_ACCORDER_AUTH_PASSWORD",
}


def _placeholderise(value: str | None, runtime_cfg: dict, function_id: str | None) -> str | None:
    """Replace any literal value in a recorded action with a placeholder
    if it matches a runtime-config key or the function ID. Idempotent —
    safe to call on values that already are placeholders."""
    if value is None:
        return None
    if function_id and value == function_id:
        return "$FUNCTION_ID"
    for placeholder, env_key in _PLACEHOLDER_VARS.items():
        actual = (runtime_cfg or {}).get(env_key)
        if actual and value == actual:
            return placeholder
    return value


def _placeholderise_action(action: dict, runtime_cfg: dict, function_id: str | None) -> dict:
    """Apply _placeholderise to every text-bearing field of a recorded
    action — values, locator names, URLs."""
    out = {**action}
    if "url" in out:
        out["url"] = _placeholderise(out["url"], runtime_cfg, function_id)
    if "value" in out:
        out["value"] = _placeholderise(out["value"], runtime_cfg, function_id)
    if "locator" in out and isinstance(out["locator"], dict):
        loc = {**out["locator"]}
        if "name" in loc:
            loc["name"] = _placeholderise(loc["name"], runtime_cfg, function_id)
        if "text" in loc:
            loc["text"] = _placeholderise(loc["text"], runtime_cfg, function_id)
        out["locator"] = loc
    return out


def _extract_step_recordings(
    log_path: Path,
    runtime_cfg: dict,
    function_id: str,
) -> dict[str, list[dict]]:
    """Walk the stream-json log; for each agent text event matching
    `STEP N: title`, start a new recording bucket. Each subsequent
    Playwright tool_use's tool_result `js` block is parsed into an action
    and added to the current bucket, with values placeholdered."""
    recordings: dict[str, list[dict]] = {}
    current_title: str | None = None

    if not log_path.exists():
        return recordings

    raw = log_path.read_text(encoding="utf-8", errors="replace")
    step_re = re.compile(r"^\s*STEP\s+(\d+):\s*(.+)\s*$", re.MULTILINE)

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")

        if etype == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text":
                    text = block.get("text") or ""
                    m = step_re.search(text)
                    if m:
                        current_title = f"Step {m.group(1)}: {m.group(2).strip()}"
                        recordings.setdefault(current_title, [])

        elif etype == "user" and current_title is not None:
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") != "tool_result":
                    continue
                content = _content_text(block.get("content"))
                if not content:
                    continue
                pw = _RE_PW_BLOCK.search(content)
                if not pw:
                    continue
                js = pw.group(1).strip()
                action = parse_playwright_js(js)
                if not action:
                    continue
                action = _placeholderise_action(action, runtime_cfg, function_id)
                recordings[current_title].append(action)

    # Drop empty buckets so the recipe stays small.
    return {k: v for k, v in recordings.items() if v}


def extract_recipe_from_log(
    log_path: Path,
    function_id: str,
    run_id: int,
) -> dict[str, Any]:
    """Read a JSONL stream-json log, return a recipe dict. The recipe is
    safe to persist as JSON in `screens.recipe_json`. Never raises on a
    malformed line — yields whatever it could extract."""
    # Pull runtime config now so we can placeholder values during extraction
    runtime_cfg: dict[str, str] = {}
    try:
        from runner import runtime_config
        runtime_cfg = {k: v for k, v in (runtime_config() or {}).items() if v}
    except Exception:
        runtime_cfg = {}

    recipe: dict[str, Any] = {
        "captured_from_run_id":  run_id,
        "captured_at":           datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "function_id":           function_id,
        "checkbox_strategy":     {},
        "lov_popup_titles":      {},
        "screen_iframe_hint":    None,
        "saw_save_success_popup": False,
        "step_recordings":       _extract_step_recordings(log_path, runtime_cfg, function_id),
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
