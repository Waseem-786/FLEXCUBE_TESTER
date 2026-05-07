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
        self.screen_frame = None  # set after fast_path step
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
    # Wait for the post-login chrome to appear before continuing.
    ctx.page.wait_for_load_state("domcontentloaded")
    return f"signed in as {username}"


def _do_dismiss_info_popup(ctx: _Ctx, args: dict) -> str:
    scope = args.get("scope", "page")
    parent = ctx.page if scope == "page" else ctx.screen()
    popup = fc.info_popup_frame(parent)
    role, name = fc.INFO_POPUP_OK_ROLE
    popup.get_by_role(role, name=name).click(timeout=10_000)
    return f"dismissed info popup ({scope})"


def _do_fast_path(ctx: _Ctx, args: dict) -> str:
    fid = args["function_id"]
    role, name = fc.FAST_PATH_ROLE
    ctx.page.get_by_role(role, name=name).fill(fid)
    role, name = fc.FAST_PATH_GO_ROLE
    ctx.page.get_by_role(role, name=name).click()
    # Give the screen iframe time to mount. We don't have a stable hook so
    # poll for any iframe whose name attribute looks numeric.
    ctx.page.wait_for_selector("iframe[name]:not([name=''])", timeout=30_000)
    # Brief settle so dynamically-injected content registers.
    ctx.page.wait_for_timeout(800)
    ctx.screen_frame = fc.screen_frame(ctx.page)
    return f"opened {fid}"


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

    # 2. Wait for the LOV iframe and click Fetch inside it.
    popup = fc.lov_popup_frame(ctx.screen(), label, ctx.recipe)
    role, name = fc.LOV_FETCH_ROLE
    fetch = popup.get_by_role(role, name=name)
    fetch.click(timeout=15_000)

    # 3. Click the result row matching `row_match`.
    fc.link_by_value(popup, row_match).click(timeout=15_000)
    return f"LOV {label} → {row_match}"


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
    "navigate":            _do_navigate,
    "login":               _do_login,
    "dismiss_info_popup":  _do_dismiss_info_popup,
    "fast_path":           _do_fast_path,
    "click_screen_action": _do_click_screen_action,
    "fill_field":          _do_fill_field,
    "enter_date":          _do_enter_date,
    "select_dropdown":     _do_select_dropdown,
    "tick_checkbox":       _do_tick_checkbox,
    "untick_checkbox":     _do_untick_checkbox,
    "select_lov":          _do_select_lov,
    "screenshot":          _do_screenshot,
    "todo":                _do_todo,
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
