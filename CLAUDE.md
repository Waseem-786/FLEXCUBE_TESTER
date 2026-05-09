# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small Flask web app that auto-generates [CLAUDE.md](samples/CLAUDE.md)-style automation plans for Oracle FLEXCUBE screens **and optionally executes them** against a real FLEXCUBE. The user uploads a screen's `UIXML` + (optional) `js` files, fills a per-field review form (or uploads an Excel for bulk mode), clicks Generate, and gets a deterministic, house-style markdown plan. Two execution paths then drive Chromium against a real FLEXCUBE: an LLM-agent runner (Claude Code + Playwright MCP) and a deterministic Python runner (Playwright sync API + selectors calibrated from a verified run).

The team-authored reference plan that defines the house style lives at [samples/CLAUDE.md](samples/CLAUDE.md). New plans are modelled on it.

For the user-facing description, run-instructions, and project layout, read [README.md](README.md) — don't duplicate them here.

## Common commands

```powershell
python -m pip install -r requirements.txt
copy .env.example .env  # then fill in MONGODB_URI (required) + FLEXCUBE_* (optional fallbacks)
python app.py
# http://127.0.0.1:5050
```

For execution, additionally:
```powershell
npm install -g @anthropic-ai/claude-code
python -m playwright install chromium
```

After Settings page is filled (or `.env` has FLEXCUBE_* values), navigate to **Settings** in the app to configure FLEXCUBE Base URL / User / Password — these persist into the MongoDB `kv` collection and the home-page upload form requires them before letting you submit.

If you have an old `screens.db` from the SQLite era:
```powershell
python migrate_sqlite_to_mongo.py
# then verify in Compass / the app, and once happy:
Remove-Item screens.db
```

Smoke-test a parser layer in isolation (debugging):
```powershell
python flexcube_uixml_parser.py samples/IADSKINP.xml --out screen_model.json
python flexcube_js_parser.py    samples/IADSKINP_SYS.js --out js_analysis.json
```

There is no test suite. The de-facto smoke test is uploading a sample screen through the UI and walking the Review → Generate → Run flow. When investigating bugs, prefer Playwright MCP (browser automation against the live Flask) over manual click-testing — `mcp__playwright__browser_navigate` + `browser_snapshot` is faster and more reliable than describe-and-click.

When tearing down dev state: stop Flask, drop the Mongo `flexcube` database (or just the relevant collections via Compass), then `Remove-Item runs, uploads -Recurse -Force`.

## Architecture in one diagram

```
                       ┌────────────────────────────┐
   Browser upload ───► │  app.py (Flask routes)     │
   (UIXML + JS +       │  POST /upload              │
    screen name)       │  GET  /screens/<id>/review │
                       │  POST /screens/<id>/generate│
                       │  POST /screens/<id>/run     │ ← LLM agent
                       │  POST /screens/<id>/run/    │
                       │       deterministic         │ ← Python runner
                       │  POST /screens/<id>/verify  │ ← promote a CC run
                       │  POST /screens/<id>/excel-* │ ← bulk-load
                       │  GET/POST /settings         │ ← runtime credentials
                       └─────┬──────────────────────┘
                             │
        ┌────────────────────┼─────────────────────────────┐
        ▼                    ▼                             ▼
flexcube_uixml_      flexcube_js_              claude_md_generator
parser.py            parser.py                 plan_compiler  ◄── reused for
→ ScreenModel        → JSAnalysisResult        → markdown +       both runners
        │                    │                     structured plan
        └─► mongo_db.py (MongoDB, indexes      ◄── excel_handler
            ensured lazily on first connect;       (bulk load)
            counters collection mints              ▲
            sequential ids for screens/runs)       │
                          │                        │
                          ▼                        │
              meta.yaml + CLAUDE.md +              │
              recipe_json + excel_path             │
              embedded on the screen document      │
                                                   │
            ┌──────────────────────────────────────┘
            │
            ▼
   ┌────────────────────────────┐    ┌────────────────────────────┐
   │  runner.start_run          │    │  runner.start_run_         │
   │  (Claude Code agent path)  │    │  deterministic              │
   │  spawns: claude -p ...     │    │  spawns: python              │
   │  + MCP Playwright tools    │    │  deterministic_runner.py     │
   │                            │    │  + Playwright sync API       │
   │  reads recipe? no — agent  │    │  reads recipe? YES — applies │
   │  re-reads DOM each step    │    │  per-screen overrides        │
   └────────────┬───────────────┘    └────────────┬─────────────────┘
                │                                 │
                └────────► Chromium → FLEXCUBE ◄──┘
                              │
                              ▼
                    log.jsonl + screenshots/
                    (same SSE stream renders both)
```

Five things are load-bearing about this design:

1. **Deterministic generation.** Same UIXML/JS + same field decisions / Excel rows always produce byte-identical markdown. That makes patch-upgrade diffs trustworthy.
2. **Two-stage user flow.** Upload deposits the parsed model; Review collects per-field decisions (or an Excel upload); Generate composes. We don't try to infer test inputs from the artifacts — the human supplies them.
3. **No LLM in the *generation* path.** A previous design (now removed) called the Anthropic API to compose plans. v1 went deterministic-template because the sample CLAUDE.md is structured enough that a template reproduces it cleanly.
4. **LLM only at *execution* time, and out-of-process.** The Claude Code runner uses the user's subscription, not an API key. Subprocess isolation means a runaway run can't crash Flask, and a Stop button kills the whole tree.
5. **Verification turns Claude Code's adaptations into deterministic recipes.** When a Claude Code run succeeds, the user can click *Verify & save recipe*; the app parses the run's stream-json log and saves per-screen selector overrides (checkbox click strategies, LOV iframe titles) the deterministic runner reads on subsequent runs. That's how a screen graduates from "needs the LLM agent" → "fast deterministic only".

