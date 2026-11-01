"""Evidence-grounded authoring validation, image policy, readiness, and preview binding.

This module is the boundary between extracted source evidence and rendered study
material.  It proves structural properties only:

* every cited evidence ID exists;
* every substantive evidence record is either cited or explicitly omitted;
* blocks are well formed and image use follows the job's source policy;
* a preview PDF/PPTX file is the current file that was accepted for the current
  content, style, preferences, and *used* assets;
* source review artifacts cover the requested interval before final rendering.

It cannot prove that a citation supports the prose attached to it, that an
explanation is scientifically complete, or that a human accepted a preview.
Those remain host/user review duties and are reported as such.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from . import image_policy
from . import math_layout
from . import output_preferences
from . import instructions
from .adaptive import DEFAULT_INTERVAL, plan_refinement, validate_refinement_level
from .jobs import file_digest, json_digest, load_job, read_json, save_job
from .timing import sample_times

SCHEMA_VERSION = 1

EVIDENCE_FILES = ("transcript.json", "screen.json", "references.json", "external.json")
BLOCK_TYPES = ("paragraph", "bullets", "table", "equation", "image", "instruction")
BLOCK_ORIGINS = ("lecture", "explanation", "agent_explanation", "example")
OMISSION_KINDS = {
    "repetition",
    "noninstructional",
    "unreadable",
    "duplicate",
    "outside_scope",
    "superseded",
    "redundant_visual",
}
REVIEWED_STATUSES = {"reviewed", "unreadable"}
PREVIEW_SUFFIXES = {".pdf", ".pptx"}

SEMANTIC_REVIEW_NOTICE = (
    "Structural validation cannot establish that an evidence ID supports the prose, "
    "that every lecture point was taught, or that diagrams/equations were interpreted "
    "correctly. The host must inspect the actual content and rendered output."
)


# ---------------------------------------------------------------------------
# Evidence access
# ---------------------------------------------------------------------------


def optional_records(root, filename):
    """Read an optional evidence file; a missing file means no records."""

    path = Path(root) / "work" / filename
    if not path.exists():
        return []
    document = read_json(path)
    if not isinstance(document, list):
        raise ValueError(f"work/{filename} must contain a JSON array of records")
    return document


def _record_id(record, filename, index):
    if not isinstance(record, dict):
        raise ValueError(f"work/{filename}[{index}] must be an object")
    identifier = record.get("id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError(f"work/{filename}[{index}] needs a nonempty string id")
    return identifier


def evidence_records(root):
    """Return all detected source evidence records with unique IDs."""

    records = []
    seen = {}
    for filename in EVIDENCE_FILES:
        for index, record in enumerate(optional_records(root, filename)):
            identifier = _record_id(record, filename, index)
            if identifier in seen:
                raise ValueError(
                    f"Evidence ID {identifier!r} is duplicated in "
                    f"work/{filename} and work/{seen[identifier]}"
                )
            seen[identifier] = filename
            records.append(record)
    return records


def evidence_index(root):
    return {record["id"]: record for record in evidence_records(root)}


def evidence_is_substantive(record):
    """Honour an explicit non-substantive marker; default to required coverage."""

    if record.get("substantive") is False:
        return False
    if record.get("coverage") in {"not_required", "metadata_only"}:
        return False
    return True


def cited_evidence_ids(content):
    identifiers = []
    for section in content.get("sections", []):
        for block in section.get("blocks", []):
            for identifier in block.get("evidence", []) or []:
                if isinstance(identifier, str):
                    identifiers.append(identifier)
    for omission in content.get("omissions", []) or []:
        if isinstance(omission.get("evidence_id"), str):
            identifiers.append(omission["evidence_id"])
    for uncertainty in content.get("uncertainties", []) or []:
        if isinstance(uncertainty.get("evidence_id"), str):
            identifiers.append(uncertainty["evidence_id"])
    return set(identifiers)


def iter_blocks(content):
    """Yield (section, block) in document order."""

    for section in content.get("sections", []):
        for block in section.get("blocks", []):
            yield section, block


def _visible_text(value):
    text = str(value)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1", text)
    text = text.replace("**", "").replace("`", "")
    return text


def block_plain_text(block):
    """Return the complete user-visible text of a block for inventories."""

    kind = block.get("type")
    if kind == "instruction":
        return "\n".join(_visible_text(value) for value in
                         [block.get("title", ""), block.get("text", ""),
                          *block.get("steps", []), block.get("result", ""), block.get("caption", "")])
    if kind in {"paragraph", "equation"}:
        parts = [_visible_text(block.get("text", ""))]
        for key in ("symbols", "labels", "units", "description", "caption"):
            value = block.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(_visible_text(value))
        return "\n".join(part for part in parts if part.strip())
    if kind == "bullets":
        return "\n".join(_visible_text(item) for item in block.get("items", []))
    if kind == "table":
        header = block.get("header") or block.get("columns") or []
        rows = block.get("rows", [])
        lines = [" | ".join(_visible_text(cell) for cell in header)] if header else []
        lines.extend(" | ".join(_visible_text(cell) for cell in row) for row in rows)
        caption = block.get("caption")
        if caption:
            lines.append(_visible_text(caption))
        return "\n".join(lines)
    if kind == "image":
        return _visible_text(block.get("caption", ""))
    return ""


def content_text_inventory(content):
    return [{"section": section["id"], "type": block.get("type"),
             "text": block_plain_text(block)} for section, block in iter_blocks(content)]


# ---------------------------------------------------------------------------
# Image source policy
# ---------------------------------------------------------------------------


def image_mode(root):
    """Normalise the job's image policy.

    Accepted spellings: ``text``/``none``, ``web``, ``user``, ``mixed``/``both``
    and explicit combinations such as ``web+user``.  Lecture captures and
    reference-material images are only allowed when the job explicitly includes
    ``lecture``/``source`` or sets ``allow_source_images``.
    """

    job = load_job(root)
    preferences = job.get("preferences") or {}
    if not isinstance(preferences, dict):
        raise ValueError("job preferences must be an object")
    raw = preferences.get("image_mode", "text")
    if not isinstance(raw, str):
        raise ValueError("preferences.image_mode must be a string")
    value = raw.strip().lower()
    if value in {"text", "none", "text_only", "text-only", "no_images", "no-images"}:
        return "text"
    if value in {"web_images", "web-images", "web images"}:
        return "web"
    if value in {"user_images", "user-images", "user images", "user-supplied", "supplied"}:
        return "user"
    tokens = {token for token in re.split(r"[\s+,/|&]+", value) if token}
    if value in {"mixed", "both", "combined"} or {"web", "user"} <= tokens:
        return "mixed"
    if tokens == {"web"}:
        return "web"
    if tokens == {"user"}:
        return "user"
    if tokens <= {"web", "user", "lecture", "source"} and ("lecture" in tokens or "source" in tokens):
        return "mixed"
    raise ValueError(
        f"Unsupported image_mode {raw!r}; use text, web, user, or an explicit combination"
    )


def _allowed_origins(root, mode):
    if mode == "text":
        return set()
    origins = {"web"} if mode == "web" else {"user"} if mode == "user" else {"web", "user"}
    preferences = load_job(root).get("preferences") or {}
    if not isinstance(preferences, dict):
        raise ValueError("job preferences must be an object")
    if mode == "mixed" and (
        preferences.get("allow_source_images") is True
        or "lecture" in str(preferences.get("image_mode", "")).lower()
        or "source" in str(preferences.get("image_mode", "")).lower()
    ):
        origins |= {"lecture", "reference"}
    return origins


def _asset_records(root):
    records = optional_records(root, "assets.json")
    identifiers = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Every image asset record must be an object")
        identifier = record.get("id")
        if identifier is not None and (not isinstance(identifier, str) or not identifier.strip()):
            raise ValueError("Image asset IDs must be nonempty strings")
        if identifier:
            identifiers.append(identifier)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Image asset IDs must be unique in work/assets.json")
    return records


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _job_file_path(root, value):
    """Resolve an asset path against the job, never against the caller's CWD."""

    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return Path(root) / path


