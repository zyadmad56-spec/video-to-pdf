"""Complete study-document PDF rendering through local Chromium.

The renderer consumes validated, preview-accepted content and a visual style.
It writes one self-contained PDF with a cover, contents, topic divisions, page
numbers, and every supported block.  Images are embedded as data URIs so a later
cleanup of working images cannot break the delivered file.

Chromium (Microsoft Edge's Chromium channel on Windows, or an installed
Chromium/Chrome fallback) performs bidi shaping, Arabic joining, table wrapping,
pagination, and font embedding.  The text is never rasterised.  A renderer or
font failure raises instead of silently emitting an incomplete text dump.
"""

from __future__ import annotations

import base64
import html
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from . import authoring
from . import design as design_module
from . import math_layout
from . import output_preferences
from . import pdf_layouts
from .jobs import file_digest, read_json, register_generated, write_json
from .styles import (
    css_stack,
    font_families,
    normalize_style,
    require_style_direction,
    resolve_style,
)

SCHEMA_VERSION = 1
PREVIEW_NAME = "preview.pdf"

ARABIC_LABELS = {
    "study_notes": "مذكرة دراسية",
    "contents": "المحتويات",
    "uncertainties": "ملاحظات المصدر غير المحسومة",
    "omissions": "ملاحظات تغطية المصدر",
    "omission_kind": "النوع",
    "omission_reason": "السبب",
    "evidence": "الدليل",
    "location": "الموضع",
    "additional_example": "مثال إضافي",
    "preview": "معاينة تمثيلية",
    "preview_note": "هذه معاينة تمثيلية؛ المستند الكامل يضيف بقية الأقسام.",
    "source": "المصدر",
    "license": "الترخيص",
    "creator": "المُنشئ",
    "page_of": "صفحة {page} من {total}",
    "section": "القسم",
    "sections": "الأقسام",
    "source_note": "ملاحظات المصدر",
}

ENGLISH_LABELS = {
    "study_notes": "Study notes",
    "contents": "Contents",
    "uncertainties": "Unresolved source notes",
    "omissions": "Source coverage notes",
    "omission_kind": "Kind",
    "omission_reason": "Reason",
    "evidence": "Evidence",
    "location": "Location",
    "additional_example": "Additional example",
    "preview": "Representative preview",
    "preview_note": "This is a representative preview; the complete document adds the remaining sections.",
    "source": "Source",
    "license": "License",
    "creator": "Creator",
    "page_of": "Page {page} of {total}",
    "section": "Section",
    "sections": "Sections",
    "source_note": "Source notes",
}

OMISSION_DETAIL_KINDS = {"unreadable", "outside_scope", "superseded", "noninstructional"}


# ---------------------------------------------------------------------------
# Loading and small helpers
# ---------------------------------------------------------------------------


def _load_content(root, content_path):
    path = Path(content_path)
    if not path.is_absolute():
        path = Path(root) / path
    if not path.is_file():
        raise ValueError(f"Content file not found: {path}")
    content = read_json(path)
    if not isinstance(content, dict):
        raise ValueError("content.json must contain an object")
    return content, path


def _style_value(style_path):
    return style_path


def _style(root, style_path):
    if isinstance(style_path, dict):
        style = normalize_style(style_path)
        if style_path.get("_source_path"):
            style["_source_path"] = str(style_path["_source_path"])
        return style
    path = Path(str(style_path))
    if not path.is_absolute():
        candidate = Path(root) / path
        if candidate.is_file():
            path = candidate
    if path.is_file():
        style = normalize_style(read_json(path))
        style["_source_path"] = str(path.resolve())
        return style
    return resolve_style(style_path)


def _labels(content):
    if content.get("direction") == "rtl" or str(content.get("language", "")).lower().startswith("ar"):
        return dict(ARABIC_LABELS)
    return dict(ENGLISH_LABELS)


def _safe_stem(value, fallback="lecture-study-notes"):
    text = re.sub(r"[^\w\u0600-\u06FF\- ]+", " ", str(value or "")).strip()
    text = re.sub(r"\s+", "-", text)[:80].strip("-")
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    if not text or text.upper() in reserved:
        return fallback
    return text


def _output_stem(content):
    requested = content.get("output_stem")
    if requested:
        if Path(str(requested)).is_absolute() or ".." in Path(str(requested)).parts:
            raise ValueError("content.output_stem must be a plain file stem")
        return _safe_stem(requested)
    return _safe_stem(content.get("title"), "lecture-study-notes")


def _register(root, path):
    try:
        register_generated(root, Path(path))
    except (ValueError, KeyError, OSError):
        # Registration only applies to managed job work; rendering can still be
        # used for disposable examples outside a registered job.
        return False
    return True


def _utc():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Browser availability
# ---------------------------------------------------------------------------


def _candidate_executables():
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        playwright_root = Path(local) / "ms-playwright"
        if playwright_root.is_dir():
            for directory in sorted(playwright_root.glob("chromium-*"), reverse=True):
                for relative in ("chrome-win64/chrome.exe", "chrome-win/chrome.exe"):
                    candidate = directory / relative
                    if candidate.is_file():
                        candidates.append(candidate)
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    return candidates


def _launch(playwright):
    channel = os.environ.get("LECTURE_MATERIALS_BROWSER_CHANNEL")
    attempts = []
    if channel:
        attempts.append({"channel": channel})
    attempts.extend([{"channel": "msedge"}, {"channel": "chrome"}, {}])
    for candidate in _candidate_executables():
        attempts.append({"executable_path": str(candidate)})
    errors = []
    for options in attempts:
        try:
            browser = playwright.chromium.launch(headless=True, **options)
            label = options.get("channel") or options.get("executable_path") or "bundled-chromium"
            return browser, str(label)
        except Exception as error:  # Playwright raises its own Error type
            errors.append(f"{options}: {error}")
    raise RuntimeError(
        "No usable local Chromium renderer was found for PDF output. "
        "Install Playwright Chromium or set LECTURE_MATERIALS_BROWSER_CHANNEL to an installed "
        "Chromium channel. Attempts: " + " | ".join(errors[-3:])
    )


def renderer_health():
    """Report whether a local Chromium route can render a PDF on this host."""

    try:
        from playwright.sync_api import sync_playwright
    except Exception as error:
        return {"available": False, "error": f"Playwright is not importable: {error}"}
    try:
        with sync_playwright() as playwright:
            browser, label = _launch(playwright)
            browser.close()
        return {"available": True, "renderer": label}
    except Exception as error:
        return {"available": False, "error": str(error)}


# ---------------------------------------------------------------------------
# HTML construction
# ---------------------------------------------------------------------------


def _rich(text):
    """Escape text, then add a deliberately small inline-markup subset."""

    escaped = html.escape(str(text))
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        lambda match: f'<a href="{match.group(2)}">{match.group(1)}</a>',
        escaped,
    )
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped, flags=re.S)
    escaped = re.sub(r"`([^`]+)`", r'<code dir="ltr">\1</code>', escaped)
    if "<a href=" not in escaped:
        escaped = re.sub(
            r"(https?://[^\s<]+)",
            lambda match: f'<a href="{match.group(1)}">{match.group(1)}</a>',
            escaped,
        )
    return escaped.replace("\n", "<br>")


