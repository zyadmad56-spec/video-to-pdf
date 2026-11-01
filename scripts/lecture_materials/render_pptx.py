"""Editable PPTX rendering with native text, tables, bullets, equations, and images.

The deck is generated from the same validated content and style as the PDF.  No
content is moved into speaker notes: notes carry only evidence identifiers, so a
slide-PDF export cannot silently lose substantive explanation.  Arabic and Latin
runs are emitted as real text runs with script-specific fonts and paragraph RTL
properties.  Images are embedded by python-pptx, so the deck stays self-contained
after working-image cleanup.

Validation after generation reads the PPTX back and, when Microsoft PowerPoint is
available, renders actual slides.  XML inspection alone is never reported as
"PowerPoint opened it".
"""

from __future__ import annotations

import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import authoring
from . import instructions
from . import output_preferences
from . import design as design_module
from .jobs import file_digest, read_json, register_generated, write_json
from .styles import (
    font_families,
    normalize_style,
    require_style_direction,
    resolve_style,
)

SCHEMA_VERSION = 1
ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
TOKEN_RE = re.compile(r"[\w\u0600-\u06FF]{3,}")

LABELS = {
    "en": {
        "study_notes": "Study notes",
        "contents": "Contents",
        "section": "Section",
        "sections": "Sections",
        "continued": "continued",
        "source_notes": "Source notes",
        "uncertainties": "Unresolved source notes",
        "omissions": "Source coverage notes",
        "additional_example": "Additional example",
        "preview": "Representative preview",
        "preview_note": "Preview only — the complete deck adds the remaining sections.",
        "source": "Source",
        "evidence": "Evidence",
        "page": "Slide",
    },
    "ar": {
        "study_notes": "مذكرة دراسية",
        "contents": "المحتويات",
        "section": "القسم",
        "sections": "الأقسام",
        "continued": "تابع",
        "source_notes": "ملاحظات المصدر",
        "uncertainties": "ملاحظات المصدر غير المحسومة",
        "omissions": "ملاحظات تغطية المصدر",
        "additional_example": "مثال إضافي",
        "preview": "معاينة تمثيلية",
        "preview_note": "معاينة فقط — العرض الكامل يضيف بقية الأقسام.",
        "source": "المصدر",
        "evidence": "الدليل",
        "page": "شريحة",
    },
}


def _labels(content):
    if content.get("direction") == "rtl" or str(content.get("language", "")).lower().startswith("ar"):
        return dict(LABELS["ar"])
    return dict(LABELS["en"])


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _rgb(value):
    from pptx.dml.color import RGBColor

    return RGBColor.from_string(str(value).replace("#", "").upper())


def _register(root, path):
    try:
        register_generated(root, Path(path))
    except (ValueError, KeyError, OSError):
        return False
    return True


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


def _safe_stem(content):
    value = content.get("output_stem") or content.get("title") or "lecture-study-slides"
    text = re.sub(r"[^\w\u0600-\u06FF\- ]+", " ", str(value)).strip()
    text = re.sub(r"\s+", "-", text)[:80].strip("-")
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    if not text or text.upper() in reserved:
        return "lecture-study-slides"
    return text


# ---------------------------------------------------------------------------
# Text measurement and splitting (conservative)
# ---------------------------------------------------------------------------


def estimate_lines(text, size_pt, width_in, rtl=False):
    """Conservative line estimate; deliberately favours extra slides over clipping."""

    width_pt = max(24.0, width_in * 72.0)
    factor = 0.58 if rtl else 0.56
    chars_per_line = max(10, int(width_pt / max(6.0, size_pt * factor)))
    lines = 0
    for paragraph in str(text).split("\n"):
        words = paragraph.split()
        if not words:
            lines += 1
            continue
        current = 0
        for word in words:
            addition = len(word) + (1 if current else 0)
            if current and current + addition > chars_per_line:
                lines += 1
                current = len(word)
            else:
                current += addition
        lines += 1
    return lines


def estimate_text_height(text, size_pt, width_in, rtl=False, spacing=1.2, padding_in=0.14):
    return estimate_lines(text, size_pt, width_in, rtl) * size_pt * spacing / 72.0 + padding_in


def _split_sentences(text):
    pieces = [piece for piece in re.split(r"(?<=[.!?؟。:؛])\s+", str(text)) if piece]
    return pieces or [str(text)]


def split_text(text, size_pt, width_in, max_height_in, rtl=False):
    """Split text so every returned chunk fits; never silently drops words."""

    text = str(text)
    if estimate_text_height(text, size_pt, width_in, rtl) <= max_height_in:
        return [text]
    chunks = []
    current = ""
    for sentence in _split_sentences(text):
        candidate = f"{current} {sentence}".strip()
        if estimate_text_height(candidate, size_pt, width_in, rtl) <= max_height_in:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if estimate_text_height(sentence, size_pt, width_in, rtl) <= max_height_in:
            current = sentence
            continue
        words = sentence.split()
        buffer = ""
        for word in words:
            candidate = f"{buffer} {word}".strip()
            if estimate_text_height(candidate, size_pt, width_in, rtl) <= max_height_in:
                buffer = candidate
            else:
                if buffer:
                    chunks.append(buffer)
                buffer = word
        if buffer:
            current = buffer
    if current:
        chunks.append(current)
    return chunks or [text]


# ---------------------------------------------------------------------------
# DrawingML helpers
# ---------------------------------------------------------------------------


def _font_name(style, key, direction):
    families = font_families(style, direction)
    if key == "arabic":
        return families["arabic"][0]
    if key == "latin":
        return families["latin"][0]
    if key == "display":
        return families["heading"][0]
    if key == "mono":
        return families["mono"][0]
    return families["body"][0]


def _set_script_font(run, latin_family, arabic_family=None, language="en-US"):
    from pptx.oxml.ns import qn
    from lxml import etree

    run.font.name = latin_family
    rpr = run._r.get_or_add_rPr()
    rpr.set("lang", language)
    arabic_family = arabic_family or latin_family
    for tag, family in (("a:ea", arabic_family), ("a:cs", arabic_family)):
        node = rpr.find(qn(tag))
        if node is None:
            node = etree.SubElement(rpr, qn(tag))
        node.set("typeface", family)


def _paragraph_rtl(paragraph, rtl):
    ppr = paragraph._p.get_or_add_pPr()
    ppr.set("rtl", "1" if rtl else "0")
    from pptx.enum.text import PP_ALIGN

    paragraph.alignment = PP_ALIGN.RIGHT if rtl else PP_ALIGN.LEFT


def _add_bullet(paragraph, character="•"):
    from pptx.oxml.ns import qn
    from lxml import etree

    ppr = paragraph._p.get_or_add_pPr()
    if ppr.find(qn("a:buChar")) is not None:
        return
    font = etree.SubElement(ppr, qn("a:buFont"))
    font.set("typeface", "Arial")
    bullet = etree.SubElement(ppr, qn("a:buChar"))
    bullet.set("char", character)


def _inline_segments(text):
    """Yield (text, bold, code, hyperlink) segments for a tiny safe inline subset."""

    pattern = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?://[^\s)]+)\)", re.S)
    position = 0
    for match in pattern.finditer(text):
        if match.start() > position:
            yield text[position:match.start()], False, False, None
        if match.group(1) is not None:
            yield match.group(1), True, False, None
        elif match.group(2) is not None:
            yield match.group(2), False, True, None
        else:
            yield match.group(3), False, False, match.group(4)
        position = match.end()
    if position < len(text):
        yield text[position:], False, False, None


def _add_runs(paragraph, text, style, direction, size_pt, color, bold=False,
              font_key=None, rtl=False):
    from pptx.util import Pt

    latin_family = _font_name(style, font_key or "latin", direction)
    arabic_family = _font_name(style, "arabic", direction)
    mono_family = _font_name(style, "mono", direction)
    for segment, segment_bold, code, hyperlink in _inline_segments(str(text)):
        if not segment:
            continue
        run = paragraph.add_run()
        run.text = segment
        run.font.size = Pt(size_pt)
        run.font.bold = bool(bold or segment_bold)
        run.font.color.rgb = _rgb(color)
        if code:
            _set_script_font(run, mono_family, mono_family,
                             "ar-SA" if ARABIC_RE.search(segment) else "en-US")
        else:
            _set_script_font(run, latin_family, arabic_family,
                             "ar-SA" if ARABIC_RE.search(segment) else "en-US")
        if hyperlink:
            run.hyperlink.address = hyperlink
        else:
            link = re.fullmatch(r"https?://[^\s]+", segment.strip())
            if link:
                run.hyperlink.address = segment.strip()


def _textbox(slide, left, top, width, height, style, direction,
             margin_in=0.04):
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE
    from pptx.util import Inches

    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.vertical_anchor = MSO_ANCHOR.TOP
    frame.margin_left = Inches(margin_in)
    frame.margin_right = Inches(margin_in)
    frame.margin_top = Inches(0.02)
    frame.margin_bottom = Inches(0.02)
    return box, frame


def _add_text(slide, left, top, width, height, text, style, direction, labels,
              size_key="body_pt", color=None, bold=False, align_center=False,
              font_key=None, rtl=None, size_pt=None):
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Pt

    if rtl is None:
        rtl = direction == "rtl"
    size = size_pt if size_pt is not None else style["slides"]["typography"][size_key]
    box, frame = _textbox(slide, left, top, width, height, style, direction)
    paragraphs = str(text).split("\n")
    for index, line in enumerate(paragraphs):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        if align_center:
            paragraph.alignment = PP_ALIGN.CENTER
            paragraph._p.get_or_add_pPr().set("rtl", "1" if rtl else "0")
        else:
            _paragraph_rtl(paragraph, rtl)
        paragraph.space_after = Pt(2)
        _add_runs(paragraph, line, style, direction, size,
                  color or style["palette"]["text"], bold=bold, font_key=font_key, rtl=rtl)
    return box


def _fill(shape, colour):
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(colour)
    shape.line.fill.background()


def _slide_background(slide, colour):
    background = slide.background
    background.fill.solid()
    background.fill.fore_color.rgb = _rgb(colour)


def _add_rule(slide, left, top, width, colour, thickness=0.03):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top),
                                   Inches(width), Inches(thickness))
    _fill(shape, colour)
    return shape


def _set_notes(slide, text):
    if not text:
        return
    frame = slide.notes_slide.notes_text_frame
    frame.text = text


def _placeholder_type(placeholder):
    try:
        value = placeholder.placeholder_format.type
        return getattr(value, "name", None) or str(value)
    except Exception:
        return "UNKNOWN"


def _find_placeholder(slide, kinds):
    kinds = set(kinds)
    for placeholder in slide.placeholders:
        if _placeholder_type(placeholder) in kinds:
            return placeholder
    return None


def _placeholder_alignment(placeholder):
    try:
        alignment = placeholder.text_frame.paragraphs[0].alignment
        if alignment is not None:
            return getattr(alignment, "name", str(alignment))
    except Exception:
        pass
    try:
        from pptx.oxml.ns import qn

        p = placeholder._element.find(".//" + qn("a:p"))
        if p is not None:
            ppr = p.find(qn("a:pPr"))
            if ppr is not None and ppr.get("algn"):
                return {"ctr": "CENTER", "r": "RIGHT", "l": "LEFT", "just": "JUSTIFY"}.get(ppr.get("algn"))
    except Exception:
        pass
    return None


