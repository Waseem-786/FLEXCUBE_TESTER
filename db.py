"""
db.py
=====

SQLite persistence for parsed FLEXCUBE screens.

One screen → one row in `screens` plus N rows across `blocks`, `fields`,
`buttons`, `dependencies`, `validations`. The full generated meta.yaml is
also stored on the screen row so re-downloads don't require re-parsing.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS screens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    function_id     TEXT    NOT NULL,
    title           TEXT,
    uixml_filename  TEXT,
    js_filename     TEXT,
    created_at      TEXT    NOT NULL,
    meta_yaml       TEXT    NOT NULL,
    workflow_mode   TEXT,
    claude_md       TEXT
);

CREATE TABLE IF NOT EXISTS field_decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_id     INTEGER NOT NULL,
    block_name    TEXT,
    field_name    TEXT    NOT NULL,
    mode          TEXT    NOT NULL,   -- value | today | option | tick | untick | lov_match | skip
    value         TEXT,
    UNIQUE (screen_id, field_name, block_name),
    FOREIGN KEY (screen_id) REFERENCES screens(id)
);

CREATE TABLE IF NOT EXISTS blocks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_id    INTEGER NOT NULL,
    name         TEXT    NOT NULL,
    label        TEXT,
    is_grid      INTEGER NOT NULL DEFAULT 0,
    is_tab       INTEGER NOT NULL DEFAULT 0,
    parent_tab   TEXT,
    FOREIGN KEY (screen_id) REFERENCES screens(id)
);

CREATE TABLE IF NOT EXISTS fields (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_id       INTEGER NOT NULL,
    block_name      TEXT,
    name            TEXT    NOT NULL,
    label           TEXT,
    datatype        TEXT,
    length          INTEGER,
    precision       INTEGER,
    required        INTEGER NOT NULL DEFAULT 0,
    readonly        INTEGER NOT NULL DEFAULT 0,
    lov             TEXT,
    default_value   TEXT,
    is_grid_column  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (screen_id) REFERENCES screens(id)
);

CREATE TABLE IF NOT EXISTS buttons (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_id     INTEGER NOT NULL,
    name          TEXT    NOT NULL,
    label         TEXT,
    parent_block  TEXT,
    FOREIGN KEY (screen_id) REFERENCES screens(id)
);

CREATE TABLE IF NOT EXISTS dependencies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_id     INTEGER NOT NULL,
    source_field  TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    target_field  TEXT    NOT NULL,
    FOREIGN KEY (screen_id) REFERENCES screens(id)
);

CREATE TABLE IF NOT EXISTS validations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_id   INTEGER NOT NULL,
    field_name  TEXT    NOT NULL,
    rule        TEXT    NOT NULL,
    FOREIGN KEY (screen_id) REFERENCES screens(id)
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_id       INTEGER NOT NULL,
    status          TEXT    NOT NULL,    -- starting | running | completed | failed | stopped
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    exit_code       INTEGER,
    pid             INTEGER,
    base_url        TEXT,
    log_path        TEXT,
    screenshots_dir TEXT,
    error_message   TEXT,
    FOREIGN KEY (screen_id) REFERENCES screens(id)
);

CREATE INDEX IF NOT EXISTS ix_blocks_screen          ON blocks(screen_id);
CREATE INDEX IF NOT EXISTS ix_fields_screen          ON fields(screen_id);
CREATE INDEX IF NOT EXISTS ix_buttons_screen         ON buttons(screen_id);
CREATE INDEX IF NOT EXISTS ix_dependencies_screen    ON dependencies(screen_id);
CREATE INDEX IF NOT EXISTS ix_validations_screen     ON validations(screen_id);
CREATE INDEX IF NOT EXISTS ix_field_decisions_screen ON field_decisions(screen_id);
CREATE INDEX IF NOT EXISTS ix_runs_screen            ON runs(screen_id);
"""


