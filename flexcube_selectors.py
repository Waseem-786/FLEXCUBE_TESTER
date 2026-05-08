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
                        timeout_per_attempt_ms: int = 5000) -> "Locator":
    """Find the + (Add Row) button for a grid. FCJNeoWeb deployments vary —
    a literal `+` text button, "Add" labelled button, or an icon-only
    button/link with title="Add"/aria-label="Add". Tries strategies in
    priority order; we use `.last` because:
      • Most screens have a single editable grid (so .last == .first).
      • Multi-grid screens emit one button per grid; the LAST one is
        usually the most recently rendered, matching typical grid
        ordering. If a future screen needs a different one, recipes can
        override.
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
        try:
            loc.last.wait_for(state="visible", timeout=timeout_per_attempt_ms)
            return loc.last
        except Exception as exc:
            last_err = exc
    raise RuntimeError(
        f"Grid Add Row button not found by any known strategy "
        f"(button: +, Add Row, Add; same as link/aria-label). Last error: {last_err}"
    )


def grid_field_in_last_row(frame: "FrameLocator", label: str,
                           datatype: str | None = None) -> "Locator":
    """A grid cell input in the most recently added row. We use `.last`
    because + Add Row appends to the bottom and the new cell becomes the
    last accessible-name match for that label."""
    if (datatype or "").upper() == "DROPDOWN":
        return frame.get_by_role("combobox", name=label).last
    return frame.get_by_role("textbox", name=label).last


def grid_lov_button_last(frame: "FrameLocator") -> "Locator":
    """LOV button in the most recently added grid row. Selects the last
    visible "List of Values" button on the screen — works because grids
    are typically the bottom-most blocks and a freshly-added row's LOV
    button is the latest one in the DOM."""
    role, name = LOV_BUTTON_ROLE
    return frame.get_by_role(role, name=name).last


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
