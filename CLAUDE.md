# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small Flask web app that auto-generates [CLAUDE.md](samples/CLAUDE.md)-style automation plans for Oracle FLEXCUBE screens. The user uploads a screen's `UIXML` + (optional) `js` files, fills a per-field review form, clicks Generate, and gets a deterministic, house-style markdown plan a Playwright/Selenium runner can follow. **No LLM call in the pipeline** — pure Python all the way through.

The team-authored reference plan that defines the house style lives at [samples/CLAUDE.md](samples/CLAUDE.md). New plans are modelled on it.

For the user-facing description, run-instructions, and project layout, read [README.md](README.md) — don't duplicate them here.

## Common commands

```powershell
python -m pip install -r requirements.txt
python app.py
# http://127.0.0.1:5000
```

Smoke-test a parser layer in isolation (debugging):

```powershell
python flexcube_uixml_parser.py samples/IADPBALO.xml --out screen_model.json
python flexcube_js_parser.py    samples/IADPBALO_SYS.js --out js_analysis.json
```

There is no test suite. The de-facto smoke test is uploading a sample screen through the UI and walking the Review → Generate → Download flow. When investigating bugs, prefer Playwright (`pip install playwright && playwright install chromium`) over manual click-testing — the headless run takes seconds.

When tearing down dev state: stop Flask, then `Remove-Item screens.db` to wipe history. The schema is recreated on next run.

## Architecture in one diagram

```
                       ┌────────────────────────────┐
   Browser upload ───► │  app.py (Flask routes)     │
   (UIXML + JS +       │  POST /upload              │
    screen name)       │  GET  /screens/<id>/review │
                       │  POST /screens/<id>/generate│
                       └─────┬──────────────────────┘
                             │
        ┌────────────────────┼─────────────────────┐
        ▼                    ▼                     ▼
flexcube_uixml_      flexcube_js_         claude_md_generator
parser.py            parser.py            (review form +
→ ScreenModel        → JSAnalysisResult    decisions  →
        │                    │             markdown plan)
        └─────► db.py (SQLite, schema    ◄────────┘
                idempotently re-applied
                on every connection)
                          │
                          ▼
              meta.yaml + CLAUDE.md
              persisted on the screens row
```

Three things are load-bearing about this design:

1. **Deterministic everywhere.** Same UIXML/JS + same field decisions always produce byte-identical markdown. That makes patch-upgrade diffs trustworthy.
2. **Two-stage user flow.** Upload deposits the parsed model; Review collects per-field decisions; Generate composes. We don't try to infer test inputs from the artifacts — the human supplies them via the review form.
3. **No LLM call.** A previous design (now removed) called the Anthropic API to compose plans. v1 went deterministic-template because the sample CLAUDE.md is structured enough that a template reproduces it cleanly.

## Module roles

- **[app.py](app.py)** — Flask routes. `GET /` upload form; `POST /upload` parses and persists, redirects to `/screens/<id>/review`; `GET /screens/<id>/review` renders the per-field form; `POST /screens/<id>/generate` saves decisions and runs the composer; `GET /screens/<id>` shows the rendered plan; `GET /screens/<id>/CLAUDE.md` and `/meta.yaml` download endpoints. Reads uploads in-memory via `parse_string()` — no temp files.

- **[db.py](db.py)** — SQLite schema (`screens`, `blocks`, `fields`, `buttons`, `dependencies`, `validations`, `field_decisions`) plus helpers. Two important patterns:
  - **Schema applies on every connection**, not just at app boot, via `_connect()` calling `executescript(SCHEMA)` (idempotent thanks to `CREATE TABLE IF NOT EXISTS`). This protects against a connection that somehow bypasses `init_db()` — earlier we hit a 0-byte `screens.db` from exactly that.
  - **Runtime column migrations**: `_RUNTIME_COLUMNS` lists `(table, column, ddl)` tuples and `_ensure_runtime_columns()` adds anything missing. Use this pattern for any new column on existing tables; don't break old DBs.