def _image_data_uri(path):
    path = Path(path)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _asset_source_line(asset):
    parts = []
    identifier = asset.get("attribution") or asset.get("creator")
    if identifier:
        parts.append(html.escape(str(identifier)))
    source = asset.get("source_url") or asset.get("source_page")
    if source:
        source_url = html.escape(str(source))
        name = asset.get("source_name")
        if isinstance(name, str) and name.strip():
            parts.append(
                f'<span dir="ltr"><a href="{source_url}" title="{source_url}">'
                f'{html.escape(name)}</a></span>'
            )
        else:
            parts.append(f'<a href="{source_url}">{source_url}</a>')
    license_value = asset.get("license") or asset.get("licensing")
    if license_value:
        parts.append(html.escape(str(license_value)))
    if asset.get("origin") == "user":
        original = asset.get("original_path") or asset.get("path")
        if original:
            parts.append(f'Supplied by user: {html.escape(Path(original).name)}')
    timestamp = asset.get("timestamp")
    if timestamp is not None:
        parts.append(html.escape(f"lecture time {timestamp}"))
    return " · ".join(part for part in parts if part)


def _pdf_design(style):
    design = style.get("design") if isinstance(style.get("design"), dict) else None
    if design and design.get("source_format") == "pdf":
        return design
    return None


def _design_direction_align(align, direction):
    if align == "center":
        return "center"
    if align == "end":
        return "left" if direction == "rtl" else "right"
    return "right" if direction == "rtl" else "left"


def _css(style, direction, labels):
    any_design = style.get("design") if isinstance(style.get("design"), dict) else {}
    design = _pdf_design(style) or {}
    design_palette = any_design.get("palette") or {}
    palette = dict(style["palette"])
    palette.update({key: value for key, value in design_palette.items() if value})
    page = style["page"]
    typography = dict(page["typography"])
    design_type = design.get("typography") or {}
    typography.update({key: value for key, value in design_type.items() if value})
    fonts = font_families(style, direction)
    body = css_stack(fonts["body"] + fonts["secondary"])
    heading = css_stack(fonts["heading"] + fonts["arabic"] + fonts["latin"])
    mono = css_stack(fonts["mono"])
    math_stack = "'Cambria Math', 'STIX Two Math', 'Segoe UI Math', serif"
    side_start = "right" if direction == "rtl" else "left"
    side_end = "left" if direction == "rtl" else "right"
    line_height = design.get("line_height") or page["line_height"]
    headings = any_design.get("headings") or {}
    title_heading = headings.get("title") or {}
    h1_heading = headings.get("h1") or {}
    h2_heading = headings.get("h2") or {}
    h1_colour = h1_heading.get("colour") or palette["accent"]
    h2_colour = h2_heading.get("colour") or palette["text"]
    title_colour = title_heading.get("colour") or palette["text"]
    table_design = any_design.get("table") or {}
    table_header_fill = table_design.get("header_fill") or palette["table_header"]
    table_header_text = table_design.get("header_text") or palette["table_header_text"]
    table_border = table_design.get("border") or palette["rule"]
    table_zebra = table_design.get("zebra") or palette["table_zebra"]
    table_body = table_design.get("body_fill") or palette["surface"]
    cover = design.get("cover") or {}
    band = cover.get("band") or None
    title_top = cover.get("title_top_mm")
    if title_top is not None:
        cover_pad = max(0.0, float(title_top) - float(page["margin_mm"]["top"]))
    else:
        cover_pad = 42.0 + (band.get("height_mm", 0) if band else 0)
    kicker_visible = not (
        title_top is not None and (float(title_top) - float(page["margin_mm"]["top"])) < 12
    )
    cover_title_margin = "0" if title_top is not None else "0.25em"
    cover_band_css = ""
    if band:
        cover_band_css = (
            f".cover {{ position: relative; }}\n"
            f"    .cover-band {{ position:absolute; top:-{page['margin_mm']['top']}mm; "
            f"left:-{page['margin_mm']['left']}mm; width:{page['width_mm']}mm; "
            f"height:{band.get('height_mm', 0)}mm; background:{band.get('colour')}; }}\n"
        )
    edge_css = ""
    return f"""
    :root {{
      --bg: {palette['background']};
      --surface: {palette['surface']};
      --text: {palette['text']};
      --muted: {palette['muted']};
      --accent: {palette['accent']};
      --accent-soft: {palette['accent_soft']};
      --rule: {palette['rule']};
      --example: {palette['example']};
      --example-soft: {palette['example_soft']};
      --warning: {palette['warning']};
    }}
    @page {{ background: {palette['background']}; }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    html {{ background: transparent; }}
    body {{
      background: transparent;
      color: var(--text);
      font-family: {body};
      font-size: {typography['body_pt']}pt;
      line-height: {line_height};
      direction: {direction};
      text-align: {'right' if direction == 'rtl' else 'left'};
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}
    a {{ color: var(--accent); text-decoration: underline; overflow-wrap: anywhere; }}
    p {{ margin: 0 0 0.65em; }}
    strong {{ font-weight: 700; }}
    code {{ font-family: {mono}; font-size: 0.92em; background: var(--surface); padding: 0 0.15em; direction: ltr; unicode-bidi: isolate; }}
    .cover {{ padding-top: {cover_pad:.1f}mm; break-after: page; display: flex; flex-direction: column; justify-content: flex-start; }}
    .cover .kicker {{ color: {palette['accent']}; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; font-size: {typography['small_pt']}pt; }}
    .cover h1 {{ font-family: {heading}; font-size: {typography['title_pt']}pt; line-height: 1.18; margin: {cover_title_margin} 0 0.4em; color: {title_colour}; overflow-wrap: anywhere; }}
    .cover .subtitle {{ font-size: {typography['subtitle_pt']}pt; color: {palette['muted']}; max-width: 150mm; }}
    .cover .meta {{ margin-top: 18mm; color: {palette['muted']}; font-size: {typography['small_pt']}pt; }}
    .cover .rule {{ margin-top: 8mm; height: 2.2pt; width: 45mm; background: {cover.get('rule_colour') or palette['accent']}; }}
    {cover_band_css}
    .toc {{ break-after: page; }}
    .toc h1 {{ font-family: {heading}; font-size: {typography['h1_pt']}pt; color: {h1_colour}; margin: 0 0 0.8em; }}
    .toc ol {{ list-style: none; margin: 0; padding: 0; counter-reset: toc; }}
    .toc li {{ counter-increment: toc; display: flex; align-items: baseline; gap: 0.5em; margin: 0 0 0.55em; }}
    .toc li::before {{ content: counter(toc) "."; color: {palette['accent']}; font-weight: 700; min-width: 2.2em; }}
    .toc a {{ color: var(--text); text-decoration: none; flex: 0 1 auto; overflow-wrap: anywhere; }}
    .toc .leader {{ flex: 1 1 auto; border-bottom: 1px dotted {palette['rule']}; min-width: 12mm; transform: translateY(-0.25em); }}
    .toc .page {{ flex: 0 0 3em; text-align: end; color: {palette['muted']}; font-variant-numeric: tabular-nums; }}
    .chapter {{ break-before: page; }}
    .section-kicker {{ color: {palette['accent']}; font-size: {typography['small_pt']}pt; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; margin: 0 0 0.3em; break-after: avoid; page-break-after: avoid; }}
    .chapter > h1 {{ font-family: {heading}; font-size: {typography['h1_pt']}pt; line-height: 1.3; color: {h1_colour}; margin: 0 0 0.55em; padding-bottom: 0.22em; border-bottom: 0.9pt solid {palette['rule']}; break-after: avoid; overflow-wrap: anywhere; }}
    .chapter > .section-summary {{ color: {palette['muted']}; font-size: {typography['subtitle_pt']}pt; margin-bottom: 1.2em; }}
    .chapter h2 {{ font-family: {heading}; font-size: {typography['h2_pt']}pt; color: {h2_colour}; margin: 1.1em 0 0.4em; break-after: avoid; }}
    .block {{ margin: 0 0 0.95em; }}
    .block.example {{ background: var(--example-soft); border-{side_start}: 3pt solid var(--example); padding: 0.55em 0.75em; border-radius: 2pt; }}
    .badge {{ display: inline-block; font-size: {typography['small_pt']}pt; font-weight: 700; color: var(--example); text-transform: uppercase; letter-spacing: 0.04em; margin: 0 0 0.2em; }}
    ul, ol {{ margin: 0 0 0.4em; padding-{side_start}: 1.35em; }}
    li {{ margin: 0 0 0.25em; }}
    table {{ width: 100%; border-collapse: collapse; margin: 0.35em 0 0.5em; font-size: {typography['table_pt']}pt; table-layout: auto; direction: {direction}; }}
    thead {{ display: table-header-group; }}
    th, td {{ border: 0.6pt solid {table_border}; padding: 0.32em 0.45em; text-align: {'right' if direction == 'rtl' else 'left'}; vertical-align: top; overflow-wrap: anywhere; word-break: break-word; background: {table_body}; }}
    th {{ background: {table_header_fill}; color: {table_header_text}; font-weight: 700; }}
    tbody tr:nth-child(even) td {{ background: {table_zebra}; }}
    tr {{ break-inside: avoid; }}
    .table-caption, figcaption {{ color: var(--muted); font-size: {typography['caption_pt']}pt; margin: 0.2em 0 0.8em; }}
    .equation {{ direction: ltr; unicode-bidi: isolate; text-align: center; font-family: {mono}; font-size: {typography['body_pt'] * 1.12:.1f}pt; white-space: pre-wrap; overflow-wrap: anywhere; background: var(--surface); border: 0.6pt solid var(--rule); padding: 0.55em 0.7em; margin: 0.3em 0 0.7em; }}
    .equation math {{ font-family: {math_stack}; font-size: 1.1em; line-height: 1.5; math-style: normal; }}
    .equation-note {{ text-align: {'right' if direction == 'rtl' else 'left'}; color: var(--muted); font-size: {typography['small_pt']}pt; }}
    figure {{ margin: 0.6em 0 1em; break-inside: avoid; text-align: center; }}
    figure img {{ max-width: {style['rules']['images']['max_width_ratio'] * 100:.0f}%; max-height: {style['page']['height_mm'] * style['rules']['images']['max_height_ratio']:.0f}mm; width: auto; height: auto; border: 0.6pt solid var(--rule); }}
    figure figcaption {{ text-align: {'right' if direction == 'rtl' else 'left'}; margin-top: 0.35em; }}
    .source-line {{ color: var(--muted); font-size: {typography['caption_pt']}pt; margin-top: 0.18em; overflow-wrap: anywhere; }}
    .appendix {{ break-before: page; }}
    .appendix h1 {{ font-family: {heading}; font-size: {typography['h1_pt']}pt; color: {h1_colour}; border-bottom: 0.9pt solid var(--rule); padding-bottom: 0.2em; }}
    .appendix h2 {{ font-family: {heading}; font-size: {typography['h2_pt']}pt; margin-top: 1.1em; }}
    .appendix ul {{ padding-{side_start}: 1.2em; }}
    .appendix .meta {{ color: var(--muted); font-size: {typography['small_pt']}pt; }}
    .appendix table {{ font-size: {typography['small_pt']}pt; }}
    .preview-banner {{ background: var(--accent-soft); color: var(--accent); border: 0.8pt solid var(--accent); padding: 0.6em 0.8em; margin: 0 0 1.2em; font-size: {typography['small_pt']}pt; }}
    body.preview .chapter {{ break-before: auto; }}
    body.preview .chapter + .chapter {{ margin-top: 1.6em; border-top: 0.8pt solid var(--rule); padding-top: 1.1em; }}
    .keep-with-next {{ break-after: avoid; }}
    """ + pdf_layouts.font_face_css(style) + pdf_layouts.layout_css(style)