# Columns added after the original schema went out the door. Auto-applied
# on every connection so DBs created from older code transparently upgrade.
# Format: (table, column, ddl-fragment)
_RUNTIME_COLUMNS = [
    ("screens", "workflow_mode",      "TEXT"),
    ("screens", "claude_md",          "TEXT"),
    # Verification: a screen is "verified" once a successful Claude Code run
    # has produced a recipe the deterministic runner can use. Three columns:
    #   verified_at         — UTC ISO timestamp of last verify, or NULL
    #   verified_by_run_id  — FK to the run that verified it
    #   recipe_json         — JSON blob (selector overrides, checkbox
    #                         strategies, LOV iframe titles) extracted from
    #                         that run's stream-json log
    ("screens", "verified_at",        "TEXT"),
    ("screens", "verified_by_run_id", "INTEGER"),
    ("screens", "recipe_json",        "TEXT"),
    # Run-level: which runner produced this row. Useful for filtering the
    # history table and for the verify-modal logic ("only Claude Code runs
    # are eligible to verify").
    ("runs",    "kind",               "TEXT"),
    # Bulk-load workflow: the uploaded Excel data file lives on disk; the
    # DB just records where + when. `excel_path` is relative to the project
    # root (so it survives if the project is moved).
    ("screens", "excel_filename",     "TEXT"),
    ("screens", "excel_path",         "TEXT"),
    ("screens", "excel_uploaded_at",  "TEXT"),
    ("screens", "excel_row_count",    "INTEGER"),
]


def _ensure_runtime_columns(conn: sqlite3.Connection) -> None:
    for table, col, ddl in _RUNTIME_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


