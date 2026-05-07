# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small Flask web app that auto-generates [CLAUDE.md](samples/CLAUDE.md)-style automation plans for Oracle FLEXCUBE screens **and optionally executes them** against a real FLEXCUBE. The user uploads a screen's `UIXML` + (optional) `js` files, fills a per-field review form (or uploads an Excel for bulk mode), clicks Generate, and gets a deterministic, house-style markdown plan. Two execution paths then drive Chromium against a real FLEXCUBE: an LLM-agent runner (Claude Code + Playwright MCP) and a deterministic Python runner (Playwright sync API + selectors calibrated from a verified run).

The team-authored reference plan that defines the house style lives at [samples/CLAUDE.md](samples/CLAUDE.md). New plans are modelled on it.

For the user-facing description, run-instructions, and project layout, read [README.md](README.md) — don't duplicate them here.

## Common commands

```powershell
python -m pip install -r requirements.txt
python app.py
# http://127.0.0.1:5000
```

For execution, additionally:
```powershell
npm install -g @anthropic-ai/claude-code
python -m playwright install chromium
copy .env.example .env  # then fill in FLEXCUBE_BASE_URL etc.
```

Smoke-test a parser layer in isolation (debugging):
```powershell
python flexcube_uixml_parser.py samples/IADSKINP.xml --out screen_model.json
python flexcube_js_parser.py    samples/IADSKINP_SYS.js --out js_analysis.json
```

There is no test suite. The de-facto smoke test is uploading a sample screen through the UI and walking the Review → Generate → Run flow. When investigating bugs, prefer Playwright (`pip install playwright && python -m playwright install chromium`) over manual click-testing — the headless run takes seconds.

When tearing down dev state: stop Flask, then `Remove-Item screens.db, runs, uploads -Recurse -Force`. The schema is recreated on next run.

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
                       └─────┬──────────────────────┘
                             │
        ┌────────────────────┼─────────────────────────────┐
        ▼                    ▼                             ▼
flexcube_uixml_      flexcube_js_              claude_md_generator
parser.py            parser.py                 plan_compiler  ◄── reused for
→ ScreenModel        → JSAnalysisResult        → markdown +       both runners
        │                    │                     structured plan
        └─► db.py (SQLite, schema       ◄────── excel_handler
            idempotently re-applied             (bulk load)
            on every connection)                ▲
                          │                     │
                          ▼                     │
              meta.yaml + CLAUDE.md +           │
              recipe_json + excel_path          │
              persisted on the screens row      │
                                                │
            ┌───────────────────────────────────┘
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

- **[db.py](db.py)** — SQLite schema (`screens`, `blocks`, `fields`, `buttons`, `dependencies`, `validations`, `field_decisions`, `runs`) plus helpers. Two important patterns:
  - **Schema applies on every connection**, not just at app boot, via `_connect()` calling `executescript(SCHEMA)` (idempotent thanks to `CREATE TABLE IF NOT EXISTS`). This protects against a connection that somehow bypasses `init_db()`.
  - **Runtime column migrations**: `_RUNTIME_COLUMNS` lists `(table, column, ddl)` tuples and `_ensure_runtime_columns()` adds anything missing. Use this pattern for any new column on existing tables; don't break old DBs. Currently tracks: `workflow_mode`, `claude_md`, `verified_at`, `verified_by_run_id`, `recipe_json`, `excel_*` (4 columns), and on `runs` table the `kind` column.