def _render_block(root, section, block, style, direction, labels):
    kind = block.get("type")
    example = block.get("origin") == "example"
    classes = "block" + (" example" if example else "")
    badge = (
        f'<div class="badge">{html.escape(labels["additional_example"])}</div>' if example else ""
    )
    if kind == "paragraph":
        return f'<div class="{classes}">{badge}<p>{_rich(block.get("text", ""))}</p></div>'
    if kind == "bullets":
        tag = "ol" if block.get("ordered") else "ul"
        items = "".join(f"<li>{_rich(item)}</li>" for item in block.get("items", []))
        caption = block.get("caption")
        caption_html = f'<div class="table-caption">{_rich(caption)}</div>' if caption else ""
        return f'<div class="{classes}">{badge}<{tag}>{items}</{tag}>{caption_html}</div>'
    if kind == "table":
        header = block.get("header") or block.get("columns") or []
        head = ""
        if header:
            head = "<thead><tr>" + "".join(f"<th>{_rich(cell)}</th>" for cell in header) + "</tr></thead>"
        body = "".join(
            "<tr>" + "".join(f"<td>{_rich(cell)}</td>" for cell in row) + "</tr>"
            for row in block.get("rows", [])
        )
        caption = block.get("caption")
        caption_html = f'<div class="table-caption">{_rich(caption)}</div>' if caption else ""
        return f'<div class="{classes}">{badge}<table>{head}<tbody>{body}</tbody></table>{caption_html}</div>'
    if kind == "equation":
        notes = []
        for key in ("symbols", "labels", "units", "description"):
            value = block.get(key)
            if isinstance(value, str) and value.strip():
                notes.append(_rich(value))
        caption = block.get("caption")
        if caption:
            notes.append(_rich(caption))
        note_html = f'<div class="equation-note">{"<br>".join(notes)}</div>' if notes else ""
        math_tree = block.get("math")
        if math_tree is None:
            body = _rich(block.get("text", ""))
        else:
            body = (
                '<math dir="ltr">'
                + math_layout.to_mathml(math_layout.validate_math(math_tree))
                + "</math>"
            )
        return (
            f'<div class="{classes}">{badge}<div class="equation">{body}</div>'
            f"{note_html}</div>"
        )
    if kind == "image":
        asset = authoring.image_asset(root, block.get("asset_id"), section.get("id"))
        source_line = _asset_source_line(asset)
        credit = (
            f'<div class="source-line">{labels["source"]}: {source_line}</div>' if source_line else ""
        )
        placement = block.get("placement")
        reviewed_placement = (asset.get("placements", {}).get(section.get("id")) or {}).get("placement")
        if not placement or placement == "auto":
            placement = reviewed_placement or "auto"
        if placement == "auto":
            placement = style.get("layout", {}).get("image_alignment", "center")
        if placement == "start":
            alignment = "right" if direction == "rtl" else "left"
        elif placement == "end":
            alignment = "left" if direction == "rtl" else "right"
        elif placement in {"left", "right"}:
            alignment = placement
        else:
            alignment = "center"
        return (
            f'<figure class="{classes}" style="text-align:{alignment}">'
            f'<img src="{_image_data_uri(asset.get("resolved_path") or asset["path"])}" alt="{html.escape(str(block.get("caption", "")))}">'
            f'<figcaption style="text-align:{alignment}">{_rich(block.get("caption", ""))}{credit}</figcaption></figure>'
        )
    raise ValueError(f"Unsupported block type during rendering: {kind!r}")