## Module roles

- **[app.py](app.py)** — Flask routes. Upload + review + generate + the verify/unverify lifecycle + Excel template download/upload + both runner entry points + the SSE log stream + screenshot serving. Reads uploaded UIXML/JS in-memory via `parse_string()` — no temp files.

- **[mongo_db.py](mongo_db.py)** — MongoDB persistence. Drop-in replacement for the v1 SQLite layer; mirrors every public function name, signature, and return shape so callers (`app.py`, `runner.py`, `recipe_extractor.py`) only changed import lines.
  - **Collections**:
    - `screens` — one document per uploaded screen with embedded `blocks`, `fields`, `buttons`, `dependencies`, `validations`, `field_decisions`, `grid_decisions`, `button_decisions` arrays/maps. One round-trip read replaces five SQLite SELECTs.
    - `runs` — plan-execution sessions, references `screen_id` (numeric).
    - `kv` — Settings-page values; `_id` IS the key, `value` field holds the value.
    - `counters` — `find_one_and_update($inc)` source for sequential integer IDs so existing URLs (`/screens/<int:id>`, `/runs/<int:id>`) keep working without ObjectId everywhere.
  - **Connection model**: lazy module-level `MongoClient` singleton. `_db()` returns the database object, opening the connection on first call and creating indexes once.
  - **Bootstrap config**: `MONGODB_URI` MUST live in `.env` / `os.environ`, NOT in the kv collection — there'd be a chicken-and-egg if the connection string were itself stored in the database we're trying to connect to. `MONGODB_DB` defaults to `"flexcube"` (override via env).
  - **`db_path` parameter is preserved** on every public function for API back-compat with the SQLite era, but it's ignored — the Mongo client is process-global. This made the migration a one-line import swap.
  - **Settings + grid + button decisions** continue to be persisted as before — the Settings UI is the canonical surface for FLEXCUBE_* values; grid_decisions persists Create-New per-row grid input; button_decisions persists per-screen custom-button click toggles. Bulk-load uses Excel sheets (not these collections) for grid rows + per-row button decisions.
  - **Field options persistence** (sub-bug fixed in this migration): the SQLite version never stored `field.options` even though the parser captured them. `save_screen` now persists `options` on each field doc so dropdown widgets in the review form and list-validation in the Excel template both light up.
- **[migrate_sqlite_to_mongo.py](migrate_sqlite_to_mongo.py)** — one-shot copy of an existing `screens.db` into the configured Mongo cluster. Idempotent (upserts by `function_id` for screens, by `id` for runs); bumps the `counters.{screens,runs}.seq` to the max id seen so future inserts continue numbering. Run once after pointing `MONGODB_URI` at the cluster, then delete `screens.db`.

- **[flexcube_uixml_parser.py](flexcube_uixml_parser.py)** — pure XML, no AI. Handles two dialects:
  - **Attribute-based**: `<FIELD Name="X" Label="Y" Required="Y" Lov="LOV_Z"/>`.
  - **Child-element-based** (real FLEXCUBE export, e.g. [IADPRFNL.xml](samples/IADPRFNL.xml), [IADSKINP.xml](samples/IADSKINP.xml), [IADADHPL.xml](samples/IADADHPL.xml)): `<FIELD><NAME>X</NAME><LBL>Y</LBL><REQD>-1</REQD><LOV><NAME>LOV_Z</NAME></LOV></FIELD>`.

  The `_attr_or_child` helper is what makes a single `_parse_field` cover both: per name in priority order, it checks attribute first, then direct child element's text. **Don't refactor it to "search children first"** — `<FIELD ID="1"><NAME>FUNDID</NAME>` would then mis-name fields as `1`. Per-name priority is load-bearing.

  Other things to know:
  - `_to_bool` accepts `-1` (FLEXCUBE Forms idiom for true).
  - `BLOCK_TAGS` includes `FLDSET` and `SUMBLOCK` for the real dialect.
  - `GRID_HINT_ATTRS` includes `ME` (multi-entry) for FLDSET TYPE.
  - `_lov_from` handles nested `<LOV><NAME>...</NAME></LOV>`.
  - `parse_file` / `parse_string` accept a `filename_hint` so the function ID can fall back to `IADSKINP.xml` → `IADSKINP` when the file itself doesn't carry one.
  - `_inject_standard_buttons()` always adds `New / Save / Enter Query / Execute Query / Unlock / Authorize / Copy / Close` so downstream code can assume they exist.
  - **`<FIELD><TYPE>BUTTON</TYPE>...</FIELD>` is routed to `model.buttons`, not `block.fields`** — FLEXCUBE encodes custom in-screen action buttons (Submit / Calculation / etc.) as a field whose widget type is BUTTON. The parser detects this in `_parse_block` and emits a `ButtonModel(is_custom=True, parent_block=..., position_in_block=N)` where N is the count of FIELDs already added to that block. Downstream the generator/compiler interleave the click step at `position_in_block` so buttons fire in their natural UIXML position (not all dumped before Save).
  - **`SKIP_IF_UNDER = {"SUMMARY", "FOOTER", "HEADER"}`** — the structural filter that excludes FLEXCUBE chrome blocks (QUERY/RESULT summary, audit footer). Don't change this without considering screens that legitimately use those tags for non-chrome content.

