"""
mongo_db.py
===========

MongoDB persistence for FLEXCUBE screens. Drop-in replacement for db.py —
mirrors every public function name, signature, and return shape so callers
change exactly one import line.

Schema layout
-------------

* `screens` collection — one document per uploaded screen, with the
  previously-relational tables (`blocks`, `fields`, `buttons`,
  `dependencies`, `validations`, `field_decisions`, `grid_decisions`)
  embedded as nested arrays / dicts. One round-trip read replaces the
  five SELECTs that the SQLite version had to issue.

* `runs` collection — one document per plan-execution session, references
  `screen_id` (the numeric id, not the ObjectId) for backward-compat with
  Flask routes that already use `<int:screen_id>`.

* `kv` collection — settings, one document per key. The `_id` IS the key,
  the `value` field holds the value. Same role as the old `kv` table.

* `counters` collection — sequential numeric IDs. Each named sequence
  (e.g. `screens`, `runs`) is one document atomically incremented via
  `find_one_and_update($inc)`. Lets us keep numeric primary keys so
  existing URL routes (`/screens/<int:screen_id>`) work unchanged.

Bootstrap
---------

`MONGODB_URI` lives in `.env` (or `os.environ`) — NOT in the kv collection.
There would be a chicken-and-egg if the connection string itself were in
the database we're connecting to. Database name defaults to `flexcube`
(override via `MONGODB_DB`).

The `db_path` first parameter on every public function is **kept for API
compatibility** with the old SQLite module but **ignored** by Mongo (the
client is process-global). This means the migration is a single-import
swap: `from db import …` → `from mongo_db import …` and nothing else
needs to change in the callers.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from pymongo import MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database


# ---------------------------------------------------------------------------
# Connection — module-level lazy singleton, thread-safe
# ---------------------------------------------------------------------------

_CLIENT: MongoClient | None = None
_DB: Database | None = None
_LOCK = threading.Lock()
_INDEXES_BUILT = False


def _load_dotenv_uri() -> str | None:
    """Read MONGODB_URI from a `.env` file at the project root if present.
    We avoid importing `runner.load_dotenv` to keep this module
    dependency-free (runner imports us — no cycles allowed)."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return None
    try:
        for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() != "MONGODB_URI":
                continue
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or \
               (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            return v
    except OSError:
        pass
    return None


def _resolve_uri() -> str:
    uri = os.environ.get("MONGODB_URI") or _load_dotenv_uri()
    if not uri:
        raise RuntimeError(
            "MONGODB_URI is not set. Add it to .env or your environment. "
            "Example: MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/"
        )
    return uri


def _resolve_db_name() -> str:
    return os.environ.get("MONGODB_DB", "flexcube")


def _db() -> Database:
    """Return the active database, opening the connection on first call."""
    global _CLIENT, _DB
    if _DB is not None:
        return _DB
    with _LOCK:
        if _DB is not None:
            return _DB
        _CLIENT = MongoClient(_resolve_uri())
        _DB = _CLIENT[_resolve_db_name()]
        _ensure_indexes(_DB)
    return _DB


def _ensure_indexes(db: Database) -> None:
    """Idempotent index creation. Mirrors the SQLite CREATE INDEX block."""
    global _INDEXES_BUILT
    if _INDEXES_BUILT:
        return
    db["screens"].create_index("id", unique=True)
    db["screens"].create_index("function_id")
    db["runs"].create_index("id", unique=True)
    db["runs"].create_index([("screen_id", 1), ("id", -1)])
    # `kv` uses _id as the key — implicit unique index on _id covers it.
    _INDEXES_BUILT = True


def _next_id(name: str) -> int:
    """Atomically allocate the next sequential id for a named sequence.
    Counterpart of SQLite's INTEGER PRIMARY KEY AUTOINCREMENT — keeps URLs
    like /screens/4 stable instead of forcing them to ObjectId strings."""
    coll = _db()["counters"]
    doc = coll.find_one_and_update(
        {"_id": name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(doc["seq"])


# ---------------------------------------------------------------------------
# Module-init / lifecycle
# ---------------------------------------------------------------------------

def init_db(db_path: str | Path | None = None) -> None:
    """Open the connection and create indexes. The `db_path` argument is
    kept for API compatibility with the SQLite module — Mongo uses
    `MONGODB_URI` from the environment instead. Calling at app boot
    surfaces a connection error early instead of at first request."""
    _ = db_path  # ignored
    _db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_id(doc: dict | None) -> dict | None:
    """Drop the Mongo _id from a returned doc so callers don't have to
    know about ObjectId. Numeric `id` is preserved."""
    if doc is None:
        return None
    out = {k: v for k, v in doc.items() if k != "_id"}
    return out


def _utcnow() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

def save_screen(
    db_path: str | Path | None = None,
    *,
    name: str,
    screen_model,
    js_analysis,
    meta_yaml: str,
    uixml_filename: str | None,
    js_filename: str | None,
) -> int:
    _ = db_path
    sid = _next_id("screens")
    blocks: list[dict] = []
    fields: list[dict] = []
    buttons: list[dict] = []
    dependencies: list[dict] = []
    validations: list[dict] = []

    for b in screen_model.blocks:
        blocks.append({
            "name":       b.name,
            "label":      b.label,
            "is_grid":    int(b.is_grid),
            "is_tab":     int(b.is_tab),
            "parent_tab": b.parent_tab,
        })
        for f in b.fields:
            fields.append({
                "block_name":     b.name,
                "name":           f.name,
                "label":          f.label,
                "datatype":       f.datatype,
                "length":         f.length,
                "precision":      f.precision,
                "required":       int(f.required),
                "readonly":       int(f.readonly),
                "lov":            f.lov,
                "default_value":  f.default,
                "is_grid_column": int(f.is_grid_column),
                # Discrete option values for SELECT/RADIO fields, parsed
                # from `<OPTION VALUE="...">Label</OPTION>` children. The
                # frontend dropdown widget and the Excel data-validation
                # list both consume this. Without it, users have to type
                # values from memory and risk typos.
                "options":        list(f.options or []),
            })
    for btn in screen_model.buttons:
        buttons.append({
            "name":         btn.name,
            "label":        btn.label,
            "parent_block": btn.parent_block,
            # `is_custom` distinguishes standard FLEXCUBE toolbar buttons
            # (New / Save / etc.) from screen-author-declared custom
            # action buttons (e.g. Submit, Calculation). The review form
            # only prompts for the latter.
            "is_custom":    bool(getattr(btn, "is_custom", False)),
            # Position relative to fields in the same block — used to
            # interleave custom-button click steps in their natural place
            # during the form fill (rather than dumping them at the end).
            "position_in_block": getattr(btn, "position_in_block", None),
        })
    if js_analysis is not None:
        for src, kind, tgt in js_analysis.cross_field_dependencies:
            dependencies.append({
                "source_field": src,
                "kind":         kind,
                "target_field": tgt,
            })
        for fname, fb in js_analysis.field_behaviours.items():
            for rule in fb.inferred_validations:
                validations.append({"field_name": fname, "rule": rule})

    doc = {
        "id":              sid,
        "name":            name,
        "function_id":     screen_model.function_id,
        "title":           screen_model.title,
        "uixml_filename":  uixml_filename,
        "js_filename":     js_filename,
        "created_at":      _utcnow(),
        "meta_yaml":       meta_yaml,
        "workflow_mode":   None,
        "claude_md":       None,
        "verified_at":     None,
        "verified_by_run_id": None,
        "recipe_json":     None,
        "excel_filename":  None,
        "excel_path":      None,
        "excel_uploaded_at": None,
        "excel_row_count": None,
        "blocks":          blocks,
        "fields":          fields,
        "buttons":         buttons,
        "dependencies":    dependencies,
        "validations":     validations,
        "field_decisions":  [],
        "grid_decisions":   {},
        "button_decisions": {},
        "schema_version":   1,
    }
    _db()["screens"].insert_one(doc)
    return sid


def list_screens(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    _ = db_path
    screens = _db()["screens"]
    runs = _db()["runs"]
    out: list[dict[str, Any]] = []
    cursor = screens.find(
        {},
        projection={
            "id": 1, "name": 1, "function_id": 1, "created_at": 1,
            "uixml_filename": 1, "js_filename": 1,
            "verified_at": 1, "verified_by_run_id": 1, "workflow_mode": 1,
            "fields": 1, "blocks": 1,
        },
    ).sort([("created_at", -1), ("id", -1)])
    for s in cursor:
        sid = s.get("id")
        run_total = runs.count_documents({"screen_id": sid})
        run_ok = runs.count_documents({"screen_id": sid, "status": "completed"})
        out.append({
            "id":                  sid,
            "name":                s.get("name"),
            "function_id":         s.get("function_id"),
            "created_at":          s.get("created_at"),
            "uixml_filename":      s.get("uixml_filename"),
            "js_filename":         s.get("js_filename"),
            "verified_at":         s.get("verified_at"),
            "verified_by_run_id":  s.get("verified_by_run_id"),
            "workflow_mode":       s.get("workflow_mode"),
            "field_count":         len(s.get("fields") or []),
            "block_count":         len(s.get("blocks") or []),
            "run_count":           run_total,
            "run_success_count":   run_ok,
        })
    return out


def get_screen(db_path: str | Path | None, screen_id: int) -> dict[str, Any] | None:
    _ = db_path
    doc = _db()["screens"].find_one({"id": int(screen_id)})
    if doc is None:
        return None
    out = _strip_id(doc)
    # Drop fields we keep purely for lifecycle bookkeeping that the
    # SQLite version never returned via get_screen — they're available
    # via more specific accessors (get_recipe, get_grid_decisions,
    # get_button_decisions).
    out.pop("field_decisions", None)
    out.pop("grid_decisions", None)
    out.pop("button_decisions", None)
    out.pop("schema_version", None)
    # Match the old contract: `dependencies`/`validations` are flat lists.
    out.setdefault("blocks", [])
    out.setdefault("fields", [])
    out.setdefault("buttons", [])
    out.setdefault("dependencies", [])
    out.setdefault("validations", [])
    return out


def get_meta_yaml(db_path: str | Path | None, screen_id: int) -> str | None:
    _ = db_path
    doc = _db()["screens"].find_one(
        {"id": int(screen_id)}, projection={"meta_yaml": 1, "_id": 0},
    )
    return doc.get("meta_yaml") if doc else None


def delete_screen(db_path: str | Path | None, screen_id: int) -> None:
    _ = db_path
    sid = int(screen_id)
    _db()["runs"].delete_many({"screen_id": sid})
    _db()["screens"].delete_one({"id": sid})


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def create_run(
    db_path: str | Path | None,
    screen_id: int,
    base_url: str | None,
    log_path: str,
    screenshots_dir: str,
) -> int:
    _ = db_path
    rid = _next_id("runs")
    _db()["runs"].insert_one({
        "id":              rid,
        "screen_id":       int(screen_id),
        "status":          "starting",
        "started_at":      _utcnow(),
        "finished_at":     None,
        "exit_code":       None,
        "pid":             None,
        "base_url":        base_url,
        "log_path":        log_path,
        "screenshots_dir": screenshots_dir,
        "error_message":   None,
        "kind":            None,
        "schema_version":  1,
    })
    return rid


def update_run(db_path: str | Path | None, run_id: int, **fields) -> None:
    _ = db_path
    if not fields:
        return
    _db()["runs"].update_one({"id": int(run_id)}, {"$set": fields})


def get_run(db_path: str | Path | None, run_id: int) -> dict | None:
    _ = db_path
    return _strip_id(_db()["runs"].find_one({"id": int(run_id)}))


def list_runs(db_path: str | Path | None, screen_id: int) -> list[dict]:
    _ = db_path
    cursor = (
        _db()["runs"]
        .find({"screen_id": int(screen_id)})
        .sort("id", -1)
        .limit(50)
    )
    return [_strip_id(d) for d in cursor]


def set_run_kind(db_path: str | Path | None, run_id: int, kind: str) -> None:
    _ = db_path
    _db()["runs"].update_one({"id": int(run_id)}, {"$set": {"kind": kind}})


# ---------------------------------------------------------------------------
# Field decisions + generated CLAUDE.md
# ---------------------------------------------------------------------------

def save_field_decisions(
    db_path: str | Path | None,
    screen_id: int,
    decisions: list[dict],
    workflow_mode: str,
    claude_md: str,
) -> None:
    _ = db_path
    cleaned = [
        {
            "block_name": d.get("block_name"),
            "field_name": d["field_name"],
            "mode":       d["mode"],
            "value":      d.get("value"),
        }
        for d in decisions
    ]
    _db()["screens"].update_one(
        {"id": int(screen_id)},
        {"$set": {
            "field_decisions": cleaned,
            "workflow_mode":   workflow_mode,
            "claude_md":       claude_md,
        }},
    )


def get_field_decisions(
    db_path: str | Path | None, screen_id: int,
) -> dict[tuple[str | None, str], dict]:
    _ = db_path
    doc = _db()["screens"].find_one(
        {"id": int(screen_id)},
        projection={"field_decisions": 1, "_id": 0},
    )
    items = (doc or {}).get("field_decisions") or []
    return {(d.get("block_name"), d["field_name"]): dict(d) for d in items}


def get_claude_md(db_path: str | Path | None, screen_id: int) -> str | None:
    _ = db_path
    doc = _db()["screens"].find_one(
        {"id": int(screen_id)}, projection={"claude_md": 1, "_id": 0},
    )
    val = (doc or {}).get("claude_md")
    return val if val else None


# ---------------------------------------------------------------------------
# Verification — recipe lifecycle
# ---------------------------------------------------------------------------

def mark_verified(
    db_path: str | Path | None,
    screen_id: int,
    run_id: int,
    recipe: dict | None,
) -> None:
    _ = db_path
    payload = json.dumps(recipe, default=str) if recipe is not None else None
    _db()["screens"].update_one(
        {"id": int(screen_id)},
        {"$set": {
            "verified_at":        _utcnow(),
            "verified_by_run_id": int(run_id),
            "recipe_json":        payload,
        }},
    )


def unmark_verified(db_path: str | Path | None, screen_id: int) -> None:
    _ = db_path
    _db()["screens"].update_one(
        {"id": int(screen_id)},
        {"$set": {
            "verified_at":        None,
            "verified_by_run_id": None,
            "recipe_json":        None,
        }},
    )


def get_recipe(db_path: str | Path | None, screen_id: int) -> dict | None:
    _ = db_path
    doc = _db()["screens"].find_one(
        {"id": int(screen_id)}, projection={"recipe_json": 1, "_id": 0},
    )
    raw = (doc or {}).get("recipe_json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Bulk-load Excel state
# ---------------------------------------------------------------------------

def save_excel_upload(
    db_path: str | Path | None,
    screen_id: int,
    *,
    filename: str,
    path: str,
    row_count: int,
) -> None:
    _ = db_path
    _db()["screens"].update_one(
        {"id": int(screen_id)},
        {"$set": {
            "excel_filename":    filename,
            "excel_path":        path,
            "excel_uploaded_at": _utcnow(),
            "excel_row_count":   row_count,
            "workflow_mode":     "bulk_load",
        }},
    )


def clear_excel_upload(db_path: str | Path | None, screen_id: int) -> None:
    _ = db_path
    _db()["screens"].update_one(
        {"id": int(screen_id)},
        {"$set": {
            "excel_filename":    None,
            "excel_path":        None,
            "excel_uploaded_at": None,
            "excel_row_count":   None,
        }},
    )


# ---------------------------------------------------------------------------
# Grid-block decisions
# ---------------------------------------------------------------------------

def save_grid_decisions(
    db_path: str | Path | None,
    screen_id: int,
    grids: dict[str, list[dict]],
) -> None:
    _ = db_path
    # Store as a plain dict (block_name → [rows]) — symmetrical to the
    # shape get_grid_decisions returns. Empty grids stay in the dict so
    # the UI distinguishes "explicitly cleared" from "never touched".
    _db()["screens"].update_one(
        {"id": int(screen_id)},
        {"$set": {"grid_decisions": dict(grids or {})}},
    )


def get_grid_decisions(
    db_path: str | Path | None, screen_id: int,
) -> dict[str, list[dict]]:
    _ = db_path
    doc = _db()["screens"].find_one(
        {"id": int(screen_id)}, projection={"grid_decisions": 1, "_id": 0},
    )
    raw = (doc or {}).get("grid_decisions") or {}
    return {k: list(v or []) for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Custom-button decisions — opt-in click prompts for non-default screen buttons
# ---------------------------------------------------------------------------

def save_button_decisions(
    db_path: str | Path | None,
    screen_id: int,
    decisions: dict[str, bool],
) -> None:
    """Persist `{button_name: True/False}` map. True means "click this button
    after filling the form, before Save". Replaces any prior decision."""
    _ = db_path
    cleaned = {str(k): bool(v) for k, v in (decisions or {}).items()}
    _db()["screens"].update_one(
        {"id": int(screen_id)},
        {"$set": {"button_decisions": cleaned}},
    )


def get_button_decisions(
    db_path: str | Path | None, screen_id: int,
) -> dict[str, bool]:
    _ = db_path
    doc = _db()["screens"].find_one(
        {"id": int(screen_id)}, projection={"button_decisions": 1, "_id": 0},
    )
    raw = (doc or {}).get("button_decisions") or {}
    return {str(k): bool(v) for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Project settings (key-value store)
# ---------------------------------------------------------------------------

def get_setting(db_path: str | Path | None, key: str) -> str | None:
    _ = db_path
    doc = _db()["kv"].find_one({"_id": key})
    return doc.get("value") if doc else None


def get_all_settings(db_path: str | Path | None = None) -> dict[str, str]:
    _ = db_path
    return {d["_id"]: d.get("value") for d in _db()["kv"].find({})}


def set_settings(db_path: str | Path | None, items: dict[str, str | None]) -> None:
    _ = db_path
    coll = _db()["kv"]
    for k, v in items.items():
        if v is None or v == "":
            coll.delete_one({"_id": k})
        else:
            coll.update_one(
                {"_id": k},
                {"$set": {"value": v}},
                upsert=True,
            )


# ---------------------------------------------------------------------------
# Eligibility for verification (most-recent successful Claude Code run)
# ---------------------------------------------------------------------------

def get_eligible_verify_run(
    db_path: str | Path | None, screen_id: int,
) -> dict | None:
    _ = db_path
    cursor = (
        _db()["runs"]
        .find({
            "screen_id": int(screen_id),
            "status":    "completed",
            # Match COALESCE(kind, 'claude_code') = 'claude_code': accept either
            # an explicit claude_code kind or a row where kind was never set.
            "$or": [{"kind": "claude_code"}, {"kind": None}, {"kind": {"$exists": False}}],
        })
        .sort("id", -1)
        .limit(1)
    )
    for d in cursor:
        return _strip_id(d)
    return None
