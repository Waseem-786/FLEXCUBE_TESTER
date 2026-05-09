# FLEXCUBE Screen Tester

A small Flask web app that turns an Oracle FLEXCUBE screen's `UIXML` + `js`
files into a ready-to-run **CLAUDE.md** automation plan, then optionally
**executes that plan** against a real FLEXCUBE — driving Chromium via either
Claude Code (the LLM agent) or a deterministic Python Playwright runner.

> **Status**: end-to-end working for **Create New** and **Bulk Load from Excel**
> on both UIXML dialects (attribute-based and the real FLEXCUBE child-element
> export). Both runners are live (LLM agent + selectors-from-a-verified-run).
> **Multi-row grids** in both runners + bulk-load Excel template (one sheet
> per grid; multi-grid disambiguation via grid-index targeting). **Custom
> in-screen action buttons** (`<TYPE>BUTTON</TYPE>` like Submit / Calculation)
> render as opt-in click prompts, interleaved at their UIXML position in the
> generated plan. **Read-only fields are eliminated** from form, Excel, and
> plan — auto-populated by FLEXCUBE. **Settings** are persisted to a shared
> **MongoDB** cluster and gate uploads with a properly-styled modal until
> credentials are configured. **Modern Linear/Vercel-style design system**
> with refined dark theme, gradient primary buttons, soft elevation, smooth
> focus rings, and motion calibrated to feel responsive without being flashy.
> Copy / Modify workflows and the maker-checker authorize half of execution
> are roadmap.

---

## Why

Hand-writing a regression test plan for a new FLEXCUBE screen takes hours of
careful reading of UIXML and JS. After every patch upgrade, it's the same
job again. This tool reduces it to: **upload two files → fill a form (or
upload one Excel) → click Generate → click Run.**

The pipeline is deterministic up to plan generation — same inputs always
produce the same plan, so diffing two runs across a patch upgrade gives a
clean change report.

---

## Quick start

Python 3.10+ and a MongoDB cluster (Atlas free tier works fine).

```powershell
python -m pip install -r requirements.txt
copy .env.example .env
# Edit .env: set MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/
python app.py
# open http://127.0.0.1:5050
```

The app stores **everything** (parsed screens, generated plans, runs, saved
settings) in MongoDB. The first connection ensures indexes lazily; no
schema/migration step required.

For plan **execution** you also need:

```powershell
# Claude Code CLI (used by the LLM-agent runner)
npm install -g @anthropic-ai/claude-code

# Playwright Chromium (used by the deterministic runner). Real Chrome
# is preferred — the deterministic runner tries channel="chrome" first
# (FCJNeoWeb's accessibility tree exposes Fast Path differently in
# real Chrome vs. bundled Chromium). Edge and bundled Chromium are
# automatic fallbacks.
python -m playwright install chromium
```

Open the app, click **Settings** (top-right) and enter the FLEXCUBE
**Base URL / User ID / Password** — these persist into the MongoDB `kv`
collection. The home page won't let you upload a screen until these are
filled (a styled modal pops up with a direct link to Settings if you try).
A `.env` file with the same `FLEXCUBE_*` keys is still honoured as a
fallback if Settings hasn't been populated.

The Playwright MCP server is already declared in [.mcp.json](.mcp.json) at
the project root, so no `claude mcp add` step is needed.

### Migrating from the SQLite era

If you have an existing `screens.db` from a previous version, copy its
data into Mongo with one command:

```powershell
python migrate_sqlite_to_mongo.py
# verify in Compass / the app, then once happy:
Remove-Item screens.db
```

The script is idempotent (upserts by `function_id`); safe to re-run.

---

## How to use it

### 1. Upload a screen

Provide a screen name, the UIXML file, and (optionally) the JS file.
The function ID is auto-detected from the file or the filename.

### 2. Pick a workflow mode

On the review page, choose:

