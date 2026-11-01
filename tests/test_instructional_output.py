"""Instructional units retain their image, steps, and evidence as one unit."""

import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lecture_materials import authoring, jobs, render_pdf, render_pptx, styles


class InstructionalOutputCase(unittest.TestCase):
    def setUp(self):
        from PIL import Image, ImageDraw

        scratch = tempfile.TemporaryDirectory(prefix="tmp-instructions-", dir=Path(__file__).parent)
        self.addCleanup(scratch.cleanup)
        self.folder = Path(scratch.name).resolve()
        source = self.folder / "source.mp4"
        source.write_bytes(b"Synthetic source; not decoded")
        self.root = self.folder / "job"
        jobs.create_job(self.root, str(source), dict(image_mode="user", image_language="en",
                        output_language="en", allow_examples=False, show_timestamps=False))
        image = Image.new("RGB", (600, 400), "white")
        ImageDraw.Draw(image).text((70, 70), "CONTROL_MARKER", fill="black")
        self.image_path = self.root / "work/panel.png"
        image.save(self.image_path)
        jobs.write_json(self.root / "work/transcript.json", [
            dict(id="audio-000001", kind="spoken", text="Teaching", start=0, end=1),
            dict(id="audio-000002", kind="spoken", text="Noise", start=1, end=2)])
        jobs.write_json(self.root / "work/assets.json", [dict(id="panel", origin="user",
            state="accepted", path="work/panel.png", source_name="Synthetic fixture",
            candidate_review=dict(inspected=True, decision="relevant", reason="Synthetic control fixture"),
            language_review=dict(inspected=True, language="en", reason="English fixture label"),
            placements={"lesson": dict(inspected_after_placement=True, decision="relevant",
                                       reason="Shows the control used by this step", placement="auto")})])
        self.block = dict(type="instruction", title="UNIT_MARKER", text="Explain the control.",
            asset_id="panel", caption="CAPTION_MARKER", steps=["STEP_MARKER: select the control.",
            "Confirm the change."], result="RESULT_MARKER: the value changes.",
            callouts=[dict(step=1, box=[0.1, 0.1, 0.3, 0.2])], evidence=["audio-000001"])
        self.content = dict(title="Software guide", language="en", direction="ltr",
            document_mode="software_tutorial", sections=[dict(id="lesson", title="Lesson",
            blocks=[self.block])], omissions=[dict(evidence_id="audio-000002",
            kind="noninstructional", reason="INTERNAL_NOISE_MARKER: unrelated conversation")])
        self.style = styles.load_builtin("clarity-grid")
        self.style["page"].update(width_mm=338.667, height_mm=190.5,
            margin_mm=dict(top=12, right=12, bottom=12, left=12))

    def test_instruction_validates_and_inventory_retains_all_teaching(self):
        report = authoring.validate_content(self.root, self.content)
        self.assertEqual(report["blocks_by_type"]["instruction"], 1)
        inventory = authoring.content_text_inventory(self.content)[0]["text"]
        for marker in ["UNIT_MARKER", "STEP_MARKER", "RESULT_MARKER", "CAPTION_MARKER"]:
            self.assertIn(marker, inventory)

    def test_instruction_keeps_image_policy_and_byte_binding(self):
        authoring.validate_content(self.root, self.content)
        before = authoring.preview_binding(self.root, self.content, self.style)
        self.assertEqual(len(before["assets"]), 1)
        from PIL import Image
        Image.new("RGB", (600, 400), "yellow").save(self.image_path)
        after = authoring.preview_binding(self.root, self.content, self.style)
        self.assertNotEqual(before, after)
        job = jobs.load_job(self.root)
        job["preferences"]["image_mode"] = "text"
        jobs.save_job(self.root, job)
        with self.assertRaisesRegex(ValueError, "Text-only"):
            authoring.validate_content(self.root, self.content)

    def test_invalid_steps_callouts_and_unpaired_images_are_rejected(self):
        invalid = [dict(steps=[]), dict(result=""), dict(callouts=[dict(step=3, box=[0, 0, .2, .2])]),
                   dict(callouts=[dict(step=1, box=[.9, 0, .2, .2])]),
                   dict(callouts=[dict(step=True, box=[0, 0, .2, .2])])]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                content = copy.deepcopy(self.content)
                content["sections"][0]["blocks"][0].update(change)
                authoring.validate_content(self.root, content)
        self.block["type"] = "image"
        with self.assertRaisesRegex(ValueError, "instruction"):
            authoring.validate_content(self.root, self.content)

    def test_pdf_pairs_every_unit_and_excludes_internal_ledger(self):
        import pymupdf
        second = copy.deepcopy(self.block)
        second.update(title="SECOND_UNIT", steps=["SECOND_STEP"], result="SECOND_RESULT", layout="stacked")
        self.content["sections"][0]["blocks"].append(second)
        text = render_pdf._html_document(self.root, self.content, self.style)
        path = self.folder / "guide.pdf"
        render_pdf._print_pdf(text, path, self.content, self.style)
        with pymupdf.open(path) as pdf:
            for marker, companions in [("UNIT_MARKER", ["STEP_MARKER", "RESULT_MARKER", "CAPTION_MARKER"]),
                                       ("SECOND_UNIT", ["SECOND_STEP", "SECOND_RESULT"])]:
                pages = [p for p in pdf if marker in p.get_text()]
                self.assertEqual(len(pages), 1)
                self.assertTrue(pages[0].get_images())
                for companion in companions:
                    self.assertIn(companion, pages[0].get_text())
            self.assertNotIn("INTERNAL_NOISE_MARKER", "".join(p.get_text() for p in pdf))

    def test_dense_pdf_refuses_to_split_a_unit_silently(self):
        self.block["steps"] = ["A long teaching step. " * 60] * 8
        text = render_pdf._html_document(self.root, self.content, self.style)
        with self.assertRaisesRegex(ValueError, "[Ss]plit.*substep"):
            render_pdf._print_pdf(text, self.folder / "too-dense.pdf", self.content, self.style)

    def test_pptx_keeps_unit_on_one_slide_with_editable_callouts(self):
        builder = render_pptx.DeckBuilder(self.content, self.style, self.root)
        builder.build()
        matching = [s for s in builder.presentation.slides if any(
            sh.has_text_frame and "UNIT_MARKER" in sh.text for sh in s.shapes)]
        self.assertEqual(len(matching), 1)
        slide = matching[0]
        text = "\n".join(sh.text for sh in slide.shapes if sh.has_text_frame)
        self.assertIn("STEP_MARKER", text)
        self.assertIn("RESULT_MARKER", text)
        self.assertTrue(any(sh.shape_type == 13 for sh in slide.shapes))
        self.assertTrue(any(sh.name.startswith("Instruction callout") for sh in slide.shapes))
        all_text = "\n".join(sh.text for s in builder.presentation.slides for sh in s.shapes if sh.has_text_frame)
        self.assertNotIn("INTERNAL_NOISE_MARKER", all_text)
        self.assertEqual(len(builder.presentation.slides), 3)  # cover, contents, teaching

    def test_material_uncertainty_survives_and_legacy_ledger_is_unchanged(self):
        self.content["uncertainties"] = [dict(evidence_id="audio-000001", location="00:00:01",
                                             description="MATERIAL_GAP_MARKER")]
        text = render_pdf._html_document(self.root, self.content, self.style)
        self.assertIn("MATERIAL_GAP_MARKER", text)
        legacy = copy.deepcopy(self.content)
        legacy.pop("document_mode")
        legacy["sections"][0]["blocks"] = [dict(type="paragraph", text="Legacy teaching", evidence=["audio-000001"])]
        self.assertIn("INTERNAL_NOISE_MARKER", render_pdf._html_document(self.root, legacy, self.style))

    def test_pdf_reference_body_font_is_preserved_in_native_instruction(self):
        self.style["design"] = {"source_format": "pdf"}
        self.style["page"]["typography"]["body_pt"] = 16.5
        self.style["slides"]["typography"]["body_pt"] = 27.2
        builder = render_pptx.DeckBuilder(self.content, self.style, self.root)
        builder.build()
        sizes = [run.font.size.pt for slide in builder.presentation.slides for shape in slide.shapes
                 if shape.has_text_frame and "STEP_MARKER" in shape.text
                 for paragraph in shape.text_frame.paragraphs for run in paragraph.runs]
        self.assertTrue(sizes)
        self.assertEqual(set(sizes), {16.5})

    def test_preview_includes_two_complete_units_instead_of_only_text(self):
        self.content["sections"][0]["blocks"].insert(0, dict(type="paragraph", text="Introductory text", evidence=[]))
        second = copy.deepcopy(self.block)
        second.update(title="DENSE_UNIT", steps=["DENSE_STEP"], result="DENSE_RESULT")
        self.content["sections"][0]["blocks"].append(second)
        html = render_pdf._html_document(self.root, self.content, self.style, preview=True)
        self.assertIn("STEP_MARKER", html)
        self.assertIn("DENSE_STEP", html)
        builder = render_pptx.DeckBuilder(self.content, self.style, self.root, preview=True)
        builder.build(preview=True)
        text = "\n".join(sh.text for slide in builder.presentation.slides for sh in slide.shapes if sh.has_text_frame)
        self.assertIn("STEP_MARKER", text)
        self.assertIn("DENSE_STEP", text)
        with self.subTest(format="pptx"):
            self.assertEqual(len(builder.presentation.slides), 3)
        import pymupdf
        path = self.folder / "preview.pdf"
        render_pdf._print_pdf(html, path, self.content, self.style)
        with self.subTest(format="pdf"), pymupdf.open(path) as pdf:
            self.assertEqual(len(pdf), 3)

    def test_dense_slide_refuses_to_drop_or_fragment_teaching(self):
        self.block["steps"] = ["A long teaching step. " * 60] * 8
        builder = render_pptx.DeckBuilder(self.content, self.style, self.root)
        with self.assertRaisesRegex(ValueError, "[Ss]plit.*substep"):
            builder.build()

    def test_reviewed_source_preview_accept_render_verify_pipeline(self):
        # Reuse the existing synthetic source setup, not a real user's approval.
        import shutil
        from test_reviewer_pipeline import ReviewerPipelineCase
        from lecture_materials import verification

        fixture = ReviewerPipelineCase(methodName="runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.preferences.update(output_language="en", image_mode="user", image_language="en",
                                   show_timestamps=False, visual_style="clarity-grid")
        jobs.write_json(fixture.prefs_path, fixture.preferences)
        spoken, retained = fixture.initialize_reviewed_fixture()
        shutil.copyfile(self.image_path, fixture.root / "work/panel.png")
        jobs.write_json(fixture.root / "work/assets.json", jobs.read_json(self.root / "work/assets.json"))
        content = copy.deepcopy(self.content)
        content["omissions"] = []
        content["sections"][0]["blocks"][0]["evidence"] = spoken + retained
        content_path = fixture.root / "work/content.json"
        jobs.write_json(content_path, content)
        fixture.cli("preview", "--content", content_path, "--style", "clarity-grid", "--format", "pdf")
        fixture.cli("accept", "--content", content_path, "--style", "clarity-grid", "--user-accepted")
        fixture.cli("render", "--content", content_path, "--style", "clarity-grid", "--format", "both")
        outputs = [*fixture.root.glob("outputs/*.pdf"), *fixture.root.glob("outputs/*.pptx")]
        self.assertEqual(len(outputs), 2)
        report = verification.inspect_outputs(fixture.root, outputs, content_path=content_path, render=False)
        self.assertTrue(report["ok"], report.get("problems"))

    def test_reference_cover_note_stays_on_the_cover(self):
        import pymupdf

        self.style["page"]["margin_mm"].update(top=30.5, bottom=10)
        self.style["page"]["typography"]["title_pt"] = 52.5
        self.content.update(title="Power BI - instructional layout sample", source_note="COVER_NOTE_MARKER")
        html = render_pdf._html_document(self.root, self.content, self.style)
        path = self.folder / "cover.pdf"
        render_pdf._print_pdf(html, path, self.content, self.style)
        with pymupdf.open(path) as pdf:
            self.assertIn("COVER_NOTE_MARKER", pdf[0].get_text())
            self.assertEqual(len(pdf), 3)

    def test_recording_credit_preserves_its_source_link(self):
        job = jobs.load_job(self.root)
        job["preferences"].update(image_mode="user+lecture", allow_source_images=True)
        jobs.save_job(self.root, job)
        assets = jobs.read_json(self.root / "work/assets.json")
        assets[0].update(origin="lecture", source_url="https://example.org/lecture", license="unknown")
        jobs.write_json(self.root / "work/assets.json", assets)
        html = render_pdf._html_document(self.root, self.content, self.style)
        self.assertTrue('href="https://example.org/lecture"' in html, "The recording's source link was lost")
        builder = render_pptx.DeckBuilder(self.content, self.style, self.root)
        builder.build()
        links = [run.hyperlink.address for slide in builder.presentation.slides
                 for shape in slide.shapes if shape.has_text_frame
                 for paragraph in shape.text_frame.paragraphs for run in paragraph.runs]
        self.assertIn("https://example.org/lecture", links)


if __name__ == "__main__":
    unittest.main()