def _set_placeholder(placeholder, text, style, direction, size_pt, colour, bold=False,
                     font_key="display", align_center=False, alignment=None):
    from pptx.enum.text import MSO_AUTO_SIZE, PP_ALIGN

    frame = placeholder.text_frame
    frame.clear()
    frame.word_wrap = True
    try:
        frame.auto_size = MSO_AUTO_SIZE.NONE
    except Exception:
        pass
    paragraph = frame.paragraphs[0]
    if alignment in {"CENTER", "RIGHT", "LEFT", "JUSTIFY"}:
        paragraph.alignment = {
            "CENTER": PP_ALIGN.CENTER,
            "RIGHT": PP_ALIGN.RIGHT,
            "LEFT": PP_ALIGN.LEFT,
            "JUSTIFY": PP_ALIGN.JUSTIFY,
        }[alignment]
        paragraph._p.get_or_add_pPr().set("rtl", "1" if direction == "rtl" else "0")
    elif align_center:
        paragraph.alignment = PP_ALIGN.CENTER
        paragraph._p.get_or_add_pPr().set("rtl", "1" if direction == "rtl" else "0")
    else:
        _paragraph_rtl(paragraph, direction == "rtl")
    paragraph.space_after = 0
    _add_runs(paragraph, text, style, direction, size_pt, colour, bold=bold, font_key=font_key)
    return placeholder


def _remove_placeholder(placeholder):
    element = placeholder._element
    parent = element.getparent()
    if parent is not None:
        parent.remove(element)


def _prune_placeholders(slide, keep=()):
    keep = set(keep)
    for placeholder in list(slide.placeholders):
        if _placeholder_type(placeholder) in keep:
            continue
        has_text = False
        try:
            has_text = bool(placeholder.text_frame.text.strip())
        except Exception:
            has_text = False
        if not has_text:
            _remove_placeholder(placeholder)


def _apply_cell_border(cell, colour, width_pt=0.75):
    from lxml import etree
    from pptx.oxml.ns import nsdecls, qn

    if not colour:
        return
    tc_pr = cell._tc.get_or_add_tcPr()
    for edge in ("lnL", "lnR", "lnT", "lnB"):
        old = tc_pr.find(qn("a:" + edge))
        if old is not None:
            tc_pr.remove(old)
        line = etree.SubElement(tc_pr, qn("a:" + edge), w=str(int(width_pt * 12700)), cap="flat",
                                cmpd="sng", algn="ctr")
        solid = etree.SubElement(line, qn("a:solidFill"))
        colour_node = etree.SubElement(solid, qn("a:srgbClr"))
        colour_node.set("val", str(colour).lstrip("#").upper())
    # tcPr children must be ordered before the fill; python-pptx writes the fill
    # separately, and PowerPoint is tolerant of the usual fill-first order. Move
    # the border elements ahead of any fill child for schema safety.
    fill = tc_pr.find(qn("a:solidFill"))
    if fill is not None:
        for edge in ("lnL", "lnR", "lnT", "lnB"):
            node = tc_pr.find(qn("a:" + edge))
            if node is not None:
                tc_pr.remove(node)
                fill.addprevious(node)


# ---------------------------------------------------------------------------
# Deck builder
# ---------------------------------------------------------------------------