| Mode | What it generates |
|---|---|
| **Create New** | One record. Per-field action: enter value, select via LOV, tick/untick checkbox, pick dropdown option, today's date, or skip. |
| **Bulk Load from Excel** | N records via an Excel file. Auto-derives one column per non-readonly field; you fill in the cells (blank cells skip), then upload. Type-aware columns — DATE → date format, CHECKBOX → Yes/No dropdown, DROPDOWN → options dropdown, NUMBER → numeric, LOV → free text. |
| Copy Existing  | (roadmap) |
| Modify Existing | (roadmap) |

### 3. Fill in values

**Create New** — type values into the per-field widgets (text input, date
picker, checkbox state, dropdown of parsed options, "row matching X" for
LOVs). Required fields can't be skipped; **read-only fields are dropped
entirely** (FLEXCUBE auto-populates them).

**Bulk Load** — click *Download template*, fill in your data offline,
*Upload* the filled file. The page shows ✓ uploaded · N rows once it lands.
The template is **multi-sheet** when the screen has editable grid blocks:
a `Master` sheet for the master block plus one sheet per grid. Grid sheets
lead with a `MASTER_KEY` column — leave it blank to broadcast the row to
every master, or fill `1` / `2` / `3` to attach a grid row to a specific
master record (1-based row index of the Master sheet). The master sheet
also gets a `Press_<NAME>` Yes/No column for each custom in-screen action
button on the screen, so each row decides whether to click that button.

**Grids in Create New** — editable grids render as a mini-table with
`+ Add row` / `× remove` buttons. Read-only-only grids (e.g. a History
panel auto-populated by FLEXCUBE) are omitted from the form, the plan,
and the Excel template.

**In-screen actions** — when a screen declares custom buttons in UIXML
(e.g. SUBMIT, Calculation), the review page shows an **In-screen actions**
panel with a styled checkbox card per button. The generated plan emits a
"Click **&lt;Label&gt;**" step at the **button's natural UIXML position
within its parent block** — so a Submit button declared at the end of
Block A clicks at the end of Block A, not lumped before Save.

### 4. Generate

Click *Generate plan →*. The plan composes deterministically — no LLM in
the generation path. You land on the screen detail page with the rendered
plan + download button + meta.yaml.

### 5. Run the plan (optional)

The detail page has two execution paths:

| Mode | How | Speed | Cost | Use when |
|---|---|---|---|---|
| **Run plan (Claude Code)** | LLM agent + Playwright MCP server. Reads the markdown, drives Chromium, adapts. | ~5–15 min | Subscription quota (~$0.30–$0.80 / run) | First time on a screen / FLEXCUBE deployment, or after a patch where selectors might have shifted |
| **Run plan (deterministic)** | Compiled structured plan + Playwright sync API. Selectors derived from a verified run. | ~30–60 s | Free | Stable, repeated runs after a screen has been "verified" |

The deterministic button is **locked** until the screen has been verified.
Verification: after a successful Claude Code run, click **Verify & save
recipe** on the run page. The app parses the run log and saves both:

- **Selector overrides** — checkbox click strategies, LOV iframe titles,
  the screen iframe's numeric name. The deterministic runner's selector
  helpers consult these per-call.
- **Step recordings** — a structured form of every Playwright action the
  agent ran, grouped by step title. The compiler swaps any title outside
  its typed-step set (Login / Fast Path / Save / field fills / grid Add Row
  …) for a single `replay_step` so unanticipated detours the agent took
  (a confirmation popup, a tab switch) are reproduced verbatim. Typed
  boilerplate keeps using its multi-strategy selectors so the runner stays
  resilient to cross-session DOM variance.

The browser opens **headed** in both cases — you watch it work, with a
Stop button live in the UI.

---

## What gets parsed

From **UIXML** (both dialects):
- Every field — name, label, datatype, length, precision, required, readonly, hidden, default.
- Discrete `<OPTION>` values for SELECT/RADIO fields.
- LOV references — both attribute-style (`Lov="..."`) and nested (`<LOV><NAME>...</NAME></LOV>`).
- Block / grid structure (single-entry FLDSET vs. multi-entry grid).
- Buttons declared on the screen, plus the standard FLEXCUBE button set
  (`New / Save / Enter Query / Execute Query / Unlock / Authorize / Copy / Close`)
  injected so downstream code can rely on them existing.