def _cover_html(content, style, labels):
    language = str(content.get("language", ""))
    design = _pdf_design(style) or {}
    cover = design.get("cover") or {}
    band_html = '<div class="cover-band" aria-hidden="true"></div>' if cover.get("band") else ""
    title_top = cover.get("title_top_mm")
    kicker_visible = not (
        title_top is not None
        and (float(title_top) - float(style["page"]["margin_mm"]["top"])) < 12
    )
    kicker_html = (
        f'<div class="kicker">{html.escape(labels["study_notes"])}</div>'
        if kicker_visible else ""
    )
    arabic_labels = labels["study_notes"] == ARABIC_LABELS["study_notes"]
    language_names = (
        {"ar": "العربية", "en": "English", "mixed": "مختلط", "ar-en": "مختلط",
         "ar_en": "مختلط", "arabic": "العربية", "english": "English"}
        if arabic_labels else
        {"ar": "Arabic", "en": "English", "mixed": "Mixed", "ar-en": "Mixed",
         "ar_en": "Mixed", "arabic": "Arabic", "english": "English"}
    )
    language_label = language_names.get(language.lower(), language)
    meta_bits = [html.escape(labels["study_notes"]), html.escape(language_label),
                 f"{html.escape(labels['sections'])}: {len(content.get('sections', []))}"]
    subtitle = content.get("subtitle")
    subtitle_html = f'<div class="subtitle">{_rich(subtitle)}</div>' if subtitle else ""
    date = content.get("date")
    meta_extra = f' · {_rich(date)}' if date else ""
    source_note = content.get("source_note")
    source_html = f'<div class="meta">{_rich(source_note)}</div>' if source_note else ""
    return f"""
    <section class="cover">
      {band_html}
      {kicker_html}
      <h1>{_rich(content['title'])}</h1>
      {subtitle_html}
      <div class="rule"></div>
      <div class="meta">{' · '.join(meta_bits)}{meta_extra}</div>
      {source_html}
    </section>
    """


def _toc_html(content, labels, page_numbers):
    items = []
    for section in content["sections"]:
        number = page_numbers.get(section["id"])
        page = str(number) if number else "—"
        items.append(
            f'<li><a href="#{quote("sec-" + section["id"], safe="")}">{_rich(section["title"])}</a>'
            f'<span class="leader"></span><span class="page">{page}</span></li>'
        )
    return f"""
    <nav class="toc">
      <h1>{html.escape(labels['contents'])}</h1>
      <ol>{''.join(items)}</ol>
    </nav>
    """


def _sections_html(root, content, style, direction, labels):
    parts = []
    for index, section in enumerate(content["sections"], start=1):
        summary = section.get("summary")
        summary_html = f'<div class="section-summary">{_rich(summary)}</div>' if summary else ""
        blocks = "".join(
            _render_block(root, section, block, style, direction, labels)
            for block in section["blocks"]
        )
        heading = _chapter_heading(section, index, labels, output_preferences.section_time_label(root, section))
        chapter = pdf_layouts.chapter_html(heading, summary_html + blocks, style["layout"])
        parts.append(f'<section class="chapter" id="sec-{html.escape(section["id"])}">{chapter}</section>')
    return "".join(parts)


def _chapter_heading(section, index, labels, time_label=""):
    timing = f'<div class="meta section-time" dir="ltr">{html.escape(time_label)}</div>' if time_label else ""
    return (
        f'<div class="section-kicker">{html.escape(labels["section"])} {index}</div>'
        f'<h1>{_rich(section["title"])}</h1>'
        f'{timing}'
    )


def _appendix_html(content, labels):
    uncertainties = content.get("uncertainties") or []
    omissions = content.get("omissions") or []
    if not uncertainties and not omissions:
        return ""
    parts = [f'<section class="appendix"><h1>{html.escape(labels["source_note"])}</h1>']
    if uncertainties:
        items = []
        for uncertainty in uncertainties:
            location = uncertainty.get("location")
            description = uncertainty.get("description", "")
            identifier = uncertainty.get("evidence_id", "")
            items.append(
                f'<li><strong>{html.escape(labels["location"])}:</strong> {_rich(location)} — '
                f"{_rich(description)} "
                f'<span class="meta">[{html.escape(labels["evidence"])}: {html.escape(str(identifier))}]</span></li>'
            )
        parts.append(
            f'<h2>{html.escape(labels["uncertainties"])}</h2><ul>{"".join(items)}</ul>'
        )
    if omissions:
        counts = {}
        for omission in omissions:
            counts[omission["kind"]] = counts.get(omission["kind"], 0) + 1
        summary = ", ".join(f"{kind}: {count}" for kind, count in sorted(counts.items()))
        parts.append(
            f'<h2>{html.escape(labels["omissions"])}</h2>'
            f'<p class="meta">{_rich(summary)}</p>'
        )
        detailed = [item for item in omissions if item["kind"] in OMISSION_DETAIL_KINDS]
        if detailed:
            rows = "".join(
                "<tr>"
                f"<td>{html.escape(str(item['evidence_id']))}</td>"
                f"<td>{html.escape(str(item['kind']))}</td>"
                f"<td>{_rich(item['reason'])}</td>"
                "</tr>"
                for item in detailed[:200]
            )
            parts.append(
                "<table><thead><tr>"
                f"<th>{html.escape(labels['evidence'])}</th>"
                f"<th>{html.escape(labels['omission_kind'])}</th>"
                f"<th>{html.escape(labels['omission_reason'])}</th>"
                "</tr></thead><tbody>"
                f"{rows}</tbody></table>"
            )
            if len(detailed) > 200:
                parts.append(
                    f'<p class="meta">Showing the first 200 of {len(detailed)} detailed omissions; '
                    "the complete record remains in content.json.</p>"
                )
    parts.append("</section>")
    return "".join(parts)


