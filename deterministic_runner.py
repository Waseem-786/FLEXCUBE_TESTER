"""
deterministic_runner.py
=======================

Standalone CLI that executes a structured plan (output of plan_compiler) via
direct Playwright sync API calls. Runs as a subprocess spawned by Flask;
emits Claude-Code-compatible stream-json events on stdout so the existing
run page renders progress without modification.

Usage (from the Flask runner):
    python deterministic_runner.py \
        --plan        runs/<sid>/<ts>/plan.json \
        --env-file    .env \
        --screenshots-dir runs/<sid>/<ts>/screenshots \
        --function-id IADSKINP

Why subprocess?
- Same isolation guarantees as the Claude Code runner (kill-tree, log file).
- Playwright sync API is happy in a fresh process; running it in Flask's
  request thread would block the event loop.

Why mimic the stream-json event shape?
- run.html already renders {type:assistant,content:[{type:tool_use,...}]}
  events nicely. Re-using that shape means zero UI changes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# Stdlib's argparse is fine; we lazy-import playwright after argparse so a
# bad CLI invocation doesn't hit the slow import path.

import flexcube_selectors as fc


# ---------------------------------------------------------------------------
# Stream-json event emission
# ---------------------------------------------------------------------------

_SESSION_ID = str(uuid.uuid4())


def _emit(obj: dict[str, Any]) -> None:
    """Write one JSON line to stdout and flush. The Flask runner captures
    stdout to a JSONL log; the SSE endpoint tails the log; run.html parses
    each line as a Claude-Code stream-json event."""
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


def _emit_text(text: str) -> None:
    _emit({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    })


def _emit_tool_use(name: str, args: dict) -> str:
    tool_use_id = "deterministic_" + uuid.uuid4().hex[:12]
    _emit({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id":   tool_use_id,
                "name": name,
                "input": args,
            }],
        },
    })
    return tool_use_id


def _emit_tool_result(tool_use_id: str, content: str, is_error: bool = False) -> None:
    _emit({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
                "is_error": is_error,
            }],
        },
    })


# ---------------------------------------------------------------------------
# .env loader (mirrors runner.load_dotenv to avoid an import cycle)
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        env[k.strip()] = v
    return env


# ---------------------------------------------------------------------------
# Variable substitution: $BASE_URL → cfg["FLEXCUBE_BASE_URL"]
# ---------------------------------------------------------------------------

_VAR_MAP = {
    "$BASE_URL": "FLEXCUBE_BASE_URL",
    "$USERNAME": "FLEXCUBE_USERNAME",
    "$PASSWORD": "FLEXCUBE_PASSWORD",
    "$ACCORDER_AUTH_USERNAME": "FLEXCUBE_ACCORDER_AUTH_USERNAME",
    "$ACCORDER_AUTH_PASSWORD": "FLEXCUBE_ACCORDER_AUTH_PASSWORD",
}


def substitute(value: str, cfg: dict[str, str]) -> str:
    """Replace $VAR placeholders with values from cfg. Unmatched placeholders
    are left as-is so a missing optional var fails loudly later, not silently."""
    if not isinstance(value, str):
        return value
    for placeholder, env_key in _VAR_MAP.items():
        if placeholder in value:
            replacement = cfg.get(env_key, "")
            value = value.replace(placeholder, replacement)
    return value


# ---------------------------------------------------------------------------
# Step dispatch
# ---------------------------------------------------------------------------

class _Ctx:
    """Mutable runner state passed through every step. Keeps the page handle,
    the current screen-iframe FrameLocator (set after fast_path), the
    screenshots directory, runtime config, and the optional verified-recipe
    that overrides per-screen selector quirks."""
    def __init__(self, page, cfg: dict, screenshots_dir: Path,
                 recipe: dict | None = None) -> None:
        self.page = page
        self.cfg = cfg
        self.screenshots_dir = screenshots_dir
        self.recipe = recipe or {}
        self.screen_frame = None        # FrameLocator pinned by name (set in fast_path)
        self.screen_iframe_name = None  # the actual `name` attr we pinned to
        self.last_step_title = ""

    def screen(self):
        if self.screen_frame is None:
            raise RuntimeError("screen iframe not yet active — fast_path must run first")
        return self.screen_frame


def _do_navigate(ctx: _Ctx, args: dict) -> str:
    url = substitute(args["url"], ctx.cfg)
    if not url:
        raise RuntimeError("FLEXCUBE_BASE_URL is empty")
    ctx.page.goto(url, wait_until="domcontentloaded")
    return f"navigated to {url}"


def _do_login(ctx: _Ctx, args: dict) -> str:
    username = substitute(args["username"], ctx.cfg)
    password = substitute(args["password"], ctx.cfg)
    role, name = fc.LOGIN_USERNAME_ROLE
    ctx.page.get_by_role(role, name=name).fill(username)
    role, name = fc.LOGIN_PASSWORD_ROLE
    ctx.page.get_by_role(role, name=name).fill(password)
    role, name = fc.LOGIN_SUBMIT_ROLE
    ctx.page.get_by_role(role, name=name).click()
    ctx.page.wait_for_load_state("domcontentloaded")

    # ---- Handle a session-conflict prompt if it appears ---------------
    # When the same user already has an active session, FLEXCUBE prompts
    # for the password again to confirm clearing it. The dialog comes up
    # in an iframe whose title typically mentions "logged in" / "session"
    # / "confirm". We try a few common patterns and silently move on if
    # nothing matches (= no conflict, normal login flow).
    conflict_msg = _try_resolve_session_conflict(ctx, password)
    suffix = f"; {conflict_msg}" if conflict_msg else ""
    return f"signed in as {username}{suffix}"


def _try_resolve_session_conflict(ctx: _Ctx, password: str) -> str | None:
    """Best-effort: if a session-conflict dialog appears, re-enter the
    password and click OK/Yes/Continue. Returns a description if we acted,
    None otherwise. Keeps timeouts short so a clean login isn't slowed
    down by a multi-second wait."""
    # Title patterns we've seen / expect on FCJNeoWeb. If none match, it's
    # almost certainly a clean login.
    title_patterns = [
        "iframe[title*='Logged In' i]",
        "iframe[title*='Session' i]",
        "iframe[title*='Confirm' i]",
        # Some deployments reuse the generic Information popup for this.
        "iframe[title='Information Message']",
    ]
    for sel in title_patterns:
        try:
            ctx.page.wait_for_selector(sel, timeout=1500, state="attached")
        except Exception:
            continue
        try:
            frame = ctx.page.frame_locator(sel)
            # Need a visible password field inside the dialog AND a confirm
            # button — that's the session-conflict shape, not the regular
            # post-login info popup (which only has an Ok button).
            pwd = frame.get_by_role("textbox", name="Password")
            if pwd.count() == 0:
                continue
            pwd.fill(password, timeout=2000)
            for btn_name in ("OK", "Ok", "Yes", "Continue", "Confirm"):
                try:
                    frame.get_by_role("button", name=btn_name).click(timeout=1500)
                    return f"resolved session conflict via {sel} (button: {btn_name})"
                except Exception:
                    continue
            # Couldn't find a confirm button — bail; user (or agent) can
            # finish manually.
            return f"session-conflict dialog matched {sel} but no confirm button"
        except Exception:
            continue
    return None


def _do_dismiss_info_popup(ctx: _Ctx, args: dict) -> str:
    scope = args.get("scope", "page")
    parent = ctx.page if scope == "page" else ctx.screen()
    popup = fc.info_popup_frame(parent)
    role, name = fc.INFO_POPUP_OK_ROLE
    popup.get_by_role(role, name=name).click(timeout=10_000)

    # FCJNeoWeb takes ~1–3s to render the workspace toolbar (where Fast
    # Path lives) after this popup closes. The original successful Claude
    # Code run masked this with an incidental snapshot+screenshot pair
    # between the dismiss and the Fast Path action — about 3 seconds of
    # implicit settling. Without that, the next step's locator can race
    # the workspace render. Wait for Fast Path itself to appear before
    # declaring the dismiss done; if it doesn't show up, fall through and
    # let the next step surface a clearer error.
    if scope == "page":
        try:
            fc.fast_path_locator(ctx.page, timeout_per_attempt_ms=4000)
        except Exception:
            pass
    return f"dismissed info popup ({scope})"


def _do_fast_path(ctx: _Ctx, args: dict) -> str:
    fid = args["function_id"]
    fc.fast_path_locator(ctx.page).fill(fid)
    fc.fast_path_go_locator(ctx.page).click()
    ctx.page.wait_for_selector("iframe[name]:not([name=''])", timeout=30_000)
    ctx.page.wait_for_timeout(800)

    # Pin the screen iframe by its actual `name` attribute. If we left the
    # FrameLocator as `iframe[name]:not(''):visible.last`, every later step
    # would re-evaluate it — and the moment an LOV popup (which is also a
    # named iframe) opens, `.last` would return the popup instead of the
    # screen. Pinning by the discovered name keeps the locator stable for
    # the whole run.
    screen_name = fc.discover_screen_iframe_name(ctx.page)
    if not screen_name:
        raise RuntimeError(
            f"Opened {fid} but no named iframe is visible — page may not have "
            "finished loading the workspace."
        )
    ctx.screen_iframe_name = screen_name
    ctx.screen_frame = fc.screen_frame(ctx.page, name=screen_name)
    return f"opened {fid} (screen iframe name={screen_name})"


def _do_click_screen_action(ctx: _Ctx, args: dict) -> str:
    action = args["action"].upper()
    spec = fc.SCREEN_ACTIONS_ROLE.get(action)
    if not spec:
        raise RuntimeError(f"unknown screen action {action!r}")
    role, name = spec
    ctx.screen().get_by_role(role, name=name).click(timeout=15_000)
    return f"clicked {action}"


def _do_fill_field(ctx: _Ctx, args: dict) -> str:
    label = args["label"]
    value = args["value"]
    fc.field_textbox(ctx.screen(), label).fill(value)
    return f"filled {label} = {value}"


def _do_enter_date(ctx: _Ctx, args: dict) -> str:
    label = args["label"]
    value = args["value"]
    fc.field_textbox(ctx.screen(), label).fill(value)
    return f"date {label} = {value}"


def _do_select_dropdown(ctx: _Ctx, args: dict) -> str:
    label = args["label"]
    value = args["value"]
    dropdown = fc.dropdown_select(ctx.screen(), label)
    # FCJNeoWeb dropdowns are native <select>; select_option matches by
    # value or by visible label, whichever fits.
    try:
        dropdown.select_option(value=value)
    except Exception:
        dropdown.select_option(label=value)
    return f"selected {label} = {value}"


def _do_tick_checkbox(ctx: _Ctx, args: dict) -> str:
    locator, strategy = fc.checkbox_target(ctx.screen(), args["label"], ctx.recipe)
    locator.click()
    return f"ticked {args['label']} (strategy: {strategy})"


def _do_untick_checkbox(ctx: _Ctx, args: dict) -> str:
    locator, strategy = fc.checkbox_target(ctx.screen(), args["label"], ctx.recipe)
    locator.click()
    return f"unticked {args['label']} (strategy: {strategy})"


def _do_select_lov(ctx: _Ctx, args: dict) -> str:
    label = args["label"]
    idx = args["lov_index"]
    row_match = args["row_match"]

    # 1. Click the LOV button at the right index in the screen frame.
    fc.lov_button_for_field(ctx.screen(), idx).click(timeout=15_000)

    # 2. LOV popup. Resolve the FrameLocator now so we can type into it.
    popup = fc.lov_popup_frame(ctx.screen(), label, ctx.recipe)

    # 3. Pre-filter: type `row_match` into the popup's first editable text
    #    input (the standard FCJNeoWeb pattern is a "Code"/"Description"
    #    filter row at the top of the LOV iframe). Without this, Fetch
    #    returns just the first page of all records — fine if the LOV has
    #    a few hundred entries and the target is alphabetically near the
    #    top, fatal if it isn't. Pre-filtering narrows the result set so
    #    the target row is guaranteed visible.
    pre_filter_used = _try_lov_prefilter(popup, label, row_match)

    # 4. Click Fetch. If the input committed an auto-fetch on Tab/Enter
    #    above, the button might be momentarily disabled — tolerate that
    #    by short-timing-out and continuing if the table is already
    #    populated.
    role, name = fc.LOV_FETCH_ROLE
    fetch = popup.get_by_role(role, name=name)
    try:
        fetch.click(timeout=8_000)
    except Exception:
        if not pre_filter_used:
            raise  # no pre-filter and Fetch failed → genuine error

    # 5. Click the result row matching `row_match` (multi-strategy in
    #    flexcube_selectors.link_by_value).
    fc.link_by_value(popup, row_match).click(timeout=15_000)
    suffix = " (pre-filtered)" if pre_filter_used else ""
    return f"LOV {label} → {row_match}{suffix}"


def _try_lov_prefilter(popup, label: str, row_match: str) -> bool:
    """Type `row_match` into the LOV popup's CODE filter input. The standard
    FCJNeoWeb LOV layout is a two-column filter row at the top of the popup
    (Code | Description), so we want to fill the code column — never the
    description, because that would either narrow nothing useful or reject
    the value entirely.

    Strategy, in priority order:
      1. `<Field Label> Code`       — e.g. "Currency Code", "GL Code"
      2. `<Field Label>`             — when the LOV uses the field label
                                       directly as the filter accessible
                                       name (e.g. "Currency", "Asset Code")
      3. Generic code-y filter names — Code / Id / Number / Reference / etc.
      4. First visible non-readonly text input that is NOT a Description /
         Name / Remarks / Address style field. Skipping those keeps us
         from mis-filtering on long-text columns when the LOV's accessible
         name is non-obvious.

    Returns True if any strategy succeeded, False otherwise. False is fine —
    older deployments with no filter row just skip pre-filtering and let
    Fetch+positional search do the work via `link_by_value`."""
    label = (label or "").strip()
    label_candidates: list[str] = []
    if label:
        # Common FLEXCUBE pattern: filter accessible name = "<Label> Code".
        # If the field label already ends in "Code", don't double it up.
        if not label.lower().endswith("code"):
            label_candidates.append(f"{label} Code")
        label_candidates.append(label)

    generic_candidates = (
        "Code", "Id", "ID", "Number", "Reference", "Reference Number",
        "Account", "Customer No", "Branch Code",
    )

    # 1+2: label-driven candidates
    for fname in label_candidates:
        try:
            loc = popup.get_by_role("textbox", name=fname).first
            loc.wait_for(state="visible", timeout=800)
            loc.fill(row_match)
            return True
        except Exception:
            continue

    # 3: generic code-y names
    for fname in generic_candidates:
        try:
            loc = popup.get_by_role("textbox", name=fname).first
            loc.wait_for(state="visible", timeout=600)
            loc.fill(row_match)
            return True
        except Exception:
            continue

    # 4: structural fallback. Walk visible non-readonly text inputs and
    #    skip any whose accessible name looks descriptive — those columns
    #    accept free text but won't filter codes correctly. Prefer inputs
    #    inside the FIRST row of the filter section (typically the only
    #    one that holds the code column).
    descriptive_words = ("description", "name", "remarks", "address",
                         "narration", "long", "text")
    try:
        inputs = popup.locator(
            'input[type="text"]:not([readonly]):not([disabled])'
        )
        count = inputs.count()
    except Exception:
        return False

    for i in range(min(count, 6)):  # cap exploration; LOVs rarely have >2 filters
        candidate = inputs.nth(i)
        try:
            candidate.wait_for(state="visible", timeout=800)
        except Exception:
            continue
        try:
            # Read the input's a11y name (label / aria-label / placeholder)
            # via the DOM. If it screams "description"-like, skip.
            name_lc = (candidate.evaluate(
                "el => (el.getAttribute('aria-label') || el.placeholder || "
                "el.title || (el.labels && el.labels[0] && el.labels[0].textContent) || '').toLowerCase()"
            ) or "").strip()
        except Exception:
            name_lc = ""
        if name_lc and any(w in name_lc for w in descriptive_words):
            continue
        try:
            candidate.fill(row_match)
            return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Grid step handlers — for editable multi-row grid blocks
# ---------------------------------------------------------------------------

def _do_grid_add_row(ctx: _Ctx, args: dict) -> str:
    fc.grid_add_row_button(ctx.screen()).click(timeout=10_000)
    # Brief settle so the new row's inputs are mounted before we target them.
    ctx.page.wait_for_timeout(400)
    grid = args.get("grid_block_name", "")
    return f"added new grid row to {grid}".rstrip()


def _do_grid_fill_field(ctx: _Ctx, args: dict) -> str:
    label = args["label"]; value = args["value"]
    fc.grid_field_in_last_row(ctx.screen(), label).fill(value)
    return f"grid filled {label} = {value}"


def _do_grid_enter_date(ctx: _Ctx, args: dict) -> str:
    label = args["label"]; value = args["value"]
    fc.grid_field_in_last_row(ctx.screen(), label).fill(value)
    return f"grid date {label} = {value}"


def _do_grid_select_dropdown(ctx: _Ctx, args: dict) -> str:
    label = args["label"]; value = args["value"]
    dropdown = fc.grid_field_in_last_row(ctx.screen(), label, datatype="DROPDOWN")
    try:
        dropdown.select_option(value=value)
    except Exception:
        dropdown.select_option(label=value)
    return f"grid selected {label} = {value}"


def _do_grid_tick_checkbox(ctx: _Ctx, args: dict) -> str:
    label = args["label"]
    # Click the LAST occurrence of the label text — that's the new row's
    # checkbox. Same label-click strategy as `_do_tick_checkbox`; recipes
    # can override per checkbox if a deployment needs input-click instead.
    ctx.screen().get_by_text(label, exact=True).last.click()
    return f"grid ticked {label}"


def _do_grid_select_lov(ctx: _Ctx, args: dict) -> str:
    label = args["label"]
    row_match = args["row_match"]
    # Click the last LOV button — the one in the row we just added.
    fc.grid_lov_button_last(ctx.screen()).click(timeout=10_000)
    popup = fc.lov_popup_frame(ctx.screen(), label, ctx.recipe)
    pre_filter_used = _try_lov_prefilter(popup, label, row_match)
    role, name = fc.LOV_FETCH_ROLE
    try:
        popup.get_by_role(role, name=name).click(timeout=8_000)
    except Exception:
        if not pre_filter_used:
            raise
    fc.link_by_value(popup, row_match).click(timeout=15_000)
    suffix = " (pre-filtered)" if pre_filter_used else ""
    return f"grid LOV {label} → {row_match}{suffix}"


# ---------------------------------------------------------------------------
# Replay-from-recording — uses the agent's verified Playwright sequence
# ---------------------------------------------------------------------------

def _do_replay_step(ctx: _Ctx, args: dict) -> str:
    """Replay a step's worth of actions captured from a verified Claude
    Code run (stored as `recipe.step_recordings[<title>]`).

    Substitution rules during replay:
      • Placeholders ($BASE_URL, $USERNAME, $PASSWORD, $FUNCTION_ID, …)
        are filled from the current runtime_config.
      • Per-step data values come from `args.substitutions`: the compiler
        passes a dict like {"value": "FND001"} for an LOV-select step,
        and we replace any literal value in the recording that matched
        the OLD plan's value with the NEW plan's value.
      • Dynamic frame-name selectors (`iframe[name=":numeric:"]`) are
        re-bound to the current pinned screen iframe.
    """
    title = args.get("step_title", "")
    actions = args.get("actions") or []
    subs = args.get("substitutions") or {}

    if not actions:
        return f"replay {title!r}: no actions"

    n_done = 0
    for act in actions:
        op = act.get("op")
        try:
            _execute_replay_action(ctx, act, subs)
            n_done += 1
        except Exception as exc:
            raise RuntimeError(
                f"replay action #{n_done + 1} ({op}) failed in step {title!r}: {exc}"
            )
    return f"replayed {n_done} action(s) for {title!r}"


def _execute_replay_action(ctx: _Ctx, action: dict, subs: dict) -> None:
    op = action.get("op")
    if op == "navigate":
        url = _sub(action.get("url"), ctx.cfg, subs)
        ctx.page.goto(url, wait_until="domcontentloaded")
        return

    # Resolve the frame chain. The leaf is whatever container holds the
    # locator: either ctx.page (top-level) or some FrameLocator chain.
    container = _resolve_frame_chain(ctx, action.get("frame_chain") or [])

    locator = _build_locator(container, action.get("locator") or {}, ctx.cfg, subs)
    if op == "click":
        locator.click(timeout=15_000)
    elif op == "fill":
        value = _sub(action.get("value"), ctx.cfg, subs)
        locator.fill(value)
    elif op == "press":
        key = action.get("key")
        locator.press(key)
    elif op == "select_option":
        value = _sub(action.get("value"), ctx.cfg, subs)
        try:
            locator.select_option(value=value)
        except Exception:
            locator.select_option(label=value)
    else:
        raise RuntimeError(f"unknown replay op: {op!r}")


def _resolve_frame_chain(ctx: _Ctx, chain: list[dict]):
    """Walk a list of frame-hops, returning the FrameLocator (or page)
    the locator should be evaluated against."""
    container = ctx.page
    for hop in chain:
        sel = hop.get("selector") or ""
        # Substitute dynamic screen iframe.
        if 'iframe[name=":numeric:"]' in sel:
            if not ctx.screen_iframe_name:
                # Fall back to "last visible named iframe" if we somehow
                # don't have a pinned name — better than crashing.
                container = ctx.page.frame_locator(
                    "iframe[name]:not([name='']):visible"
                ).last
                continue
            sel = sel.replace(':numeric:', ctx.screen_iframe_name)
        container = container.frame_locator(sel)
    return container


def _build_locator(container, loc: dict, cfg: dict, subs: dict):
    kind = loc.get("kind")
    nth = loc.get("nth")

    if kind == "role":
        role = loc.get("role")
        name = _sub(loc.get("name"), cfg, subs)
        if name is not None:
            base = container.get_by_role(role, name=name, exact=loc.get("exact", False))
        else:
            base = container.get_by_role(role)
    elif kind == "text":
        text = _sub(loc.get("text") or "", cfg, subs)
        base = container.get_by_text(text, exact=loc.get("exact", False))
    elif kind == "placeholder":
        base = container.get_by_placeholder(loc.get("placeholder") or "")
    elif kind == "css":
        base = container.locator(loc.get("selector") or "")
    else:
        raise RuntimeError(f"unknown locator kind: {kind!r}")

    # Apply .nth() / .first() / .last() index
    if nth is None:
        return base
    if nth == "first":
        return base.first
    if nth == "last":
        return base.last
    return base.nth(int(nth))


# Map placeholder strings → runtime_config keys (matches the recorder).
_REPLAY_PLACEHOLDERS = {
    "$BASE_URL":               "FLEXCUBE_BASE_URL",
    "$USERNAME":               "FLEXCUBE_USERNAME",
    "$PASSWORD":               "FLEXCUBE_PASSWORD",
    "$ACCORDER_AUTH_USERNAME": "FLEXCUBE_ACCORDER_AUTH_USERNAME",
    "$ACCORDER_AUTH_PASSWORD": "FLEXCUBE_ACCORDER_AUTH_PASSWORD",
}


def _sub(value, cfg: dict, subs: dict) -> str:
    """Apply placeholder + per-step substitutions. Order:
       1. Per-step `subs` (e.g. {"value": new_lov_value}) — substituted by
          replacing any occurrence of the literal old value (= what was
          recorded) with the new value. This handles cases where the
          recipe extractor couldn't placeholder a per-step value.
       2. $-prefixed placeholders → cfg / $FUNCTION_ID.
    """
    if value is None:
        return None
    s = str(value)
    # Per-step substitutions (old_value → new_value) — substitutions dict
    # is keyed by the OLD value text and maps to the new value.
    for old, new in (subs or {}).items():
        if old and new is not None:
            s = s.replace(str(old), str(new))
    # Placeholder substitution
    if s.startswith("$"):
        if s == "$FUNCTION_ID":
            return subs.get("$FUNCTION_ID") or s
        env_key = _REPLAY_PLACEHOLDERS.get(s)
        if env_key:
            replacement = cfg.get(env_key)
            if replacement:
                return replacement
    return s


def _do_screenshot(ctx: _Ctx, args: dict) -> str:
    name = args["name"]
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", name)
    path = ctx.screenshots_dir / safe
    ctx.page.screenshot(path=str(path), full_page=True)
    return f"saved {safe}"


def _launch_chrome_first(pw):
    """Try real Chrome first (matches the Playwright MCP server's setup that
    we calibrated selectors against), then Edge, then fall back to bundled
    Chromium. We emit a system event noting which one was used so the live
    log shows it for diagnosis."""
    last_err = None
    for channel in ("chrome", "msedge", None):
        kwargs = {
            "headless": False,
            # FLEXCUBE on a self-signed cert; navigation otherwise fails.
            "args": ["--ignore-certificate-errors"],
        }
        if channel is not None:
            kwargs["channel"] = channel
        try:
            browser = pw.chromium.launch(**kwargs)
            _emit({
                "type": "system",
                "subtype": "browser_launched",
                "channel": channel or "bundled-chromium",
            })
            return browser
        except Exception as exc:
            last_err = exc
            continue
    raise RuntimeError(
        f"Could not launch any browser (chrome / msedge / bundled chromium). "
        f"Last error: {last_err}. Install Google Chrome or run "
        f"`python -m playwright install chromium` to fetch the bundled build."
    )


def _do_todo(ctx: _Ctx, args: dict) -> str:
    # No-op step the compiler emits when something isn't supported yet.
    return f"skipped: {args.get('reason', 'todo')}"


_DISPATCH = {
    "navigate":             _do_navigate,
    "login":                _do_login,
    "dismiss_info_popup":   _do_dismiss_info_popup,
    "fast_path":            _do_fast_path,
    "click_screen_action":  _do_click_screen_action,
    "fill_field":           _do_fill_field,
    "enter_date":           _do_enter_date,
    "select_dropdown":      _do_select_dropdown,
    "tick_checkbox":        _do_tick_checkbox,
    "untick_checkbox":      _do_untick_checkbox,
    "select_lov":           _do_select_lov,
    # Grid (multi-row) step handlers — invoked from compiled plans for
    # editable grid blocks. See plan_compiler._compile_grid_steps.
    "grid_add_row":         _do_grid_add_row,
    "grid_fill_field":      _do_grid_fill_field,
    "grid_enter_date":      _do_grid_enter_date,
    "grid_select_dropdown": _do_grid_select_dropdown,
    "grid_tick_checkbox":   _do_grid_tick_checkbox,
    "grid_select_lov":      _do_grid_select_lov,
    "screenshot":           _do_screenshot,
    "todo":                 _do_todo,
    # Replay-from-recording: walks a captured Playwright sequence with
    # placeholder + per-step substitutions. Emitted by plan_compiler when
    # the screen has a verified recipe with step_recordings.
    "replay_step":          _do_replay_step,
}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(plan_path: Path, env_path: Path, screenshots_dir: Path,
        function_id: str, recipe_path: Path | None = None) -> int:
    cfg = load_dotenv(env_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    recipe = None
    if recipe_path and recipe_path.exists():
        try:
            recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            recipe = None
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    _emit({
        "type": "system", "subtype": "init",
        "session_id": _SESSION_ID,
        "model": "deterministic-runner",
        "function_id": function_id,
        "step_count": len(plan),
        "recipe_loaded": bool(recipe),
        "recipe_summary": _recipe_summary(recipe) if recipe else None,
    })

    from playwright.sync_api import sync_playwright  # noqa: E402

    with sync_playwright() as pw:
        # Browser channel matters: FCJNeoWeb's accessibility tree exposes
        # different ARIA roles in real Chrome vs. bundled Chromium (the Fast
        # Path control resolves as `combobox` in Chrome but not in Chromium —
        # this caused a 30s timeout in the first deterministic run). Match
        # the MS Playwright MCP server's `--browser chrome` setup. Fall back
        # to msedge / bundled chromium if Chrome isn't installed.
        browser = _launch_chrome_first(pw)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.set_default_timeout(30_000)

        ctx = _Ctx(page, cfg, screenshots_dir, recipe=recipe)
        last_title = ""

        try:
            for step in plan:
                title = step.get("title") or ""
                if title and title != last_title:
                    _emit_text(title)
                    last_title = title

                kind = step["kind"]
                args = step.get("args") or {}
                handler = _DISPATCH.get(kind)
                if handler is None:
                    _emit_tool_result(_emit_tool_use(kind, args),
                                      f"unknown step kind {kind!r}", is_error=True)
                    raise RuntimeError(f"unknown step kind {kind!r}")

                tid = _emit_tool_use(kind, args)
                try:
                    result = handler(ctx, args)
                except Exception as exc:
                    # Try to capture a screenshot of the failure scene before
                    # bailing — invaluable for debugging.
                    try:
                        err_shot = screenshots_dir / f"error_at_step_{int(time.time())}.png"
                        page.screenshot(path=str(err_shot), full_page=True)
                    except Exception:
                        pass
                    _emit_tool_result(tid, f"{type(exc).__name__}: {exc}", is_error=True)
                    _emit({
                        "type": "result",
                        "subtype": "error",
                        "duration_ms": int((time.time() - started) * 1000),
                        "is_error": True,
                        "result": f"failed at step {title!r}: {exc}",
                    })
                    return 2
                _emit_tool_result(tid, result)

            _emit({
                "type": "result",
                "subtype": "success",
                "duration_ms": int((time.time() - started) * 1000),
            })
            return 0
        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass


def _recipe_summary(recipe: dict) -> str:
    """Compact human-readable recipe overview for the live log's init event."""
    parts = []
    cb = recipe.get("checkbox_strategy") or {}
    if cb:
        parts.append(f"{len(cb)} checkbox override(s)")
    lov = recipe.get("lov_popup_titles") or {}
    if lov:
        parts.append(f"{len(lov)} LOV title(s)")
    return ", ".join(parts) or "no overrides"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan",            required=True, help="Path to plan.json")
    ap.add_argument("--env-file",        required=True, help="Path to .env")
    ap.add_argument("--screenshots-dir", required=True, help="Where to save screenshots")
    ap.add_argument("--function-id",     required=True, help="FLEXCUBE function ID, for the init event")
    ap.add_argument("--recipe",          required=False, default=None,
                    help="Optional path to a verified recipe JSON. If present, the runner "
                         "applies per-screen selector overrides extracted from a successful "
                         "Claude Code run.")
    args = ap.parse_args()
    try:
        return run(
            plan_path=Path(args.plan),
            env_path=Path(args.env_file),
            screenshots_dir=Path(args.screenshots_dir),
            function_id=args.function_id,
            recipe_path=Path(args.recipe) if args.recipe else None,
        )
    except Exception as exc:
        _emit({"type": "result", "subtype": "error", "is_error": True,
               "result": f"{type(exc).__name__}: {exc}"})
        return 3


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