- Function ID — from `FunctionId` attribute, or descendant `FunctionId`, or
  the uploaded filename (`IADPRFNL.xml` → `IADPRFNL`).

From **JS**:
- `onChange` / `onValidate` / `onLoad` handlers attached to fields.
- Cross-field dependencies (`reads / writes / enables / disables / shows / hides`).
- Inferred validation rules (regex literals, length checks, empty/null guards).

These signals are written into a `meta.yaml` you can also download separately.

## What gets filtered out — generically

A block or tab is excluded from the parsed model if any of its XML ancestors is
`<SUMMARY>`, `<HEADER>`, or `<FOOTER>`. That generically removes the FLEXCUBE
chrome a tester doesn't manually fill on the main form:

| Ancestor | Contents | Why we skip |
|---|---|---|
| `<SUMMARY>` | `SUMBLOCK TABPAGE="QUERY"` and `"RESULT"` | Search / list view |
| `<FOOTER>` | `FLDSET ID="FLD_AUDIT*"` (Maker, Checker, AuthStatus, Mod No, …) | Maker-checker chrome |
| `<HEADER>` | Empty structural wrapper | No real fields |

The attribute-based dialect declares blocks at the root with no
SUMMARY/HEADER/FOOTER wrappers, so the filter is a no-op there — backward
compatibility preserved.

---

## Output

A markdown file matching the team's house style — see
[samples/CLAUDE.md](samples/CLAUDE.md) for the hand-written reference plan
that v1 was modelled on. Every generated plan has:

- `## Configuration` block with the standard 6 keys (`base_url`, `username`,
  `password`, `screen_id`, `accorder_auth_username`, `accorder_auth_password`).
- Login → post-login popup → navigate to function ID.
- For **Create New**: one numbered step per non-grid block, one bullet per field action.
- For **Bulk Load**: one numbered step per Excel row, with Process row N of M
  headings showing a row identifier (e.g. `GL Code = 000093633`).
- Save → Override-popup → Accept → success-confirm.
- Authorization Status check + maker-checker round-trip.
- `## Technical Requirements` footer.

Field actions render naturally per type:

```markdown
- Click the **LOV button** next to the **Fund Id** (FUND_ID) field.
- In the LOV popup, click **Fetch**.
- Find and click the row matching `FND001`.

- Enter `FUND` into the **Pool Group Code** (POOLGROUPCODE) field.

- In the **Equity Base** (EQUITY_BASE) dropdown, select `M`.

- **Tick** the **Profit Calculation Required** (PROFIT_CALC_REQ) checkbox.

- Enter today's date into the **Fund Start Date** (START_DATE) field.
```

Required fields with no value emit a `<!-- TODO: required field X has no value -->`
comment so nothing silently goes missing.

---

## Persistence

Everything lives in MongoDB — `screens` (with embedded blocks / fields /
buttons / dependencies / validations / decisions), `runs`, `kv` (Settings),
and a `counters` collection that mints sequential integer IDs so URLs like
`/screens/4` and `/runs/19` keep working. The home page shows recent
uploads with verified-state badges so persistence is visible immediately.

`screens.db` (the legacy SQLite file) is no longer used by the app — if
yours still exists from a previous version, run
`python migrate_sqlite_to_mongo.py` to copy its data into Mongo, then
delete the file.

---

## Project layout