- **[flexcube_uixml_parser.py](flexcube_uixml_parser.py)** — pure XML, no AI. Handles two dialects:
  - **Attribute-based** (illustrative, e.g. [IADFNONL.UIXML](samples/IADFNONL.UIXML)): `<FIELD Name="X" Label="Y" Required="Y" Lov="LOV_Z"/>`.
  - **Child-element-based** (real FLEXCUBE export, e.g. [IADPRFNL.xml](samples/IADPRFNL.xml), [IADSKINP.xml](samples/IADSKINP.xml)): `<FIELD><NAME>X</NAME><LBL>Y</LBL><REQD>-1</REQD><LOV><NAME>LOV_Z</NAME></LOV></FIELD>`.

  The `_attr_or_child` helper is what makes a single `_parse_field` cover both: per name in priority order, it checks attribute first, then direct child element's text. **Don't refactor it to "search children first"** — `<FIELD ID="1"><NAME>FUNDID</NAME>` would then mis-name fields as `1`. Per-name priority is load-bearing.

  Other things to know:
  - `_to_bool` accepts `-1` (FLEXCUBE Forms idiom for true).
  - `BLOCK_TAGS` includes `FLDSET` and `SUMBLOCK` for the real dialect.
  - `GRID_HINT_ATTRS` includes `ME` (multi-entry) for FLDSET TYPE.
  - `_lov_from` handles nested `<LOV><NAME>...</NAME></LOV>`.
  - `parse_file` / `parse_string` accept a `filename_hint` so the function ID can fall back to `IADSKINP.xml` → `IADSKINP` when the file itself doesn't carry one.
  - `_inject_standard_buttons()` always adds `New / Save / Enter Query / Execute Query / Unlock / Authorize / Copy / Close` so downstream code can assume they exist.
  - **`SKIP_IF_UNDER = {"SUMMARY", "FOOTER", "HEADER"}`** — the structural filter that excludes FLEXCUBE chrome blocks (QUERY/RESULT summary, audit footer). Don't change this without considering screens that legitimately use those tags for non-chrome content. The old dialect has none of these wrappers, so the filter is a no-op there.

- **[flexcube_js_parser.py](flexcube_js_parser.py)** — heuristic regex-based static analysis (not a real AST). Surfaces three signals: attached events (`onChange` / `onValidate` / etc.) via `ATTACH_RE`; inferred validations (regex literals, length checks, empty/null guards, numeric ranges); cross-field reads/writes/enables/disables/shows/hides. The result feeds `meta.yaml` only — v1 doesn't use it for the workflow body itself. Documented escape hatch if precision matters: replace `_collect_handlers` and `_collect_field_refs` with AST-backed implementations (esprima / acorn-via-subprocess).

- **[meta_generator.py](meta_generator.py)** — `(screen_name, ScreenModel, JSAnalysisResult) → meta.yaml`. Multiline strings render as YAML literal blocks (`|`) thanks to `_str_representer`. The `instructions_for_claude` constant embeds FLEXCUBE house conventions so the YAML is self-sufficient if a user wants to hand it to Claude directly.

- **[claude_md_generator.py](claude_md_generator.py)** — `(screen_dict, workflow_mode, decisions, excel_rows=None) → CLAUDE.md`. Two composition paths:
  - **`create_new`** — one record. The contract:
    - `decisions` is a list of dicts: `{block_name, field_name, mode, value}`.
    - `mode ∈ {value, today, option, tick, untick, lov_match, skip, excel}`.
    - `parse_decisions_from_form(fields, request.form)` translates the multipart form into this shape; the form keys are `mode_<block>__<field>` and `value_<block>__<field>`.
    - `_pick_primary_key` heuristically picks the field the checker user queries by: first required text-like field on the first non-grid block that has a value.
    - `_field_action_lines` is where field-type → markdown bullet rendering lives. Adding a new datatype means a new branch here.
  - **`bulk_load`** — N records, one per Excel row. `_generate_bulk_load` reuses `_field_action_lines` per row, pre-expanded. Cap: `BULK_LOAD_ROW_CAP = 50` (configurable). Per-row materialisation happens in `_materialise_for_row(decisions, excel_row, fields)` which is **type-aware**: LOV-bound fields → `lov_match`, CHECKBOX cells `{Yes, Y, TRUE, 1, …}` → `tick`, DROPDOWN/RADIO → `option`, others → `value`. Empty cells → `skip`. Mirror copy of this lives in [plan_compiler.py](plan_compiler.py); **keep them in sync**.

- **[plan_compiler.py](plan_compiler.py)** — same `(screen, decisions, workflow_mode, excel_rows)` triple as the markdown generator → `list[step_dict]`. The deterministic runner consumes this. Step kinds: `navigate`, `login`, `dismiss_info_popup`, `fast_path`, `click_screen_action`, `fill_field`, `enter_date`, `select_dropdown`, `tick_checkbox`, `untick_checkbox`, `select_lov`, `screenshot`, `todo`. LOV indices are precomputed by walking declaration order — UIXML declaration order matches on-screen render order in FLEXCUBE, so positional `.nth(idx)` indexing is reliable.

