"""
runner.py
=========

Spawns Claude Code as a subprocess to execute a generated CLAUDE.md plan
against a real FLEXCUBE screen using the Microsoft Playwright MCP server.

Architecture:
    Flask  ──► runner.start_run()  ──► subprocess.Popen("claude -p ...")
                                              │
                                              ├─ stdin: wrapper prompt
                                              │  (refs the plan file, embeds
                                              │   runtime config from .env)
                                              │
                                              ├─ stdout/stderr → log.jsonl
                                              │
                                              └─ MCP tool calls → Chromium
                                                 (browser launches headed
                                                  on the user's machine)

The wrapper prompt is piped via stdin (not argv) so credentials never appear
in `tasklist`/`ps`. Each run gets its own directory under `runs/<screen_id>/<run_id>/`
holding `log.jsonl` and `screenshots/`.

Why a subprocess (not a library import)?
- Claude Code is a CLI tool, not a Python package. Subprocess is its only
  programmatic interface.
- Process isolation means a crashed run can't take down Flask.
- Killing the subprocess (Stop button) cleanly cleans up the agent and the
  browser via `taskkill /T` on Windows.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from mongo_db import (
    create_run, set_run_kind, update_run, get_recipe, get_grid_decisions,
    get_button_decisions,
)
from plan_compiler import compile_plan

PROJECT_ROOT = Path(__file__).resolve().parent
RUNS_DIR = PROJECT_ROOT / "runs"
DOTENV_PATH = PROJECT_ROOT / ".env"
# The MCP server config we ship with the project. The spawned `claude -p` is
# pointed at this file via --mcp-config so it always sees the Playwright
# server regardless of where the user previously ran `claude mcp add`. Without
# this, the agent inherits only the user's local-scope MCP config (tied to
# whatever cwd they originally added the server in) — which usually means no
# MCP tools at all when we spawn from the project root.
MCP_CONFIG_PATH = PROJECT_ROOT / ".mcp.json"

# All env vars are prefixed with FLEXCUBE_ to avoid colliding with system
# variables — notably Windows always sets `USERNAME` (the OS login), which
# would otherwise silently masquerade as the FLEXCUBE username.
REQUIRED_ENV = ["FLEXCUBE_BASE_URL", "FLEXCUBE_USERNAME", "FLEXCUBE_PASSWORD"]
OPTIONAL_ENV = ["FLEXCUBE_ACCORDER_AUTH_USERNAME", "FLEXCUBE_ACCORDER_AUTH_PASSWORD"]

# Logical name → env-var name. The wrapper prompt uses the logical names
# (e.g. "base_url"); they happen to map to the prefixed env vars.
_ENV_TO_LOGICAL = {
    "FLEXCUBE_BASE_URL":                "base_url",
    "FLEXCUBE_USERNAME":                "username",
    "FLEXCUBE_PASSWORD":                "password",
    "FLEXCUBE_ACCORDER_AUTH_USERNAME":  "accorder_auth_username",
    "FLEXCUBE_ACCORDER_AUTH_PASSWORD":  "accorder_auth_password",
}

# MCP server name as registered in the user's Claude Code config. The default
# matches the recommended `claude mcp add playwright -- npx ...` setup. Tools
# end up exposed as `mcp__<name>__<tool>` by Claude Code's wiring.
PLAYWRIGHT_MCP_NAME = os.environ.get("PLAYWRIGHT_MCP_NAME", "playwright")

# In-memory registry of live subprocesses, keyed by run_id. Holding the Popen
# object lets the Stop endpoint kill it. Populated by `start_run`, drained by
# the pump thread when the process exits.
_LIVE: dict[int, subprocess.Popen] = {}
_LIVE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# .env loader (no python-dotenv dep — keep requirements.txt small)
# ---------------------------------------------------------------------------

def load_dotenv(path: Path = DOTENV_PATH) -> dict[str, str]:
    """Tiny .env parser. KEY=VALUE per line, # comments allowed, optional quotes."""
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


