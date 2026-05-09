"""
app.py
======

Flask web app for FLEXCUBE Screen Tester.

Flow:
  1. User uploads UIXML + (optional) JS + screen name.
  2. App parses both files, persists everything to SQLite, and generates a
     single self-contained `meta.yaml`.
  3. User downloads the meta.yaml and hands it to Claude to produce a
     CLAUDE.md test plan.

Run:
  python app.py
  → http://127.0.0.1:5050   (override with FLASK_PORT=xxxx)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from flask import (
    Flask, abort, flash, get_flashed_messages, redirect, render_template,
    request, Response, send_from_directory, stream_with_context, url_for,
)

import runner
from claude_md_generator import (
    WORKFLOW_MODES, generate_claude_md, parse_decisions_from_form,
    parse_grid_decisions_from_form,
)
from mongo_db import (
    clear_excel_upload, create_run, delete_screen, get_all_settings,
    get_button_decisions, get_claude_md, get_eligible_verify_run,
    get_field_decisions, get_grid_decisions, get_meta_yaml, get_recipe,
    get_run, get_screen, init_db, list_runs, list_screens, mark_verified,
    save_button_decisions, save_excel_upload, save_field_decisions,
    save_grid_decisions, save_screen, set_settings, unmark_verified,
    update_run,
)
from excel_handler import (
    read_uploaded as read_uploaded_excel,
    read_uploaded_full as read_uploaded_excel_full,
    write_template,
)
from recipe_extractor import extract_recipe_from_log
from flexcube_js_parser import FlexcubeJSParser
from flexcube_uixml_parser import FlexcubeUIXMLParser
from meta_generator import generate_meta_yaml


BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "screens.db"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB per file is plenty for UIXML/JS

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES * 2 + 1024
app.secret_key = "flexcube-screen-tester-dev"  # only used for flash messages

init_db(DB_PATH)


def _read_upload(file_storage) -> str:
    return file_storage.read().decode("utf-8", errors="replace")


@app.get("/")
def index():
    # Show the 10 most recent screens on the upload page so a user landing
    # here after a refresh immediately sees the persisted history.
    return render_template("index.html", recent_screens=list_screens(DB_PATH)[:10])


@app.post("/upload")
def upload():
    # Gate: Settings page must be filled before any screen can be uploaded.
    # The home page enforces this client-side via a modal, but check here
    # too so the API can't be bypassed by direct POSTs.
    cfg_ok, cfg_missing = runner.runtime_config_status()
    if not cfg_ok:
        flash(
            "Configure FLEXCUBE credentials in Settings before uploading "
            "screens. Missing: " + ", ".join(cfg_missing),
            "error",
        )
        return redirect(url_for("settings_view"))

    screen_name = (request.form.get("screen_name") or "").strip()
    uixml_file = request.files.get("uixml")
    js_file = request.files.get("js")

    if not screen_name:
        flash("Screen name is required.", "error")
        return redirect(url_for("index"))
    if not uixml_file or not uixml_file.filename:
        flash("UIXML file is required.", "error")
        return redirect(url_for("index"))

    uixml_text = _read_upload(uixml_file)
    js_text = _read_upload(js_file) if js_file and js_file.filename else None

    try:
        screen_model = FlexcubeUIXMLParser().parse_string(
            uixml_text, filename_hint=uixml_file.filename
        )
    except Exception as exc:
        flash(f"Failed to parse UIXML: {exc}", "error")
        return redirect(url_for("index"))

    js_analysis = None
    if js_text:
        try:
            js_analysis = FlexcubeJSParser().parse_string(js_text)
        except Exception as exc:
            flash(f"Failed to parse JS: {exc}", "error")
            return redirect(url_for("index"))

    meta_yaml = generate_meta_yaml(screen_name, screen_model, js_analysis)

    screen_id = save_screen(
        DB_PATH,
        name=screen_name,
        screen_model=screen_model,
        js_analysis=js_analysis,
        meta_yaml=meta_yaml,
        uixml_filename=uixml_file.filename,
        js_filename=js_file.filename if js_file and js_file.filename else None,
    )
    flash(f"Parsed {len(screen_model.all_fields())} fields across "
          f"{len(screen_model.blocks)} blocks. Now mark each field's value below.",
          "success")
    return redirect(url_for("screen_review", screen_id=screen_id))


@app.get("/screens")
def screens_list():
    return render_template("screens.html", screens=list_screens(DB_PATH))


@app.get("/screens/<int:screen_id>")
def screen_detail(screen_id: int):
    screen = get_screen(DB_PATH, screen_id)
    if not screen:
        abort(404)
    runs = list_runs(DB_PATH, screen_id)
    cfg_ok, cfg_missing = runner.runtime_config_status()
    return render_template(
        "screen.html",
        screen=screen,
        runs=runs,
        runner_cfg_ok=cfg_ok,
        runner_cfg_missing=cfg_missing,
        runner_cli_ok=runner.claude_cli_path() is not None,
        is_verified=bool(screen.get("verified_at")),
    )


@app.post("/screens/<int:screen_id>/verify")
def screen_verify(screen_id: int):
    """Promotes a successful Claude Code run to 'verified'. Extracts a recipe
    from the run's log, saves it on the screens row, and unlocks the
    deterministic runner as the primary action."""
    screen = get_screen(DB_PATH, screen_id)
    if not screen:
        abort(404)

    run_id = request.form.get("run_id", type=int)
    if not run_id:
        # Fallback: pick the most recent eligible Claude Code run.
        eligible = get_eligible_verify_run(DB_PATH, screen_id)
        if not eligible:
            flash("No completed Claude Code run available to verify against.", "error")
            return redirect(url_for("screen_detail", screen_id=screen_id))
        run_id = eligible["id"]

    run = get_run(DB_PATH, run_id)
    if not run or run["screen_id"] != screen_id or run["status"] != "completed":
        flash("That run is not eligible for verification.", "error")
        return redirect(url_for("screen_detail", screen_id=screen_id))

    log_path = (BASE_DIR / run["log_path"]).resolve() if run.get("log_path") else None
    recipe = (extract_recipe_from_log(log_path, screen["function_id"], run_id)
              if log_path else {"captured_from_run_id": run_id, "function_id": screen["function_id"]})

    mark_verified(DB_PATH, screen_id, run_id, recipe)
    cb = (recipe.get("checkbox_strategy") or {})
    lov = (recipe.get("lov_popup_titles") or {})
    flash(
        f"Screen verified from run #{run_id}. "
        f"Captured: {len(cb)} checkbox override(s), {len(lov)} LOV title(s).",
        "success",
    )
    return redirect(url_for("screen_detail", screen_id=screen_id))


UPLOADS_DIR = BASE_DIR / "uploads"


# Settings-related keys are the same names runtime_config uses internally.
SETTING_KEYS_REQUIRED = ["FLEXCUBE_BASE_URL", "FLEXCUBE_USERNAME", "FLEXCUBE_PASSWORD"]
SETTING_KEYS_OPTIONAL = ["FLEXCUBE_ACCORDER_AUTH_USERNAME", "FLEXCUBE_ACCORDER_AUTH_PASSWORD"]


@app.context_processor
def inject_runtime_config_status():
    """Make `cfg_ok` and `cfg_missing` available in every template so
    base.html can render the 'Configure credentials' nav prompt and any
    page can detect missing config without re-querying."""
    ok, missing = runner.runtime_config_status()
    return {"cfg_ok": ok, "cfg_missing": missing}


@app.get("/settings")
def settings_view():
    saved = get_all_settings(DB_PATH)
    # Surface the legacy-fallback values so the user can see what would be
    # used if they leave a field blank. Don't surface the actual values
    # though — that'd leak .env passwords back into the rendered HTML.
    return render_template(
        "settings.html",
        saved=saved,
        required_keys=SETTING_KEYS_REQUIRED,
        optional_keys=SETTING_KEYS_OPTIONAL,
    )


@app.post("/settings")
def settings_save():
    keys = SETTING_KEYS_REQUIRED + SETTING_KEYS_OPTIONAL
    updates: dict[str, str | None] = {}
    for k in keys:
        v = (request.form.get(k) or "").strip()
        # Empty string in the form means "unset / use legacy fallback if any".
        updates[k] = v or None
    set_settings(DB_PATH, updates)
    flash("Settings saved.", "success")
    return redirect(url_for("settings_view"))


def _bulk_load_decisions(screen: dict) -> list[dict]:
    """Auto-derive 'from Excel' decisions for every non-readonly field.
    Bulk mode is meant to be zero-config — the user picks bulk_load,
    downloads a template that already has every editable field as a
    column, and uses blank-vs-filled cells to drive per-row behaviour.
    No per-field UI interaction needed."""
    return [
        {"block_name": f["block_name"], "field_name": f["name"],
         "mode": "excel", "value": f["name"]}
        for f in (screen.get("fields") or [])
        if not f.get("readonly")
    ]


@app.get("/screens/<int:screen_id>/excel-template.xlsx")
def screen_excel_template(screen_id: int):
    """Build an XLSX template with one column per non-readonly field on
    the screen — no per-field config needed. The user just fills the
    cells and uploads. Empty cells = skip that field for that row."""
    screen = get_screen(DB_PATH, screen_id)
    if not screen:
        abort(404)
    decisions = _bulk_load_decisions(screen)
    xlsx_bytes = write_template(decisions, screen)
    fname = f"{screen['function_id']}.bulk_template.xlsx"
    return Response(
        xlsx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/screens/<int:screen_id>/excel-upload")
def screen_excel_upload(screen_id: int):
    screen = get_screen(DB_PATH, screen_id)
    if not screen:
        abort(404)
    f = request.files.get("excel")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("screen_review", screen_id=screen_id))
    if not f.filename.lower().endswith(".xlsx"):
        flash("Please upload an .xlsx file (Excel 2007+ format).", "error")
        return redirect(url_for("screen_review", screen_id=screen_id))

    dest_dir = UPLOADS_DIR / str(screen_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(f.filename).name  # strip any directory part
    dest = dest_dir / safe_name
    f.save(str(dest))

    # Validate the file is parseable. If it's not, clean up and surface a
    # helpful error rather than letting `generate` fail later.
    try:
        rows = read_uploaded_excel(dest)
    except Exception as exc:
        try: dest.unlink()
        except OSError: pass
        flash(f"Could not parse the Excel file: {exc}", "error")
        return redirect(url_for("screen_review", screen_id=screen_id))

    rel_path = dest.relative_to(BASE_DIR).as_posix()
    save_excel_upload(DB_PATH, screen_id,
                      filename=safe_name, path=rel_path, row_count=len(rows))
    flash(f"Uploaded {safe_name} — {len(rows)} data row(s) detected.", "success")
    return redirect(url_for("screen_review", screen_id=screen_id))


@app.post("/screens/<int:screen_id>/excel-clear")
def screen_excel_clear(screen_id: int):
    screen = get_screen(DB_PATH, screen_id)
    if not screen:
        abort(404)
    clear_excel_upload(DB_PATH, screen_id)
    flash("Excel upload cleared.", "success")
    return redirect(url_for("screen_review", screen_id=screen_id))


@app.post("/screens/<int:screen_id>/unverify")
def screen_unverify(screen_id: int):
    screen = get_screen(DB_PATH, screen_id)
    if not screen:
        abort(404)
    unmark_verified(DB_PATH, screen_id)
    flash("Screen marked as unverified. Run Claude Code to re-verify.", "success")
    return redirect(url_for("screen_detail", screen_id=screen_id))


@app.get("/screens/<int:screen_id>/review")
def screen_review(screen_id: int):
    screen = get_screen(DB_PATH, screen_id)
    if not screen:
        abort(404)
    prior = get_field_decisions(DB_PATH, screen_id)
    prior_grids = get_grid_decisions(DB_PATH, screen_id)
    prior_buttons = get_button_decisions(DB_PATH, screen_id)
    return render_template(
        "review.html",
        screen=screen,
        prior_decisions=prior,
        prior_grid_decisions=prior_grids,
        prior_button_decisions=prior_buttons,
        workflow_modes=WORKFLOW_MODES,
        active_mode=screen.get("workflow_mode") or "create_new",
    )


@app.post("/screens/<int:screen_id>/generate")
def screen_generate(screen_id: int):
    screen = get_screen(DB_PATH, screen_id)
    if not screen:
        abort(404)
    workflow_mode = (request.form.get("workflow_mode") or "create_new").strip()
    if workflow_mode not in WORKFLOW_MODES:
        flash(f"Unsupported workflow mode: {workflow_mode}", "error")
        return redirect(url_for("screen_review", screen_id=screen_id))
    if workflow_mode in ("copy_existing", "modify"):
        flash(f"{WORKFLOW_MODES[workflow_mode]} is still on the roadmap.", "error")
        return redirect(url_for("screen_review", screen_id=screen_id))

    if workflow_mode == "bulk_load":
        # Bulk mode is zero-config on the frontend — every non-readonly
        # field is automatically a column in the Excel template.
        decisions = _bulk_load_decisions(screen)
        grid_rows: dict[str, list[dict]] = {}
    else:
        decisions = parse_decisions_from_form(screen["fields"], request.form)
        grid_rows = parse_grid_decisions_from_form(
            screen["blocks"], screen["fields"], request.form,
        )

    # Custom in-screen button decisions: form keys are `button_<NAME>=1` for
    # the buttons the user opted to click. Only consider buttons declared
    # as `is_custom=True` — standard toolbar buttons are always part of
    # the plan and not user-toggleable.
    button_decisions: dict[str, bool] = {
        b["name"]: bool(request.form.get(f"button_{b['name']}"))
        for b in (screen.get("buttons") or []) if b.get("is_custom")
    }

    excel_rows = None
    excel_grid_rows: dict[str, list[dict]] = {}
    if workflow_mode == "bulk_load":
        excel_path_rel = screen.get("excel_path")
        if excel_path_rel:
            abs_path = BASE_DIR / excel_path_rel
            try:
                full = read_uploaded_excel_full(abs_path)
                excel_rows = full.get("_master") or []
                # Map each grid sheet (by sheet title = block name) into the
                # excel_grid_rows dict the bulk composer consumes.
                for sheet_name, rows in full.items():
                    if sheet_name == "_master":
                        continue
                    excel_grid_rows[sheet_name] = rows
            except Exception as exc:
                flash(f"Failed to read Excel file: {exc}", "error")
                return redirect(url_for("screen_review", screen_id=screen_id))

    try:
        claude_md = generate_claude_md(
            screen, workflow_mode, decisions,
            excel_rows=excel_rows,
            grid_rows=grid_rows,
            excel_grid_rows=excel_grid_rows,
            button_decisions=button_decisions,
        )
    except Exception as exc:
        flash(f"Failed to generate plan: {exc}", "error")
        return redirect(url_for("screen_review", screen_id=screen_id))

    save_field_decisions(DB_PATH, screen_id, decisions, workflow_mode, claude_md)
    save_grid_decisions(DB_PATH, screen_id, grid_rows)
    save_button_decisions(DB_PATH, screen_id, button_decisions)

    used = sum(1 for d in decisions if d["mode"] != "skip")
    flash(f"Generated CLAUDE.md from {used} field decisions.", "success")
    return redirect(url_for("screen_detail", screen_id=screen_id))


@app.get("/screens/<int:screen_id>/CLAUDE.md")
def screen_claude_md_download(screen_id: int):
    md = get_claude_md(DB_PATH, screen_id)
    if md is None:
        abort(404)
    screen = get_screen(DB_PATH, screen_id)
    filename = f"{screen['function_id']}.CLAUDE.md" if screen else f"screen_{screen_id}.CLAUDE.md"
    return Response(
        md,
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Plan execution: spawn Claude Code + Playwright MCP, stream progress
# ---------------------------------------------------------------------------

@app.post("/screens/<int:screen_id>/run")
def run_start(screen_id: int):
    screen = get_screen(DB_PATH, screen_id)
    if not screen:
        abort(404)
    plan = screen.get("claude_md")
    if not plan:
        flash("No CLAUDE.md generated yet — run Review & Generate first.", "error")
        return redirect(url_for("screen_review", screen_id=screen_id))

    run_id, err = runner.start_run(
        db_path=DB_PATH,
        screen_id=screen_id,
        plan_md=plan,
        function_id=screen["function_id"],
    )
    if err:
        flash(err, "error")
        if run_id:
            return redirect(url_for("run_detail", screen_id=screen_id, run_id=run_id))
        return redirect(url_for("screen_detail", screen_id=screen_id))

    flash(f"Run #{run_id} started (Claude Code).", "success")
    return redirect(url_for("run_detail", screen_id=screen_id, run_id=run_id))


@app.post("/screens/<int:screen_id>/run/deterministic")
def run_start_deterministic(screen_id: int):
    """v1.2 path. Compiles the existing field decisions into a structured
    plan and spawns deterministic_runner.py instead of Claude Code. Same
    SSE log page; no LLM, no quota, ~30s per run."""
    screen = get_screen(DB_PATH, screen_id)
    if not screen:
        abort(404)
    if not screen.get("claude_md"):
        flash("No plan generated yet — run Review & Generate first.", "error")
        return redirect(url_for("screen_review", screen_id=screen_id))

    decisions_map = get_field_decisions(DB_PATH, screen_id)
    if not decisions_map:
        flash("No field decisions saved — run Review & Generate first.", "error")
        return redirect(url_for("screen_review", screen_id=screen_id))
    decisions = list(decisions_map.values())
    workflow_mode = screen.get("workflow_mode") or "create_new"

    run_id, err = runner.start_run_deterministic(
        db_path=DB_PATH,
        screen_id=screen_id,
        screen=screen,
        workflow_mode=workflow_mode,
        decisions=decisions,
    )
    if err:
        flash(err, "error")
        if run_id:
            return redirect(url_for("run_detail", screen_id=screen_id, run_id=run_id))
        return redirect(url_for("screen_detail", screen_id=screen_id))

    flash(f"Run #{run_id} started (deterministic).", "success")
    return redirect(url_for("run_detail", screen_id=screen_id, run_id=run_id))


@app.get("/screens/<int:screen_id>/runs/<int:run_id>")
def run_detail(screen_id: int, run_id: int):
    run = get_run(DB_PATH, run_id)
    if not run or run["screen_id"] != screen_id:
        abort(404)
    screen = get_screen(DB_PATH, screen_id)
    # The "Verify & save" prompt appears only when:
    #   - this run completed successfully
    #   - this is the Claude Code runner (only those carry recipe-extractable logs)
    #   - the screen isn't already verified by THIS run
    can_verify = (
        run["status"] == "completed"
        and (run.get("kind") or "claude_code") == "claude_code"
        and screen.get("verified_by_run_id") != run["id"]
    )
    return render_template("run.html", screen=screen, run=run, can_verify=can_verify)


@app.get("/screens/<int:screen_id>/runs/<int:run_id>/stream")
def run_stream(screen_id: int, run_id: int):
    run = get_run(DB_PATH, run_id)
    if not run or run["screen_id"] != screen_id:
        abort(404)
    log_path = BASE_DIR / run["log_path"]

    def generate():
        last_size = 0
        # Tail the log file. The reaper thread updates the DB row when the
        # subprocess exits; we exit this loop the next time we sample status.
        while True:
            current = get_run(DB_PATH, run_id) or {}
            if log_path.exists():
                size = log_path.stat().st_size
                if size > last_size:
                    with log_path.open("rb") as fp:
                        fp.seek(last_size)
                        chunk = fp.read(size - last_size)
                        last_size = size
                    text = chunk.decode("utf-8", errors="replace")
                    for raw_line in text.splitlines():
                        line = raw_line.strip()
                        if not line:
                            continue
                        # SSE: each event is `data: ...\n\n`. Any newline in
                        # the JSON would break parsing client-side, but
                        # JSONL guarantees one object per line.
                        yield f"data: {line}\n\n"

            status = current.get("status")
            if status not in ("starting", "running"):
                final = {
                    "status": status,
                    "exit_code": current.get("exit_code"),
                    "error_message": current.get("error_message"),
                    "finished_at": current.get("finished_at"),
                }
                yield "event: done\ndata: " + json.dumps(final) + "\n\n"
                return

            time.sleep(0.5)

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.post("/screens/<int:screen_id>/runs/<int:run_id>/stop")
def run_stop(screen_id: int, run_id: int):
    run = get_run(DB_PATH, run_id)
    if not run or run["screen_id"] != screen_id:
        abort(404)
    if runner.stop_run(DB_PATH, run_id):
        flash(f"Run #{run_id} stopped.", "success")
    else:
        flash(f"Run #{run_id} was not live.", "error")
    return redirect(url_for("run_detail", screen_id=screen_id, run_id=run_id))


@app.get("/screens/<int:screen_id>/runs/<int:run_id>/screenshots/<path:filename>")
def run_screenshot(screen_id: int, run_id: int, filename: str):
    run = get_run(DB_PATH, run_id)
    if not run or run["screen_id"] != screen_id:
        abort(404)
    abs_dir = (BASE_DIR / run["screenshots_dir"]).resolve()
    # Defence-in-depth: keep the served file inside the run's own dir.
    if not str(abs_dir).startswith(str(BASE_DIR.resolve())):
        abort(403)
    return send_from_directory(abs_dir, filename)


@app.get("/screens/<int:screen_id>/runs/<int:run_id>/screenshots-list")
def run_screenshots_list(screen_id: int, run_id: int):
    """Polled by run.html every few seconds to refresh the gallery."""
    run = get_run(DB_PATH, run_id)
    if not run or run["screen_id"] != screen_id:
        abort(404)
    shots_dir = BASE_DIR / run["screenshots_dir"]
    if not shots_dir.exists():
        return {"screenshots": []}
    files = sorted(p.name for p in shots_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"})
    return {"screenshots": files}


@app.get("/screens/<int:screen_id>/meta.yaml")
def screen_meta_download(screen_id: int):
    yaml_text = get_meta_yaml(DB_PATH, screen_id)
    if yaml_text is None:
        abort(404)
    screen = get_screen(DB_PATH, screen_id)
    filename = f"{screen['function_id']}.meta.yaml" if screen else f"screen_{screen_id}.meta.yaml"
    return Response(
        yaml_text,
        mimetype="text/yaml; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/screens/<int:screen_id>/delete")
def screen_delete(screen_id: int):
    delete_screen(DB_PATH, screen_id)
    flash("Screen deleted.", "success")
    return redirect(url_for("screens_list"))


if __name__ == "__main__":
    # Port 5000 is taken on this machine by another project (GoldJewelryAPI).
    # Override with FLASK_PORT in the environment if you need a different one.
    port = int(os.environ.get("FLASK_PORT", "5050"))
    app.run(debug=True, host="127.0.0.1", port=port)
