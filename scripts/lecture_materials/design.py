"""Reference-design extraction, native template preservation, and comparison.

This module is the production bridge between an inspected design reference and
the renderers:

* PDF references are measured (page size, margins, typography, colours, header
  and footer bands, simple recurring vector decoration) and rendered to real
  preview images.  Flattened pages are explicitly marked as visual estimates.
* PPTX/PPT references become a sanitized native template: all original slides
  are removed, while masters, layouts, placeholders, theme, dimensions and
  vector decoration are retained.  Recurring vector decoration that exists only
  on slides is captured as validated native DrawingML for reuse.  Original body
  text, tables, charts and pictures are never copied.
* Legacy binary ``.ppt`` is detected by its OLE signature and converted with a
  documented local read-only route (PowerPoint COM, or LibreOffice headless).
  A rename is never treated as conversion.
* Reference and new-content previews can be composed into a real side-by-side
  artifact with measured differences.

Everything here consumes local files only.  No network service, paid API,
credential, or global configuration is used.
"""

from __future__ import annotations

import copy
import io
import math
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
EMU_PER_INCH = 914400
ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
OOXML_SUFFIXES = {".pptx", ".pptm"}
OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
ZIP_SIGNATURE = b"PK\x03\x04"

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

SIMPLE_DECORATION_SHAPES = {
    "RECTANGLE",
    "ROUNDED_RECTANGLE",
    "SNIP_SAME_SIDE_CORNER_RECTANGLE",
    "SNIP_ONE_CORNER_RECTANGLE",
    "SNIP_ROUND_RECTANGLE",
    "OVAL",
    "LINE",
    "RIGHT_ARROW",
    "LEFT_ARROW",
    "UP_ARROW",
    "DOWN_ARROW",
    "CHEVRON",
    "PENTAGON",
    "TRIANGLE",
    "ISOSCELES_TRIANGLE",
    "RIGHT_TRIANGLE",
    "PARALLELOGRAM",
    "TRAPEZOID",
    "DIAMOND",
    "CAN",
    "PLAQUE",
    "FLOWCHART_PROCESS",
}

DECORATION_MIN_SHARE = 0.5
DECORATION_MAX_AREA = 0.20


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _read_signature(path):
    with Path(path).open("rb") as stream:
        return stream.read(8)


def _normalise_hex(value):
    if not value:
        return None
    text = str(value).strip().lstrip("#")
    if len(text) == 8:
        text = text[:6]
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", text):
        return None
    return "#" + text.upper()


def _rgb(value):
    text = _normalise_hex(value)
    if not text:
        return (0, 0, 0)
    return tuple(int(text[index:index + 2], 16) for index in (1, 3, 5))


def _pixel_counts(image):
    getter = getattr(image, "get_flattened_data", None)
    if callable(getter):
        return Counter(getter())
    return Counter(image.getdata())


def _hex(rgb):
    return "#{:02X}{:02X}{:02X}".format(*[max(0, min(255, int(round(item)))) for item in rgb])


def _blend(first, second, ratio):
    ratio = max(0.0, min(1.0, float(ratio)))
    a, b = _rgb(first), _rgb(second)
    return _hex(tuple(a[index] * (1 - ratio) + b[index] * ratio for index in range(3)))


def _luminance(value):
    red, green, blue = [channel / 255 for channel in _rgb(value)]
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_text(value):
    return "#FFFFFF" if _luminance(value) < 0.55 else "#141A24"