- **[flexcube_uixml_parser.py](flexcube_uixml_parser.py)** — pure XML, no AI. Handles two dialects:
  - **Attribute-based** (illustrative, e.g. [IADFNONL.UIXML](samples/IADFNONL.UIXML)): `<FIELD Name="X" Label="Y" Required="Y" Lov="LOV_Z"/>`.
  - **Child-element-based** (real FLEXCUBE export, e.g. [IADPRFNL.xml](samples/IADPRFNL.xml), [IADPBALO.xml](samples/IADPBALO.xml)): `<FIELD><NAME>X</NAME><LBL>Y</LBL><REQD>-1</REQD><LOV><NAME>LOV_Z</NAME></LOV></FIELD>`.

  The `_attr_or_child` helper is what makes a single `_parse_field` cover both: per name in priority order, it checks attribute first, then direct child element's text. **Don't refactor it to "search children first"** — `<FIELD ID="1"><NAME>FUNDID</NAME>` would then mis-name fields as `1`. Per-name priority is load-bearing.

  Other things to know:
  - `_to_bool` accepts `-1` (FLEXCUBE Forms idiom for true).
  - `BLOCK_TAGS` includes `FLDSET` and `SUMBLOCK` for the real dialect.
  - `GRID_HINT_ATTRS` includes `ME` (multi-entry) for FLDSET TYPE.
  - `_lov_from` handles nested `<LOV><NAME>...</NAME></LOV>`.
  - `parse_file` / `parse_string` accept a `filename_hint` so the function ID can fall back to `IADPRFNL.xml` → `IADPRFNL` when the file itself doesn't carry one.
  - `_inject_standard_buttons()` always adds `New / Save / Enter Query / Execute Query / Unlock / Authorize / Copy / Close` so downstream code can assume they exist.
  - **`SKIP_IF_UNDER = {"SUMMARY", "FOOTER", "HEADER"}`** — the structural filter that excludes FLEXCUBE chrome blocks (QUERY/RESULT summary, audit footer). Don't change this without considering screens that legitimately use those tags for non-chrome content. The old dialect has none of these wrappers, so the filter is a no-op there.

- **[flexcube_js_parser.py](flexcube_js_parser.py)** — heuristic regex-based static analysis (not a real AST). Surfaces three signals: attached events (`onChange` / `onValidate` / etc.) via `ATTACH_RE`; inferred validations (regex literals, length checks, empty/null guards, numeric ranges); cross-field reads/writes/enables/disables/shows/hides. The result feeds `meta.yaml` only — v1 doesn't use it for the workflow body itself. Documented escape hatch if precision matters: replace `_collect_handlers` and `_collect_field_refs` with AST-backed implementations (esprima / acorn-via-subprocess).

- **[meta_generator.py](meta_generator.py)** — `(screen_name, ScreenModel, JSAnalysisResult) → meta.yaml`. Multiline strings render as YAML literal blocks (`|`) thanks to `_str_representer`. The `instructions_for_claude` constant embeds FLEXCUBE house conventions so the YAML is self-sufficient if a user wants to hand it to Claude directly.

- **[claude_md_generator.py](claude_md_generator.py)** — `(screen_dict, workflow_mode, decisions) → CLAUDE.md`. The contract:
  - `decisions` is a list of dicts: `{block_name, field_name, mode, value}`.
  - `mode ∈ {value, today, option, tick, untick, lov_match, skip}`.
  - `parse_decisions_from_form(fields, request.form)` translates the multipart form into this shape; the form keys are `mode_<block>__<field>` and `value_<block>__<field>`.
  - `WORKFLOW_MODES` is the source of truth for which modes the UI offers; only `create_new` actually composes today, the others raise `ValueError` from `generate_claude_md`. **Don't enable a workflow in `WORKFLOW_MODES` before its branch is implemented** — the form will accept it and the Generate POST will 500.
  - `_pick_primary_key` heuristically picks the field the checker user queries by: first required text-like field on the first non-grid block that has a value. If you change this, make sure the maker-checker step still names the right field.
  - `_field_action_lines` is where field-type → markdown bullet rendering lives. Adding a new datatype means a new branch here.

## Templates