def _html_document(root, content, style, page_numbers=None, preview=False):
    direction = content.get("direction", "ltr")
    labels = _labels(content)
    require_style_direction(style, direction)
    page_numbers = page_numbers or {}
    body = ""
    if preview:
        preview_sections = _preview_sections(content)
        body += _cover_html(content, style, labels)
        body += (
            f'<div class="preview-banner"><strong>{html.escape(labels["preview"])}</strong> — '
            f'{html.escape(labels["preview_note"])}</div>'
        )
        for index, (section, blocks) in enumerate(preview_sections, start=1):
            heading = _chapter_heading(section, index, labels, output_preferences.section_time_label(root, section))
            block_html = "".join(_render_block(root, section, block, style, direction, labels) for block in blocks)
            chapter = pdf_layouts.chapter_html(heading, block_html, style["layout"])
            body += f'<section class="chapter" id="sec-{html.escape(section["id"])}">{chapter}</section>'
    else:
        body = _cover_html(content, style, labels)
        body += _toc_html(content, labels, page_numbers)
        body += _sections_html(root, content, style, direction, labels)
        body += _appendix_html(content, labels)
    page = style["page"]
    body_class = ' class="preview"' if preview else ""
    return f"""<!doctype html>
<html lang="{html.escape(str(content.get('language', 'en')))}" dir="{direction}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(str(content.get('title', '')))}</title>
<style>{_css(style, direction, labels)}</style>
</head>
<body{body_class}>{body}</body>
</html>"""


def _preview_sections(content):
    """Select representative real blocks covering the available block types."""

    sections = content["sections"]
    chosen = []
    used = set()
    ordered = sorted(sections, key=lambda section: (-len(section.get("blocks", [])), section["id"]))
    for section in ordered:
        wanted = []
        for block in section.get("blocks", []):
            kind = block.get("type")
            if kind in {"paragraph", "bullets", "table", "equation", "image"}:
                if kind not in used or kind == "paragraph":
                    wanted.append(block)
                    used.add(kind)
            if len(wanted) >= 4:
                break
        if wanted:
            chosen.append((section, wanted))
        if used >= {"paragraph", "bullets", "table", "equation", "image"} or len(chosen) >= 3:
            break
    if not chosen:
        chosen = [(sections[0], sections[0].get("blocks", [])[:3])]
    return chosen


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _decor_element_html(spec, offset_top_mm=0.0):
    rect = spec.get("rect_mm") or {}
    left = float(rect.get("left", 0))
    top = float(rect.get("top", 0)) - offset_top_mm
    right = float(rect.get("right", 0))
    bottom = float(rect.get("bottom", 0)) - offset_top_mm
    width = max(0.0, right - left)
    height = max(0.0, bottom - top)
    fill = spec.get("fill")
    stroke = spec.get("stroke")
    kind = spec.get("kind")
    position = f"position:absolute; left:{left:.2f}mm; top:{top:.2f}mm;"
    if kind == "line":
        colour = fill or stroke
        thickness = max(0.3, float(spec.get("stroke_width_mm", 0.5)) or height or 0.3)
        return (
            f'<div style="{position} width:{width:.2f}mm; height:0; '
            f'border-top:{thickness:.2f}mm solid {colour};"></div>'
        )
    if fill and not stroke:
        # Backgrounds are not reliably painted inside Chromium's print
        # header/footer boxes; a thick border is vector and prints correctly.
        if width >= height:
            return (
                f'<div style="{position} width:{width:.2f}mm; height:0; '
                f'border-top:{height:.2f}mm solid {fill};"></div>'
            )
        return (
            f'<div style="{position} width:0; height:{height:.2f}mm; '
            f'border-left:{width:.2f}mm solid {fill};"></div>'
        )
    if stroke:
        thickness = max(0.35, float(spec.get("stroke_width_mm", 0.5)))
        background = f" background:{fill};" if fill else ""
        return (
            f'<div style="{position} width:{width:.2f}mm; height:{height:.2f}mm; '
            f'border:{thickness:.2f}mm solid {stroke};{background}"></div>'
        )
    return ""


def _template_document(page_width_mm, height_mm, parts):
    return (
        "<html><head><style>"
        "*{box-sizing:border-box;} html,body{margin:0;padding:0;background:transparent;}"
        f"body{{width:{page_width_mm}mm;height:{height_mm}mm;position:relative;overflow:hidden;}}"
        "</style></head><body>" + "".join(parts) + "</body></html>"
    )


def _header_template(content, style, labels):
    design = _pdf_design(style)
    if not design:
        return "<span></span>"
    page = style["page"]
    width = page["width_mm"]
    height = page["height_mm"]
    margin_top = page["margin_mm"]["top"]
    direction = content.get("direction", "ltr")
    parts = []
    for spec in design.get("vector_decor") or []:
        if spec.get("role") in {"header", "edge"}:
            element = _decor_element_html(spec)
            if element:
                parts.append(element)
    header = design.get("header") or {}
    if header.get("apply"):
        y = float(header.get("y_mm") or margin_top * 0.45)
        size = float(header.get("size_pt") or style["page"]["typography"]["small_pt"])
        colour = header.get("colour") or style["palette"]["muted"]
        align = _design_direction_align(header.get("align") or "start", direction)
        top = max(0.0, y - size * 0.42 * 0.3528)
        text = html.escape(str(content.get("title", ""))[:90])
        font = css_stack([str(header.get("font") or style["fonts"]["latin"]["family"]),
                          *style["fonts"]["arabic"].get("fallbacks", []),
                          *style["fonts"]["latin"].get("fallbacks", [])]).replace('"', "'")
        parts.append(
            f'<div style="position:absolute; left:{page["margin_mm"]["left"]}mm; '
            f'top:{top:.2f}mm; width:{width - page["margin_mm"]["left"] - page["margin_mm"]["right"]}mm; '
            f'text-align:{align}; font-family:{font}; font-size:{size:.1f}pt; '
            f'color:{colour}; white-space:nowrap; overflow:hidden;">{text}</div>'
        )
    if not parts:
        return "<span></span>"
    return _template_document(width, height, parts)