def _contrast_ratio(first, second):
    def relative(value):
        channels = [channel / 255 for channel in _rgb(value)]
        adjusted = [channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4
                    for channel in channels]
        return 0.2126 * adjusted[0] + 0.7152 * adjusted[1] + 0.0722 * adjusted[2]

    high, low = sorted((relative(first), relative(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _saturation(value):
    red, green, blue = [channel / 255 for channel in _rgb(value)]
    high, low = max(red, green, blue), min(red, green, blue)
    return 0 if high == 0 else (high - low) / high


def _colour_distance(first, second):
    a, b = _rgb(first), _rgb(second)
    return math.sqrt(sum((a[index] - b[index]) ** 2 for index in range(3)))


def _round(value, digits=3):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _slug(value):
    text = unicodedata.normalize("NFC", str(value or "")).strip() or "reference"
    text = re.sub(r"[^\w\u0600-\u06FF ._()&'’\-]", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip().lstrip("._- ")
    return (text[:60] or "reference").strip()


def _relative_or_absolute(path, base):
    path = Path(path).resolve()
    try:
        return str(path.relative_to(Path(base).resolve()).as_posix())
    except ValueError:
        return str(path)


def _source_identity(path):
    from .jobs import file_digest

    path = Path(path)
    return {
        "name": path.name,
        "path": str(path.resolve()),
        "sha256": file_digest(path),
        "bytes": path.stat().st_size,
    }


def _apply_brightness(value, node):
    """Apply the common DrawingML luminance/tint/shade transforms."""

    from pptx.oxml.ns import qn

    red, green, blue = _rgb(value)
    channels = [red, green, blue]
    if node is None:
        return _hex(channels)
    lum_mod = node.find(qn("a:lumMod"))
    lum_off = node.find(qn("a:lumOff"))
    tint = node.find(qn("a:tint"))
    shade = node.find(qn("a:shade"))
    try:
        if lum_mod is not None:
            factor = int(lum_mod.get("val", "100000")) / 100000
            channels = [channel * factor for channel in channels]
        if lum_off is not None:
            offset = int(lum_off.get("val", "0")) / 100000
            channels = [channel + 255 * offset for channel in channels]
        if tint is not None:
            factor = int(tint.get("val", "100000")) / 100000
            channels = [channel + (255 - channel) * factor for channel in channels]
        if shade is not None:
            factor = int(shade.get("val", "100000")) / 100000
            channels = [channel * factor for channel in channels]
    except (TypeError, ValueError):
        pass
    return _hex(channels)


def _scheme_colour(theme, name):
    if not name:
        return None
    colours = theme.get("colours", {}) if theme else {}
    colour_map = theme.get("colour_map", {}) if theme else {}
    key = str(name).lower()
    if key in colour_map:
        key = colour_map[key]
    value = colours.get(key) or colours.get(key.lower())
    if value:
        return _normalise_hex(value)
    return None


def _colour_from_node(node, theme):
    """Resolve a DrawingML colour element (or a container such as solidFill)."""

    if node is None:
        return None
    from lxml import etree
    from pptx.oxml.ns import qn

    tag = etree.QName(node).localname
    colour_node = None
    if tag in {"srgbClr", "sysClr", "schemeClr", "prstClr"}:
        colour_node = node
    else:
        for child_tag in ("srgbClr", "sysClr", "schemeClr", "prstClr"):
            candidate = node.find(qn("a:" + child_tag))
            if candidate is not None:
                colour_node = candidate
                break
    if colour_node is None:
        return None
    local = etree.QName(colour_node).localname
    if local == "srgbClr":
        value = _normalise_hex(colour_node.get("val"))
    elif local == "sysClr":
        value = _normalise_hex(colour_node.get("lastClr") or colour_node.get("val"))
    elif local == "schemeClr":
        value = _scheme_colour(theme, colour_node.get("val"))
        value = _apply_brightness(value, colour_node) if value else None
    else:
        presets = {
            "black": "#000000", "white": "#FFFFFF", "red": "#FF0000",
            "green": "#00FF00", "blue": "#0000FF", "yellow": "#FFFF00",
            "gray": "#808080", "grey": "#808080",
        }
        value = presets.get(str(colour_node.get("val", "")).lower())
    return value


def _fill_colour(container, theme):
    from pptx.oxml.ns import qn

    if container is None:
        return None
    solid = container.find(qn("a:solidFill"))
    if solid is not None:
        return _colour_from_node(solid, theme)
    for tag in ("a:gradFill", "a:pattFill"):
        fill = container.find(qn(tag))
        if fill is not None:
            gradient = fill.find(qn("a:gsLst"))
            if gradient is not None:
                stop = gradient.find(qn("a:gs"))
                if stop is not None:
                    return _colour_from_node(stop, theme)
            return None
    return None


def _line_colour(container, theme):
    from pptx.oxml.ns import qn

    if container is None:
        return None
    line = container.find(qn("a:ln"))
    if line is None:
        return None
    no_fill = line.find(qn("a:noFill"))
    if no_fill is not None:
        return None
    solid = line.find(qn("a:solidFill"))
    return _colour_from_node(solid, theme) if solid is not None else None


# ---------------------------------------------------------------------------
# Format detection and legacy conversion
# ---------------------------------------------------------------------------


def detect_format(path):
    path = Path(path)
    suffix = path.suffix.lower()
    signature = _read_signature(path)
    if suffix == ".ppt":
        if signature.startswith(ZIP_SIGNATURE):
            raise ValueError(
                "This file is named .ppt but is actually an OOXML presentation. "
                "Rename or export it as .pptx and retry; renaming alone is not treated as conversion."
            )
        if not signature.startswith(OLE_SIGNATURE):
            raise ValueError("The file is not a readable legacy PowerPoint (.ppt) document")
        return "ppt"
    if suffix in OOXML_SUFFIXES:
        if signature.startswith(OLE_SIGNATURE):
            raise ValueError(
                "This file is a legacy binary .ppt presentation with a .pptx extension. "
                "Convert it to real OOXML in PowerPoint and retry."
            )
        if not signature.startswith(ZIP_SIGNATURE):
            raise ValueError("The file is not a readable OOXML PowerPoint presentation")
        return "pptx"
    if suffix == ".pdf":
        if not _read_signature(path)[:5] == b"%PDF-":
            raise ValueError("The file does not start with a PDF signature")
        return "pdf"
    if suffix in RASTER_SUFFIXES:
        return "raster"
    raise ValueError(
        "Design references must be PDF, PPTX, legacy PPT, or a supported raster image; "
        f"got {suffix or 'no extension'}"
    )


def _find_soffice():
    configured = os.environ.get("LECTURE_MATERIALS_SOFFICE")
    candidates = []
    if configured:
        candidates.append(Path(configured))
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
        if base:
            candidates.append(Path(base) / "LibreOffice" / "program" / "soffice.exe")
    candidates.extend([
        Path("/usr/bin/soffice"),
        Path("/usr/local/bin/soffice"),
        Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
    ])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def convert_legacy_ppt(path, destination, original_sha256=None):
    """Convert a binary .ppt to .pptx through a read-only local route.

    The original is opened read-only and is never saved.  PowerPoint automation
    is attempted first (Windows), then LibreOffice headless.  If neither is
    available the caller receives an actionable conversion requirement.
    """

    from .jobs import file_digest

    path = Path(path)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    before = original_sha256 or file_digest(path)
    errors = []

    if sys.platform == "win32":
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            app = None
            deck = None
            try:
                app = win32com.client.DispatchEx("PowerPoint.Application")
                try:
                    app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
                except Exception as error:
                    raise RuntimeError(
                        "PowerPoint macro security could not be force-disabled; refusing to open the .ppt: "
                        f"{error}"
                    ) from error
                try:
                    deck = app.Presentations.Open(
                        str(path.resolve()), ReadOnly=True, Untitled=False, WithWindow=False
                    )
                except Exception:
                    deck = app.Presentations.Open(str(path.resolve()), ReadOnly=True)
                deck.SaveAs(str(destination.resolve()), 24)  # ppSaveAsOpenXMLPresentation
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
            if destination.is_file() and destination.stat().st_size:
                if file_digest(path) != before:
                    destination.unlink(missing_ok=True)
                    raise ValueError("The original .ppt changed during conversion; no conversion was kept")
                return {
                    "available": True,
                    "method": "Microsoft PowerPoint read-only SaveAs (ppSaveAsOpenXMLPresentation)",
                    "converted_path": str(destination.resolve()),
                    "original_preserved": True,
                    "original_sha256": before,
                }
            errors.append("PowerPoint did not produce a converted file")
        except Exception as error:
            errors.append(f"PowerPoint conversion failed: {error}")

    soffice = _find_soffice()
    if soffice is not None:
        try:
            result = subprocess.run(
                [str(soffice), "--headless", "--convert-to", "pptx", "--outdir",
                 str(destination.parent), str(path.resolve())],
                capture_output=True, text=True, timeout=180,
            )
            produced = destination.parent / (path.stem + ".pptx")
            if produced.is_file() and produced.stat().st_size:
                if produced.resolve() != destination.resolve():
                    shutil.move(str(produced), str(destination))
                if file_digest(path) != before:
                    destination.unlink(missing_ok=True)
                    raise ValueError("The original .ppt changed during conversion; no conversion was kept")
                return {
                    "available": True,
                    "method": "LibreOffice headless --convert-to pptx (read-only)",
                    "converted_path": str(destination.resolve()),
                    "original_preserved": True,
                    "original_sha256": before,
                }
            errors.append((result.stderr or result.stdout or "LibreOffice produced no output").strip()[:400])
        except Exception as error:
            errors.append(f"LibreOffice conversion failed: {error}")

    destination.unlink(missing_ok=True)
    raise ValueError(
        "Legacy binary .ppt cannot be inspected by Python. Convert a copy to .pptx with "
        "PowerPoint (File > Save As > PowerPoint Presentation) or LibreOffice "
        "(--convert-to pptx), then retry. The original was left unchanged. "
        "No renaming fallback is used. Local conversion details: " + " | ".join(errors[-3:])
    )


# ---------------------------------------------------------------------------
# PPTX theme, layouts, and native template handling
# ---------------------------------------------------------------------------


def _theme_root(prs):
    from lxml import etree

    for part in prs.part.package.iter_parts():
        if str(part.partname).endswith("theme/theme1.xml"):
            return etree.fromstring(part.blob)
    return None


def _read_theme(prs, master=None):
    from lxml import etree
    from pptx.oxml.ns import qn

    root = _theme_root(prs)
    theme = {"colours": {}, "fonts": {}, "colour_map": {}, "name": None}
    if root is None:
        return theme
    name = root.get("name")
    theme["name"] = name
    scheme = root.find(".//" + qn("a:clrScheme"))
    if scheme is not None:
        for child in scheme:
            tag = etree.QName(child).localname
            value = None
            srgb = child.find(qn("a:srgbClr"))
            if srgb is not None:
                value = _normalise_hex(srgb.get("val"))
            else:
                sys_colour = child.find(qn("a:sysClr"))
                if sys_colour is not None:
                    value = _normalise_hex(sys_colour.get("lastClr") or sys_colour.get("val"))
            if value:
                theme["colours"][tag] = value
    for label, path in (("major", ".//" + qn("a:majorFont")), ("minor", ".//" + qn("a:minorFont"))):
        node = root.find(path)
        if node is None:
            continue
        for script in ("latin", "ea", "cs"):
            child = node.find(qn("a:" + script))
            if child is not None and child.get("typeface"):
                theme["fonts"][f"{label}_{script}"] = child.get("typeface")
    if master is not None:
        clr_map = master._element.find(".//" + qn("p:clrMap"))
        if clr_map is not None:
            for key, value in clr_map.attrib.items():
                theme["colour_map"][key] = value
    return theme


def _placeholder_geometry(placeholder, master):
    left, top = placeholder.left, placeholder.top
    width, height = placeholder.width, placeholder.height
    if left is None or top is None or width is None or height is None:
        counterpart = None
        try:
            idx = placeholder.placeholder_format.idx
            ptype = placeholder.placeholder_format.type
            for candidate in master.placeholders:
                if candidate.placeholder_format.idx == idx or candidate.placeholder_format.type == ptype:
                    counterpart = candidate
                    break
        except Exception:
            counterpart = None
        if counterpart is not None:
            left = left if left is not None else counterpart.left
            top = top if top is not None else counterpart.top
            width = width if width is not None else counterpart.width
            height = height if height is not None else counterpart.height
    if None in (left, top, width, height):
        return None
    return {
        "left_in": round(float(left.inches), 3),
        "top_in": round(float(top.inches), 3),
        "width_in": round(float(width.inches), 3),
        "height_in": round(float(height.inches), 3),
    }


def _font_size_points(element, fallback=None):
    """Read an explicit size from a placeholder/shape or its level-one style."""

    from pptx.oxml.ns import qn

    if element is None:
        return fallback
    try:
        text_frame = element.text_frame
        for paragraph in text_frame.paragraphs:
            if paragraph.font.size:
                return round(float(paragraph.font.size.pt), 1)
            for run in paragraph.runs:
                if run.font.size:
                    return round(float(run.font.size.pt), 1)
    except Exception:
        pass
    try:
        xml = element._element if hasattr(element, "_element") else element
    except Exception:
        return fallback
    for path in (".//" + qn("a:lvl1pPr") + "/" + qn("a:defRPr"),
                 ".//" + qn("a:lstStyle") + "/" + qn("a:lvl1pPr") + "/" + qn("a:defRPr")):
        node = xml.find(path)
        if node is not None and node.get("sz"):
            try:
                return round(int(node.get("sz")) / 100, 1)
            except ValueError:
                pass
    return fallback


def _master_text_style_size(master, kind):
    from pptx.oxml.ns import qn

    root = master._element
    for path in (".//" + qn("p:txStyles") + "/" + qn("p:titleStyle") + "/" + qn("a:lvl1pPr") + "/" + qn("a:defRPr"),
                 ".//" + qn("p:txStyles") + "/" + qn("p:bodyStyle") + "/" + qn("a:lvl1pPr") + "/" + qn("a:defRPr")):
        node = root.find(path)
        if node is not None and node.get("sz"):
            try:
                value = round(int(node.get("sz")) / 100, 1)
                if (kind == "title" and "titleStyle" in path) or (kind == "body" and "bodyStyle" in path):
                    return value
            except ValueError:
                continue
    return None


def _placeholder_type_name(placeholder):
    try:
        value = placeholder.placeholder_format.type
        return getattr(value, "name", None) or str(value)
    except Exception:
        return "UNKNOWN"


def _placeholder_alignment(placeholder):
    try:
        alignment = placeholder.text_frame.paragraphs[0].alignment
        if alignment is not None:
            return getattr(alignment, "name", str(alignment))
    except Exception:
        pass
    try:
        from pptx.oxml.ns import qn

        paragraph = placeholder._element.find(".//" + qn("a:p"))
        if paragraph is not None:
            properties = paragraph.find(qn("a:pPr"))
            if properties is not None and properties.get("algn"):
                return {"ctr": "CENTER", "r": "RIGHT", "l": "LEFT", "just": "JUSTIFY"}.get(
                    properties.get("algn")
                )
    except Exception:
        pass
    return None


def _read_layout(layout, master, theme):
    placeholders = []
    for placeholder in layout.placeholders:
        geometry = _placeholder_geometry(placeholder, master)
        size = _font_size_points(placeholder)
        if size is None:
            size = _master_text_style_size(
                master,
                "title" if _placeholder_type_name(placeholder) in {"TITLE", "CENTER_TITLE"} else "body",
            )
        family = None
        try:
            for paragraph in placeholder.text_frame.paragraphs:
                for run in paragraph.runs:
                    if run.font.name:
                        family = run.font.name
                        break
                if family:
                    break
        except Exception:
            pass
        alignment = _placeholder_alignment(placeholder)
        if alignment is None:
            try:
                for counterpart in master.placeholders:
                    if (counterpart.placeholder_format.idx == placeholder.placeholder_format.idx
                            or counterpart.placeholder_format.type == placeholder.placeholder_format.type):
                        alignment = _placeholder_alignment(counterpart)
                        if alignment:
                            break
            except Exception:
                pass
        placeholders.append({
            "idx": placeholder.placeholder_format.idx,
            "type": _placeholder_type_name(placeholder),
            "name": placeholder.name,
            "geometry": geometry,
            "font_size_pt": size,
            "font_family": family,
            "alignment": alignment,
        })
    extras = []
    for shape in layout.shapes:
        if shape.is_placeholder:
            continue
        extras.append({
            "name": shape.name,
            "kind": _shape_kind(shape),
            "has_text": bool(getattr(shape, "has_text_frame", False) and shape.text.strip()),
            "pictures": sum(1 for _ in shape._element.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}blip")),
        })
    return {
        "index": layout.slide_id if hasattr(layout, "slide_id") else None,
        "name": layout.name,
        "placeholders": placeholders,
        "extra_shapes": extras,
        "shape_count": len(layout.shapes),
    }


def _pick_layout_roles(layouts):
    names = [item["name"].casefold() for item in layouts]

    def find(*needles, exclude=()):
        for needle in needles:
            for index, name in enumerate(names):
                if needle in name and not any(bad in name for bad in exclude):
                    return index
        return None

    cover = find("title slide", "cover", "opening", "title")
    section = find("section header", "section")
    content = find("title and content", "content", "title only", exclude=("caption", "picture", "comparison", "vertical"))
    toc = find("table of contents", "contents", "agenda", "toc")
    image = find("picture with caption", "image", "photo")
    comparison = find("comparison", "two content")

    def placeholder_types(index):
        return {item.get("type") for item in layouts[index].get("placeholders", [])}

    if cover is None:
        for index, layout in enumerate(layouts):
            types = placeholder_types(index)
            if "CENTER_TITLE" in types or ("TITLE" in types and "SUBTITLE" in types):
                cover = index
                break
    if content is None:
        scored = []
        for index, layout in enumerate(layouts):
            name = layout["name"].casefold()
            if any(bad in name for bad in ("caption", "picture", "comparison", "vertical", "blank")):
                continue
            types = placeholder_types(index)
            body_count = sum(1 for item in types if item in {"BODY", "OBJECT", "CONTENT"})
            if "TITLE" in types and body_count:
                area = sum(
                    float(item.get("height_in") or 0) * float(item.get("width_in") or 0)
                    for item in layout.get("placeholders", [])
                    if item.get("type") in {"BODY", "OBJECT", "CONTENT"}
                )
                scored.append((body_count, area, -index, index))
        if scored:
            content = max(scored)[-1]
    if content is None:
        content = cover if cover is not None else 0
    if cover is None:
        cover = content
    if section is None:
        section = cover
    return {
        "cover_layout": cover,
        "content_layout": content,
        "section_layout": section,
        "toc_layout": toc,
        "image_layout": image,
        "comparison_layout": comparison,
    }


def _shape_kind(shape):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    if shape.is_placeholder:
        return "placeholder"
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        return "picture"
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        return "group"
    if getattr(shape, "has_table", False) and shape.has_table:
        return "table"
    if getattr(shape, "has_chart", False) and shape.has_chart:
        return "chart"
    if shape.shape_type == MSO_SHAPE_TYPE.MEDIA:
        return "media"
    if getattr(shape, "has_text_frame", False) and shape.text.strip():
        return "text"
    if shape.shape_type == MSO_SHAPE_TYPE.LINE:
        return "vector"
    if shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
        return "vector"
    if shape.shape_type == MSO_SHAPE_TYPE.FREEFORM:
        return "vector"
    if shape.shape_type == MSO_SHAPE_TYPE.TEXT_BOX:
        return "vector"
    return "other"


def _shape_box(shape):
    try:
        return {
            "left_in": round(float(shape.left.inches), 3),
            "top_in": round(float(shape.top.inches), 3),
            "width_in": round(float(shape.width.inches), 3),
            "height_in": round(float(shape.height.inches), 3),
        }
    except Exception:
        return None


def _shape_has_blip(shape):
    from pptx.oxml.ns import qn

    try:
        return any(True for _ in shape._element.iter(qn("a:blip")))
    except Exception:
        return False


def _shape_xml(shape):
    """Return sanitized native DrawingML for a text-free, picture-free shape."""

    from lxml import etree
    from pptx.oxml.ns import qn

    element = copy.deepcopy(shape._element)
    for node in element.iter():
        local = etree.QName(node).localname
        for attribute in ("id", "embed", "link", "pict"):
            key = f"{{{R_NS}}}{attribute}"
            node.attrib.pop(key, None)
        if local in {"blip", "hlinkClick", "hlinkMouseOver", "pic"}:
            return None
        if local == "t":
            node.text = ""
    return etree.tostring(element, encoding="unicode")


def _shape_signature(shape):
    """Return XML normalized for cross-slide identity (ids/names ignored)."""

    from lxml import etree
    from pptx.oxml.ns import qn

    xml = _shape_xml(shape)
    if not xml:
        return ""
    try:
        root = etree.fromstring(xml.encode("utf-8"))
        for node in root.iter(qn("p:cNvPr")):
            node.set("id", "0")
            node.set("name", "shape")
        return etree.tostring(root, encoding="unicode")
    except Exception:
        return xml


def _slide_background_source(slide, layout, master):
    from pptx.oxml.ns import qn

    for element in (slide._element, layout._element, master._element):
        c_sld = element.find(qn("p:cSld"))
        if c_sld is None:
            continue
        bg = c_sld.find(qn("p:bg"))
        if bg is not None:
            return element, bg
    return None, None


def _background_info(element, bg, theme):
    from pptx.oxml.ns import qn

    if bg is None:
        return {"source": "theme", "kind": "none", "colour": None, "apply": False}
    bg_pr = bg.find(qn("p:bgPr"))
    if bg_pr is not None:
        solid = bg_pr.find(qn("a:solidFill"))
        if solid is not None:
            return {
                "source": "explicit",
                "kind": "solid",
                "colour": _colour_from_node(solid, theme),
                "apply": True,
            }
        gradient = bg_pr.find(qn("a:gradFill"))
        if gradient is not None:
            stops = gradient.find(qn("a:gsLst"))
            first = _colour_from_node(stops.find(qn("a:gs")), theme) if stops is not None and stops.find(qn("a:gs")) is not None else None
            return {"source": "explicit", "kind": "gradient", "colour": first, "apply": True,
                    "approximation": "gradient reduced to its first colour"}
        blip = bg_pr.find(qn("a:blipFill"))
        if blip is not None:
            return {"source": "explicit", "kind": "picture", "colour": None, "apply": True,
                    "approximation": "picture background excluded; solid background used"}
    ref = bg.find(qn("p:bgRef"))
    if ref is not None:
        colour = _colour_from_node(ref, theme)
        return {"source": "theme", "kind": "solid", "colour": colour, "apply": True}
    return {"source": "theme", "kind": "none", "colour": None, "apply": False}


def _run_colour(run, theme):
    try:
        if run.font.color and run.font.color.type is not None:
            rgb = run.font.color.rgb
            if rgb is not None:
                return _normalise_hex(str(rgb))
    except Exception:
        pass
    try:
        from pptx.oxml.ns import qn

        rpr = run._r.rPr
        if rpr is not None:
            solid = rpr.find(qn("a:solidFill"))
            if solid is not None:
                value = _colour_from_node(solid, theme)
                if value:
                    return value
            scheme = rpr.find(qn("a:solidFill") + "/" + qn("a:schemeClr"))
            if scheme is not None:
                return _colour_from_node(scheme, theme)
    except Exception:
        pass
    return None


def _table_style_from_shape(shape, theme):
    from pptx.oxml.ns import qn

    table = shape.table
    tbl = table._tbl
    tbl_pr = tbl.tblPr
    style_id = None
    if tbl_pr is not None:
        node = tbl_pr.find(qn("a:tableStyleId"))
        if node is not None:
            style_id = node.text
    header_fill = header_text = None
    body_fill = zebra_fill = None
    border = None
    header_size = None
    header_family = None
    if len(table.rows) and len(table.columns):
        cell = table.cell(0, 0)
        header_fill = _fill_colour(cell._tc.find(qn("a:tcPr")), theme)
        for paragraph in cell.text_frame.paragraphs:
            for run in paragraph.runs:
                if header_text is None:
                    header_text = _run_colour(run, theme)
                if header_size is None and run.font.size:
                    header_size = round(float(run.font.size.pt), 1)
                if header_family is None and run.font.name:
                    header_family = run.font.name
        line = cell._tc.find(qn("a:tcPr") + "/" + qn("a:lnB"))
        if line is not None:
            colour_node = line.find(qn("a:solidFill"))
            if colour_node is not None:
                border = _colour_from_node(colour_node, theme)
    if len(table.rows) > 1 and len(table.columns):
        body_fill = _fill_colour(table.cell(1, 0)._tc.find(qn("a:tcPr")), theme)
    if len(table.rows) > 2 and len(table.columns):
        zebra_fill = _fill_colour(table.cell(2, 0)._tc.find(qn("a:tcPr")), theme)
    return {
        "style_id": style_id,
        "first_row": bool(tbl_pr is not None and tbl_pr.get("firstRow") in {"1", "true"}),
        "band_row": bool(tbl_pr is not None and tbl_pr.get("bandRow") in {"1", "true"}),
        "header_fill": header_fill,
        "header_text": header_text,
        "header_size_pt": header_size,
        "header_font": header_family,
        "body_fill": body_fill,
        "zebra_fill": zebra_fill,
        "border": border,
        "source": "reference table",
    }


def _recurring_shapes(slides, theme, slide_width, slide_height):
    """Classify top-level slide shapes and select conservative vector decoration."""

    from collections import defaultdict

    signatures = defaultdict(list)
    candidates = []
    ambiguous = []
    excluded_pictures = []
    recurring_text = []
    text_candidates = defaultdict(list)
    slide_count = max(1, len(slides))
    minimum = max(2, int(math.ceil(slide_count * DECORATION_MIN_SHARE)))
    for slide_index, slide in enumerate(slides, start=1):
        for shape in slide.shapes:
            kind = _shape_kind(shape)
            box = _shape_box(shape)
            if kind == "picture":
                excluded_pictures.append({
                    "slide": slide_index,
                    "name": shape.name,
                    "box": box,
                    "reason": "picture content is never reused as decoration and requires separate permission",
                })
                continue
            if kind == "table":
                continue
            if kind == "chart":
                ambiguous.append({
                    "slide": slide_index,
                    "name": shape.name,
                    "kind": "chart",
                    "box": box,
                    "reason": "embedded chart data is original lecture content; not copied",
                })
                continue
            if kind == "text":
                text = shape.text.strip()
                if 0 < len(text) <= 120:
                    signature = (
                        text,
                        round(box["top_in"], 2) if box else None,
                        round(box["left_in"], 2) if box else None,
                        round(box["width_in"], 2) if box else None,
                    )
                    text_candidates[signature].append((slide_index, box))
                continue
            if kind != "vector":
                if kind in {"group", "media", "other"}:
                    ambiguous.append({
                        "slide": slide_index,
                        "name": shape.name,
                        "kind": kind,
                        "box": box,
                        "reason": "group/complex object is not claimed as decoration; review the reference render",
                    })
                continue
            if _shape_has_blip(shape):
                excluded_pictures.append({
                    "slide": slide_index,
                    "name": shape.name,
                    "box": box,
                    "reason": "vector shape uses a picture fill; excluded from design reuse",
                })
                continue
            text = ""
            try:
                text = shape.text.strip()
            except Exception:
                pass
            if text or not box:
                continue
            area = box["width_in"] * box["height_in"]
            area_ratio = area / max(0.01, slide_width * slide_height)
            shape_type = ""
            try:
                shape_type = getattr(shape.auto_shape_type, "name", str(shape.auto_shape_type))
            except Exception:
                shape_type = "LINE" if shape.shape_type == 9 else "UNKNOWN"
            simple = shape_type in SIMPLE_DECORATION_SHAPES
            near_top = box["top_in"] <= slide_height * 0.18
            near_bottom = box["top_in"] + box["height_in"] >= slide_height * 0.82
            touches_edge = (
                box["left_in"] <= 0.06 * slide_width
                or box["top_in"] <= 0.06 * slide_height
                or box["left_in"] + box["width_in"] >= 0.94 * slide_width
                or box["top_in"] + box["height_in"] >= 0.94 * slide_height
            )
            candidate = {
                "slide": slide_index,
                "name": shape.name,
                "box": box,
                "shape_type": shape_type,
                "simple": simple,
                "area_ratio": round(area_ratio, 3),
                "band": "header" if near_top else ("footer" if near_bottom else ("edge" if touches_edge else "body")),
            }
            if not simple:
                ambiguous.append({
                    "slide": slide_index,
                    "name": shape.name,
                    "kind": "freeform/complex vector",
                    "box": box,
                    "reason": "not copied automatically; review whether it is a design element or a diagram",
                })
                continue
            if area_ratio > DECORATION_MAX_AREA:
                ambiguous.append({
                    "slide": slide_index,
                    "name": shape.name,
                    "kind": "large vector",
                    "box": box,
                    "reason": "large shape may be content; not copied automatically",
                })
                continue
            if candidate["band"] == "body":
                ambiguous.append({
                    "slide": slide_index,
                    "name": shape.name,
                    "kind": "body vector",
                    "box": box,
                    "reason": "not in a header/footer/edge band; not claimed as decoration",
                })
                continue
            signature = (
                box["left_in"], box["top_in"], box["width_in"], box["height_in"],
                shape_type,
                _shape_signature(shape) or "",
            )
            signatures[signature].append(candidate)
    for signature, occurrences in signatures.items():
        if len({item["slide"] for item in occurrences}) < minimum:
            representative = occurrences[0]
            ambiguous.append({
                "slide": representative["slide"],
                "name": representative["name"],
                "kind": "low-occurrence vector",
                "box": representative["box"],
                "reason": "not repeated across enough slides to classify as design decoration; not copied",
            })
            continue
        representative = occurrences[0]
        xml = signature[-1]
        if not xml:
            continue
        candidates.append({
            "role": representative["band"],
            "name": representative["name"],
            "box": representative["box"],
            "shape_type": representative["shape_type"],
            "occurrences": len({item["slide"] for item in occurrences}),
            "area_ratio": representative["area_ratio"],
            "xml": xml,
        })
    for signature, occurrences in text_candidates.items():
        if len({item["slide"] for item, _ in occurrences}) < minimum:
            continue
        text, top, left, width = signature
        box = occurrences[0][1]
        band = "header" if box and box["top_in"] <= slide_height * 0.18 else (
            "footer" if box and box["top_in"] + box["height_in"] >= slide_height * 0.82 else "body"
        )
        if band == "body":
            continue
        recurring_text.append({
            "text_sha256": _sha256_text(text),
            "characters": len(text),
            "band": band,
            "box": box,
            "occurrences": len({item["slide"] for item, _ in occurrences}),
            "reuse": "not copied automatically; host reviews the reference render and decides whether the label belongs to the new document",
        })
    return {
        "native_shapes": candidates,
        "ambiguous": ambiguous,
        "excluded_pictures": excluded_pictures,
        "recurring_text": recurring_text,
    }


def _sha256_text(text):
    import hashlib

    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _slide_text_stats(slides, theme):
    title_sizes = Counter()
    body_sizes = Counter()
    table_sizes = Counter()
    other_sizes = Counter()
    title_fonts = Counter()
    body_fonts = Counter()
    title_colours = Counter()
    body_colours = Counter()
    table_colours = Counter()
    old_text_characters = 0
    arabic_characters = 0
    latin_characters = 0
    picture_count = 0
    table_count = 0
    text_shape_count = 0
    all_fonts = Counter()
    size_counter = Counter()
    colour_counter = Counter()
    for slide in slides:
        for shape in slide.shapes:
            if getattr(shape, "shape_type", None) is not None and str(shape.shape_type).startswith("PICTURE"):
                picture_count += 1
            if getattr(shape, "has_table", False) and shape.has_table:
                table_count += 1
                for row in shape.table.rows:
                    for cell in row.cells:
                        for paragraph in cell.text_frame.paragraphs:
                            for run in paragraph.runs:
                                if not run.text.strip():
                                    continue
                                old_text_characters += len(run.text)
                                arabic_characters += len(ARABIC_RE.findall(run.text))
                                latin_characters += len(re.findall(r"[A-Za-z]", run.text))
                                if run.font.size:
                                    table_sizes[round(float(run.font.size.pt), 1)] += len(run.text)
                                    size_counter[round(float(run.font.size.pt), 1)] += len(run.text)
                                if run.font.name:
                                    all_fonts[run.font.name] += len(run.text)
                                colour = _run_colour(run, theme)
                                if colour:
                                    table_colours[colour] += len(run.text)
                                    colour_counter[colour] += len(run.text)
                continue
            if not getattr(shape, "has_text_frame", False) or not shape.has_text_frame:
                continue
            if not shape.is_placeholder and shape.text.strip():
                text_shape_count += 1
            is_title = shape.is_placeholder and _placeholder_type_name(shape) in {"TITLE", "CENTER_TITLE"}
            target_sizes = title_sizes if is_title else body_sizes
            target_fonts = title_fonts if is_title else body_fonts
            target_colours = title_colours if is_title else body_colours
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    if not run.text.strip():
                        continue
                    old_text_characters += len(run.text)
                    arabic_characters += len(ARABIC_RE.findall(run.text))
                    latin_characters += len(re.findall(r"[A-Za-z]", run.text))
                    if run.font.size:
                        target_sizes[round(float(run.font.size.pt), 1)] += len(run.text)
                        size_counter[round(float(run.font.size.pt), 1)] += len(run.text)
                    if run.font.name:
                        target_fonts[run.font.name] += len(run.text)
                        all_fonts[run.font.name] += len(run.text)
                    colour = _run_colour(run, theme)
                    if colour:
                        target_colours[colour] += len(run.text)
                        colour_counter[colour] += len(run.text)
    return {
        "title_sizes": title_sizes,
        "body_sizes": body_sizes,
        "table_sizes": table_sizes,
        "other_sizes": other_sizes,
        "title_fonts": title_fonts,
        "body_fonts": body_fonts,
        "title_colours": title_colours,
        "body_colours": body_colours,
        "table_colours": table_colours,
        "all_fonts": all_fonts,
        "size_sizes": size_counter,
        "colour_colours": colour_counter,
        "old_text_characters": old_text_characters,
        "arabic_characters": arabic_characters,
        "latin_characters": latin_characters,
        "pictures": picture_count,
        "tables": table_count,
        "text_shapes": text_shape_count,
    }


def _mode(counter, fallback=None):
    if not counter:
        return fallback
    return max(counter.items(), key=lambda item: (item[1], item[0]))[0]


def _drop_unused_image_relationships(part, element):
    """Drop unused image/chart/package/OLE/diagram/media relationships."""

    from pptx.oxml.ns import qn

    used = set()
    for node in element.iter():
        for attribute in (qn("r:embed"), qn("r:link"), qn("r:id")):
            value = node.get(attribute)
            if value:
                used.add(value)
    try:
        relationships = list(part.rels.items())
    except Exception:
        return 0
    content_suffixes = (
        "/image", "/chart", "/package", "/oleObject", "/diagramData",
        "/diagramLayout", "/diagramQuickStyle", "/diagramColors",
        "/media", "/video", "/audio",
    )
    dropped = 0
    for relationship_id, relationship in relationships:
        if relationship_id in used:
            continue
        if not str(relationship.reltype).endswith(content_suffixes):
            continue
        try:
            part.drop_rel(relationship_id)
            dropped += 1
        except Exception:
            pass
    return dropped


def _element_name(element):
    from lxml import etree
    from pptx.oxml.ns import qn

    node = element.find(".//" + qn("p:cNvPr"))
    if node is None and etree.QName(element).localname == "cNvPr":
        node = element
    return node.get("name") if node is not None else "shape"


def _element_text(element):
    from pptx.oxml.ns import qn

    return "".join(node.text or "" for node in element.iter(qn("a:t")))


def _is_placeholder_element(element):
    from pptx.oxml.ns import qn

    return element.find(".//" + qn("p:ph")) is not None


def _clear_placeholder_text(element):
    """Keep placeholder geometry/formatting but remove prompt/default text."""

    from lxml import etree
    from pptx.oxml.ns import qn

    text_body = element.find(".//" + qn("p:txBody"))
    if text_body is None:
        return False
    changed = False
    paragraphs = list(text_body.findall(qn("a:p")))
    for paragraph in paragraphs:
        for child in list(paragraph):
            local = etree.QName(child).localname
            if local in {"r", "fld", "br"}:
                paragraph.remove(child)
                changed = True
    if not paragraphs:
        etree.SubElement(text_body, qn("a:p"))
        changed = True
    return changed


def _strip_picture_fills_from_element(element):
    from pptx.oxml.ns import qn

    count = 0
    for shape_properties in list(element.iter(qn("p:spPr"))) + list(element.iter(qn("p:grpSpPr"))):
        if shape_properties.find(qn("a:blipFill")) is not None:
            _replace_blip_fill(shape_properties, qn)
            count += 1
    return count


def _strip_pictures_from_tree(element):
    """Remove picture objects and picture fills from a slide tree only."""

    from pptx.oxml.ns import qn

    removed = []
    sp_tree = element.find(".//" + qn("p:spTree"))
    if sp_tree is None:
        return {"removed_pictures": removed, "picture_fills_replaced": 0}
    for picture in list(sp_tree.iter(qn("p:pic"))):
        parent = picture.getparent()
        if parent is not None:
            removed.append(_element_name(picture))
            parent.remove(picture)
    picture_fills = _strip_picture_fills_from_element(sp_tree)
    return {"removed_pictures": removed, "picture_fills_replaced": picture_fills}


def _sanitize_design_tree(element):
    """Sanitize one master/layout/notes-master design tree.

    Preserves text-free vector decoration and placeholder geometry/formatting.
    Removes arbitrary non-placeholder prose, pictures, tables, charts,
    SmartArt/graphic frames, OLE/content objects and picture fills.  Placeholder
    prompt/default text is cleared while the placeholder itself remains.
    """

    from lxml import etree
    from pptx.oxml.ns import qn

    report = {
        "removed_pictures": [],
        "picture_fills_replaced": 0,
        "removed_text_shapes": [],
        "removed_content_objects": [],
        "cleared_placeholder_texts": [],
    }
    sp_tree = element.find(".//" + qn("p:spTree"))
    if sp_tree is None:
        return report

    def walk(parent):
        for child in list(parent):
            local = etree.QName(child).localname
            if local in {"graphicFrame", "pic", "contentPart", "oleObj"}:
                if local == "pic":
                    report["removed_pictures"].append(_element_name(child))
                else:
                    report["removed_content_objects"].append(_element_name(child))
                parent.remove(child)
                continue
            if local == "grpSp":
                walk(child)
                if not any(etree.QName(node).localname in {
                    "sp", "pic", "cxnSp", "grpSp", "graphicFrame"
                } for node in child):
                    parent.remove(child)
                continue
            if local == "sp":
                if _is_placeholder_element(child):
                    if _clear_placeholder_text(child):
                        report["cleared_placeholder_texts"].append(_element_name(child))
                elif _element_text(child).strip():
                    report["removed_text_shapes"].append(_element_name(child))
                    parent.remove(child)
                    continue
                report["picture_fills_replaced"] += _strip_picture_fills_from_element(child)
                continue
            # Connections and other text-free vector objects remain native.

    walk(sp_tree)
    report["picture_fills_replaced"] += _strip_picture_fills_from_element(sp_tree)
    return report


def _replace_blip_fill(parent, qn):
    from pptx.oxml import parse_xml
    from pptx.oxml.ns import nsdecls

    blip = parent.find(qn("a:blipFill"))
    if blip is None:
        return
    parent.remove(blip)
    solid = parse_xml(f'<a:solidFill {nsdecls("a")}><a:srgbClr val="FFFFFF"/></a:solidFill>')
    line = parent.find(qn("a:ln"))
    if line is not None:
        line.addprevious(solid)
    else:
        effect = parent.find(qn("a:effectLst"))
        if effect is not None:
            effect.addprevious(solid)
        else:
            parent.append(solid)


def _strip_background_pictures(element, fallback="#FFFFFF"):
    from pptx.oxml import parse_xml
    from pptx.oxml.ns import nsdecls, qn

    c_sld = element.find(qn("p:cSld"))
    if c_sld is None:
        return 0
    count = 0
    for bg_pr in c_sld.iter(qn("p:bgPr")):
        blip = bg_pr.find(qn("a:blipFill"))
        if blip is not None:
            bg_pr.remove(blip)
            solid = parse_xml(
                f'<a:solidFill {nsdecls("a")}><a:srgbClr val="{fallback.lstrip("#")}"/></a:solidFill>'
            )
            bg_pr.insert(0, solid)
            count += 1
    return count


def _clear_extended_properties(body):
    """Remove identifying extended properties while keeping a valid typed app.xml."""

    from lxml import etree

    try:
        root = etree.fromstring(body)
    except Exception:
        return body
    identifying = {
        "template", "manager", "company", "hyperlinkbase", "category",
        "contentstatus", "identifier", "language", "keywords", "subject",
        "title", "author", "lastmodifiedby", "creator", "description",
    }
    # HeadingPairs/TitlesOfParts carry the old presentation's title vectors as
    # vt:lpstr values; the remaining extended properties are typed numeric or
    # boolean fields that PowerPoint expects to stay valid.
    title_vectors = {"headingpairs", "titlesofparts"}
    changed = False
    for child in list(root):
        if etree.QName(child).localname.lower() in identifying | title_vectors:
            root.remove(child)
            changed = True
    for node in list(root.iter()):
        if etree.QName(node).localname in {"lpstr", "lpwstr"}:
            node.text = ""
            changed = True
    if not changed:
        return body
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _clear_core_properties(body):
    """Blank identifying core-property values without invalidating core.xml."""

    from lxml import etree

    try:
        root = etree.fromstring(body)
    except Exception:
        return body
    identifying = {
        "title", "subject", "creator", "keywords", "description",
        "lastmodifiedby", "category", "contentstatus", "identifier",
        "language",
    }
    for node in root.iter():
        if etree.QName(node).localname.lower() in identifying:
            node.text = ""
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _strip_thumbnail_relationships(body):
    """Drop package relationships that would keep an old thumbnail reachable."""

    from lxml import etree

    try:
        root = etree.fromstring(body)
    except Exception:
        return body
    changed = False
    for relationship in list(root):
        if etree.QName(relationship).localname != "Relationship":
            continue
        reltype = (relationship.get("Type") or "").lower()
        target = (relationship.get("Target") or "").replace("\\", "/").lower()
        if reltype.endswith("/metadata/thumbnail") or "docprops/thumbnail" in target:
            root.remove(relationship)
            changed = True
    if not changed:
        return body
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _strip_thumbnail_content_types(body):
    """Remove content-type overrides for a dropped thumbnail part."""

    from lxml import etree

    try:
        root = etree.fromstring(body)
    except Exception:
        return body
    changed = False
    for node in list(root):
        if etree.QName(node).localname != "Override":
            continue
        part_name = (node.get("PartName") or "").replace("\\", "/").lower()
        if "docprops/thumbnail" in part_name:
            root.remove(node)
            changed = True
    if not changed:
        return body
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _sanitize_package_metadata(path):
    """Remove old source thumbnails and identifying package metadata."""

    import zipfile

    path = Path(path)
    temporary = path.with_name(path.name + ".metadata.tmp")
    with zipfile.ZipFile(path, "r") as package:
        entries = [(info, package.read(info.filename)) for info in package.infolist()]
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as package:
            for info, body in entries:
                name = info.filename.replace("\\", "/").lower()
                if name.startswith("docprops/thumbnail"):
                    continue
                if name.endswith(".rels"):
                    body = _strip_thumbnail_relationships(body)
                elif name == "[content_types].xml":
                    body = _strip_thumbnail_content_types(body)
                elif name == "docprops/app.xml":
                    body = _clear_extended_properties(body)
                elif name == "docprops/core.xml":
                    body = _clear_core_properties(body)
                package.writestr(info, body)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def sanitize_pptx(source, destination, strip_images=True):
    """Freeze masters/layouts/theme/vector design without retaining old slides."""

    from pptx import Presentation
    from .jobs import file_digest

    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    presentation = Presentation(str(source))
    removed_slides = len(presentation.slides)
    xml_slides = presentation.slides._sldIdLst
    for slide_id in list(xml_slides):
        presentation.part.drop_rel(slide_id.rId)
        xml_slides.remove(slide_id)
    # Remove custom-show/section references to the deleted slide IDs so the
    # package does not advertise non-existent slides.
    try:
        from lxml import etree
        from pptx.oxml.ns import qn

        root = presentation.part._element
        for tag in ("p:custShowLst",):
            node = root.find(qn(tag))
            if node is not None:
                root.remove(node)
        for node in list(root.iter()):
            if etree.QName(node).localname in {"sectionLst", "custShowLst"}:
                parent = node.getparent()
                if parent is not None:
                    parent.remove(node)
    except Exception:
        pass
    picture_report = {
        "removed_pictures": [],
        "picture_fills_replaced": 0,
        "removed_text_shapes": [],
        "removed_content_objects": [],
        "cleared_placeholder_texts": [],
    }
    background_images = 0
    if strip_images:
        for master in presentation.slide_masters:
            for element, part in [(master._element, master.part), *[
                (layout._element, layout.part) for layout in master.slide_layouts
            ]]:
                report = _sanitize_design_tree(element)
                for key in ("removed_pictures", "removed_text_shapes",
                            "removed_content_objects", "cleared_placeholder_texts"):
                    picture_report[key].extend(report[key])
                picture_report["picture_fills_replaced"] += report["picture_fills_replaced"]
                background_images += _strip_background_pictures(element)
                _drop_unused_image_relationships(part, element)
        for master_name in ("notes_master", "handout_master"):
            master = getattr(presentation, master_name, None)
            if master is None:
                continue
            element = getattr(master, "_element", None)
            part = getattr(master, "part", None)
            if element is None or part is None:
                continue
            report = _sanitize_design_tree(element)
            for key in ("removed_pictures", "removed_text_shapes",
                        "removed_content_objects", "cleared_placeholder_texts"):
                picture_report[key].extend(report[key])
            picture_report["picture_fills_replaced"] += report["picture_fills_replaced"]
            background_images += _strip_background_pictures(element)
            _drop_unused_image_relationships(part, element)
    # Any notes masters are not attached to newly added slides and contain no
    # lecture prose; drop them when possible so no old notes survive.
    # Remove identifying lecture metadata from the frozen template/output.
    try:
        core = presentation.core_properties
        for attribute in ("title", "subject", "author", "keywords", "comments", "category",
                          "content_status", "identifier", "language", "last_modified_by"):
            try:
                setattr(core, attribute, "")
            except Exception:
                pass
    except Exception:
        pass
    try:
        custom = presentation.custom_properties
        for name in list(custom):
            try:
                del custom[name]
            except Exception:
                pass
    except Exception:
        pass
    temporary = destination.with_name(destination.name + ".tmp")
    presentation.save(str(temporary))
    _sanitize_package_metadata(temporary)
    temporary.replace(destination)
    return {
        "path": str(destination.resolve()),
        "sha256": file_digest(destination),
        "removed_slides": removed_slides,
        "removed_pictures": picture_report["removed_pictures"],
        "removed_text_shapes": picture_report["removed_text_shapes"],
        "removed_content_objects": picture_report["removed_content_objects"],
        "cleared_placeholder_texts": picture_report["cleared_placeholder_texts"],
        "picture_fills_replaced": picture_report["picture_fills_replaced"],
        "background_images_replaced": background_images,
        "bytes": destination.stat().st_size,
    }


def strip_pptx_pictures(presentation):
    """Sanitize template design trees and pictures in an in-memory deck."""

    report = {
        "removed_pictures": [],
        "picture_fills_replaced": 0,
        "background_images_replaced": 0,
        "removed_text_shapes": [],
        "removed_content_objects": [],
        "cleared_placeholder_texts": [],
    }
    for master in presentation.slide_masters:
        elements = [(master._element, master.part)] + [
            (layout._element, layout.part) for layout in master.slide_layouts
        ]
        for element, part in elements:
            result = _sanitize_design_tree(element)
            for key in ("removed_pictures", "removed_text_shapes",
                        "removed_content_objects", "cleared_placeholder_texts"):
                report[key].extend(result[key])
            report["picture_fills_replaced"] += result["picture_fills_replaced"]
            report["background_images_replaced"] += _strip_background_pictures(element)
            _drop_unused_image_relationships(part, element)
    for slide in presentation.slides:
        result = _strip_pictures_from_tree(slide._element)
        report["removed_pictures"].extend(result["removed_pictures"])
        report["picture_fills_replaced"] += result["picture_fills_replaced"]
        report["background_images_replaced"] += _strip_background_pictures(slide._element)
        _drop_unused_image_relationships(slide.part, slide._element)
    for master_name in ("notes_master", "handout_master"):
        master = getattr(presentation, master_name, None)
        if master is None:
            continue
        element = getattr(master, "_element", None)
        part = getattr(master, "part", None)
        if element is None or part is None:
            continue
        result = _sanitize_design_tree(element)
        for key in ("removed_pictures", "removed_text_shapes",
                    "removed_content_objects", "cleared_placeholder_texts"):
            report[key].extend(result[key])
        report["picture_fills_replaced"] += result["picture_fills_replaced"]
        report["background_images_replaced"] += _strip_background_pictures(element)
        _drop_unused_image_relationships(part, element)
    return report


def add_recurring_shapes(slide, specs):
    """Insert validated native decoration XML behind new slide content."""

    from lxml import etree
    from pptx.oxml import parse_xml
    from pptx.oxml.ns import qn

    sp_tree = slide.shapes._spTree
    existing = [
        int(element.get("id"))
        for element in sp_tree.iter(qn("p:cNvPr"))
        if element.get("id") and str(element.get("id")).isdigit()
    ]
    next_id = max(existing or [1]) + 1
    added = []
    for spec in specs or []:
        xml = spec.get("xml")
        if not xml:
            continue
        try:
            element = parse_xml(xml)
        except Exception:
            continue
        for node in element.iter():
            local = etree.QName(node).localname
            if local in {"blip", "pic", "hlinkClick", "hlinkMouseOver"}:
                element = None
                break
            if local == "t":
                node.text = ""
        if element is None:
            continue
        for c_nv_pr in element.iter(qn("p:cNvPr")):
            c_nv_pr.set("id", str(next_id))
            c_nv_pr.set("name", "Reference design: " + str(spec.get("role", "decoration")))
            next_id += 1
        try:
            sp_tree.insert(2, element)
            added.append(spec.get("role"))
        except Exception:
            continue
    return added


# ---------------------------------------------------------------------------
# PPTX extraction
# ---------------------------------------------------------------------------


def render_pptx_slides(path, destination, limit=8):
    """Render actual slides via a separate read-only PowerPoint instance.

    If PowerPoint automation is unavailable, a local LibreOffice headless
    conversion to PDF plus PyMuPDF page rendering is used.  Both routes are
    read-only with respect to the reference.
    """

    try:
        from .render_pptx import export_rendered_slides

        return export_rendered_slides(path, destination, limit=limit)
    except Exception as error:
        fallback = pptx_soffice_previews(path, destination, limit=limit)
        if fallback.get("available"):
            return fallback
        return {"available": False, "error": f"Reference slide rendering failed: {error}", "paths": []}


def pptx_soffice_previews(path, destination, limit=8):
    """Render PPTX/PPT previews with LibreOffice headless + PyMuPDF."""

    path = Path(path)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    # A failure or unavailable renderer must not leave an earlier run's images
    # looking like the current preview.
    for stale in list(destination.glob("*.PNG")) + list(destination.glob("*.png")):
        stale.unlink(missing_ok=True)
    soffice = _find_soffice()
    if soffice is None:
        return {
            "available": False,
            "error": "No local PowerPoint or LibreOffice renderer was available for slide previews",
            "paths": [],
        }
    pdf_path = destination / (path.stem + "-preview.pdf")
    pdf_path.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            [str(soffice), "--headless", "--convert-to", "pdf", "--outdir",
             str(destination), str(path.resolve())],
            capture_output=True, text=True, timeout=180,
        )
        produced = destination / (path.stem + ".pdf")
        if produced.is_file():
            produced.replace(pdf_path)
        if not pdf_path.is_file():
            return {
                "available": False,
                "error": (result.stderr or result.stdout or "LibreOffice produced no PDF").strip()[:500],
                "paths": [],
            }
        rendered = render_pdf_pages(pdf_path, destination / "pages", limit=limit or 100)
        pdf_path.unlink(missing_ok=True)
        return {
            "available": True,
            "renderer": "LibreOffice headless + PyMuPDF",
            "slides": len(rendered.get("paths", [])),
            "paths": rendered.get("paths", []),
        }
    except Exception as error:
        return {"available": False, "error": f"LibreOffice preview failed: {error}", "paths": []}


def _collect_pptx_design(path, name, workspace, conversion=None):
    from pptx import Presentation
    from .jobs import file_digest

    path = Path(path)
    workspace = Path(workspace)
    presentation = Presentation(str(path))
    master = presentation.slide_masters[0]
    theme = _read_theme(presentation, master)
    layouts = [_read_layout(layout, master, theme) for layout in master.slide_layouts]
    roles = _pick_layout_roles(layouts)
    slides = list(presentation.slides)
    slide_count = len(slides)
    slide_width = round(float(presentation.slide_width.inches), 3)
    slide_height = round(float(presentation.slide_height.inches), 3)
    stats = _slide_text_stats(slides, theme)
    recurring = _recurring_shapes(slides, theme, slide_width, slide_height)

    # Background: prefer a consistent explicit slide background, then layout,
    # then master.  A picture background is replaced with the theme colour.
    background_candidates = []
    for slide in slides:
        layout = slide.slide_layout
        element, bg = _slide_background_source(slide, layout, master)
        info = _background_info(element, bg, theme)
        if info.get("kind") in {"solid", "gradient", "picture"}:
            background_candidates.append(info)
    if background_candidates:
        background = Counter(
            (item.get("kind"), item.get("colour")) for item in background_candidates
        ).most_common(1)[0][0]
        background = next(
            item for item in background_candidates
            if (item.get("kind"), item.get("colour")) == background
        )
    else:
        element, bg = _slide_background_source(slides[0], slides[0].slide_layout, master) if slides else (master._element, None)
        background = _background_info(element, bg, theme)
    theme_background = _scheme_colour(theme, "bg1") or "#FFFFFF"
    if background.get("kind") in {"picture", "gradient"}:
        background["colour"] = background.get("colour") or theme_background
        background["apply"] = True
        background["approximation"] = background.get("approximation") or "background reduced to a solid colour"
    elif background.get("kind") == "solid" and background.get("colour"):
        background["apply"] = True
    else:
        background = {"source": "theme", "kind": "solid", "colour": theme_background,
                      "apply": False}
    if background.get("colour") is None:
        background["colour"] = theme_background

    # Table treatment from the first real table in the reference.
    table_style = None
    for slide in slides:
        for shape in slide.shapes:
            if getattr(shape, "has_table", False) and shape.has_table:
                table_style = _table_style_from_shape(shape, theme)
                break
        if table_style:
            break

    # Typography: observed run sizes, then placeholder defaults, then master defaults.
    body_size = _mode(stats["body_sizes"])
    title_size = _mode(stats["title_sizes"])
    table_size = _mode(stats["table_sizes"])
    body_family = _mode(stats["body_fonts"])
    title_family = _mode(stats["title_fonts"])
    content_layout = layouts[roles["content_layout"]]
    cover_layout = layouts[roles["cover_layout"]]
    content_title_ph = next(
        (item for item in content_layout["placeholders"] if item["type"] in {"TITLE", "CENTER_TITLE"}),
        None,
    )
    content_body_ph = None
    body_candidates = [
        item for item in content_layout["placeholders"]
        if item["type"] in {"BODY", "OBJECT", "CONTENT"} and item.get("geometry")
    ]
    if body_candidates:
        content_body_ph = max(
            body_candidates,
            key=lambda item: float(item["geometry"]["width_in"]) * float(item["geometry"]["height_in"]),
        )
    cover_title_ph = next(
        (item for item in cover_layout["placeholders"] if item["type"] in {"TITLE", "CENTER_TITLE"}),
        None,
    )
    cover_subtitle_ph = next(
        (item for item in cover_layout["placeholders"] if item["type"] in {"SUBTITLE", "BODY", "OBJECT"}),
        None,
    )
    if body_size is None:
        body_size = _font_size_points(
            next((p for p in master.placeholders if _placeholder_type_name(p) == "BODY"), None)
        ) or _master_text_style_size(master, "body") or 18
    if title_size is None:
        title_size = (content_title_ph or {}).get("font_size_pt") or _master_text_style_size(master, "title") or 32
    if table_size is None:
        table_size = max(9, min(body_size, 14))
    body_family = body_family or _mode(stats["all_fonts"]) or theme["fonts"].get("minor_latin") or "Segoe UI"
    title_family = title_family or (content_title_ph or {}).get("font_family") or theme["fonts"].get("major_latin") or body_family
    arabic_family = theme["fonts"].get("minor_cs") or body_family
    title_colour = _mode(stats["title_colours"]) or _scheme_colour(theme, "tx1") or "#20262B"
    body_colour = _mode(stats["body_colours"]) or _scheme_colour(theme, "tx1") or "#20262B"
    accent = (
        (table_style or {}).get("header_fill")
        or _mode(stats["title_colours"])
        or _scheme_colour(theme, "accent1")
        or "#2B4ACB"
    )
    if _colour_distance(accent, background.get("colour") or "#FFFFFF") < 18:
        accent = _scheme_colour(theme, "accent1") or "#2B4ACB"
    table_header = (table_style or {}).get("header_fill") or accent
    table_header_text = (table_style or {}).get("header_text") or _contrast_text(table_header)
    table_border = (table_style or {}).get("border") or _blend(body_colour, background.get("colour") or "#FFFFFF", 0.72)
    table_zebra = (table_style or {}).get("zebra_fill") or _blend(background.get("colour") or "#FFFFFF", accent, 0.08)
    table_body = (table_style or {}).get("body_fill") or _blend(background.get("colour") or "#FFFFFF", "#FFFFFF", 0.35)
    if table_zebra == table_body:
        table_zebra = _blend(table_body, accent, 0.08)

    margin_in = 0.55
    if content_body_ph and content_body_ph.get("geometry"):
        geometry = content_body_ph["geometry"]
        margin_in = round(min(geometry["left_in"], slide_width - geometry["left_in"] - geometry["width_in"]), 3)
        margin_in = max(0.2, margin_in)
    title_bar_in = 0.95
    if content_title_ph and content_title_ph.get("geometry"):
        geometry = content_title_ph["geometry"]
        title_bar_in = round(max(0.35, geometry["top_in"] + geometry["height_in"]), 3)
    body_geometry = (content_body_ph or {}).get("geometry") or {
        "left_in": margin_in,
        "top_in": title_bar_in,
        "width_in": slide_width - 2 * margin_in,
        "height_in": slide_height - title_bar_in - margin_in,
    }

    footer_specs = [item for item in recurring["native_shapes"] if item.get("role") == "footer"]
    header_specs = [item for item in recurring["native_shapes"] if item.get("role") == "header"]
    footer_geometry = None
    for placeholder in layouts[roles["content_layout"]]["placeholders"]:
        if placeholder["type"] in {"FOOTER", "SLIDE_NUMBER", "DATE"} and placeholder.get("geometry"):
            candidate = placeholder["geometry"]
            if float(candidate.get("top_in", 0)) >= slide_height * 0.72:
                candidate = dict(candidate)
                candidate["height_in"] = max(
                    0.18, min(float(candidate.get("height_in", 0.26)),
                              slide_height - float(candidate.get("top_in", 0)) - 0.02)
                )
                footer_geometry = candidate
                break
    if footer_geometry is None:
        footer_geometry = {
            "left_in": margin_in,
            "top_in": round(max(body_geometry["top_in"] + body_geometry["height_in"], slide_height - margin_in * 0.6), 3),
            "width_in": round(slide_width - 2 * margin_in, 3),
            "height_in": 0.26,
        }

    typography = {
        "title_pt": round(float(title_size), 1),
        "section_pt": round(float(max(title_size, body_size * 1.2)), 1),
        "body_pt": round(float(body_size), 1),
        "bullet_pt": round(float(max(body_size * 0.95, 9)), 1),
        "caption_pt": round(float(max(body_size * 0.62, 8)), 1),
        "table_pt": round(float(max(table_size, 8)), 1),
        "equation_pt": round(float(max(body_size * 1.15, 14)), 1),
    }

    # Sanitized native template (no old slides, no picture objects).
    template_path = workspace / "template.pptx"
    freeze = sanitize_pptx(path, template_path, strip_images=True)
    source_identity = _source_identity(path)

    support = {
        "supported": [],
        "preserved": [],
        "approximated": [],
        "unsupported": [],
        "ambiguous": [],
        "excluded": [],
    }
    support["preserved"].extend([
        {"property": "slide dimensions", "detail": f"{slide_width} x {slide_height} in", "renderer": "pptx-native"},
        {"property": "slide masters and layouts", "detail": f"{len(layouts)} layouts retained in a sanitized native template", "renderer": "pptx-native"},
        {"property": "theme colours and fonts", "detail": "Theme XML is retained; observed run fonts/sizes are also recorded as tokens", "renderer": "pptx-native"},
        {"property": "vector decoration on masters/layouts", "detail": "Retained natively through the sanitized template", "renderer": "pptx-native"},
    ])
    support["supported"].extend([
        {"property": "cover/title layout", "detail": cover_layout["name"], "renderer": "pptx-native"},
        {"property": "content layout", "detail": content_layout["name"], "renderer": "pptx-native"},
        {"property": "placeholder geometry", "detail": "Cover, title and body regions use the layout placeholder boxes", "renderer": "pptx-native"},
        {"property": "recurring vector decoration", "detail": f"{len(recurring['native_shapes'])} shape(s) copied as native DrawingML", "renderer": "pptx-native"},
    ])
    if table_style:
        support["supported"].append({
            "property": "table treatment", "detail": "Header fill/text, banding and border tokens read from the reference table",
            "renderer": "pptx-native",
        })
    if background.get("approximation"):
        support["approximated"].append({
            "property": "slide background", "detail": background["approximation"], "renderer": "pptx-native",
        })
    for item in recurring["ambiguous"]:
        support["ambiguous"].append({
            "property": item["kind"],
            "detail": f"slide {item['slide']} shape {item['name']!r}: {item['reason']}",
            "renderer": "host-review",
        })
    for item in recurring["recurring_text"]:
        support["ambiguous"].append({
            "property": "recurring text",
            "detail": f"{item['band']} text ({item['characters']} chars, sha256 {item['text_sha256'][:12]}); not copied",
            "renderer": "host-review",
        })
    if recurring["excluded_pictures"]:
        support["unsupported"].append({
            "property": "template pictures/logos",
            "detail": f"{len(recurring['excluded_pictures'])} picture object(s) are excluded from design reuse; "
                      "text-only output must contain no pictures and other image modes need explicit permission",
            "renderer": "policy",
        })
        support["excluded"].append({
            "property": "original pictures",
            "detail": "Picture objects are never copied from a template, even when named decoration.",
        })
    if freeze.get("removed_pictures"):
        support["unsupported"].append({
            "property": "master/layout picture elements",
            "detail": f"{len(freeze['removed_pictures'])} picture object(s) removed while freezing the native template: "
                      + ", ".join(str(name) for name in freeze["removed_pictures"][:6]),
            "renderer": "policy",
        })
        support["excluded"].append({
            "property": "template master/layout pictures",
            "detail": "A template is not blanket permission to insert its pictures; text-only output contains none.",
        })
    if freeze.get("removed_text_shapes"):
        support["unsupported"].append({
            "property": "master/layout custom text",
            "detail": f"{len(freeze['removed_text_shapes'])} text-bearing master/layout shape(s) removed: "
                      + ", ".join(str(name) for name in freeze["removed_text_shapes"][:6]),
            "renderer": "policy",
        })
        support["excluded"].append({
            "property": "master/layout prose",
            "detail": "Arbitrary master/layout text is not treated as design decoration.",
        })
    if freeze.get("removed_content_objects"):
        support["unsupported"].append({
            "property": "master/layout content objects",
            "detail": f"{len(freeze['removed_content_objects'])} table/chart/SmartArt/OLE/content object(s) removed: "
                      + ", ".join(str(name) for name in freeze["removed_content_objects"][:6]),
            "renderer": "policy",
        })
        support["excluded"].append({
            "property": "master/layout content objects",
            "detail": "Content-bearing master/layout objects are not carried into generated slides.",
        })
    if freeze.get("cleared_placeholder_texts"):
        support["approximated"].append({
            "property": "placeholder prompt/default text",
            "detail": f"{len(freeze['cleared_placeholder_texts'])} placeholder(s) retained with their geometry/formatting; "
                      "default prompt text was cleared",
            "renderer": "pptx-native",
        })
    if freeze.get("background_images_replaced"):
        support["approximated"].append({
            "property": "template background picture",
            "detail": f"{freeze['background_images_replaced']} picture background(s) replaced with a solid colour; "
                      "template pictures are not inserted without per-image permission",
            "renderer": "pptx-native",
        })
    support["excluded"].append({
        "property": "original slide prose/tables/diagrams",
        "detail": f"{freeze['removed_slides']} original slide(s) removed; {stats['old_text_characters']} characters of old text were not copied",
    })
    support["unsupported"].extend([
        {"property": "animations/transitions", "detail": "Not represented in the reusable design", "renderer": "pptx-native"},
        {"property": "SmartArt/charts/embedded media", "detail": "Original content objects are not treated as decoration", "renderer": "pptx-native"},
        {"property": "exact table style package", "detail": "Cell fills and fonts are reproduced; a referenced table style part is approximated", "renderer": "pptx-native"},
    ])
    support["approximated"].append({
        "property": "placeholder prompt text",
        "detail": "Empty placeholders are removed from generated slides; the layouts remain for native editing",
        "renderer": "pptx-native",
    })

    design = {
        "schema_version": SCHEMA_VERSION,
        "kind": "reference_design",
        "source_format": "pptx",
        "method": "native-template",
        "reference": {
            "name": path.name,
            "path": str(path.resolve()),
            "sha256": source_identity["sha256"],
            "bytes": source_identity["bytes"],
            "slides": slide_count,
            "conversion": conversion,
        },
        "identity": {
            "mode": "frozen",
            "source_path": str(path.resolve()),
            "source_sha256": source_identity["sha256"],
        },
        "template": {
            "path": str(template_path.resolve()),
            "sha256": freeze["sha256"],
            "frozen": True,
            "sanitized": True,
            "source_path": str(path.resolve()),
            "source_sha256": source_identity["sha256"],
            "removed_slides": freeze["removed_slides"],
            "removed_pictures": freeze["removed_pictures"],
            "removed_text_shapes": freeze.get("removed_text_shapes", []),
            "removed_content_objects": freeze.get("removed_content_objects", []),
            "cleared_placeholder_texts": freeze.get("cleared_placeholder_texts", []),
            "picture_fills_replaced": freeze.get("picture_fills_replaced", 0),
            "background_images_replaced": freeze.get("background_images_replaced", 0),
        },
        "slide_width_in": slide_width,
        "slide_height_in": slide_height,
        "theme": theme,
        "layouts": layouts,
        "roles": roles,
        "background": background,
        "typography": typography,
        "headings": {
            "title": {"size_pt": typography["title_pt"], "colour": title_colour, "font": title_family, "bold": True},
            "h1": {"size_pt": typography["title_pt"], "colour": title_colour, "font": title_family, "bold": True},
        },
        "table": table_style or {
            "style_id": None, "first_row": True, "band_row": True,
            "header_fill": table_header, "header_text": table_header_text,
            "header_size_pt": typography["table_pt"], "header_font": body_family,
            "body_fill": table_body, "zebra_fill": table_zebra,
            "border": table_border, "source": "theme/token estimate",
        },
        "geometry": {
            "margin_in": margin_in,
            "title_bar_in": title_bar_in,
            "body": body_geometry,
            "cover": {
                "title": (cover_title_ph or {}).get("geometry"),
                "subtitle": (cover_subtitle_ph or {}).get("geometry"),
            },
            "header": {
                "height_in": 0.4,
                "shapes": header_specs,
            },
            "footer": footer_geometry,
        },
        "recurring": recurring,
        "palette": {
            "background": background.get("colour") or "#FFFFFF",
            "surface": table_body,
            "text": body_colour,
            "muted": _blend(body_colour, background.get("colour") or "#FFFFFF", 0.45),
            "accent": accent,
            "accent_soft": _blend(background.get("colour") or "#FFFFFF", accent, 0.12),
            "rule": _blend(body_colour, background.get("colour") or "#FFFFFF", 0.72),
            "table_header": table_header,
            "table_header_text": table_header_text,
            "table_zebra": table_zebra,
            "example": "#8A5A16",
            "example_soft": "#FBF0D9",
            "warning": "#8A3B12",
        },
        "fonts": {
            "latin": {"family": body_family, "fallbacks": ["Segoe UI", "Arial", "sans-serif"]},
            "arabic": {"family": arabic_family, "fallbacks": ["Segoe UI", "Tahoma", "Arial", "sans-serif"]},
            "display": {"family": title_family, "fallbacks": [body_family, "Segoe UI", "sans-serif"]},
            "mono": {"family": "Consolas", "fallbacks": ["Courier New", "monospace"]},
        },
        "support": support,
    }

    direction_estimate = (
        "rtl" if stats["arabic_characters"] > stats["latin_characters"]
        else ("both" if stats["arabic_characters"] else "ltr")
    )
    analysis = {
        "method": "native-template",
        "slide_count": slide_count,
        "slide_size_in": {"width": slide_width, "height": slide_height},
        "theme": theme,
        "theme_colours": theme.get("colours", {}),
        "theme_fonts": theme.get("fonts", {}),
        "masters": [
            {"name": item.name, "layout_count": len(item.slide_layouts), "shapes": len(item.shapes)}
            for item in presentation.slide_masters
        ],
        "layouts": layouts,
        "layout_names": [layout["name"] for layout in layouts],
        "roles": roles,
        "shape_inventory": {
            "native_shapes_reused": len(recurring["native_shapes"]),
            "pictures_excluded": len(recurring["excluded_pictures"]),
            "text_shapes_not_copied": stats["text_shapes"],
            "tables_not_copied": stats["tables"],
            "ambiguities": len(recurring["ambiguous"]),
            "old_text_characters": stats["old_text_characters"],
            "removed_slides": freeze["removed_slides"],
        },
        "typography": typography,
        "table": table_style,
        "background": background,
        "sanitized_template": freeze,
        "run_fonts": [{"font": font, "characters": count} for font, count in stats["all_fonts"].most_common(8)],
        "run_sizes": [
            {"pt": size, "characters": count}
            for size, count in sorted(stats["size_sizes"].items(), reverse=True)[:10]
        ],
        "run_colours": [
            {"colour": colour, "characters": count}
            for colour, count in stats["colour_colours"].most_common(8)
        ],
        "pictures": stats["pictures"],
        "tables": stats["tables"],
        "direction_estimate": direction_estimate,
        "direction": direction_estimate,
    }
    return design, analysis, support


def extract_pptx_design(path, name, workspace):
    path = Path(path)
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    preview_dir = workspace / "reference-slides"
    previews = render_pptx_slides(path, preview_dir, limit=8)
    design, analysis, support = _collect_pptx_design(path, name, workspace)
    support["approximated"].append({
        "property": "font availability",
        "detail": "Observed font names were not checked against fonts installed on this host; render reports "
                  "declared families and substitutions.",
        "renderer": "pptx-native",
    })
    design["previews"] = {
        "reference_slides": previews.get("paths", []),
        "renderer": previews.get("renderer"),
        "rendering_note": None if previews.get("available") else previews.get("error"),
    }
    if not previews.get("available"):
        support["unsupported"].append({
            "property": "rendered reference slides",
            "detail": previews.get("error") or "No local PowerPoint renderer was available; the reference was only inspected structurally.",
            "renderer": "host-review",
        })
    return design, analysis, support, previews


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------


def render_pdf_pages(path, destination, limit=6):
    import pymupdf

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for stale in destination.glob("page-*.png"):
        stale.unlink(missing_ok=True)
    document = pymupdf.open(path)
    paths = []
    try:
        for index in range(min(limit, document.page_count)):
            page = document[index]
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.8, 1.8), alpha=False)
            target = destination / f"page-{index + 1:03d}.png"
            pixmap.save(target)
            paths.append(str(target))
    finally:
        document.close()
    return {"available": True, "paths": paths, "pages": len(paths)}


def _span_colour(span):
    return _hex(tuple(int(span.get("color", 0)) >> shift & 0xFF for shift in (16, 8, 0)))


def _span_is_bold(span):
    return bool(int(span.get("flags", 0)) & 16)


def _page_spans(page):
    records = []
    raw = page.get_text("dict")
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue
                bbox = span.get("bbox") or (0, 0, 0, 0)
                records.append({
                    "text": text,
                    "bbox": [float(value) for value in bbox],
                    "size": round(float(span.get("size", 0)), 2),
                    "font": span.get("font", "unknown"),
                    "colour": _span_colour(span),
                    "bold": _span_is_bold(span),
                    "page": page.number,
                })
    return records


def _weighted_mode(counter, minimum=0):
    items = [(value, weight) for value, weight in counter.items() if weight >= minimum]
    if not items:
        return None
    return max(items, key=lambda item: (item[1], item[0]))[0]


def _percentile(values, fraction):
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    index = max(0, min(len(values) - 1, int(round((len(values) - 1) * fraction))))
    return values[index]


def _rgb_distance(rgb, other):
    return math.sqrt(sum((rgb[index] - other[index]) ** 2 for index in range(3)))


def _page_palette(page, background="#FFFFFF"):
    import pymupdf
    from PIL import Image

    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5), alpha=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    image = image.resize((120, max(1, int(120 * pixmap.height / max(1, pixmap.width)))))
    quantized = image.quantize(colors=12, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    counts = _pixel_counts(quantized)
    total = sum(counts.values()) or 1
    colours = []
    for index, count in counts.most_common():
        base = index * 3
        rgb = tuple(palette[base:base + 3])
        if len(rgb) == 3:
            colours.append((rgb, count / total))
    background_rgb = _rgb(background)
    if colours:
        background_rgb = max(colours, key=lambda item: item[1])[0]
    non_background = [
        (rgb, share) for rgb, share in colours
        if _rgb_distance(rgb, background_rgb) > 35
    ]
    accent = max(non_background, key=lambda item: (_saturation(_hex(item[0])), item[1]))[0] \
        if non_background else background_rgb
    dark = min(colours, key=lambda item: sum(item[0]))[0] if colours else (0, 0, 0)
    return {
        "background": _hex(background_rgb),
        "text": _hex(dark),
        "accent": _hex(accent),
        "sampled": [{"colour": _hex(rgb), "share": round(share, 3)} for rgb, share in colours[:6]],
    }


def _ink_margins(page, background):
    import pymupdf
    from PIL import Image

    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5), alpha=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples).convert("RGB")
    width, height = image.size
    background_rgb = _rgb(background)
    rows = []
    columns = []
    top_limit = int(height * 0.06)
    bottom_limit = int(height * 0.94)
    left_limit = int(width * 0.02)
    right_limit = int(width * 0.98)
    for y in range(top_limit, bottom_limit):
        ink = 0
        for x in range(left_limit, right_limit, 2):
            if _rgb_distance(image.getpixel((x, y)), background_rgb) > 26:
                ink += 1
        fraction = ink / max(1, (right_limit - left_limit) // 2)
        if 0.02 <= fraction <= 0.78:
            rows.append(y)
    for x in range(left_limit, right_limit):
        ink = 0
        for y in range(top_limit, bottom_limit, 2):
            if _rgb_distance(image.getpixel((x, y)), background_rgb) > 26:
                ink += 1
        fraction = ink / max(1, (bottom_limit - top_limit) // 2)
        if 0.02 <= fraction <= 0.78:
            columns.append(x)
    if not rows or not columns:
        return None
    top = rows[0] / height * page.rect.height * 25.4 / 72
    bottom = (height - 1 - rows[-1]) / height * page.rect.height * 25.4 / 72
    left = columns[0] / width * page.rect.width * 25.4 / 72
    right = (width - 1 - columns[-1]) / width * page.rect.width * 25.4 / 72
    return {"top": round(top, 1), "right": round(right, 1), "bottom": round(bottom, 1), "left": round(left, 1)}


def _pdf_colour(value):
    """Convert a PyMuPDF colour (0..1 floats or 0..255 ints) to #RRGGBB."""

    if value is None:
        return None
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        channels = [float(item) for item in value[:3]]
        if max(channels) <= 1.0:
            channels = [item * 255 for item in channels]
        return _hex(channels)
    return None


def _drawing_info(drawing, page):
    rect = drawing.get("rect") or (0, 0, 0, 0)
    width = page.rect.width
    height = page.rect.height
    if not width or not height:
        return None
    x0, y0, x1, y1 = [float(value) for value in rect]
    normalized = (
        round(x0 / width, 3), round(y0 / height, 3),
        round(x1 / width, 3), round(y1 / height, 3),
    )
    items = drawing.get("items") or []
    has_curve = any(item and item[0] == "c" for item in items)
    fill = _pdf_colour(drawing.get("fill"))
    stroke = _pdf_colour(drawing.get("color"))
    signature = (
        normalized,
        fill,
        stroke,
        round(float(drawing.get("width") or 0), 1),
        len(items),
        has_curve,
    )
    return {
        "signature": signature,
        "rect_pt": [x0, y0, x1, y1],
        "rect_mm": {
            "left": round(x0 * 25.4 / 72, 2),
            "top": round(y0 * 25.4 / 72, 2),
            "right": round(x1 * 25.4 / 72, 2),
            "bottom": round(y1 * 25.4 / 72, 2),
        },
        "fill": fill,
        "stroke": stroke,
        "stroke_width_mm": round(float(drawing.get("width") or 0) * 25.4 / 72, 3),
        "items": len(items),
        "has_curve": has_curve,
        "normalized": normalized,
    }


def _collect_pdf_design(path, name, workspace):
    import pymupdf

    path = Path(path)
    workspace = Path(workspace)
    document = pymupdf.open(path)
    if document.page_count == 0:
        document.close()
        raise ValueError("Reference PDF has no pages")
    total_pages = document.page_count
    sampled = min(total_pages, 12)
    page_indices = list(range(sampled))
    pages_spans = []
    pages_drawings = []
    texts = []
    for index in page_indices:
        page = document[index]
        texts.append(page.get_text())
        pages_spans.append(_page_spans(page))
        pages_drawings.append([
            info for info in (_drawing_info(drawing, page) for drawing in page.get_drawings())
            if info is not None
        ])
    text = "\n".join(texts)
    characters = len(re.sub(r"\s+", "", text))
    page = document[0]
    width_pt, height_pt = float(page.rect.width), float(page.rect.height)
    width_mm, height_mm = width_pt * 25.4 / 72, height_pt * 25.4 / 72
    flattened = characters < 60 or characters / max(1, sampled) < 20

    size_counter = Counter()
    font_counter = Counter()
    colour_counter = Counter()
    body_size_counter = Counter()
    body_font_counter = Counter()
    body_colour_counter = Counter()
    large_size_counter = Counter()
    large_colour_counter = Counter()
    large_font_counter = Counter()
    arabic_font_counter = Counter()
    for page_spans in pages_spans:
        for span in page_spans:
            text_value = span["text"].strip()
            if not text_value:
                continue
            chars = len(text_value)
            size_counter[span["size"]] += chars
            font_counter[span["font"]] += chars
            colour_counter[span["colour"]] += chars
            relative_y = (span["bbox"][1] + span["bbox"][3]) / 2 / height_pt
            if 0.07 <= relative_y <= 0.93:
                body_size_counter[span["size"]] += chars
                body_font_counter[span["font"]] += chars
                body_colour_counter[span["colour"]] += chars
            if re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text_value):
                arabic_font_counter[span["font"]] += chars
    body_size = _weighted_mode(body_size_counter) or _weighted_mode(size_counter) or 10.5
    for page_spans in pages_spans:
        for span in page_spans:
            if span["size"] >= body_size * 1.35 and 0.04 <= (span["bbox"][1] / height_pt) <= 0.96:
                chars = len(span["text"].strip())
                large_size_counter[span["size"]] += chars
                large_font_counter[span["font"]] += chars
                large_colour_counter[span["colour"]] += chars
    title_size = max(_weighted_mode(large_size_counter) or body_size * 2.2,
                     _weighted_mode(large_size_counter) or body_size * 2.2)
    if large_size_counter:
        sizes = sorted(large_size_counter)
        title_size = max(sizes)
        heading_sizes = [size for size in sizes if size < title_size]
        h1_size = _weighted_mode(Counter({size: large_size_counter[size] for size in heading_sizes})) \
            if heading_sizes else body_size * 1.6
        h2_size = body_size * 1.25
        if heading_sizes:
            lower = [size for size in heading_sizes if size < h1_size]
            if lower:
                h2_size = max(lower)
    else:
        h1_size = body_size * 1.6
        h2_size = body_size * 1.25
    body_font = _weighted_mode(body_font_counter) or _weighted_mode(font_counter) or "Georgia"
    heading_font = _weighted_mode(large_font_counter) or body_font
    arabic_font = _weighted_mode(arabic_font_counter) or "Segoe UI"
    body_colour = _weighted_mode(body_colour_counter) or _weighted_mode(colour_counter) or "#20262B"
    heading_colour = _weighted_mode(large_colour_counter) or body_colour
    accent = heading_colour
    if _colour_distance(accent, "#FFFFFF") < 20 or _colour_distance(accent, body_colour) < 12:
        candidates = [
            colour for colour, weight in colour_counter.most_common(12)
            if weight >= 2 and _saturation(colour) >= 0.18 and _colour_distance(colour, "#FFFFFF") > 40
        ]
        accent = candidates[0] if candidates else "#2B4ACB"

    palette = _page_palette(page, background="#FFFFFF")
    if flattened:
        palette = _page_palette(page, background="#FFFFFF")
    background = palette["background"]
    if _colour_distance(background, "#FFFFFF") < 10:
        background = "#FFFFFF"
    text_colour = body_colour if _colour_distance(body_colour, background) > 30 else palette["text"]
    if _colour_distance(text_colour, background) < 30:
        text_colour = "#20262B"
    # Reject near-background accents.
    if _colour_distance(accent, background) < 25:
        accent = palette["accent"]
    if _colour_distance(accent, background) < 25:
        accent = "#2B4ACB"

    # Margins from actual line boxes, ignoring header/footer bands.
    margins = {"top": [], "right": [], "bottom": [], "left": []}
    for page_index, page_spans in enumerate(pages_spans):
        boxes = [
            span["bbox"] for span in page_spans
            if span["size"] <= max(body_size * 1.45, h1_size * 1.1)
            and span["bbox"][1] >= height_pt * 0.075
            and span["bbox"][3] <= height_pt * 0.925
        ]
        for info in pages_drawings[page_index]:
            x0, y0, x1, y1 = info["normalized"]
            if y1 <= 0.18 or y0 >= 0.82:
                continue
            if x0 <= 0.02 or y0 <= 0.02 or x1 >= 0.98 or y1 >= 0.98:
                continue
            if (x1 - x0) * (y1 - y0) > 0.50:
                continue
            boxes.append([x0 * width_pt, y0 * height_pt, x1 * width_pt, y1 * height_pt])
        if not boxes:
            continue
        x0 = min(box[0] for box in boxes)
        y0 = min(box[1] for box in boxes)
        x1 = max(box[2] for box in boxes)
        y1 = max(box[3] for box in boxes)
        margins["left"].append(x0 * 25.4 / 72)
        margins["top"].append(y0 * 25.4 / 72)
        margins["right"].append((width_pt - x1) * 25.4 / 72)
        margins["bottom"].append((height_pt - y1) * 25.4 / 72)
    if flattened or not margins["left"]:
        ink = _ink_margins(page, background)
        if ink:
            measured_margins = ink
        else:
            measured_margins = {"top": 20, "right": 20, "bottom": 20, "left": 20}
        if flattened:
            measured_margins = {
                side: max(10.0, min(40.0, value)) for side, value in measured_margins.items()
            }
    else:
        measured_margins = {
            "top": round(_percentile(margins["top"], 0.1) or 20, 1),
            "right": round(_percentile(margins["right"], 0.1) or 20, 1),
            "bottom": round(_percentile(margins["bottom"], 0.1) or 20, 1),
            "left": round(_percentile(margins["left"], 0.1) or 20, 1),
        }
    for side, value in list(measured_margins.items()):
        measured_margins[side] = max(3.0, min(value, min(width_mm, height_mm) * 0.35))

    # A cover title position is measured from the first page when a large
    # heading appears in the upper half; it drives the cover spacing.
    cover_title_top_mm = None
    if pages_spans:
        first_spans = [
            span for span in pages_spans[0]
            if span["size"] >= title_size * 0.85 and span["bbox"][1] <= height_pt * 0.45
        ]
        if first_spans:
            cover_title_top_mm = round(min(span["bbox"][1] for span in first_spans) * 25.4 / 72, 1)

    # Header/footer repeated spans.
    def margin_spans(spans, band):
        records = []
        for span in spans:
            y0, y1 = span["bbox"][1], span["bbox"][3]
            if band == "header" and y1 <= height_pt * 0.135:
                records.append(span)
            elif band == "footer" and y0 >= height_pt * 0.865:
                records.append(span)
        return records

    def align_of(span):
        x0, x1 = span["bbox"][0], span["bbox"][2]
        center = (x0 + x1) / 2
        if abs(center - width_pt / 2) <= width_pt * 0.08:
            return "center"
        if x1 >= width_pt * 0.78:
            return "end"
        if x0 <= width_pt * 0.22:
            return "start"
        return "end" if x0 > width_pt * 0.5 else "start"

    detected = {"header": None, "footer": None}
    detected_items = {"header": [], "footer": []}
    for band in ("header", "footer"):
        counter = Counter()
        representative = {}
        for page_spans in pages_spans:
            seen = set()
            for span in margin_spans(page_spans, band):
                label = re.sub(r"\d+", "#", span["text"].strip())
                signature = (
                    label,
                    round(span["size"], 1),
                    span["colour"],
                    align_of(span),
                    round((span["bbox"][1] + span["bbox"][3]) / 2 / height_pt * height_mm, 0),
                )
                if signature in seen:
                    continue
                seen.add(signature)
                counter[signature] += 1
                representative.setdefault(signature, span)
        threshold = max(2, int(math.ceil(sampled * 0.4)))
        items = []
        for signature, count in counter.most_common():
            if count < threshold:
                continue
            span = representative[signature]
            label = re.sub(r"\d+", "#", span["text"].strip())
            has_digits = bool(re.search(r"\d", span["text"]))
            word_characters = re.sub(r"[\W_#]+", "", label, flags=re.UNICODE)
            is_page_number = (
                has_digits and "#" in label and len(label.strip()) <= 16
                and len(word_characters) <= 10
            )
            item = {
                "band": band,
                "text_sha256": _sha256_text(span["text"].strip()),
                "characters": len(span["text"].strip()),
                "text_kind": "page-number" if is_page_number else "label",
                "y_mm": round((span["bbox"][1] + span["bbox"][3]) / 2 / height_pt * height_mm, 1),
                "x_mm": round(span["bbox"][0] * 25.4 / 72, 1),
                "size_pt": round(span["size"], 1),
                "font": span["font"],
                "colour": span["colour"],
                "bold": span["bold"],
                "align": align_of(span),
                "occurrences": count,
                "on_first_page": span.get("page") == 0,
                "text": span["text"].strip()[:120],
            }
            items.append(item)
        items.sort(key=lambda item: (item["y_mm"], item["x_mm"]))
        detected_items[band] = items
        if items:
            page_numbers = [item for item in items if item["text_kind"] == "page-number"]
            labels = [item for item in items if item["text_kind"] != "page-number"]
            detected[band] = (page_numbers or labels or items)[0]

    # Recurring vector drawings in header/footer/edge bands.
    drawing_counter = Counter()
    drawing_pages = {}
    drawing_representative = {}
    for page_index, drawings in enumerate(pages_drawings):
        seen = set()
        for info in drawings:
            signature = info["signature"]
            if signature in seen:
                continue
            seen.add(signature)
            drawing_counter[signature] += 1
            drawing_pages.setdefault(signature, set()).add(page_index)
            drawing_representative.setdefault(signature, info)
    vector_specs = []
    background_vector = None
    approximated = []
    vector_threshold = max(2, int(math.ceil(sampled * 0.4)))
    for signature, count in drawing_counter.most_common():
        if count < vector_threshold:
            continue
        info = drawing_representative[signature]
        x0, y0, x1, y1 = info["normalized"]
        area_ratio = max(0, (x1 - x0)) * max(0, (y1 - y0))
        touches_edge = x0 <= 0.025 or y0 <= 0.025 or x1 >= 0.975 or y1 >= 0.975
        if area_ratio >= 0.80 and info["fill"]:
            background_vector = info["fill"]
            continue
        if (info["stroke"] and not info["fill"] and touches_edge
                and area_ratio >= 0.70 and not info["has_curve"] and info["items"] <= 10):
            vector_specs.append({
                "role": "edge",
                "rect_mm": info["rect_mm"],
                "fill": None,
                "stroke": info["stroke"],
                "stroke_width_mm": info["stroke_width_mm"],
                "area_ratio": round(area_ratio, 3),
                "occurrences": count,
                "kind": "stroke-rectangle",
            })
            continue
        if info["has_curve"] or info["items"] > 10 or area_ratio > 0.35:
            if y1 <= 0.20 or y0 >= 0.80 or touches_edge:
                approximated.append(info)
            continue
        if y1 <= 0.16:
            role = "header"
        elif y0 >= 0.84:
            role = "footer"
        elif touches_edge and area_ratio <= 0.12:
            role = "edge"
        else:
            # Body drawings are content (for example a table grid), not design
            # decoration.  They are intentionally neither copied nor reported
            # as an approximate design element.
            continue
        if y0 >= 0.16 and y1 <= 0.84 and role != "edge":
            continue
        spec = {
            "role": role,
            "rect_mm": info["rect_mm"],
            "fill": info["fill"],
            "stroke": info["stroke"],
            "stroke_width_mm": info["stroke_width_mm"],
            "area_ratio": round(area_ratio, 3),
            "occurrences": count,
        }
        if not info["fill"] and info["stroke"]:
            spec["kind"] = "line" if min(
                info["rect_mm"]["right"] - info["rect_mm"]["left"],
                info["rect_mm"]["bottom"] - info["rect_mm"]["top"],
            ) <= 0.9 else "stroke-rectangle"
        else:
            spec["kind"] = "fill-rectangle"
        vector_specs.append(spec)
    if background_vector:
        background = background_vector

    # Cover band: a filled shape at the top of the first page.
    cover_band = None
    first_page_drawings = pages_drawings[0] if pages_drawings else []
    for info in first_page_drawings:
        x0, y0, x1, y1 = info["normalized"]
        height_value = (y1 - y0) * height_mm
        if info["fill"] and y0 <= 0.02 and x1 - x0 >= 0.45 and 1.5 <= height_value <= 45:
            cover_band = {
                "colour": info["fill"],
                "height_mm": round(height_value, 1),
                "width_ratio": round(x1 - x0, 3),
            }
            break

    # Tables: extract any drawing-bordered grid on the first page as an estimate.
    table_border = _blend(text_colour, background, 0.72)
    table_fill = _blend(background, accent, 0.10)
    table_spec = {
        "header_fill": accent,
        "header_text": _contrast_text(accent),
        "border": table_border,
        "zebra": _blend(background, accent, 0.06),
        "body_fill": _blend(background, "#FFFFFF", 0.35),
        "font_size_pt": round(max(7.5, body_size * 0.9), 1),
        "source": "typography/palette estimate; PDF cell fills are not reliably extractable",
    }

    header = detected["header"]
    footer = detected["footer"]
    header_rule = None
    footer_rule = None
    for spec in vector_specs:
        if spec["role"] == "header" and spec.get("kind") in {"line", "stroke-rectangle"}:
            header_rule = spec
        if spec["role"] == "footer" and spec.get("kind") in {"line", "stroke-rectangle"}:
            footer_rule = spec
    # The footer position is a better estimate of the bottom margin than the
    # last line of a short page: the margin exists to hold the footer band.
    if footer_rule:
        measured_margins["bottom"] = round(
            max(10.0, min(45.0, height_mm - float(footer_rule["rect_mm"]["top"]))), 1
        )
    elif footer:
        measured_margins["bottom"] = round(
            max(10.0, min(45.0, height_mm - (float(footer["y_mm"]) - float(footer["size_pt"]) * 0.4))), 1
        )
    contrast_adjustments = []
    for band, items in detected_items.items():
        for item in items:
            behind = background
            for spec in vector_specs:
                if spec.get("role") != band or not spec.get("fill"):
                    continue
                rect = spec.get("rect_mm") or {}
                if float(rect.get("top", 0)) <= float(item["y_mm"]) <= float(rect.get("bottom", 0)):
                    behind = spec["fill"]
                    break
            if _contrast_ratio(item["colour"], behind) < 1.8:
                original = item["colour"]
                candidate = accent if _contrast_ratio(accent, behind) >= 2.4 else text_colour
                if _contrast_ratio(candidate, behind) < 2.0:
                    candidate = _contrast_text(behind)
                item["colour_original"] = original
                item["colour"] = candidate
                item["colour_adjusted"] = True
                contrast_adjustments.append({
                    "property": f"{band} text contrast",
                    "detail": f"Reference text colour {original} had insufficient contrast against {behind}; "
                              f"the new label uses {candidate}",
                    "renderer": "pdf",
                })
    if detected["header"] is not None:
        header = detected["header"]
    if detected["footer"] is not None:
        footer = detected["footer"]

    line_height = 1.5
    if not flattened:
        gaps = []
        for page_spans in pages_spans[:4]:
            by_y = sorted({round(span["bbox"][1], 1) for span in page_spans})
            for first, second in zip(by_y, by_y[1:]):
                gap = second - first
                if 0 < gap <= body_size * 3.2:
                    gaps.append(gap / body_size)
        if gaps:
            gaps.sort()
            line_height = round(gaps[len(gaps) // 2], 2)
            line_height = max(1.15, min(2.3, line_height))

    direction = "ltr"
    if re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text):
        latin = len(re.findall(r"[A-Za-z]", text))
        arabic = len(re.findall(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text))
        direction = "rtl" if arabic > latin else "both"
    # The layout can be mirrored for the other direction; the observed direction
    # is recorded in the report rather than restricting a later job.
    direction_support = ["ltr", "rtl"]

    # Slide geometry for the same visual identity when the job asks for PPTX.
    landscape = width_mm >= height_mm
    if landscape:
        slide_width = 13.333
        slide_height = round(13.333 * height_mm / max(1.0, width_mm), 3)
        slide_height = max(5.5, min(10.0, slide_height))
    else:
        slide_width, slide_height = 13.333, 7.5
    slide_body = round(max(12, min(28, body_size * 1.65)), 1)
    slide_title = round(max(slide_body * 1.5, min(54, title_size * 1.15)), 1)
    slide_margin = round(max(0.3, min(2.0, measured_margins["left"] / max(1.0, width_mm) * slide_width)), 3)
    slide_title_bar = round(
        max(0.55, min(slide_height * 0.45,
                      measured_margins["top"] / max(1.0, height_mm) * slide_height
                      + h1_size / 72 * 1.2)), 3
    ) if not flattened else 0.95

    typography = {
        "title_pt": round(float(title_size), 1),
        "subtitle_pt": round(float(max(body_size * 1.15, body_size)), 1),
        "h1_pt": round(float(h1_size), 1),
        "h2_pt": round(float(h2_size), 1),
        "body_pt": round(float(body_size), 1),
        "small_pt": round(float(max(body_size * 0.88, 6)), 1),
        "caption_pt": round(float(max(body_size * 0.82, 6)), 1),
        "table_pt": round(float(max(body_size * 0.9, 6)), 1),
        "code_pt": round(float(max(body_size * 0.9, 6)), 1),
    }
    if flattened:
        typography.update({
            "title_pt": round(max(20, min(40, height_mm / 8.5)), 1),
            "h1_pt": round(max(14, min(26, height_mm / 13)), 1),
            "h2_pt": round(max(11, min(20, height_mm / 17)), 1),
            "body_pt": round(max(9, min(12.5, height_mm / 26)), 1),
            "subtitle_pt": round(max(10, min(16, height_mm / 19)), 1),
            "caption_pt": round(max(7, min(10, height_mm / 32)), 1),
            "table_pt": round(max(8, min(11, height_mm / 30)), 1),
        })

    palette_style = {
        "background": background,
        "surface": _blend(background, "#FFFFFF", 0.35),
        "text": text_colour,
        "muted": _blend(text_colour, background, 0.45),
        "accent": accent,
        "accent_soft": _blend(background, accent, 0.12),
        "rule": _blend(text_colour, background, 0.72),
        "table_header": accent,
        "table_header_text": _contrast_text(accent),
        "table_zebra": _blend(background, accent, 0.06),
        "example": "#8A5A16",
        "example_soft": "#FBF0D9",
        "warning": "#8A3B12",
    }

    support = {
        "supported": [],
        "preserved": [],
        "approximated": [],
        "unsupported": [],
        "ambiguous": [],
        "excluded": [],
    }
    support["preserved"].extend([
        {"property": "page size", "detail": f"{round(width_mm, 3)} x {round(height_mm, 3)} mm", "renderer": "pdf"},
        {"property": "estimated margins" if flattened else "measured margins",
         "detail": str(measured_margins), "renderer": "pdf"},
        {"property": "estimated typography" if flattened else "body/heading typography",
         "detail": f"body {typography['body_pt']} pt, h1 {typography['h1_pt']} pt, title {typography['title_pt']} pt",
         "renderer": "pdf"},
        {"property": "background and accent colours", "detail": f"{background} / {accent}", "renderer": "pdf"},
    ])
    support["supported"].extend([
        {"property": "header band", "detail": "Detected repeated margin text and rules affect the running header", "renderer": "pdf"} if header else
        {"property": "header treatment", "detail": "No repeated header text was detected; the generated header uses the measured margin", "renderer": "pdf"},
        {"property": "footer band", "detail": "Detected repeated footer text/page number affects the running footer", "renderer": "pdf"} if footer else
        {"property": "footer treatment", "detail": "No repeated footer text was detected; page numbers use the measured margin", "renderer": "pdf"},
        {"property": "simple recurring vector decoration", "detail": f"{len(vector_specs)} rectangle/line element(s) reproduced as CSS vector decoration", "renderer": "pdf"},
        {"property": "table treatment", "detail": "Typography, header fill/text and borders derived from the reference palette", "renderer": "pdf"},
    ])
    if cover_band:
        support["supported"].append({
            "property": "cover band", "detail": f"{cover_band['height_mm']} mm at the page top", "renderer": "pdf",
        })
    for band in ("header", "footer"):
        for item in detected_items[band]:
            support["ambiguous"].append({
                "property": f"reference {band} label",
                "detail": f"{item['characters']} characters at {item['y_mm']} mm ({item['text_kind']}, "
                          f"sha256 {item['text_sha256'][:12]}); the new document's own labels/page numbers are used",
                "renderer": "host-review",
            })
    for info in approximated:
        support["approximated"].append({
            "property": "complex vector decoration",
            "detail": f"{info['items']} drawing item(s) at {info['rect_mm']} not reproduced; review the reference render",
            "renderer": "pdf",
        })
    if flattened:
        support["approximated"].append({
            "property": "flattened/scanned typography",
            "detail": "No reliable text layer; sizes and margins are visual estimates from page ink",
            "renderer": "pdf",
        })
        support["unsupported"].append({
            "property": "flattened header/footer text",
            "detail": "Text inside a flattened scan cannot be measured or regenerated safely",
            "renderer": "pdf",
        })
    support["approximated"].extend(contrast_adjustments)
    if any(not item.get("on_first_page", True) for item in detected_items["header"] + detected_items["footer"]):
        support["approximated"].append({
            "property": "first-page header/footer",
            "detail": "Chromium print templates apply the running header/footer to the cover as well; the reference "
                      "did not show this element on its first page",
            "renderer": "pdf",
        })
    if header or footer:
        support["approximated"].append({
            "property": "header/footer labels",
            "detail": "Positions, sizes and colours follow the reference; the words are new-document labels, not copies",
            "renderer": "pdf",
        })
    support["unsupported"].extend([
        {"property": "complex logo/vector artwork", "detail": "Only simple repeated rectangles/lines are reproduced; a flattened or complex logo is reported, not silently approximated", "renderer": "pdf"},
        {"property": "reference pictures", "detail": "Pictures inside the reference are not reused; text-only output contains no picture objects", "renderer": "policy"},
        {"property": "pixel-identical reproduction", "detail": "A PDF reference cannot be promised as a pixel-identical template in another renderer", "renderer": "pdf"},
    ])
    support["excluded"].append({
        "property": "original page prose/diagrams",
        "detail": "Rendered pages are for inspection and comparison only; they are never placed behind new prose",
    })

    design = {
        "schema_version": SCHEMA_VERSION,
        "kind": "reference_design",
        "source_format": "pdf",
        "method": "visual-estimate" if flattened else "measured",
        "reference": {
            "name": path.name,
            "path": str(path.resolve()),
            "sha256": _source_identity(path)["sha256"],
            "bytes": path.stat().st_size,
            "pages": total_pages,
            "pages_inspected": sampled,
        },
        "identity": {
            "mode": "self-contained",
            "source_path": str(path.resolve()),
            "source_sha256": _source_identity(path)["sha256"],
        },
        "page_size_mm": {"width": round(width_mm, 3), "height": round(height_mm, 3)},
        "margins_mm": measured_margins,
        "palette": palette_style,
        "fonts": {
            "latin": {"family": _font_name(body_font), "fallbacks": ["Georgia", "Times New Roman", "serif"]},
            "arabic": {"family": _font_name(arabic_font), "fallbacks": ["Segoe UI", "Tahoma", "Arial", "sans-serif"]},
            "display": {"family": _font_name(heading_font), "fallbacks": [_font_name(body_font), "Segoe UI", "serif"]},
            "mono": {"family": "Consolas", "fallbacks": ["Courier New", "monospace"]},
        },
        "typography": typography,
        "line_height": line_height,
        "direction_support": direction_support,
        "body": {
            "font": body_font,
            "size_pt": typography["body_pt"],
            "colour": text_colour,
        },
        "headings": {
            "title": {"size_pt": typography["title_pt"], "colour": heading_colour, "font": heading_font, "bold": True},
            "h1": {"size_pt": typography["h1_pt"], "colour": heading_colour, "font": heading_font, "bold": True},
            "h2": {"size_pt": typography["h2_pt"], "colour": text_colour, "font": heading_font, "bold": True},
        },
        "cover": {
            "band": cover_band,
            "rule_colour": accent,
            "title_size_pt": typography["title_pt"],
            "title_top_mm": cover_title_top_mm,
        },
        "header": {
            "detected": {key: value for key, value in (header or {}).items() if key != "text"} if header else None,
            "items": [
                {key: value for key, value in item.items() if key != "text"}
                for item in detected_items["header"]
            ],
            "apply": bool(detected_items["header"]),
            "rule": header_rule,
            "size_pt": (header or {}).get("size_pt"),
            "colour": (header or {}).get("colour"),
            "align": (header or {}).get("align"),
            "font": (header or {}).get("font"),
            "y_mm": (header or {}).get("y_mm"),
        },
        "footer": {
            "detected": {key: value for key, value in (footer or {}).items() if key != "text"} if footer else None,
            "items": [
                {key: value for key, value in item.items() if key != "text"}
                for item in detected_items["footer"]
            ],
            "apply": bool(detected_items["footer"]),
            "rule": footer_rule,
            "size_pt": (footer or {}).get("size_pt"),
            "colour": (footer or {}).get("colour"),
            "align": (footer or {}).get("align"),
            "font": (footer or {}).get("font"),
            "y_mm": (footer or {}).get("y_mm"),
        },
        "vector_decor": vector_specs,
        "table": table_spec,
        "slides": {
            "width_in": slide_width,
            "height_in": slide_height,
            "margin_in": slide_margin,
            "title_bar_in": slide_title_bar,
            "typography": {
                "title_pt": slide_title,
                "section_pt": round(max(slide_title * 1.05, slide_body * 1.5), 1),
                "body_pt": slide_body,
                "bullet_pt": round(max(slide_body * 0.95, 12), 1),
                "caption_pt": round(max(slide_body * 0.6, 9), 1),
                "table_pt": round(max(slide_body * 0.75, 10), 1),
                "equation_pt": round(max(slide_body * 1.15, 14), 1),
            },
        },
        "support": support,
    }

    analysis = {
        "method": "visual-estimate" if flattened else "measured",
        "page_count": total_pages,
        "pdf_page_count": total_pages,
        "pages_inspected": sampled,
        "page_size_mm": {"width": round(width_mm, 3), "height": round(height_mm, 3)},
        "font_observations": [{"font": font, "characters": count} for font, count in font_counter.most_common(8)],
        "size_observations": [{"pt": size, "characters": count} for size, count in sorted(size_counter.items(), reverse=True)[:10]],
        "span_colours": [{"colour": colour, "characters": count} for colour, count in colour_counter.most_common(8)],
        "colours": palette,
        "margins_mm": measured_margins,
        "typography": typography,
        "line_height": line_height,
        "extractable_characters": characters,
        "flattened": flattened,
        "direction_estimate": direction,
        "direction": direction,
        "direction_support": direction_support,
        "header": {key: value for key, value in (header or {}).items() if key != "text"} if header else None,
        "footer": {key: value for key, value in (footer or {}).items() if key != "text"} if footer else None,
        "recurring_vector_elements": len(vector_specs),
        "approximated_vector_elements": len(approximated),
        "cover_band": cover_band,
    }
    document.close()
    return design, analysis, support


def _font_name(value):
    text = str(value or "").split("+")[-1].strip()
    text = re.sub(r"-(Bold|Italic|BoldItalic|Regular|Light|Medium|Semibold|SemiBold)$", "", text, flags=re.I)
    aliases = {
        "times": "Times New Roman",
        "timesroman": "Times New Roman",
        "times-roman": "Times New Roman",
        "helvetica": "Arial",
        "helv": "Arial",
        "courier": "Courier New",
        "couriernew": "Courier New",
        "symbol": "Segoe UI Symbol",
    }
    key = text.replace(" ", "").casefold()
    return aliases.get(key, text) or "Georgia"


def extract_pdf_design(path, name, workspace):
    path = Path(path)
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    preview_dir = workspace / "reference-pages"
    previews = render_pdf_pages(path, preview_dir, limit=6)
    design, analysis, support = _collect_pdf_design(path, name, workspace)
    support["approximated"].append({
        "property": "font availability",
        "detail": "Observed font names were not checked against fonts installed on this host; Chromium may substitute "
                  "a fallback, reported in the PDF read-back.",
        "renderer": "pdf",
    })
    design["previews"] = {
        "reference_pages": previews.get("paths", []),
        "renderer": "PyMuPDF",
        "rendering_note": None,
    }
    return design, analysis, support, previews


# ---------------------------------------------------------------------------
# Raster extraction
# ---------------------------------------------------------------------------


def extract_raster_design(path, name, workspace):
    from PIL import Image
    from .jobs import file_digest

    path = Path(path)
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    preview_dir = workspace / "reference-image"
    preview_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as image:
        width, height = image.size
        rgb = image.convert("RGB")
        preview = rgb.copy()
        preview.thumbnail((1800, 1800))
        preview_path = preview_dir / "reference.png"
        preview.save(preview_path)
        sampled = rgb.resize((160, max(1, int(160 * height / max(1, width)))))
        quantized = sampled.quantize(colors=10, method=Image.Quantize.MEDIANCUT)
        palette = quantized.getpalette() or []
        counts = _pixel_counts(quantized)
        colours = []
        for index, count in counts.most_common():
            base = index * 3
            colours.append({"colour": _hex(palette[base:base + 3]), "share": round(count / sum(counts.values() or 1), 3)})
    background = colours[0]["colour"] if colours else "#FFFFFF"
    accent = next(
        (item["colour"] for item in colours[1:] if _saturation(item["colour"]) > 0.2),
        "#2B4ACB",
    )
    typography = {
        "title_pt": 30, "subtitle_pt": 14, "h1_pt": 20, "h2_pt": 13,
        "body_pt": 11, "small_pt": 9, "caption_pt": 8.5, "table_pt": 9.5, "code_pt": 9,
    }
    support = {
        "supported": [
            {"property": "raster preview", "detail": str(preview_path), "renderer": "host-review"},
            {"property": "palette estimate", "detail": ", ".join(item["colour"] for item in colours[:6]), "renderer": "pdf/pptx"},
            {"property": "aspect ratio", "detail": f"{width} x {height} px", "renderer": "pdf/pptx"},
        ],
        "preserved": [
            {"property": "reference image", "detail": "A normalised preview is emitted; the original file is never modified", "renderer": "host-review"},
        ],
        "approximated": [
            {"property": "typography and margins", "detail": "A raster reference has no structural text; values are conservative estimates", "renderer": "pdf/pptx"},
        ],
        "unsupported": [
            {"property": "named layouts/placeholders", "detail": "A raster image cannot supply editable layout structure", "renderer": "pptx"},
            {"property": "exact fonts", "detail": "Font names cannot be read from pixels", "renderer": "pdf/pptx"},
            {"property": "reference pictures", "detail": "The raster itself is not inserted into output", "renderer": "policy"},
        ],
        "ambiguous": [],
        "excluded": [
            {"property": "original image content", "detail": "The preview is for inspection, not a background layer", "renderer": "policy"},
        ],
    }
    support["approximated"].append({
        "property": "font availability",
        "detail": "A raster reference cannot identify installed fonts; conservative fallbacks are used and reported.",
        "renderer": "pdf/pptx",
    })
    design = {
        "schema_version": SCHEMA_VERSION,
        "kind": "reference_design",
        "source_format": "raster",
        "method": "visual-estimate",
        "reference": {
            "name": path.name, "path": str(path.resolve()), "sha256": file_digest(path),
            "bytes": path.stat().st_size, "pixel_size": {"width": width, "height": height},
        },
        "identity": {"mode": "self-contained", "source_path": str(path.resolve()),
                     "source_sha256": file_digest(path)},
        "palette": {
            "background": background,
            "surface": _blend(background, "#FFFFFF", 0.35),
            "text": "#20262B",
            "muted": "#5B6670",
            "accent": accent,
            "accent_soft": _blend(background, accent, 0.12),
            "rule": _blend("#20262B", background, 0.72),
            "table_header": accent,
            "table_header_text": _contrast_text(accent),
            "table_zebra": _blend(background, accent, 0.06),
            "example": "#8A5A16",
            "example_soft": "#FBF0D9",
            "warning": "#8A3B12",
        },
        "fonts": {
            "latin": {"family": "Segoe UI", "fallbacks": ["Arial", "sans-serif"]},
            "arabic": {"family": "Segoe UI", "fallbacks": ["Tahoma", "Arial", "sans-serif"]},
            "display": {"family": "Segoe UI Semibold", "fallbacks": ["Segoe UI", "Arial", "sans-serif"]},
            "mono": {"family": "Consolas", "fallbacks": ["Courier New", "monospace"]},
        },
        "typography": typography,
        "previews": {"reference_pages": [str(preview_path)], "renderer": "PIL", "rendering_note": None},
        "support": support,
    }
    analysis = {
        "method": "visual-estimate",
        "pixel_size": {"width": width, "height": height},
        "palette": colours[:10],
        "notes": "A raster reference permits colour/aspect estimates only; it is not a structural template.",
    }
    return design, analysis, support, {"available": True, "paths": [str(preview_path)], "renderer": "PIL"}


# ---------------------------------------------------------------------------
# Preview comparison
# ---------------------------------------------------------------------------


def _image_stats(path):
    from PIL import Image

    with Image.open(path) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        sampled = rgb.resize((100, max(1, int(100 * height / max(1, width)))))
        quantized = sampled.quantize(colors=10, method=Image.Quantize.MEDIANCUT)
        palette = quantized.getpalette() or []
        counts = _pixel_counts(quantized)
        colours = []
        for index, count in counts.most_common():
            base = index * 3
            colours.append((tuple(palette[base:base + 3]), count))
        total = sum(count for _, count in colours) or 1
        background = colours[0][0] if colours else (255, 255, 255)
        non_background = [
            (rgb_value, count) for rgb_value, count in colours
            if _rgb_distance(rgb_value, background) > 35
        ]
        accent = max(non_background, key=lambda item: _saturation(_hex(item[0])))[0] \
            if non_background else background
    return {
        "size": {"width": width, "height": height},
        "aspect": round(width / max(1, height), 3),
        "background": _hex(background),
        "accent": _hex(accent),
        "colours": [{"colour": _hex(rgb_value), "share": round(count / total, 3)} for rgb_value, count in colours[:6]],
    }


def _font_loader(size):
    from PIL import ImageFont

    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def make_side_by_side(reference_paths, new_paths, destination, title="Reference versus new content",
                      reference_label="REFERENCE", new_label="NEW CONTENT",
                      reference_total=None, new_total=None):
    """Compose real side-by-side preview images and a measured difference report."""

    from PIL import Image, ImageDraw

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for stale in destination.glob("comparison-*.png"):
        stale.unlink(missing_ok=True)
    references = [Path(path) for path in (reference_paths or []) if Path(path).is_file()]
    news = [Path(path) for path in (new_paths or []) if Path(path).is_file()]
    if not references:
        return {
            "available": False,
            "reason": "No rendered reference preview is available. Re-extract the reference or keep its preview files.",
            "images": [],
        }
    if not news:
        return {
            "available": False,
            "reason": "No rendered new-content preview is available; actual slide/page rendering must succeed before a visual comparison can be made.",
            "images": [],
        }
    pairs = min(len(references), len(news))
    height = 900
    gap = 24
    label_height = 46
    images = []
    pair_reports = []
    differences = []
    for index in range(pairs):
        ref = Image.open(references[index]).convert("RGB")
        new = Image.open(news[index]).convert("RGB")
        ref_ratio = height / ref.height
        new_ratio = height / new.height
        ref_scaled = ref.resize((max(1, int(ref.width * ref_ratio)), height), Image.LANCZOS)
        new_scaled = new.resize((max(1, int(new.width * new_ratio)), height), Image.LANCZOS)
        canvas = Image.new("RGB", (ref_scaled.width + gap + new_scaled.width, height + label_height), "white")
        canvas.paste(ref_scaled, (0, label_height))
        canvas.paste(new_scaled, (ref_scaled.width + gap, label_height))
        draw = ImageDraw.Draw(canvas)
        font = _font_loader(22)
        draw.rectangle([0, 0, canvas.width, label_height], fill="#20262B")
        draw.text((12, 12), f"{reference_label} - {references[index].name}", fill="white", font=font)
        draw.text((ref_scaled.width + gap + 12, 12), f"{new_label} - {news[index].name}", fill="white", font=font)
        target = destination / f"comparison-{index + 1:03d}.png"
        canvas.save(target)
        images.append(str(target))
        ref_stats = _image_stats(references[index])
        new_stats = _image_stats(news[index])
        pair_reports.append({
            "reference": str(references[index]),
            "new_content": str(news[index]),
            "comparison": str(target),
            "reference_stats": ref_stats,
            "new_stats": new_stats,
        })
        if abs(ref_stats["aspect"] - new_stats["aspect"]) > 0.08:
            differences.append(
                f"Page {index + 1}: aspect ratio differs ({ref_stats['aspect']} reference vs {new_stats['aspect']} new)"
            )
        background_delta = _colour_distance(ref_stats["background"], new_stats["background"])
        accent_delta = _colour_distance(ref_stats["accent"], new_stats["accent"])
        if background_delta > 18:
            differences.append(
                f"Page {index + 1}: sampled background differs by {round(background_delta)} RGB distance "
                f"({ref_stats['background']} vs {new_stats['background']})"
            )
        if accent_delta > 45:
            differences.append(
                f"Page {index + 1}: sampled accent differs by {round(accent_delta)} RGB distance "
                f"({ref_stats['accent']} vs {new_stats['accent']})"
            )
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "reference_comparison",
        "title": title,
        "available": True,
        "images": images,
        "pairs": pair_reports,
        "reference_count": len(references),
        "new_content_count": len(news),
        "reference_total": reference_total if reference_total is not None else len(references),
        "new_content_total": new_total if new_total is not None else len(news),
        "paired_count": pairs,
        "differences": differences,
        "notes": [
            "The two sides contain different content by design; colour similarity alone does not prove style fidelity.",
            "Inspect the comparison images at full size. Reference and output renderers may use different font substitution.",
            "Content completeness and scientific correctness are not established by this comparison.",
        ],
    }
    if (report["reference_total"] > pairs) or (report["new_content_total"] > pairs):
        report["differences"].append(
            f"Representative pairing covers {pairs} pair(s) from {report['reference_total']} reference and "
            f"{report['new_content_total']} new-content page/slide(s); inspect the complete files separately."
        )
    elif len(references) != len(news):
        report["differences"].append(
            f"Representative pairing covers {pairs} of {len(references)} reference and {len(news)} new-content images; "
            "this is not a page-by-page identity comparison."
        )
    if not report["differences"]:
        report["differences"].append(
            "No measurable page-size/background/accent difference was detected in the paired images, but different "
            "content and renderer font substitution still require full-size visual review."
        )
    return report


# ---------------------------------------------------------------------------
# Saved-design verification and asset freezing
# ---------------------------------------------------------------------------


def resolve_style_base(style, style_path=None):
    if style_path:
        path = Path(style_path)
        if path.is_file():
            return path.resolve().parent
    source = style.get("_source_path") if isinstance(style, dict) else None
    if source:
        return Path(source).resolve().parent
    return None


def _resolve_design_path(value, style, style_path=None):
    path = Path(str(value))
    if path.is_absolute():
        return path
    base = resolve_style_base(style, style_path)
    if base is None:
        raise ValueError(
            "This style references a relative design asset, but it was supplied as an in-memory object. "
            "Pass the saved style JSON path (or an absolute design asset path) so its frozen files resolve."
        )
    resolved = (base / path).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError(
            "A relative design asset path escapes the style directory; refusing to read it."
        )
    return resolved


def verify_style_design(style, style_path=None):
    """Verify frozen design assets and detect a changed source reference."""

    from .jobs import file_digest

    design = style.get("design") if isinstance(style, dict) else None
    if not isinstance(design, dict):
        return {"available": False, "reason": "This style has no reference design"}
    result = {
        "available": True,
        "method": design.get("method"),
        "source_format": design.get("source_format"),
        "warnings": [],
        "frozen": False,
        "source_changed": False,
        "missing": [],
    }
    pptx = design.get("pptx") if isinstance(design.get("pptx"), dict) else design
    template = (pptx or {}).get("template") if isinstance(pptx, dict) else None
    if isinstance(template, dict) and template.get("path"):
        try:
            template_path = _resolve_design_path(template["path"], style, style_path)
        except ValueError as error:
            raise ValueError(str(error)) from error
        if not template_path.is_file():
            result["missing"].append(str(template_path))
            raise ValueError(
                "The saved design's frozen PPTX template is missing; the style cannot be reused safely. "
                f"Expected: {template_path}. Re-extract and re-save the reference design."
            )
        digest = file_digest(template_path)
        if template.get("sha256") and digest != template["sha256"]:
            raise ValueError(
                "The saved design's frozen PPTX template changed after saving; refusing to bind to a different file."
            )
        result["frozen"] = True
        result["template_path"] = str(template_path)
        result["template_sha256"] = digest
        source_path = template.get("source_path")
        source_sha = template.get("source_sha256")
        if source_path and Path(source_path).is_file():
            current = file_digest(source_path)
            if source_sha and current != source_sha:
                result["source_changed"] = True
                result["warnings"].append(
                    "The original reference changed after extraction. The explicitly frozen sanitized template "
                    "is used; the new content is not silently rebound to the changed source."
                )
        elif source_path:
            result["warnings"].append(
                "The original reference is no longer present. The frozen sanitized template is self-contained and is used."
            )
    elif template and not template.get("path"):
        result["warnings"].append("The design record has no native template path; only design tokens can be reused.")
    if not result.get("frozen"):
        identity = design.get("identity") or {}
        source_path = identity.get("source_path") or (design.get("reference") or {}).get("original_path")
        source_sha = identity.get("source_sha256") or (design.get("reference") or {}).get("original_sha256")
        if source_path and Path(source_path).is_file():
            current = file_digest(source_path)
            if source_sha and current != source_sha:
                result["source_changed"] = True
                result["warnings"].append(
                    "The original reference changed after extraction. The saved style remains a self-contained design "
                    "record, but a new visual comparison will not bind to the changed file."
                )
        elif source_path:
            result["warnings"].append(
                "The original reference is no longer present. The saved design record is self-contained, but reference "
                "previews cannot be regenerated."
            )
    previews = design.get("previews") or {}
    missing_previews = []
    for group in ("reference_pages", "reference_slides"):
        for value in previews.get(group) or []:
            try:
                path = _resolve_design_path(value, style, style_path)
            except ValueError:
                continue
            if not path.is_file():
                missing_previews.append(str(path))
    if missing_previews:
        result["warnings"].append(
            "Some reference preview files are missing (they may have been job-local). "
            "The design tokens remain usable; regenerate previews from the intact original for a visual comparison."
        )
        result["missing_previews"] = missing_previews
    return result


def design_template_path(style, style_path=None):
    design = style.get("design") if isinstance(style, dict) else None
    if not isinstance(design, dict):
        return None
    pptx = design.get("pptx") if isinstance(design.get("pptx"), dict) else design
    template = (pptx or {}).get("template") if isinstance(pptx, dict) else None
    if not isinstance(template, dict) or not template.get("path"):
        return None
    verify_style_design(style, style_path)
    return _resolve_design_path(template["path"], style, style_path)


def reference_preview_paths(style, style_path=None, kind="pdf"):
    design = style.get("design") if isinstance(style, dict) else None
    if not isinstance(design, dict):
        return []
    previews = design.get("previews") or {}
    if kind == "pptx":
        values = previews.get("reference_slides") or []
    else:
        values = previews.get("reference_pages") or []
    resolved = []
    for value in values:
        try:
            path = _resolve_design_path(value, style, style_path)
        except ValueError:
            continue
        if path.is_file():
            resolved.append(str(path))
    return resolved


def ensure_reference_previews(style, style_path, destination, kind="pdf", limit=6):
    """Return rendered reference previews, regenerating them from an intact source.

    A saved style deliberately does not store reference screenshots (they may
    contain original lecture content).  If the original file is present and its
    hash still matches the recorded identity, previews are regenerated on
    demand; a changed or missing source is reported and never silently rebound.
    """

    from .jobs import file_digest

    design = style.get("design") if isinstance(style, dict) else None
    if not isinstance(design, dict):
        return {"available": False, "reason": "This style has no reference design", "paths": []}
    existing = reference_preview_paths(style, style_path, kind=kind)
    if existing:
        return {"available": True, "paths": existing, "source": "recorded previews"}
    reference = design.get("reference") or {}
    source_value = reference.get("original_path") or reference.get("path")
    expected = reference.get("original_sha256") or reference.get("sha256")
    if not source_value:
        return {"available": False, "reason": "The design record has no source reference path", "paths": []}
    source = Path(str(source_value))
    if not source.is_file():
        return {
            "available": False,
            "reason": "The original reference file is missing. The frozen design can still be used, but a new visual "
                      "comparison needs the intact original.",
            "paths": [],
        }
    if expected and file_digest(source) != expected:
        return {
            "available": False,
            "reason": "The original reference changed after extraction. Refusing to compare against a different file; "
                      "re-extract and re-save the design for a new comparison.",
            "paths": [],
            "source_changed": True,
        }
    destination = Path(destination)
    format_value = design.get("source_format")
    if kind == "pdf" and format_value == "pptx":
        return {
            "available": False,
            "reason": "This style came from a slide template. It supplies typography and palette to the PDF, but a "
                      "slide master is not a PDF page master; compare the design with the PPTX output instead.",
            "paths": [],
        }
    if kind == "pptx" and format_value == "pdf":
        return {
            "available": False,
            "reason": "This style came from a PDF. It supplies typography and palette to the deck, but PDF page "
                      "geometry is not a native slide template; compare the design with the PDF output instead.",
            "paths": [],
        }
    if format_value in {"pptx"}:
        result = render_pptx_slides(source, destination, limit=limit)
    elif format_value == "pdf":
        result = render_pdf_pages(source, destination, limit=limit)
    elif format_value == "raster":
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / "reference.png"
        from PIL import Image

        with Image.open(source) as image:
            image.convert("RGB").save(target)
        result = {"available": True, "paths": [str(target)], "renderer": "PIL"}
    else:
        result = {"available": False, "reason": f"Unsupported reference format: {format_value}", "paths": []}
    if result.get("available"):
        result["source"] = "regenerated from intact original"
    return result


def freeze_style_assets(style, style_path=None, library_style_path=None):
    """Copy a sanitized template into a shared-library style directory.

    Reference preview images are deliberately not copied: they may contain
    original lecture content.  They remain job-local inspection artifacts.
    """

    from .jobs import file_digest

    design = style.get("design") if isinstance(style, dict) else None
    if not isinstance(design, dict):
        return style
    pptx = design.get("pptx") if isinstance(design.get("pptx"), dict) else design
    template = (pptx or {}).get("template") if isinstance(pptx, dict) else None
    if not isinstance(template, dict) or not template.get("path") or library_style_path is None:
        # PDF/raster designs are self-contained token records.  Their reference
        # previews are still job-local inspection artifacts, not library content.
        design["previews"] = dict(design.get("previews") or {})
        design["previews"]["reference_pages"] = []
        design["previews"]["reference_slides"] = []
        design["previews"]["note"] = (
            "Reference preview images are job-local inspection artifacts and are not stored in the shared library; "
            "they may contain original lecture content. Re-render the intact original for a later visual comparison."
        )
        design["report_path"] = None
        return style
    library_style_path = Path(library_style_path)
    assets_dir = library_style_path.parent / "assets" / library_style_path.stem
    assets_dir.mkdir(parents=True, exist_ok=True)
    source = _resolve_design_path(template["path"], style, style_path)
    if not source.is_file():
        raise ValueError(
            "Cannot save the named style: its frozen design template is missing. "
            f"Expected: {source}. Re-extract the reference or choose an intact frozen style."
        )
    if template.get("sha256") and file_digest(source) != template["sha256"]:
        raise ValueError(
            "Cannot save the named style: its design template changed after extraction. "
            "Re-extract the reference before saving."
        )
    target = assets_dir / "template.pptx"
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    digest = file_digest(target)
    relative = target.relative_to(library_style_path.parent).as_posix()
    template.update({
        "path": relative,
        "sha256": digest,
        "frozen": True,
        "sanitized": True,
        "source_path": template.get("source_path"),
        "source_sha256": template.get("source_sha256"),
    })
    design["identity"] = dict(design.get("identity") or {})
    design["identity"].update({
        "mode": "frozen",
        "frozen_path": relative,
        "frozen_sha256": digest,
        "source_path": template.get("source_path"),
        "source_sha256": template.get("source_sha256"),
    })
    # Preview paths remain job-local and are not persisted as personal content.
    design["previews"] = dict(design.get("previews") or {})
    design["previews"]["reference_pages"] = []
    design["previews"]["reference_slides"] = []
    design["previews"]["note"] = (
        "Reference preview images are job-local inspection artifacts and are not stored in the shared library; "
        "they may contain original lecture content. Re-render the intact original for a later visual comparison."
    )
    design["report_path"] = None
    return style


# ---------------------------------------------------------------------------
# Public design summary
# ---------------------------------------------------------------------------


def design_summary(style, style_path=None):
    design = style.get("design") if isinstance(style, dict) else None
    if not isinstance(design, dict):
        return {"available": False}
    identity = verify_style_design(style, style_path)
    return {
        "available": True,
        "source_format": design.get("source_format"),
        "method": design.get("method"),
        "reference": design.get("reference"),
        "identity": identity,
        "support": design.get("support"),
        "previews": design.get("previews"),
    }