class DeckBuilder:
    def __init__(self, content, style, root, preview=False, text_only=False):
        from pptx import Presentation
        from pptx.util import Inches

        self.content = content
        self.style = style
        self.root = Path(root)
        self.preview = preview
        self.text_only = text_only
        self.direction = content.get("direction", "ltr")
        self.labels = _labels(content)
        self.design = style.get("design") if isinstance(style.get("design"), dict) else None
        self.pdf_tokens = self.design if self.design and self.design.get("source_format") == "pdf" else None
        headings = (self.design or {}).get("headings") or {}
        self.heading_colour = (
            (headings.get("title") or {}).get("colour")
            or (headings.get("h1") or {}).get("colour")
            or style["palette"]["text"]
        )
        background_colour = (
            ((self.design or {}).get("background") or {}).get("colour")
            or style["palette"]["background"]
        )
        if design_module._colour_distance(self.heading_colour, background_colour) < 32:
            self.heading_colour = (
                style["palette"]["accent"]
                if design_module._colour_distance(style["palette"]["accent"], background_colour) >= 32
                else style["palette"]["text"]
            )
        self.design_identity = None
        self.design_picture_report = None
        self.template_path = None
        self.reference_decoration_count = 0
        self.layout_roles = {}
        self.layouts = []
        self.using_reference_template = False
        if self.design and self.design.get("source_format") == "pptx":
            self.design_identity = design_module.verify_style_design(
                style, style.get("_source_path")
            )
            self.template_path = design_module.design_template_path(style, style.get("_source_path"))
            if not self.template_path:
                raise ValueError(
                    "This PPTX reference style has no frozen native template. Re-extract the reference or save "
                    "the style with its design assets; only tokens cannot reproduce the master/layout design."
                )
            self.presentation = Presentation(self.template_path)
            self.using_reference_template = True
            self._remove_all_slides()
            # Picture objects are never inserted from a template: text-only output
            # must contain none, and other image modes require separate per-job
            # permission for each image.
            self.design_picture_report = design_module.strip_pptx_pictures(self.presentation)
            self.layout_roles = dict(self.design.get("roles") or {})
            self.layouts = list(self.presentation.slide_layouts)
            self.blank = self._layout("content_layout")
        else:
            self.presentation = Presentation()
            self.presentation.slide_width = Inches(style["slides"]["width_in"])
            self.presentation.slide_height = Inches(style["slides"]["height_in"])
            self.blank = self.presentation.slide_layouts[6]
            self.layouts = list(self.presentation.slide_layouts)
        self.slide_width = float(self.presentation.slide_width.inches)
        self.slide_height = float(self.presentation.slide_height.inches)
        geometry = (self.design or {}).get("geometry") or {}
        self.margin = float(style["slides"]["margin_in"])
        self.title_bar = float(style["slides"]["title_bar_in"])
        self.body_top = self.margin + self.title_bar
        self.body_left = self.margin
        self.body_width = self.slide_width - 2 * self.margin
        self.footer_reserve = 0.42
        self.body_bottom = self.slide_height - self.margin - self.footer_reserve
        self.footer_top = self.slide_height - self.margin - 0.3
        if self.design:
            if geometry.get("margin_in") is not None:
                self.margin = float(geometry["margin_in"])
                self.body_left = self.margin
            body = geometry.get("body") or {}
            self.body_top = float(body.get("top_in", self.body_top))
            self.body_left = float(body.get("left_in", self.body_left))
            self.body_width = float(body.get("width_in", self.slide_width - 2 * self.body_left))
            self.body_bottom = min(
                float(body.get("top_in", self.body_top)) + float(body.get("height_in", self.body_bottom - self.body_top)),
                self.slide_height - self.margin - 0.2,
            )
            footer = geometry.get("footer") or {}
            if footer.get("top_in") is not None and float(footer["top_in"]) >= self.slide_height * 0.68:
                self.footer_top = float(footer["top_in"])
                self.body_bottom = min(self.body_bottom, self.footer_top - 0.08)
            self.title_bar = max(0.35, self.body_top - self.margin)
            self.margin = self.body_left
        self.body_height = max(0.5, self.body_bottom - self.body_top)
        self.slide = None
        self.cursor = 0.0
        self.section = None
        self.section_index = 0
        self.continued = False
        self.section_starts = {}
        self.workspace = self.root / "work" / "render" / "pptx"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.image_cache = {}
        self.table_spec = (self.design or {}).get("table") or {}

    def _remove_all_slides(self):
        xml_slides = self.presentation.slides._sldIdLst
        for slide_id in list(xml_slides):
            try:
                self.presentation.part.drop_rel(slide_id.rId)
            except Exception:
                pass
            xml_slides.remove(slide_id)

    def _layout(self, role):
        index = self.layout_roles.get(role)
        if index is None:
            index = self.layout_roles.get("content_layout")
        if index is None or index < 0 or index >= len(self.layouts):
            if len(self.layouts) > 6:
                return self.layouts[6]
            return self.layouts[0]
        return self.layouts[index]

    def _design_alignment(self, role, types):
        index = self.layout_roles.get(role)
        layouts = (self.design or {}).get("layouts") or []
        if index is None or index < 0 or index >= len(layouts):
            return None
        for placeholder in layouts[index].get("placeholders") or []:
            if placeholder.get("type") in set(types):
                return placeholder.get("alignment")
        return None

    # -- slide scaffolding -------------------------------------------------

    def _new_slide(self, role="content_layout"):
        if self.using_reference_template:
            layout = self._layout(role)
            slide = self.presentation.slides.add_slide(layout)
            background = (self.design or {}).get("background") or {}
            if background.get("apply") and background.get("colour"):
                _slide_background(slide, background["colour"])
            recurring = (self.design or {}).get("recurring") or {}
            self.reference_decoration_count += len(
                design_module.add_recurring_shapes(slide, recurring.get("native_shapes"))
            )
            # Remove date/footer/slide-number placeholders; the renderer adds its
            # own matching footer, and empty placeholder prompts are not content.
            _prune_placeholders(slide, keep=("TITLE", "CENTER_TITLE", "SUBTITLE", "BODY", "OBJECT"))
            return slide
        slide = self.presentation.slides.add_slide(self.blank)
        _slide_background(slide, self.style["palette"]["background"])
        return slide

    def _body_region(self, title_shape=None):
        if not self.using_reference_template:
            return self.margin, self.margin + self.title_bar, self.body_width
        top = self.body_top
        if title_shape is not None:
            try:
                bottom = float(title_shape.top.inches) + float(title_shape.height.inches)
                top = max(top, bottom + 0.12)
            except Exception:
                pass
        left = self.body_left
        width = self.body_width
        return left, top, width

    def _header(self, slide, title, kicker=None, rule=True):
        if self.using_reference_template:
            title_placeholder = _find_placeholder(slide, {"TITLE", "CENTER_TITLE"})
            if title_placeholder is not None:
                box = title_placeholder
                try:
                    box_height = float(box.height.inches)
                    box_width = float(box.width.inches)
                except Exception:
                    box_height, box_width = 0.8, self.body_width
                size = self.style["slides"]["typography"]["title_pt"]
                title_height = estimate_text_height(title, size, box_width, self.direction == "rtl")
                while title_height > box_height and size > 18:
                    size *= 0.9
                    title_height = estimate_text_height(title, size, box_width, self.direction == "rtl")
                _set_placeholder(
                    title_placeholder, title, self.style, self.direction, size,
                    self.heading_colour, bold=True, font_key="display",
                )
                self.cursor = max(
                    self.body_top,
                    float(title_placeholder.top.inches) + float(title_placeholder.height.inches) + 0.12,
                )
            else:
                size = self.style["slides"]["typography"]["title_pt"]
                available = self.body_top - self.margin - 0.05
                height = estimate_text_height(title, size, self.body_width, self.direction == "rtl")
                while height > available and size > 18:
                    size *= 0.9
                    height = estimate_text_height(title, size, self.body_width, self.direction == "rtl")
                _add_text(slide, self.margin, self.margin, self.body_width, max(0.35, height),
                          title, self.style, self.direction, self.labels, size_key="title_pt",
                          color=self.heading_colour, bold=True, font_key="display",
                          size_pt=size)
                self.cursor = self.body_top
            if kicker:
                kicker_size = self.style["slides"]["typography"]["caption_pt"]
                title_top = None
                if title_placeholder is not None:
                    try:
                        title_top = float(title_placeholder.top.inches)
                    except Exception:
                        title_top = None
                if title_top is not None and title_top - 0.05 >= 0.35:
                    kicker_top = max(0.06, title_top - 0.32)
                    _add_text(slide, self.margin, kicker_top, self.body_width, 0.3, kicker,
                              self.style, self.direction, self.labels, size_key="caption_pt",
                              color=self.style["palette"]["accent"], bold=True)
                elif title_top is None:
                    _add_text(slide, self.margin, max(0.06, self.margin * 0.45),
                              self.body_width, 0.3, kicker, self.style, self.direction,
                              self.labels, size_key="caption_pt",
                              color=self.style["palette"]["accent"], bold=True)
            # Do not add a second rule: recurring/master decoration supplies it.
            _prune_placeholders(slide, keep=("TITLE", "CENTER_TITLE"))
            return
        top = self.margin * 0.55
        if kicker:
            _add_text(slide, self.margin, top, self.body_width, 0.3, kicker, self.style,
                      self.direction, self.labels, size_key="caption_pt",
                      color=self.style["palette"]["accent"], bold=True)
            top += 0.28
        available = self.margin + self.title_bar - 0.12 - top
        size = self.style["slides"]["typography"]["title_pt"]
        title_height = estimate_text_height(title, size, self.body_width, self.direction == "rtl")
        while title_height > available and size > 18:
            size *= 0.9
            title_height = estimate_text_height(title, size, self.body_width, self.direction == "rtl")
        _add_text(slide, self.margin, top, self.body_width, max(0.35, min(available, title_height)),
                  title, self.style, self.direction, self.labels, size_key="title_pt",
                  color=self.heading_colour, bold=True, font_key="display",
                  size_pt=size)
        if rule:
            if self.pdf_tokens:
                header = self.pdf_tokens.get("header") or {}
                spec = header.get("rule") or {}
                rect = spec.get("rect_mm") or {}
                rule_colour = spec.get("stroke") or spec.get("fill") or self.style["palette"]["accent"]
                page_width = (self.pdf_tokens.get("page_size_mm") or {}).get("width") or self.slide_width
                ratio = max(0.05, min(1.0, (float(rect.get("right", 0)) - float(rect.get("left", 0))) / max(1.0, page_width)))
                width = max(0.6, self.body_width * ratio)
                thickness = max(0.02, min(0.08, float(spec.get("stroke_width_mm", 0.5)) / 25.4 * 1.6))
            else:
                rule_colour = self.style["palette"]["accent"]
                width = min(2.2, self.body_width * 0.25)
                thickness = 0.035
            left = self.slide_width - self.margin - width if self.direction == "rtl" else self.margin
            _add_rule(slide, left, self.margin + self.title_bar - 0.1,
                      width, rule_colour, thickness=thickness)
        self.cursor = self.margin + self.title_bar

    def new_content_slide(self, section, continued=False):
        slide = self._new_slide("content_layout")
        kicker = f"{self.labels['section']} {self.section_index}"
        time_label = output_preferences.section_time_label(self.root, section)
        if time_label:
            kicker += f" · {time_label}"
        title = section["title"] + (
            f" — {self.labels['continued']}" if continued else ""
        )
        self._header(slide, title, kicker=kicker)
        self.slide = slide
        self.cursor = max(self.cursor, self.body_top)
        self.continued = continued

    def ensure_space(self, needed):
        if self.slide is None:
            self.new_content_slide(self.section, False)
        if self.cursor + needed > self.body_bottom + 0.01:
            self.new_content_slide(self.section, True)
        return self.slide

    def _notes(self, evidence, kind):
        if evidence:
            _set_notes(self.slide, f"Evidence: {', '.join(str(item) for item in evidence)}\nBlock: {kind}")

    # -- cover, contents, dividers ----------------------------------------

    def add_cover(self, preview=False):
        slide = self._new_slide("cover_layout")
        palette = self.style["palette"]
        kicker = self.labels["preview"] if preview else self.labels["study_notes"]
        if self.using_reference_template:
            title_placeholder = _find_placeholder(slide, {"TITLE", "CENTER_TITLE"})
            subtitle_placeholder = _find_placeholder(slide, {"SUBTITLE"})
            if title_placeholder is not None:
                try:
                    box_height = float(title_placeholder.height.inches)
                    box_width = float(title_placeholder.width.inches)
                    title_top = float(title_placeholder.top.inches)
                except Exception:
                    box_height, box_width, title_top = 1.4, self.body_width, self.slide_height * 0.28
                kicker_top = max(0.05, title_top - 0.45)
                _add_text(slide, self.margin, kicker_top, self.body_width, 0.32, kicker,
                          self.style, self.direction, self.labels, size_key="caption_pt",
                          color=palette["accent"], bold=True)
                size = self.style["slides"]["typography"]["section_pt"]
                title_height = estimate_text_height(
                    self.content["title"], size, box_width, self.direction == "rtl"
                )
                while title_height > box_height and size > 20:
                    size *= 0.9
                    title_height = estimate_text_height(
                        self.content["title"], size, box_width, self.direction == "rtl"
                    )
                _set_placeholder(
                    title_placeholder, self.content["title"], self.style, self.direction,
                    size, self.heading_colour, bold=True, font_key="display",
                    alignment=_placeholder_alignment(title_placeholder)
                    or self._design_alignment("cover_layout", {"TITLE", "CENTER_TITLE"}),
                )
                if self.content.get("subtitle") and subtitle_placeholder is not None:
                    subtitle_size = self.style["slides"]["typography"]["body_pt"]
                    try:
                        subtitle_height = float(subtitle_placeholder.height.inches)
                    except Exception:
                        subtitle_height = 0.9
                    while estimate_text_height(
                        self.content["subtitle"], subtitle_size,
                        float(subtitle_placeholder.width.inches), self.direction == "rtl",
                    ) > subtitle_height and subtitle_size > 10:
                        subtitle_size -= 1
                    _set_placeholder(
                        subtitle_placeholder, self.content["subtitle"], self.style,
                        self.direction, subtitle_size, palette["muted"], bold=False,
                        font_key="latin",
                    )
                elif self.content.get("subtitle"):
                    _add_text(
                        slide, self.margin,
                        title_top + box_height + 0.12,
                        self.body_width * 0.85, 1.0,
                        self.content["subtitle"], self.style, self.direction, self.labels,
                        size_key="body_pt", color=palette["muted"],
                    )
            else:
                size = self.style["slides"]["typography"]["section_pt"]
                _add_text(slide, self.margin, self.slide_height * 0.28,
                          self.body_width, self.slide_height * 0.2,
                          self.content["title"], self.style, self.direction, self.labels,
                          size_key="section_pt", color=self.heading_colour, bold=True,
                          font_key="display", size_pt=size)
            language = str(self.content.get("language", ""))
            arabic_labels = self.labels["study_notes"] == LABELS["ar"]["study_notes"]
            language_names = (
                {"ar": "العربية", "en": "English", "mixed": "مختلط", "ar-en": "مختلط",
                 "ar_en": "مختلط", "arabic": "العربية", "english": "English"}
                if arabic_labels else
                {"ar": "Arabic", "en": "English", "mixed": "Mixed", "ar-en": "Mixed",
                 "ar_en": "Mixed", "arabic": "Arabic", "english": "English"}
            )
            language_label = language_names.get(language.lower(), language)
            meta = f"{self.labels['study_notes']} · {language_label} · " \
                   f"{self.labels['sections']}: {len(self.content.get('sections', []))}"
            meta_top = max(self.slide_height - self.margin - 0.7, self.footer_top - 0.5)
            _add_text(slide, self.margin, meta_top, self.body_width, 0.36, meta,
                      self.style, self.direction, self.labels, size_key="caption_pt",
                      color=palette["muted"])
            if preview:
                _add_text(slide, self.margin, meta_top - 0.5, self.body_width, 0.5,
                          self.labels["preview_note"], self.style, self.direction,
                          self.labels, size_key="caption_pt", color=palette["accent"])
            _prune_placeholders(
                slide,
                keep=("TITLE", "CENTER_TITLE", "SUBTITLE") if self.content.get("subtitle") else ("TITLE", "CENTER_TITLE"),
            )
            return slide

        top_band = 0.32
        band_colour = palette["accent"]
        if self.pdf_tokens:
            band = (self.pdf_tokens.get("cover") or {}).get("band") or {}
            page_height = (self.pdf_tokens.get("page_size_mm") or {}).get("height") or self.slide_height
            if band:
                top_band = max(0.08, min(0.9, float(band.get("height_mm", 0)) / max(1.0, page_height) * self.slide_height))
                band_colour = band.get("colour") or band_colour
        _add_rule(slide, 0, 0, self.slide_width, band_colour, thickness=top_band)
        rule_width = 2.6
        rule_colour = palette["accent"]
        if self.pdf_tokens:
            cover = self.pdf_tokens.get("cover") or {}
            rule_colour = cover.get("rule_colour") or rule_colour
        rule_left = self.slide_width - self.margin - rule_width if self.direction == "rtl" else self.margin
        _add_rule(slide, rule_left, self.slide_height * 0.52, rule_width, rule_colour, thickness=0.045)
        _add_text(slide, self.margin, self.slide_height * 0.24, self.body_width, 0.4,
                  kicker, self.style, self.direction, self.labels, size_key="caption_pt",
                  color=palette["accent"], bold=True)
        size = self.style["slides"]["typography"]["section_pt"]
        available = self.slide_height * 0.22
        title_height = estimate_text_height(self.content["title"], size, self.body_width,
                                            self.direction == "rtl")
        while title_height > available and size > 20:
            size *= 0.9
            title_height = estimate_text_height(self.content["title"], size, self.body_width,
                                                self.direction == "rtl")
        _add_text(slide, self.margin, self.slide_height * 0.29, self.body_width,
                  max(0.5, min(available, title_height)), self.content["title"], self.style,
                  self.direction, self.labels, size_key="section_pt", color=self.heading_colour,
                  bold=True, font_key="display", size_pt=size)
        if self.content.get("subtitle"):
            _add_text(slide, self.margin, self.slide_height * 0.55, self.body_width * 0.85,
                      1.0, self.content["subtitle"], self.style, self.direction, self.labels,
                      size_key="body_pt", color=palette["muted"])
        language = str(self.content.get("language", ""))
        arabic_labels = self.labels["study_notes"] == LABELS["ar"]["study_notes"]
        language_names = (
            {"ar": "العربية", "en": "English", "mixed": "مختلط", "ar-en": "مختلط",
             "ar_en": "مختلط", "arabic": "العربية", "english": "English"}
            if arabic_labels else
            {"ar": "Arabic", "en": "English", "mixed": "Mixed", "ar-en": "Mixed",
             "ar_en": "Mixed", "arabic": "Arabic", "english": "English"}
        )
        language_label = language_names.get(language.lower(), language)
        meta = f"{self.labels['study_notes']} · {language_label} · " \
               f"{self.labels['sections']}: {len(self.content.get('sections', []))}"
        _add_text(slide, self.margin, self.slide_height - self.margin - 0.6,
                  self.body_width, 0.4, meta, self.style, self.direction, self.labels,
                  size_key="caption_pt", color=palette["muted"])
        if preview:
            _add_text(slide, self.margin, self.slide_height - self.margin - 1.15,
                      self.body_width, 0.5, self.labels["preview_note"], self.style,
                      self.direction, self.labels, size_key="caption_pt",
                      color=palette["accent"])
        return slide

    def add_toc(self):
        sections = self.content["sections"]
        columns = 2 if len(sections) > 8 else 1
        per_slide = max(1, int(self.body_height / 0.46))
        position = 0
        while position < len(sections):
            role = "toc_layout" if self.layout_roles.get("toc_layout") is not None else "content_layout"
            slide = self._new_slide(role)
            self._header(slide, self.labels["contents"])
            batch = sections[position:position + per_slide * columns]
            for column in range(columns):
                chunk = batch[column * per_slide:(column + 1) * per_slide]
                if not chunk:
                    continue
                visual_column = (columns - 1 - column) if self.direction == "rtl" else column
                column_left = self.margin + visual_column * (self.body_width / columns)
                column_width = self.body_width / columns - 0.2
                number_width = 0.55
                for row, section in enumerate(chunk):
                    index = position + column * per_slide + row + 1
                    top = self.body_top + row * 0.46
                    row_height = 0.42
                    if self.direction == "rtl":
                        number_left = column_left + column_width - number_width
                        title_left = column_left
                        title_width = column_width - number_width - 0.08
                    else:
                        number_left = column_left
                        title_left = column_left + number_width + 0.08
                        title_width = column_width - number_width - 0.08
                    _add_text(slide, number_left, top, number_width, row_height,
                              f"{index}.", self.style, self.direction, self.labels,
                              size_key="body_pt", color=self.style["palette"]["accent"],
                              bold=True, rtl=self.direction == "rtl")
                    title_size = min(self.style["slides"]["typography"]["body_pt"],
                                     max(12, self.style["slides"]["typography"]["body_pt"] - 2))
                    title_height = estimate_text_height(section["title"], title_size, title_width,
                                                        self.direction == "rtl")
                    _add_text(slide, title_left, top,
                              title_width, max(row_height, min(0.9, title_height)),
                              section["title"], self.style, self.direction, self.labels,
                              color=self.style["palette"]["text"], size_pt=title_size)
            position += per_slide * columns

    def add_section_divider(self, section, index):
        slide = self._new_slide("section_layout")
        palette = self.style["palette"]
        if self.using_reference_template:
            title_placeholder = _find_placeholder(slide, {"TITLE", "CENTER_TITLE"})
            if title_placeholder is not None:
                try:
                    box_height = float(title_placeholder.height.inches)
                    box_width = float(title_placeholder.width.inches)
                except Exception:
                    box_height, box_width = 1.2, self.body_width
                size = self.style["slides"]["typography"]["section_pt"]
                title_height = estimate_text_height(
                    section["title"], size, box_width, self.direction == "rtl"
                )
                while title_height > box_height and size > 20:
                    size *= 0.9
                    title_height = estimate_text_height(
                        section["title"], size, box_width, self.direction == "rtl"
                    )
                _set_placeholder(
                    title_placeholder, section["title"], self.style, self.direction,
                    size, self.heading_colour, bold=True, font_key="display",
                )
            else:
                _add_text(slide, self.margin, self.slide_height * 0.33, self.body_width,
                          self.slide_height * 0.24, section["title"], self.style,
                          self.direction, self.labels, size_key="section_pt",
                          color=self.heading_colour, bold=True, font_key="display")
            body_placeholder = _find_placeholder(slide, {"BODY", "OBJECT", "SUBTITLE"})
            if section.get("summary") and body_placeholder is not None:
                _set_placeholder(
                    body_placeholder, section["summary"], self.style, self.direction,
                    self.style["slides"]["typography"]["body_pt"], palette["muted"],
                    bold=False, font_key="latin",
                )
            elif section.get("summary"):
                _add_text(slide, self.margin, self.slide_height * 0.62,
                          self.body_width * 0.82, self.slide_height * 0.16,
                          section["summary"], self.style, self.direction,
                          self.labels, size_key="body_pt", color=palette["muted"])
            elif body_placeholder is not None:
                _remove_placeholder(body_placeholder)
            _add_text(slide, self.margin, max(0.04, self.margin * 0.45), self.body_width, 0.3,
                      f"{self.labels['section']} {index}", self.style, self.direction,
                      self.labels, size_key="caption_pt", color=palette["accent"], bold=True)
            _prune_placeholders(slide, keep=("TITLE", "CENTER_TITLE", "BODY", "OBJECT", "SUBTITLE"))
        else:
            _add_rule(slide, 0, 0, self.slide_width, palette["accent"], thickness=0.18)
            _add_text(slide, self.margin, self.slide_height * 0.26, self.body_width, 0.5,
                      f"{self.labels['section']} {index}", self.style, self.direction, self.labels,
                      size_key="caption_pt", color=palette["accent"], bold=True)
            size = self.style["slides"]["typography"]["section_pt"]
            available = self.slide_height * 0.24
            title_height = estimate_text_height(section["title"], size, self.body_width,
                                                self.direction == "rtl")
            while title_height > available and size > 20:
                size *= 0.9
                title_height = estimate_text_height(section["title"], size, self.body_width,
                                                    self.direction == "rtl")
            _add_text(slide, self.margin, self.slide_height * 0.33, self.body_width,
                      max(0.5, min(available, title_height)), section["title"], self.style,
                      self.direction, self.labels, size_key="section_pt",
                      color=self.heading_colour, bold=True, font_key="display", size_pt=size)
            if section.get("summary"):
                _add_text(slide, self.margin, self.slide_height * 0.62, self.body_width * 0.82,
                          self.slide_height * 0.16, section["summary"], self.style, self.direction,
                          self.labels, size_key="body_pt", color=palette["muted"])
        self.section_starts[section["id"]] = len(self.presentation.slides)
        return slide

    # -- blocks ------------------------------------------------------------

    def add_paragraph(self, block):
        from pptx.util import Pt

        text = str(block.get("text", ""))
        size = self.style["slides"]["typography"]["body_pt"]
        chunks = split_text(text, size, self.body_width, self.body_height * 0.94,
                            self.direction == "rtl")
        if self.preview:
            chunks = chunks[:2]
        for chunk in chunks:
            height = min(self.body_height, estimate_text_height(
                chunk, size, self.body_width, self.direction == "rtl"
            ) + (0.28 if block.get("origin") == "example" else 0))
            self.ensure_space(height)
            if block.get("origin") == "example":
                _add_text(self.slide, self.margin, self.cursor, self.body_width, 0.24,
                          self.labels["additional_example"], self.style, self.direction,
                          self.labels, size_key="caption_pt",
                          color=self.style["palette"]["example"], bold=True)
                self.cursor += 0.24
                height = min(self.body_height - 0.24, height)
            _add_text(self.slide, self.margin, self.cursor, self.body_width, height,
                      chunk, self.style, self.direction, self.labels)
            self._notes(block.get("evidence"), "paragraph")
            self.cursor += height + 0.12

    def add_bullets(self, block):
        from pptx.util import Pt

        size = self.style["slides"]["typography"]["bullet_pt"]
        items = [str(item) for item in block.get("items", [])]
        if self.preview:
            items = items[:6]
        ordered = bool(block.get("ordered"))
        entries = []
        for number, item in enumerate(items, start=1):
            chunks = split_text(
                item, size, self.body_width - 0.35, self.body_height * 0.8,
                self.direction == "rtl",
            )
            for index, chunk in enumerate(chunks):
                entries.append((number, chunk, index > 0))
        queue = list(entries)
        while queue:
            slide_batch = []
            height = 0.0
            while queue:
                number, item, continuation = queue[0]
                item_height = estimate_text_height(item, size, self.body_width - 0.35,
                                                   self.direction == "rtl") + 0.06
                if slide_batch and height + item_height > self.body_height * 0.92:
                    break
                slide_batch.append(queue.pop(0))
                height += item_height
            height = max(0.3, min(self.body_height - 0.05, height))
            self.ensure_space(height)
            box, frame = _textbox(self.slide, self.margin, self.cursor, self.body_width, height,
                                  self.style, self.direction)
            for paragraph_index, (number, item, continuation) in enumerate(slide_batch):
                paragraph = frame.paragraphs[0] if paragraph_index == 0 else frame.add_paragraph()
                _paragraph_rtl(paragraph, self.direction == "rtl")
                paragraph.space_after = Pt(4)
                if continuation:
                    paragraph.level = 1
                    _add_runs(paragraph, item, self.style, self.direction, size,
                              self.style["palette"]["text"])
                elif ordered:
                    _add_runs(paragraph, f"{number}. {item}", self.style, self.direction, size,
                              self.style["palette"]["text"])
                else:
                    _add_bullet(paragraph, "•")
                    _add_runs(paragraph, item, self.style, self.direction, size,
                              self.style["palette"]["text"])
            self._notes(block.get("evidence"), "bullets")
            self.cursor += height + 0.12

    def add_equation(self, block):
        if block.get("math") is not None:
            raise ValueError(
                "Equation block carries a structured 'math' tree; editable PPTX "
                "cannot preserve it as native editable math. Render this deck to "
                "PDF instead, or drop the 'math' field to keep the plain-text "
                "equation."
            )
        size = self.style["slides"]["typography"]["equation_pt"]
        text = str(block.get("text", ""))
        notes = [str(block[key]) for key in ("symbols", "labels", "units", "description", "caption")
                 if isinstance(block.get(key), str) and block[key].strip()]
        note_text = "\n".join(notes)
        main_height = estimate_text_height(text, size, self.body_width * 0.9, False) + 0.18
        note_height = estimate_text_height(note_text, self.style["slides"]["typography"]["caption_pt"],
                                           self.body_width, self.direction == "rtl") if note_text else 0
        total = main_height + note_height + 0.16
        self.ensure_space(min(total, self.body_height - 0.05))
        box, frame = _textbox(self.slide, self.margin, self.cursor, self.body_width, main_height,
                              self.style, self.direction)
        paragraph = frame.paragraphs[0]
        from pptx.enum.text import PP_ALIGN
        paragraph.alignment = PP_ALIGN.CENTER
        paragraph._p.get_or_add_pPr().set("rtl", "0")
        _add_runs(paragraph, text, self.style, self.direction, size,
                  self.style["palette"]["text"], font_key="mono")
        self.cursor += main_height
        if note_text:
            _add_text(self.slide, self.margin, self.cursor, self.body_width, note_height,
                      note_text, self.style, self.direction, self.labels, size_key="caption_pt",
                      color=self.style["palette"]["muted"])
            self.cursor += note_height
        self.cursor += 0.14
        self._notes(block.get("evidence"), "equation")

    def _image_size(self, path):
        from PIL import Image

        if path in self.image_cache:
            return self.image_cache[path]
        with Image.open(path) as image:
            width, height = image.size
        aspect = width / height if height else 1.0
        self.image_cache[path] = (width, height, aspect)
        return self.image_cache[path]

    def add_image(self, section, block):
        asset = authoring.image_asset(self.root, block.get("asset_id"), section["id"])
        image_path = asset.get("resolved_path") or asset["path"]
        width_px, height_px, aspect = self._image_size(image_path)
        caption = str(block.get("caption", ""))
        caption_size = self.style["slides"]["typography"]["caption_pt"]
        caption_height = estimate_text_height(caption, caption_size, self.body_width,
                                              self.direction == "rtl") + 0.04
        source_line = _asset_source_line(asset)
        source_height = estimate_text_height(source_line, caption_size * 0.9, self.body_width,
                                             self.direction == "rtl") if source_line else 0
        max_width = self.body_width * self.style["rules"]["images"]["max_width_ratio"]
        max_height = self.slide_height * self.style["rules"]["images"]["max_height_ratio"]
        available = self.body_height - caption_height - source_height - 0.2
        max_height = min(max_height, available)
        if max_height <= 0.5 or max_width <= 1:
            raise ValueError("Not enough slide space for a readable image; content needs another slide")
        draw_width = max_width
        draw_height = draw_width / aspect
        if draw_height > max_height:
            draw_height = max_height
            draw_width = draw_height * aspect
        total = draw_height + caption_height + source_height + 0.22
        self.ensure_space(min(total, self.body_height - 0.05))
        from pptx.util import Inches

        placement = block.get("placement")
        reviewed_placement = (asset.get("placements", {}).get(section["id"]) or {}).get("placement")
        if not placement or placement == "auto":
            placement = reviewed_placement or "auto"
        if placement == "auto":
            placement = self.style.get("layout", {}).get("image_alignment", "end")
        if placement == "start":
            alignment = "right" if self.direction == "rtl" else "left"
        elif placement == "end":
            alignment = "left" if self.direction == "rtl" else "right"
        elif placement in {"left", "right"}:
            alignment = placement
        else:
            alignment = "center"
        if alignment == "left":
            left = self.margin
        elif alignment == "right":
            left = self.slide_width - self.margin - draw_width
        else:
            left = self.margin + (self.body_width - draw_width) / 2
        slide_shape = self.slide.shapes.add_picture(
            str(Path(image_path).resolve()), Inches(left), Inches(self.cursor),
            width=Inches(draw_width), height=Inches(draw_height)
        )
        # Keep scientific labels: the whole image is fitted, never cropped.
        slide_shape.name = f"Reviewed image {asset['id']}"
        self.cursor += draw_height + 0.08
        _add_text(self.slide, self.margin, self.cursor, self.body_width, caption_height,
                  caption, self.style, self.direction, self.labels, size_key="caption_pt",
                  color=self.style["palette"]["muted"])
        self.cursor += caption_height
        if source_line:
            _add_text(self.slide, self.margin, self.cursor, self.body_width, source_height,
                      f"{self.labels['source']}: {source_line}", self.style, self.direction,
                      self.labels, size_key="caption_pt", color=self.style["palette"]["muted"])
            self.cursor += source_height
        self.cursor += 0.12
        self._notes(block.get("evidence"), "image")

    def _instruction_text_height(self, text, size, width):
        # Technical identifiers can wrap within one long word in PowerPoint.
        capacity = max(10, int(width * 72 / (size * (.58 if self.direction == "rtl" else .56))))
        extra = sum((len(word) - 1) // capacity for word in str(text).split())
        return estimate_text_height(text, size, width, self.direction == "rtl") + extra * size * 1.2 / 72 + .1

    def _instruction_regions(self, block, aspect, top, pieces, size):
        available = self.body_bottom - top
        if instructions.layout(block, aspect) == "stacked":
            needed = sum(self._instruction_text_height(p, size, self.body_width) for p in pieces)
            image_height = available - needed - .15
            image_area = (self.body_left, top, self.body_width, image_height)
            copy_area = (self.body_left, top + image_height + .15, self.body_width, needed)
        else:
            image_area = (self.body_left, top, self.body_width * .55, available)
            copy_area = (self.body_left + self.body_width * .58, top, self.body_width * .42, available)
            if self.direction == "rtl":
                copy_area = (self.body_left, top, self.body_width * .42, available)
                image_area = (self.body_left + self.body_width * .45, top, self.body_width * .55, available)
            needed = sum(self._instruction_text_height(p, size, copy_area[2]) for p in pieces)
        if needed > copy_area[3] + .001 or image_area[3] < .7:
            raise ValueError("Split the dense instruction at a meaningful substep; its image and steps must fit one slide")
        return image_area, copy_area

    def _instruction_callouts(self, slide, block, geometry):
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.util import Inches, Pt

        left, top, width, height = geometry
        for mark in block.get("callouts", []):
            x, y, w, h = mark["box"]
            rect = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left + x * width),
                Inches(top + y * height), Inches(w * width), Inches(h * height))
            rect.name = f"Instruction callout {mark['step']}"
            rect.fill.background()
            rect.line.color.rgb = RGBColor.from_string("CD5710")
            rect.line.width = Pt(1.5)
            badge_size = .23
            badge_x = min(left + width - badge_size, left + (x + w) * width - badge_size / 2)
            badge_y = max(top, top + y * height - badge_size / 2)
            badge = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(badge_x), Inches(badge_y),
                                           Inches(badge_size), Inches(badge_size))
            badge.name = f"Instruction callout number {mark['step']}"
            badge.fill.solid()
            badge.fill.fore_color.rgb = RGBColor.from_string("CD5710")
            badge.line.fill.background()
            _add_text(slide, badge_x, badge_y, badge_size, badge_size, str(mark["step"]),
                      self.style, "ltr", self.labels, size_pt=10, color="#FFFFFF", bold=True, align_center=True)

    def _instruction_picture(self, slide, asset, block, area):
        from pptx.util import Inches

        path = asset.get("resolved_path") or asset["path"]
        width, height, _ = self._image_size(path)
        source = instructions.recording_label(asset) or _asset_source_line(asset)
        caption = block["caption"] + (f"\n{source}" if source else "")
        caption_height = self._instruction_text_height(caption, 10, area[2])
        picture_area = (area[0], area[1], area[2], area[3] - caption_height - .1)
        if picture_area[3] < .5:
            raise ValueError("Split the instruction at a meaningful substep; the caption leaves no readable image")
        geometry = instructions.fit_image(width, height, picture_area)
        left, top, draw_width, draw_height = geometry
        shape = slide.shapes.add_picture(str(Path(path).resolve()), Inches(left), Inches(top),
                                        width=Inches(draw_width), height=Inches(draw_height))
        shape.name = f"Reviewed instruction image {asset['id']}"
        self._instruction_callouts(slide, block, geometry)
        credit_box = _add_text(slide, area[0], area[1] + picture_area[3] + .05, area[2], caption_height,
                               caption, self.style, self.direction, self.labels, size_pt=10,
                               color=self.style["palette"]["muted"])
        source_url = asset.get("source_url") or asset.get("source_page")
        if isinstance(source_url, str) and re.fullmatch(r"https?://[^\s]+", source_url):
            for paragraph in credit_box.text_frame.paragraphs[1:]:
                for run in paragraph.runs:
                    run.hyperlink.address = source_url

    def add_instruction(self, section, block):
        instructions.validate(block)
        asset = authoring.image_asset(self.root, block["asset_id"], section["id"])
        _, _, aspect = self._image_size(asset.get("resolved_path") or asset["path"])
        self.section_starts.setdefault(section["id"], len(self.presentation.slides))
        slide = self._new_slide("content_layout")
        timing = output_preferences.section_time_label(self.root, section)
        context = section["title"] + (f" · {timing}" if timing else "")
        if block.get("origin") == "example":
            context = self.labels["additional_example"] + " - " + context
        _add_text(slide, self.body_left, self.margin * .55, self.body_width, .25,
                  context, self.style, self.direction, self.labels, size_pt=11,
                  color=self.style["palette"]["muted"])
        title_size = min(30, self.style["slides"]["typography"]["title_pt"])
        title_height = self._instruction_text_height(block["title"], title_size, self.body_width)
        if title_height > 1.1:
            raise ValueError("Split the instruction title at a meaningful substep; it is too long for one slide")
        _add_text(slide, self.body_left, self.margin + .05, self.body_width, title_height,
                  block["title"], self.style, self.direction, self.labels, size_pt=title_size,
                  bold=True, color=self.style["palette"]["accent"])
        top = self.margin + title_height + .3
        if self.using_reference_template:
            top = max(top, self.body_top)
        size = (self.style["page"]["typography"]["body_pt"] if self.pdf_tokens else
                min(20, self.style["slides"]["typography"]["body_pt"]))
        pieces = [block["text"], *[f"{i}. {step}" for i, step in enumerate(block["steps"], 1)], block["result"]]
        image_area, copy_area = self._instruction_regions(block, aspect, top, pieces, size)
        y = copy_area[1]
        for index, piece in enumerate(pieces):
            text_height = self._instruction_text_height(piece, size, copy_area[2])
            _add_text(slide, copy_area[0], y, copy_area[2], text_height - .02, piece,
                      self.style, self.direction, self.labels, size_pt=size, bold=index == len(pieces) - 1)
            y += text_height
        self._instruction_picture(slide, asset, block, image_area)
        self.slide, self.cursor = slide, self.body_bottom
        self._notes(block.get("evidence"), "instruction")

    def add_table(self, block):
        header = block.get("header") or block.get("columns") or []
        rows = [[str(cell) for cell in row] for row in block.get("rows", [])]
        if self.preview:
            rows = rows[:5]
        if not rows:
            raise ValueError("Table block has no rows")
        columns = len(rows[0])
        header = [str(cell) for cell in header] if header else []
        size = self.style["slides"]["typography"]["table_pt"]
        widths = []
        for column in range(columns):
            sample = [header[column]] if header else []
            sample.extend(row[column] for row in rows)
            longest = max(len(str(cell)) for cell in sample) if sample else 8
            widths.append(max(0.9, min(4.2, longest * size * 0.009 + 0.35)))
        total_width = sum(widths)
        widths = [value * self.body_width / total_width for value in widths]
        row_heights = []
        for row in rows:
            lines = max(
                estimate_lines(cell, size, widths[index] - 0.2, self.direction == "rtl")
                for index, cell in enumerate(row)
            )
            row_heights.append(max(0.34, lines * size * 1.22 / 72 + 0.12))
        header_height = 0.0
        if header:
            header_height = max(0.34, max(
                estimate_lines(cell, size, widths[index] - 0.2, self.direction == "rtl")
                for index, cell in enumerate(header)
            ) * size * 1.22 / 72 + 0.12)

        remaining = list(range(len(rows)))
        while remaining:
            available = self.body_height - 0.1
            chosen = []
            used = header_height
            for index in remaining:
                if chosen and used + row_heights[index] > available:
                    break
                chosen.append(index)
                used += row_heights[index]
            if not chosen:
                chosen = [remaining[0]]
                used = header_height + row_heights[remaining[0]]
            self.ensure_space(min(used + 0.12, self.body_height - 0.05))
            used = min(used, self.body_height - 0.05)
            from pptx.util import Inches, Pt

            rows_count = len(chosen) + (1 if header else 0)
            shape = self.slide.shapes.add_table(
                rows_count, columns, Inches(self.margin), Inches(self.cursor),
                Inches(self.body_width), Inches(used)
            )
            table = shape.table
            table.first_row = bool(header)
            table.horz_banding = False
            table._tbl.tblPr.set("rtl", "1" if self.direction == "rtl" else "0")
            for index, width in enumerate(widths):
                table.columns[index].width = Inches(width)
            table_spec = self.table_spec or {}
            header_fill = table_spec.get("header_fill") or self.style["palette"]["table_header"]
            header_text = table_spec.get("header_text") or self.style["palette"]["table_header_text"]
            border_colour = table_spec.get("border") or self.style["palette"]["rule"]
            body_fill = table_spec.get("body_fill") or self.style["palette"]["surface"]
            zebra_fill = table_spec.get("zebra_fill") or self.style["palette"]["table_zebra"]
            row_offset = 0
            if header:
                table.rows[0].height = Inches(header_height)
                for column, text in enumerate(header):
                    cell = table.cell(0, column)
                    _fill_cell(cell, header_fill)
                    _table_cell_text(cell, text, self.style, self.direction,
                                     size, header_text, bold=True,
                                     font_family=table_spec.get("header_font"))
                    _apply_cell_border(cell, border_colour, 0.75)
                row_offset = 1
            for row_index, index in enumerate(chosen, start=row_offset):
                table.rows[row_index].height = Inches(row_heights[index])
                for column, text in enumerate(rows[index]):
                    fill = (
                        zebra_fill
                        if self.style["rules"]["tables"]["zebra"] and (index % 2 == 1)
                        else body_fill
                    )
                    cell = table.cell(row_index, column)
                    _fill_cell(cell, fill)
                    _table_cell_text(cell, text, self.style, self.direction, size,
                                     self.style["palette"]["text"])
                    _apply_cell_border(cell, border_colour, 0.6)
            self._notes(block.get("evidence"), "table")
            self.cursor += used + 0.12
            remaining = remaining[len(chosen):]
            if remaining:
                self.new_content_slide(self.section, True)
        if block.get("caption"):
            caption_height = estimate_text_height(
                block["caption"], self.style["slides"]["typography"]["caption_pt"],
                self.body_width, self.direction == "rtl"
            )
            self.ensure_space(caption_height)
            _add_text(self.slide, self.margin, self.cursor, self.body_width, caption_height,
                      block["caption"], self.style, self.direction, self.labels,
                      size_key="caption_pt", color=self.style["palette"]["muted"])
            self.cursor += caption_height + 0.1

    # -- appendices --------------------------------------------------------

    def add_appendix(self):
        uncertainties = self.content.get("uncertainties") or []
        omissions = [] if instructions.tutorial_mode(self.content) else self.content.get("omissions") or []
        if not uncertainties and not omissions:
            return
        size = self.style["slides"]["typography"]["body_pt"] - 1
        if uncertainties:
            queue = [f"{item.get('location')}: {item.get('description')} [{item.get('evidence_id')}]"
                     for item in uncertainties]
            while queue:
                slide = self._new_slide()
                self._header(slide, self.labels["uncertainties"])
                box, frame = _textbox(slide, self.margin, self.margin + self.title_bar,
                                      self.body_width, self.body_height, self.style, self.direction)
                used = 0
                while queue and used < self.body_height * 0.9:
                    item = queue.pop(0)
                    paragraph = frame.paragraphs[0] if used == 0 else frame.add_paragraph()
                    _add_bullet(paragraph, "•")
                    _paragraph_rtl(paragraph, self.direction == "rtl")
                    _add_runs(paragraph, item, self.style, self.direction, size,
                              self.style["palette"]["text"])
                    used += estimate_text_height(item, size, self.body_width - 0.3,
                                                 self.direction == "rtl")
        if omissions:
            counts = {}
            for omission in omissions:
                counts[omission["kind"]] = counts.get(omission["kind"], 0) + 1
            summary = "; ".join(f"{kind}: {count}" for kind, count in sorted(counts.items()))
            details = [
                f"{item['evidence_id']} — {item['kind']}: {item['reason']}"
                for item in omissions
                if item["kind"] in {"unreadable", "outside_scope", "superseded", "noninstructional"}
            ]
            slide = self._new_slide()
            self._header(slide, self.labels["omissions"])
            _add_text(slide, self.margin, self.margin + self.title_bar, self.body_width,
                      0.8, summary, self.style, self.direction, self.labels,
                      size_key="body_pt", color=self.style["palette"]["muted"])
            if details:
                box, frame = _textbox(slide, self.margin, self.margin + self.title_bar + 0.9,
                                      self.body_width, self.body_height - 0.95, self.style,
                                      self.direction)
                for index, item in enumerate(details[:12]):
                    paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
                    _add_bullet(paragraph, "•")
                    _paragraph_rtl(paragraph, self.direction == "rtl")
                    _add_runs(paragraph, item, self.style, self.direction, size - 1,
                              self.style["palette"]["text"])
                if len(details) > 12:
                    _add_text(slide, self.margin, self.slide_height - self.margin - 0.5,
                              self.body_width, 0.3,
                              f"Showing 12 of {len(details)} detailed omissions; the complete record is in content.json.",
                              self.style, self.direction, self.labels, size_key="caption_pt",
                              color=self.style["palette"]["muted"])

    def add_footers(self):
        total = len(self.presentation.slides)
        title = str(self.content.get("title", ""))[:60]
        footer = ((self.design or {}).get("geometry") or {}).get("footer") or {}
        if self.using_reference_template and footer:
            footer_left = float(footer.get("left_in", self.margin))
            footer_top = float(footer.get("top_in", self.footer_top))
            footer_width = float(footer.get("width_in", self.body_width))
            footer_height = max(0.22, float(footer.get("height_in", 0.26)))
            footer_left = max(self.margin * 0.4, footer_left)
            footer_width = max(footer_width, self.body_width)
            footer_width = min(footer_width, self.slide_width - footer_left - 0.05)
            footer_top = max(footer_top, self.body_bottom - 0.02)
            footer_height = min(footer_height, max(0.2, self.slide_height - footer_top - 0.02))
            footer_top = min(footer_top, self.slide_height - footer_height - 0.02)
        else:
            footer_left = self.margin
            footer_top = self.slide_height - self.margin * 0.55
            footer_width = self.body_width
            footer_height = 0.26
            if self.pdf_tokens:
                footer_tokens = self.pdf_tokens.get("footer") or {}
                items = footer_tokens.get("items") or []
                item = next((entry for entry in items if entry.get("text_kind") == "page-number"), None) \
                    or next((entry for entry in items if entry.get("text_kind") == "label"), None) \
                    or footer_tokens
                page_height = (self.pdf_tokens.get("page_size_mm") or {}).get("height") or self.slide_height
                if item.get("y_mm") is not None:
                    footer_top = max(self.body_bottom + 0.02,
                                     float(item["y_mm"]) / max(1.0, page_height) * self.slide_height)
                footer_height = max(
                    0.2, float(item.get("size_pt") or 9) / 72 * 1.9
                )
                footer_top = min(footer_top, self.slide_height - footer_height - 0.02)
            footer_width = min(footer_width, self.slide_width - footer_left - 0.05)
        number_width = 1.4
        chip_reserve = 0.0
        if self.using_reference_template:
            for decoration in ((self.design or {}).get("recurring") or {}).get("native_shapes") or []:
                if decoration.get("role") == "footer":
                    box = decoration.get("box") or {}
                    if float(box.get("left_in", 0)) > self.slide_width * 0.62:
                        chip_reserve = max(chip_reserve, 1.3)
        title_width = max(1.0, footer_width - number_width - 0.1 - chip_reserve)
        footer_size = self.style["slides"]["typography"]["caption_pt"]
        footer_colour = self.style["palette"]["muted"]
        if self.pdf_tokens:
            footer_tokens = self.pdf_tokens.get("footer") or {}
            items = footer_tokens.get("items") or []
            item = next((entry for entry in items if entry.get("text_kind") == "page-number"), None) \
                or next((entry for entry in items if entry.get("text_kind") == "label"), None) \
                or footer_tokens
            footer_size = float(item.get("size_pt") or footer_size)
            footer_colour = item.get("colour") or footer_colour
        if self.direction == "rtl":
            title_left = footer_left + number_width + 0.1
            number_left = footer_left
        else:
            title_left = footer_left
            number_left = footer_left + footer_width - number_width - chip_reserve
        for index, slide in enumerate(self.presentation.slides, start=1):
            _add_text(slide, title_left, footer_top, title_width, footer_height, title,
                      self.style, self.direction, self.labels, size_key="caption_pt",
                      color=footer_colour, size_pt=footer_size)
            page = f"{index} / {total}"
            _add_text(slide, number_left, footer_top, number_width, footer_height, page,
                      self.style, self.direction, self.labels, size_key="caption_pt",
                      color=footer_colour, rtl=False, size_pt=footer_size)
        # Footers are placed in the reserved footer band; they do not overlap body.

    # -- build -------------------------------------------------------------

    def build(self, preview=False):
        self.add_cover(preview=preview)
        if not preview:
            self.add_toc()
        sections = self.content["sections"]
        if preview:
            sections = _preview_sections(self.content)
        for index, section in enumerate(sections, start=1):
            self.section = section
            self.section_index = index
            if not preview and not instructions.tutorial_mode(self.content):
                self.add_section_divider(section, index)
            if section["blocks"][0].get("type") != "instruction":
                self.new_content_slide(section, False)
            for block in _preview_blocks(section) if preview else section["blocks"]:
                kind = block.get("type")
                if kind == "instruction":
                    self.add_instruction(section, block)
                elif kind == "paragraph":
                    self.add_paragraph(block)
                elif kind == "bullets":
                    self.add_bullets(block)
                elif kind == "table":
                    self.add_table(block)
                elif kind == "equation":
                    self.add_equation(block)
                elif kind == "image":
                    self.add_image(section, block)
                else:
                    raise ValueError(f"Unsupported block type during slide rendering: {kind!r}")
        if not preview:
            self.add_appendix()
        self.add_footers()
        return self.presentation

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            core = self.presentation.core_properties
            core.title = str(self.content.get("title", ""))[:255]
            core.subject = str(self.content.get("subtitle", ""))[:255]
            core.author = ""
            core.last_modified_by = ""
            core.comments = ""
            core.keywords = ""
        except Exception:
            pass
        self.presentation.save(str(path))
        return path