@contextmanager
def _connect(db_path: str | Path):
    """Open a SQLite connection AND ensure the schema exists.

    Idempotently re-applies SCHEMA (`CREATE TABLE IF NOT EXISTS ...`) on every
    connection. This protects against any path that creates the DB file
    without going through `init_db()` — e.g. a stray sqlite3.connect() in a
    test, or Flask's reloader importing in a way that skips module-level init.
    The cost when the schema already exists is a single parse + sqlite_master
    lookup per request, which is negligible for this app.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _ensure_runtime_columns(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str | Path) -> None:
    # Schema application now happens inside _connect; this stays as an explicit
    # "create the file now" entry point used at app boot.
    with _connect(db_path):
        pass


def save_screen(
    db_path: str | Path,
    *,
    name: str,
    screen_model,
    js_analysis,
    meta_yaml: str,
    uixml_filename: str | None,
    js_filename: str | None,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO screens
               (name, function_id, title, uixml_filename, js_filename, created_at, meta_yaml)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                screen_model.function_id,
                screen_model.title,
                uixml_filename,
                js_filename,
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                meta_yaml,
            ),
        )
        screen_id = cur.lastrowid

        for b in screen_model.blocks:
            conn.execute(
                """INSERT INTO blocks
                   (screen_id, name, label, is_grid, is_tab, parent_tab)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (screen_id, b.name, b.label, int(b.is_grid), int(b.is_tab), b.parent_tab),
            )
            for f in b.fields:
                conn.execute(
                    """INSERT INTO fields
                       (screen_id, block_name, name, label, datatype, length, precision,
                        required, readonly, lov, default_value, is_grid_column)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        screen_id, b.name, f.name, f.label, f.datatype, f.length, f.precision,
                        int(f.required), int(f.readonly), f.lov, f.default, int(f.is_grid_column),
                    ),
                )

        for btn in screen_model.buttons:
            conn.execute(
                """INSERT INTO buttons (screen_id, name, label, parent_block)
                   VALUES (?, ?, ?, ?)""",
                (screen_id, btn.name, btn.label, btn.parent_block),
            )

        if js_analysis is not None:
            for src, kind, tgt in js_analysis.cross_field_dependencies:
                conn.execute(
                    """INSERT INTO dependencies (screen_id, source_field, kind, target_field)
                       VALUES (?, ?, ?, ?)""",
                    (screen_id, src, kind, tgt),
                )
            for fname, fb in js_analysis.field_behaviours.items():
                for rule in fb.inferred_validations:
                    conn.execute(
                        """INSERT INTO validations (screen_id, field_name, rule)
                           VALUES (?, ?, ?)""",
                        (screen_id, fname, rule),
                    )

        return screen_id


def list_screens(db_path: str | Path) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT s.id, s.name, s.function_id, s.created_at, s.uixml_filename, s.js_filename,
                      s.verified_at, s.verified_by_run_id, s.workflow_mode,
                      (SELECT COUNT(*) FROM fields f WHERE f.screen_id = s.id) AS field_count,
                      (SELECT COUNT(*) FROM blocks b WHERE b.screen_id = s.id) AS block_count,
                      (SELECT COUNT(*) FROM runs   r WHERE r.screen_id = s.id) AS run_count,
                      (SELECT COUNT(*) FROM runs   r WHERE r.screen_id = s.id
                                                      AND r.status = 'completed') AS run_success_count
               FROM screens s
               ORDER BY s.created_at DESC, s.id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_screen(db_path: str | Path, screen_id: int) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM screens WHERE id = ?", (screen_id,)
        ).fetchone()
        if not row:
            return None
        screen = dict(row)
        screen["blocks"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM blocks WHERE screen_id = ? ORDER BY id", (screen_id,)
            ).fetchall()
        ]
        screen["fields"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM fields WHERE screen_id = ? ORDER BY id", (screen_id,)
            ).fetchall()
        ]
        screen["buttons"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM buttons WHERE screen_id = ? ORDER BY id", (screen_id,)
            ).fetchall()
        ]
        screen["dependencies"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM dependencies WHERE screen_id = ? ORDER BY id", (screen_id,)
            ).fetchall()
        ]
        screen["validations"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM validations WHERE screen_id = ? ORDER BY id", (screen_id,)
            ).fetchall()
        ]
        return screen


def get_meta_yaml(db_path: str | Path, screen_id: int) -> str | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT meta_yaml FROM screens WHERE id = ?", (screen_id,)
        ).fetchone()
        return row["meta_yaml"] if row else None


def delete_screen(db_path: str | Path, screen_id: int) -> None:
    with _connect(db_path) as conn:
        for table in ("blocks", "fields", "buttons", "dependencies", "validations",
                      "field_decisions", "runs"):
            conn.execute(f"DELETE FROM {table} WHERE screen_id = ?", (screen_id,))
        conn.execute("DELETE FROM screens WHERE id = ?", (screen_id,))


# ---------------------------------------------------------------------------
# Runs (plan-execution sessions)
# ---------------------------------------------------------------------------

def create_run(
    db_path: str | Path,
    screen_id: int,
    base_url: str | None,
    log_path: str,
    screenshots_dir: str,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO runs (screen_id, status, started_at, base_url, log_path, screenshots_dir)
               VALUES (?, 'starting', ?, ?, ?, ?)""",
            (
                screen_id,
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                base_url,
                log_path,
                screenshots_dir,
            ),
        )
        return cur.lastrowid


def update_run(db_path: str | Path, run_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE runs SET {cols} WHERE id = ?",
                     (*fields.values(), run_id))


