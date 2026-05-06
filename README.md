# FLEXCUBE Screen Tester

A small Flask web app that turns an Oracle FLEXCUBE screen's `UIXML` + `js` files
into a ready-to-run **CLAUDE.md** automation plan. Upload the screen, mark each
field's value on a review form, click Generate. The output is a deterministic,
house-style markdown plan a Playwright/Selenium runner can follow step by step.

> **v1 status**: end-to-end working for the **Create New** workflow on both UIXML
> dialects we've seen (attribute-based and the real FLEXCUBE child-element export).
> Copy / Modify / Bulk-Load are stubbed in the UI as "coming soon".

---

## Why

Hand-writing a regression test plan for a new FLEXCUBE screen takes hours of careful
reading of UIXML and JS. After every patch upgrade, it's the same job again. This
tool reduces it to: **upload two files → fill a form → click Generate**.

The pipeline is deterministic — same inputs always produce the same plan, so
diffing two runs across a patch upgrade gives a clean change report.

---

## Quick start

```powershell
python -m pip install -r requirements.txt
python app.py
# open http://127.0.0.1:5000
```

Python 3.10+. SQLite is created automatically at `screens.db` on first run.

---

## How to use it

1. **Upload** — provide a screen name, the UIXML file, and (optionally) the JS file.
   The function ID is auto-detected from the file or filename.

2. **Review fields** — the app parses the screen and shows one row per field with a
   widget that matches its type:

   | Field type | Widget |
   |---|---|
   | Text / VARCHAR2 | text input + `[Skip]` |
   | Number | number input + `[Skip]` |
   | Date | date picker + `[Today]` shortcut + `[Skip]` |
   | Checkbox | `Tick` / `Untick` / `Skip` |
   | Dropdown | `<select>` populated from the parsed `<OPTION>` values |
   | LOV-bound | "select row matching:" text input |
   | Required | `Skip` removed — must have a value |
   | Read-only | dimmed; auto-populated by FLEXCUBE so you don't drive it |

3. **Generate** — click the button. The app composes a CLAUDE.md and lands you on
   the screen detail page with the rendered plan plus a download button.

4. **Hand the CLAUDE.md to your runner** — Playwright, Selenium, or paste it into a
   Claude chat to expand into runnable code.

Re-uploads of the same screen go through the review form again; prior decisions
are pre-filled so you only edit what changed.

---

## What gets parsed

From **UIXML** (both dialects):
- Every field — name, label, datatype, length, precision, required, readonly, hidden, default.
- Discrete `<OPTION>` values for SELECT/RADIO fields (e.g. `AUTHSTAT={A,U,R}`).
- LOV references — both attribute (`Lov="..."`) and nested (`<LOV><NAME>...</NAME></LOV>`).
- Block / grid structure (single-entry FLDSET vs. multi-entry grid).
- Buttons declared on the screen, plus the standard FLEXCUBE button set
  (`New / Save / Enter Query / Execute Query / Unlock / Authorize / Copy / Close`)
  injected so downstream code can rely on them existing.
- Function ID — from `FunctionId` attribute, or descendant `FunctionId`, or the
  uploaded filename (`IADPRFNL.xml` → `IADPRFNL`).

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
| `<FOOTER>` | `FLDSET ID="FLD_AUDIT*"` (Maker, Checker, AuthStatus, Mod No, etc.) | Maker-checker chrome |
| `<HEADER>` | Empty structural wrapper | No real fields |

The older attribute-based dialect (e.g. the `IADFNONL.UIXML` sample) declares
blocks at the root with no SUMMARY/HEADER/FOOTER wrappers, so the filter is a
no-op there — backward compatibility preserved.

---

## Output

A markdown file matching the team's house style — see `samples/CLAUDE.md` for the
hand-written reference plan that v1 was modelled on. Every generated plan has:

- `## Configuration` block with the standard 6 keys (`base_url`, `username`,
  `password`, `screen_id`, `accorder_auth_username`, `accorder_auth_password`).
- Login → post-login popup → navigate to function ID.
- `### N. Initiate New Entry` (for Create-New).
- One numbered step per non-grid block, with one bullet per field action.
- One numbered step per grid block, in Add-Row pattern with a "repeat for each
  additional input row" closer.
- Save → Override-popup → Accept → success-confirm.
- `### N. Check Authorization Status` + `### N+1. Authorize Record (second user)`
  — full maker-checker round-trip.
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
comment in the workflow body, so nothing silently goes missing.

---

## Project layout

```
.
├── app.py                       Flask app + routes
├── db.py                        SQLite schema, helpers, runtime migrations
├── flexcube_uixml_parser.py     UIXML → ScreenModel (handles both dialects)
├── flexcube_js_parser.py        JS → JSAnalysisResult (handlers, deps, validations)
├── meta_generator.py            (screen, js) → meta.yaml
├── claude_md_generator.py       (screen, decisions, mode) → CLAUDE.md
├── templates/
│   ├── base.html                Layout + dark-theme CSS
│   ├── index.html               Upload form
│   ├── review.html              Per-field review (the heart of the v1 UX)
│   ├── screen.html              Screen detail + generated plan
│   └── screens.html             History
├── samples/                     UIXML/JS for screens we tested against
│   ├── IADFNONL.UIXML/.js       Old attribute-based dialect (illustrative)
│   ├── IADPRFNL.xml/.js         Real FLEXCUBE child-element dialect
│   ├── IADPBALO.xml/.js         Real FLEXCUBE child-element dialect
│   └── CLAUDE.md                Team's hand-written reference plan
├── requirements.txt
├── README.md
├── CLAUDE.md                    Repo guidance for future Claude Code instances
└── .gitignore
```

---

## Roadmap

In rough order — none of this is in v1.

1. **Copy-Existing workflow** — query existing record, click Copy, override key, save.
2. **Modify workflow** — query, Unlock, edit, save.
3. **Excel-bulk-load** — per grid block: column-mapping form + "loop over rows" template.
4. **Negative & edge test cases** — append a `## Test Cases` section to generated plans.
5. **Two-version diff** — pick two uploads of the same screen and render a side-by-side
   change report (the killer use case for patch upgrades).

---

## Architecture in one sentence

UIXML/JS go in → deterministic parsers extract a structured model → it's stored in SQLite →
a per-field review form collects the test inputs → a deterministic Python composer builds
the markdown. No LLM call anywhere in the pipeline. See [CLAUDE.md](CLAUDE.md) for the
detailed mental model.
