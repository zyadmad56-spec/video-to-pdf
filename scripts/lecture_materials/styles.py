"""Visual styles, teaching profiles, reference extraction, and a portable library.

Visual design and teaching approach are deliberately separate:

* ``visual_style`` records fonts, palette, page/slide geometry, layout families,
  and image/table rules.  It never carries lecture subject matter.
* ``teaching_profile`` records explanation preferences inferred from the user's
  own examples.  It stores preferences, not the personal example text itself.

The module reads and writes ordinary UTF-8 JSON.  It never executes style or
profile content.  A library is resolved explicitly (``save_named_profile``'s
``library`` argument) or through ``LECTURE_MATERIALS_LIBRARY``; the fallback is
``<package>/library`` so a moved skill keeps its shared styles together while
remaining outside every generated job directory.
"""

from __future__ import annotations

import copy
import math
import os
import re
import stat
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .jobs import file_digest, json_digest, read_json, write_json

SCHEMA_VERSION = 1
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
STYLE_DIRECTORY = PACKAGE_ROOT / "assets" / "styles"
BUILTIN_IDS = ("scholar-ink", "clarity-grid")
STYLE_KIND = "visual_style"
PROFILE_KIND = "teaching_profile"
KIND_DIRECTORIES = {
    STYLE_KIND: "visual-styles",
    PROFILE_KIND: "teaching-profiles",
}
NAME_PATTERN = re.compile(r"^[\w\u0600-\u06FF][\w\u0600-\u06FF ._()&'’\-]*$", re.UNICODE)
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
PROFILE_FIELDS = {
    "tone",
    "detail_level",
    "depth",
    "step_size",
    "steps",
    "example_type",
    "example_style",
    "analogy",
    "terminology",
    "terminology_treatment",
    "technical_level",
    "structure",
    "format",
    "question_style",
    "language_style",
    "audience",
    "pacing",
    "notes",
}

DEFAULT_STYLE = {
    "schema_version": SCHEMA_VERSION,
    "kind": STYLE_KIND,
    "id": "scholar-ink",
    "name": "Scholar Ink",
    "version": "1.0.0",
    "builtin": False,
    "direction_support": ["ltr", "rtl"],
    "languages": ["en", "ar", "mixed"],
    "description": "",
    "fonts": {
        "latin": {"family": "Georgia", "fallbacks": ["Times New Roman", "serif"]},
        "arabic": {"family": "Segoe UI", "fallbacks": ["Tahoma", "Arial", "sans-serif"]},
        "display": {"family": "Georgia", "fallbacks": ["Segoe UI", "serif"]},
        "mono": {"family": "Consolas", "fallbacks": ["Courier New", "monospace"]},
    },
    "palette": {
        "background": "#F8F4EA",
        "surface": "#FFFDF8",
        "text": "#20262B",
        "muted": "#5B6670",
        "accent": "#0E5B54",
        "accent_soft": "#DCEAE5",
        "rule": "#C7D3CE",
        "table_header": "#0E5B54",
        "table_header_text": "#FFFFFF",
        "table_zebra": "#F0F5F2",
        "example": "#8A5A16",
        "example_soft": "#FBF0D9",
        "warning": "#8A3B12",
    },
    "page": {
        "width_mm": 210,
        "height_mm": 297,
        "margin_mm": {"top": 20, "right": 20, "bottom": 20, "left": 20},
        "line_height": 1.55,
        "typography": {
            "title_pt": 28,
            "subtitle_pt": 13,
            "h1_pt": 18,
            "h2_pt": 12.5,
            "body_pt": 10.5,
            "small_pt": 8.5,
            "caption_pt": 8.5,
            "table_pt": 9,
            "code_pt": 9,
        },
    },
    "slides": {
        "width_in": 13.333,
        "height_in": 7.5,
        "margin_in": 0.6,
        "title_bar_in": 1.0,
        "typography": {
            "title_pt": 30,
            "section_pt": 34,
            "body_pt": 18,
            "bullet_pt": 17,
            "caption_pt": 11,
            "table_pt": 13,
            "equation_pt": 22,
        },
    },
    "layout": {
        "cover": "band",
        "section": "band",
        "image_alignment": "end",
        "toc_columns": 2,
        "rule_under_titles": True,
        "families": ["cover", "section", "explanation", "bullets", "table", "equation", "image-with-text"],
    },
    "rules": {
        "images": {"max_width_ratio": 0.72, "max_height_ratio": 0.46, "caption_pt": 8.5},
        "tables": {"zebra": True, "repeat_header": True, "min_font_pt": 8},
        "equations": {"direction": "ltr", "align": "center"},
    },
    "provenance": {"source": "built-in", "method": "handcrafted", "reference_sha256": None,
                   "preview": None, "notes": ""},
}


# ---------------------------------------------------------------------------
# Generic JSON helpers
# ---------------------------------------------------------------------------