def runtime_config(db_path: Path | None = None) -> dict[str, str | None]:
    """Resolve every FLEXCUBE_ runtime setting in priority order:

      1. The Settings page (kv collection in MongoDB).  ← canonical source
      2. The `.env` file at the project root.            ← legacy fallback
      3. os.environ.                                      ← last resort

    The `db_path` argument is kept for back-compat with the SQLite era;
    Mongo doesn't use it (the connection string comes from MONGODB_URI).
    Connection errors fall through silently — `.env` and os.environ act
    as fallbacks so a misconfigured Mongo doesn't lock the user out of
    the run page entirely (they can still work by filling .env).
    """
    _ = db_path  # kept for back-compat
    db_settings: dict[str, str] = {}
    try:
        from mongo_db import get_all_settings
        db_settings = get_all_settings() or {}
    except Exception:
        db_settings = {}

    file_env = load_dotenv()
    out: dict[str, str | None] = {}
    for k in REQUIRED_ENV + OPTIONAL_ENV:
        out[k] = (
            db_settings.get(k)
            or file_env.get(k)
            or os.environ.get(k)
        )
    return out


def runtime_config_status() -> tuple[bool, list[str]]:
    cfg = runtime_config()
    missing = [k for k in REQUIRED_ENV if not cfg.get(k)]
    return (len(missing) == 0, missing)


# ---------------------------------------------------------------------------
# Claude Code CLI discovery
# ---------------------------------------------------------------------------

def claude_cli_path() -> str | None:
    """Resolve the Claude Code CLI on PATH. On Windows this picks up
    claude.cmd / claude.exe correctly via shutil.which."""
    return shutil.which("claude")


def precheck() -> tuple[bool, str | None]:
    """Pre-flight: cheap to call from a Flask route to fail fast with a
    helpful message before spawning anything."""
    if claude_cli_path() is None:
        return (False, "Claude Code CLI not found on PATH. Install with `npm i -g @anthropic-ai/claude-code` and try again.")
    if not MCP_CONFIG_PATH.exists():
        return (False, f"MCP config not found at {MCP_CONFIG_PATH.name}. The project ships this file — restore it (likely from git) and try again.")
    ok, missing = runtime_config_status()
    if not ok:
        return (False, f"Missing required env vars in .env: {', '.join(missing)}. Copy .env.example and fill it in.")
    return (True, None)


# ---------------------------------------------------------------------------
# Wrapper prompt — what we hand to the agent on stdin
# ---------------------------------------------------------------------------

def build_wrapper_prompt(
    plan_path: Path,
    screenshots_dir: Path,
    function_id: str,
    cfg: dict[str, str | None],
) -> str:
    """The instruction the spawned Claude reads. Constrains tool surface,
    pins screenshot directory, and embeds the runtime config so the agent
    knows what to type into the FLEXCUBE login screen."""
    plan_ref = "@" + plan_path.relative_to(PROJECT_ROOT).as_posix()
    shots = screenshots_dir.relative_to(PROJECT_ROOT).as_posix()

    auth_user = cfg.get("FLEXCUBE_ACCORDER_AUTH_USERNAME") or "<not configured>"
    auth_pass = cfg.get("FLEXCUBE_ACCORDER_AUTH_PASSWORD") or "<not configured>"

    return f"""\
You are executing a FLEXCUBE automation plan. Read the plan from {plan_ref} and
follow its numbered steps in order against a real browser.

Runtime configuration — substitute these wherever the plan refers to them:
  base_url:                 {cfg.get('FLEXCUBE_BASE_URL')}
  username:                 {cfg.get('FLEXCUBE_USERNAME')}
  password:                 {cfg.get('FLEXCUBE_PASSWORD')}
  screen_id:                {function_id}
  accorder_auth_username:   {auth_user}
  accorder_auth_password:   {auth_pass}

Hard rules — these are non-negotiable:

1. Use ONLY the Playwright MCP server tools (names like
   `mcp__{PLAYWRIGHT_MCP_NAME}__browser_navigate`, `..._click`, `..._type`,
   `..._press_key`, `..._wait_for`, `..._take_screenshot`, `..._snapshot`).
   You may use Read to re-read the plan if needed. Do NOT use Bash, Write,
   Edit, or any other tool.

2. After completing each numbered step in the plan, take a screenshot. Save
   under `{shots}/` with filename `step_NN_<short-title>.png`. NN is the
   step number zero-padded to two digits.

3. STOP after the maker Save + Validation step. Do NOT execute the
   "Authorize Record (second user)" step — that requires a different login
   session and is handled by a follow-up run.

3a. If logging in surfaces a "user already logged in" / session-conflict
   dialog (FLEXCUBE asks for the password again to clear the previous
   session), re-enter the same password and click OK / Continue / Yes.
   Don't abandon the run on this — it's expected when the same user has
   another active session. After the dialog clears, proceed normally.

3b. **Grid cells in FCJNeoWeb are lazily mounted.** When typing into an
   editable grid cell, do NOT target the underlying `#BLK_<block>__<field>I`
   input by ID — that input only exists in the DOM while the cell has
   focus, and a `.fill()` against it can complete "successfully" while
   the value silently fails to commit when focus moves away. Instead:
     1. Click the cell first via `getByRole('gridcell', {{ name: '<label>' }})`
        — or `getByRole('cell', …)` / the visible label text near the
        target row as fallbacks. The click mounts the input and focuses it.
     2. Type the value with `browser_type` on the now-focused input.
     3. Press **Tab** to fire the on-blur handler so FCJNeoWeb commits the
        value. Without Tab the value can revert.
   Apply this whenever a fill into a grid row's input appears to succeed
   but the visible cell shows blank or the wrong value afterwards.

4. If any action fails (timeout, element not found, FLEXCUBE validation
   error, unexpected popup not described in the plan), STOP, take a
   screenshot named `error_<step-number>.png`, and emit a brief explanation
   of which step you were on and what failed. Do not improvise around
   errors.

5. Do not click any buttons or links not mentioned in the plan.

6. Wait for page transitions via explicit waits (browser_wait_for). Never
   use sleep. Default action timeout: 30 seconds.

7. As you start each numbered step, emit one line of plain text:
   `STEP <N>: <step title from the plan>`. This is what the live progress
   panel shows to the operator.

Begin now.
"""