- **[excel_handler.py](excel_handler.py)** — bulk-load XLSX I/O. `write_template(decisions, screen)` builds a workbook with one column per "from Excel" decision; per-type formatting via `_apply_column_format`:
  - DATE → `YYYY-MM-DD` cell format
  - CHECKBOX → Yes/No data-validation dropdown
  - DROPDOWN/RADIO → list-validation dropdown of parsed `<OPTION>` values
  - NUMBER → `0` or `0.<precision>` cell format
  - LOV-bound → free text (no validation)
  - VARCHAR / TEXT → free text
  
  Row 1 = field NAME (canonical key for read-back); row 2 = format-aware label hint, ignored by reader. `read_uploaded(path)` parses back into row dicts keyed by header. Coerces dates to ISO, integer floats to ints, booleans to `Yes`/`No`. The bulk_load workflow is currently **zero-config** on the frontend — every non-readonly field is automatically a column; the user fills cells and uploads.

- **[runner.py](runner.py)** + **[.mcp.json](.mcp.json)** — the v1 plan-execution layer. Two flavours of `start_run` sharing the same Popen / log / Stop machinery:
  - **`start_run(...)`** spawns Claude Code as a subprocess (`claude -p --mcp-config .mcp.json --strict-mcp-config --permission-mode bypassPermissions --output-format stream-json --verbose`); the wrapper prompt (piped via stdin, NOT argv, so credentials don't show up in `tasklist`/`ps`) tells Claude to read the generated CLAUDE.md and execute it via Playwright MCP tools.
  - **`start_run_deterministic(...)`** spawns `python deterministic_runner.py --plan <plan.json> --env-file .env --screenshots-dir <dir> --function-id <fid> --recipe <recipe.json>`; the runner uses Playwright sync API directly. Recipe is loaded from `screens.recipe_json` if the screen is verified.

  Things to know:
  - **All env vars are `FLEXCUBE_`-prefixed.** Earlier `USERNAME` collided with the Windows OS login env var and silently leaked the wrong value into the prompt. Don't drop the prefix.
  - **MCP config travels with the project.** `.mcp.json` at project root declares the Playwright MCP server, and the Claude Code subprocess gets `--mcp-config <path> --strict-mcp-config`. This is **load-bearing**: without it, `claude -p` only sees MCP servers from the user's local-scope config, which is tied to whatever cwd they originally ran `claude mcp add` in (usually their home dir). We hit exactly this in development.
  - **`--permission-mode bypassPermissions`** is set because there's no human to approve per-tool prompts in non-interactive `-p` mode. Safety still comes from two layers: (a) `--strict-mcp-config` means only Playwright tools are exposed via MCP, (b) the wrapper prompt's Hard Rules forbid Bash/Write/Edit. Don't relax either layer without thinking through the consequences.
  - `_LIVE: dict[run_id, Popen]` is module-level and protected by `_LIVE_LOCK`. The Flask reloader will reset it on file change — runs from before a reload couldn't be Stop'd anyway because their Popen object is gone. Known limitation; don't try to make it survive reloads (would need an out-of-process supervisor).
  - **Tree-killing**: on Windows, `_kill_tree` shells out to `taskkill /T /F /PID`. Without `/T` the Chromium that the MCP server spawned gets orphaned and piles up on screen. On POSIX, `os.killpg(os.getpgid(pid), SIGTERM)` covers it.
  - The wrapper prompt's "Hard rules" section is what keeps the agent on rails: it only allows `mcp__<server>__browser_*` tools (no Bash, no Write), pins screenshot directory, requires "STEP N: …" prefixes for live progress, and stops after the maker Save. **If you weaken any of these rules, document why.**
  - Each Claude Code log starts with a **spawn header** (`{"type":"system","subtype":"spawn",...}`) recording the exact command, mcp-config path and contents, and cwd. Crucial for diagnosing "agent says no MCP tools".
  - `precheck()` deliberately fails fast on missing `claude` CLI, missing `.mcp.json`, or missing env vars.

- **[deterministic_runner.py](deterministic_runner.py)** + **[flexcube_selectors.py](flexcube_selectors.py)** — standalone Playwright sync runner. Selectors were derived from one real successful Claude Code run (saved at [samples/SAMPLE_TEST_THROUGH_CLAUDE_CODE.txt](samples/SAMPLE_TEST_THROUGH_CLAUDE_CODE.txt)). When FLEXCUBE's UI changes, `flexcube_selectors.py` is the only file that needs updating; the runner code is selector-free.
  - **Browser channel**: launches with `channel="chrome"` first, falling back to `msedge` then bundled chromium. FCJNeoWeb's accessibility tree exposes different ARIA roles in real Chrome vs. bundled Chromium (Fast Path resolves as `combobox` only in Chrome). `_launch_chrome_first()` emits a `system / browser_launched` event so the live log shows which channel was used.
  - **LOV button indexing is positional** — `lov_button_for_field(frame, idx)` does `.nth(idx)`. The compiler computes `idx` by walking parsed fields in declaration order and counting LOV-bound ones. Works because UIXML order matches on-screen order in FLEXCUBE.
  - **Checkbox click goes through the visible label**, not the `<input>`. The agent's run timed out on direct input clicks; label-click works. `flexcube_selectors.checkbox_target` enforces this and supports per-screen recipe overrides (`label_click` vs `input_click`).
  - **Two iframe levels matter**: top-level (login + Fast Path), screen iframe (`name=<numeric>`, dynamic, found via `screen_frame()`), and LOV/info-popup iframes nested inside the screen iframe. Frame discipline is the trickiest part of the runner; helpers live in `flexcube_selectors`.
  - **Recipe overrides** flow through `_Ctx.recipe` and are applied by the selector module (not the runner code) — `lov_popup_frame(parent, label, recipe)` and `checkbox_target(frame, label, recipe)`. Adding a new override kind = new key in the recipe dict + a new branch in the relevant selector helper.
  - **Grid Add-Row is a TODO**: the compiler emits a `todo` step with a reason; the runner reports it and continues. Wire it up when implementing the bulk-load grid feature.

- **[recipe_extractor.py](recipe_extractor.py)** — parses a successful Claude Code stream-json log into a per-screen recipe. Captures: `checkbox_strategy` (label_click vs input_click per labelled checkbox), `lov_popup_titles` (actual `iframe[title="..."]` strings observed), `screen_iframe_hint` (numeric name attr the agent saw), `saw_save_success_popup`. Heuristic regex-based; doesn't try to do full per-step selector replay. Extending it = new regex patterns + new keys in the recipe schema (also document them in `db._RUNTIME_COLUMNS`'s comment + the selectors that consume them).

