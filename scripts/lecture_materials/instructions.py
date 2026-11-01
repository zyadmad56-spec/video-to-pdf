"""Shared contracts for a screenshot and the actions it teaches."""

import math


def tutorial_mode(content):
    mode = content.get("document_mode", "study_notes")
    if not isinstance(mode, str) or mode not in {"study_notes", "software_tutorial"}:
        raise ValueError("document_mode must be study_notes or software_tutorial")
    return mode == "software_tutorial"


def validate(block):
    for key in ("title", "text", "result", "caption"):
        if not isinstance(block.get(key), str) or not block[key].strip():
            raise ValueError(f"Instruction blocks require nonempty {key}")
    steps = block.get("steps")
    if not isinstance(steps, list) or not steps or any(
        not isinstance(step, str) or not step.strip() for step in steps
    ):
        raise ValueError("Instruction steps must be a nonempty list of strings")
    selected = block.get("layout", "auto")
    if not isinstance(selected, str) or selected not in {"auto", "side-by-side", "stacked"}:
        raise ValueError("Instruction layout must be auto, side-by-side, or stacked")
    callouts = block.get("callouts", [])
    if not isinstance(callouts, list):
        raise ValueError("Instruction callouts must be a list")
    for callout in callouts:
        if not isinstance(callout, dict) or set(callout) != {"step", "box"}:
            raise ValueError("Each callout needs exactly step and box")
        step = callout["step"]
        if type(step) is not int or not 1 <= step <= len(steps):
            raise ValueError("Callout step must identify an existing numbered step")
        box = callout["box"]
        if not isinstance(box, list) or len(box) != 4 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in box
        ):
            raise ValueError("Callout box must contain four finite normalized coordinates")
        x, y, width, height = box
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
            raise ValueError("Callout box must stay inside the image")


def layout(block, aspect):
    selected = block.get("layout", "auto")
    return ("stacked" if aspect >= 2.4 else "side-by-side") if selected == "auto" else selected


def preview_sections(content):
    """Select two complete authored units, retaining their containing sections."""
    chosen = []
    remaining = 2
    for section in content["sections"]:
        units = [block for block in section["blocks"] if block.get("type") == "instruction"][:remaining]
        if units:
            chosen.append(dict(section, blocks=units))
            remaining -= len(units)
        if not remaining:
            break
    return chosen


def fit_image(width, height, area):
    """Contain the complete image in (left, top, width, height), without crop."""
    left, top, available_width, available_height = area
    scale = min(available_width / width, available_height / height)
    drawn_width, drawn_height = width * scale, height * scale
    return (left + (available_width - drawn_width) / 2,
            top + (available_height - drawn_height) / 2, drawn_width, drawn_height)


def recording_label(asset):
    """A reviewed recording's short credit; full permissions stay in its record."""
    if asset.get("origin") == "lecture" and not (asset.get("source_url") or asset.get("source_page")):
        return asset.get("source_name")
    return None