def _footer_template(content, style, labels):
    design = _pdf_design(style)
    page = style["page"]
    width = page["width_mm"]
    height = page["height_mm"]
    margin_bottom = page["margin_mm"]["bottom"]
    margin_left = page["margin_mm"]["left"]
    margin_right = page["margin_mm"]["right"]
    direction = content.get("direction", "ltr")
    parts = []
    card_rule = pdf_layouts.footer_rule(style)
    if card_rule:
        parts.append(_decor_element_html(card_rule))
    if design:
        for spec in design.get("vector_decor") or []:
            if spec.get("role") == "footer":
                element = _decor_element_html(spec)
                if element:
                    parts.append(element)
        footer = design.get("footer") or {}
        items = footer.get("items") or []
        page_item = next((item for item in items if item.get("text_kind") == "page-number"), None)
        label_item = next((item for item in items if item.get("text_kind") != "page-number"), None)
        style_item = page_item or label_item or footer
        compact = bool(page_item and page_item.get("characters", 99) <= 8)
        page_label = labels["page_of"].replace("{page}", '<span class="pageNumber"></span>').replace(
            "{total}", '<span class="totalPages"></span>'
        )
        if compact:
            page_label = '<span class="pageNumber"></span>'
        size = float(style_item.get("size_pt") or footer.get("size_pt") or style["page"]["typography"]["caption_pt"])
        colour = style_item.get("colour") or footer.get("colour") or style["palette"]["muted"]
        align = _design_direction_align(style_item.get("align") or "end", direction)
        y = float(style_item.get("y_mm") or (height - margin_bottom * 0.45))
        top = max(0.0, min(height - size * 0.5 * 0.3528, y - size * 0.42 * 0.3528))
        font = css_stack([str(style_item.get("font") or footer.get("font") or style["fonts"]["latin"]["family"]),
                          *style["fonts"]["arabic"].get("fallbacks", []),
                          *style["fonts"]["latin"].get("fallbacks", [])]).replace('"', "'")
        header = design.get("header") or {}
        parts.append(
            f'<div style="position:absolute; left:{margin_left}mm; top:{top:.2f}mm; '
            f'width:{width - margin_left - margin_right}mm; display:flex; '
            f'justify-content:{"center" if align == "center" else ("flex-end" if align == "right" else "flex-start")}; '
            f'font-family:{font}; font-size:{size:.1f}pt; color:{colour}; white-space:nowrap;">'
            f'<span>{page_label}</span></div>'
        )
        if not header.get("apply"):
            label_y = float((label_item or {}).get("y_mm") or y)
            label_align = _design_direction_align((label_item or {}).get("align") or "start", direction)
            label_top = max(0.0, min(height - size * 0.5 * 0.3528, label_y - size * 0.42 * 0.3528))
            parts.append(
                f'<div style="position:absolute; left:{margin_left}mm; top:{label_top:.2f}mm; '
                f'width:{width - margin_left - margin_right}mm; font-family:{font}; '
                f'font-size:{size:.1f}pt; color:{colour}; '
                f'text-align:{label_align}; white-space:nowrap; overflow:hidden;">'
                f'{html.escape(str(content.get("title", ""))[:70])}</div>'
            )
        return _template_document(width, height, parts) if parts else "<span></span>"
    family = css_stack(font_families(style, direction)["body"]).replace('"', "'")
    page_label = labels["page_of"].replace("{page}", '<span class="pageNumber"></span>').replace(
        "{total}", '<span class="totalPages"></span>'
    )
    return (
        "<html><head><style>*{box-sizing:border-box;} html,body{margin:0;padding:0;background:transparent;}"
        f"body{{width:{width}mm;height:{margin_bottom}mm;font-family:{family};font-size:8pt;"
        f"color:{style['palette']['muted']};}}</style></head><body>"
        f'<table style="width:100%; border-collapse:collapse; direction:{direction};"><tr>'
        f'<td style="text-align:start; padding:0 {margin_left}mm;">{html.escape(str(content.get("title", ""))[:90])}</td>'
        f'<td style="text-align:end; padding:0 {margin_right}mm; white-space:nowrap;">{page_label}</td>'
        "</tr></table></body></html>"
    )


def _audit(page):
    return page.evaluate(
        """() => {
            const root = document.documentElement;
            const overflows = [];
            const elements = document.querySelectorAll('table, pre, .equation, figure, img, p, li, h1, h2, th, td');
            for (const element of elements) {
                if (element.scrollWidth > element.clientWidth + 2 || element.scrollHeight > element.clientHeight + 2) {
                    const style = getComputedStyle(element);
                    if (style.overflow !== 'visible') {
                        overflows.push({
                            tag: element.tagName.toLowerCase(),
                            text: (element.textContent || '').slice(0, 80),
                            scrollWidth: element.scrollWidth,
                            clientWidth: element.clientWidth,
                            scrollHeight: element.scrollHeight,
                            clientHeight: element.clientHeight
                        });
                    }
                }
            }
            const images = Array.from(document.images).map(image => ({
                complete: image.complete,
                naturalWidth: image.naturalWidth,
                naturalHeight: image.naturalHeight
            }));
            return {
                pageWidth: root.clientWidth,
                scrollWidth: root.scrollWidth,
                horizontalOverflow: root.scrollWidth > root.clientWidth + 2,
                overflows: overflows.slice(0, 12),
                images
            };
        }"""
    )


def _print_pdf(html_text, destination, content, style, footer=True):
    from playwright.sync_api import sync_playwright

    page_settings = style["page"]
    margins = page_settings["margin_mm"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".render")
    if temporary.exists():
        temporary.unlink(missing_ok=True)
    with sync_playwright() as playwright:
        browser, label = _launch(playwright)
        try:
            context = browser.new_context(offline=True, viewport={"width": 1200, "height": 1600})
            page = context.new_page()
            page.set_content(html_text, wait_until="load")
            page.evaluate("document.fonts.ready")
            page.emulate_media(media="print")
            audit = _audit(page)
            if audit.get("horizontalOverflow"):
                raise ValueError(
                    "The document exceeds the printable width; fix wide tables/long unbroken strings "
                    "instead of clipping content"
                )
            failed = [item for item in audit.get("images", []) if not item.get("complete") or not item.get("naturalWidth")]
            if failed:
                raise ValueError("A rendered image failed to load; verify the asset file before rendering")
            options = {
                "path": str(temporary),
                "print_background": True,
                "prefer_css_page_size": False,
                "width": (
                    f"{round(page_settings['width_mm'] * 96 / 25.4)}px"
                    if _pdf_design(style)
                    else f"{page_settings['width_mm']}mm"
                ),
                "height": (
                    f"{round(page_settings['height_mm'] * 96 / 25.4)}px"
                    if _pdf_design(style)
                    else f"{page_settings['height_mm']}mm"
                ),
                "margin": {
                    "top": f"{margins['top']}mm",
                    "right": f"{margins['right']}mm",
                    "bottom": f"{margins['bottom']}mm",
                    "left": f"{margins['left']}mm",
                },
                "display_header_footer": True,
                "header_template": _header_template(content, style, _labels(content)),
                "footer_template": _footer_template(content, style, _labels(content)),
            }
            page.pdf(**options)
            context.close()
        finally:
            browser.close()
    if not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Chromium did not produce a PDF file")
    temporary.replace(destination)
    return {"renderer": label, "audit": audit}


def _section_pages(pdf_path, content):
    import pymupdf

    mapping = {}
    with pymupdf.open(pdf_path) as document:
        destinations = {unquote(name): target for name, target in document.resolve_names().items()}
        for section in content["sections"]:
            destination = destinations.get(f"sec-{section['id']}", {})
            page = destination.get("page", -1)
            if not 0 <= page < document.page_count:
                raise ValueError(f"The rendered PDF is missing the section destination: {section['id']}")
            mapping[section["id"]] = page + 1
    return mapping


def _declared_fonts(style):
    families = []
    for key in ("latin", "arabic", "display", "mono"):
        group = style.get("fonts", {}).get(key, {})
        if group.get("family"):
            families.append(group["family"])
    return sorted(dict.fromkeys(families))


