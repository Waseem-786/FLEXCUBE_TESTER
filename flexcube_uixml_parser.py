"""
flexcube_uixml_parser.py
========================

Deterministic parser for Oracle FLEXCUBE UIXML screen files.

The parser produces a Screen Model — a structured JSON-serialisable dict that
captures every field, block, grid, button, tab, and LOV reference on a screen.

Why deterministic (no AI here)?
- The UIXML structure is regular. Parsing it with code is reliable, fast, and
  free. We do NOT want an LLM to "read" XML and possibly miss fields.
- The Screen Model becomes the ground truth that downstream layers (test-case
  synthesiser, CLAUDE.md composer) consume. If the test plan has a phantom
  field, the bug is in the composer prompt, not in field discovery.

NOTE on UIXML dialects:
FLEXCUBE UIXML differs slightly between versions (FCUBS 12.x vs 14.x) and
between modules. The parser is written defensively: unknown attributes are
preserved in `extra`, and tag-name lookups are case-insensitive. If your
dialect uses different tag names (e.g. `Field` vs `FIELD` vs `FldName`),
extend the FIELD_TAGS / BLOCK_TAGS sets at the top of the file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


# ---------------------------------------------------------------------------
# Tag-name configuration. Extend these if your UIXML dialect uses other names.
# ---------------------------------------------------------------------------

FIELD_TAGS = {"FIELD", "Field", "FLD", "FieldDef"}
# BLOCK_TAGS recognises both dialects:
#   - Attribute-based: <BLOCK Name="..." Type="...">  (older / illustrative)
#   - Child-element based: <FLDSET ID="..."><BLOCK>NAME</BLOCK><FIELD>..</FIELD></FLDSET>
#     and <SUMBLOCK SCREEN="SUMMARY"><FIELD>..</FIELD></SUMBLOCK>  (real FLEXCUBE export)
BLOCK_TAGS = {"BLOCK", "Block", "BLK", "BlockDef", "FLDSET", "FldSet", "SUMBLOCK", "SumBlock"}
TAB_TAGS = {"TAB", "Tab", "TabDef"}
BUTTON_TAGS = {"BUTTON", "Button", "BTN", "Action"}
LOV_TAGS = {"LOV", "Lov", "LovDef"}
# GRID_HINT_ATTRS: values of a block's Type/BlockType that indicate a multi-row grid.
# "ME" = "Multi Entry" in FLEXCUBE FLDSET TYPE; "SE" = "Single Entry".
GRID_HINT_ATTRS = {"MULTI", "MULTIPLE", "GRID", "ME"}

# Any block or tab whose XML ancestor chain includes one of these tags is
# FLEXCUBE chrome, not part of the user-facing data-entry form, and is excluded
# from the parsed model. The downstream meta.yaml then describes only the
# fields a tester actually interacts with on the main screen.
#
#   <SUMMARY>  →  query/list summary screen (QUERY tabpage + RESULT grid)
#   <FOOTER>   →  audit / maker-checker fields (Maker, Checker, AuthStatus, etc.)
#   <HEADER>   →  structural-only header chrome
#
# The OLDER attribute-based dialect (e.g. IADFNONL.UIXML) declares blocks at
# the root with no SUMMARY/HEADER/FOOTER wrappers, so this filter is a no-op
# there — backward-compat preserved.
SKIP_IF_UNDER = {"SUMMARY", "Summary", "FOOTER", "Footer", "HEADER", "Header"}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FieldModel:
    name: str
    label: str | None = None
    datatype: str | None = None       # VARCHAR2 / NUMBER / DATE / CHECKBOX / RADIO / DROPDOWN / SELECT
    length: int | None = None         # max length for VARCHAR2/NUMBER
    precision: int | None = None      # decimal precision for NUMBER
    required: bool = False
    readonly: bool = False
    hidden: bool = False
    lov: str | None = None            # name of LOV definition the field uses
    default: str | None = None
    parent_block: str | None = None
    is_grid_column: bool = False
    options: list[dict[str, str]] = field(default_factory=list)  # SELECT/RADIO discrete values
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class BlockModel:
    name: str
    label: str | None = None
    is_grid: bool = False             # True for multi-row sections
    is_tab: bool = False
    parent_tab: str | None = None
    fields: list[FieldModel] = field(default_factory=list)


@dataclass
class ButtonModel:
    name: str
    label: str | None = None
    parent_block: str | None = None


@dataclass
class TabModel:
    name: str
    label: str | None = None


@dataclass
class ScreenModel:
    function_id: str
    title: str | None
    blocks: list[BlockModel] = field(default_factory=list)
    tabs: list[TabModel] = field(default_factory=list)
    buttons: list[ButtonModel] = field(default_factory=list)
    lovs: dict[str, dict[str, str]] = field(default_factory=dict)
    raw_root_tag: str | None = None
    parser_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def all_fields(self) -> list[FieldModel]:
        out: list[FieldModel] = []
        for b in self.blocks:
            out.extend(b.fields)
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _attr(elem: ET.Element, *names: str, default: str | None = None) -> str | None:
    """Case-insensitive attribute lookup. Returns first match across `names`."""
    if not names:
        return default
    lower_map = {k.lower(): v for k, v in elem.attrib.items()}
    for n in names:
        v = lower_map.get(n.lower())
        if v is not None:
            return v
    return default


def _attr_or_child(elem: ET.Element, *names: str, default: str | None = None) -> str | None:
    """Attribute-OR-child-text lookup (case-insensitive on both sides).

    For each name in `names` (priority order), checks the attribute first; if
    absent or empty, checks for a direct child element whose tag matches and
    returns its non-empty text. Only falls through to the next name when both
    attribute and child are missing for the current one.

    This is what lets a single _parse_field implementation cover both the
    attribute dialect (<FIELD Name="X" Label="Y"/>) and the child-element
    dialect (<FIELD><NAME>X</NAME><LBL>Y</LBL></FIELD>) without crashing
    on numeric `Id="1"` SUMBLOCK fields whose real name lives in <NAME>.
    """
    if not names:
        return default

    lower_attrs = {k.lower(): v for k, v in elem.attrib.items()}
    children_by_tag: dict[str, list[ET.Element]] = {}
    for child in elem:
        children_by_tag.setdefault(_strip_ns(child.tag).lower(), []).append(child)

    for n in names:
        nl = n.lower()
        v = lower_attrs.get(nl)
        if v is not None and v != "":
            return v
        for child in children_by_tag.get(nl, []):
            text = (child.text or "").strip()
            if text:
                return text
    return default


def _lov_from(elem: ET.Element) -> str | None:
    """LOV reference lookup. Handles both dialects:
       - Attribute: <FIELD Lov="LOV_X" .../>
       - Nested:    <FIELD><LOV><NAME>LOV_X</NAME></LOV></FIELD>
    """
    v = _attr(elem, "Lov", "LOV", "LovName")
    if v:
        return v
    for child in elem:
        if _strip_ns(child.tag).upper() != "LOV":
            continue
        # Nested <NAME> / <ID> child of LOV holds the reference name.
        for grand in child:
            if _strip_ns(grand.tag).upper() in ("NAME", "ID"):
                text = (grand.text or "").strip()
                if text:
                    return text
        # Sometimes the LOV wraps just the name as its own text.
        if child.text and child.text.strip():
            return child.text.strip()
    return None


def _to_bool(v: str | None) -> bool:
    if v is None:
        return False
    # FLEXCUBE child-element dialect uses "-1" for true (a legacy Forms idiom).
    return str(v).strip().upper() in {"Y", "YES", "TRUE", "1", "T", "-1"}


def _to_int(v: str | None) -> int | None:
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except ValueError:
        return None


def _strip_ns(tag: str) -> str:
    """Strip XML namespace, e.g. '{ns}FIELD' -> 'FIELD'."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class FlexcubeUIXMLParser:
    """
    Parses a FLEXCUBE UIXML file into a ScreenModel.

    Usage:
        parser = FlexcubeUIXMLParser()
        model = parser.parse_file("IADFNONL.UIXML")
        print(model.function_id, len(model.all_fields()))
    """

    def parse_file(self, path: str | Path) -> ScreenModel:
        tree = ET.parse(str(path))
        root = tree.getroot()
        return self.parse_root(root, filename_hint=Path(path).name)

    def parse_string(self, xml_text: str, filename_hint: str | None = None) -> ScreenModel:
        root = ET.fromstring(xml_text)
        return self.parse_root(root, filename_hint=filename_hint)

    def parse_root(self, root: ET.Element, filename_hint: str | None = None) -> ScreenModel:
        function_id = (
            _attr(root, "FunctionId", "Function", "FUNCTION_ID")
            or self._find_function_id_in_descendants(root)
            or self._function_id_from_filename(filename_hint)
            or "UNKNOWN"
        )
        # Title may live on the root, on a descendant <SCREEN TITLE="...">,
        # or on a <SUMMARY TITLE="..."> in the real FLEXCUBE export.
        title = _attr(root, "Title", "ScreenTitle", "Description")
        if not title:
            for elem in root.iter():
                tag = _strip_ns(elem.tag).upper()
                if tag in ("SCREEN", "SUMMARY"):
                    t = _attr(elem, "Title")
                    if t:
                        title = t
                        break

        model = ScreenModel(
            function_id=function_id,
            title=title,
            raw_root_tag=_strip_ns(root.tag),
        )

        # First pass: tabs (so blocks can be associated to tabs).
        # Skip TAB elements that are pure structural wrappers under HEADER/
        # FOOTER (e.g. TAB_HEADER, TAB_FOOTER) — only the BODY tab is a real
        # user-visible tab page.
        for tab_elem in self._iter_by_tags(root, TAB_TAGS):
            if self._has_ancestor_in(tab_elem, root, SKIP_IF_UNDER):
                continue
            model.tabs.append(TabModel(
                name=_attr(tab_elem, "Name", "Id") or "UNNAMED_TAB",
                label=_attr(tab_elem, "Label", "Caption", "DisplayLabel"),
            ))

        # Second pass: LOV definitions (often at top of file)
        for lov_elem in self._iter_by_tags(root, LOV_TAGS):
            lov_name = _attr(lov_elem, "Name", "Id")
            if lov_name:
                model.lovs[lov_name] = {
                    k: v for k, v in lov_elem.attrib.items()
                }

        # Third pass: blocks and their fields.
        # Two filters apply:
        #   1. Skip BLOCK_TAGS elements with no FIELD descendants — these are
        #      text-only block-name references (e.g. <BLOCK>BLK_MASTER</BLOCK>
        #      child of FLDSET) that just name a data block.
        #   2. Skip blocks under SUMMARY/HEADER/FOOTER chrome — those are the
        #      query/list/audit/maker-checker blocks the tester doesn't
        #      manually fill on the main form.
        for block_elem in self._iter_by_tags(root, BLOCK_TAGS):
            if not self._has_field_descendant(block_elem):
                continue
            if self._has_ancestor_in(block_elem, root, SKIP_IF_UNDER):
                continue
            block = self._parse_block(block_elem, model)
            model.blocks.append(block)

        # Fourth pass: top-level buttons (some UIXML put them outside blocks)
        for btn_elem in self._iter_by_tags(root, BUTTON_TAGS):
            # Skip buttons already attached to blocks
            if self._has_ancestor_in(btn_elem, root, BLOCK_TAGS):
                continue
            model.buttons.append(ButtonModel(
                name=_attr(btn_elem, "Name", "Id") or "UNNAMED_BUTTON",
                label=_attr(btn_elem, "Label", "Caption"),
                parent_block=None,
            ))

        # Standard FLEXCUBE buttons that are always implicitly present.
        # Add them if they're not already in the parsed list — downstream
        # consumers should always be able to assume Save/Authorize exist.
        self._inject_standard_buttons(model)

        return model

    # ---- internal helpers ----

    def _find_function_id_in_descendants(self, root: ET.Element) -> str | None:
        for elem in root.iter():
            v = _attr(elem, "FunctionId")
            if v:
                return v
        return None

    @staticmethod
    def _function_id_from_filename(filename_hint: str | None) -> str | None:
        """Fallback: real FLEXCUBE UIXML doesn't carry the function ID inside
        the file. The convention is that the filename IS the function ID
        (e.g. IADPRFNL.xml → IADPRFNL). Strip every extension."""
        if not filename_hint:
            return None
        stem = Path(filename_hint).name.split(".", 1)[0].strip()
        return stem.upper() if stem else None

    def _iter_by_tags(self, root: ET.Element, tags: set[str]):
        wanted = {t.lower() for t in tags}
        for elem in root.iter():
            if _strip_ns(elem.tag).lower() in wanted:
                yield elem

    def _has_field_descendant(self, elem: ET.Element) -> bool:
        """True if `elem` has any FIELD_TAGS descendant. Used to skip block-name
        reference elements (text-only `<BLOCK>NAME</BLOCK>`) that are not real
        block containers."""
        wanted = {t.lower() for t in FIELD_TAGS}
        for child in elem.iter():
            if child is elem:
                continue
            if _strip_ns(child.tag).lower() in wanted:
                return True
        return False

    def _has_ancestor_in(self, target: ET.Element, root: ET.Element, tags: set[str]) -> bool:
        wanted = {t.lower() for t in tags}
        # ElementTree has no parent pointers; build a child→parent map.
        parent_map = {c: p for p in root.iter() for c in p}
        cur = parent_map.get(target)
        while cur is not None:
            if _strip_ns(cur.tag).lower() in wanted:
                return True
            cur = parent_map.get(cur)
        return False

    def _parse_block(self, elem: ET.Element, model: ScreenModel) -> BlockModel:
        # Priority: Name → Id (so each FLDSET has its unique on-screen ID like
        # FST_MASTER vs FLD_AUDIT1) → Block (the data-block, may collide across
        # FLDSETs that share one) → TabPage (for SUMBLOCKs identified only by
        # TABPAGE="QUERY"/"RESULT").
        name = _attr_or_child(elem, "Name", "Id", "FldName", "Block", "TabPage") or "UNNAMED_BLOCK"
        label = _attr_or_child(elem, "Label", "Caption", "Title", "DisplayLabel", "Lbl")
        block_type = (_attr_or_child(elem, "Type", "BlockType") or "").upper()
        is_grid = block_type in GRID_HINT_ATTRS or _to_bool(_attr_or_child(elem, "IsMulti", "Multi"))
        # FLDSET uses VIEW="ME" as well as TYPE="ME" in places; check both.
        if not is_grid:
            is_grid = (_attr(elem, "VIEW", "View") or "").upper() in GRID_HINT_ATTRS
        parent_tab = _attr_or_child(elem, "Tab", "ParentTab", "TabName")
        is_tab = block_type == "TAB" or _to_bool(_attr_or_child(elem, "IsTab"))

        block = BlockModel(
            name=name,
            label=label,
            is_grid=is_grid,
            is_tab=is_tab,
            parent_tab=parent_tab,
        )

        # Fields directly under this block (depth-first, but skip nested blocks)
        for child in elem.iter():
            if child is elem:
                continue
            tag = _strip_ns(child.tag)
            if tag in BLOCK_TAGS:
                # Nested block — handled in its own pass at top level.
                # Don't pull its fields into this block.
                continue
            if tag in FIELD_TAGS:
                # Make sure this field's nearest ancestor block is THIS block,
                # not some nested one. We do this with a quick walk-up.
                if self._nearest_block_is(child, elem):
                    block.fields.append(self._parse_field(child, block))
            elif tag in BUTTON_TAGS:
                model.buttons.append(ButtonModel(
                    name=_attr(child, "Name", "Id") or "UNNAMED_BUTTON",
                    label=_attr(child, "Label", "Caption"),
                    parent_block=block.name,
                ))
        return block

    def _nearest_block_is(self, fld: ET.Element, target_block: ET.Element) -> bool:
        """True if `fld`'s nearest BLOCK ancestor is exactly `target_block`."""
        # Build parent map up to target_block. We assume target_block is in the same tree.
        # Walk up via iter — ElementTree limitation forces a search.
        # For performance on large UIXML, you may want to memoise parent_map at parse time.
        # For correctness over speed, we recompute.
        parent_map: dict[ET.Element, ET.Element] = {}
        for p in target_block.iter():
            for c in p:
                parent_map[c] = p
        cur = parent_map.get(fld)
        while cur is not None:
            if cur is target_block:
                return True
            tag = _strip_ns(cur.tag)
            if tag in BLOCK_TAGS:
                return False  # closer block ancestor exists
            cur = parent_map.get(cur)
        return False

    def _parse_field(self, elem: ET.Element, block: BlockModel) -> FieldModel:
        # In the child-element dialect both `<TYPE>TEXT</TYPE>` (HTML widget kind)
        # and `<DTYPE>VARCHAR2</DTYPE>` (data type) exist. Prefer DTYPE since
        # downstream consumers care about the storage type, not the widget.
        datatype = _attr_or_child(elem, "DataType", "Datatype", "DTYPE", "Type")
        # Standard FLEXCUBE select/radio widget kinds — keep as-is so they map
        # to "DROPDOWN"/"RADIO"/"CHECKBOX" semantics in downstream prompts.
        widget = (_attr_or_child(elem, "Type") or "").upper()
        if widget in {"SELECT", "ROSELECT", "DROPDOWN"} and datatype not in {"SELECT", "DROPDOWN"}:
            datatype = "DROPDOWN"
        elif widget in {"RADIO"} and datatype != "RADIO":
            datatype = "RADIO"
        elif widget in {"CHECKBOX"} and datatype != "CHECKBOX":
            datatype = "CHECKBOX"
        elif widget == "DATETIME" and not datatype:
            datatype = "DATE"

        options: list[dict[str, str]] = []
        for child in elem:
            if _strip_ns(child.tag).upper() == "OPTION":
                v = _attr(child, "Value")
                if v is None:
                    continue
                lbl = (child.text or "").strip() or v
                options.append({"value": v, "label": lbl})

        used = {
            # attribute names already consumed by explicit fields below
            "name", "id", "fldname", "label", "caption", "displaylabel", "lbl",
            "type", "datatype", "dtype", "length", "maxlength", "size",
            "precision", "decimals", "max_decimals",
            "required", "mandatory", "notnull", "reqd",
            "readonly", "read_only", "disabled",
            "hidden",
            "lov", "lovname",
            "default", "defaultvalue",
        }

        return FieldModel(
            name=_attr_or_child(elem, "Name", "FldName", "Id") or "UNNAMED_FIELD",
            label=_attr_or_child(elem, "Label", "Caption", "DisplayLabel", "Lbl"),
            datatype=(datatype or "").upper() or None,
            length=_to_int(_attr_or_child(elem, "Length", "MaxLength", "Size")),
            precision=_to_int(_attr_or_child(elem, "Precision", "Decimals", "Max_Decimals")),
            required=_to_bool(_attr_or_child(elem, "Required", "Mandatory", "NotNull", "Reqd")),
            readonly=_to_bool(_attr_or_child(elem, "ReadOnly", "Read_Only", "Disabled")),
            hidden=_to_bool(_attr_or_child(elem, "Hidden")),
            lov=_lov_from(elem),
            default=_attr_or_child(elem, "Default", "DefaultValue"),
            parent_block=block.name,
            is_grid_column=block.is_grid,
            options=options,
            extra={k: v for k, v in elem.attrib.items() if k.lower() not in used},
        )

    def _inject_standard_buttons(self, model: ScreenModel) -> None:
        """FLEXCUBE screens implicitly have Save/Authorize/etc. even if not
        explicitly declared in UIXML. Add them so downstream knows they exist."""
        existing = {b.name.upper() for b in model.buttons}
        standard = [
            ("NEW", "New"),
            ("SAVE", "Save"),
            ("ENTERQUERY", "Enter Query"),
            ("EXECUTEQUERY", "Execute Query"),
            ("UNLOCK", "Unlock"),
            ("AUTHORIZE", "Authorize"),
            ("COPY", "Copy"),
            ("CLOSE", "Close"),
        ]
        for n, lbl in standard:
            if n not in existing:
                model.buttons.append(ButtonModel(
                    name=n, label=lbl, parent_block=None,
                ))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Parse a FLEXCUBE UIXML file.")
    ap.add_argument("uixml_path", help="Path to UIXML file")
    ap.add_argument("--out", default=None, help="Write JSON model to this path")
    args = ap.parse_args(argv)

    parser = FlexcubeUIXMLParser()
    model = parser.parse_file(args.uixml_path)

    out_json = json.dumps(model.to_dict(), indent=2, default=str)
    if args.out:
        Path(args.out).write_text(out_json)
        print(f"Wrote {args.out}")
    else:
        print(out_json)
    print(
        f"\n[summary] function_id={model.function_id}  "
        f"blocks={len(model.blocks)}  "
        f"fields={len(model.all_fields())}  "
        f"buttons={len(model.buttons)}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