def _fill_cell(cell, colour):
    cell.fill.solid()
    cell.fill.fore_color.rgb = _rgb(colour)


def _table_cell_text(cell, text, style, direction, size, colour, bold=False, font_family=None):
    from pptx.enum.text import MSO_ANCHOR
    from pptx.util import Inches, Pt

    frame = cell.text_frame
    frame.word_wrap = True
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    cell.margin_left = Inches(0.05)
    cell.margin_right = Inches(0.05)
    cell.margin_top = Inches(0.025)
    cell.margin_bottom = Inches(0.025)
    paragraph = frame.paragraphs[0]
    _paragraph_rtl(paragraph, direction == "rtl")
    _add_runs(paragraph, text, style, direction, size, colour, bold=bold)
    if font_family:
        for run in paragraph.runs:
            run.font.name = font_family


def _asset_source_line(asset):
    parts = []
    attribution = asset.get("attribution") or asset.get("creator")
    if attribution:
        parts.append(str(attribution))
    source = asset.get("source_url") or asset.get("source_page")
    if source:
        parts.append(str(source))
    license_value = asset.get("license") or asset.get("licensing")
    if license_value:
        parts.append(str(license_value))
    if asset.get("origin") == "user":
        original = asset.get("original_path") or asset.get("path")
        if original:
            parts.append(f"Supplied by user: {Path(original).name}")
    timestamp = asset.get("timestamp")
    if timestamp is not None:
        parts.append(f"lecture time {timestamp}")
    return " · ".join(parts)