- **[base.html](templates/base.html)** — layout + dark-theme CSS. All other templates extend it. The `<style>` block is intentionally inline so there's no separate `static/` folder to manage.
- **[index.html](templates/index.html)** — upload form.
- **[review.html](templates/review.html)** — the heart of the v1 UX. Per-field widgets, workflow-mode radio (only `create_new` enabled). Required fields lose the `Skip` mode option; readonly fields are dimmed. Prior decisions pre-fill on revisit.
- **[screen.html](templates/screen.html)** — screen detail. Shows field mapping, blocks, buttons, dependencies, validations, the generated CLAUDE.md (if any), and the meta.yaml.
- **[screens.html](templates/screens.html)** — history list with delete + meta.yaml download per row.

## Working on this codebase

- **The deterministic pipeline is the product.** Resist requests to "just have the LLM compose it" — the whole point of v1 is reproducible diffs across patch upgrades. If a feature genuinely needs LLM creativity (e.g. naming subtle test cases), call it from a separate, opt-in path; don't put it on the critical path.

- **Adding a new UIXML dialect** → extend the tag-name sets at the top of [flexcube_uixml_parser.py](flexcube_uixml_parser.py) (`FIELD_TAGS`, `BLOCK_TAGS`, etc.). If field properties are encoded differently, prefer extending `_attr_or_child`'s name list over rewriting `_parse_field`.

- **Adding a new datatype** → branch in `_field_action_lines` in [claude_md_generator.py](claude_md_generator.py) and add a case in [review.html](templates/review.html)'s `field_row` macro for the right widget. The widget's name conventions are `mode_<block>__<field>` and `value_<block>__<field>` — keep them so `parse_decisions_from_form` doesn't need to change.

- **Adding a new workflow mode** (Copy-Existing / Modify / Bulk-Load):
  1. Enable it in `WORKFLOW_MODES` and remove the `disabled` flag in [review.html](templates/review.html).
  2. Add a branch in `generate_claude_md` to compose the right body.
  3. Bulk-Load also needs Excel column-mapping fields per grid block on the review page.
  4. Keep the deterministic guarantee: same inputs → same markdown.

- **Adding a column to a `screens`-row field** → add it to `_RUNTIME_COLUMNS` in [db.py](db.py); old DBs get migrated transparently.

- **Adding a new table** → add the `CREATE TABLE IF NOT EXISTS` to the `SCHEMA` constant in [db.py](db.py); it'll be created on every connection.

- **Don't put state in module-level globals.** All state lives in SQLite; the Flask reloader can re-import modules at any time and module state would silently diverge.

- **Sample fixtures live in [samples/](samples/).** `IADFNONL.UIXML` (old attribute-based dialect — illustrative), `IADPRFNL.xml` and `IADPBALO.xml` (real FLEXCUBE child-element dialect, exercise the production code path), plus `CLAUDE.md` (the hand-written reference plan that defines the house style).

## Roadmap

The items below are deliberately not in v1. Listed in rough priority order:

1. **Copy-Existing workflow** — query existing record, click Copy, override key, save.
2. **Modify workflow** — query, Unlock, edit, save.
3. **Excel-bulk-load** — per grid block: column-mapping form, "loop over rows" template.
4. **Negative & edge test cases** — append a `## Test Cases` section. The deterministic test-case generator that lived at `test_case_synthesiser.py` was removed in v1 cleanup; restore from git history (`git show <pre-v1>:test_case_synthesiser.py`) when re-implementing.
5. **Two-version diff** — pick two uploads of the same screen, render a side-by-side change report. This is the killer use case for patch upgrades.
6. **Multi-step `<HEADER>`-tab screens** — some screens have a real header tab with fields. Today the `SKIP_IF_UNDER = {SUMMARY, HEADER, FOOTER}` filter would skip them. Reconsider the filter when we hit one.

## What's intentionally not here

- No `tests/` directory. The pipeline has no unit tests; smoke-testing is via Playwright against a live Flask. If you add tests, put them under `tests/` and follow `pytest` conventions.
- No CI config. Add when the team is ready to enforce something on PRs.
- No Dockerfile. Single-file Flask app with two pip deps; `python app.py` is the runbook.
- No `static/` folder — base.html inlines its CSS.
- No `anthropic` dep — v1 doesn't call any LLM. If a future workflow does, add the dep behind a feature flag rather than making the deterministic path depend on it.