def image_asset(root, asset_id, section_id):
    """Return an accepted image asset that is reviewed for this section.

    Any excluded/pending state, missing review reason, missing section-specific
    placement review, policy mismatch, missing file, or undecodable bytes fails
    closed.  A generally relevant asset is not accepted for other sections.
    """

    if not _nonempty(asset_id):
        raise ValueError("Image blocks require a nonempty asset_id")
    mode = image_mode(root)
    if mode == "text":
        raise ValueError("Text-only output cannot contain inserted image objects")
    assets = {record["id"]: record for record in _asset_records(root) if _nonempty(record.get("id"))}
    asset = assets.get(asset_id)
    if asset is None:
        raise ValueError(f"Unknown image asset: {asset_id}")

    state = asset.get("state")
    if not isinstance(state, str) or state not in {"accepted", "excluded", "pending"}:
        raise ValueError(
            f"Image {asset_id} needs an explicit accepted/excluded/pending state"
        )
    if state != "accepted":
        reason = asset.get("exclusion_reason") or asset.get("state_reason") or "no reason recorded"
        raise ValueError(f"Image {asset_id} is {state}, not accepted for insertion: {reason}")

    origin = asset.get("origin")
    if not isinstance(origin, str):
        raise ValueError(f"Image {asset_id} has an unsupported origin: {origin!r}")
    origin_aliases = {
        "supplied": "user", "user_supplied": "user", "user-supplied": "user",
        "web_search": "web", "web-search": "web", "internet": "web",
        "lecture_capture": "lecture", "lecture-capture": "lecture", "screen": "lecture",
        "reference_material": "reference", "reference-material": "reference",
    }
    origin = origin_aliases.get(origin, origin)
    if origin not in {"user", "web", "lecture", "reference"}:
        raise ValueError(f"Image {asset_id} has an unsupported origin: {origin!r}")
    allowed = _allowed_origins(root, mode)
    if origin not in allowed:
        raise ValueError(
            f"Image origin {origin!r} is outside the selected source policy ({mode}); "
            "ask the user before expanding the image source"
        )

    candidate = asset.get("candidate_review")
    if not isinstance(candidate, dict):
        raise ValueError(f"Image {asset_id} lacks a candidate_review record")
    if candidate.get("inspected") is not True:
        raise ValueError(f"Image {asset_id} was not actually inspected")
    if candidate.get("decision") != "relevant" or not _nonempty(candidate.get("reason")):
        raise ValueError(
            f"Image {asset_id} needs a candidate relevance decision and reason"
        )

    placements = asset.get("placements")
    if not isinstance(placements, dict):
        raise ValueError(f"Image {asset_id} lacks per-section placement records")
    placement = placements.get(section_id)
    if not isinstance(placement, dict):
        raise ValueError(
            f"Image {asset_id} has no placement review for section {section_id!r}"
        )
    if placement.get("inspected_after_placement") is not True:
        raise ValueError(
            f"Image {asset_id} was not inspected after placement in {section_id!r}"
        )
    if placement.get("decision") != "relevant" or not _nonempty(placement.get("reason")):
        raise ValueError(
            f"Image {asset_id} needs a section-specific relevance decision for {section_id!r}"
        )
    crop = placement.get("crop")
    if isinstance(crop, dict) and crop.get("preserves_labels") is not True:
        raise ValueError(
            f"Image {asset_id} uses a crop that does not confirm scientific labels are preserved"
        )
    placement_name = placement.get("placement")
    if placement_name is not None and (
        not isinstance(placement_name, str)
        or placement_name not in {"auto", "start", "end", "left", "right", "center"}
    ):
        raise ValueError(f"Image {asset_id} has an invalid placement value: {placement_name!r}")

    source_name = asset.get("source_name")
    if source_name is not None and (
        not isinstance(source_name, str)
        or not source_name.strip()
        or len(source_name) > 48
        or "\ufffd" in source_name
        or any(ord(ch) < 32 for ch in source_name)
    ):
        raise ValueError(
            f"Image {asset_id} source_name must be 1-48 printable characters"
        )

    if origin == "web":
        source = asset.get("source_url") or asset.get("source_page")
        if not _nonempty(source):
            raise ValueError(f"Web image {asset_id} must retain its source URL or page")
        if "license" not in asset and "licensing" not in asset:
            raise ValueError(f"Web image {asset_id} must record licensing status (unknown is allowed)")
    if origin == "user":
        original = asset.get("original_path")
        if original is not None:
            original_path = _job_file_path(root, original)
            if original_path is None or not original_path.is_file():
                raise ValueError(f"Original supplied image is missing: {original!r}")

    path = asset.get("path")
    if not _nonempty(path):
        raise ValueError(f"Image {asset_id} has no file path")
    image_path = _job_file_path(root, path)
    if image_path is None or not image_path.is_file():
        raise ValueError(f"Image file not found: {path}")
    try:
        from PIL import Image

        with Image.open(image_path) as handle:
            handle.verify()
            width, height = handle.size
    except Exception as error:  # Pillow raises several unrelated exception types
        raise ValueError(f"Image {asset_id} cannot be read: {error}") from error
    if width <= 0 or height <= 0:
        raise ValueError(f"Image {asset_id} has invalid dimensions")
    preferences = load_job(root).get("preferences") or {}
    if not isinstance(preferences, dict):
        raise ValueError("job preferences must be an object")
    image_policy.validate_asset_language(preferences, asset)
    asset = dict(asset)
    asset["origin"] = origin
    asset["resolved_path"] = str(image_path.resolve())
    return asset


def reviewed_image_ids(root, section_id, mode=None):
    """List asset IDs that would pass :func:`image_asset` for one section."""

    identifiers = []
    for record in _asset_records(root):
        identifier = record.get("id") if isinstance(record, dict) else None
        if not identifier:
            continue
        try:
            image_asset(root, identifier, section_id)
        except ValueError:
            continue
        identifiers.append(identifier)
    return identifiers


def excluded_assets(root):
    """Return excluded/pending assets with their recorded reasons for the host to explain."""

    result = []
    for record in _asset_records(root):
        if not isinstance(record, dict):
            continue
        state = record.get("state")
        if state not in {"excluded", "pending"}:
            continue
        reason = (
            record.get("exclusion_reason")
            or record.get("state_reason")
            or (record.get("candidate_review") or {}).get("reason")
            or "No reason recorded; ask the user before reconsidering this asset."
        )
        result.append({
            "id": record.get("id"),
            "state": state,
            "origin": record.get("origin"),
            "reason": reason,
            "original_path": record.get("original_path") or record.get("path"),
            "subject": record.get("subject"),
        })
    return result