```
.
├── app.py                       Flask routes
├── mongo_db.py                  MongoDB persistence (screens / runs / kv /
│                                counters). Drop-in replacement for the
│                                v1 SQLite layer; same public function
│                                names and signatures.
├── migrate_sqlite_to_mongo.py   One-shot copy of legacy screens.db → Mongo.
│                                Idempotent; safe to re-run.
├── runner.py                    Subprocess manager: Claude Code (LLM) +
│                                deterministic runner. Same SSE log page,
│                                same Stop button.
├── deterministic_runner.py      Standalone Playwright sync runner. Reads a
│                                compiled plan + recipe, emits stream-json
│                                events to stdout.
├── flexcube_selectors.py        Per-deployment selector profile derived from
│                                a real successful Claude Code run. The only
│                                file to touch when FLEXCUBE's UI shifts.
├── flexcube_uixml_parser.py     UIXML → ScreenModel (handles both dialects)
├── flexcube_js_parser.py        JS → JSAnalysisResult
├── meta_generator.py            (screen, js) → meta.yaml
├── claude_md_generator.py       (screen, decisions, mode, excel_rows) → CLAUDE.md
├── plan_compiler.py             (screen, decisions, mode, excel_rows) →
│                                structured step list for the deterministic runner
├── recipe_extractor.py          Parse a stream-json log → selector overrides +
│                                step_recordings (structured Playwright actions
│                                the deterministic runner can replay)
├── excel_handler.py             Multi-sheet template I/O — Master sheet plus
│                                one sheet per editable grid block, MASTER_KEY
│                                column to join grid rows to master rows.
├── templates/
│   ├── base.html                Layout + dark-theme CSS
│   ├── index.html               Upload form + recent screens
│   ├── review.html              Per-field review (Create New + Bulk Load UIs)
│   ├── screen.html              Detail + generated plan + Run buttons + run history
│   ├── run.html                 Live run page (SSE event stream + screenshots)
│   ├── screens.html             History
│   └── settings.html            Runtime credentials form (FLEXCUBE_* values
│                                persist into the kv table; canonical for
│                                runtime_config())
├── samples/                     Real FLEXCUBE artifacts to test against
│   ├── IADPRFNL.xml/.js         Real FLEXCUBE child-element dialect
│   ├── IADPBALO.xml/.js         Real FLEXCUBE — has a SELECT dropdown field
│   ├── IADSKINP.xml/.js         Real FLEXCUBE child-element dialect
│   ├── IADASFNL.xml/.js         Real FLEXCUBE — has an editable grid block
│   ├── IADADHPL.xml/.js         Real FLEXCUBE — has multi-grid (Asset + Liab),
│   │                            custom in-screen buttons (Submit / Calculation),
│   │                            and SELECT dropdowns with parsed options
│   ├── CLAUDE.md                Team's hand-written reference plan
│   └── SAMPLE_TEST_THROUGH_CLAUDE_CODE.txt
│                                The Claude Code run log the deterministic
│                                runner's selectors were calibrated against.
├── .mcp.json                    Project-shipped MCP server config (Playwright)
├── .env.example                 Template for runtime credentials (copy to .env)
├── requirements.txt
├── README.md
├── CLAUDE.md                    Repo guidance for future Claude Code instances
└── .gitignore
```

---

## Roadmap

In rough order:

1. **Copy-Existing workflow** — query existing record, click Copy, override key, save.
2. **Modify workflow** — query, Unlock, edit, save.
3. **Maker-checker dual-session execution** — currently the runner stops after
   the maker save; should launch a second Chromium as the checker user and
   complete the authorize step in one run.
4. **Replay value-substitution** — v1 of `replay_step` only swaps placeholder
   credentials. Future: pair each recorded step with the original plan's args
   so a Bulk Load row can replay an exact recorded sequence with the new
   row's value substituted for the recorded one.
5. **Negative & edge test cases** — append a `## Test Cases` section to generated plans.
6. **Two-version diff** — pick two uploads of the same screen and render a
   side-by-side change report (the killer use case for patch upgrades).

---

## Architecture in one sentence

UIXML/JS go in → deterministic parsers extract a structured model → it's
stored in SQLite → a per-field review form (or an Excel upload for bulk
mode) collects the test inputs → a deterministic Python composer builds the
markdown plan → an LLM agent or a calibrated deterministic runner executes
it against a real browser. See [CLAUDE.md](CLAUDE.md) for the detailed
mental model.
