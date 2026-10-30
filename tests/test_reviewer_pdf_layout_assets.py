"""Independent layout/font boundary checks after the local implementation."""
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lecture_materials import jobs, pdf_layouts, render_pdf, styles


class PdfLayoutAssetCase(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name).resolve()
        self.style_path = self.folder / "style.json"
        self.font_path = self.folder / "font.ttf"
        self.font_path.write_bytes(b"\x00\x01\x00\x00truncated font")
        self.style = styles.load_builtin("scholar-ink")
        self.style["font_assets"] = [{"family": "Test Font", "path": "font.ttf",
                                      "sha256": jobs.file_digest(self.font_path)}]
        jobs.write_json(self.style_path, self.style)

    def test_font_manifest_survives_normalization_and_changes_style_fingerprint(self):
        normalized = styles.normalize_style(self.style)
        self.assertEqual(normalized["font_assets"], self.style["font_assets"])
        without_font = copy.deepcopy(self.style)
        without_font.pop("font_assets")
        self.assertNotEqual(normalized["fingerprint"], styles.normalize_style(without_font)["fingerprint"])

    def test_malformed_font_manifests_are_rejected(self):
        original = self.style["font_assets"][0]
        cases = [None, {}, [dict(original, family="x</style>")],
                 [dict(original, path="../font.ttf")], [dict(original, path="https://site/font.ttf")],
                 [dict(original, path="font.otf")], [dict(original, sha256="not a hash")],
                 [original, original], [dict(original, extra="unexpected")]]
        for manifest in cases:
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                styles.normalize_style(dict(self.style, font_assets=manifest))

    def test_missing_changed_truncated_and_oversized_font_files_fail_explicitly(self):
        self.style["_source_path"] = str(self.style_path)
        # Each case fails at a different real file boundary, without mocking validation.
        for payload in (b"missing", b"changed", b"\x00\x01\x00\x00truncated font", b"0" * (16 * 1024 * 1024 + 1)):
            with self.subTest(case=len(payload)):
                if payload == b"missing":
                    self.font_path.unlink(missing_ok=True)
                else:
                    self.font_path.write_bytes(payload)
                if payload != b"changed":
                    self.style["font_assets"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
                with self.assertRaises(ValueError):
                    pdf_layouts.font_face_css(self.style)

    def test_asset_bearing_in_memory_style_requires_its_source_path(self):
        with self.assertRaisesRegex(ValueError, "source path"):
            pdf_layouts.font_face_css(self.style)

    def test_invalid_asset_save_does_not_publish_a_library_profile(self):
        library = self.folder / "library"
        with self.assertRaises(ValueError):
            styles.save_named_profile(library, {"kind": "visual_style", "name": "Test Fonts",
                                      "style": self.style, "style_source_path": str(self.style_path),
                                      "user_agreed": True})
        self.assertEqual(list(library.rglob("*.json")), [])

    def test_builtin_layout_and_empty_assets_keep_the_existing_document_structure(self):
        for identifier in ("scholar-ink", "clarity-grid"):
            with self.subTest(identifier=identifier):
                style = styles.load_builtin(identifier)
                self.assertEqual(pdf_layouts.layout_css(style), "")
                self.assertEqual(pdf_layouts.font_face_css(style), "")
                self.assertEqual(pdf_layouts.chapter_html("heading", "body", style["layout"]), "headingbody")

    def test_side_cards_wrap_real_blocks_and_preserve_escaped_prose(self):
        style = styles.load_builtin("clarity-grid")
        style["layout"].update(section="side-cards", cover="panel")
        content = {"title": "AVL", "language": "en", "direction": "ltr", "sections": [
            {"id": "balance", "title": "Balance", "blocks": [
                {"type": "paragraph", "text": "Keep <script> as text & preserve the tail.", "evidence": []}]}]}
        for preview in (False, True):
            with self.subTest(preview=preview):
                document = render_pdf._html_document(self.folder, content, style, preview=preview)
                self.assertIn('class="chapter-heading"', document)
                self.assertIn('class="chapter-body"', document)
                self.assertIn("&lt;script&gt;", document)
                self.assertIn("preserve the tail.", document)
                self.assertNotIn("<script>", document)


if __name__ == "__main__":
    unittest.main()