# ---------------------------------------------------------------------------
# Subprocess lifecycle
# ---------------------------------------------------------------------------

def start_run(
    db_path: Path,
    screen_id: int,
    plan_md: str,
    function_id: str,
) -> tuple[int, str | None]:
    """Persist a run row, write the plan to disk, spawn Claude Code.
    Returns (run_id, error_message_or_None)."""
    ok, why = precheck()
    if not ok:
        return (0, why)

    cfg = runtime_config()
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS_DIR / str(screen_id) / timestamp
    screenshots_dir = run_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    plan_path = run_dir / "CLAUDE.md"
    plan_path.write_text(plan_md, encoding="utf-8")
    log_path = run_dir / "log.jsonl"

    run_id = create_run(
        db_path,
        screen_id=screen_id,
        base_url=cfg.get("BASE_URL"),
        log_path=str(log_path.relative_to(PROJECT_ROOT).as_posix()),
        screenshots_dir=str(screenshots_dir.relative_to(PROJECT_ROOT).as_posix()),
    )

    prompt = build_wrapper_prompt(plan_path, screenshots_dir, function_id, cfg)

    claude = claude_cli_path()
    cmd = [
        claude, "-p",
        # Point at the project-shipped MCP config so the Playwright server is
        # always available regardless of the user's existing `claude mcp add`
        # state. --strict makes this the ONLY MCP source — predictable, no
        # interference from user-configured servers.
        "--mcp-config", str(MCP_CONFIG_PATH),
        "--strict-mcp-config",
        # Non-interactive: there's no human to approve per-tool prompts. We
        # rely on the wrapper prompt's Hard Rules + --strict-mcp-config to
        # constrain the agent to Playwright tools only.
        "--permission-mode", "bypassPermissions",
        "--output-format", "stream-json",
        "--verbose",                # required by Claude Code for stream-json
    ]

    # Inherit current env + push runtime config so the MCP server can also see
    # it if it ever needs to (most won't).
    env = os.environ.copy()
    for k, v in cfg.items():
        if v is not None:
            env[k] = v

    # Force synchronous MCP-server connection. Without this, Claude Code
    # 2.1.116+ runs --mcp-config servers fully async (it logs
    # `[MCP] --mcp-config servers running fully async (MCP_CONNECTION_NONBLOCKING)`
    # at startup) and finalises the agent's tool list BEFORE the playwright
    # server has registered its tools — so the agent stops with "no MCP
    # tools available" even though the server connects fine ~3 seconds
    # later. We diagnosed this from a debug log showing the server
    # successfully connecting AFTER the agent's response. Setting the env
    # var to "false" makes startup wait for MCP servers to register before
    # exposing the toolset to the agent.
    env["MCP_CONNECTION_NONBLOCKING"] = "false"

    # Open log file in binary append; subprocess writes raw bytes here.
    log_fp = log_path.open("wb")

    # Spawn header — written before the subprocess starts so it's the first
    # line of every Claude Code log. Crucial for diagnosing "agent says no
    # MCP tools" because it captures exactly which CLI / flags / config were
    # actually in effect at spawn time. Mimics the stream-json shape so
    # run.html renders it as a normal `system` event.
    spawn_header = {
        "type": "system",
        "subtype": "spawn",
        "claude_path": claude,
        "cmd": cmd,
        "cwd": str(PROJECT_ROOT),
        "mcp_config_path": str(MCP_CONFIG_PATH),
        "mcp_config_content": _safe_read_text(MCP_CONFIG_PATH),
    }
    log_fp.write((json.dumps(spawn_header) + "\n").encode("utf-8"))
    log_fp.flush()

    creationflags = 0
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP lets us send CTRL_BREAK if we wanted,
        # AND lets `taskkill /T /PID` reach Chromium spawned by the MCP server.
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.PIPE,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creationflags,
        )
    except (FileNotFoundError, OSError) as exc:
        log_fp.close()
        update_run(db_path, run_id, status="failed",
                   finished_at=_now_iso(), error_message=f"failed to spawn: {exc}")
        return (run_id, f"failed to spawn Claude Code: {exc}")

    # Pipe the wrapper prompt via stdin, then close so the subprocess sees EOF.
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
    except (BrokenPipeError, OSError) as exc:
        proc.kill()
        log_fp.close()
        update_run(db_path, run_id, status="failed",
                   finished_at=_now_iso(), error_message=f"prompt write failed: {exc}")
        return (run_id, f"failed to write prompt: {exc}")

    update_run(db_path, run_id, status="running", pid=proc.pid)
    set_run_kind(db_path, run_id, "claude_code")

    with _LIVE_LOCK:
        _LIVE[run_id] = proc

    # Reaper thread: wait for exit, update DB, remove from registry.
    threading.Thread(
        target=_reap, args=(db_path, run_id, proc, log_fp), daemon=True
    ).start()

    return (run_id, None)


