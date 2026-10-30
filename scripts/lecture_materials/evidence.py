"""Original-material evidence, bounded review packets, and review merging.

Importing a PDF/PPTX only extracts text and readable page/slide evidence.  It
never declares diagrams or scanned pages understood.  Review files are accepted
only for known retained frame/screen IDs or known reference IDs; a packet or a
contact sheet is navigation, not review.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
from datetime import datetime, timezone

from .jobs import (
    add_attachment,
    file_digest,
    json_digest,
    load_job,
    read_json,
    register_generated,
    save_job,
    unregister_generated,
    write_json,
)
from .timing import timestamp


SCHEMA_VERSION = 1
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif"}
STATUSES = {"reviewed", "unreadable", "pending", "extraction_failed"}
REVIEWED = {"reviewed", "unreadable"}
OBSERVATION_KEYS = (
    "observations",
    "observation",
    "text",
    "description",
    "notes",
    "review_notes",
    "visual_observation",
    "diagram",
    "content",
    "summary",
)


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _observation(value):
    for key in OBSERVATION_KEYS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, list) and item:
            return " | ".join(str(entry) for entry in item if str(entry).strip())
    return ""


def _uncertainty(value):
    for key in ("uncertainty", "uncertainty_note", "unreadable_reason", "limitation", "reason"):
        item = value.get(key)
        if _nonempty(item):
            return item.strip()
    return ""


def _load_references(root):
    path = Path(root) / "work" / "references.json"
    if not path.is_file():
        return []
    document = read_json(path)
    if not isinstance(document, list):
        raise ValueError("work/references.json must contain a JSON array")
    return document


def _write_references(root, records):
    path = Path(root) / "work" / "references.json"
    write_json(path, records)
    register_generated(root, path)
    return path


def _next_id(existing, prefix="ref"):
    highest = 0
    for record in existing:
        identifier = record.get("id")
        match = re.match(rf"{prefix}-(\d+)", str(identifier))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{prefix}-{highest + 1:06}"


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:80] or "reference"


def _register_extracted(root, path, record_id):
    relative = path.relative_to(Path(root)).as_posix()
    register_generated(root, path)
    return relative


def _import_pdf(root, path, digest, existing):
    try:
        import pymupdf
    except Exception as error:
        raise ValueError(
            "PDF reference import requires PyMuPDF (import pymupdf) in the environment"
        ) from error
    try:
        document = pymupdf.open(path)
    except Exception as error:
        raise ValueError(f"Could not open PDF reference {path}: {error}") from error
    records = []
    try:
        if document.is_encrypted and document.needs_pass:
            raise ValueError("Encrypted PDF references are not supported; supply a password-free copy")
        if document.page_count < 1:
            raise ValueError("PDF reference has no pages")
        base = _next_id(existing)
        directory = Path(root) / "work" / "references"
        directory.mkdir(parents=True, exist_ok=True)
        for page_index in range(document.page_count):
            identifier = f"{base}-p{page_index + 1:04}"
            page = document.load_page(page_index)
            error_text = None
            try:
                text = page.get_text("text").strip()
            except Exception as error:
                text = ""
                error_text = str(error)
            images = []
            try:
                images = page.get_images(full=True)
            except Exception:
                images = []
            drawings = []
            try:
                drawings = page.get_drawings()
            except Exception as error:
                error_text = (error_text + "; " if error_text else "") + (
                    f"vector drawing inspection failed: {error}"
                )
                drawings = []
            vector_drawing_count = len(drawings)
            requires_review = (
                bool(images)
                or vector_drawing_count > 0
                or len(text) < 40
                or error_text is not None
            )
            kind = (
                "scanned_page"
                if len(text) < 40 and images
                else "diagram"
                if images or vector_drawing_count
                else "page"
            )
            rendered = None
            text_path = None
            if requires_review:
                try:
                    pixmap = page.get_pixmap(dpi=100, alpha=False)
                    rendered_path = directory / f"{identifier}.png"
                    pixmap.save(str(rendered_path))
                    rendered = _register_extracted(root, rendered_path, identifier)
                except Exception as error:
                    error_text = (error_text + "; " if error_text else "") + f"page render failed: {error}"
            if text:
                text_path_file = directory / f"{identifier}.txt"
                text_path_file.write_text(text + "\n", encoding="utf-8")
                text_path = _register_extracted(root, text_path_file, identifier)
            records.append(
                {
                    "id": identifier,
                    "kind": kind,
                    "evidence_type": "reference",
                    "source_file": str(path),
                    "source_name": path.name,
                    "source_sha256": digest,
                    "page": page_index + 1,
                    "page_count": document.page_count,
                    "text": text,
                    "text_excerpt": text[:500],
                    "text_path": text_path,
                    "has_images": bool(images),
                    "image_count": len(images),
                    "vector_drawing_count": vector_drawing_count,
                    "rendered_path": rendered,
                    "requires_visual_review": requires_review,
                    "review_status": "pending",
                    "review": {
                        "visual_required": requires_review,
                        "status": "pending",
                        "observations": None,
                    },
                    "extraction_error": error_text,
                    "imported_at": _utc(),
                }
            )
    finally:
        document.close()
    return records


def _import_pptx(root, path, digest, existing):
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
    except Exception as error:
        raise ValueError(
            "PPTX reference import requires python-pptx in the environment"
        ) from error
    vector_types = {
        MSO_SHAPE_TYPE.AUTO_SHAPE,
        MSO_SHAPE_TYPE.CALLOUT,
        MSO_SHAPE_TYPE.FREEFORM,
        MSO_SHAPE_TYPE.GROUP,
        MSO_SHAPE_TYPE.LINE,
    }
    try:
        presentation = Presentation(str(path))
    except Exception as error:
        raise ValueError(f"Could not open PPTX reference {path}: {error}") from error
    slides = list(presentation.slides)
    if not slides:
        raise ValueError("PPTX reference has no slides")
    base = _next_id(existing)
    directory = Path(root) / "work" / "references"
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for slide_index, slide in enumerate(slides, start=1):
        identifier = f"{base}-s{slide_index:04}"
        texts = []
        notes = ""
        pictures = 0
        tables = 0
        charts = 0
        vector_shapes = 0
        image_paths = []
        for shape in slide.shapes:
            shape_type = getattr(shape, "shape_type", None)
            if shape_type in vector_types:
                vector_shapes += 1
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                value = shape.text_frame.text.strip()
                if value:
                    texts.append(value)
            if getattr(shape, "has_table", False) and shape.has_table:
                tables += 1
                for row in shape.table.rows:
                    texts.append(" | ".join(cell.text for cell in row.cells))
            if getattr(shape, "has_chart", False) and shape.has_chart:
                charts += 1
            if getattr(shape, "shape_type", None) == 13 or getattr(shape, "image", None):
                pictures += 1
                if len(image_paths) < 20:
                    try:
                        image = shape.image
                        extension = (image.ext or "png").lower()
                        image_path = directory / f"{identifier}-img{len(image_paths) + 1:02}.{extension}"
                        image_path.write_bytes(image.blob)
                        image_paths.append(_register_extracted(root, image_path, identifier))
                    except Exception:
                        pass
        try:
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
        except Exception:
            notes = ""
        text = "\n".join(texts).strip()
        requires_review = (
            pictures > 0
            or tables > 0
            or charts > 0
            or vector_shapes > 0
            or len(text) < 40
        )
        kind = "diagram" if pictures or charts or vector_shapes else "slide"
        records.append(
            {
                "id": identifier,
                "kind": kind,
                "evidence_type": "reference",
                "source_file": str(path),
                "source_name": path.name,
                "source_sha256": digest,
                "slide": slide_index,
                "slide_count": len(slides),
                "text": text,
                "notes": notes,
                "text_excerpt": text[:500],
                "has_images": pictures > 0,
                "image_count": pictures,
                "extracted_images": image_paths,
                "table_count": tables,
                "chart_count": charts,
                "vector_shape_count": vector_shapes,
                "requires_visual_review": requires_review,
                "review_status": "pending",
                "review": {
                    "visual_required": requires_review,
                    "status": "pending",
                    "observations": None,
                },
                "imported_at": _utc(),
            }
        )
    return records


def _import_image(root, path, digest, existing):
    try:
        from PIL import Image
    except Exception as error:
        raise ValueError("Raster reference import requires Pillow") from error
    try:
        with Image.open(path) as image:
            image.verify()
            width, height = image.size
    except Exception as error:
        raise ValueError(f"Could not read raster reference {path}: {error}") from error
    identifier = _next_id(existing)
    return [
        {
            "id": identifier,
            "kind": "image",
            "evidence_type": "reference",
            "source_file": str(path),
            "source_name": path.name,
            "source_sha256": digest,
            "page": None,
            "slide": None,
            "width": width,
            "height": height,
            "text": "",
            "text_excerpt": "",
            "has_images": True,
            "image_count": 1,
            "requires_visual_review": True,
            "review_status": "pending",
            "review": {
                "visual_required": True,
                "status": "pending",
                "observations": None,
            },
            "imported_at": _utc(),
        }
    ]


def _remove_records(root, records):
    for record in records:
        for key in ("rendered_path", "text_path"):
            value = record.get(key)
            if value:
                path = Path(root) / value
                try:
                    if path.is_file():
                        path.unlink()
                except OSError:
                    pass
                unregister_generated(root, path)
        for value in record.get("extracted_images", []) or []:
            path = Path(root) / value
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass
            unregister_generated(root, path)


def import_reference(root, reference_path, replace=False):
    """Import one original PDF/PPTX/image as page/slide evidence."""

    root = Path(root).resolve()
    load_job(root)
    path = Path(reference_path)
    if not path.is_file():
        raise ValueError(f"Reference file not found: {path}")
    path = path.resolve()
    suffix = path.suffix.lower()
    if suffix not in {".pdf", ".pptx", ".ppt"} and suffix not in IMAGE_SUFFIXES:
        raise ValueError(
            "Unsupported reference format; supply a PDF, PPTX, or raster image"
        )
    digest_before = file_digest(path)
    records = _load_references(root)
    same_source = [record for record in records if record.get("source_file") == str(path)]
    if same_source:
        if all(record.get("source_sha256") == digest_before for record in same_source):
            return {
                "status": "already_imported",
                "source": str(path),
                "records": len(same_source),
                "pending_visual_review": sum(
                    1 for record in same_source if record.get("requires_visual_review")
                ),
            }
        if not replace:
            raise ValueError(
                "Reference file changed since import; stale evidence will not be mixed. "
                "Re-import with an explicit replace decision."
            )
        _remove_records(root, same_source)
        records = [record for record in records if record.get("source_file") != str(path)]
    if suffix == ".pdf":
        new_records = _import_pdf(root, path, digest_before, records)
    elif suffix in {".pptx", ".ppt"}:
        if suffix == ".ppt":
            raise ValueError("Legacy .ppt references are unsupported; save a .pptx copy")
        new_records = _import_pptx(root, path, digest_before, records)
    else:
        new_records = _import_image(root, path, digest_before, records)
    if file_digest(path) != digest_before:
        raise ValueError("Reference file changed during import; original was not modified by this tool")
    records.extend(new_records)
    _write_references(root, records)
    try:
        add_attachment(root, path, "reference")
    except ValueError:
        # A reference already inside the job area is still evidence, but not an original attachment.
        pass
    job = load_job(root)
    job.setdefault("stages", {})["references"] = {
        "status": "imported",
        "source": str(path),
        "records": len(new_records),
        "pending_visual_review": sum(
            1 for record in new_records if record.get("requires_visual_review")
        ),
        "updated_at": _utc(),
    }
    job["cleanup"] = None
    save_job(root, job)
    return {
        "status": "imported",
        "source": str(path),
        "records": len(new_records),
        "pending_visual_review": sum(
            1 for record in new_records if record.get("requires_visual_review")
        ),
        "reference_ids": [record["id"] for record in new_records],
        "note": (
            "Text and page/slide identities were extracted. Visual review is still pending "
            "where marked; extraction is not understanding."
        ),
    }


def _transcript_context(root, start, end, limit=3):
    path = Path(root) / "work" / "transcript.json"
    if not path.is_file():
        return []
    try:
        records = read_json(path)
    except Exception:
        return []
    if not isinstance(records, list):
        return []
    context = []
    for record in records:
        if not isinstance(record, dict):
            continue
        record_start = record.get("start")
        record_end = record.get("end")
        if not isinstance(record_start, (int, float)) or not isinstance(record_end, (int, float)):
            continue
        if record_end < start or record_start > end:
            continue
        context.append(
            {
                "id": record.get("id"),
                "start": record_start,
                "end": record_end,
                "text": record.get("text", ""),
            }
        )
        if len(context) >= limit:
            break
    return context


def _screen_reviews(root):
    path = Path(root) / "work" / "screen.json"
    if not path.is_file():
        return {}
    records = read_json(path)
    if not isinstance(records, list):
        raise ValueError("work/screen.json must contain a JSON array")
    return {
        record.get("id"): record
        for record in records
        if isinstance(record, dict) and record.get("id")
    }


def _retained_representatives(root):
    path = Path(root) / "work" / "frames-state.json"
    if not path.is_file():
        raise ValueError("Extract frames before requesting a screen review packet")
    state = read_json(path)
    if not isinstance(state, dict) or not isinstance(state.get("frames"), list):
        raise ValueError("work/frames-state.json must contain a frames array")
    representatives = state.get("representatives")
    if isinstance(representatives, list) and representatives:
        return representatives
    ordered = {}
    for frame in state["frames"]:
        if not isinstance(frame, dict) or not frame.get("frame_id"):
            continue
        identifier = frame["frame_id"]
        if identifier not in ordered:
            ordered[identifier] = {
                "frame_id": identifier,
                "path": frame.get("path"),
                "first_requested": frame.get("requested"),
                "occurrences": [],
            }
        ordered[identifier]["occurrences"].append(
            {
                "requested": frame.get("requested"),
                "decoded": frame.get("timestamp"),
                "duplicate": bool(frame.get("duplicate")),
                "path": frame.get("path"),
            }
        )
    return list(ordered.values())


def review_packet(root, offset=0, limit=10, kind="screen"):
    """Return one bounded ordered review packet; it never records approval."""

    root = Path(root).resolve()
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 25:
        raise ValueError("limit must be between 1 and 25")
    reviews = _screen_reviews(root) if kind == "screen" else {}
    items = []
    if kind == "screen":
        representatives = _retained_representatives(root)
        for representative in representatives:
            occurrences = representative.get("occurrences") or []
            requested = [
                value.get("requested")
                for value in occurrences
                if isinstance(value.get("requested"), (int, float))
            ]
            if not requested and isinstance(representative.get("first_requested"), (int, float)):
                requested = [representative["first_requested"]]
            first = min(requested) if requested else None
            last = max(requested) if requested else first
            identifier = representative["frame_id"]
            review = reviews.get(identifier)
            item = {
                "id": identifier,
                "kind": "screen",
                "frame_path": str((root / representative["path"]).resolve())
                if representative.get("path")
                else None,
                "occurrences": occurrences,
                "first_requested": first,
                "last_requested": last,
                "occurrence_count": len(occurrences),
                "review_status": (
                    (review.get("review_status") or review.get("status"))
                    if review
                    else "pending"
                ),
                "transcript_context": _transcript_context(
                    root,
                    max(0.0, float(first or 0) - 10.0),
                    float(last or first or 0) + 10.0,
                ),
                "occurrence_context": [
                    {
                        "requested": occurrence.get("requested"),
                        "context": _transcript_context(
                            root,
                            max(0.0, float(occurrence.get("requested") or 0) - 5.0),
                            float(occurrence.get("requested") or 0) + 15.0,
                            limit=2,
                        ),
                    }
                    for occurrence in occurrences[:3]
                    if isinstance(occurrence.get("requested"), (int, float))
                ],
            }
            items.append(item)
        source = "work/frames-state.json"
    elif kind == "reference":
        records = _load_references(root)
        for record in records:
            if not record.get("requires_visual_review"):
                continue
            rendered = record.get("rendered_path")
            items.append(
                {
                    "id": record.get("id"),
                    "kind": "reference",
                    "reference_kind": record.get("kind"),
                    "source_file": record.get("source_file"),
                    "page": record.get("page"),
                    "slide": record.get("slide"),
                    "evidence_path": str((root / rendered).resolve()) if rendered else None,
                    "text_excerpt": record.get("text_excerpt", ""),
                    "review_status": record.get("review_status", "pending"),
                    "uncertainty": record.get("uncertainty"),
                    "imported_at": record.get("imported_at"),
                }
            )
        source = "work/references.json"
    else:
        raise ValueError("kind must be 'screen' or 'reference'")
    total = len(items)
    page = items[offset : offset + limit]
    next_offset = offset + limit if offset + limit < total else None
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "source": source,
        "offset": offset,
        "limit": limit,
        "total": total,
        "returned": len(page),
        "next_offset": next_offset,
        "complete": next_offset is None,
        "items": page,
        "note": (
            "A bounded navigation packet. It does not review anything; the host must open the "
            "actual frame/page and record a concrete observation for each retained ID."
        ),
    }


def _normalize_review_document(document):
    if isinstance(document, list):
        records = document
    elif isinstance(document, dict):
        for key in ("reviews", "records", "screen", "items"):
            if isinstance(document.get(key), list):
                records = document[key]
                break
        else:
            raise ValueError("Review document must be an array or contain a reviews/records array")
    else:
        raise ValueError("Review file must contain a JSON array or object")
    if not all(isinstance(record, dict) for record in records):
        raise ValueError("Every review record must be an object")
    return records


def _merge_review_records(root, review_path, kind, known, existing, force=False):
    path = Path(review_path)
    if not path.is_file():
        raise ValueError(f"Review file not found: {path}")
    path = path.resolve()
    document = read_json(path)
    records = _normalize_review_document(document)
    seen = set()
    merged = []
    for index, record in enumerate(records):
        identifier = record.get("id") or record.get("frame_id") or record.get("evidence_id")
        if not _nonempty(identifier):
            raise ValueError(f"Review record {index} has no id")
        if identifier in seen:
            raise ValueError(f"Duplicate review record for {identifier}")
        seen.add(identifier)
        if identifier not in known:
            raise ValueError(
                f"Review refers to unknown {kind} evidence {identifier}; only known retained IDs "
                "may be merged"
            )
        status = record.get("review_status") or record.get("status")
        if status not in STATUSES:
            raise ValueError(
                f"Review for {identifier} has invalid status {status!r}; "
                f"allowed: {', '.join(sorted(STATUSES))}"
            )
        observation = _observation(record)
        uncertainty = _uncertainty(record)
        if status == "reviewed" and not observation:
            raise ValueError(
                f"Reviewed {kind} evidence {identifier} needs a concrete observation; "
                "a status alone is not review"
            )
        if status == "unreadable" and not uncertainty:
            raise ValueError(f"Unreadable {kind} evidence {identifier} needs an uncertainty description")
        if status == "extraction_failed" and not uncertainty:
            raise ValueError(f"Failed extraction for {identifier} needs a reason")
        merged_record = {
            "id": identifier,
            "kind": kind,
            "review_status": status,
            "status": status,
            "observations": observation or None,
            "uncertainty": uncertainty or None,
            "reviewed_at": record.get("reviewed_at") or _utc(),
            "reviewer": record.get("reviewer") or record.get("reviewed_by"),
            "review_notes": record.get("review_notes"),
            "source_review": record,
        }
        if identifier in existing:
            prior = existing[identifier]
            prior_status = prior.get("review_status")
            if prior_status in REVIEWED or prior_status == "extraction_failed":
                if (
                    prior_status == status
                    and prior.get("observations") == merged_record["observations"]
                ):
                    merged.append(prior)
                    continue
                if not force:
                    raise ValueError(
                        f"Conflicting existing review for {identifier}; pass an explicit replace "
                        "decision after checking the actual evidence"
                    )
        merged.append(merged_record)
    return merged, len(records)


def import_screen_review(root, review_path, force=False):
    """Merge reviewed observations only for known retained screen representatives."""

    root = Path(root).resolve()
    representatives = _retained_representatives(root)
    known = {item["frame_id"]: item for item in representatives}
    existing = _screen_reviews(root)
    merged, supplied = _merge_review_records(
        root, review_path, "screen", known, existing, force=force
    )
    by_id = dict(existing)
    for record in merged:
        by_id[record["id"]] = record
    output = []
    for representative in representatives:
        identifier = representative["frame_id"]
        if identifier in by_id:
            record = dict(by_id[identifier])
            record["kind"] = "screen"
            record["frame_path"] = (
                str((root / representative["path"]).resolve())
                if representative.get("path")
                else record.get("frame_path")
            )
            record["occurrences"] = representative.get("occurrences") or record.get("occurrences")
            times = [
                item.get("requested")
                for item in record["occurrences"] or []
                if isinstance(item.get("requested"), (int, float))
            ]
            if times:
                record["location"] = timestamp(min(times))
            output.append(record)
    path = root / "work" / "screen.json"
    write_json(path, output)
    register_generated(root, path)
    job = load_job(root)
    job.setdefault("stages", {})["screen"] = {
        "status": "review_merged",
        "records": len(output),
        "supplied": supplied,
        "updated_at": _utc(),
    }
    job["cleanup"] = None
    save_job(root, job)
    reviewed = sum(1 for record in output if record.get("review_status") in REVIEWED)
    return {
        "status": "merged",
        "kind": "screen",
        "supplied": supplied,
        "known_representatives": len(representatives),
        "records": len(output),
        "reviewed": reviewed,
        "pending": len(representatives) - reviewed,
        "readiness_complete": reviewed == len(representatives),
    }


def import_reference_review(root, review_path, force=False):
    """Merge reviewed observations for imported reference pages/slides."""

    root = Path(root).resolve()
    records = _load_references(root)
    known = {record["id"]: record for record in records if record.get("id")}
    existing = {
        record["id"]: {
            "id": record["id"],
            "kind": "reference",
            "review_status": record.get("review_status"),
            "observations": _observation(record),
            "uncertainty": _uncertainty(record),
        }
        for record in records
    }
    merged, supplied = _merge_review_records(
        root, review_path, "reference", known, existing, force=force
    )
    by_id = {record["id"]: record for record in records}
    for record in merged:
        target = by_id[record["id"]]
        target["review_status"] = record["review_status"]
        target["observations"] = record["observations"]
        target["uncertainty"] = record["uncertainty"]
        target["reviewed_at"] = record["reviewed_at"]
        target["reviewer"] = record["reviewer"]
        target["review"] = {
            "visual_required": bool(target.get("requires_visual_review")),
            "status": record["review_status"],
            "observations": record["observations"],
            "uncertainty": record["uncertainty"],
        }
    _write_references(root, records)
    job = load_job(root)
    job.setdefault("stages", {})["references"] = {
        "status": "review_merged",
        "records": len(records),
        "supplied": supplied,
        "updated_at": _utc(),
    }
    job["cleanup"] = None
    save_job(root, job)
    required = [record for record in records if record.get("requires_visual_review")]
    reviewed = sum(1 for record in required if record.get("review_status") in REVIEWED)
    return {
        "status": "merged",
        "kind": "reference",
        "supplied": supplied,
        "records": len(records),
        "visual_review_required": len(required),
        "reviewed": reviewed,
        "pending": len(required) - reviewed,
        "readiness_complete": reviewed == len(required),
    }


def evidence_status(root):
    root = Path(root).resolve()
    references = _load_references(root)
    packet = None
    try:
        state = read_json(Path(root) / "work" / "frames-state.json")
        if isinstance(state, dict):
            frames = state.get("frames") if isinstance(state.get("frames"), list) else []
            refinements = state.get("refinements") if isinstance(state.get("refinements"), list) else []
            representatives = (
                state.get("representatives") if isinstance(state.get("representatives"), list) else []
            )
            counts = state.get("counts") if isinstance(state.get("counts"), dict) else {}
            retained_ids = {
                item.get("frame_id")
                for source_list in (representatives, frames, refinements)
                for item in source_list
                if isinstance(item, dict) and item.get("frame_id")
            }
            baseline_extracted = len(frames)
            baseline_candidates = counts.get("candidates")
            if not isinstance(baseline_candidates, int) or baseline_candidates < baseline_extracted:
                settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
                try:
                    from .timing import sample_times

                    start_time = settings.get("start", state.get("start", 0.0))
                    end_time = settings.get("end", state.get("end", state.get("requested_end")))
                    interval_seconds = settings.get("interval", state.get("interval", 15.0))
                    if start_time is not None and end_time is not None and interval_seconds:
                        baseline_candidates = len(
                            list(sample_times(float(start_time), float(end_time), float(interval_seconds)))
                        )
                    else:
                        baseline_candidates = baseline_extracted
                except (TypeError, ValueError, OverflowError):
                    baseline_candidates = baseline_extracted
            baseline_duplicates = sum(
                1 for frame in frames if isinstance(frame, dict) and frame.get("duplicate")
            )
            refinement_candidates = len(refinements)
            refinement_extracted = len(refinements)
            refinement_duplicates = sum(
                1 for ref in refinements if isinstance(ref, dict) and ref.get("duplicate")
            )
            packet = {
                "candidates": baseline_candidates,
                "extracted": baseline_extracted,
                "retained": len(retained_ids),
                "baseline_candidates": baseline_candidates,
                "baseline_extracted": baseline_extracted,
                "baseline_duplicates": baseline_duplicates,
                "refinement_candidates": refinement_candidates,
                "refinement_extracted": refinement_extracted,
                "refinement_duplicates": refinement_duplicates,
                "duplicates": baseline_duplicates + refinement_duplicates,
            }
    except Exception:
        packet = None
    return {
        "references": {
            "records": len(references),
            "requires_visual_review": sum(
                1 for record in references if record.get("requires_visual_review")
            ),
            "pending": sum(
                1
                for record in references
                if record.get("requires_visual_review")
                and record.get("review_status") not in REVIEWED
            ),
        },
        "frames": packet,
    }