def _font_substitutions(style, used_fonts):
    used = [re.sub(r"[\s\-]", "", name.split("+")[-1]).casefold() for name in used_fonts]
    substitutions = []
    for requested in _declared_fonts(style):
        needle = re.sub(r"[\s\-]", "", requested).casefold()
        if not any(needle in name for name in used):
            substitutions.append({
                "requested": requested,
                "note": "Not reported in the rendered PDF; Chromium used a fallback or a variant.",
            })
    return substitutions


def _read_back(pdf_path, content, style, text_only):
    import pymupdf

    document = pymupdf.open(pdf_path)
    texts = []
    fonts = set()
    images = 0
    empty_pages = []
    for index, page in enumerate(document):
        text = page.get_text()
        texts.append(text)
        if not text.strip() and not page.get_images(full=True):
            empty_pages.append(index + 1)
        images += len(page.get_images(full=True))
        for font in page.get_fonts():
            if len(font) > 3 and font[3]:
                fonts.add(font[3])
    full = "\n".join(texts)
    content_text = str(content.get("title", "")) + " " + " ".join(
        authoring.block_plain_text(block) for _, block in authoring.iter_blocks(content)
    )
    expected_images = sum(1 for _, block in authoring.iter_blocks(content) if block.get("type") == "image")
    report = {
        "pages": document.page_count,
        "characters": len(full),
        "fonts": sorted(fonts),
        "declared_fonts": _declared_fonts(style),
        "embedded_image_objects": images,
        "expected_image_blocks": expected_images,
        "empty_pages": empty_pages,
        "replacement_characters": full.count("\ufffd"),
        "page_size_pt": [round(document[0].rect.width, 2), round(document[0].rect.height, 2)]
        if document.page_count else None,
        "text_selectable": len(full.strip()) > 0,
        "arabic_characters": len(re.findall(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", full)),
        "has_arabic_content": bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", content_text)),
        "font_substitutions": _font_substitutions(style, sorted(fonts)),
        "self_contained": True,
        "producer_metadata": (document.metadata or {}).get("producer"),
    }
    document.close()
    problems = []
    if report["pages"] < 1:
        problems.append("PDF has no pages")
    if not report["text_selectable"]:
        problems.append("PDF has no extractable text")
    if report["replacement_characters"]:
        problems.append("PDF text contains replacement characters")
    if report["has_arabic_content"] and not report["arabic_characters"]:
        problems.append("Arabic content produced no extractable Arabic text (possible shaping/font failure)")
    if text_only and images:
        problems.append("Text-only PDF contains embedded image objects")
    if not text_only and expected_images and not images:
        problems.append("Reviewed image blocks produced no embedded image in the PDF")
    for title in [section["title"] for section in content["sections"]]:
        marks = r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]"
        title_text = re.sub(r"\s+(" + marks + ")", r"\1", str(title).replace("**", "").replace("`", ""))
        document_text = re.sub(r"\s+(" + marks + ")", r"\1", full)
        title_text = re.sub(marks, "", title_text)
        document_text = re.sub(marks, "", document_text)
        words = [word for word in re.findall(r"[\w\u0600-\u06FF]{3,}", title_text)]
        if words and sum(1 for word in words if word in document_text) / len(words) < 0.5:
            problems.append(f"Section title may be missing from the PDF text: {title}")
    report["problems"] = problems
    return report


def _render_pages(pdf_path, directory, limit=3):
    import pymupdf

    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("page-*.png"):
        stale.unlink(missing_ok=True)
    document = pymupdf.open(pdf_path)
    paths = []
    try:
        for index in range(min(limit, document.page_count)):
            page = document[index]
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4), alpha=False)
            target = directory / f"page-{index + 1:03d}.png"
            pixmap.save(target)
            paths.append(str(target))
    finally:
        document.close()
    return paths