def _preview_sections(content):
    if instructions.tutorial_mode(content):
        selected = instructions.preview_sections(content)
        if selected:
            return selected
    sections = content["sections"]
    used = set()
    chosen = []
    for section in sorted(sections, key=lambda item: (-len(item.get("blocks", [])), item["id"])):
        blocks = []
        for block in section.get("blocks", []):
            kind = block.get("type")
            if kind not in used or kind == "paragraph":
                blocks.append(block)
                used.add(kind)
            if len(blocks) >= 4:
                break
        if blocks:
            chosen.append(section)
        if used >= {"paragraph", "bullets", "table", "equation", "image"} or len(chosen) >= 2:
            break
    return chosen or sections[:1]


def _preview_blocks(section):
    blocks = []
    used = set()
    for block in section.get("blocks", []):
        kind = block.get("type")
        if kind not in used or kind in {"paragraph", "instruction"}:
            blocks.append(block)
            used.add(kind)
        if len(blocks) >= 4:
            break
    return blocks or section.get("blocks", [])[:2]


# ---------------------------------------------------------------------------
# PowerPoint rendering (optional, honest when unavailable)
# ---------------------------------------------------------------------------


def _clear_rendered_previews(destination):
    """Remove owned preview images before a renderer can fail or be unavailable."""

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    for stale in list(destination.glob("*.PNG")) + list(destination.glob("*.png")):
        stale.unlink(missing_ok=True)


