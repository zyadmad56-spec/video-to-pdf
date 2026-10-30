import importlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class AuthoringCase(unittest.TestCase):
    def setUp(self):
        try:
            self.authoring = importlib.import_module("lecture_materials.authoring")
        except ModuleNotFoundError:
            self.fail("Evidence-grounded authoring and preview approval are not implemented")
        from lecture_materials.jobs import create_job, write_json
        self.scratch = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / "job"
        video = Path(self.scratch.name) / "video.mp4"
        video.write_bytes(b"source")
        create_job(self.root, str(video), {"image_mode": "text", "allow_examples": False})
        write_json(self.root / "work" / "transcript.json", [{"id": "audio-000001", "kind": "spoken", "text": "Entity has attributes", "start": 0, "end": 5}])
        write_json(self.root / "work" / "screen.json", [])
        self.content = {"title": "Lecture", "language": "en", "direction": "ltr", "sections": [
            {"id": "entities", "title": "Entities", "blocks": [
                {"type": "paragraph", "text": "An entity has attributes.", "evidence": ["audio-000001"]}]}], "omissions": []}
        self.style = {"name": "clean", "font": "Arial", "accent": "#136B65"}

    def test_unknown_citation_is_rejected(self):
        self.content["sections"][0]["blocks"][0]["evidence"] = ["invented-source"]
        with self.assertRaises(ValueError):
            self.authoring.validate_content(self.root, self.content)

    def test_text_only_rejects_inserted_images(self):
        self.content["sections"][0]["blocks"].append({"type": "image", "asset_id": "picture", "evidence": ["audio-000001"]})
        with self.assertRaises(ValueError):
            self.authoring.validate_content(self.root, self.content)

    def test_disabled_examples_cannot_be_added(self):
        self.content["sections"][0]["blocks"][0]["origin"] = "example"
        with self.assertRaises(ValueError):
            self.authoring.validate_content(self.root, self.content)

    def test_missing_source_coverage_is_reported(self):
        self.content["sections"][0]["blocks"] = []
        with self.assertRaises(ValueError):
            self.authoring.validate_content(self.root, self.content)

    def test_changed_content_invalidates_preview_acceptance(self):
        from pypdf import PdfWriter
        preview = PdfWriter()
        preview.add_blank_page(595, 842)
        preview.write(self.root / "work" / "preview.pdf")
        self.authoring.record_preview(self.root, self.content, self.style)
        self.authoring.accept_preview(self.root, self.content, self.style)
        self.authoring.require_accepted_preview(self.root, self.content, self.style)
        self.content["sections"][0]["blocks"][0]["text"] = "Changed explanation"
        with self.assertRaises(ValueError):
            self.authoring.require_accepted_preview(self.root, self.content, self.style)

    def test_approval_before_a_rendered_preview_is_rejected(self):
        with self.assertRaises(ValueError):
            self.authoring.accept_preview(self.root, self.content, self.style)


if __name__ == "__main__":
    unittest.main()