def start_run_deterministic(
    db_path: Path,
    screen_id: int,
    screen: dict,
    workflow_mode: str,
    decisions: list[dict],
) -> tuple[int, str | None]:
    """v1.2 path: compile the plan to a structured step list, spawn the
    local `deterministic_runner.py` script, stream its stream-json output
    into the same log infrastructure as the Claude Code runner.

    Returns (run_id, error_message_or_None). On success, run_id has been
    inserted into the runs table with status='running' and pid set. The
    reaper thread will update finished_at/exit_code/status on exit.
    """
    # Pre-flight: env vars + dotenv presence (claude CLI is irrelevant here).
    ok, missing = runtime_config_status()
    if not ok:
        return (0, f"Missing required env vars in .env: {', '.join(missing)}. Copy .env.example and fill it in.")

    cfg = runtime_config()
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS_DIR / str(screen_id) / timestamp
    screenshots_dir = run_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    # Load grid-row decisions from the DB (Create New) or read multi-sheet
    # Excel (Bulk Load) so the compiler can emit real Add-Row + fill steps
    # instead of a TODO stub.
    excel_rows = None
    excel_grid_rows: dict[str, list[dict]] = {}
    grid_rows = get_grid_decisions(db_path, screen_id) or {}
    button_decisions = get_button_decisions(db_path, screen_id) or {}

    if workflow_mode == "bulk_load":
        excel_path = screen.get("excel_path")
        if not excel_path:
            return (0, "Bulk Load needs an uploaded Excel file. Upload one on the Review page first.")
        from excel_handler import read_uploaded_full
        try:
            full = read_uploaded_full(PROJECT_ROOT / excel_path)
        except Exception as exc:
            return (0, f"failed to read Excel file: {exc}")
        excel_rows = full.get("_master") or []
        if not excel_rows:
            return (0, "The uploaded Excel file's master sheet has no data rows.")
        for sheet_name, rows in full.items():
            if sheet_name == "_master":
                continue
            excel_grid_rows[sheet_name] = rows

    # Load the verified recipe (if any) BEFORE compiling so the compiler
    # can swap typed step groups for `replay_step` actions where the
    # recording covers them. The same recipe is also persisted next to
    # the plan so the runner subprocess can read selector overrides from
    # disk (checkbox_strategy / lov_popup_titles / etc.).
    recipe = get_recipe(db_path, screen_id)

    # Compile the plan and persist it next to the log/screenshots.
    try:
        plan = compile_plan(
            screen, workflow_mode, decisions,
            excel_rows=excel_rows,
            grid_rows=grid_rows,
            excel_grid_rows=excel_grid_rows,
            button_decisions=button_decisions,
            recipe=recipe,
        )
    except Exception as exc:
        return (0, f"plan compilation failed: {exc}")
    plan_path = run_dir / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    log_path = run_dir / "log.jsonl"

    run_id = create_run(
        db_path,
        screen_id=screen_id,
        base_url=cfg.get("FLEXCUBE_BASE_URL"),
        log_path=str(log_path.relative_to(PROJECT_ROOT).as_posix()),
        screenshots_dir=str(screenshots_dir.relative_to(PROJECT_ROOT).as_posix()),
    )

    recipe_path = run_dir / "recipe.json"
    if recipe is not None:
        recipe_path.write_text(json.dumps(recipe, indent=2), encoding="utf-8")

    cmd = [
        sys.executable,                            # use the same interpreter Flask is running on
        str(PROJECT_ROOT / "deterministic_runner.py"),
        "--plan",            str(plan_path),
        "--env-file",        str(DOTENV_PATH),
        "--screenshots-dir", str(screenshots_dir),
        "--function-id",     screen.get("function_id", "UNKNOWN"),
    ]
    if recipe is not None:
        cmd += ["--recipe", str(recipe_path)]

    env = os.environ.copy()
    # Force UTF-8 stdout so the JSONL log doesn't get cp1252-encoded on
    # Windows (would break SSE parsing on the page).
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    log_fp = log_path.open("wb")

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creationflags,
        )
    except (FileNotFoundError, OSError) as exc:
        log_fp.close()
        update_run(db_path, run_id, status="failed",
                   finished_at=_now_iso(), error_message=f"failed to spawn: {exc}")
        return (run_id, f"failed to spawn deterministic runner: {exc}")

    update_run(db_path, run_id, status="running", pid=proc.pid)
    set_run_kind(db_path, run_id, "deterministic")

    with _LIVE_LOCK:
        _LIVE[run_id] = proc

    threading.Thread(
        target=_reap, args=(db_path, run_id, proc, log_fp), daemon=True
    ).start()

    return (run_id, None)


