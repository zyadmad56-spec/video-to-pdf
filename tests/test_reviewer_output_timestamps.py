"""Source-navigation choices affect output, not speech evidence or teaching text."""

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lecture_materials import authoring, cli, jobs, output_preferences, render_pdf, render_pptx, styles


class OutputTimestampCase(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name).resolve()
        source = self.folder / "source.mp4"
        source.write_bytes(b"Test source; not decoded")
        self.root = self.folder / "job"
        self.preferences = dict(output_language="en", image_mode="text", formats=["pdf"], allow_examples=False)
        jobs.create_job(self.root, str(source), self.preferences)
        self.content = dict(title="Trees", language="en", direction="ltr", sections=[
            dict(id="repair", title="Repair", source_range=dict(start=1038, end=1098), blocks=[
                dict(type="paragraph", text="Keep -1 and 2026. Wait 30 seconds.", evidence=[])])])

    def invoke(self, command, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main([command, "--root", str(self.root), *arguments])
        return code, json.loads(output.getvalue() or errors.getvalue())

    def visibility(self, visible):
        job = jobs.load_job(self.root)
        job["preferences"]["show_timestamps"] = visible
        jobs.save_job(self.root, job)

    def test_unanswered_question_is_read_only_and_false_is_a_durable_answer(self):
        before = (self.root / "job.json").read_bytes()
        code, response = self.invoke("output-intake")
        self.assertEqual(code, 0, response)
        question = response["output_intake"]["questions"][0]
        self.assertEqual(question["id"], "show_timestamps")
        self.assertEqual([option["value"] for option in question["options"]], [True, False])
        code, response = self.invoke("output-intake", "--show-timestamps", "no")
        self.assertEqual(code, 0, response)
        self.assertIs(response["preferences"]["show_timestamps"], False)
        self.assertEqual((self.root / "job.json").read_bytes(), before)
        code, response = self.invoke("output-intake", "--show-timestamps", "no", "--apply")
        self.assertEqual(code, 0, response)
        self.assertIs(jobs.load_job(self.root)["preferences"]["show_timestamps"], False)
        code, response = self.invoke("status")
        self.assertEqual(code, 0, response)
        self.assertEqual(response["output_intake"]["questions"], [])

    def test_init_surfaces_the_question_and_rejects_non_boolean_answers_before_creation(self):
        for index, choice in enumerate(("missing", False, "false")):
            with self.subTest(choice=choice):
                target = self.folder / f"new-{index}"
                preferences = dict(self.preferences)
                if choice != "missing":
                    preferences["show_timestamps"] = choice
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = cli.main(["init", "--root", str(target), "--source", str(self.folder / "source.mp4"),
                                     "--preferences-json", json.dumps(preferences)])
                response = json.loads(output.getvalue() or errors.getvalue())
                if choice == "false":
                    self.assertEqual(code, 2, response)
                    self.assertFalse((target / "job.json").exists())
                else:
                    self.assertEqual(code, 0, response)
                    self.assertEqual(response["output_intake"]["answered"], choice != "missing")

    def test_invalid_ranges_and_boolean_preferences_fail_explicitly(self):
        for choice in (None, 0, 1, "yes", "false", []):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                output_preferences.timestamp_intake({"show_timestamps": choice})
        intervals = [None, {}, dict(start=True, end=10), dict(start=0, end=float("inf")),
                     dict(start=float("nan"), end=10), dict(start=-1, end=10),
                     dict(start=10, end=10), dict(start=11, end=10)]
        for interval in intervals:
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                output_preferences.section_source_range({"source_range": interval})

    def test_range_formatting_handles_zero_hours_and_missing_navigation(self):
        self.visibility(True)
        for bounds, expected in [((0, 0.2), "00:00–00:01"), ((1038.8, 1097.2), "17:18–18:18"),
                                 ((3600, 3661), "01:00:00–01:01:01")]:
            with self.subTest(bounds=bounds):
                section = dict(source_range=dict(zip(("start", "end"), bounds)))
                self.assertEqual(output_preferences.section_time_label(self.root, section), expected)
        self.assertEqual(output_preferences.section_time_label(self.folder / "no-job", {}), "")

    def test_pdf_preview_and_final_obey_visibility_in_both_directions_without_mutation(self):
        original = copy.deepcopy(self.content)
        style = styles.load_builtin("clarity-grid")
        for direction in ("ltr", "rtl"):
            content = {**self.content, "direction": direction}
            for visible in (True, False):
                self.visibility(visible)
                for preview in (True, False):
                    with self.subTest(direction=direction, visible=visible, preview=preview):
                        document = render_pdf._html_document(self.root, content, style, preview=preview)
                        self.assertEqual("17:18–18:18" in document, visible)
                        self.assertIn("Keep -1 and 2026. Wait 30 seconds.", document)
                        if visible:
                            self.assertIn('class="meta section-time" dir="ltr"', document)
        self.assertEqual(self.content, original)

    def test_editable_slide_headers_obey_visibility_in_preview_and_final(self):
        style = styles.load_builtin("clarity-grid")
        for direction in ("ltr", "rtl"):
            content = {**self.content, "direction": direction}
            for visible in (True, False):
                self.visibility(visible)
                for preview in (True, False):
                    with self.subTest(direction=direction, visible=visible, preview=preview):
                        builder = render_pptx.DeckBuilder(content, style, self.root, preview=preview, text_only=True)
                        builder.build(preview=preview)
                        text = "\n".join(shape.text for slide in builder.presentation.slides
                                         for shape in slide.shapes if shape.has_text_frame)
                        self.assertEqual("17:18–18:18" in text, visible)
                        self.assertIn("Keep -1 and 2026. Wait 30 seconds.", text)

    def test_visibility_change_invalidates_binding_but_preserves_timestamped_evidence(self):
        transcript = self.root / "work/transcript.json"
        jobs.write_json(transcript, [dict(id="speech", kind="spoken", start=1038, end=1098, text="Repair")])
        original = transcript.read_bytes()
        self.content["sections"][0]["blocks"][0]["evidence"] = ["speech"]
        self.visibility(True)
        shown = jobs.json_digest(authoring.preview_binding(self.root, self.content, None))
        self.visibility(False)
        hidden = jobs.json_digest(authoring.preview_binding(self.root, self.content, None))
        self.assertNotEqual(shown, hidden)
        self.assertEqual(transcript.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
