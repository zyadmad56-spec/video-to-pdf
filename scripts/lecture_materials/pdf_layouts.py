"""Flowing PDF layouts and verified, portable local font assets."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import re


def _reject_links(path, directory):
    if not path.is_relative_to(directory):
        raise ValueError("Font asset path escaped its directory")
    while path != directory:
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            attributes = 0
        if path.is_symlink() or attributes & 0x400:
            raise ValueError("Font asset paths must not traverse links or junctions")
        path = path.parent


def normalize_font_assets(assets):
    if not isinstance(assets, list):
        raise ValueError("font_assets must be an array")
    families = set()
    for asset in assets:
        family = _font_asset_family(asset)
        if family in families:
            raise ValueError("Font asset family names must be unique")
        families.add(family)
    return [dict(asset) for asset in assets]


def _font_asset_family(asset):
    if not isinstance(asset, dict) or set(asset) != {"family", "path", "sha256"}:
        raise ValueError("Each font asset needs exactly family, path and sha256")
    family = asset["family"]
    if not isinstance(family, str) or not re.fullmatch(r"[\w -]{1,80}", family):
        raise ValueError("Font asset family must be a plain font-family name")
    _font_relative_path(asset["path"])
    if not isinstance(asset["sha256"], str) or not re.fullmatch(r"[a-fA-F0-9]{64}", asset["sha256"]):
        raise ValueError("Font asset sha256 must contain 64 hexadecimal characters")
    return family


def _font_relative_path(filename):
    if not isinstance(filename, str) or not filename:
        raise ValueError("Font asset path must be a relative .ttf path")
    path = Path(filename)
    if path.root or path.drive or ":" in filename or ".." in path.parts or path.suffix.lower() != ".ttf":
        raise ValueError("Font asset path must stay under the style directory and end in .ttf")
    return path


def _font_path(asset, style_path):
    if not style_path or not Path(style_path).is_file():
        raise ValueError("Font assets require the existing style JSON source path")
    directory = Path(style_path).resolve().parent
    candidate = directory / _font_relative_path(asset["path"])
    if not candidate.resolve().is_relative_to(directory):
        raise ValueError("Font asset path escaped the style directory")
    _reject_links(candidate, directory)
    if not candidate.is_file():
        raise ValueError(f"Font asset is missing: {asset['path']}")
    return candidate


def _font_bytes(asset, style_path):
    import pymupdf

    path = _font_path(asset, style_path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Font assets must not exceed 16 MiB")
    font_bytes = path.read_bytes()
    if font_bytes[:4] not in {b"\x00\x01\x00\x00", b"true"}:
        raise ValueError("Font asset is not a supported TrueType file")
    if hashlib.sha256(font_bytes).hexdigest() != asset["sha256"].lower():
        raise ValueError(f"Font asset hash changed: {asset['path']}")
    try:
        pymupdf.Font(fontbuffer=font_bytes)
    except pymupdf.mupdf.FzErrorBase as error:
        raise ValueError("Font asset cannot be decoded as a TrueType font") from error
    return font_bytes


def font_face_css(style):
    rules = []
    for asset in normalize_font_assets(style.get("font_assets", [])):
        encoded = base64.b64encode(_font_bytes(asset, style.get("_source_path"))).decode("ascii")
        rules.append(
            f"@font-face{{font-family:'{asset['family']}';"
            f"src:url(data:font/ttf;base64,{encoded}) format('truetype');"
            "font-weight:400;font-style:normal;}"
        )
    return "\n".join(rules)


def freeze_font_assets(style, source_path, target_path):
    assets = normalize_font_assets(style.get("font_assets", []))
    verified = [(asset, _font_bytes(asset, source_path)) for asset in assets]
    target_path = Path(target_path).resolve()
    directory = target_path.parent / "assets" / target_path.stem
    if not verified:
        return style
    _reject_links(directory, target_path.parent.resolve())
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(target_path.parent.resolve()):
        raise ValueError("Shared font directory escaped the library")
    frozen = []
    for asset, font_bytes in verified:
        destination = directory / (asset["sha256"].lower() + ".ttf")
        _store_font(destination, font_bytes, target_path.parent)
        frozen.append(dict(asset, path=destination.relative_to(target_path.parent).as_posix()))
    return dict(style, font_assets=frozen)


def _store_font(destination, font_bytes, library_root):
    _reject_links(destination, library_root)
    if destination.exists() and destination.read_bytes() != font_bytes:
        raise ValueError("An existing shared font asset has changed")
    if not destination.exists():
        destination.write_bytes(font_bytes)


def layout_css(style):
    layout = style.get("layout", {})
    rules = []
    if layout.get("cover") == "panel":
        rules.append("""
        .cover { background:#F2F2F2; padding:28pt; }
        .cover h1 { color:#C55C1A; font-weight:400; margin:0; }
        .cover .subtitle { color:#002060; }
        .cover > * { background:white; padding:12pt 20pt; margin:0; width:54%; }
        .cover .kicker { font-size:12pt; }
        .cover .meta { margin:0; font-size:10pt; padding:8pt 20pt; }
        .cover .rule { margin:0; padding:0; height:6pt; width:54%; }
        .cover > :last-child { border-bottom:6pt solid #1973EB; }
        """)
    if layout.get("section") == "side-cards":
        rules.append("""
        .chapter { clear:both; padding-top:16pt; border-top:6pt solid #1973EB; }
        .chapter-heading { float:inline-start; width:29%; }
        .chapter-heading h1 { font-size:36pt; font-weight:400; line-height:1.2;
                             overflow-wrap:anywhere; margin:0 0 16pt; }
        .chapter-body { margin-inline-start:34%; font-size:15pt; }
        .chapter-body > .block { background:#F2F2F2; border-radius:13pt;
                                padding:12pt 18pt; margin-bottom:14pt; break-inside:avoid; }
        .chapter-body .equation { font-size:18pt; }
        .chapter-body .equation-note { font-size:11pt; }
        .chapter-body p:last-child { margin-bottom:0; }
        .chapter-body .section-summary { margin:0 0 16pt; }
        body.preview .chapter { break-before:page; }
        body.preview .preview-banner { font-size:11pt; padding:6pt 12pt; break-after:avoid; }
        body.preview .preview-banner + .chapter { break-before:auto; }
        .appendix h1 { font-size:26pt; }
        .appendix h2 { font-size:20pt; }
        .appendix table, .appendix .meta { font-size:11pt; }
        """)
    return "\n".join(rules)


def chapter_html(heading, body, layout):
    if layout.get("section") == "side-cards":
        return f'<div class="chapter-heading">{heading}</div><div class="chapter-body">{body}</div>'
    return heading + body


def footer_rule(style):
    if style.get("layout", {}).get("section") != "side-cards":
        return None
    page = style["page"]
    margins = page["margin_mm"]
    top = page["height_mm"] - margins["bottom"] + 2
    return dict(kind="line", stroke="#1973EB", stroke_width_mm=2.117,
                rect_mm=dict(left=margins["left"], right=page["width_mm"] - margins["right"],
                             top=top, bottom=top))