def get_run(db_path: str | Path, run_id: int) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def list_runs(db_path: str | Path, screen_id: int) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE screen_id = ? ORDER BY id DESC LIMIT 50",
            (screen_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Field decisions + generated CLAUDE.md
# ---------------------------------------------------------------------------

def save_field_decisions(
    db_path: str | Path,
    screen_id: int,
    decisions: list[dict],
    workflow_mode: str,
    claude_md: str,
) -> None:
    """Replace any prior decisions and the generated plan for this screen."""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM field_decisions WHERE screen_id = ?", (screen_id,))
        for d in decisions:
            conn.execute(
                """INSERT INTO field_decisions (screen_id, block_name, field_name, mode, value)
                   VALUES (?, ?, ?, ?, ?)""",
                (screen_id, d.get("block_name"), d["field_name"], d["mode"], d.get("value")),
            )
        conn.execute(
            "UPDATE screens SET workflow_mode = ?, claude_md = ? WHERE id = ?",
            (workflow_mode, claude_md, screen_id),
        )


def get_field_decisions(db_path: str | Path, screen_id: int) -> dict[tuple[str | None, str], dict]:
    """Return decisions keyed by (block_name, field_name) for fast lookup
    when re-rendering the review form with prior selections preselected."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT block_name, field_name, mode, value FROM field_decisions WHERE screen_id = ?",
            (screen_id,),
        ).fetchall()
        return {(r["block_name"], r["field_name"]): dict(r) for r in rows}


def get_claude_md(db_path: str | Path, screen_id: int) -> str | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT claude_md FROM screens WHERE id = ?", (screen_id,)
        ).fetchone()
        return row["claude_md"] if row and row["claude_md"] else None


# ---------------------------------------------------------------------------
# Verification — pinning a screen to a successful Claude Code run + its recipe
# ---------------------------------------------------------------------------

def mark_verified(
    db_path: str | Path,
    screen_id: int,
    run_id: int,
    recipe: dict | None,
) -> None:
    import json as _json
    payload = _json.dumps(recipe, default=str) if recipe is not None else None
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE screens
                  SET verified_at        = ?,
                      verified_by_run_id = ?,
                      recipe_json        = ?
                WHERE id = ?""",
            (datetime.utcnow().isoformat(timespec="seconds") + "Z",
             run_id, payload, screen_id),
        )


def unmark_verified(db_path: str | Path, screen_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE screens
                  SET verified_at = NULL,
                      verified_by_run_id = NULL,
                      recipe_json = NULL
                WHERE id = ?""",
            (screen_id,),
        )


def get_recipe(db_path: str | Path, screen_id: int) -> dict | None:
    """Return the parsed recipe dict for a verified screen, or None.
    Used by the deterministic runner to apply per-screen selector overrides."""
    import json as _json
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT recipe_json FROM screens WHERE id = ?", (screen_id,)
        ).fetchone()
        if not row or not row["recipe_json"]:
            return None
        try:
            return _json.loads(row["recipe_json"])
        except _json.JSONDecodeError:
            return None


def set_run_kind(db_path: str | Path, run_id: int, kind: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("UPDATE runs SET kind = ? WHERE id = ?", (kind, run_id))


# ---------------------------------------------------------------------------
# Bulk-load Excel state
# ---------------------------------------------------------------------------

def save_excel_upload(
    db_path: str | Path,
    screen_id: int,
    *,
    filename: str,
    path: str,
    row_count: int,
) -> None:
    """Pin an uploaded XLSX file to a screen. Re-uploading replaces the
    previous values (path is recorded but the old file isn't deleted —
    that's a manual cleanup if needed).

    Also pins `workflow_mode = 'bulk_load'` because an Excel upload is a
    strong signal the user wants bulk mode. Without this, the workflow
    radio resets to its default ('create_new') when the page reloads
    after the upload — so a subsequent Generate click submits with the
    wrong mode and produces a create_new plan against the stub fields.
    """
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE screens
                  SET excel_filename    = ?,
                      excel_path        = ?,
                      excel_uploaded_at = ?,
                      excel_row_count   = ?,
                      workflow_mode     = 'bulk_load'
                WHERE id = ?""",
            (filename, path,
             datetime.utcnow().isoformat(timespec="seconds") + "Z",
             row_count, screen_id),
        )


def clear_excel_upload(db_path: str | Path, screen_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE screens
                  SET excel_filename = NULL,
                      excel_path = NULL,
                      excel_uploaded_at = NULL,
                      excel_row_count = NULL
                WHERE id = ?""",
            (screen_id,),
        )


def get_eligible_verify_run(db_path: str | Path, screen_id: int) -> dict | None:
    """Most-recent successful Claude Code run for a screen, if any. Used by
    the run-detail page to decide whether to show the 'Verify & save' prompt.
    A run is eligible if status='completed' AND kind='claude_code' AND it
    isn't already the verifying run."""
    with _connect(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM runs
                WHERE screen_id = ?
                  AND status = 'completed'
                  AND COALESCE(kind, 'claude_code') = 'claude_code'
                ORDER BY id DESC LIMIT 1""",
            (screen_id,),
        ).fetchone()
        return dict(row) if row else None