def _export_rendered_slides_powerpoint(path, destination, limit=0):
    """Render actual slides through a separate PowerPoint instance if available."""
    import sys

    _clear_rendered_previews(destination)
    if sys.platform != "win32":
        return {"available": False, "error": "PowerPoint rendering is only automated on Windows"}
    try:
        import pythoncom
        import win32com.client
        from pptx import Presentation
    except Exception as error:
        return {"available": False, "error": f"PowerPoint automation is unavailable: {error}"}
    destination = Path(destination)
    pythoncom.CoInitialize()
    app = None
    deck = None
    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")
        try:
            app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
        except Exception as error:
            raise RuntimeError(
                f"PowerPoint macro security could not be force-disabled; refusing to open the original: {error}"
            ) from error
        deck = app.Presentations.Open(str(Path(path).resolve()), ReadOnly=True, WithWindow=False)
        width = 1600
        height = 900
        try:
            presentation = Presentation(str(path))
            ratio = float(presentation.slide_height) / float(presentation.slide_width)
            height = max(600, int(width * ratio))
        except Exception:
            pass
        deck.Export(str(destination.resolve()), "PNG", width, height)
        files = sorted(
            set(destination.glob("*.PNG")) | set(destination.glob("*.png")),
            key=lambda item: int(re.search(r"(\d+)", item.stem).group(1))
            if re.search(r"(\d+)", item.stem) else 0,
        )
        if limit:
            selected = files[:limit]
            for extra in files[limit:]:
                extra.unlink(missing_ok=True)
        else:
            selected = files
        return {
            "available": True,
            "renderer": "Microsoft PowerPoint",
            "slides": len(selected),
            "paths": [str(item) for item in selected],
        }
    except Exception as error:
        for stale in list(destination.glob("*.PNG")) + list(destination.glob("*.png")):
            stale.unlink(missing_ok=True)
        message = str(error)
        return {
            "available": False,
            "error": message,
            "security_error": message.startswith("PowerPoint macro security"),
        }
    finally:
        if deck is not None:
            try:
                deck.Close()
            except Exception:
                pass
        if app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def export_rendered_slides(path, destination, limit=0):
    """Render actual slides with PowerPoint, then LibreOffice, or report honestly."""

    result = _export_rendered_slides_powerpoint(path, destination, limit=limit)
    if result.get("available"):
        return result
    if result.get("security_error"):
        return result
    try:
        fallback = design_module.pptx_soffice_previews(path, destination, limit=limit or 100)
        if fallback.get("available"):
            return fallback
        result["error"] = ((result.get("error") or "") + " | LibreOffice: "
                           + (fallback.get("error") or "unavailable")).strip(" |")
    except Exception as error:
        result["error"] = (result.get("error") or "") + f" | LibreOffice fallback failed: {error}"
    return result