def _reap(db_path: Path, run_id: int, proc: subprocess.Popen, log_fp) -> None:
    rc = proc.wait()
    log_fp.close()
    with _LIVE_LOCK:
        _LIVE.pop(run_id, None)

    # Status reflects who terminated it. If a Stop button kicked in, the DB
    # row is already 'stopped' — don't overwrite that with 'completed'.
    from mongo_db import get_run as _get  # local import to avoid cycles in some setups
    current = _get(db_path, run_id)
    if current and current.get("status") == "stopped":
        update_run(db_path, run_id, finished_at=_now_iso(), exit_code=rc)
    else:
        update_run(
            db_path, run_id,
            status="completed" if rc == 0 else "failed",
            finished_at=_now_iso(),
            exit_code=rc,
        )


def stop_run(db_path: Path, run_id: int) -> bool:
    """Kill the subprocess tree for this run. Returns True if a live process
    was killed, False if the run had already finished or was unknown."""
    with _LIVE_LOCK:
        proc = _LIVE.get(run_id)
    if proc is None:
        return False
    update_run(db_path, run_id, status="stopped")
    _kill_tree(proc.pid)
    return True


def _kill_tree(pid: int) -> None:
    """Cross-platform 'kill the process and all its descendants'. The MCP
    server spawns Chromium as a child of the Claude process; without /T those
    browser windows get orphaned and pile up on screen."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return f"<could not read {path}>"


def is_live(run_id: int) -> bool:
    with _LIVE_LOCK:
        return run_id in _LIVE