def _deep_merge(base, override):
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _number(value, label, minimum=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return float(value)


def _colour(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        raise ValueError(f"{label} must be a #RRGGBB colour")
    return value.upper()


def _font_group(document, label, fallback):
    group = document.get(label)
    if not isinstance(group, dict):
        group = copy.deepcopy(fallback)
    family = group.get("family")
    if not isinstance(family, str) or not family.strip():
        family = fallback["family"]
    fallbacks = group.get("fallbacks", fallback.get("fallbacks", []))
    if not isinstance(fallbacks, list) or any(not isinstance(item, str) or not item.strip() for item in fallbacks):
        fallbacks = list(fallback.get("fallbacks", []))
    return {"family": family.strip(), "fallbacks": [item.strip() for item in fallbacks]}


def normalize_style(document):
    """Return a validated, fully populated style object (never mutates input)."""

    if not isinstance(document, dict):
        raise ValueError("A visual style must be a JSON object")
    style = _deep_merge(DEFAULT_STYLE, document)
    if style.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported visual style schema version")
    if style.get("kind") != STYLE_KIND:
        raise ValueError("A visual style document must have kind='visual_style'")
    for key in ("id", "name", "version"):
        if not isinstance(style.get(key), str) or not style[key].strip():
            raise ValueError(f"A visual style needs a nonempty {key}")
    directions = style.get("direction_support")
    if not isinstance(directions, list) or not directions \
            or any(not isinstance(item, str) for item in directions) \
            or not set(directions) <= {"ltr", "rtl"}:
        raise ValueError("direction_support must contain 'ltr' and/or 'rtl'")
    for label in ("fonts", "palette", "page", "slides", "layout", "rules"):
        if not isinstance(style.get(label), dict):
            raise ValueError(f"style.{label} must be an object")
    for label in ("latin", "arabic", "display", "mono"):
        style["fonts"][label] = _font_group(style["fonts"], label, DEFAULT_STYLE["fonts"][label])
    for label in DEFAULT_STYLE["palette"]:
        style["palette"][label] = _colour(style["palette"].get(label), f"palette.{label}")
    page = style["page"]
    page["width_mm"] = _number(page.get("width_mm"), "page.width_mm", 50)
    page["height_mm"] = _number(page.get("height_mm"), "page.height_mm", 50)
    margins = page.get("margin_mm")
    if not isinstance(margins, dict):
        raise ValueError("page.margin_mm must be an object")
    for side in ("top", "right", "bottom", "left"):
        margins[side] = _number(margins.get(side, 20), f"page.margin_mm.{side}", 0)
        if margins[side] >= min(page["width_mm"], page["height_mm"]) / 2:
            raise ValueError(f"page.margin_mm.{side} leaves no printable area")
    page["line_height"] = _number(page.get("line_height"), "page.line_height", 0.9)
    typography = page.get("typography")
    if not isinstance(typography, dict):
        raise ValueError("page.typography must be an object")
    for key, fallback in DEFAULT_STYLE["page"]["typography"].items():
        typography[key] = _number(typography.get(key, fallback), f"page.typography.{key}", 4)
    slides = style["slides"]
    slides["width_in"] = _number(slides.get("width_in"), "slides.width_in", 4)
    slides["height_in"] = _number(slides.get("height_in"), "slides.height_in", 3)
    slides["margin_in"] = _number(slides.get("margin_in"), "slides.margin_in", 0)
    if slides["margin_in"] * 2 >= min(slides["width_in"], slides["height_in"]):
        raise ValueError("slides.margin_in leaves no slide area")
    slides["title_bar_in"] = _number(slides.get("title_bar_in"), "slides.title_bar_in", 0.2)
    if slides["title_bar_in"] + slides["margin_in"] * 2 >= slides["height_in"]:
        raise ValueError("slides.title_bar_in leaves no body area")
    slide_type = slides.get("typography")
    if not isinstance(slide_type, dict):
        raise ValueError("slides.typography must be an object")
    for key, fallback in DEFAULT_STYLE["slides"]["typography"].items():
        slide_type[key] = _number(slide_type.get(key, fallback), f"slides.typography.{key}", 6)
    rules = style["rules"]
    for key in ("images", "tables", "equations"):
        if not isinstance(rules.get(key), dict):
            raise ValueError(f"rules.{key} must be an object")
    images = rules["images"]
    for key in ("max_width_ratio", "max_height_ratio"):
        ratio = _number(images.get(key), f"rules.images.{key}", 0.05)
        if ratio > 1:
            raise ValueError(f"rules.images.{key} cannot exceed 1")
        images[key] = ratio
    images["caption_pt"] = _number(images.get("caption_pt"), "rules.images.caption_pt", 4)
    tables = rules["tables"]
    tables["min_font_pt"] = _number(tables.get("min_font_pt"), "rules.tables.min_font_pt", 5)
    for key in ("zebra", "repeat_header"):
        if not isinstance(tables.get(key), bool):
            raise ValueError(f"rules.tables.{key} must be true or false")
    equation_align = rules["equations"].get("align")
    if not isinstance(equation_align, str) or equation_align not in {"center", "start"}:
        raise ValueError("rules.equations.align must be center or start")
    if style.get("provenance") is None:
        style["provenance"] = copy.deepcopy(DEFAULT_STYLE["provenance"])
    if not isinstance(style["provenance"], dict):
        raise ValueError("provenance must be an object")
    if style.get("design") is not None and not isinstance(style["design"], dict):
        raise ValueError("design must be an object when present")
    for private in [key for key in style if str(key).startswith("_")]:
        style.pop(private, None)
    if "font_assets" in style:
        from .pdf_layouts import normalize_font_assets

        style["font_assets"] = normalize_font_assets(style["font_assets"])
    style.pop("fingerprint", None)
    style["fingerprint"] = json_digest(style)
    return style


def load_builtin(identifier="scholar-ink"):
    key = str(identifier).strip().lower()
    aliases = {
        "scholar": "scholar-ink",
        "scholar ink": "scholar-ink",
        "ink": "scholar-ink",
        "clarity": "clarity-grid",
        "clarity grid": "clarity-grid",
        "grid": "clarity-grid",
    }
    key = aliases.get(key, key)
    if key not in BUILTIN_IDS:
        raise ValueError(
            f"Unknown built-in style {identifier!r}; available: {', '.join(BUILTIN_IDS)}"
        )
    path = STYLE_DIRECTORY / f"{key}.json"
    if not path.is_file():
        raise ValueError(f"Built-in style asset is missing: {path}")
    return normalize_style(read_json(path))


def builtin_styles():
    return [load_builtin(identifier) for identifier in BUILTIN_IDS]


def resolve_style(value=None):
    """Resolve a style dict, a style JSON path, a built-in id, or None."""

    if value is None:
        return load_builtin("scholar-ink")
    if isinstance(value, dict):
        return normalize_style(value)
    if isinstance(value, (str, Path)):
        text = str(value)
        path = Path(text)
        if path.is_file():
            style = normalize_style(read_json(path))
            style["_source_path"] = str(path.resolve())
            return style
        if path.suffix.lower() == ".json":
            raise ValueError(f"Style file not found: {text}")
        return load_builtin(text)
    raise ValueError("Style must be an object, a JSON path, or a built-in style name")


def require_style_direction(style, direction):
    if direction not in style.get("direction_support", []):
        raise ValueError(
            f"Style {style.get('name')!r} does not support {direction} output; choose another style"
        )
    return style


def font_families(style, direction="ltr"):
    """Return font family names for a writing direction."""

    primary = "arabic" if direction == "rtl" else "latin"
    secondary = "latin" if direction == "rtl" else "arabic"
    return {
        "body": [style["fonts"][primary]["family"], *style["fonts"][primary]["fallbacks"]],
        "secondary": [style["fonts"][secondary]["family"], *style["fonts"][secondary]["fallbacks"]],
        "heading": [style["fonts"]["display"]["family"], *style["fonts"]["display"]["fallbacks"]],
        "arabic": [style["fonts"]["arabic"]["family"], *style["fonts"]["arabic"]["fallbacks"]],
        "latin": [style["fonts"]["latin"]["family"], *style["fonts"]["latin"]["fallbacks"]],
        "mono": [style["fonts"]["mono"]["family"], *style["fonts"]["mono"]["fallbacks"]],
    }


def css_stack(families):
    generic = {"serif", "sans-serif", "monospace", "cursive", "system-ui"}
    parts = []
    for family in families:
        cleaned = str(family).strip()
        if not cleaned:
            continue
        parts.append(cleaned if cleaned in generic else f'"{cleaned}"')
    return ", ".join(dict.fromkeys(parts))


# ---------------------------------------------------------------------------
# Shared library paths and names
# ---------------------------------------------------------------------------


def default_library():
    configured = os.environ.get("LECTURE_MATERIALS_LIBRARY")
    if configured:
        return Path(configured).expanduser()
    return PACKAGE_ROOT / "library"


def _reject_link_ancestors(path, stop):
    """Reject symlink/junction components below ``stop`` (Windows-safe)."""

    path = Path(path)
    stop = Path(stop)
    current = path
    candidates = []
    while True:
        candidates.append(current)
        if current == stop or current.parent == current:
            break
        current = current.parent
    for candidate in candidates:
        if not candidate.exists() and not candidate.is_symlink():
            continue
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if candidate.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Shared library paths cannot traverse symlinks or junctions")


def _normalize_kind(kind):
    value = str(kind or "").strip().lower()
    if value in {"visual_style", "visual-style", "style", "visual"}:
        return STYLE_KIND
    if value in {"teaching_profile", "teaching-profile", "profile", "teaching"}:
        return PROFILE_KIND
    raise ValueError("kind must be visual_style or teaching_profile")


def validate_name(name):
    if not isinstance(name, str):
        raise ValueError("A profile/style name must be a string")
    value = unicodedata.normalize("NFC", name).strip()
    if not value:
        raise ValueError("A profile/style name cannot be empty")
    if len(value) > 80:
        raise ValueError("A profile/style name cannot exceed 80 characters")
    if any(ord(character) < 32 for character in value):
        raise ValueError("A profile/style name cannot contain control characters")
    if value in {".", ".."} or ".." in value:
        raise ValueError("A profile/style name cannot contain traversal sequences")
    if any(separator in value for separator in ("/", "\\", ":", "*", "?", '"', "<", ">", "|")):
        raise ValueError("A profile/style name cannot contain path separators or reserved characters")
    if Path(value).is_absolute() or re.match(r"^[A-Za-z]:", value):
        raise ValueError("A profile/style name cannot be an absolute path")
    if value.endswith((".", " ")):
        raise ValueError("A profile/style name cannot end with a dot or space")
    if value.split(".")[0].upper() in WINDOWS_RESERVED:
        raise ValueError("A profile/style name cannot use a reserved device name")
    if not NAME_PATTERN.fullmatch(value):
        raise ValueError(
            "A profile/style name may contain letters, numbers, spaces, dots, hyphens, underscores, "
            "parentheses, ampersands, and apostrophes"
        )
    return value


def slugify_name(name):
    value = validate_name(name).casefold()
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[\s._]+", "-", value)
    value = re.sub(r"[^\w\u0600-\u06FF\-]", "", value, flags=re.UNICODE)
    value = re.sub(r"-{2,}", "-", value, flags=re.UNICODE).strip("-")
    if not value:
        raise ValueError("The name produces an empty identifier")
    if len(value) > 80:
        raise ValueError("The name produces an identifier longer than 80 characters")
    if value in WINDOWS_RESERVED:
        raise ValueError("The name produces a reserved device identifier")
    return value


def _kind_directory(library, kind):
    library = Path(library).expanduser()
    directory = library / KIND_DIRECTORIES[kind]
    if directory.exists():
        _reject_link_ancestors(directory, library)
    return directory


def _version_tuple(version):
    parts = re.findall(r"\d+", str(version))
    return tuple(int(part) for part in parts) or (0,)


def _find_versions(directory, slug):
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob(f"{glob_escape(slug)}-v*.json"):
        match = re.fullmatch(rf"{glob_escape(slug)}-v(.+)\.json", path.name)
        if match:
            found.append((match.group(1), path))
    found.sort(key=lambda item: _version_tuple(item[0]))
    return found


def glob_escape(text):
    return re.sub(r"([*?\[\]])", r"[\1]", str(text))


def _update_index(directory, kind, slug, record):
    index_path = directory / "index.json"
    index = {"schema_version": SCHEMA_VERSION, "kind": kind, "entries": {}}
    if index_path.exists():
        existing = read_json(index_path)
        if isinstance(existing, dict) and isinstance(existing.get("entries"), dict):
            index = existing
    entry = index["entries"].setdefault(slug, {"name": record["name"], "versions": []})
    entry["name"] = record["name"]
    versions = [item for item in entry.get("versions", []) if item.get("version") != record["version"]]
    versions.append({"version": record["version"], "file": record["file"],
                     "created_at": record["created_at"]})
    versions.sort(key=lambda item: _version_tuple(item["version"]))
    entry["versions"] = versions
    write_json(index_path, index)


# ---------------------------------------------------------------------------
# Saving and loading named styles/profiles
# ---------------------------------------------------------------------------


def _validate_profile(profile):
    if not isinstance(profile, dict) or not profile:
        raise ValueError("A teaching profile needs a preferences object")
    unknown = sorted(set(profile) - PROFILE_FIELDS)
    if unknown:
        raise ValueError(
            "Teaching profiles store preferences, not personal source text; unsupported fields: "
            + ", ".join(unknown[:20])
        )
    cleaned = {}
    for key, value in profile.items():
        if isinstance(value, str):
            if len(value) > 1000:
                raise ValueError(f"teaching profile field {key} is too long; store a preference, not example text")
            cleaned[key] = value.strip()
        elif isinstance(value, list) and all(isinstance(item, str) and len(item) <= 300 for item in value):
            cleaned[key] = [item.strip() for item in value]
        else:
            raise ValueError(f"teaching profile field {key} must be text or a list of short texts")
    return cleaned


def save_named_profile(library, request):
    """Save a named, versioned visual style or teaching profile.

    ``request`` keys:
      kind: ``visual_style`` or ``teaching_profile``
      name: user-selected display name
      user_agreed: must be true (the host must have obtained agreement)
      style/document: style object for visual styles
      profile/preferences: preference fields for teaching profiles
      collision: ``new_version`` or ``rename``; omitted means reject a collision
      new_name: replacement name when ``collision`` is ``rename``
      version: optional initial version (default ``1.0.0``)

    A collision never overwrites.  Unknown/custom teaching fields are rejected so
    personal example prose is not silently persisted.
    """

    if not isinstance(request, dict):
        raise ValueError("A save request must be an object")
    kind_value = request.get("kind")
    if kind_value is not None and not isinstance(kind_value, str):
        raise ValueError("kind must be visual_style or teaching_profile")
    if not kind_value:
        if any(key in request for key in ("fonts", "palette", "page", "slides")):
            request = dict(request)
            request["kind"] = STYLE_KIND
        elif "profile" in request or "preferences" in request:
            request = dict(request)
            request["kind"] = PROFILE_KIND
    if request.get("kind") in {STYLE_KIND, "style", "visual", "visual-style"} \
            and "style" not in request and "document" not in request \
            and any(key in request for key in ("fonts", "palette", "page", "slides")):
        document_style = {
            key: request[key] for key in
            ("schema_version", "id", "name", "version", "direction_support", "languages",
             "description", "fonts", "font_assets", "palette", "page", "slides", "layout", "rules", "provenance")
            if key in request
        }
        document_style["kind"] = STYLE_KIND
        request = dict(request)
        request["style"] = document_style
    kind = _normalize_kind(request.get("kind"))
    agreed = request.get("user_agreed")
    if agreed is None and "agreed" in request:
        agreed = request.get("agreed")
    if kind == PROFILE_KIND and agreed is not True:
        raise ValueError("Save a teaching profile only after the user has explicitly agreed")
    if agreed is False:
        raise ValueError("Do not save a style/profile when the user declined")
    name = validate_name(request.get("name"))
    library_path = Path(library).expanduser()
    try:
        library_path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ValueError(f"Cannot create the shared library: {error}") from error
    directory = _kind_directory(library_path, kind)
    directory.mkdir(parents=True, exist_ok=True)
    _reject_link_ancestors(directory, library_path)

    collision_note = None
    slug = slugify_name(name)
    versions = _find_versions(directory, slug)
    version = request.get("version", "1.0.0")
    if not isinstance(version, str) or not re.fullmatch(r"\d+(\.\d+){0,3}", version):
        raise ValueError("version must look like 1 or 1.0.0")
    if versions:
        action = request.get("collision") or request.get("on_collision")
        if action is not None and not isinstance(action, str):
            raise ValueError("collision must be new_version or rename")
        if action in {"new_version", "version", "bump"}:
            highest = max(_version_tuple(item[0]) for item in versions)
            major = (highest[0] if highest else 0) + 1
            version = f"{major}.0.0"
            collision_note = "created a new version; the previous version was preserved"
        elif action in {"rename", "new_name"}:
            replacement = request.get("new_name") or request.get("rename_to")
            if not replacement:
                raise ValueError("Choose a new name for the duplicated style/profile")
            if slugify_name(replacement) == slug:
                raise ValueError("The replacement name is the same as the existing name")
            nested = dict(request)
            nested.update(name=replacement, collision=None, on_collision=None,
                          new_name=None, rename_to=None, version="1.0.0")
            return save_named_profile(library_path, nested)
        elif action == "overwrite":
            raise ValueError("Silent overwrite is not allowed; choose a new name or a new version")
        else:
            raise ValueError(
                f"A saved {kind} named {name!r} already exists; choose a new name or a new version"
            )
    target = directory / f"{slug}-v{version}.json"
    if target.exists():
        raise ValueError("That style/profile version already exists; choose a new version")
    if not target.resolve().is_relative_to(directory.resolve()):
        raise ValueError("The profile path escaped the shared library")

    created_at = datetime.now(timezone.utc).isoformat()
    if kind == STYLE_KIND:
        style_document = request.get("style", request.get("document"))
        style_source_hint = request.get("style_source_path") or request.get("style_path")
        if not style_source_hint and isinstance(style_document, dict):
            style_source_hint = style_document.get("_source_path")
        style = normalize_style(style_document)
        style.update(id=slug, name=name, version=version, builtin=False)
        provenance = dict(style.get("provenance") or {})
        provenance.update({
            "source": "saved",
            "saved_at": created_at,
            "saved_from": request.get("reference_name") or provenance.get("source"),
            "reference_sha256": request.get("reference_sha256") or provenance.get("reference_sha256"),
            "preview": request.get("preview") or provenance.get("preview"),
        })
        style["provenance"] = provenance
        try:
            from . import design as design_module

            from .pdf_layouts import freeze_font_assets

            style = freeze_font_assets(style, style_source_hint, target)
            style = design_module.freeze_style_assets(
                style,
                style_path=style_source_hint,
                library_style_path=target,
            )
        except ValueError:
            raise
        except Exception as error:
            raise ValueError(f"The style's reference design assets could not be frozen safely: {error}") from error
        style.pop("fingerprint", None)
        style["fingerprint"] = json_digest(style)
        document = style
    else:
        profile = request.get("profile", request.get("preferences"))
        document = {
            "schema_version": SCHEMA_VERSION,
            "kind": PROFILE_KIND,
            "id": slug,
            "name": name,
            "version": version,
            "preferences": _validate_profile(profile),
            "agreed": True,
            "agreed_at": created_at,
            "created_at": created_at,
            "provenance": {
                "source": "saved",
                "note": "Preference fields only; personal example text is not retained.",
            },
        }
    write_json(target, document)
    record = {"name": name, "version": version, "file": target.name, "created_at": created_at}
    _update_index(directory, kind, slug, record)
    return {
        "path": str(target),
        "id": slug,
        "name": name,
        "kind": kind,
        "version": version,
        "created_at": created_at,
        "collision_note": collision_note,
        "document": document,
        "style" if kind == STYLE_KIND else "profile": document,
    }


def load_named_profile(library, kind, name_or_id, version=None):
    """Load the newest (or an explicit version of a) saved style/profile."""

    kind = _normalize_kind(kind)
    directory = _kind_directory(Path(library).expanduser(), kind)
    slug = slugify_name(str(name_or_id))
    versions = _find_versions(directory, slug)
    if not versions:
        raise ValueError(f"No saved {kind} named {name_or_id!r} was found")
    if version is None:
        selected = versions[-1]
    else:
        match = [item for item in versions if item[0] == str(version)]
        if not match:
            raise ValueError(f"{kind} {name_or_id!r} has no version {version}")
        selected = match[0]
    path = selected[1]
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Saved profile path escaped the shared library")
    document = read_json(path)
    if kind == STYLE_KIND:
        document["_source_path"] = str(path.resolve())
    return {
        "path": str(path),
        "kind": kind,
        "id": slug,
        "name": document.get("name"),
        "version": document.get("version"),
        "document": document,
    }


def list_named_profiles(library, kind):
    kind = _normalize_kind(kind)
    directory = _kind_directory(Path(library).expanduser(), kind)
    index_path = directory / "index.json"
    if index_path.exists():
        index = read_json(index_path)
        if isinstance(index, dict) and isinstance(index.get("entries"), dict):
            return [
                {"id": slug, "name": entry.get("name"),
                 "versions": [item.get("version") for item in entry.get("versions", [])]}
                for slug, entry in sorted(index["entries"].items())
            ]
    return [
        {"id": path.name.rsplit("-v", 1)[0], "name": read_json(path).get("name"),
         "versions": [path.name.rsplit("-v", 1)[1][:-5]]}
        for path in sorted(directory.glob("*-v*.json"))
    ]


# ---------------------------------------------------------------------------
# Reference inspection
# ---------------------------------------------------------------------------


def _relative_or_text(path):
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _safe_reference_name(path):
    stem = unicodedata.normalize("NFC", path.stem).strip() or "Reference Style"
    stem = re.sub(r"[^\w\u0600-\u06FF ._()&'’\-]", " ", stem)
    stem = re.sub(r"\s{2,}", " ", stem).strip().lstrip("._- ")
    if not stem:
        stem = "Reference Style"
    return stem[:80]


def _design_workspace(destination):
    base = Path(destination).with_suffix("")
    return base.parent / f"{base.name}-design"


def _style_from_reference(name, reference, original_path, design_document, analysis, conversion=None):
    """Build style tokens from the design representation returned by ``design``."""

    palette = design_document.get("palette") or DEFAULT_STYLE["palette"]
    fonts = design_document.get("fonts") or DEFAULT_STYLE["fonts"]
    page = design_document.get("page_size_mm") or {}
    margins = design_document.get("margins_mm") or {}
    typography = design_document.get("typography") or {}
    direction_support = design_document.get("direction_support") or ["ltr", "rtl"]
    slides = design_document.get("slides") or {}

    page_settings = {
        "width_mm": page.get("width", DEFAULT_STYLE["page"]["width_mm"]),
        "height_mm": page.get("height", DEFAULT_STYLE["page"]["height_mm"]),
        "margin_mm": {
            side: margins.get(side, DEFAULT_STYLE["page"]["margin_mm"][side])
            for side in ("top", "right", "bottom", "left")
        },
        "line_height": design_document.get("line_height", DEFAULT_STYLE["page"]["line_height"]),
        "typography": {
            key: typography.get(key, fallback)
            for key, fallback in DEFAULT_STYLE["page"]["typography"].items()
        },
    }
    slide_settings = {
        "width_in": slides.get("width_in", design_document.get("slide_width_in", DEFAULT_STYLE["slides"]["width_in"])),
        "height_in": slides.get("height_in", design_document.get("slide_height_in", DEFAULT_STYLE["slides"]["height_in"])),
        "margin_in": slides.get("margin_in", (design_document.get("geometry") or {}).get("margin_in", DEFAULT_STYLE["slides"]["margin_in"])),
        "title_bar_in": slides.get("title_bar_in", (design_document.get("geometry") or {}).get("title_bar_in", DEFAULT_STYLE["slides"]["title_bar_in"])),
        "typography": {
            key: (slides.get("typography") or design_document.get("typography") or {}).get(key, fallback)
            for key, fallback in DEFAULT_STYLE["slides"]["typography"].items()
        },
    }
    table = design_document.get("table") or {}
    families = []
    if design_document.get("source_format") == "pptx":
        for layout in design_document.get("layouts") or []:
            if layout.get("name"):
                families.append(layout["name"])
    # A slide reference still supplies a visual identity for PDF output: scale
    # its observed type hierarchy into the print page rather than ignoring it.
    if design_document.get("source_format") == "pptx":
        slide_type = slide_settings["typography"]
        slide_width_pt = max(1.0, float(slide_settings["width_in"]) * 72.0)
        ratio = max(0.35, min(1.2, float(page_settings["width_mm"]) * 72 / 25.4 / slide_width_pt))
        body_pt = max(9.5, min(13.0, float(slide_type["body_pt"]) * ratio * 1.05))
        title_pt = max(20.0, min(34.0, float(slide_type["title_pt"]) * ratio))
        h1_pt = max(14.0, min(24.0, float(slide_type["section_pt"]) * ratio))
        page_settings["typography"].update({
            "title_pt": round(title_pt, 1),
            "subtitle_pt": round(max(body_pt * 1.15, 11.0), 1),
            "h1_pt": round(h1_pt, 1),
            "h2_pt": round(max(h1_pt * 0.78, 11.0), 1),
            "body_pt": round(body_pt, 1),
            "small_pt": round(max(body_pt * 0.82, 7.5), 1),
            "caption_pt": round(max(body_pt * 0.8, 7.5), 1),
            "table_pt": round(max(body_pt * 0.88, 8.0), 1),
            "code_pt": round(max(body_pt * 0.88, 8.0), 1),
        })
        margin_in = float(slide_settings.get("margin_in") or 0.55)
        slide_width_in = max(1.0, float(slide_settings["width_in"]))
        print_margin = max(12.0, min(28.0, margin_in / slide_width_in * page_settings["width_mm"]))
        page_settings["margin_mm"] = {
            "top": round(print_margin + 3.0, 1),
            "right": round(print_margin, 1),
            "bottom": round(print_margin + 3.0, 1),
            "left": round(print_margin, 1),
        }
    style = normalize_style({
        "schema_version": SCHEMA_VERSION,
        "kind": STYLE_KIND,
        "id": slugify_name(name),
        "name": name,
        "version": "1.0.0",
        "builtin": False,
        "direction_support": direction_support,
        "languages": ["en", "ar", "mixed"] if direction_support == ["ltr", "rtl"] else ["en"],
        "description": (
            "Style proposed from an inspected reference. Design tokens are accompanied by a "
            "structured support report and rendered reference previews; the proposal is not "
            "automatically named or approved."
        ),
        "fonts": fonts,
        "palette": palette,
        "page": page_settings,
        "slides": slide_settings,
        "layout": {
            "cover": "reference" if design_document.get("source_format") == "pptx" else "measured",
            "section": "reference" if design_document.get("source_format") == "pptx" else "measured",
            "image_alignment": "end",
            "toc_columns": 2,
            "rule_under_titles": True,
            "families": families[:12] or DEFAULT_STYLE["layout"]["families"],
        },
        "rules": {
            "images": copy.deepcopy(DEFAULT_STYLE["rules"]["images"]),
            "tables": {
                "zebra": bool(table.get("band_row", True)),
                "repeat_header": True,
                "min_font_pt": max(7, min(9, float(table.get("font_size_pt") or 8))),
            },
            "equations": copy.deepcopy(DEFAULT_STYLE["rules"]["equations"]),
        },
        "design": design_document,
        "provenance": {
            "source": "reference",
            "method": design_document.get("method", "measured"),
            "reference_sha256": design_document.get("reference", {}).get("sha256"),
            "reference_name": original_path.name,
            "reference_path": _relative_or_text(original_path),
            "reference_format": design_document.get("source_format"),
            "conversion": conversion,
            "pages_inspected": analysis.get("pages_inspected"),
            "slides": analysis.get("slide_count"),
            "preview": None,
            "notes": (
                "The reference was opened read-only and hashed before and after inspection. "
                "Rendered previews and the design report belong to this job; the shared style "
                "library stores design data and a sanitized native template, not reference screenshots."
            ),
        },
    })
    return style


def extract_style(reference_path, destination, overwrite=False):
    """Inspect a design reference and write a proposed style plus previews/report.

    The reference is opened read-only and hashed before and after inspection.
    The proposal is not a promise of pixel-identical translation.  A legacy
    binary ``.ppt`` is converted through a documented local read-only route (or
    an actionable conversion error is returned); renaming is never accepted as
    conversion.  The user must choose a name before saving the style.
    """

    from . import design as design_module

    reference = Path(reference_path)
    if not reference.is_file():
        raise ValueError(f"Style reference not found: {reference}")

    destination = Path(destination)
    if destination.exists() and destination.is_dir():
        destination = destination / f"{reference.stem}-proposed-style.json"
    if destination.suffix.lower() != ".json":
        destination = destination.with_suffix(".json")
    if destination.resolve() == reference.resolve():
        raise ValueError("The style proposal cannot overwrite the original reference")
    if destination.exists() and not overwrite:
        raise ValueError(f"Destination already exists; choose a new path or pass overwrite=True: {destination}")

    digest_before = file_digest(reference)
    reference_name = _safe_reference_name(reference)
    reference_format = design_module.detect_format(reference)
    workspace = _design_workspace(destination)
    workspace.mkdir(parents=True, exist_ok=True)
    conversion = None
    inspection_path = reference
    if reference_format == "ppt":
        converted = workspace / "converted-from-ppt.pptx"
        conversion = design_module.convert_legacy_ppt(reference, converted, original_sha256=digest_before)
        inspection_path = converted
        reference_format = "pptx"

    if reference_format == "pdf":
        design_document, analysis, support, previews = design_module.extract_pdf_design(
            inspection_path, reference_name, workspace
        )
    elif reference_format == "pptx":
        design_document, analysis, support, previews = design_module.extract_pptx_design(
            inspection_path, reference_name, workspace
        )
    else:
        design_document, analysis, support, previews = design_module.extract_raster_design(
            inspection_path, reference_name, workspace
        )
    if file_digest(reference) != digest_before:
        raise ValueError("The reference changed while it was inspected; use an intact copy")

    # Preserve the original identity even when a legacy file was converted.
    original_identity = {
        "name": reference.name,
        "path": str(reference.resolve()),
        "sha256": digest_before,
        "bytes": reference.stat().st_size,
        "format": "ppt" if conversion else design_document.get("source_format"),
    }
    design_document["reference"].update({
        "original_name": reference.name,
        "original_path": str(reference.resolve()),
        "original_sha256": digest_before,
        "original_format": "ppt" if conversion else design_document.get("source_format"),
        "conversion": conversion,
    })
    design_document["identity"] = dict(design_document.get("identity") or {})
    design_document["identity"].update({
        "mode": "frozen" if reference_format == "pptx" else "self-contained",
        "source_path": str(reference.resolve()),
        "source_sha256": digest_before,
    })
    if isinstance(design_document.get("template"), dict):
        design_document["template"]["source_path"] = str(reference.resolve())
        design_document["template"]["source_sha256"] = digest_before
    design_document["original"] = original_identity

    style = _style_from_reference(
        reference_name, reference, reference, design_document, analysis, conversion=conversion
    )
    style["provenance"]["reference_sha256"] = original_identity["sha256"]
    style["provenance"]["original_reference_sha256"] = original_identity["sha256"]
    style["provenance"]["inspection_sha256"] = design_document.get("reference", {}).get("sha256")
    style["provenance"]["reference_format"] = original_identity["format"]
    report_path = workspace / "design-report.json"
    design_document["report_path"] = str(report_path.resolve())
    style["design"] = design_document
    style.pop("fingerprint", None)
    style["fingerprint"] = json_digest(style)

    preview_list = list(previews.get("paths", []))
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "reference_design_report",
        "reference": original_identity,
        "method": design_document.get("method"),
        "inspection_path": str(Path(inspection_path).resolve()),
        "conversion": conversion,
        "previews": previews,
        "reference_previews": preview_list,
        "supported": support.get("supported", []),
        "preserved": support.get("preserved", []),
        "approximated": support.get("approximated", []),
        "unsupported": support.get("unsupported", []),
        "ambiguous": support.get("ambiguous", []),
        "excluded": support.get("excluded", []),
        "analysis": analysis,
        "design": design_document,
        "style_path": str(destination.resolve()),
        "style_fingerprint": style["fingerprint"],
        "rendered_reference_required": True,
        "comparison_required": "Render a representative new-content preview and compare it side by side before acceptance.",
        "limitations": [
            "Reference fonts are observed names; availability and licensing were not verified.",
            "Colour values are read or sampled and may differ in another renderer.",
            "A PDF reference cannot be promised as a pixel-identical template in another renderer.",
            "Flattened references permit estimates, not structural text/vector recovery.",
            "Rendered reference pages/slides are inspection artifacts and are never placed behind new prose.",
        ],
    }
    write_json(report_path, report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_json(destination, style)

    # If the proposal lives inside a managed job, register its generated
    # artifacts so GLM's cleanup owns them.  Unregistered working files would
    # otherwise be missed by an explicit generated-file cleanup.
    try:
        from .jobs import register_generated

        for parent in [destination.parent, *destination.parent.parents]:
            if (parent / "job.json").is_file():
                for generated in sorted(workspace.rglob("*")):
                    if generated.is_file():
                        try:
                            register_generated(parent, generated)
                        except (ValueError, KeyError, OSError):
                            pass
                for generated in (destination, report_path):
                    try:
                        register_generated(parent, generated)
                    except (ValueError, KeyError, OSError):
                        pass
                break
    except Exception:
        pass

    unsupported_strings = [
        f"{item.get('property')}: {item.get('detail')}" for item in support.get("unsupported", [])
    ]
    observed_differences = [
        f"{item.get('property')}: {item.get('detail')}" for item in support.get("approximated", [])
    ] + unsupported_strings + [
        "Rendered previews and a side-by-side new-content comparison must still be inspected at full size.",
    ]
    return {
        "proposal_path": str(destination),
        "reference_path": str(reference.resolve()),
        "reference_sha256": digest_before,
        "reference_hash": digest_before,
        "reference_format": original_identity["format"],
        "source_preserved": True,
        "style": style,
        "proposed_style": style,
        "tokens": style,
        "design": design_document,
        "design_report_path": str(report_path),
        "design_directory": str(workspace),
        "reference_previews": preview_list,
        "rendered_reference_previews": previews.get("paths", []),
        "preview_renderer": previews.get("renderer"),
        "supported": support.get("supported", []),
        "preserved": support.get("preserved", []),
        "approximated": support.get("approximated", []),
        "unsupported": unsupported_strings,
        "ambiguous": support.get("ambiguous", []),
        "excluded": support.get("excluded", []),
        "support": support,
        "analysis": analysis,
        "conversion": conversion,
        "observed_differences": observed_differences,
        "differences": observed_differences,
        "preview_required": True,
        "next_steps": [
            "Inspect the rendered reference pages/slides and the structured design report at full size.",
            "Ask the user for the style name before saving it to the shared library.",
            "Render a representative preview with real lecture content and compare it side by side.",
            "Report missing fonts or approximated/unsupported properties before claiming a reusable style.",
        ],
    }


def style_schema():
    """Return a small JSON-schema-style description for documentation/tools."""

    return {
        "schema_version": SCHEMA_VERSION,
        "required": ["schema_version", "kind", "id", "name", "version", "fonts", "palette",
                     "page", "slides", "rules"],
        "optional": ["design", "font_assets", "provenance", "layout", "direction_support", "languages",
                     "description", "fingerprint"],
        "design": {
            "kind": "reference_design",
            "fields": ["reference", "identity", "previews", "support", "report_path"],
            "pptx": ["template", "theme", "layouts", "roles", "background", "typography",
                     "table", "geometry", "recurring", "palette", "fonts"],
            "pdf": ["page_size_mm", "margins_mm", "palette", "fonts", "typography", "line_height",
                    "body", "headings", "cover", "header", "footer", "vector_decor", "table",
                    "slides"],
        },
        "kinds": [STYLE_KIND, PROFILE_KIND],
        "notes": "JSON data only; implementations must not execute style content.",
    }


def design_summary(style, style_path=None):
    """Return the reference-design identity/support report for a style, if any."""

    from . import design as design_module

    if isinstance(style, (str, Path)):
        style_path = style_path or str(style)
        style = resolve_style(style)
    if isinstance(style, dict) and style_path is None:
        style_path = style.get("_source_path")
    return design_module.design_summary(normalize_style(style), style_path=style_path)


def reference_previews(style, style_path=None, kind="pdf"):
    """Resolve rendered reference preview image paths for a style (may be empty)."""

    from . import design as design_module

    if isinstance(style, (str, Path)):
        style_path = style_path or str(style)
        style = resolve_style(style)
    if isinstance(style, dict) and style_path is None:
        style_path = style.get("_source_path")
    return design_module.reference_preview_paths(normalize_style(style), style_path=style_path, kind=kind)


def resolve_design_template(style, style_path=None):
    """Verify and return the frozen native PPTX template for a reference style."""

    from . import design as design_module

    if isinstance(style, (str, Path)):
        style_path = style_path or str(style)
        style = resolve_style(style)
    if isinstance(style, dict) and style_path is None:
        style_path = style.get("_source_path")
    return design_module.design_template_path(normalize_style(style), style_path=style_path)


def verify_style_design(style, style_path=None):
    """Verify frozen reference assets and report a changed/missing source."""

    from . import design as design_module

    if isinstance(style, (str, Path)):
        style_path = style_path or str(style)
        style = resolve_style(style)
    if isinstance(style, dict) and style_path is None:
        style_path = style.get("_source_path")
    return design_module.verify_style_design(normalize_style(style), style_path=style_path)