def _html_page_preview(pdf_path, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    import pymupdf

    document = pymupdf.open(pdf_path)
    try:
        pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
        pixmap.save(destination)
    finally:
        document.close()
    return destination


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def render_preview(root, content_path, style_path):
    """Render a representative preview and bind it to the current material.

    A real preview PDF and PNG are written under ``work/``.  The host must show
    the artifact to the user and then call ``authoring.accept_preview``.  This
    function never records acceptance.
    """

    root = Path(root)
    content, content_file = _load_content(root, content_path)
    style = _style(root, style_path)
    report_content = authoring.validate_content(root, content)
    require_style_direction(style, content.get("direction", "ltr"))
    design_identity = design_module.verify_style_design(style, style.get("_source_path"))
    if design_identity.get("source_format") == "pptx":
        design_identity["renderer_note"] = (
            "A slide-template design supplies typography, colours and table tokens to this PDF. "
            "Its native slide masters/layouts cannot be used as PDF page masters; compare the deck output for native reuse."
        )

    readiness = None
    try:
        readiness = authoring.require_output_readiness(root, content)
    except ValueError as error:
        readiness = {"output_readiness": False, "reason": str(error)}

    work = root / "work"
    render_dir = work / "render" / "pdf-preview"
    render_dir.mkdir(parents=True, exist_ok=True)
    html_text = _html_document(root, content, style, preview=True)
    html_file = render_dir / "preview.html"
    html_file.write_text(html_text, encoding="utf-8")
    _register(root, html_file)

    pdf_path = work / PREVIEW_NAME
    render_info = _print_pdf(html_text, pdf_path, content, style, footer=True)
    _register(root, pdf_path)
    png_path = work / "preview.png"
    _html_page_preview(pdf_path, png_path)
    _register(root, png_path)
    preview_pages = _render_pages(pdf_path, work / "preview-pages", limit=3)
    for path in preview_pages:
        _register(root, path)

    if style.get("design"):
        reference_result = design_module.ensure_reference_previews(
            style, style.get("_source_path"),
            work / "reference-previews" / "pdf",
            kind="pdf",
        )
        reference_pages = reference_result.get("paths", [])
        for path in reference_pages:
            _register(root, path)
    else:
        reference_result = {"available": False, "reason": "This style has no reference design", "paths": []}
        reference_pages = []
    comparison_dir = work / "reference-comparison" / "pdf-preview"
    if reference_pages and preview_pages:
        comparison = design_module.make_side_by_side(
            reference_pages, preview_pages, comparison_dir,
            title="Reference pages versus new study-document preview",
            reference_total=((style.get("design") or {}).get("reference") or {}).get("pages"),
            new_total=len(preview_pages),
        )
    elif not style.get("design"):
        comparison = {"available": False, "reason": "This style has no reference design to compare against."}
    else:
        comparison = {
            "available": False,
            "reason": reference_result.get("reason") or "Rendered reference or new-content previews are unavailable.",
        }
    comparison_report_path = comparison_dir / "comparison-report.json"
    comparison["report_path"] = str(comparison_report_path) if comparison.get("available") else None
    comparison["design_support"] = (style.get("design") or {}).get("support")
    if comparison.get("available"):
        write_json(comparison_report_path, comparison)
        for path in comparison.get("images", []):
            _register(root, path)
        _register(root, comparison_report_path)

    key = authoring.approval_key(root, content, _style_value(style_path))
    meta = {
        "schema_version": SCHEMA_VERSION,
        "kind": "preview",
        "binding_digest": key,
        "preview": str(pdf_path.relative_to(root).as_posix()),
        "preview_sha256": file_digest(pdf_path),
        "files": {
            str(pdf_path.relative_to(root).as_posix()): file_digest(pdf_path),
        },
        "png": str(png_path.relative_to(root).as_posix()),
        "renderer": render_info["renderer"],
        "rendered_at": _utc(),
        "style": {"id": style["id"], "name": style["name"], "version": style["version"],
                  "fingerprint": style.get("fingerprint")},
        "reference_design": design_identity,
        "reference_previews": reference_result,
        "reference_comparison": comparison,
        "audit": render_info["audit"],
        "source_review_complete": bool(readiness and readiness.get("output_readiness")),
        "content_file": str(content_file),
    }
    meta_path = work / "preview-meta.json"
    write_json(meta_path, meta)
    _register(root, meta_path)

    return {
        "ok": True,
        "preview_pdf": str(pdf_path),
        "pdf": str(pdf_path),
        "preview_png": str(png_path),
        "png": str(png_path),
        "preview_pages": preview_pages,
        "meta": str(meta_path),
        "binding_digest": key,
        "renderer": render_info["renderer"],
        "pages": _pdf_page_count(pdf_path),
        "reference_design": design_identity,
        "reference_previews": reference_result,
        "reference_comparison": comparison,
        "content_report": report_content,
        "source_readiness": readiness,
        "layout_audit": render_info["audit"],
        "next_step": "Show the PNG/PDF and the side-by-side reference comparison, then call authoring.accept_preview only after real acceptance.",
    }


def _pdf_page_count(path):
    import pymupdf

    document = pymupdf.open(path)
    try:
        return document.page_count
    finally:
        document.close()


def render_final(root, content_path, style_path):
    """Assemble the complete PDF after all final-output gates pass."""

    root = Path(root)
    content, content_file = _load_content(root, content_path)
    style = _style(root, style_path)
    require_style_direction(style, content.get("direction", "ltr"))
    design_identity = design_module.verify_style_design(style, style.get("_source_path"))
    if design_identity.get("source_format") == "pptx":
        design_identity["renderer_note"] = (
            "A slide-template design supplies typography, colours and table tokens to this PDF. "
            "Its native slide masters/layouts cannot be used as PDF page masters; compare the deck output for native reuse."
        )
    gates = authoring.prepare_render(root, content, _style_value(style_path), require_acceptance=True)

    work = root / "work"
    render_dir = work / "render" / "pdf"
    render_dir.mkdir(parents=True, exist_ok=True)

    page_numbers = {}
    html_text = _html_document(root, content, style, page_numbers=page_numbers)
    pass_one = render_dir / "pass-1.pdf"
    render_info = _print_pdf(html_text, pass_one, content, style)
    _register(root, pass_one)
    page_numbers = _section_pages(pass_one, content)
    if page_numbers:
        html_text = _html_document(root, content, style, page_numbers=page_numbers)
        pass_two = render_dir / "pass-2.pdf"
        _print_pdf(html_text, pass_two, content, style)
        _register(root, pass_two)
        verified = _section_pages(pass_two, content)
        if verified != page_numbers:
            page_numbers = verified
            html_text = _html_document(root, content, style, page_numbers=page_numbers)
            pass_three = render_dir / "pass-3.pdf"
            _print_pdf(html_text, pass_three, content, style)
            _register(root, pass_three)
    final_html = render_dir / "document.html"
    final_html.write_text(html_text, encoding="utf-8")
    _register(root, final_html)

    outputs = root / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    destination = outputs / f"{_output_stem(content)}.pdf"
    temporary = destination.with_name(destination.name + ".tmp")
    render_info = _print_pdf(html_text, temporary, content, style)
    # Re-check the exact approval binding immediately before replacing the final
    # output; a mid-render content/style/used-image change must not slip through.
    authoring.require_accepted_preview(root, content, _style_value(style_path))
    temporary.replace(destination)

    text_only = authoring.image_mode(root) == "text"
    readback = _read_back(destination, content, style, text_only)
    preview_pages = _render_pages(destination, work / "render" / "pdf-pages", limit=3)
    for path in preview_pages:
        _register(root, path)
    if text_only and readback["embedded_image_objects"]:
        destination.unlink(missing_ok=True)
        raise ValueError("Text-only PDF unexpectedly contains image objects; output was removed")
    if readback["problems"]:
        destination.unlink(missing_ok=True)
        raise ValueError(
            "PDF read-back failed: " + "; ".join(readback["problems"])
        )

    if style.get("design"):
        reference_result = design_module.ensure_reference_previews(
            style, style.get("_source_path"),
            work / "reference-previews" / "pdf",
            kind="pdf",
        )
        reference_pages = reference_result.get("paths", [])
        for path in reference_pages:
            _register(root, path)
    else:
        reference_result = {"available": False, "reason": "This style has no reference design", "paths": []}
        reference_pages = []
    comparison_dir = work / "reference-comparison" / "pdf-final"
    if reference_pages and preview_pages:
        comparison = design_module.make_side_by_side(
            reference_pages, preview_pages, comparison_dir,
            title="Reference pages versus final study document",
            reference_total=((style.get("design") or {}).get("reference") or {}).get("pages"),
            new_total=_pdf_page_count(destination),
        )
    elif not style.get("design"):
        comparison = {"available": False, "reason": "This style has no reference design to compare against."}
    else:
        comparison = {
            "available": False,
            "reason": reference_result.get("reason") or "Rendered reference or final-page previews are unavailable.",
        }
    comparison["design_support"] = (style.get("design") or {}).get("support")
    if comparison.get("available"):
        comparison["report_path"] = str(comparison_dir / "comparison-report.json")
        write_json(comparison_dir / "comparison-report.json", comparison)
        for path in comparison.get("images", []):
            _register(root, path)
        _register(root, comparison_dir / "comparison-report.json")

    report = {
        "schema_version": SCHEMA_VERSION,
        "format": "pdf",
        "status": "complete",
        "ok": True,
        "output": str(destination),
        "pdf": str(destination),
        "output_sha256": file_digest(destination),
        "output_bytes": destination.stat().st_size,
        "content_file": str(content_file),
        "style": {"id": style["id"], "name": style["name"], "version": style["version"],
                  "fingerprint": style.get("fingerprint")},
        "renderer": render_info["renderer"],
        "reference_design": design_identity,
        "reference_previews": reference_result,
        "reference_comparison": comparison,
        "toc_page_numbers": page_numbers,
        "gates": gates,
        "readback": readback,
        "rendered_previews": preview_pages,
        "layout_audit": render_info["audit"],
        "checks_not_performed": [
            "scientific correctness and completeness of the explanation",
            "full-size human inspection of every Arabic or complex page",
            "pixel-identical comparison with a reference style",
        ],
        "generated_at": _utc(),
    }
    report_path = render_dir / "pdf-report.json"
    write_json(report_path, report)
    _register(root, report_path)
    return report