# ---------------------------------------------------------------------------
# Content validation
# ---------------------------------------------------------------------------


def _validate_evidence_list(block, evidence):
    citations = block.get("evidence")
    if not isinstance(citations, list) or not citations:
        raise ValueError(f"{block.get('type', 'Content')} blocks require evidence IDs")
    if any(not isinstance(item, str) or not item.strip() for item in citations):
        raise ValueError("Evidence IDs must be nonempty strings")
    unknown = sorted(set(citations) - evidence)
    if unknown:
        raise ValueError("Unknown evidence IDs: " + ", ".join(unknown[:20]))


def _validate_text_block(block):
    text = block.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Text blocks cannot be empty")
    if "\ufffd" in text:
        raise ValueError("Text contains replacement characters; repair the source reading")


def _validate_table(block):
    rows = block.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Tables require nonempty rectangular rows")
    width = None
    for row in rows:
        if not isinstance(row, list) or not row:
            raise ValueError("Every table row must be a nonempty list of cells")
        if width is None:
            width = len(row)
        elif len(row) != width:
            raise ValueError("Tables must have rows of equal length")
        for cell in row:
            if isinstance(cell, bool) or not isinstance(cell, (str, int, float)):
                raise ValueError("Table cells must be strings or numbers")
            if isinstance(cell, str) and not cell.strip():
                raise ValueError("Table cells cannot be empty")
            if isinstance(cell, float) and not math.isfinite(cell):
                raise ValueError("Table cells cannot contain non-finite numbers")
    for key in ("header", "columns"):
        header = block.get(key)
        if header is not None:
            if not isinstance(header, list) or len(header) != width:
                raise ValueError("Table headers must match the column count")
            if any(not isinstance(cell, str) or not cell.strip() for cell in header):
                raise ValueError("Table header cells must be nonempty strings")
    block.setdefault("header", block.get("columns"))


def _validate_equation(block):
    _validate_text_block(block)
    text = block["text"]
    if "\ufffd" in text:
        raise ValueError("Equation text contains replacement characters; repair the source reading")
    if "math" in block and block["math"] is not None:
        try:
            math_layout.validate_math(block["math"])
        except ValueError as error:
            raise ValueError(f"Equation 'math' tree is invalid: {error}") from error


def validate_block(root, section, block, evidence=None):
    if not isinstance(block, dict):
        raise ValueError("Every content block must be an object")
    preferences = load_job(root).get("preferences", {})
    if evidence is None:
        evidence = evidence_ids_for(root)
    origin = block.get("origin", "lecture")
    if not isinstance(origin, str):
        raise ValueError("Block origin must be a string")
    if origin in {"additional_example", "added_example", "agent_example"}:
        origin = "example"
        block["origin"] = "example"
    if block.get("is_example") is True and origin != "example":
        block["origin"] = "example"
        origin = "example"
    if origin not in BLOCK_ORIGINS:
        raise ValueError(f"Unsupported block origin: {origin!r}")
    if origin == "example" and not preferences.get("allow_examples", False):
        raise ValueError("Additional examples are disabled for this job")
    kind = block.get("type")
    if kind not in BLOCK_TYPES:
        raise ValueError(f"Unsupported content block type: {kind!r}")

    if kind in {"image", "instruction"}:
        if kind == "instruction":
            instructions.validate(block)
        if image_mode(root) == "text":
            raise ValueError("Text-only output cannot contain inserted image objects")
        caption = block.get("caption")
        if not _nonempty(caption):
            raise ValueError("Image blocks require a caption close to the image")
        intended = block.get("section")
        if intended is not None and intended != section.get("id"):
            raise ValueError("An image block must name its actual containing section")
        asset = image_asset(root, block.get("asset_id"), section.get("id"))
        if asset.get("evidence") is not None:
            _validate_evidence_list(asset, evidence)
        placement = asset.get("placements", {}).get(section.get("id"), {})
        requested = block.get("placement")
        reviewed_placement = placement.get("placement")
        if requested is not None and (
            not isinstance(requested, str)
            or requested not in {"auto", "start", "end", "left", "right", "center"}
        ):
            raise ValueError("Image placement must be auto, start, end, left, right, or center")
        if requested and requested != "auto" and reviewed_placement and reviewed_placement != "auto" \
                and requested != reviewed_placement:
            raise ValueError("Image block placement conflicts with the reviewed placement record")
    elif kind == "table":
        _validate_table(block)
    elif kind == "bullets":
        items = block.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("Bullet blocks cannot be empty")
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError("Bullet items must be nonempty strings")
        if "ordered" in block and not isinstance(block["ordered"], bool):
            raise ValueError("Bullet 'ordered' must be true or false")
    elif kind == "equation":
        _validate_equation(block)
    else:
        _validate_text_block(block)

    _validate_evidence_list(block, evidence)
    return block


def evidence_ids_for(root):
    return {record["id"] for record in evidence_records(root)}