- **[flexcube_js_parser.py](flexcube_js_parser.py)** — heuristic regex-based static analysis (not a real AST). Surfaces three signals: attached events (`onChange` / `onValidate` / etc.) via `ATTACH_RE`; inferred validations (regex literals, length checks, empty/null guards, numeric ranges); cross-field reads/writes/enables/disables/shows/hides. The result feeds `meta.yaml` only — v1 doesn't use it for the workflow body itself. Documented escape hatch if precision matters: replace `_collect_handlers` and `_collect_field_refs` with AST-backed implementations (esprima / acorn-via-subprocess).

- **[meta_generator.py](meta_generator.py)** — `(screen_name, ScreenModel, JSAnalysisResult) → meta.yaml`. Multiline strings render as YAML literal blocks (`|`) thanks to `_str_representer`. The `instructions_for_claude` constant embeds FLEXCUBE house conventions so the YAML is self-sufficient if a user wants to hand it to Claude directly.

- **[claude_md_generator.py](claude_md_generator.py)** — `(screen_dict, workflow_mode, decisions, excel_rows=None, button_decisions=None) → CLAUDE.md`. Two composition paths:
  - **`create_new`** — one record. The contract:
    - `decisions` is a list of dicts: `{block_name, field_name, mode, value}`.
    - `mode ∈ {value, today, option, tick, untick, lov_match, skip, excel}`.
    - `parse_decisions_from_form(fields, request.form)` translates the multipart form into this shape; the form keys are `mode_<block>__<field>` and `value_<block>__<field>`.
    - `_pick_primary_key` heuristically picks the field the checker user queries by: first required text-like field on the first non-grid block that has a value.
    - `_field_action_lines` is where field-type → markdown bullet rendering lives. **Read-only fields early-return `[]`** so they never produce a bullet or a "required field has no value" TODO — they're treated as auto-populated by FLEXCUBE and excluded everywhere (form, plan, Excel template, plan_compiler steps).
  - **`bulk_load`** — N records, one per Excel row. `_generate_bulk_load` reuses `_field_action_lines` per row, pre-expanded. Cap: `BULK_LOAD_ROW_CAP = 50` (configurable). Per-row materialisation happens in `_materialise_for_row(decisions, excel_row, fields)` which is **type-aware**: LOV-bound fields → `lov_match`, CHECKBOX cells `{Yes, Y, TRUE, 1, …}` → `tick`, DROPDOWN/RADIO → `option`, others → `value`. Empty cells → `skip`. Mirror copy of this lives in [plan_compiler.py](plan_compiler.py); **keep them in sync**.
  - **Grids (multi-row)** — `_render_grid_rows(grid, grid_fields, rows)` emits one `Click + (Add Row)` block per supplied row, with each cell rendered through `_field_action_lines` via `_cell_to_decision`. Read-only-only grids are filtered out via `_all_readonly()` — they never appear in the plan, the review form, or the Excel template. For Create New, rows come from `parse_grid_decisions_from_form()` which reads form keys of the shape `grid_<BLOCK>_<ROW_IDX>_<FIELD>=value` (cap: `GRID_MAX_ROWS = 20`). For Bulk Load, rows come from extra Excel sheets named after the grid block, joined to master rows via a `MASTER_KEY` column (`_filter_grid_rows_for_master`).
  - **Custom in-screen buttons** — `_render_block_actions` interleaves "Click **&lt;Label&gt;** button" lines at each button's `position_in_block` during the block's field walk, so a SUBMIT button declared after the last field of FST_MASTER_1 fires at the end of that block (not after the entire form). The toggle comes from `button_decisions[name]` (review-form checkbox, global) for create_new, and from `_resolve_button_decisions_for_row(buttons, excel_row, global_decisions)` for bulk_load — Excel cell `Press_<NAME>` with Yes/No overrides the global toggle on a per-row basis. Buttons with no parent_block emit before Save as an orphan-fallback step.
  - **Conditional troubleshooting section** — when the screen has at least one editable grid, the generated CLAUDE.md appends a `## Troubleshooting` section explaining FCJNeoWeb's lazy-mounted grid inputs and the click-cell-then-type-then-Tab workaround so the agent reading the plan has the recipe to recover from a silent fill.