def _export_pdf(path, destination):
    import sys

    if sys.platform != "win32":
        return {"available": False, "error": "PowerPoint PDF export is only automated on Windows"}
    try:
        import pythoncom
        import win32com.client
    except Exception as error:
        return {"available": False, "error": str(error)}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pythoncom.CoInitialize()
    app = None
    deck = None
    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")
        try:
            app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
        except Exception as error:
            raise RuntimeError(
                f"PowerPoint macro security could not be force-disabled; refusing to open the original: {error}"
            ) from error
        deck = app.Presentations.Open(str(Path(path).resolve()), ReadOnly=True, WithWindow=False)
        deck.SaveAs(str(destination.resolve()), 32)  # ppSaveAsPDF
        return {"available": True, "path": str(destination)}
    except Exception as error:
        return {"available": False, "error": str(error)}
    finally:
        if deck is not None:
            try:
                deck.Close()
            except Exception:
                pass
        if app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


# ---------------------------------------------------------------------------
# Read-back validation
# ---------------------------------------------------------------------------


def _slide_texts(prs):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    slides = []
    for index, slide in enumerate(prs.slides, start=1):
        texts = []
        pictures = 0
        tables = 0
        table_rows = 0
        fonts = set()
        rtl_paragraphs = 0
        links = 0
        bounds_problems = []
        for shape in slide.shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                pictures += 1
            if getattr(shape, "has_table", False) and shape.has_table:
                tables += 1
                table_rows += len(shape.table.rows)
                for row in shape.table.rows:
                    for cell in row.cells:
                        if cell.text.strip():
                            texts.append(cell.text)
                        for paragraph in cell.text_frame.paragraphs:
                            for run in paragraph.runs:
                                if run.font.name:
                                    fonts.add(run.font.name)
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                if shape.text.strip():
                    texts.append(shape.text)
                for paragraph in shape.text_frame.paragraphs:
                    ppr = paragraph._p.pPr
                    if ppr is not None and ppr.get("rtl") == "1":
                        rtl_paragraphs += 1
                    for run in paragraph.runs:
                        if run.font.name:
                            fonts.add(run.font.name)
                        if run.hyperlink and run.hyperlink.address:
                            links += 1
            try:
                left = float(shape.left.inches)
                top = float(shape.top.inches)
                right = left + float(shape.width.inches)
                bottom = top + float(shape.height.inches)
                if left < -0.05 or top < -0.05 or right > float(prs.slide_width.inches) + 0.05 or \
                        bottom > float(prs.slide_height.inches) + 0.05:
                    bounds_problems.append(shape.name)
            except Exception:
                pass
        notes = ""
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        rpr = run._r.rPr
                        if rpr is not None:
                            cs = rpr.find("{http://schemas.openxmlformats.org/drawingml/2006/main}cs")
                            if cs is not None and cs.get("typeface"):
                                fonts.add(cs.get("typeface"))
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        for paragraph in cell.text_frame.paragraphs:
                            for run in paragraph.runs:
                                rpr = run._r.rPr
                                if rpr is not None:
                                    cs = rpr.find("{http://schemas.openxmlformats.org/drawingml/2006/main}cs")
                                    if cs is not None and cs.get("typeface"):
                                        fonts.add(cs.get("typeface"))
        slides.append({
            "slide": index,
            "text": "\n".join(texts),
            "texts": texts,
            "pictures": pictures,
            "tables": tables,
            "table_rows": table_rows,
            "fonts": sorted(fonts),
            "rtl_paragraphs": rtl_paragraphs,
            "links": links,
            "notes": notes,
            "bounds_problems": bounds_problems,
        })
    return slides


def _token_coverage(expected, corpus):
    diacritics = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
    marks = r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]"
    expected_text = re.sub(r"\s+(" + marks + ")", r"\1", str(expected))
    corpus_text = re.sub(r"\s+(" + marks + ")", r"\1", str(corpus))
    expected_text = diacritics.sub("", expected_text)
    corpus_text = diacritics.sub("", corpus_text)
    tokens = TOKEN_RE.findall(expected_text)
    if not tokens:
        return 1.0, []
    missing = [token for token in tokens if token not in corpus_text]
    return 1 - len(missing) / len(tokens), missing[:12]