def _validate_omissions(content, evidence):
    omissions = content.get("omissions") or []
    if not isinstance(omissions, list):
        raise ValueError("Content omissions must be a list")
    seen = set()
    for omission in omissions:
        if not isinstance(omission, dict):
            raise ValueError("Every omission must be an object")
        identifier = omission.get("evidence_id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("Omission evidence_id must be a nonempty string")
        if identifier not in evidence:
            raise ValueError(f"Omission refers to nonexistent evidence: {identifier!r}")
        if identifier in seen:
            raise ValueError(f"Evidence {identifier!r} is omitted more than once")
        seen.add(identifier)
        kind = omission.get("kind")
        if not isinstance(kind, str) or kind not in OMISSION_KINDS:
            raise ValueError(
                f"Omitted evidence needs an allowed kind, not {kind!r}; "
                f"allowed: {', '.join(sorted(OMISSION_KINDS))}"
            )
        reason = omission.get("reason")
        if not isinstance(reason, str) or len(reason.strip()) < 8:
            raise ValueError("Omitted evidence needs a specific documented reason")
        if reason.strip().casefold() in {"n/a", "none", "misc", "todo", "not needed", "skip"}:
            raise ValueError("Omitted evidence needs a specific reason, not a placeholder")
    return omissions


def _validate_uncertainties(content, evidence, omissions):
    uncertainties = content.get("uncertainties") or []
    if not isinstance(uncertainties, list):
        raise ValueError("Content uncertainties must be a list")
    seen = set()
    for uncertainty in uncertainties:
        if not isinstance(uncertainty, dict):
            raise ValueError("Every uncertainty must be an object")
        identifier = uncertainty.get("evidence_id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("Uncertainty evidence_id must be a nonempty string")
        if identifier not in evidence:
            raise ValueError(f"Uncertainty refers to nonexistent evidence: {identifier!r}")
        if identifier in seen:
            raise ValueError(f"Evidence {identifier!r} appears twice in uncertainties")
        seen.add(identifier)
        if not _nonempty(uncertainty.get("description")):
            raise ValueError("Uncertainties require a description")
        location = uncertainty.get("location")
        if not _nonempty(location) and not isinstance(location, (int, float)):
            raise ValueError("Uncertainties require a source location or timestamp")
    unreadable = {
        omission["evidence_id"] for omission in omissions if omission["kind"] == "unreadable"
    }
    unreadable |= {
        record["id"] for record in evidence.values()
        if (isinstance(record.get("status"), str)
            and record.get("status") in {"unreadable", "failed"})
        or record.get("review_status") == "unreadable"
    }
    missing = sorted(unreadable - seen)
    if missing:
        raise ValueError(
            "Unreadable source evidence must be surfaced as uncertainties: "
            + ", ".join(missing[:20])
        )
    return uncertainties


def validate_content(root, content):
    """Validate structure, citations, coverage, omissions, and image policy.

    Returns a report describing what was proven and what the host must still
    review.  Every failure is a ``ValueError`` with an actionable message.
    """

    if not isinstance(content, dict):
        raise ValueError("content.json must contain an object")
    is_tutorial = instructions.tutorial_mode(content)
    if content.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ValueError("Unsupported content schema version")
    if not _nonempty(content.get("title")):
        raise ValueError("Content needs a nonempty title")
    if not _nonempty(content.get("language")):
        raise ValueError("Content needs the chosen output language")
    direction = content.get("direction")
    if not isinstance(direction, str) or direction not in {"rtl", "ltr"}:
        raise ValueError("Content needs an rtl or ltr direction")
    sections = content.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("Content needs at least one section")
    content.setdefault("omissions", [])
    content.setdefault("uncertainties", [])

    evidence = evidence_index(root)
    preferences = load_job(root).get("preferences") or {}
    if not isinstance(preferences, dict):
        raise ValueError("job preferences must be an object")
    raw_image_language = preferences.get("image_language")
    output_preferences.timestamps_visible(preferences)
    if raw_image_language is not None and not isinstance(raw_image_language, str):
        raise ValueError("preferences.image_language must be ar, en, or mixed")
    declared_image_language = None
    if isinstance(raw_image_language, str) and raw_image_language.strip():
        declared_image_language = image_policy.intake_requirements(preferences)[
            "normalized"
        ].get("image_language")
    if content.get("examples_permitted") is True and not preferences.get("allow_examples", False):
        raise ValueError("Content permits examples but the job disabled them")
    if content.get("examples_permitted") is False and any(
        block.get("origin") == "example" for _, block in iter_blocks(content)
    ):
        raise ValueError("Content contains examples while examples_permitted is false")

    covered = set()
    section_ids = set()
    block_count = 0
    blocks_by_type = {}
    examples = []
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("Every section must be an object")
        identifier = section.get("id")
        if not _nonempty(identifier):
            raise ValueError("Sections require nonempty string IDs")
        if identifier in section_ids:
            raise ValueError(f"Duplicate section ID: {identifier}")
        section_ids.add(identifier)
        if not _nonempty(section.get("title")):
            raise ValueError(f"Section {identifier!r} needs a nonempty title")
        output_preferences.section_source_range(section)
        blocks = section.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise ValueError(f"Section {identifier!r} needs at least one supported block")
        for block in blocks:
            if is_tutorial and isinstance(block, dict) and block.get("type") == "image":
                raise ValueError("Software tutorial pictures must use an instruction block with their steps")
            validate_block(root, section, block, evidence=set(evidence))
            citations = block.get("evidence") or []
            unknown = sorted(set(citations) - set(evidence))
            if unknown:
                raise ValueError("Unknown evidence IDs: " + ", ".join(unknown[:20]))
            covered.update(citations)
            block_count += 1
            kind = block.get("type")
            blocks_by_type[kind] = blocks_by_type.get(kind, 0) + 1
            if block.get("origin") == "example":
                examples.append({"section": identifier, "evidence": list(citations)})

    omissions = _validate_omissions(content, set(evidence))
    uncertainties = _validate_uncertainties(content, evidence, omissions)
    covered.update(omission["evidence_id"] for omission in omissions)
    covered.update(uncertainty["evidence_id"] for uncertainty in uncertainties)

    required = {
        identifier for identifier, record in evidence.items() if evidence_is_substantive(record)
    }
    non_substantive = sorted(set(evidence) - required)
    missing = sorted(required - covered)
    if missing:
        raise ValueError(
            "Uncovered substantive source evidence (cite it or record a documented omission): "
            + ", ".join(missing[:30])
            + (" ..." if len(missing) > 30 else "")
        )

    warnings = []
    language = str(content.get("language", "")).lower()
    if ("ar" in language or language == "mixed") and content.get("direction") == "ltr":
        warnings.append("Arabic/mixed content is marked ltr; verify the intended reading direction")
    if ("en" == language) and content.get("direction") == "rtl":
        warnings.append("English content is marked rtl; verify the intended reading direction")
    if examples:
        warnings.append(
            "Additional examples are present and marked; confirm they are not attributed to the lecturer"
        )
    if omissions:
        warnings.append(
            "Omissions are authoring decisions; compare each reason with the source. "
            "Structural validation cannot detect a plausible but false exclusion reason."
        )
    if uncertainties:
        warnings.append("Unresolved source notes are surfaced; confirm their wording and locations")
    mode = image_mode(root)
    if (blocks_by_type.get("image") or blocks_by_type.get("instruction")) and mode != "text" and declared_image_language is None:
        warnings.append(
            "Images are used without an image_language preference; only legacy jobs may omit it, "
            "and their labels were not validated against a requested label language."
        )

    checks_proven = [
        "block structure and required fields",
        "evidence identifiers exist",
        "substantive evidence is cited or explicitly omitted",
        "omissions use documented kinds and reasons",
        "unreadable evidence is surfaced as uncertainty",
        "image assets are accepted, inspected, and reviewed for their section",
        "text-only policy contains no image blocks",
        "example blocks require the job-level permission",
    ]
    if (blocks_by_type.get("image") or blocks_by_type.get("instruction")) and declared_image_language is not None:
        checks_proven.append(
            "used image labels are explicitly inspected and match the requested image language"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "title": content["title"],
        "language": content["language"],
        "direction": content["direction"],
        "sections": len(section_ids),
        "blocks": block_count,
        "blocks_by_type": dict(sorted(blocks_by_type.items())),
        "evidence_count": len(evidence),
        "substantive_evidence_count": len(required),
        "cited_evidence": len(covered - {o["evidence_id"] for o in omissions}),
        "omitted_evidence": len(omissions),
        "omissions": [
            {"evidence_id": item["evidence_id"], "kind": item["kind"], "reason": item["reason"]}
            for item in omissions
        ],
        "uncertainties": len(uncertainties),
        "non_substantive_evidence": non_substantive,
        "examples": {"permitted": bool(preferences.get("allow_examples")), "items": examples},
        "image_mode": mode,
        "image_language": declared_image_language,
        "checks_proven": checks_proven,
        "semantic_completeness_verified": False,
        "host_review_required": [
            "confirm each citation actually supports its block",
            "confirm the complete lecture explanation is present and correct",
            "review diagrams, equations, numbers, and terminology at full size",
            SEMANTIC_REVIEW_NOTICE,
        ],
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Source review readiness
# ---------------------------------------------------------------------------


def _frames_state(root):
    path = Path(root) / "work" / "frames-state.json"
    if not path.exists():
        raise ValueError("Extract and review the lecture screen before rendering")
    state = read_json(path)
    if not isinstance(state, dict) or not isinstance(state.get("frames"), list):
        raise ValueError("work/frames-state.json must contain a frames array")
    if not state["frames"]:
        raise ValueError("Frame extraction has not produced any reviewed candidates")
    if state.get("complete") is False:
        raise ValueError("Frame extraction is explicitly incomplete; finish the requested interval")
    return state


def _requested_end(root, state):
    job = load_job(root)
    media = job.get("media") or {}
    for value in (state.get("requested_end"), media.get("end"),
                  job.get("preferences", {}).get("end")):
        if isinstance(value, (int, float)) and math.isfinite(value):
            return float(value)
    raise ValueError(
        "Cannot establish the requested interval end from media metadata, "
        "preferences, or frames-state.json"
    )


def _screen_reviews(root):
    records = optional_records(root, "screen.json")
    reviews = {}
    for index, record in enumerate(records):
        identifier = _record_id(record, "screen.json", index)
        if identifier in reviews:
            raise ValueError(f"Duplicate screen review ID: {identifier}")
        reviews[identifier] = record
    return reviews


def _status_of(record):
    return record.get("review_status") or record.get("status")


def _has_review_observation(record):
    for key in ("observations", "observation", "text", "description", "notes", "review_notes",
                "visual_observation", "diagram", "content", "summary"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and value:
            return True
        if isinstance(value, dict) and value:
            return True
    return False


def _uncertainty_of(record):
    for key in ("uncertainty", "uncertainty_note", "unreadable_reason", "limitation"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _validate_refinements(root, state, frames):
    """Validate recovery refinements and return unique recovery representative IDs."""
    refinements = state.get("refinements")
    adaptive_windows = state.get("adaptive_windows")
    if refinements is None and adaptive_windows is None:
        return []
    if refinements is None:
        refinements = []
    if not isinstance(refinements, list):
        raise ValueError("work/frames-state.json refinements must be an array")
    if adaptive_windows is None:
        adaptive_windows = {}
    if not isinstance(adaptive_windows, dict):
        raise ValueError("work/frames-state.json adaptive_windows must be an object")

    baseline_times = [float(f["requested"]) for f in frames]
    representatives = state.get("representatives")
    if refinements:
        if not isinstance(representatives, list) or not representatives:
            raise ValueError("work/frames-state.json representatives must be a list when refinements exist")
        rep_map = {}
        for r in representatives:
            if not isinstance(r, dict) or not _nonempty(r.get("frame_id")):
                raise ValueError("Representative record is invalid")
            rep_map[r["frame_id"]] = r
    else:
        rep_map = {
            r["frame_id"]: r
            for r in (representatives if isinstance(representatives, list) else [])
            if isinstance(r, dict) and _nonempty(r.get("frame_id"))
        }

    validated_windows = {}
    for wkey, w_entry in adaptive_windows.items():
        if not isinstance(w_entry, dict):
            raise ValueError(f"Adaptive window {wkey!r} must be an object")
        w_start = w_entry.get("window_start")
        w_end = w_entry.get("window_end")
        b_idx = w_entry.get("baseline_index")
        if isinstance(b_idx, bool) or not isinstance(b_idx, int):
            raise ValueError("baseline_index must be an integer, not boolean")
        if isinstance(w_start, bool) or isinstance(w_end, bool):
            raise ValueError("Window endpoints cannot be booleans")
        if not isinstance(w_start, (int, float)) or not isinstance(w_end, (int, float)):
            raise ValueError(f"Adaptive window {wkey!r} has invalid endpoints")
        w_start, w_end = float(w_start), float(w_end)
        if not math.isfinite(w_start) or not math.isfinite(w_end) or w_start < 0 or w_start >= w_end:
            raise ValueError(f"Adaptive window {wkey!r} has invalid range")
        canonical_key = f"{w_start:.6f}:{w_end:.6f}"
        if wkey != canonical_key:
            raise ValueError(f"Adaptive window key {wkey!r} does not match canonical {canonical_key!r}")
        if (w_end - w_start) > DEFAULT_INTERVAL + 1e-6:
            raise ValueError(f"Adaptive window {wkey!r} exceeds maximum {DEFAULT_INTERVAL}s")

        if (
            b_idx < 0
            or b_idx >= len(baseline_times) - 1
            or abs(baseline_times[b_idx] - w_start) > 1e-6
            or abs(baseline_times[b_idx + 1] - w_end) > 1e-6
        ):
            raise ValueError(f"Adaptive window {wkey!r} does not match adjacent baseline frames")

        completed = w_entry.get("completed_intervals")
        if not isinstance(completed, list):
            raise ValueError(f"Adaptive window {wkey!r} completed_intervals must be a list")
        if completed:
            validate_refinement_level(completed, completed[-1])

        validated_windows[canonical_key] = (w_start, w_end, completed)

    resolved_root = Path(root).resolve()
    expected_requests = set()
    for wkey, (w_start, w_end, completed) in validated_windows.items():
        prior_requested = [w_start, w_end]
        for lvl in completed:
            lvl_float = float(lvl)
            planned_points = plan_refinement(w_start, w_end, lvl_float, prior_requested)
            for pt in planned_points:
                expected_requests.add((wkey, lvl_float, round(pt, 6)))
                prior_requested.append(pt)

    seen_rows = set()
    extra_ids = []
    for idx, row in enumerate(refinements):
        if not isinstance(row, dict):
            raise ValueError(f"Refinement row {idx} must be an object")
        w_start = row.get("window_start")
        w_end = row.get("window_end")
        req = row.get("requested")
        interval = row.get("interval")
        reason = row.get("reason")
        fid = row.get("frame_id")
        path = row.get("path")
        duplicate = row.get("duplicate")
        decoded = row.get("decoded", row.get("timestamp"))

        if (
            isinstance(w_start, bool)
            or isinstance(w_end, bool)
            or isinstance(req, bool)
            or isinstance(interval, bool)
        ):
            raise ValueError(f"Refinement row {idx} contains boolean timestamp values")
        if (
            not isinstance(w_start, (int, float))
            or not isinstance(w_end, (int, float))
            or not isinstance(req, (int, float))
            or not isinstance(interval, (int, float))
        ):
            raise ValueError(f"Refinement row {idx} missing numeric timestamps")
        w_start, w_end, req, interval = float(w_start), float(w_end), float(req), float(interval)
        if not (math.isfinite(w_start) and math.isfinite(w_end) and math.isfinite(req) and math.isfinite(interval)):
            raise ValueError(f"Refinement row {idx} has non-finite values")

        if (
            isinstance(decoded, bool)
            or not isinstance(decoded, (int, float))
            or not math.isfinite(decoded)
            or decoded < 0
        ):
            raise ValueError(f"Refinement row {idx} has invalid decoded timestamp: {decoded!r}")

        wkey = f"{w_start:.6f}:{w_end:.6f}"
        if wkey not in validated_windows:
            raise ValueError(f"Refinement row {idx} window {wkey} is not in adaptive_windows")

        row_key = (wkey, interval, round(req, 6))
        if row_key in seen_rows:
            raise ValueError(f"Duplicate refinement row for window and request: {row_key}")
        seen_rows.add(row_key)

        if row_key not in expected_requests:
            raise ValueError(
                f"Refinement row {idx} for {wkey} at {interval}s does not match any planned completed point"
            )

        if not _nonempty(reason):
            raise ValueError(f"Refinement row {idx} requires a nonempty reason")
        if not _nonempty(fid):
            raise ValueError(f"Refinement row {idx} requires a nonempty frame_id")
        if not _nonempty(path):
            raise ValueError(f"Refinement row {idx} requires a nonempty path")
        if not isinstance(duplicate, bool):
            raise ValueError(f"Refinement row {idx} duplicate flag must be a boolean")

        if fid not in rep_map:
            raise ValueError(f"Refinement row {idx} refers to unknown representative {fid}")
        if rep_map[fid].get("path") != path:
            raise ValueError(f"Refinement row {idx} path does not match representative {fid}")

        img_path = _job_file_path(root, path)
        if img_path is None:
            raise ValueError(f"Refinement row {idx} image path is empty")
        try:
            resolved_img = img_path.resolve()
            resolved_img.relative_to(resolved_root)
        except (ValueError, OSError):
            raise ValueError(f"Refinement row {idx} path escapes job root: {path}")
        if not resolved_img.is_file():
            raise ValueError(f"Refinement row {idx} image file does not exist: {path}")

        if fid not in extra_ids:
            extra_ids.append(fid)

    missing_requests = expected_requests - seen_rows
    if missing_requests:
        raise ValueError(
            f"Completed refinement level has missing planned rows: {sorted(missing_requests)[:5]}"
        )

    return extra_ids


def require_screen_review(root):
    """Reject missing or incomplete visual source review before final output."""

    state = _frames_state(root)
    end = _requested_end(root, state)
    frames = state["frames"]
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise ValueError(f"frames-state.json frame {index} must be an object")
        requested = frame.get("requested")
        if not isinstance(requested, (int, float)) or not math.isfinite(requested):
            raise ValueError(f"Frame {index} has no finite requested timestamp")
        if not _nonempty(frame.get("frame_id")):
            raise ValueError(f"Frame {index} has no representative frame_id")
    last = float(frames[-1]["requested"])
    if last + 1e-6 < end:
        raise ValueError(
            f"Frame extraction stops at {last:.3f}s but the requested interval ends at {end:.3f}s"
        )
    job = load_job(root)
    media = job.get("media") or {}
    preferences = job.get("preferences") or {}
    if not isinstance(preferences, dict):
        raise ValueError("job preferences must be an object")

    def first_number(*values):
        for value in values:
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and math.isfinite(value):
                return float(value)
        return None

    start = first_number(state.get("start"), media.get("start"), preferences.get("start"))
    if start is None:
        start = 0.0
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    interval = first_number(
        state.get("interval"),
        settings.get("interval"),
        preferences.get("frame_interval_seconds"),
    )
    if interval is None:
        interval = DEFAULT_INTERVAL
    if interval <= 0 or not math.isfinite(interval):
        raise ValueError("Frame sampling interval must be positive and finite")
    try:
        expected_schedule = list(sample_times(start, end, interval))
    except ValueError as error:
        raise ValueError(f"Invalid frame sampling interval or range: {error}") from error
    requested_values = [float(frame["requested"]) for frame in frames]
    if len(requested_values) != len(expected_schedule):
        raise ValueError(
            f"Frame schedule has {len(requested_values)} candidates but the selected "
            f"{start:.3f}-{end:.3f}s interval at {interval:.3f}s requires "
            f"{len(expected_schedule)} ordered candidates"
        )
    for index, (actual, expected) in enumerate(zip(requested_values, expected_schedule)):
        if abs(actual - expected) > 1e-6:
            raise ValueError(
                "Frame candidates do not form the required ordered sampling schedule: "
                f"candidate {index} is {actual:.3f}s, expected {expected:.3f}s"
            )

    reviews = _screen_reviews(root)
    retained = []
    for frame in frames:
        if frame["frame_id"] not in retained:
            retained.append(frame["frame_id"])
    for identifier in _validate_refinements(root, state, frames):
        if identifier not in retained:
            retained.append(identifier)
    missing = [identifier for identifier in retained if identifier not in reviews]
    if missing:
        raise ValueError(
            "Retained screen representatives are not reviewed: " + ", ".join(missing[:20])
        )
    unknown = sorted(set(reviews) - set(retained))
    if unknown:
        raise ValueError(
            "Screen reviews refer to frames that are not retained representatives: "
            + ", ".join(unknown[:20])
        )
    for identifier in retained:
        review = reviews[identifier]
        status = _status_of(review)
        if not isinstance(status, str) or status not in REVIEWED_STATUSES:
            raise ValueError(
                f"Screen evidence {identifier} has review status {status!r}, not reviewed/unreadable"
            )
        if status == "unreadable":
            if not _uncertainty_of(review):
                raise ValueError(
                    f"Unreadable screen evidence {identifier} must describe its uncertainty"
                )
        elif not _has_review_observation(review):
            raise ValueError(
                f"Screen evidence {identifier} has no recorded observation; a status alone is not review"
            )

    return {
        "retained_representatives": len(retained),
        "reviewed_representatives": len(retained),
        "review_records": len(reviews),
        "candidates": len(frames),
        "baseline_candidates": len(frames),
        "refinement_candidates": len(state.get("refinements") or []),
        "requested_end": end,
        "last_requested": last,
        "coverage_complete": True,
    }


SPEECH_INCOMPLETE_STATUSES = {
    "partial",
    "running",
    "transcribing",
    "incomplete",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "stopped",
}
SPEECH_COMPLETE_STATUSES = {"transcribed", "complete", "completed"}
SPEECH_NO_SPEECH_STATUSES = {
    "no_speech",
    "no_speech_reviewed",
    "no_audio",
    "not_applicable",
}


def _speech_checkpoints(root):
    job = load_job(root)
    stage = job.get("stages", {}).get("speech")
    candidates = []
    if isinstance(stage, dict):
        candidates.append(("job.stages.speech", stage))
    elif isinstance(stage, str):
        candidates.append(("job.stages.speech", {"status": stage}))
    for filename in ("speech-state.json", "audio-state.json"):
        path = Path(root) / "work" / filename
        if path.exists():
            document = read_json(path)
            if isinstance(document, dict):
                candidates.append((f"work/{filename}", document))
            else:
                raise ValueError(f"work/{filename} must contain a speech state object")
    return candidates


def _speech_progress_error(marker):
    status = marker.get("status")
    if status is not None and not isinstance(status, str):
        return "has a malformed speech status"
    if marker.get("complete") is False:
        return "is explicitly incomplete"
    if status in SPEECH_INCOMPLETE_STATUSES:
        return f"reports status {status!r}"
    if status in SPEECH_NO_SPEECH_STATUSES or status == "no_speech_detected":
        return f"reports {status!r} while a nonempty transcript exists"
    if status is not None and status not in SPEECH_COMPLETE_STATUSES:
        return f"uses an unrecognized speech status {status!r}"
    total = marker.get("chunks_total")
    completed = marker.get("chunks_completed")
    try:
        if total is not None and completed is not None:
            total_value = int(total)
            completed_value = int(completed)
            if total_value < 0 or completed_value < 0:
                return "has invalid chunk counters"
            if completed_value < total_value:
                return f"has only {completed_value}/{total_value} completed chunks"
        completed_chunks = marker.get("completed_chunks")
        if completed_chunks is not None and total is not None:
            if not isinstance(completed_chunks, list):
                return "has malformed completed_chunks"
            if len(completed_chunks) < int(total):
                return f"lists only {len(completed_chunks)}/{int(total)} completed chunks"
    except (TypeError, ValueError):
        return "has malformed chunk counters"
    return None


def _speech_marker(root):
    for _source, marker in _speech_checkpoints(root):
        status = marker.get("status")
        if not isinstance(status, str):
            continue
        if status in SPEECH_NO_SPEECH_STATUSES:
            if marker.get("complete") is False:
                raise ValueError("A no-speech/no-audio marker cannot be explicitly incomplete")
            reason = marker.get("reason") or marker.get("note") or marker.get("review_note")
            if not _nonempty(reason):
                raise ValueError("A no-speech/no-audio marker needs a review reason")
            return marker
    return None


def require_audio_review(root):
    """Require completed speech evidence or an explicit reviewed no-speech record."""

    transcript_path = Path(root) / "work" / "transcript.json"
    checkpoints = _speech_checkpoints(root)
    if transcript_path.exists():
        records = optional_records(root, "transcript.json")
        if records and any(
            isinstance(record, dict) and isinstance(record.get("text"), str) and record["text"].strip()
            for record in records
        ):
            for source, marker in checkpoints:
                problem = _speech_progress_error(marker)
                if problem:
                    raise ValueError(
                        f"Speech checkpoint {source} {problem}; a nonempty transcript does not "
                        "make partial, failed, or conflicting source work complete"
                    )
            return {
                "status": "transcribed",
                "records": len(records),
                "checkpoints": len(checkpoints),
            }
        marker = _speech_marker(root)
        if marker is not None:
            return {"status": marker.get("status"), "records": 0}
        raise ValueError("transcript.json is empty; transcribe audio or record a reviewed no-speech status")
    marker = _speech_marker(root)
    if marker is not None:
        return {"status": marker.get("status"), "records": 0}
    raise ValueError(
        "Audio/speech review is incomplete: no usable transcript and no reviewed no-speech record"
    )


def _reference_needs_visual_review(record):
    for key in ("requires_visual_review", "needs_visual_review", "visual_review_required"):
        if record.get(key) is True:
            return True
    nested = record.get("review")
    if isinstance(nested, dict) and nested.get("visual_required") is True:
        return True
    kind = record.get("kind")
    if record.get("has_images") is True and (
        not isinstance(kind, str) or kind not in {"image", "figure"}
    ):
        return True
    return isinstance(kind, str) and kind in {
        "diagram", "scanned_page", "scanned-page", "figure", "image", "photo"
    }


def _reference_status(record):
    nested = record.get("review")
    return (
        record.get("review_status")
        or record.get("status")
        or (nested.get("status") if isinstance(nested, dict) else None)
    )


def require_reference_review(root):
    """Reject imported pages/diagrams silently treated as understood."""

    records = optional_records(root, "references.json")
    checked = 0
    for index, record in enumerate(records):
        _record_id(record, "references.json", index)
        if not _reference_needs_visual_review(record):
            continue
        checked += 1
        status = _reference_status(record)
        if not isinstance(status, str) or status not in REVIEWED_STATUSES:
            raise ValueError(
                f"Reference evidence {record['id']} requires visual review but has status {status!r}"
            )
        if status == "reviewed" and not _has_review_observation(record):
            raise ValueError(
                f"Reference evidence {record['id']} has no recorded observation; "
                "a status alone is not visual review"
            )
        if status == "unreadable" and not _uncertainty_of(record):
            raise ValueError(
                f"Unreadable reference evidence {record['id']} must describe its uncertainty"
            )
    return {"records": len(records), "visual_review_required": checked}


def require_output_readiness(root, content=None):
    """Combined final-output gate for speech, screen, and reference review.

    Image permissions are separate: text-only output still requires visual source
    review, and review readiness never authorises inserting pictures.
    """

    speech = require_audio_review(root)
    screen = require_screen_review(root)
    references = require_reference_review(root)
    content_report = validate_content(root, content) if content is not None else None
    return {
        "ok": True,
        "speech": speech,
        "screen": screen,
        "references": references,
        "content": content_report,
        "image_mode": image_mode(root),
        "output_readiness": True,
        "note": (
            "Artifact-based readiness only. A contact sheet, a single frame, or one status "
            "field does not prove that a long lecture was meaningfully reviewed."
        ),
    }


# ---------------------------------------------------------------------------
# Preview binding and acceptance
# ---------------------------------------------------------------------------


def _style_document(style, root=None):
    def normalize_if_complete(document):
        if not isinstance(document, dict):
            return document
        if all(key in document for key in ("fonts", "palette", "page", "slides")):
            from . import styles

            try:
                return styles.normalize_style(document)
            except ValueError:
                return json.loads(json.dumps(document))
        return json.loads(json.dumps(document))

    if style is None:
        from . import styles

        return styles.resolve_style(None)
    if isinstance(style, dict):
        return normalize_if_complete(style)
    if isinstance(style, (str, Path)):
        path = Path(style)
        if not path.is_absolute() and root is not None:
            candidate = Path(root) / path
            if candidate.is_file():
                path = candidate
        if not path.is_file():
            from . import styles

            try:
                return styles.resolve_style(str(style))
            except ValueError as error:
                raise ValueError(f"Style file not found and not a built-in style: {style}") from error
        document = read_json(path)
        if not isinstance(document, dict):
            raise ValueError("A style file must contain a JSON object")
        return normalize_if_complete(document)
    raise ValueError("Style must be a style object or a path to a style JSON file")


def _used_assets(root, content):
    assets = {record.get("id"): record for record in _asset_records(root)
              if isinstance(record, dict) and record.get("id")}
    used = []
    seen = set()
    for section, block in iter_blocks(content):
        if block.get("type") not in {"image", "instruction"}:
            continue
        asset_id = block.get("asset_id")
        key = (asset_id, section.get("id"))
        if key in seen:
            continue
        seen.add(key)
        asset = assets.get(asset_id)
        if asset is None:
            used.append({"id": asset_id, "section": section.get("id"),
                         "missing": True})
            continue
        path = _job_file_path(root, asset.get("path"))
        used.append({
            "id": asset_id,
            "section": section.get("id"),
            "origin": asset.get("origin"),
            "state": asset.get("state"),
            "sha256": file_digest(path) if path is not None and path.is_file() else None,
            "candidate_review": asset.get("candidate_review"),
            "language_review": asset.get("language_review"),
            "placement": (asset.get("placements") or {}).get(section.get("id")),
            "caption": block.get("caption"),
            "source_name": asset.get("source_name"),
            "source_url": asset.get("source_url"),
            "source_page": asset.get("source_page"),
            "attribution": asset.get("attribution"),
            "creator": asset.get("creator"),
            "license": asset.get("license"),
            "licensing": asset.get("licensing"),
            "origin": asset.get("origin"),
            "original_path": asset.get("original_path"),
            "path": asset.get("path"),
            "timestamp": asset.get("timestamp"),
        })
    return sorted(used, key=lambda item: (str(item.get("id")), str(item.get("section"))))


def preview_binding(root, content, style):
    """Return the exact binding document that a preview acceptance refers to.

    Only assets actually referenced by ``content`` are included, so an unrelated
    excluded/pending asset does not invalidate an unchanged used-image preview.
    Only evidence referenced by the content is included, so later extraction of
    unused evidence does not invalidate an otherwise unchanged preview.
    """

    job = load_job(root)
    evidence = evidence_index(root)
    cited = sorted(cited_evidence_ids(content))
    relevant_evidence = []
    for identifier in cited:
        record = evidence.get(identifier)
        if record is not None:
            relevant_evidence.append(record)
    return {
        "schema_version": SCHEMA_VERSION,
        "content": content,
        "style": _style_document(style, root),
        "assets": _used_assets(root, content),
        "evidence": relevant_evidence,
        "preferences": job.get("preferences", {}),
        "source": {
            "sha256": (job.get("media") or {}).get("sha256"),
            "start": (job.get("media") or {}).get("start"),
            "end": (job.get("media") or {}).get("end"),
        },
    }


def approval_key(root, content, style):
    return json_digest(preview_binding(root, content, style))


def _readable_preview(path):
    if not path.is_file():
        raise ValueError(f"Preview file not found: {path}")
    if path.stat().st_size == 0:
        raise ValueError("Preview file is empty")
    suffix = path.suffix.lower()
    if suffix not in PREVIEW_SUFFIXES:
        raise ValueError("Preview must be a readable PDF or PPTX file")
    if suffix == ".pdf":
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("Preview is not a PDF file")
        try:
            import pypdf

            reader = pypdf.PdfReader(str(path), strict=False)
            if len(reader.pages) < 1:
                raise ValueError("Preview PDF has no pages")
            return {"format": "pdf", "pages": len(reader.pages)}
        except ValueError:
            raise
        except Exception as error:
            raise ValueError(f"Preview PDF cannot be read: {error}") from error
    try:
        from pptx import Presentation

        deck = Presentation(str(path))
        if len(deck.slides) < 1:
            raise ValueError("Preview PPTX has no slides")
        return {"format": "pptx", "slides": len(deck.slides)}
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"Preview PPTX cannot be read: {error}") from error


def _preview_path(root, preview_path=None):
    if preview_path is None:
        pdf = Path(root) / "work" / "preview.pdf"
        if pdf.is_file():
            return pdf
        slides = Path(root) / "work" / "preview-slides.pptx"
        if slides.is_file():
            return slides
        return pdf
    path = Path(preview_path)
    if not path.is_absolute():
        path = Path(root) / path
    return path


def _relative_to_job(root, path):
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def record_preview(root, content, style, preview_path=None, source=None):
    """Bind an actual rendered preview file to the current material.

    ``preview_path`` defaults to ``work/preview.pdf``.  When the renderer wrote
    ``work/preview-meta.json`` the recorded binding and file hash must match it,
    preventing a stale preview from being adopted silently.
    """

    validate_content(root, content)
    path = _preview_path(root, preview_path)
    details = _readable_preview(path)
    binding = preview_binding(root, content, style)
    key = json_digest(binding)
    digest = file_digest(path)

    meta_path = Path(root) / "work" / "preview-meta.json"
    meta = read_json(meta_path) if meta_path.exists() else None
    if isinstance(meta, dict):
        meta_key = meta.get("binding_digest") or meta.get("key")
        if meta_key and meta_key != key:
            raise ValueError(
                "Preview was rendered for different content, style, preferences, or used assets; "
                "rerender the preview before recording it"
            )
        expected = None
        files = meta.get("files")
        if isinstance(files, dict):
            expected = files.get(_relative_to_job(root, path))
            if expected is None:
                expected = files.get(path.name)
        if expected is None:
            expected = meta.get("preview_sha256")
        if expected and expected != digest:
            raise ValueError("Preview file was replaced after rendering; rerender it")

    job = load_job(root)
    job["preview"] = {
        "schema_version": SCHEMA_VERSION,
        "key": key,
        "binding_digest": key,
        "file": _relative_to_job(root, path),
        "format": details["format"],
        "pages": details.get("pages") or details.get("slides"),
        "file_sha256": digest,
        "accepted": False,
        "accepted_at": None,
        "acceptance_source": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "renderer": source or (meta.get("renderer") if isinstance(meta, dict) else None),
        "summary": {
            "sections": len(content.get("sections", [])),
            "used_assets": [item["id"] for item in binding["assets"]],
            "language": content.get("language"),
            "direction": content.get("direction"),
        },
    }
    save_job(root, job)
    return job["preview"]


def _current_preview(root):
    job = load_job(root)
    preview = job.get("preview")
    if not preview:
        raise ValueError("No preview has been rendered and shown for this job")
    return job, preview


def _verify_preview_file(root, preview):
    recorded = preview.get("file")
    if not isinstance(recorded, str) or not recorded.strip():
        raise ValueError("The recorded preview has no file identity; render and record it again")
    path = Path(recorded)
    if not path.is_absolute():
        path = Path(root) / recorded
    details = _readable_preview(path)
    digest = file_digest(path)
    if digest != preview.get("file_sha256"):
        raise ValueError("Preview file changed after it was shown; render and show the current preview again")
    return path, details, digest


def accept_preview(root, content, style, confirmed_by="host-confirmed user acceptance", note=None):
    """Record explicit external acceptance of the currently rendered preview.

    The host must obtain the user's real acceptance; this function records it.
    It cannot independently prove that a human was shown the file.
    """

    job, preview = _current_preview(root)
    key = approval_key(root, content, style)
    if preview.get("key") != key:
        raise ValueError("No matching preview; render and show the current draft first")
    path, details, digest = _verify_preview_file(root, preview)
    preview["accepted"] = True
    preview["accepted_at"] = datetime.now(timezone.utc).isoformat()
    preview["acceptance_source"] = str(confirmed_by)
    if note:
        preview["acceptance_note"] = str(note)
    preview["format"] = details["format"]
    preview["pages"] = details.get("pages") or details.get("slides")
    preview["file"] = _relative_to_job(root, path)
    preview["binding_summary"] = {
        "content_sections": len(content.get("sections", [])),
        "used_assets": [item["id"] for item in _used_assets(root, content)],
        "style": (preview_binding(root, content, style)["style"] or {}).get("name"),
    }
    save_job(root, job)
    return preview


def require_accepted_preview(root, content, style):
    """Enforce a current, matching, still-existing preview acceptance."""

    job, preview = _current_preview(root)
    if not preview.get("accepted"):
        raise ValueError("Wait for explicit user acceptance of the current teaching/design preview")
    key = approval_key(root, content, style)
    if preview.get("key") != key:
        raise ValueError(
            "Content, style, preferences, used-image bytes, or cited evidence changed after "
            "acceptance; render a new preview and ask again"
        )
    path, details, _ = _verify_preview_file(root, preview)

    meta_path = Path(root) / "work" / "preview-meta.json"
    if meta_path.exists():
        meta = read_json(meta_path)
        if isinstance(meta, dict):
            meta_key = meta.get("binding_digest") or meta.get("key")
            if meta_key and meta_key != key:
                raise ValueError("Preview metadata does not match the accepted material; rerender")
    return {
        "accepted": True,
        "file": _relative_to_job(root, path),
        "format": details["format"],
        "pages": details.get("pages") or details.get("slides"),
        "accepted_at": preview.get("accepted_at"),
        "binding_digest": key,
    }


def prepare_render(root, content, style, require_acceptance=True):
    """Run all final-output gates before complete document assembly."""

    report = require_output_readiness(root, content)
    acceptance = require_accepted_preview(root, content, style) if require_acceptance else None
    return {"readiness": report, "preview_acceptance": acceptance}
