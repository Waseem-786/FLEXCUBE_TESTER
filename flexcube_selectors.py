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

FAST_PATH_ROLE      = ("combobox", "Fast Path")
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

def screen_frame(page: "Page") -> "FrameLocator":
    """Return a FrameLocator for the currently-open screen iframe.

    FLEXCUBE drops each opened screen into `iframe[name="<numeric_id>"]`. The
    numeric id is allocated per-session so we can't hard-code it. We pick the
    iframe with a numeric `name` attribute — there's usually only one of
    those in the page at a time, the screen the user just navigated to.
    """
    return page.frame_locator("iframe[name]:not([name='']):visible").last


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


def link_by_value(frame: "FrameLocator", value: str) -> "Locator":
    """LOV result rows are rendered as `<a>` links whose accessible name is
    the visible value (e.g. '000093633', 'PKR'). After clicking Fetch in an
    LOV popup, we use this to pick the row matching the user's input."""
    return frame.get_by_role("link", name=value, exact=True)
