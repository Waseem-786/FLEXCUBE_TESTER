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

CREATE INDEX IF NOT EXISTS ix_blocks_screen       ON blocks(screen_id);
CREATE INDEX IF NOT EXISTS ix_fields_screen       ON fields(screen_id);
CREATE INDEX IF NOT EXISTS ix_buttons_screen      ON buttons(screen_id);
CREATE INDEX IF NOT EXISTS ix_dependencies_screen ON dependencies(screen_id);
CREATE INDEX IF NOT EXISTS ix_validations_screen  ON validations(screen_id);
CREATE INDEX IF NOT EXISTS ix_field_decisions_screen ON field_decisions(screen_id);
"""


# Columns added after the original schema went out the door. Auto-applied
# on every connection so DBs created from older code transparently upgrade.
# Format: (table, column, ddl-fragment)
_RUNTIME_COLUMNS = [
    ("screens", "workflow_mode", "TEXT"),
    ("screens", "claude_md",     "TEXT"),
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
                      (SELECT COUNT(*) FROM fields  f WHERE f.screen_id = s.id) AS field_count,
                      (SELECT COUNT(*) FROM blocks  b WHERE b.screen_id = s.id) AS block_count
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
                      "field_decisions"):
            conn.execute(f"DELETE FROM {table} WHERE screen_id = ?", (screen_id,))
        conn.execute("DELETE FROM screens WHERE id = ?", (screen_id,))


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
