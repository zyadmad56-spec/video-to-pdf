"""Read generated PDF/PPTX files back and report what can and cannot be proven.

The report distinguishes:

* structural facts read from the actual file (pages/slides, text, fonts, images,
  tables, links, bounds, embedded media);
* available rendered page/slide previews;
* content-inventory coverage against ``content.json``;
* checks that require a human (scientific correctness, full-size Arabic review,
  reference-style fidelity).

Successful export is never reported as proof that a human opened or approved the
document, and XML inspection is never described as a PowerPoint render.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import authoring, render_pptx
from .jobs import file_digest, load_job, read_json, register_generated, write_json

SCHEMA_VERSION = 1
ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
TOKEN_RE = re.compile(r"[\w\u0600-\u06FF]{3,}")
DIACRITICS_RE = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u08D3-\u08E1\u08E3-\u08FF]")


def _normalize_for_matching(text):
    value = str(text)
    # PDF text extraction can place a space before a combining mark.
    value = re.sub(r"\s+([\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u08D3-\u08E1\u08E3-\u08FF])", r"\1", value)
    value = DIACRITICS_RE.sub("", value)
    return " ".join(value.split())


def _register(root, path):
    try:
        register_generated(root, Path(path))
    except (ValueError, KeyError, FileNotFoundError):
        return False
    return True


def _load_content(root, content_path=None):
    candidates = []
    if content_path is not None:
        path = Path(content_path)
        candidates.append(path if path.is_absolute() else Path(root) / path)
    candidates.append(Path(root) / "work" / "content.json")
    for candidate in candidates:
        if candidate.is_file():
            try:
                document = read_json(candidate)
            except Exception:
                continue
            if isinstance(document, dict):
                return document, candidate
    return None, None


def _text_only(root):
    try:
        return authoring.image_mode(root) == "text", None
    except Exception as error:
        return None, f"Cannot verify picture policy: {error}"


def _coverage(expected, corpus):
    expected_text = _normalize_for_matching(expected)
    corpus_text = _normalize_for_matching(corpus)
    tokens = TOKEN_RE.findall(expected_text)
    if not tokens:
        return 1.0, []
    missing = [token for token in tokens if token not in corpus_text]
    return 1 - len(missing) / len(tokens), missing[:15]


def _content_inventory(content, corpus):
    if not content:
        return []
    inventory = []
    for section, block in authoring.iter_blocks(content):
        coverage, missing = _coverage(authoring.block_plain_text(block), corpus)
        inventory.append({
            "section": section["id"],
            "type": block.get("type"),
            "token_coverage": round(coverage, 3),
            "missing_tokens": missing,
        })
    return inventory


def _titles_present(content, corpus):
    missing = []
    if not content:
        return missing
    for section in content.get("sections", []):
        coverage, _ = _coverage(str(section.get("title", "")).replace("**", "").replace("`", ""), corpus)
        if coverage < 0.6:
            missing.append(section.get("title"))
    return missing


def _preview_pages(document, directory, limit=30):
    import pymupdf

    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("page-*.png"):
        stale.unlink(missing_ok=True)
    paths = []
    count = document.page_count
    if count <= limit:
        indices = list(range(count))
    else:
        indices = sorted(set([0, 1, count - 2, count - 1]))
        for index in range(count):
            text = document[index].get_text()
            if ARABIC_RE.search(text) or document[index].get_images(full=True):
                indices.append(index)
            if len(indices) >= limit:
                break
    for index in sorted(set(indices)):
        page = document[index]
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.35, 1.35), alpha=False)
        target = directory / f"page-{index + 1:03d}.png"
        pixmap.save(target)
        paths.append(str(target))
    return paths


def inspect_pdf(root, path, content, text_only, render=True):
    import pymupdf

    path = Path(path)
    document = pymupdf.open(path)
    texts = []
    fonts = set()
    images = []
    links = 0
    empty_pages = []
    outside = []
    for index, page in enumerate(document, start=1):
        text = page.get_text()
        texts.append(text)
        if not text.strip() and not page.get_images(full=True):
            empty_pages.append(index)
        for font in page.get_fonts():
            if len(font) > 3 and font[3]:
                fonts.add(font[3])
        for image in page.get_images(full=True):
            images.append({"page": index, "xref": image[0], "width": image[2], "height": image[3]})
        links += len(page.get_links())
        for block in page.get_text("dict").get("blocks", []):
            bbox = block.get("bbox")
            if not bbox:
                continue
            if bbox[0] < -2 or bbox[1] < -2 or bbox[2] > page.rect.width + 2 or bbox[3] > page.rect.height + 2:
                outside.append({"page": index, "bbox": [round(value, 1) for value in bbox]})
    full = "\n".join(texts)
    document_metadata = document.metadata or {}
    page_sizes = sorted({
        (round(page.rect.width, 2), round(page.rect.height, 2)) for page in document
    })
    page_count = document.page_count
    expected_images = sum(
        1 for _, block in authoring.iter_blocks(content or {"sections": []})
        if block.get("type") in {"image", "instruction"}
    )
    preview_paths = []
    render_note = None
    if render:
        if (Path(root) / "work").exists():
            try:
                preview_paths = _preview_pages(
                    document,
                    Path(root) / "work" / "verification" / "pdf-pages" / path.stem,
                )
                for item in preview_paths:
                    _register(root, item)
            except Exception as error:
                render_note = f"PDF page previews failed: {error}"
        else:
            render_note = "Page previews skipped because the job work directory is absent"
    report = {
        "schema_version": SCHEMA_VERSION,
        "format": "pdf",
        "file": str(path),
        "sha256": file_digest(path),
        "bytes": path.stat().st_size,
        "pages": page_count,
        "page_sizes_pt": [list(size) for size in page_sizes],
        "characters": len(full),
        "page_text_lengths": [len(text) for text in texts],
        "text_sample": full[:600],
        "fonts": sorted(fonts),
        "image_objects": len(images),
        "expected_image_blocks": expected_images,
        "picture_policy_checked": text_only is not None,
        "content_inventory_checked": content is not None,
        "images": images[:50],
        "links": links,
        "empty_pages": empty_pages,
        "text_outside_page": outside[:20],
        "replacement_characters": full.count("\ufffd"),
        "arabic_characters": len(ARABIC_RE.findall(full)),
        "has_arabic_content": bool(
            ARABIC_RE.search(full)
            or (content and ARABIC_RE.search(
                str(content.get("title", "")) + " " + " ".join(
                    authoring.block_plain_text(block) for _, block in authoring.iter_blocks(content)
                )
            ))
        ),
        "selectable_text": len(full.strip()) > 0,
        "table_of_contents_detected": any(
            marker in "\n".join(texts[: min(6, len(texts))])
            for marker in ("Contents", "المحتويات", "Table of contents")
        ),
        "page_numbers_detected": bool(re.search(r"\b\d+\s*(/|of|من)\s*\d+\b", full[-2000:])),
        "self_contained": True,
        "producer": document_metadata.get("producer"),
        "metadata": {key: value for key, value in document_metadata.items() if value},
    }
    document.close()

    inventory = _content_inventory(content, full)
    low = [item for item in inventory if item["token_coverage"] < 0.8]
    missing_titles = _titles_present(content, full)
    problems = []
    warnings = []
    if page_count < 1:
        problems.append("PDF has no pages")
    if not report["selectable_text"]:
        problems.append("PDF contains no selectable/extractable text")
    if report["replacement_characters"]:
        problems.append("PDF text contains replacement characters")
    if report["has_arabic_content"] and not report["arabic_characters"]:
        problems.append("Arabic content produced no extractable Arabic text")
    if text_only is True and report["image_objects"]:
        problems.append("Text-only PDF contains embedded image objects")
    if text_only is False and expected_images and not report["image_objects"]:
        problems.append("Reviewed image blocks produced no embedded image in the PDF")
    if low:
        problems.append(
            "Possible missing PDF content: " + ", ".join(
                f"{item['section']}/{item['type']} ({item['token_coverage']:.0%})" for item in low[:8]
            )
        )
    if missing_titles:
        problems.append("Section titles may be missing from the PDF: " + "; ".join(missing_titles[:6]))
    if outside:
        warnings.append("Some text blocks extend beyond the page boundary; inspect at full size")
    if empty_pages:
        warnings.append(f"Pages with neither text nor images: {empty_pages}")
    if report["table_of_contents_detected"] is False:
        warnings.append("No contents marker was detected on the first pages")
    if not report["fonts"]:
        warnings.append("No font resources were reported")
    if text_only is None:
        warnings.append("Picture policy could not be verified; no text-only/inserted-image conclusion is claimed")
    if content is None:
        warnings.append("Content inventory was not checked because content.json could not be loaded")
    if render_note:
        warnings.append(render_note)
    report.update({
        "ok": not problems,
        "inventory": inventory,
        "problems": problems,
        "warnings": warnings,
        "rendered_previews": preview_paths,
        "previews_not_rendered": max(0, page_count - len(preview_paths)),
        "rendering_note": render_note,
        "checks_not_performed": [
            "scientific correctness, completeness, and teaching quality",
            "full-size human inspection of every Arabic/complex page",
            "pixel-identical comparison with a reference style",
        ],
    })
    return report


def inspect_outputs(root, output_paths, content_path=None, render=True, write_report=False):
    """Read each final output back and return an honest inspection report."""

    root = Path(root)
    if isinstance(output_paths, (str, Path)):
        output_paths = [output_paths]
    content, content_file = _load_content(root, content_path)
    text_only, policy_error = _text_only(root)
    outputs = []
    problems = []
    warnings = []
    if policy_error:
        problems.append({"path": None, "message": policy_error})
    if content is None:
        warnings.append({
            "path": None,
            "message": "Content inventory was not checked because content.json could not be loaded",
        })
    for supplied in output_paths:
        path = Path(supplied)
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            problems.append({"path": str(path), "message": "Output file not found"})
            continue
        suffix = path.suffix.lower()
        local_render = render
        if suffix == ".pptx" and not (root / "work").exists():
            local_render = False
        try:
            if suffix == ".pdf":
                report = inspect_pdf(root, path, content, text_only, render=local_render)
            elif suffix == ".pptx":
                render_directory = root / "work" / "verification" / "pptx-slides" / path.stem
                report = render_pptx.inspect_pptx(
                    root, path, content or {"sections": []}, None, text_only,
                    content_file=content_file, render=local_render,
                    render_directory=render_directory if local_render else None,
                )
            else:
                problems.append({"path": str(path), "message": "Unsupported output format"})
                continue
        except Exception as error:
            problems.append({"path": str(path), "message": f"Inspection failed: {error}"})
            continue
        outputs.append(report)
        report["content_inventory_checked"] = content is not None
        report["picture_policy_checked"] = text_only is not None
        for message in report.get("problems", []):
            problems.append({"path": str(path), "message": message})
        for message in report.get("warnings", []):
            warnings.append({"path": str(path), "message": message})

    summary = {
        "ok": not problems and bool(outputs),
        "errors": len(problems) + (0 if outputs else 1),
        "warnings": len(warnings),
        "outputs": len(outputs),
        "pdf_pages": sum(item.get("pages", 0) for item in outputs if item.get("format") == "pdf"),
        "pptx_slides": sum(item.get("slides", 0) for item in outputs if item.get("format") == "pptx"),
        "text_only_policy": text_only,
        "picture_policy_checked": text_only is not None,
        "content_inventory_checked": content is not None,
    }
    if not outputs:
        problems.append({"path": None, "message": "No readable final outputs were supplied"})
    checks_performed = [
        "file identity and hashes",
        "page/slide counts and dimensions",
        "extractable text, fonts, links, and embedded images",
        "table and picture structure for PPTX",
        "shape bounds and available actual slide/PDF renders",
        "self-containment of embedded media",
    ]
    if content is not None:
        checks_performed.append("content-inventory token coverage against content.json")
    if text_only is not None:
        checks_performed.append("text-only picture policy")
    report = {
        "schema_version": SCHEMA_VERSION,
        "root": str(root.resolve()),
        "ok": not problems,
        "content_file": str(content_file) if content_file else None,
        "summary": summary,
        "problems": problems,
        "warnings": warnings,
        "outputs": outputs,
        "checks_performed": checks_performed,
        "checks_not_performed": [
            "scientific correctness or teaching completeness",
            "the user's visual acceptance of Arabic bidi, wrapping, and typography at full size",
            "reference-style fidelity beyond available render comparison",
            "correctness of image licensing beyond the recorded asset metadata",
        ],
        "human_review_required": [
            "Open every output and inspect representative complex pages/slides at full size",
            "Confirm Arabic joining, punctuation, numbers, tables, equations, and captions",
            "Confirm that cited evidence materially supports the prose",
        ],
    }
    if write_report and (root / "work").exists():
        path = root / "work" / "verification" / "inspection-report.json"
        write_json(path, report)
        _register(root, path)
        report["report_path"] = str(path)
    return report
