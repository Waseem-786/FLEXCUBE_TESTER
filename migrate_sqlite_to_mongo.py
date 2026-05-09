"""
migrate_sqlite_to_mongo.py
==========================

One-shot copy of the existing `screens.db` (SQLite) into the configured
MongoDB cluster. Idempotent — re-running upserts by `function_id` for
screens and by `id` for runs, so it's safe to re-run after fixing data.

Usage:
    # Set MONGODB_URI in .env first, then:
    python migrate_sqlite_to_mongo.py

    # Or point at a non-default sqlite file:
    python migrate_sqlite_to_mongo.py --sqlite path/to/old.db

What it copies:
    screens.db.screens         → Mongo `screens`     (one nested doc each,
                                 with blocks/fields/buttons/dependencies/
                                 validations/field_decisions/grid_decisions
                                 embedded).
    screens.db.runs            → Mongo `runs`
    screens.db.kv              → Mongo `kv`           (one doc per key)

Skipped on purpose:
    - SQLite `counters` semantics (auto-increment) — we set `counters` to
      `max(id)` for screens / runs after copy so future _next_id() picks up
      from there.
    - The `meta_yaml` blob is preserved as-is (it's a string).

Safety:
    - SQLite is read-only here; never modified.
    - On collection conflict, screens are upserted on `function_id`;
      runs on `id`. So a partial run can be retried.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import mongo_db


def _open_sqlite(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"sqlite file not found: {path}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _to_dict_list(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def _build_screen_doc(conn: sqlite3.Connection, screen_row: sqlite3.Row) -> dict:
    sid = screen_row["id"]

    blocks = _to_dict_list(conn.execute(
        "SELECT name, label, is_grid, is_tab, parent_tab "
        "FROM blocks WHERE screen_id = ? ORDER BY id", (sid,)
    ).fetchall())

    fields = _to_dict_list(conn.execute(
        "SELECT block_name, name, label, datatype, length, precision, "
        "       required, readonly, lov, default_value, is_grid_column "
        "FROM fields WHERE screen_id = ? ORDER BY id", (sid,)
    ).fetchall())

    buttons = _to_dict_list(conn.execute(
        "SELECT name, label, parent_block "
        "FROM buttons WHERE screen_id = ? ORDER BY id", (sid,)
    ).fetchall())

    dependencies = _to_dict_list(conn.execute(
        "SELECT source_field, kind, target_field "
        "FROM dependencies WHERE screen_id = ? ORDER BY id", (sid,)
    ).fetchall())

    validations = _to_dict_list(conn.execute(
        "SELECT field_name, rule "
        "FROM validations WHERE screen_id = ? ORDER BY id", (sid,)
    ).fetchall())

    field_decisions = _to_dict_list(conn.execute(
        "SELECT block_name, field_name, mode, value "
        "FROM field_decisions WHERE screen_id = ?", (sid,)
    ).fetchall())

    grid_rows = conn.execute(
        "SELECT block_name, rows_json FROM grid_decisions WHERE screen_id = ?",
        (sid,),
    ).fetchall()
    grid_decisions: dict[str, list[dict]] = {}
    for g in grid_rows:
        try:
            grid_decisions[g["block_name"]] = json.loads(g["rows_json"]) or []
        except json.JSONDecodeError:
            grid_decisions[g["block_name"]] = []

    s = dict(screen_row)
    return {
        "id":                 s["id"],
        "name":               s["name"],
        "function_id":        s["function_id"],
        "title":              s.get("title"),
        "uixml_filename":     s.get("uixml_filename"),
        "js_filename":        s.get("js_filename"),
        "created_at":         s.get("created_at"),
        "meta_yaml":          s.get("meta_yaml"),
        "workflow_mode":      s.get("workflow_mode"),
        "claude_md":          s.get("claude_md"),
        "verified_at":        s.get("verified_at"),
        "verified_by_run_id": s.get("verified_by_run_id"),
        "recipe_json":        s.get("recipe_json"),
        "excel_filename":     s.get("excel_filename"),
        "excel_path":         s.get("excel_path"),
        "excel_uploaded_at":  s.get("excel_uploaded_at"),
        "excel_row_count":    s.get("excel_row_count"),
        "blocks":             blocks,
        "fields":             fields,
        "buttons":            buttons,
        "dependencies":       dependencies,
        "validations":        validations,
        "field_decisions":    field_decisions,
        "grid_decisions":     grid_decisions,
        "schema_version":     1,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite", default="screens.db",
                    help="Path to the SQLite file to read from.")
    args = ap.parse_args()

    sqlite_path = Path(args.sqlite).resolve()
    print(f"reading: {sqlite_path}")

    # Surface connection errors before touching SQLite.
    print(f"connecting to MongoDB ({mongo_db._resolve_db_name()}) …")
    db = mongo_db._db()  # raises if MONGODB_URI is missing
    print("connected.")

    conn = _open_sqlite(sqlite_path)

    # ----- screens -----
    screens = conn.execute("SELECT * FROM screens ORDER BY id").fetchall()
    print(f"screens to migrate: {len(screens)}")
    for row in screens:
        doc = _build_screen_doc(conn, row)
        db["screens"].update_one(
            {"function_id": doc["function_id"]},
            {"$set": doc},
            upsert=True,
        )
        print(f"  · {doc['function_id']:<10} (id={doc['id']}, "
              f"fields={len(doc['fields'])}, blocks={len(doc['blocks'])}, "
              f"verified={'yes' if doc['verified_at'] else 'no'})")

    # ----- runs -----
    runs = conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
    print(f"runs to migrate: {len(runs)}")
    for row in runs:
        d = dict(row)
        d["schema_version"] = 1
        db["runs"].update_one({"id": d["id"]}, {"$set": d}, upsert=True)

    # ----- kv -----
    kv_rows = conn.execute("SELECT key, value FROM kv").fetchall()
    print(f"kv settings to migrate: {len(kv_rows)} key(s)")
    for r in kv_rows:
        if r["value"] is None or r["value"] == "":
            db["kv"].delete_one({"_id": r["key"]})
        else:
            db["kv"].update_one(
                {"_id": r["key"]},
                {"$set": {"value": r["value"]}},
                upsert=True,
            )

    # ----- counters: bump to max(id) so _next_id() resumes correctly -----
    if screens:
        max_screen = max(r["id"] for r in screens)
        db["counters"].update_one(
            {"_id": "screens"}, {"$max": {"seq": max_screen}}, upsert=True,
        )
        print(f"counters.screens → {max_screen}")
    if runs:
        max_run = max(r["id"] for r in runs)
        db["counters"].update_one(
            {"_id": "runs"}, {"$max": {"seq": max_run}}, upsert=True,
        )
        print(f"counters.runs    → {max_run}")

    conn.close()
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
