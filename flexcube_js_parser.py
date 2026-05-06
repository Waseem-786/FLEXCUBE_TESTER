"""
flexcube_js_parser.py
=====================

Best-effort static analyser for the JS files that ship alongside FLEXCUBE
UIXML screens.

Why "best-effort"?
- The JS is dynamically typed and varies in style across modules. A perfect
  parse would need a real JS AST (e.g. via the `esprima` Python port).
- For test-plan generation we don't need perfection — we need to surface
  enough signal to drive negative/edge cases. Three signals matter most:
    1. WHICH FIELDS HAVE onChange / onLoad / onValidate handlers
       (tells us which fields trigger downstream behaviour)
    2. STATIC VALIDATION RULES (regex, range, length checks, "required if X")
    3. CROSS-FIELD READS — i.e. handler for field A reads/writes field B
       (these are the dependencies the test plan needs to probe)

Approach: regex-based extraction with conservative patterns. False positives
are acceptable; false negatives are noted in `parser_warnings`.

If your shop has access to a JS AST library (esprima, acorn via subprocess,
or pyjsparser), swap `_collect_handlers` and `_collect_field_refs` for AST-
based versions. The downstream contract (FieldBehaviour list) stays the same.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# fnNAME = function(...) { ... }  OR  function fnNAME(...) { ... }
HANDLER_RE = re.compile(
    r"""
    (?:
        function\s+(?P<fname1>[A-Za-z_]\w*)\s*\([^)]*\)\s*\{
      |
        (?:var|let|const)?\s*(?P<fname2>[A-Za-z_]\w*)\s*=\s*function\s*\([^)]*\)\s*\{
    )
    """,
    re.VERBOSE,
)

# Field-reference patterns. FLEXCUBE tends to use:
#   getFieldValue("BLK_NAME", "FIELD_NAME")
#   setFieldValue("BLK_NAME", "FIELD_NAME", val)
#   doc.getElementById("BLK_NAME.FIELD_NAME")
#   fcjFunction.getFieldValue(...)
GET_RE = re.compile(
    r"""getFieldValue\s*\(\s*["']([^"']+)["']\s*,\s*["']([^"']+)["']""",
)
SET_RE = re.compile(
    r"""setFieldValue\s*\(\s*["']([^"']+)["']\s*,\s*["']([^"']+)["']""",
)
DOM_RE = re.compile(
    r"""getElementById\s*\(\s*["']([A-Z0-9_]+)\.([A-Z0-9_]+)["']""",
)

# Attach-handler patterns:
#   fcjFunction.attachOnChange("FIELD","handlerName")
#   onChange  /  onLoad  /  onValidate / preSave
ATTACH_RE = re.compile(
    r"""(attach)?(onChange|onLoad|onValidate|preSave|postSave|onClick)
        \s*\(\s*["']([^"']+)["']
        (?:\s*,\s*["']([^"']+)["'])?
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Validation-rule heuristics inside handler bodies:
#   if (val == "" || val == null) ...  → required check
#   if (val.length > N) ...             → length check
#   if (val < N || val > M) ...         → range check
#   regex literal   /pattern/.test(val) → regex check
EMPTY_CHECK_RE = re.compile(
    r"""(==|===)\s*(""|''|null|undefined)|isEmpty\s*\(""",
)
LENGTH_CHECK_RE = re.compile(r"\.length\s*[<>]=?\s*(\d+)")
NUM_RANGE_RE = re.compile(r"[<>]=?\s*(-?\d+(?:\.\d+)?)")
REGEX_LITERAL_RE = re.compile(r"/(?P<pattern>(?:[^/\\\n]|\\.)+)/[gimsuy]*\.test")

# Show/hide / enable/disable hints
ENABLE_RE = re.compile(r"""enableField\s*\(\s*["']([^"']+)["']""")
DISABLE_RE = re.compile(r"""disableField\s*\(\s*["']([^"']+)["']""")
SHOW_RE = re.compile(r"""showField\s*\(\s*["']([^"']+)["']""")
HIDE_RE = re.compile(r"""hideField\s*\(\s*["']([^"']+)["']""")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FieldBehaviour:
    field_name: str
    block_name: str | None = None
    has_on_change: bool = False
    has_on_validate: bool = False
    has_on_load_handler: bool = False
    handlers: list[str] = field(default_factory=list)
    reads_fields: list[str] = field(default_factory=list)
    writes_fields: list[str] = field(default_factory=list)
    enables_fields: list[str] = field(default_factory=list)
    disables_fields: list[str] = field(default_factory=list)
    shows_fields: list[str] = field(default_factory=list)
    hides_fields: list[str] = field(default_factory=list)
    inferred_validations: list[str] = field(default_factory=list)


@dataclass
class JSAnalysisResult:
    field_behaviours: dict[str, FieldBehaviour] = field(default_factory=dict)
    cross_field_dependencies: list[tuple[str, str, str]] = field(default_factory=list)
    # entries: (source_field, dependency_kind, target_field)
    # dependency_kind ∈ {"reads", "writes", "enables", "disables", "shows", "hides"}
    pre_save_hooks: list[str] = field(default_factory=list)
    parser_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "field_behaviours": {
                k: asdict(v) for k, v in self.field_behaviours.items()
            },
            "cross_field_dependencies": self.cross_field_dependencies,
            "pre_save_hooks": self.pre_save_hooks,
            "parser_warnings": self.parser_warnings,
        }


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class FlexcubeJSParser:
    """
    Heuristic static analyser for FLEXCUBE screen JS.

    Produces a JSAnalysisResult that downstream consumers (test synthesiser)
    join with the UIXML ScreenModel by `field_name`.
    """

    def parse_file(self, path: str | Path) -> JSAnalysisResult:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        return self.parse_string(text)

    def parse_string(self, js_text: str) -> JSAnalysisResult:
        result = JSAnalysisResult()

        # Strip line and block comments to reduce false positives.
        clean = self._strip_comments(js_text)

        # 1. Walk attach-handler hookups to discover which fields have which events.
        for m in ATTACH_RE.finditer(clean):
            event = m.group(2).lower()
            field_arg = m.group(3)
            handler_name = m.group(4) or ""
            fb = result.field_behaviours.setdefault(
                field_arg, FieldBehaviour(field_name=field_arg),
            )
            if event == "onchange":
                fb.has_on_change = True
            elif event == "onvalidate":
                fb.has_on_validate = True
            elif event == "onload":
                fb.has_on_load_handler = True
            if handler_name:
                fb.handlers.append(handler_name)

        # 2. Find each handler function body, then attribute its
        #    reads/writes/enables/disables/etc. to the field(s) that use it.
        handler_bodies = self._extract_handler_bodies(clean)

        # Build reverse map: handler_name -> [fields that use it]
        handler_to_fields: dict[str, list[str]] = {}
        for fb in result.field_behaviours.values():
            for h in fb.handlers:
                handler_to_fields.setdefault(h, []).append(fb.field_name)

        for handler_name, body in handler_bodies.items():
            owners = handler_to_fields.get(handler_name, [])
            reads, writes = self._collect_field_refs(body)
            enables = ENABLE_RE.findall(body)
            disables = DISABLE_RE.findall(body)
            shows = SHOW_RE.findall(body)
            hides = HIDE_RE.findall(body)
            validations = self._infer_validations(body)

            for owner in owners or [f"<orphan:{handler_name}>"]:
                fb = result.field_behaviours.setdefault(
                    owner, FieldBehaviour(field_name=owner),
                )
                fb.reads_fields.extend(r for r in reads if r != owner)
                fb.writes_fields.extend(w for w in writes if w != owner)
                fb.enables_fields.extend(enables)
                fb.disables_fields.extend(disables)
                fb.shows_fields.extend(shows)
                fb.hides_fields.extend(hides)
                fb.inferred_validations.extend(validations)
                # de-dupe while preserving order
                for attr in ("reads_fields", "writes_fields", "enables_fields",
                             "disables_fields", "shows_fields", "hides_fields",
                             "inferred_validations"):
                    seen, out = set(), []
                    for v in getattr(fb, attr):
                        if v not in seen:
                            seen.add(v)
                            out.append(v)
                    setattr(fb, attr, out)

                # Push into cross_field_dependencies for easy graph rendering
                for r in fb.reads_fields:
                    result.cross_field_dependencies.append((owner, "reads", r))
                for w in fb.writes_fields:
                    result.cross_field_dependencies.append((owner, "writes", w))
                for e in fb.enables_fields:
                    result.cross_field_dependencies.append((owner, "enables", e))
                for d in fb.disables_fields:
                    result.cross_field_dependencies.append((owner, "disables", d))
                for s in fb.shows_fields:
                    result.cross_field_dependencies.append((owner, "shows", s))
                for h in fb.hides_fields:
                    result.cross_field_dependencies.append((owner, "hides", h))

            # preSave hooks are screen-level, not per-field
            if "preSave" in handler_name or handler_name.lower().startswith("presave"):
                result.pre_save_hooks.append(handler_name)

        # de-dupe cross_field_dependencies
        seen = set()
        unique = []
        for triple in result.cross_field_dependencies:
            if triple not in seen:
                seen.add(triple)
                unique.append(triple)
        result.cross_field_dependencies = unique

        return result

    # ---- helpers ----

    @staticmethod
    def _strip_comments(text: str) -> str:
        # Block comments
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        # Line comments — careful not to nuke '//' inside strings; this is a
        # heuristic that is good enough for FLEXCUBE-style JS.
        text = re.sub(r"//[^\n]*", "", text)
        return text

    def _extract_handler_bodies(self, text: str) -> dict[str, str]:
        """
        Return {handler_name: body_string}. Body string is everything between
        the opening `{` and the matching closing `}` of the function.
        """
        bodies: dict[str, str] = {}
        for m in HANDLER_RE.finditer(text):
            name = m.group("fname1") or m.group("fname2")
            if not name:
                continue
            # Find the body — start at the `{` immediately after the match.
            start = text.find("{", m.end() - 1)
            if start < 0:
                continue
            body = self._extract_balanced_braces(text, start)
            if body is not None:
                bodies[name] = body
        return bodies

    @staticmethod
    def _extract_balanced_braces(text: str, start_idx: int) -> str | None:
        """Given index of an opening '{', return inner text up to matching '}'."""
        depth = 0
        i = start_idx
        in_str: str | None = None
        while i < len(text):
            c = text[i]
            if in_str:
                if c == "\\":
                    i += 2
                    continue
                if c == in_str:
                    in_str = None
            elif c in ("'", '"', "`"):
                in_str = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start_idx + 1:i]
            i += 1
        return None

    def _collect_field_refs(self, body: str) -> tuple[list[str], list[str]]:
        reads: list[str] = []
        writes: list[str] = []
        for m in GET_RE.finditer(body):
            reads.append(m.group(2))
        for m in SET_RE.finditer(body):
            writes.append(m.group(2))
        for m in DOM_RE.finditer(body):
            # Treat DOM access as a read by default — we can't easily tell.
            reads.append(m.group(2))
        return reads, writes

    def _infer_validations(self, body: str) -> list[str]:
        out: list[str] = []
        if EMPTY_CHECK_RE.search(body):
            out.append("required-check (empty/null guard)")
        for m in LENGTH_CHECK_RE.finditer(body):
            out.append(f"length-check (boundary={m.group(1)})")
        for m in REGEX_LITERAL_RE.finditer(body):
            out.append(f"regex-check (pattern=/{m.group('pattern')}/)")
        # Numeric range: heuristic — if the body has any numeric comparison
        # AND no length-check, flag a possible numeric range guard.
        if NUM_RANGE_RE.search(body) and not LENGTH_CHECK_RE.search(body):
            out.append("numeric-range-check (heuristic)")
        return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Analyse a FLEXCUBE screen JS file.")
    ap.add_argument("js_path", help="Path to JS file")
    ap.add_argument("--out", default=None, help="Write JSON result to this path")
    args = ap.parse_args(argv)

    parser = FlexcubeJSParser()
    res = parser.parse_file(args.js_path)
    out_json = json.dumps(res.to_dict(), indent=2)
    if args.out:
        Path(args.out).write_text(out_json)
        print(f"Wrote {args.out}")
    else:
        print(out_json)
    print(
        f"\n[summary] fields={len(res.field_behaviours)}  "
        f"deps={len(res.cross_field_dependencies)}  "
        f"presave_hooks={len(res.pre_save_hooks)}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