- **[plan_compiler.py](plan_compiler.py)** — same `(screen, decisions, workflow_mode, excel_rows, grid_rows, excel_grid_rows, button_decisions, recipe)` inputs as the markdown generator → `list[step_dict]`. The deterministic runner consumes this. Step kinds:
  - **Master block** — `navigate`, `login`, `dismiss_info_popup`, `fast_path`, `click_screen_action`, `fill_field`, `enter_date`, `select_dropdown`, `tick_checkbox`, `untick_checkbox`, `select_lov`, `click_screen_button`, `screenshot`, `todo`.
  - **Grid (multi-row)** — `grid_add_row`, `grid_fill_field`, `grid_enter_date`, `grid_select_dropdown`, `grid_tick_checkbox`, `grid_select_lov`. Emitted by `_compile_grid_steps(grid, fields, rows, grid_index=N)` for editable grid blocks. Read-only grids (every field readonly) are auto-skipped, mirroring the markdown generator.
  - **Replay** — `replay_step` is emitted post-compile by `_apply_recipe_recordings(steps, recipe, screen)` when a verified recipe contains `step_recordings` for a title that the typed compiler can't model. **Boilerplate titles (Login / Post-login / Navigate / NEW / Save / Validation / Fill … / Add Rows … / Process row …) match `_TYPED_TITLE_PATTERNS` and are intentionally NOT replayed** — their typed handlers have multi-strategy selector fallbacks that a single-observation recording can't match. Replay is reserved for the long tail (custom confirmation dialogs, multi-tab navigation) that one specific agent run had to deal with.

  Two positional indices flow from the compiler to the runner via step args:

  - **LOV index** — precomputed by walking declaration order, counting LOV-bound fields. `lov_button_for_field(frame, idx)` does `.nth(idx)`. Works because UIXML declaration order matches on-screen render order in FLEXCUBE.
  - **Grid index** — `_editable_grid_index_map(blocks, fields)` returns `{block_name → 0-based position among editable grids in DOM order}`. Read-only-only grids are excluded from the count. Stamped on every `grid_add_row` step's args so the runner picks the right grid's `+` button on multi-grid screens (e.g. IADADHPL has both an Asset grid and a Borrower / Liability grid). Without this, `.last` would consistently target whichever grid renders last in DOM. **Old recipes / plans without `grid_index` keep working** via the `.last` fallback in `flexcube_selectors.grid_add_row_button`.

  **Custom in-screen buttons interleave at their UIXML position.** `_block_steps(block, fields, decisions, lov_index_map, block_buttons, button_decisions)` mirrors the markdown generator: it walks `block_fields` and at each "field-index slot" emits any buttons whose `position_in_block == slot` as `click_screen_button` steps. So a SUBMIT button declared at the end of FST_MASTER_1 emits inside Step 5 "Fill Pool Selection" (after the last field), not lumped at end-of-flow before Save. Orphan buttons (no parent_block) emit a fallback step before Save.

  **Read-only fields are early-returned with `[]` from `_field_steps`** even if a stale decision exists in the saved plan; defensive coding so legacy data doesn't accidentally produce a step.

- **[excel_handler.py](excel_handler.py)** — bulk-load XLSX I/O. **Multi-sheet**: a Master sheet (master-block editable fields, sheet name `Master` when there are also grid sheets, legacy `Data` otherwise) plus one extra sheet per editable grid block. Each grid sheet leads with a `MASTER_KEY` column that the bulk composer uses to join grid rows to master rows. Read-only-only grids (every field readonly) are skipped from the template entirely. `read_uploaded_full(path)` returns `{"_master": [...], "<grid_block_name>": [...]}` for downstream use; `read_uploaded(path)` is a back-compat shim that returns just the master rows. `write_template(decisions, screen)` derives the sheet structure from `screen.blocks` and per-type formatting via `_apply_column_format`:
  - DATE → `YYYY-MM-DD` cell format
  - CHECKBOX → Yes/No data-validation dropdown
  - DROPDOWN/RADIO → list-validation dropdown of parsed `<OPTION>` values
  - NUMBER → `0` or `0.<precision>` cell format
  - LOV-bound → free text (no validation)
  - VARCHAR / TEXT → free text
  - BUTTON_PRESS (synthetic — for `Press_<NAME>` columns appended at the end of the master sheet for each custom in-screen button) → Yes/No data-validation dropdown. Read back via `_resolve_button_decisions_for_row` so each row decides independently whether to click the button.

  Row 1 = field NAME (canonical key for read-back); row 2 = format-aware label hint, ignored by reader. `read_uploaded(path)` parses back into row dicts keyed by header. Coerces dates to ISO, integer floats to ints, booleans to `Yes`/`No`. The bulk_load workflow is currently **zero-config** on the frontend — every non-readonly field is automatically a column; the user fills cells and uploads.

