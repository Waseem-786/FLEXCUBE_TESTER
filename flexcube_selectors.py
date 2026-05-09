"""
flexcube_selectors.py
=====================

Selector + frame-navigation profile for FLEXCUBE FCJNeoWeb (the Oracle Forms
front-end the team's deployment uses). Every selector here was extracted
from a real successful run captured in
`SAMPLE_TEST_THROUGH_CLAUDE_CODE.txt` against IADSKINP.

The deterministic runner ([deterministic_runner.py](deterministic_runner.py))
reads from this module instead of hard-coding selectors so per-deployment
tweaks live in one file.

Three things to know about the FLEXCUBE DOM:

1. **iframe nesting is everywhere.**
     • Top-level page = login + Fast Path bar.
     • After Fast Path → screen content lives in `iframe[name="<numeric>"]`
       where the number is per-session (`"20844"` was one example).
     • LOVs and info popups open as further-nested iframes by `title`.

2. **Most things use accessibility roles, not CSS.** Buttons exposed as
   `<a>` links by FCJNeoWeb (Save, New, Authorize), real `<button>` for
   Sign In / Fetch / Go / popup OK. We use Playwright's
   `get_by_role(role, name=...)` which abstracts both.

3. **Multiple "List of Values" buttons per screen** are disambiguated only by
   position. The parsed `ScreenModel` preserves declaration order from the
   UIXML, which matches on-screen render order, so positional indexing is
   actually safe — but document it as fragile so a future contributor knows
   why this layer exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Playwright is only imported by the runner; keep this module
    # importable from environments without playwright installed.
    from playwright.sync_api import Frame, FrameLocator, Locator, Page


# ---------------------------------------------------------------------------
# Top-level page (login + Fast Path)
# ---------------------------------------------------------------------------

LOGIN_USERNAME_ROLE = ("textbox", "User ID")
LOGIN_PASSWORD_ROLE = ("textbox", "Password")
LOGIN_SUBMIT_ROLE   = ("button",  "Sign In")

FAST_PATH_ROLE      = ("combobox", "Fast Path")  # canonical (real Chrome accessibility tree)
FAST_PATH_GO_ROLE   = ("button",   "Go")


# ---------------------------------------------------------------------------
# Information / message popups (login info + Save success)
# ---------------------------------------------------------------------------

INFO_POPUP_IFRAME_TITLE = "Information Message"
INFO_POPUP_OK_ROLE      = ("button", "Ok")


# ---------------------------------------------------------------------------
# Standard FLEXCUBE screen-toolbar actions (live inside the screen iframe)
# ---------------------------------------------------------------------------

# All FCJNeoWeb toolbar items render as <a> links, not <button>.
SCREEN_ACTIONS_ROLE: dict[str, tuple[str, str]] = {
    "NEW":            ("link", "New"),
    "SAVE":           ("link", "Save"),
    "ENTER_QUERY":    ("link", "Enter Query"),
    "EXECUTE_QUERY":  ("link", "Execute Query"),
    "UNLOCK":         ("link", "Unlock"),
    "AUTHORIZE":      ("link", "Authorize"),
    "COPY":           ("link", "Copy"),
    "CLOSE":          ("link", "Close"),
    "DELETE":         ("link", "Delete"),
}


# ---------------------------------------------------------------------------
# LOV (List Of Values) popups
# ---------------------------------------------------------------------------

LOV_BUTTON_ROLE         = ("button", "List of Values")
LOV_POPUP_TITLE_PREFIX  = "List of Values "  # full title = prefix + field label
LOV_FETCH_ROLE          = ("button", "Fetch")
# Result row: getByRole('link', name=<value>) inside the popup frame.


# ---------------------------------------------------------------------------
# Frame navigation helpers
# ---------------------------------------------------------------------------

def fast_path_locator(page: "Page", timeout_per_attempt_ms: int = 8000) -> "Locator":
    """Resolve the Fast Path screen-id input. The successful calibration
    run had it as `combobox[name="Fast Path"]` in real Chrome, but FCJNeoWeb
    sometimes exposes it as a textbox or only via the underlying input id
    `FUNCID`. We try the strategies in priority order and return the first
    that becomes visible within the per-attempt timeout."""
    candidates = [
        page.get_by_role("combobox", name="Fast Path"),
        page.get_by_role("textbox",  name="Fast Path"),
        page.get_by_placeholder("Fast Path"),
        page.locator("input[id*='FUNCID' i]"),
        page.locator("input[name*='FUNCID' i]"),
    ]
    last_err: Exception | None = None
    for loc in candidates:
        try:
            loc.first.wait_for(state="visible", timeout=timeout_per_attempt_ms)
            return loc.first
        except Exception as exc:
            last_err = exc
    raise RuntimeError(
        f"Fast Path control not found by any known strategy "
        f"(combobox/textbox by name 'Fast Path', placeholder, or input id "
        f"FUNCID). Last error: {last_err}"
    )


def fast_path_go_locator(page: "Page", timeout_per_attempt_ms: int = 5000) -> "Locator":
    """Resolve the Go button next to Fast Path. Same multi-strategy pattern."""
    candidates = [
        page.get_by_role("button", name="Go"),
        page.get_by_role("link",   name="Go"),
        page.locator("input[type='submit'][value*='Go' i]"),
    ]
    last_err: Exception | None = None
    for loc in candidates:
        try:
            loc.first.wait_for(state="visible", timeout=timeout_per_attempt_ms)
            return loc.first
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"Fast Path Go button not found. Last error: {last_err}")


def screen_frame(page: "Page", *, name: str | None = None) -> "FrameLocator":
    """Return a FrameLocator for the screen iframe.

    Two modes:
      • `name` provided → pin to that specific `iframe[name="<value>"]`.
        Use this once we've discovered the screen's numeric name attr at
        fast_path time. The pinned locator is stable even after LOV
        popups (also iframes with names) open later in the run.
      • `name` omitted → fall back to "the last visible named iframe",
        which works when the screen is open and no LOV popup has appeared
        yet.

    The dynamic-fallback path was load-bearing for the original IADSKINP
    run but breaks the moment a second named iframe (the LOV popup)
    becomes visible: `.last` then returns the LOV popup, shadowing the
    screen. Pinning by the discovered `name` is what stops that.
    """
    if name:
        return page.frame_locator(f'iframe[name="{name}"]')
    return page.frame_locator("iframe[name]:not([name='']):visible").last


def discover_screen_iframe_name(page: "Page") -> str | None:
    """Find the `name` attribute of the screen iframe (the most-recently-
    added visible iframe with a non-empty name). Returns None if no
    candidate is found — callers should treat that as a fast_path failure."""
    names = page.evaluate(
        "Array.from(document.querySelectorAll('iframe[name]:not([name=\"\"])'))"
        ".filter(f => f.offsetParent !== null).map(f => f.name)"
    )
    return names[-1] if names else None


def lov_popup_frame(
    parent: "FrameLocator",
    field_label: str,
    recipe: dict | None = None,
) -> "FrameLocator":
    """LOV popups nest inside the screen iframe. The popup iframe carries
    `title="List of Values <FieldLabel>"` by default. If a verified recipe
    overrides the title for this field (some screens use a non-standard
    title), use that exactly."""
    if recipe:
        override = (recipe.get("lov_popup_titles") or {}).get(field_label)
        if override:
            return parent.frame_locator(f'iframe[title="{override}"]')
    return parent.frame_locator(f'iframe[title="{LOV_POPUP_TITLE_PREFIX}{field_label}"]')


def info_popup_frame(parent: "FrameLocator | Page") -> "FrameLocator":
    """The info-message popup. Appears at top-level after login, and nested
    inside the screen iframe after Save success. The runner picks the
    correct parent before calling this."""
    return parent.frame_locator(f'iframe[title="{INFO_POPUP_IFRAME_TITLE}"]')


# ---------------------------------------------------------------------------
# Field locators inside the screen frame
# ---------------------------------------------------------------------------

def field_textbox(frame: "FrameLocator", label: str) -> "Locator":
    """A text/number/date input addressed by its visible label.
    Works for: TEXT, VARCHAR2, NUMBER, DATE inputs in FCJNeoWeb."""
    return frame.get_by_role("textbox", name=label)


def lov_button_for_field(frame: "FrameLocator", lov_index: int) -> "Locator":
    """Returns the LOV button at the given index (0-based) among all LOV
    buttons on the screen. The agent's run used `.first()` / `.nth(1)` to
    target the 1st and 2nd LOV-bound fields by declaration order; we do the
    same here.

    Caveat: if the user reorders fields in the UIXML between runs, the
    index changes. For the use cases we're targeting (one screen, stable
    UIXML across runs), this is fine.
    """
    role, name = LOV_BUTTON_ROLE
    return frame.get_by_role(role, name=name).nth(lov_index)


def checkbox_target(
    frame: "FrameLocator",
    label: str,
    recipe: dict | None = None,
) -> tuple["Locator", str]:
    """Returns (locator, strategy) for a checkbox. Strategy is one of:
        'label_click'  — click the visible label text (default; the only
                         strategy validated against the team's deployment).
        'input_click'  — click the underlying <input>. Recorded if a recipe
                         has explicitly seen this work for this label.

    The agent's run on IADSKINP showed `<input>` clicks timing out and
    label-clicks succeeding, so the default is label_click; recipes can
    override per label if a future screen needs the input strategy."""
    strategy = "label_click"
    if recipe:
        strategy = (recipe.get("checkbox_strategy") or {}).get(label, strategy)
    if strategy == "input_click":
        return (frame.get_by_role("checkbox", name=label), strategy)
    return (frame.get_by_text(label, exact=True), strategy)


def dropdown_select(frame: "FrameLocator", label: str) -> "Locator":
    """Native HTML select rendered by FCJNeoWeb. The accessible name is the
    visible label. Use Playwright's `select_option` to choose by value."""
    return frame.get_by_role("combobox", name=label)


def grid_add_row_button(frame: "FrameLocator",
                        grid_index: int | None = None,
                        timeout_per_attempt_ms: int = 5000) -> "Locator":
    """Find the + (Add Row) button for a grid.

    On screens with **multiple editable grids** (e.g. IADADHPL has both
    an Asset grid and a Borrower / Liability grid), every grid has its
    own visually-identical `+` button. Without a positional hint we'd
    just pick `.last` and consistently click the wrong grid's button.

    Pass `grid_index` (0-based among editable grids in UIXML / DOM order)
    so the selector targets `.nth(grid_index)` on the resolved candidate
    set. When `grid_index` is None we fall back to `.last` — fine for
    single-grid screens.

    Strategies tried in priority order: `<button>` named +, "Add Row",
    "Add", then CSS / aria-label variants for `<a>` and icon-only buttons.
    """
    candidates = [
        frame.get_by_role("button", name="+"),
        frame.get_by_role("button", name="Add Row"),
        frame.get_by_role("button", name="Add"),
        frame.locator('button[title="Add Row"]'),
        frame.locator('button[title="Add"]'),
        frame.locator('a[title="Add Row"]'),
        frame.locator('a[title="Add"]'),
        frame.locator('[aria-label="Add Row"]'),
        frame.locator('[aria-label="Add"]'),
    ]
    last_err: Exception | None = None
    for loc in candidates:
        target = loc.nth(grid_index) if isinstance(grid_index, int) else loc.last
        try:
            target.wait_for(state="visible", timeout=timeout_per_attempt_ms)
            return target
        except Exception as exc:
            last_err = exc
    raise RuntimeError(
        f"Grid Add Row button not found by any known strategy "
        f"(grid_index={grid_index!r}; tried button: +, Add Row, Add; "
        f"same as link/aria-label). Last error: {last_err}"
    )


def grid_field_in_last_row(frame: "FrameLocator", label: str,
                           datatype: str | None = None) -> "Locator":
    """A grid cell input in the most recently added row. We use `.last`
    because + Add Row appends to the bottom and the new cell becomes the
    last accessible-name match for that label."""
    if (datatype or "").upper() == "DROPDOWN":
        return frame.get_by_role("combobox", name=label).last
    return frame.get_by_role("textbox", name=label).last


def grid_cell_focus(frame: "FrameLocator", page, label: str,
                    timeout_per_attempt_ms: int = 2500) -> "Locator":
    """Mount + focus a grid cell's lazy `<input>` and return it.

    Why this is needed: FCJNeoWeb grids render cells as styled `<td>` text
    by default. The editable `<input>` is mounted *only* while the cell
    has focus, and unmounted on blur. So:
      - `getByRole('textbox', name=label)` doesn't match — the input
        isn't in the DOM yet.
      - `getByRole('cell', name=label)` doesn't match either — the ARIA
        name of a cell is its *content* (empty for unfilled cells), not
        the column header.
      - `getByText(label)` matches the column header `<th>` text.
        Clicking the header does NOT mount the row's input.

    Reliable approach: locate the column **header** (which DOES carry the
    label as accessible name) to get its X bounds, then click in the last
    row's Y band at that X. That precisely hits the visible cell area
    and triggers FCJNeoWeb to mount + focus the input.

    Returns the now-mounted input Locator. Callers can `.fill()` it or
    `keyboard.type()` after — both work once the input is in the DOM.
    """
    input_loc = frame.get_by_role("textbox", name=label).last

    # Path 1: already mounted (e.g. auto-focused right after grid_add_row,
    # or focus arrived here by Tab from the previous cell). Just click to
    # be sure focus is on it, then return.
    try:
        input_loc.wait_for(state="visible", timeout=600)
        input_loc.click(timeout=1000)
        return input_loc
    except Exception:
        pass

    # Path 2: header X × last-row Y click. Try several header selectors
    # in priority order; first one whose box we can resolve wins.
    header_candidates = [
        frame.get_by_role("columnheader", name=label).last,
        frame.locator("th").filter(has_text=label).last,
        frame.get_by_text(label, exact=True).first,
    ]
    last_err: Exception | None = None
    for header in header_candidates:
        try:
            header.wait_for(state="visible", timeout=timeout_per_attempt_ms)
            header_box = header.bounding_box(timeout=1000)
            if not header_box:
                continue
            # Find the table containing this header, then its last data row.
            table = frame.locator("table").filter(has=header).last
            last_row = table.locator("tbody tr").last
            try:
                last_row.wait_for(state="visible", timeout=800)
            except Exception:
                # Fallback: any tr in the table (some grids skip tbody)
                last_row = table.locator("tr").last
                last_row.wait_for(state="visible", timeout=800)
            row_box = last_row.bounding_box(timeout=1000)
            if not row_box:
                continue
            # Click viewport-relative; bounding_box is iframe-aware.
            page.mouse.click(
                header_box["x"] + header_box["width"] / 2,
                row_box["y"] + row_box["height"] / 2,
            )
            # Now wait for the textbox to actually appear in DOM.
            input_loc.wait_for(state="visible", timeout=timeout_per_attempt_ms)
            return input_loc
        except Exception as exc:
            last_err = exc
            continue

    raise RuntimeError(
        f"Grid cell {label!r} could not be focused for editing. Tried "
        f"already-mounted input, then column-header X × last-row Y click. "
        f"Last error: {last_err}"
    )


def grid_lov_button_last(frame: "FrameLocator") -> "Locator":
    """LOV button in the most recently added grid row. Selects the last
    visible "List of Values" button on the screen — works because grids
    are typically the bottom-most blocks and a freshly-added row's LOV
    button is the latest one in the DOM."""
    role, name = LOV_BUTTON_ROLE
    return frame.get_by_role(role, name=name).last


def screen_button(frame: "FrameLocator", label: str,
                  timeout_per_attempt_ms: int = 4000) -> "Locator":
    """Resolve a custom in-screen action button (Submit / Calculation / etc.)
    by visible label. Multi-strategy because FCJNeoWeb renders these as
    `<button>` in some screens and `<a>` (link) elsewhere; some deployments
    expose only the input or a styled span. Falls back to clickable text
    last so the failure message still names the locator. Per-screen recipe
    overrides aren't needed here — the label IS the public surface."""
    candidates = [
        frame.get_by_role("button", name=label, exact=True),
        frame.get_by_role("button", name=label),
        frame.get_by_role("link",   name=label, exact=True),
        frame.locator(f'input[type="button"][value="{label}"]'),
        frame.locator(f'input[type="submit"][value="{label}"]'),
        frame.get_by_text(label, exact=True),
    ]
    last_err: Exception | None = None
    for loc in candidates:
        try:
            loc.first.wait_for(state="visible", timeout=timeout_per_attempt_ms)
            return loc.first
        except Exception as exc:
            last_err = exc
    raise RuntimeError(
        f"In-screen button {label!r} not found by any known strategy "
        f"(button / link / input by name '{label}'). Last error: {last_err}"
    )


def link_by_value(frame: "FrameLocator", value: str,
                  timeout_per_attempt_ms: int = 3000) -> "Locator":
    """Find the LOV result row matching `value`. FCJNeoWeb usually renders
    these as `<a>` links whose accessible name is the visible value, but
    deployment variants use `<button>` for row activation, plain `<td>`
    cells, or just clickable text. Try each strategy in priority order
    and return the first that resolves to a visible element. Falls back
    to the canonical link locator so caller's `.click()` produces a
    helpful error message instead of an opaque None."""
    strategies = [
        frame.get_by_role("link",   name=value, exact=True),
        frame.get_by_role("link",   name=value),                # contains-match
        frame.get_by_role("button", name=value, exact=True),
        frame.get_by_role("cell",   name=value, exact=True),
        frame.get_by_text(value, exact=True),
    ]
    for loc in strategies:
        try:
            loc.first.wait_for(state="visible", timeout=timeout_per_attempt_ms)
            return loc.first
        except Exception:
            continue
    return strategies[0].first