def inspect_pptx(root, path, content, style, text_only, content_file=None, render=True,
                 render_directory=None):
    from pptx import Presentation

    content = content or {"sections": []}

    prs = Presentation(str(path))
    slides = _slide_texts(prs)
    corpus = "\n".join(item["text"] for item in slides)
    everything = corpus + "\n" + "\n".join(item["notes"] for item in slides)
    inventory = []
    for section, block in authoring.iter_blocks(content):
        expected = authoring.block_plain_text(block)
        coverage, missing = _token_coverage(expected, everything)
        inventory.append({
            "section": section["id"],
            "type": block.get("type"),
            "token_coverage": round(coverage, 3),
            "missing_tokens": missing,
        })
    total_pictures = sum(item["pictures"] for item in slides)
    total_tables = sum(item["tables"] for item in slides)
    total_table_rows = sum(item["table_rows"] for item in slides)
    expected_tables = sum(1 for _, block in authoring.iter_blocks(content) if block.get("type") == "table")
    expected_rows = sum(len(block.get("rows", [])) for _, block in authoring.iter_blocks(content)
                        if block.get("type") == "table")
    expected_pictures = sum(1 for _, block in authoring.iter_blocks(content) if block.get("type") in {"image", "instruction"})

    problems = []
    warnings = []
    if len(prs.slides) < 2:
        problems.append("Deck has fewer than two slides")
    if text_only and total_pictures:
        problems.append("Text-only deck contains picture objects")
    if not text_only and total_pictures < expected_pictures:
        problems.append("Not every reviewed image block produced a picture")
    if total_tables < expected_tables:
        problems.append("A table block was not emitted natively")
    if total_table_rows < expected_rows:
        problems.append("Table rows are missing from the deck")
    low = [item for item in inventory if item["token_coverage"] < 0.95]
    if low:
        problems.append(
            "Possible missing slide content: " + ", ".join(
                f"{item['section']}/{item['type']} ({item['token_coverage']:.0%})" for item in low[:8]
            )
        )
    empty = [item["slide"] for item in slides if not item["text"].strip() and not item["pictures"]]
    if empty:
        warnings.append(f"Slides without text or pictures: {empty}")
    if any(item["bounds_problems"] for item in slides):
        problems.append("Shapes extend beyond the slide boundary")
    notes_only = []
    for section, block in authoring.iter_blocks(content):
        expected = authoring.block_plain_text(block)
        coverage_corpus, _ = _token_coverage(expected, corpus)
        if coverage_corpus < 0.6:
            notes_coverage, _ = _token_coverage(expected, everything)
            if notes_coverage >= 0.8:
                notes_only.append(f"{section['id']}/{block.get('type')}")
    if notes_only:
        problems.append("Substantive content appears only in notes: " + ", ".join(notes_only[:8]))

    embedded_media = []
    external_links = []
    external_hyperlinks = 0
    try:
        import xml.etree.ElementTree as ElementTree

        relationship_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            embedded_media = [name for name in names if name.startswith("ppt/media/")]
            for name in names:
                if not name.endswith(".rels"):
                    continue
                try:
                    root_node = ElementTree.fromstring(archive.read(name))
                except ElementTree.ParseError:
                    warnings.append(f"Unreadable relationship file: {name}")
                    continue
                for relationship in root_node.findall(f"{relationship_ns}Relationship"):
                    if relationship.get("TargetMode") != "External":
                        continue
                    target = relationship.get("Target", "")
                    relationship_type = relationship.get("Type", "")
                    if relationship_type.endswith("/hyperlink"):
                        external_hyperlinks += 1
                        continue
                    external_links.append({"part": name, "target": target, "type": relationship_type})
                payload = archive.read(name).decode("utf-8", errors="replace")
                if "work/render" in payload or "work\\\\render" in payload:
                    warnings.append(f"Internal relationship references a working path: {name}")
    except Exception as error:
        problems.append(f"PPTX package could not be inspected: {error}")

    if external_links:
        warnings.append(
            "The PPTX references external non-hyperlink resources; verify they are available "
            "after working-file cleanup"
        )
    if text_only and embedded_media:
        problems.append("Text-only deck contains embedded media (picture objects or picture backgrounds)")

    render_result = {"available": False, "error": "Rendering disabled"}
    if render:
        destination = Path(render_directory) if render_directory is not None else (
            Path(root) / "work" / "render" / "pptx-slides"
        )
        limit = 0 if len(prs.slides) <= 24 else 12
        render_result = export_rendered_slides(path, destination, limit=limit)
        if render_result.get("available"):
            render_result["not_rendered"] = max(0, len(prs.slides) - render_result["slides"])
            for rendered in render_result.get("paths", []):
                _register(root, rendered)
        else:
            warnings.append(
                "Actual PowerPoint slide rendering was not available; XML/read-back inspection "
                "is not proof the deck was opened."
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "format": "pptx",
        "ok": not problems,
        "file": str(path),
        "content_file": str(content_file) if content_file else None,
        "reference_design": {
            "available": bool(isinstance(style, dict) and style.get("design")),
            "source_format": (style.get("design") or {}).get("source_format") if isinstance(style, dict) else None,
            "method": (style.get("design") or {}).get("method") if isinstance(style, dict) else None,
            "template": ((style.get("design") or {}).get("template") or {}) if isinstance(style, dict) else None,
            "source_slides_removed": (
                ((style.get("design") or {}).get("template") or {}).get("removed_slides")
                if isinstance(style, dict) else None
            ),
            "note": (
                "Reference masters/layouts/theme are reused through a sanitized native template; "
                "original slides, prose, tables, charts and pictures are not copied."
                if isinstance(style, dict) and style.get("design") else
                "No reference design was attached to this style."
            ),
        },
        "slides": len(prs.slides),
        "slide_size_in": [round(float(prs.slide_width.inches), 3), round(float(prs.slide_height.inches), 3)],
        "characters": len(corpus),
        "arabic_characters": len(ARABIC_RE.findall(corpus)),
        "selectable_text": bool(corpus.strip()),
        "text_only": text_only,
        "pictures": total_pictures,
        "expected_pictures": expected_pictures,
        "tables": total_tables,
        "table_rows": total_table_rows,
        "expected_table_rows": expected_rows,
        "fonts": sorted({font for item in slides for font in item["fonts"]}),
        "declared_fonts": sorted({
            style["fonts"][key]["family"]
            for key in ("latin", "arabic", "display", "mono")
            if isinstance(style, dict) and style.get("fonts", {}).get(key, {}).get("family")
        }) if isinstance(style, dict) else [],
        "font_verification": (
            "Declared font families are written into the file; exact installed-family "
            "substitution is not identifiable from the PPTX XML alone."
        ),
        "rtl_paragraphs": sum(item["rtl_paragraphs"] for item in slides),
        "links": sum(item["links"] for item in slides),
        "notes_slides": sum(1 for item in slides if item["notes"].strip()),
        "inventory": inventory,
        "problems": problems,
        "warnings": warnings,
        "embedded_media": len(embedded_media),
        "external_relationships": external_links,
        "external_hyperlinks": external_hyperlinks,
        "self_contained": not external_links,
        "render": render_result,
        "rendered_previews": render_result.get("paths", []),
        "renderer": render_result.get("renderer"),
        "rendering_note": (
            None if render_result.get("available")
            else render_result.get("error", "Actual slide rendering was not available")
        ),
        "checks_not_performed": [
            "scientific accuracy and semantic completeness",
            "human inspection of every rendered Arabic/mixed-language slide at full size",
            "pixel-identical reference-style reproduction",
            "installed-font substitution (renderer images must be inspected; XML only records declared families)",
        ],
        "slide_details": slides,
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def render_preview(root, content_path, style_path):
    """Build a small representative editable deck and try to export a PDF preview."""

    root = Path(root)
    content, content_file = _load_content(root, content_path)
    style = _style(root, style_path)
    require_style_direction(style, content.get("direction", "ltr"))
    text_only = authoring.image_mode(root) == "text"
    design_identity = design_module.verify_style_design(style, style.get("_source_path"))
    if design_identity.get("source_format") == "pdf":
        design_identity["renderer_note"] = (
            "A PDF reference supplies typography, colours and table tokens to this deck. "
            "PDF page margins/headers are not a native slide master; compare the PDF output for native page geometry."
        )
    report_content = authoring.validate_content(root, content)
    readiness = None
    try:
        readiness = authoring.require_output_readiness(root, content)
    except ValueError as error:
        readiness = {"output_readiness": False, "reason": str(error)}

    builder = DeckBuilder(content, style, root, preview=True, text_only=text_only)
    builder.build(preview=True)
    destination = root / "work" / "preview-slides.pptx"
    temporary = destination.with_name(destination.name + ".tmp")
    builder.save(temporary)
    temporary.replace(destination)
    _register(root, destination)

    preview_pdf_path = root / "work" / "preview.pdf"
    preview_pdf_path.unlink(missing_ok=True)
    pdf_result = _export_pdf(destination, preview_pdf_path)
    images = export_rendered_slides(destination, root / "work" / "preview-slides", limit=6)
    if pdf_result.get("available"):
        _register(root, root / "work" / "preview.pdf")
    for path in images.get("paths", []):
        _register(root, path)

    if style.get("design"):
        reference_result = design_module.ensure_reference_previews(
            style, style.get("_source_path"),
            root / "work" / "reference-previews" / "pptx",
            kind="pptx",
        )
        reference_images = reference_result.get("paths", [])
        for path in reference_images:
            _register(root, path)
    else:
        reference_result = {"available": False, "reason": "This style has no reference design", "paths": []}
        reference_images = []
    comparison_dir = root / "work" / "reference-comparison" / "pptx-preview"
    if reference_images and images.get("paths"):
        comparison = design_module.make_side_by_side(
            reference_images,
            images.get("paths", []),
            comparison_dir,
            title="Reference slides versus new editable slide preview",
            reference_total=((style.get("design") or {}).get("reference") or {}).get("slides"),
            new_total=len(builder.presentation.slides),
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

    key = authoring.approval_key(root, content, style_path)
    pdf_path = root / "work" / "preview.pdf"
    files = {str(destination.relative_to(root).as_posix()): file_digest(destination)}
    if pdf_result.get("available") and pdf_path.is_file():
        files[str(pdf_path.relative_to(root).as_posix())] = file_digest(pdf_path)
    meta = {
        "schema_version": SCHEMA_VERSION,
        "kind": "preview",
        "binding_digest": key,
        "preview": str(destination.relative_to(root).as_posix()),
        "preview_sha256": files[str(destination.relative_to(root).as_posix())],
        "files": files,
        "preview_pdf": str((root / "work" / "preview.pdf").relative_to(root).as_posix())
        if pdf_result.get("available") else None,
        "renderer": images.get("renderer") if images.get("available") else None,
        "rendered_at": _utc(),
        "style": {"id": style["id"], "name": style["name"], "version": style["version"],
                  "fingerprint": style.get("fingerprint")},
        "reference_design": design_identity,
        "reference_previews": reference_result,
        "reference_comparison": comparison,
        "template_reused": builder.using_reference_template,
        "reference_decoration_shapes": builder.reference_decoration_count,
        "template_pictures_stripped": builder.design_picture_report,
        "slides": len(builder.presentation.slides),
        "source_review_complete": bool(readiness and readiness.get("output_readiness")),
        "content_file": str(content_file),
        "image_rendering": images,
        "pdf_export": pdf_result,
    }
    meta_path = root / "work" / "preview-meta.json"
    write_json(meta_path, meta)
    _register(root, meta_path)
    return {
        "ok": True,
        "preview_pptx": str(destination),
        "pptx": str(destination),
        "preview_pdf": str(root / "work" / "preview.pdf") if pdf_result.get("available") else None,
        "pdf": str(root / "work" / "preview.pdf") if pdf_result.get("available") else None,
        "preview_images": images.get("paths", []),
        "meta": str(meta_path),
        "binding_digest": key,
        "slides": len(builder.presentation.slides),
        "renderer": images.get("renderer"),
        "template_reused": builder.using_reference_template,
        "reference_design": design_identity,
        "reference_previews": reference_result,
        "reference_comparison": comparison,
        "template_pictures_stripped": builder.design_picture_report,
        "content_report": report_content,
        "source_readiness": readiness,
        "record_with": (
            'authoring.record_preview(root, content, style)'
            if pdf_result.get("available")
            else 'authoring.record_preview(root, content, style, preview_path="work/preview-slides.pptx")'
        ),
        "next_step": "Show the slides/PDF and the reference-versus-new comparison, then call authoring.accept_preview only after real user acceptance.",
    }


def render_final(root, content_path, style_path):
    """Build the complete editable deck after all final-output gates pass."""

    root = Path(root)
    content, content_file = _load_content(root, content_path)
    style = _style(root, style_path)
    require_style_direction(style, content.get("direction", "ltr"))
    text_only = authoring.image_mode(root) == "text"
    design_identity = design_module.verify_style_design(style, style.get("_source_path"))
    if design_identity.get("source_format") == "pdf":
        design_identity["renderer_note"] = (
            "A PDF reference supplies typography, colours and table tokens to this deck. "
            "PDF page margins/headers are not a native slide master; compare the PDF output for native page geometry."
        )
    gates = authoring.prepare_render(root, content, style_path, require_acceptance=True)

    builder = DeckBuilder(content, style, root, preview=False, text_only=text_only)
    builder.build(preview=False)
    outputs = root / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    destination = outputs / f"{_safe_stem(content)}.pptx"
    temporary = destination.with_name(destination.name + ".tmp")
    builder.save(temporary)
    # Re-check the approval binding immediately before replacing the final output.
    authoring.require_accepted_preview(root, content, style_path)
    temporary.replace(destination)

    readback = inspect_pptx(root, destination, content, style, text_only,
                            content_file=content_file, render=True)
    if readback["problems"]:
        destination.unlink(missing_ok=True)
        raise ValueError("PPTX read-back failed: " + "; ".join(readback["problems"]))

    if style.get("design"):
        reference_result = design_module.ensure_reference_previews(
            style, style.get("_source_path"),
            root / "work" / "reference-previews" / "pptx",
            kind="pptx",
        )
        reference_images = reference_result.get("paths", [])
        for path in reference_images:
            _register(root, path)
    else:
        reference_result = {"available": False, "reason": "This style has no reference design", "paths": []}
        reference_images = []
    comparison_dir = root / "work" / "reference-comparison" / "pptx-final"
    if reference_images and readback.get("render", {}).get("paths"):
        comparison = design_module.make_side_by_side(
            reference_images,
            readback.get("render", {}).get("paths", []),
            comparison_dir,
            title="Reference slides versus final editable deck",
            reference_total=((style.get("design") or {}).get("reference") or {}).get("slides"),
            new_total=readback.get("slides"),
        )
    elif not style.get("design"):
        comparison = {"available": False, "reason": "This style has no reference design to compare against."}
    else:
        comparison = {
            "available": False,
            "reason": reference_result.get("reason") or "Rendered reference or final-deck previews are unavailable.",
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
        "format": "pptx",
        "status": "complete",
        "ok": True,
        "output": str(destination),
        "pptx": str(destination),
        "output_sha256": file_digest(destination),
        "output_bytes": destination.stat().st_size,
        "content_file": str(content_file),
        "style": {"id": style["id"], "name": style["name"], "version": style["version"],
                  "fingerprint": style.get("fingerprint")},
        "reference_design": design_identity,
        "reference_previews": reference_result,
        "reference_comparison": comparison,
        "template_reused": builder.using_reference_template,
        "reference_decoration_shapes": builder.reference_decoration_count,
        "template_pictures_stripped": builder.design_picture_report,
        "gates": gates,
        "readback": readback,
        "rendered_previews": readback.get("render", {}).get("paths", []),
        "renderer": readback.get("render", {}).get("renderer"),
        "checks_not_performed": readback["checks_not_performed"],
        "generated_at": _utc(),
    }
    report_path = root / "work" / "render" / "pptx" / "pptx-report.json"
    write_json(report_path, report)
    _register(root, report_path)
    return report