- **[runner.py](runner.py)** + **[.mcp.json](.mcp.json)** — the v1 plan-execution layer. Two flavours of `start_run` sharing the same Popen / log / Stop machinery:
  - **`start_run(...)`** spawns Claude Code as a subprocess (`claude -p --mcp-config .mcp.json --strict-mcp-config --permission-mode bypassPermissions --output-format stream-json --verbose`); the wrapper prompt (piped via stdin, NOT argv, so credentials don't show up in `tasklist`/`ps`) tells Claude to read the generated CLAUDE.md and execute it via Playwright MCP tools.
  - **`start_run_deterministic(...)`** spawns `python deterministic_runner.py --plan <plan.json> --env-file .env --screenshots-dir <dir> --function-id <fid> --recipe <recipe.json>`; the runner uses Playwright sync API directly. Recipe is loaded from `screens.recipe_json` if the screen is verified, **passed to `compile_plan(...)` so `_apply_recipe_recordings` can swap any matching titles for `replay_step`**, and ALSO persisted next to the plan so the runner subprocess reads selector overrides (`checkbox_strategy`, `lov_popup_titles`, `screen_iframe_hint`) from disk.
  - **`runtime_config(db_path=None)`** resolves runtime values in priority order: Settings page (`kv` table) → `.env` → `os.environ`. The Settings UI is the canonical surface; `.env` is the legacy fallback. All callers (Claude Code spawn env, deterministic runner CLI args, recipe extractor's placeholder generation) go through this single function so the config story is uniform.

  Things to know:
  - **All env vars are `FLEXCUBE_`-prefixed.** Earlier `USERNAME` collided with the Windows OS login env var and silently leaked the wrong value into the prompt. Don't drop the prefix.
  - **MCP config travels with the project.** `.mcp.json` at project root declares the Playwright MCP server, and the Claude Code subprocess gets `--mcp-config <path> --strict-mcp-config`. This is **load-bearing**: without it, `claude -p` only sees MCP servers from the user's local-scope config, which is tied to whatever cwd they originally ran `claude mcp add` in (usually their home dir). We hit exactly this in development.
  - **`--permission-mode bypassPermissions`** is set because there's no human to approve per-tool prompts in non-interactive `-p` mode. Safety still comes from two layers: (a) `--strict-mcp-config` means only Playwright tools are exposed via MCP, (b) the wrapper prompt's Hard Rules forbid Bash/Write/Edit. Don't relax either layer without thinking through the consequences.
  - **`MCP_CONNECTION_NONBLOCKING=false`** is set on the spawn env. **This is load-bearing for Claude Code 2.1.116+.** Without it, Claude Code runs `--mcp-config` servers fully async at startup; the agent's tool list is finalised *before* the Playwright MCP server finishes registering, so the agent reports "no MCP tools available" and aborts even though the server eventually connects ~3 s later. The smoking-gun debug line is `[MCP] --mcp-config servers running fully async (MCP_CONNECTION_NONBLOCKING)`. We diagnosed this against a real run; setting the env var to `"false"` makes the startup synchronous so MCP tools are registered before the toolset is exposed to the agent.
  - `_LIVE: dict[run_id, Popen]` is module-level and protected by `_LIVE_LOCK`. The Flask reloader will reset it on file change — runs from before a reload couldn't be Stop'd anyway because their Popen object is gone. Known limitation; don't try to make it survive reloads (would need an out-of-process supervisor).
  - **Tree-killing**: on Windows, `_kill_tree` shells out to `taskkill /T /F /PID`. Without `/T` the Chromium that the MCP server spawned gets orphaned and piles up on screen. On POSIX, `os.killpg(os.getpgid(pid), SIGTERM)` covers it.
  - The wrapper prompt's "Hard rules" section is what keeps the agent on rails: it only allows `mcp__<server>__browser_*` tools (no Bash, no Write), pins screenshot directory, requires "STEP N: …" prefixes for live progress, and stops after the maker Save. **Rule 3b** specifically tells the agent how to handle FCJNeoWeb's lazy-mounted grid cells (click cell → type → Tab; do NOT target the underlying `#BLK_<block>__<field>I` mirror input by ID). **If you weaken any of these rules, document why.**
  - **f-string brace escaping in the wrapper prompt** — `build_wrapper_prompt` uses an f-string. JS object literals like `{ name: '<label>' }` must be doubled (`{{ name: '<label>' }}`) or Python parses them as expressions and raises `NameError: name 'name' is not defined` at render time. We hit exactly this when adding Hard Rule 3b — the bug surfaced as an unrelated-looking 500 on the Re-verify button.
  - Each Claude Code log starts with a **spawn header** (`{"type":"system","subtype":"spawn",...}`) recording the exact command, mcp-config path and contents, and cwd. Crucial for diagnosing "agent says no MCP tools".
  - `precheck()` deliberately fails fast on missing `claude` CLI, missing `.mcp.json`, or missing env vars.

- **[deterministic_runner.py](deterministic_runner.py)** + **[flexcube_selectors.py](flexcube_selectors.py)** — standalone Playwright sync runner. Selectors were derived from one real successful Claude Code run (saved at [samples/SAMPLE_TEST_THROUGH_CLAUDE_CODE.txt](samples/SAMPLE_TEST_THROUGH_CLAUDE_CODE.txt)). When FLEXCUBE's UI changes, `flexcube_selectors.py` is the only file that needs updating; the runner code is selector-free.
  - **Browser channel**: launches with `channel="chrome"` first, falling back to `msedge` then bundled chromium. FCJNeoWeb's accessibility tree exposes different ARIA roles in real Chrome vs. bundled Chromium (Fast Path resolves as `combobox` only in Chrome). `_launch_chrome_first()` emits a `system / browser_launched` event so the live log shows which channel was used.
  - **Screen iframe pinning.** At `fast_path` time, `discover_screen_iframe_name(page)` reads the page DOM for the visible named iframe and pins a FrameLocator by its actual `name="<numeric>"` attribute via `screen_frame(page, name=...)`. The original "last visible named iframe" fallback is shadowed the moment a LOV popup opens (it's also a named iframe), so pinning is what keeps the screen frame stable for the rest of the run. Required for any flow that opens an LOV — `_Ctx.screen_iframe_name` holds the pinned value.
  - **LOV button indexing is positional** — `lov_button_for_field(frame, idx)` does `.nth(idx)`. The compiler computes `idx` by walking parsed fields in declaration order and counting LOV-bound ones. Works because UIXML order matches on-screen order in FLEXCUBE.
  - **LOV pre-filter is label-driven.** `_try_lov_prefilter(popup, label, row_match)` derives candidate filter-input names from the field's label first (`"<Label> Code"`, then `"<Label>"`), then tries generic code names (Code, Id, Number, Reference, Account…), then falls back to visible non-readonly text inputs while **explicitly skipping** description-/name-/remarks-/address-style columns. Prevents the bug where typing `PKR` into the Currency LOV's *Description* column made Fetch return wrong rows. Both `_do_select_lov` and `_do_grid_select_lov` pass `label` through.
  - **Checkbox click goes through the visible label**, not the `<input>`. The agent's run timed out on direct input clicks; label-click works. `flexcube_selectors.checkbox_target` enforces this and supports per-screen recipe overrides (`label_click` vs `input_click`).
  - **Two iframe levels matter**: top-level (login + Fast Path), screen iframe (`name=<numeric>`, dynamic, found via `discover_screen_iframe_name()`), and LOV/info-popup iframes nested inside the screen iframe. Frame discipline is the trickiest part of the runner; helpers live in `flexcube_selectors`.
  - **Recipe overrides** flow through `_Ctx.recipe` and are applied by the selector module (not the runner code) — `lov_popup_frame(parent, label, recipe)` and `checkbox_target(frame, label, recipe)`. Adding a new override kind = new key in the recipe dict + a new branch in the relevant selector helper.
  - **`replay_step` handler** walks an action list captured by the recipe extractor: `op ∈ {navigate, click, fill, press, select_option}` plus a `frame_chain` (each hop is a CSS selector — dynamic numeric iframe names are stored as the literal token `iframe[name=":numeric:"]` and re-bound to `_Ctx.screen_iframe_name` at replay), plus a structured `locator` (`{kind: role|text|placeholder|css, ...nth: int|"first"|"last"}`). `_sub(value, cfg, subs)` applies `$BASE_URL`/`$USERNAME`/`$PASSWORD`/`$FUNCTION_ID` placeholder substitution and per-step text-replacement subs. Used only when the compiler emits a `replay_step` (i.e. the recipe had a recording for a title outside `_TYPED_TITLE_PATTERNS`).
  - **Custom in-screen action buttons** — `_do_click_screen_button(args.label)` dispatches via `flexcube_selectors.screen_button(frame, label)` which has a 6-strategy fallback (`button` exact/loose, `link` exact, `input[type=button]`, `input[type=submit]`, visible text). After the click, attempts to dismiss any info popup that follows (some custom actions like Calculation pop a "completed" message); failure to find a popup is silently ignored.
  - **Grid Add-Row is implemented**: 6 grid step kinds. `flexcube_selectors.grid_add_row_button(frame, grid_index=N)` accepts a 0-based grid index and uses `.nth(grid_index)` instead of `.last`, so multi-grid screens (e.g. IADADHPL has Asset + Borrower grids) target the right grid's `+` button. The compiler stamps `grid_index` on each `grid_add_row` step's args via `_editable_grid_index_map`; old plans without `grid_index` fall back to `.last`. Last-row targeting for cell fills via `.last` on `get_by_role("textbox", name=label)`. LOV button in the new row uses `grid_lov_button_last(frame)`.
  - **`grid_cell_focus(frame, page, label)` mounts FCJNeoWeb's lazy grid input.** FCJNeoWeb renders grid cells as styled `<td>` text by default and mounts the editable `<input>` only while the cell has focus, then unmounts on blur. Symptoms: a bare `getByRole('textbox', name=label).fill(value)` either times out (input never in DOM) or briefly succeeds and the value silently disappears when focus moves. The selector helper resolves the column header's bounding box (via `getByRole('columnheader', name=label)` / `<th>:has-text(label)` / `getByText(label)` fallbacks) and the last grid row's bounding box, then issues `page.mouse.click(header_X, row_Y)` — a precise coordinate click in the visible cell area mounts the input. After click the helper waits for the textbox to actually appear in DOM, then returns the locator. Callers (`_do_grid_fill_field`, `_do_grid_enter_date`) `.fill()` the returned locator and press Tab to fire `onValidate`.

- **[recipe_extractor.py](recipe_extractor.py)** — parses a successful Claude Code stream-json log into a per-screen recipe. Two layers of capture:
  - **Selector overrides** (sparse): `checkbox_strategy` (label_click vs input_click per labelled checkbox), `lov_popup_titles` (actual `iframe[title="..."]` strings observed), `screen_iframe_hint` (numeric name attr the agent saw), `saw_save_success_popup`. Heuristic regex-based.
  - **Step recordings** (full replay): `step_recordings: {<step_title>: [<action>, ...]}`. The Playwright JS parser (`parse_playwright_js`, `_parse_terminal_locator`) walks each tool_result's ` ```js …``` ` block and turns `await page.locator('iframe[…]').contentFrame().getByRole('textbox', {name:'…'}).fill('…')` into a structured action with frame_chain + locator + value. Bucketing is anchored on the agent's "STEP N: title" emissions. Captured values are placeholdered via `_placeholderise()` against `runtime_config()` so a replay against a different deployment swaps in new credentials cleanly.

  Two parser-quirks worth knowing: (1) the JS body starts with `page.locator(…)` so the parser strips the leading `page.` before walking the frame chain (otherwise the leading-dot anchor in `_RE_LOC_CSS` never matches the first hop); (2) `_RE_LOC_CSS` and `_RE_GOTO` use a `(?:'…'|"…")` alternation because CSS selectors routinely embed both quote types (e.g. `.locator('iframe[name="21154"]')`).

  Extending it = new regex patterns + new keys in the recipe schema (document them next to `mongo_db.save_screen`'s field doc and in the selectors that consume them).

## Templates + design system

The CSS is a Linear/Vercel-style modern dev-tool aesthetic. It lives in one `<style>` block in [base.html](templates/base.html) — there's no separate `static/` folder.

- **Design tokens** (top of `<style>` in base.html) — surfaces (`--bg`, `--panel`, `--panel-2`, `--panel-hover`, `--border`, `--border-strong`), text (`--text`, `--text-soft`, `--muted`, `--muted-soft`), accent + semantics (`--accent`, `--accent-strong`, `--accent-soft`, `--accent-glow`, `--accent-grad` linear gradient), success/warning/danger w/ matching `*-soft` variants, four radius steps (`--r-sm/md/lg/xl`), three elevation shadows (`--shadow-sm/md/lg`), motion tokens (`--ease`, `--ease-out`, `--t-fast/base/slow`). Page-load `page-in` animation respects `prefers-reduced-motion`. Sticky header with translucent fade. Active-nav state auto-detected from `request.endpoint`. Custom thin scrollbars. Mobile @ 600px breakpoint flexes the nav.

- **[base.html](templates/base.html)** — design tokens + reset + components (panel, pill, button incl. gradient primary / ghost / danger, inputs incl. styled file-selector + dark date-picker icon, tables w/ row-hover, flash messages w/ slide-in animation, code blocks). **Brand mark** (`<span class="brand-mark">FX</span>`) renders a 26×26 gradient tile next to the title in the header. **Settings-required modal** lives at the bottom — invisible by default; the inline script intercepts forms with `data-requires-config` when `cfg_ok` is false, opens the modal, lists missing keys, and offers an "Open Settings →" CTA. Reusable for any future "you must do X first" gate.

- **[index.html](templates/index.html)** — upload form (carries `data-requires-config` so the modal gates it) + recent-screens panel with verified pill column. When `cfg_ok=False`, an inline error banner above the form points users to Settings.

- **[review.html](templates/review.html)** — the heart of the v1 UX.
  - **Form structure is non-trivial**: the page uses HTML5's `form="form-generate"` attribute pattern. There's an empty `<form id="form-generate">` whose action is `/generate`; every per-field input/select carries `form="form-generate"`. The Excel-upload sub-form is a separate sibling form. **Don't nest the upload form inside the generate form** — HTML forbids nested `<form>` elements; browsers silently merge them, so an Upload click would actually submit to /generate (we hit this exact bug; the fix is the attribute pattern).
  - JS toggles UI by `workflow_mode`: `bulk_load` shows the Excel panel and hides the per-field tables (`#field-blocks`). Wrapped in `DOMContentLoaded` because `#field-blocks` appears later in the document than the inline script.
  - Per-field widgets and "From Excel column" option are documented in the file's `field_row` macro.
  - **Read-only fields are dropped entirely** — `editable_fields = block_fields | rejectattr('readonly')`. The metadata header shows the editable count plus a muted `+ N read-only auto-populated` suffix so the user still knows they exist. Blocks with zero editable fields don't render at all.
  - **Workflow-mode card radios** use `:has(input:checked)` for selection state — gives the picked card an accent border + soft glow.
  - **Bulk-load value pollution fix** — when a screen was last saved in bulk_load mode (`mode='excel'` decisions), the saved value is the Excel column NAME (= field name). When the user switches to Create New, the macro now uses `prior_value = '' if prior.mode == 'excel' else prior.value` so the value input doesn't get prefilled with `ASSETCODE` / `SUKKUKHCODE`.
  - **Grid editor** (Create New) — for editable grid blocks the form renders one mini-table per grid with `+ Add row` / `× remove` buttons. Form keys are `grid_<BLOCK>_<ROW_IDX>_<FIELD>=value`; `parse_grid_decisions_from_form()` reads them back. Cap: `GRID_MAX_ROWS = 20`. Read-only-only grids are filtered out so a tester never has to fill auto-populated history columns.
  - **In-screen actions panel** — for any screen.buttons with `is_custom=True`, renders one styled checkbox card per button with hover lift + selected glow. Form keys are `button_<NAME>=1`; the route reads them into the `button_decisions` dict. Hidden when there are no custom buttons.
  - **Sticky action bar** at the bottom (Generate plan → / Cancel) uses `position: sticky; bottom: 16px;` with a `body { padding-bottom: 120px }` reserve so the last form block isn't covered.

- **[screen.html](templates/screen.html)** — screen detail. Two execution-button states: unverified (Claude Code primary, deterministic locked) vs. verified (deterministic primary, Re-verify ghost button). Verified pill + Mark unverified link. Recent runs table. Generated CLAUDE.md preview + download.

- **[run.html](templates/run.html)** — live run page. Subscribes to a Server-Sent Events stream of stream-json output and renders each event (assistant text / tool_use / tool_result / system / result) as a row. Polls `/runs/<id>/screenshots-list` every 3s to refresh the gallery. Top of page shows a green "Verify & save recipe?" prompt when the run is a successful Claude Code run on an unverified screen.

- **[screens.html](templates/screens.html)** — history list with verified-state column, screen-count badge in heading, delete + meta.yaml download per row, and a friendly empty-state CTA when the table is empty.

- **[settings.html](templates/settings.html)** — runtime credentials form. Posts to `/settings`, persists into the MongoDB `kv` collection (canonical source for `runtime_config()`). The header link tints red with a ⚠ when required values are missing; the page itself surfaces a banner with the missing key list.

## Working on this codebase

- **The deterministic-generation pipeline is the product.** Resist requests to "just have the LLM compose it" — the whole point is reproducible diffs across patch upgrades. If a feature genuinely needs LLM creativity, call it from a separate, opt-in path; don't put it on the critical path.

- **Adding a new UIXML dialect** → extend the tag-name sets at the top of [flexcube_uixml_parser.py](flexcube_uixml_parser.py) (`FIELD_TAGS`, `BLOCK_TAGS`, etc.). If field properties are encoded differently, prefer extending `_attr_or_child`'s name list over rewriting `_parse_field`.

- **Adding a new datatype** → branch in `_field_action_lines` in [claude_md_generator.py](claude_md_generator.py) AND `_field_steps` in [plan_compiler.py](plan_compiler.py) (so both runners handle it). Add a case in [review.html](templates/review.html)'s `field_row` macro for the right widget. Add Excel-formatting in `_apply_column_format` and `_format_hint` in [excel_handler.py](excel_handler.py). Update both `_materialise_for_row` functions (one in claude_md_generator, one in plan_compiler) symmetrically. **The widget's name conventions are `mode_<block>__<field>` and `value_<block>__<field>` — keep them so `parse_decisions_from_form` doesn't need to change.**

- **Adding a new workflow mode** (Copy-Existing / Modify):
  1. Enable it in `WORKFLOW_MODES` and add to `ENABLED_MODES` in [review.html](templates/review.html).
  2. Add a branch in `generate_claude_md` AND `compile_plan` to compose the right body / step list.
  3. Keep the deterministic guarantee: same inputs → same markdown.

- **Adding a field to the `screens` document** → just add it where it makes sense in [mongo_db.py:save_screen](mongo_db.py); MongoDB is schemaless so old docs simply lack the new field. If the field needs to surface in history views, also touch the `list_screens` projection. For data already in the cluster you may need a one-shot backfill (see how `migrate_sqlite_to_mongo.py` handles re-parsing samples to fill missing fields).

- **Adding a new collection** → it's lazy — Mongo creates the collection on first insert. Add an index in `_ensure_indexes()` if you'll query/sort by anything other than `_id`.

- **Adding a new recipe override** → new key in the recipe dict shape (document next to `mongo_db.save_screen`) + new regex/extraction in [recipe_extractor.py](recipe_extractor.py) + new branch in the relevant `flexcube_selectors` helper that reads `recipe.get(...)` for an override.

- **Adding a custom-button-like step kind** (something the user opt-in toggles per-screen) → mirror the in-screen-button machinery: parser surfaces it on the model, `mongo_db.save_screen` persists, `app.py` parses form keys into the relevant decisions dict, generator + compiler emit the step at the right slot, runner dispatches to a typed selector helper, recipe extractor adds optional override capture.

- **Don't put state in module-level globals.** All durable state lives in SQLite; `runner._LIVE` is the one exception (process registry for live Stop), and it's documented as resetting on Flask reload.

- **Sample fixtures live in [samples/](samples/).** Real FLEXCUBE artifacts the parsers and runners are battle-tested against, plus the team's hand-written reference plan, plus the Claude Code run log (`SAMPLE_TEST_THROUGH_CLAUDE_CODE.txt`) the deterministic runner's selectors were calibrated from.

## Roadmap

The items below are deliberately not in this snapshot. Listed in rough priority order:

1. **Copy-Existing workflow** — query existing record, click Copy, override key, save.
2. **Modify workflow** — query, Unlock, edit, save.
3. **Maker-checker dual-session execution.** Today the runner stops after the maker save (rule #3 in `runner.build_wrapper_prompt`). Should launch a second Chromium as the checker user and complete the authorize step in one run. Needs the Playwright MCP server to support multiple browser contexts, or two separate MCP server invocations.
4. **Replay value-substitution.** v1 of `replay_step` only swaps `$BASE_URL`-style placeholders + per-step text replacements supplied by the compiler. Future: pair each recorded step with the original plan's args at extract time, so a Bulk Load row can replay the agent's exact action sequence with the new row's value substituted for the recorded one.
5. **Negative & edge test cases** — append a `## Test Cases` section. The deterministic test-case generator that lived at `test_case_synthesiser.py` was removed in early cleanup; restore from git history when re-implementing.
6. **Two-version diff** — pick two uploads of the same screen, render a side-by-side change report. This is the killer use case for patch upgrades.
7. **Multi-step `<HEADER>`-tab screens** — some screens have a real header tab with fields. Today the `SKIP_IF_UNDER` filter would skip them. Reconsider the filter when we hit one.
8. **Run supervisor that survives Flask reloads.** Today `runner._LIVE` is in-memory; if Flask debug-reloads (e.g. you edit a `.py`), the Stop button can't reach already-running subprocesses. Could persist PIDs in Mongo and use psutil to look up the process at Stop time.
9. **Remove the legacy SQLite migration script** — `migrate_sqlite_to_mongo.py` and the `screens.db` files it consumes are transitional. Once everyone in the team has migrated, the script can be deleted.
10. **Auth on the Mongo cluster** — current `MONGODB_URI` carries the user/password inline. For a shared deployment, switch to per-developer credentials with read-only scope where appropriate.

## What's intentionally not here

- No `tests/` directory. The pipeline has no unit tests; smoke-testing is via Playwright against a live Flask. If you add tests, put them under `tests/` and follow `pytest` conventions.
- No CI config. Add when the team is ready to enforce something on PRs.
- No Dockerfile. `python app.py` is the runbook; the runner subprocess needs a real Chrome browser on the host anyway, so containerising is more work than it's worth right now.
- No `static/` folder — base.html inlines its CSS.
- No `anthropic` Python package — execution uses the user's Claude Code subscription via subprocess, not the API. If a future workflow does need the API, add the dep behind a feature flag rather than making the deterministic generation depend on it.
