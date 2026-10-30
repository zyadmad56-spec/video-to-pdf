"""Small structured-math validator and MathML serializer for equation blocks.

An equation block may carry optional ``math``: a tiny validated tree that the
PDF renders as native MathML (a genuine stacked fraction, lowered subscripts,
raised superscripts) instead of the plain-slash fallback.  The required
``text`` field stays the readable inventory/fallback representation.

Safety: node types, field names, value shapes, depth, and node counts are
restricted here; unknown types/fields, empty required values, malformed
numbers, and oversized trees are rejected.  Every leaf string is escaped on
serialization, so document data can never smuggle active markup, attributes,
scripts, custom fonts, or network resources.  No eval, no code execution, no
network calls.

PPTX note: python-pptx cannot preserve this structure as native editable
math; ``render_pptx`` refuses such blocks explicitly instead of silently
flattening them (documented PDF-only limitation).
"""

from __future__ import annotations

import html
import math
import re

MAX_DEPTH = 20
MAX_NODES = 128
MAX_NAME = 24
MAX_TEXT = 120

OPERATORS = frozenset({
    "=", "+", "-", "−", "×", "÷", "·", "≈", "≠", "<", ">", "≤", "≥", "(", ")", "%",
    "∝", ",", "°",
})
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?$")
_CHILD_FIELDS = {
    "row": ("children",),
    "identifier": ("name",),
    "number": ("value",),
    "operator": ("symbol",),
    "text": ("value",),
    "fraction": ("numerator", "denominator"),
    "subscript": ("base", "sub"),
    "superscript": ("base", "sup"),
    "sqrt": ("radicand",),
}


def validate_math(node, _depth=0, _counter=None):
    """Validate one math-tree node and return a normalized deep copy.

    Raises ``ValueError`` with an actionable message for any malformed
    structure listed in the module docstring.  The returned value contains
    only plain dicts/lists/strings built from validated leaves.
    """

    if _counter is None:
        counter = {"count": 0}
    else:
        counter = _counter
    counter["count"] += 1
    if counter["count"] > MAX_NODES:
        raise ValueError(f"math tree exceeds the {MAX_NODES}-node limit")
    if _depth > MAX_DEPTH:
        raise ValueError(f"math tree exceeds the {MAX_DEPTH}-depth limit")
    if not isinstance(node, dict):
        raise ValueError("Every math node must be an object with a 'type'")
    kind = node.get("type")
    if not isinstance(kind, str) or kind not in _CHILD_FIELDS:
        allowed = ", ".join(sorted(_CHILD_FIELDS))
        raise ValueError(f"Unsupported math node type: {kind!r}; allowed types: {allowed}")
    expected = _CHILD_FIELDS[kind]
    unknown = sorted(set(node) - {"type", *expected})
    if unknown:
        raise ValueError(
            f"Math node {kind!r} has unsupported fields: {', '.join(unknown)}"
        )
    normalized = {"type": kind}

    if kind == "row":
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise ValueError("row math nodes need a nonempty 'children' node list")
        normalized["children"] = [
            validate_math(child, _depth=_depth + 1, _counter=counter)
            for child in children
        ]
    elif kind in {"fraction", "subscript", "superscript", "sqrt"}:
        for field in expected:
            child = node.get(field)
            if not isinstance(child, dict):
                raise ValueError(f"Math node {kind!r} needs an object in '{field}'")
            normalized[field] = validate_math(
                child, _depth=_depth + 1, _counter=counter
            )
    elif kind == "identifier":
        name = node.get("name")
        if not isinstance(name, str) or not name.isalpha() or not 1 <= len(name) <= MAX_NAME:
            raise ValueError(
                f"Math identifier 'name' must be 1-{MAX_NAME} letters, got {name!r}"
            )
        normalized["name"] = name
    elif kind == "number":
        value = node.get("value")
        if isinstance(value, str):
            candidate = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool) \
                and math.isfinite(value):
            candidate = repr(value)
        else:
            raise ValueError("Math number 'value' must be a string or a finite real number")
        if not _NUMBER_RE.fullmatch(candidate):
            raise ValueError(
                f"Math number 'value' must look like 12 or -3.04, got {candidate!r}"
            )
        normalized["value"] = candidate
    elif kind == "operator":
        symbol = node.get("symbol")
        if not isinstance(symbol, str) or symbol not in OPERATORS:
            raise ValueError(
                "Math operator 'symbol' must be one of: " + " ".join(sorted(OPERATORS))
            )
        normalized["symbol"] = symbol
    else:  # text
        value = node.get("value")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Math text nodes need a nonempty 'value'")
        if len(value) > MAX_TEXT or "\ufffd" in value or any(ord(ch) < 32 for ch in value):
            raise ValueError(
                f"Math text 'value' must be 1-{MAX_TEXT} printable characters "
                "without control or replacement characters"
            )
        normalized["value"] = value
    return normalized


def to_mathml(node):
    """Serialize a validated math tree to one MathML element per node.

    Fraction/subscript/superscript produce exactly the required two child
    elements (a ``row`` child is already one ``mrow``).  All leaf strings are
    HTML-escaped, so no document-supplied text can become active markup.
    """

    kind = node.get("type")
    if kind == "row":
        return "<mrow>" + "".join(to_mathml(child) for child in node["children"]) + "</mrow>"
    if kind == "fraction":
        return (
            "<mfrac>" + to_mathml(node["numerator"]) + to_mathml(node["denominator"])
            + "</mfrac>"
        )
    if kind == "subscript":
        return "<msub>" + to_mathml(node["base"]) + to_mathml(node["sub"]) + "</msub>"
    if kind == "superscript":
        return "<msup>" + to_mathml(node["base"]) + to_mathml(node["sup"]) + "</msup>"
    if kind == "sqrt":
        return "<msqrt>" + to_mathml(node["radicand"]) + "</msqrt>"
    if kind == "identifier":
        return "<mi>" + html.escape(node["name"]) + "</mi>"
    if kind == "number":
        return "<mn>" + html.escape(node["value"]) + "</mn>"
    if kind == "operator":
        return "<mo>" + html.escape(node["symbol"]) + "</mo>"
    if kind == "text":
        return "<mtext>" + html.escape(node["value"]) + "</mtext>"
    raise ValueError(f"Unsupported math node type during serialization: {kind!r}")
