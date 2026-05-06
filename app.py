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
  → http://127.0.0.1:5000
"""

from __future__ import annotations

from pathlib import Path

from flask import (
    Flask, abort, flash, get_flashed_messages, redirect, render_template,
    request, Response, url_for,
)

from claude_md_generator import (
    WORKFLOW_MODES, generate_claude_md, parse_decisions_from_form,
)
from db import (
    delete_screen, get_claude_md, get_field_decisions, get_meta_yaml, get_screen,
    init_db, list_screens, save_field_decisions, save_screen,
)
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
    return render_template("index.html")


@app.post("/upload")
def upload():
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
    return render_template("screen.html", screen=screen)


@app.get("/screens/<int:screen_id>/review")
def screen_review(screen_id: int):
    screen = get_screen(DB_PATH, screen_id)
    if not screen:
        abort(404)
    prior = get_field_decisions(DB_PATH, screen_id)
    return render_template(
        "review.html",
        screen=screen,
        prior_decisions=prior,
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
    if workflow_mode != "create_new":
        flash(f"{WORKFLOW_MODES[workflow_mode]} is on the roadmap; only "
              f"Create New is implemented in v1.", "error")
        return redirect(url_for("screen_review", screen_id=screen_id))

    decisions = parse_decisions_from_form(screen["fields"], request.form)
    try:
        claude_md = generate_claude_md(screen, workflow_mode, decisions)
    except Exception as exc:
        flash(f"Failed to generate plan: {exc}", "error")
        return redirect(url_for("screen_review", screen_id=screen_id))

    save_field_decisions(DB_PATH, screen_id, decisions, workflow_mode, claude_md)

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
    app.run(debug=True, host="127.0.0.1", port=5000)