## Templates

- **[base.html](templates/base.html)** — layout + dark-theme CSS. All other templates extend it. The `<style>` block is intentionally inline so there's no separate `static/` folder to manage.
- **[index.html](templates/index.html)** — upload form + recent screens panel.
- **[review.html](templates/review.html)** — the heart of the v1 UX.
  - **Form structure is non-trivial**: the page uses HTML5's `form="form-generate"` attribute pattern. There's an empty `<form id="form-generate">` whose action is `/generate`; every per-field input/select carries `form="form-generate"`. The Excel-upload sub-form is a separate sibling form. **Don't nest the upload form inside the generate form** — HTML forbids nested `<form>` elements; browsers silently merge them, so an Upload click would actually submit to /generate (we hit this exact bug; the fix is the attribute pattern).
  - JS toggles UI by `workflow_mode`: `bulk_load` shows the Excel panel and hides the per-field tables (`#field-blocks`). Wrapped in `DOMContentLoaded` because `#field-blocks` appears later in the document than the inline script.
  - Per-field widgets and "From Excel column" option are documented in the file's `field_row` macro.
- **[screen.html](templates/screen.html)** — screen detail. Two execution-button states: unverified (Claude Code primary, deterministic locked) vs. verified (deterministic primary, Re-verify ghost button). Verified pill + Mark unverified link. Recent runs table. Generated CLAUDE.md preview + download.
- **[run.html](templates/run.html)** — live run page. Subscribes to a Server-Sent Events stream of stream-json output and renders each event (assistant text / tool_use / tool_result / system / result) as a row. Polls `/runs/<id>/screenshots-list` every 3s to refresh the gallery. Top of page shows a green "Verify & save recipe?" prompt when the run is a successful Claude Code run on an unverified screen.
- **[screens.html](templates/screens.html)** — history list with delete + meta.yaml download per row.

## Working on this codebase

- **The deterministic-generation pipeline is the product.** Resist requests to "just have the LLM compose it" — the whole point is reproducible diffs across patch upgrades. If a feature genuinely needs LLM creativity, call it from a separate, opt-in path; don't put it on the critical path.

- **Adding a new UIXML dialect** → extend the tag-name sets at the top of [flexcube_uixml_parser.py](flexcube_uixml_parser.py) (`FIELD_TAGS`, `BLOCK_TAGS`, etc.). If field properties are encoded differently, prefer extending `_attr_or_child`'s name list over rewriting `_parse_field`.

- **Adding a new datatype** → branch in `_field_action_lines` in [claude_md_generator.py](claude_md_generator.py) AND `_field_steps` in [plan_compiler.py](plan_compiler.py) (so both runners handle it). Add a case in [review.html](templates/review.html)'s `field_row` macro for the right widget. Add Excel-formatting in `_apply_column_format` and `_format_hint` in [excel_handler.py](excel_handler.py). Update both `_materialise_for_row` functions (one in claude_md_generator, one in plan_compiler) symmetrically. **The widget's name conventions are `mode_<block>__<field>` and `value_<block>__<field>` — keep them so `parse_decisions_from_form` doesn't need to change.**

- **Adding a new workflow mode** (Copy-Existing / Modify):
  1. Enable it in `WORKFLOW_MODES` and add to `ENABLED_MODES` in [review.html](templates/review.html).
  2. Add a branch in `generate_claude_md` AND `compile_plan` to compose the right body / step list.
  3. Keep the deterministic guarantee: same inputs → same markdown.

- **Adding a column to a `screens`-row field** → add it to `_RUNTIME_COLUMNS` in [db.py](db.py); old DBs get migrated transparently. Update `list_screens`'s SELECT clause if the column should surface in history views.

- **Adding a new table** → add the `CREATE TABLE IF NOT EXISTS` to the `SCHEMA` constant in [db.py](db.py); it'll be created on every connection.

- **Adding a new recipe override** → new key in the recipe dict shape (document in `db._RUNTIME_COLUMNS` comment) + new regex/extraction in [recipe_extractor.py](recipe_extractor.py) + new branch in the relevant `flexcube_selectors` helper that reads `recipe.get(...)` for an override.

- **Don't put state in module-level globals.** All durable state lives in SQLite; `runner._LIVE` is the one exception (process registry for live Stop), and it's documented as resetting on Flask reload.

- **Sample fixtures live in [samples/](samples/).** Real FLEXCUBE artifacts the parsers and runners are battle-tested against, plus the team's hand-written reference plan, plus the Claude Code run log (`SAMPLE_TEST_THROUGH_CLAUDE_CODE.txt`) the deterministic runner's selectors were calibrated from.

## Roadmap

The items below are deliberately not in v1. Listed in rough priority order:

1. **Copy-Existing workflow** — query existing record, click Copy, override key, save.
2. **Modify workflow** — query, Unlock, edit, save.
3. **Maker-checker dual-session execution.** Today the runner stops after the maker save (rule #3 in `runner.build_wrapper_prompt`). Should launch a second Chromium as the checker user and complete the authorize step in one run. Needs the Playwright MCP server to support multiple browser contexts, or two separate MCP server invocations.
4. **Bulk-load: grid blocks** — multi-sheet templates (one sheet per grid), key-column to group rows. Compiler's `_compile_bulk_load` and generator's `_generate_bulk_load` currently emit a `todo` step / TODO comment for grid blocks.
5. **Negative & edge test cases** — append a `## Test Cases` section. The deterministic test-case generator that lived at `test_case_synthesiser.py` was removed in early cleanup; restore from git history when re-implementing.
6. **Two-version diff** — pick two uploads of the same screen, render a side-by-side change report. This is the killer use case for patch upgrades.
7. **Multi-step `<HEADER>`-tab screens** — some screens have a real header tab with fields. Today the `SKIP_IF_UNDER` filter would skip them. Reconsider the filter when we hit one.
8. **Run supervisor that survives Flask reloads.** Today `runner._LIVE` is in-memory; if Flask debug-reloads (e.g. you edit a `.py`), the Stop button can't reach already-running subprocesses. Could persist PIDs in the DB and use psutil.

## What's intentionally not here

- No `tests/` directory. The pipeline has no unit tests; smoke-testing is via Playwright against a live Flask. If you add tests, put them under `tests/` and follow `pytest` conventions.
- No CI config. Add when the team is ready to enforce something on PRs.
- No Dockerfile. `python app.py` is the runbook; the runner subprocess needs a real Chrome browser on the host anyway, so containerising is more work than it's worth right now.
- No `static/` folder — base.html inlines its CSS.
- No `anthropic` Python package — execution uses the user's Claude Code subscription via subprocess, not the API. If a future workflow does need the API, add the dep behind a feature flag rather than making the deterministic generation depend on it.
