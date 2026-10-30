"""Local, real-artifact acceptance after implementer handoff.

Every lecture/approval here is a synthetic reviewer fixture. The ASR smoke uses
real offline Whisper on Windows-generated English speech, not a real lecture.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lecture_materials import jobs, media, speech

PROJECT = Path(__file__).resolve().parents[1]
ENTRY = PROJECT / "scripts/lecture.py"
ARTIFACTS = PROJECT / ".test-artifacts"


def native_slide_text(path):
    from pptx import Presentation
    deck = Presentation(path)
    parts = []
    for slide in deck.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text)
            if shape.has_table:
                parts.extend(cell.text for row in shape.table.rows for cell in row.cells)
    return deck, "\n".join(parts)


class ReviewerPipelineCase(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="tmp-reviewer-pipeline-", dir=Path(__file__).parent)
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name).resolve()
        self.foreign = self.folder / "different cwd عربي"
        self.foreign.mkdir()
        self.root = self.folder / "job عربي"
        self.video = self.folder / "original lecture.mkv"
        subprocess.run([media.tool_path("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                        "color=white:s=640x360:r=2:d=12", "-vf",
                        "drawbox=x=120:y=100:w=130:h=80:color=black:t=fill:enable='gte(t,6)'",
                        "-c:v", "ffv1", str(self.video)], check=True)
        self.preferences = {"output_language": "mixed", "direction": "rtl", "image_mode": "text",
                            "formats": ["pdf", "pptx"], "allow_examples": False,
                            "teaching_style": "stepwise", "visual_style": "scholar-ink",
                            "image_placement": "auto"}
        self.prefs_path = self.folder / "prefs.json"
        jobs.write_json(self.prefs_path, self.preferences)

    def cli(self, command, *arguments, expected=0, with_root=True):
        argv = [sys.executable, "-X", "utf8", str(ENTRY), command]
        if with_root:
            argv.extend(["--root", str(self.root)])
        argv.extend(map(str, arguments))
        completed = subprocess.run(argv, cwd=self.foreign, capture_output=True, timeout=240)
        stdout = completed.stdout.decode("utf-8-sig", errors="replace").strip()
        stderr = completed.stderr.decode("utf-8-sig", errors="replace").strip()
        self.assertEqual(completed.returncode, expected, f"{command}\nSTDOUT: {stdout[-3000:]}\nSTDERR: {stderr[-3000:]}")
        return json.loads(stdout or stderr)

    def initialize_reviewed_fixture(self):
        self.cli("init", "--source", self.video, "--preferences", self.prefs_path)
        self.cli("prepare")
        captions = self.folder / "original.en.vtt"
        captions.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nAn entity has attributes.\n\n"
                            "00:00:06.000 --> 00:00:12.000\nA primary key is unique: KEY(i) differs from KEY(j) for i not equal to j.\n", encoding="utf-8")
        self.cli("captions", "--caption", captions, "--language", "en", "--provenance", "manual")
        self.cli("frames", "--limit", 1)
        self.cli("readiness", expected=2)
        self.cli("frames")
        state = jobs.read_json(self.root / "work/frames-state.json")
        retained = list(dict.fromkeys(frame["frame_id"] for frame in state["frames"]))
        reviews = [{"id": identifier, "status": "reviewed",
                    "observations": "Synthetic fixture: initially a white board, then one added black box."}
                   for identifier in retained]
        review_path = self.folder / "screen-review.json"
        jobs.write_json(review_path, reviews)
        self.cli("merge-review", "--kind", "screen", "--review", review_path)
        spoken = jobs.read_json(self.root / "work/transcript.json")
        return [record["id"] for record in spoken], retained

    def content(self, spoken, retained):
        return {"schema_version": 1, "title": "Synthetic reviewer fixture — مذكرة تجريبية",
                "language": "mixed", "direction": "rtl", "sections": [
                    {"id": "entities", "title": "الكيانات Entity", "blocks": [
                        {"type": "paragraph", "text": "الكيان Entity له خصائص attributes. The diagram adds one black box. EntityMarker.",
                         "evidence": [spoken[0], *retained]},
                        {"type": "bullets", "items": ["المصطلح Entity يعني كيان.", "كل خاصية attribute تصف الكيان."],
                         "evidence": [spoken[0]]},
                    ]},
                    {"id": "keys", "title": "المفاتيح Primary key", "blocks": [
                        {"type": "paragraph", "text": "المفتاح الأساسي Primary key فريد. " +
                         "Each row retains its own identifier and the explanation continues on later slides. " * 45 + " TailMarker987.",
                         "evidence": [spoken[1]]},
                        {"type": "table", "header": ["Row", "Identifier", "Unit"],
                         "rows": [[f"row_{index:03}", f"key_{index:03}", "rows"] for index in range(40)],
                         "caption": "جدول تجريبي لاختبار استمرار الصفوف بدون فقد.", "evidence": [spoken[1]]},
                        {"type": "equation", "text": "KEY(i) ≠ KEY(j), i ≠ j", "symbols": "KEY = primary key; i,j = row indices",
                         "units": "rows", "description": "المفتاح الأساسي فريد لكل صف.", "evidence": [spoken[1]]},
                    ]},
                ], "omissions": [], "uncertainties": []}

    def test_both_styles_complete_cli_preview_delivery_cleanup_and_real_outputs(self):
        import pymupdf
        spoken, retained = self.initialize_reviewed_fixture()
        content = self.content(spoken, retained)
        transcript_before = jobs.file_digest(self.root / "work/transcript.json")
        frames_before = jobs.file_digest(self.root / "work/frames-state.json")
        final_paths = []
        for style in ("scholar-ink", "clarity-grid"):
            with self.subTest(style=style):
                job = jobs.load_job(self.root)
                job["preferences"]["visual_style"] = style
                jobs.save_job(self.root, job)
                content["output_stem"] = "reviewer-" + style
                jobs.write_json(self.root / "work/content.json", content)
                self.cli("validate", "--content", "work/content.json")
                self.cli("readiness", "--content", "work/content.json")
                preview = self.cli("preview", "--content", "work/content.json", "--style", style, "--format", "pdf")
                self.cli("render", "--content", "work/content.json", "--style", style, "--format", "pdf", expected=4)
                self.cli("accept", "--content", "work/content.json", "--style", style, expected=2)
                self.cli("accept", "--content", "work/content.json", "--style", style, "--user-accepted",
                         "--confirmed-by", "synthetic reviewer fixture")
                built = self.cli("render", "--content", "work/content.json", "--style", style, "--format", "both")
                pdf = Path(built["rendered"]["pdf"]["output"])
                pptx = Path(built["rendered"]["pptx"]["output"])
                final_paths.extend([pdf, pptx])
                with pymupdf.open(pdf) as document:
                    corpus = "\n".join(page.get_text() for page in document)
                    self.assertGreaterEqual(len(document), 4)
                    self.assertEqual(sum(len(page.get_images(full=True)) for page in document), 0)
                    self.assertRegex(corpus, r"[\u0600-\u06ff]")
                    for token in ("EntityMarker", "TailMarker987", "row_000", "row_039", "key_039", "KEY"):
                        self.assertIn(token, corpus)
                deck, text = native_slide_text(pptx)
                self.assertGreater(len(deck.slides), 5)
                self.assertTrue(any(shape.has_table for slide in deck.slides for shape in slide.shapes))
                for token in ("EntityMarker", "TailMarker987", "row_000", "row_039", "key_039", "KEY"):
                    self.assertIn(token, text)
                with zipfile.ZipFile(pptx) as package:
                    self.assertFalse(any(name.startswith("ppt/media/") for name in package.namelist()))
                readback = built["rendered"]["pptx"]["readback"]
                self.assertGreater(readback["rtl_paragraphs"], 0)
                if os.name == "nt":
                    self.assertTrue(readback["render"]["available"], readback.get("rendering_note"))
                self.cli("verify", "--output", pdf, "--output", pptx, "--no-render")
                target = ARTIFACTS / style
                target.mkdir(parents=True, exist_ok=True)
                for path in (pdf, pptx, self.root / "work/preview.png"):
                    shutil.copyfile(path, target / path.name)
                for index, path in enumerate(built["rendered"]["pdf"].get("rendered_previews", [])):
                    shutil.copyfile(path, target / f"pdf-page-{index + 1}.png")
                for index, path in enumerate(built["rendered"]["pptx"].get("rendered_previews", [])):
                    shutil.copyfile(path, target / f"slide-{index + 1}.png")
                jobs.write_json(target / "reviewer-render-report.json", built)
        self.assertEqual(jobs.file_digest(self.root / "work/transcript.json"), transcript_before)
        self.assertEqual(jobs.file_digest(self.root / "work/frames-state.json"), frames_before)
        self.cli("deliver", "--output", final_paths[-2], "--output", final_paths[-1], "--no-render")
        note = self.root / "work/unknown-host-note.txt"
        note.write_text("Preserve unregistered work", encoding="utf-8")
        plan = self.cli("cleanup-request")
        self.assertTrue((self.root / "work/transcript.txt").exists())
        self.cli("keep")
        self.cli("cleanup-confirm", "--token", plan["confirmation_token"], expected=2)
        self.assertTrue((self.root / "work/transcript.txt").exists())
        plan = self.cli("cleanup-request")
        self.cli("cleanup-confirm", "--token", plan["confirmation_token"])
        self.assertFalse((self.root / "work/transcript.txt").exists())
        self.assertTrue(note.is_file())
        self.assertTrue(self.video.is_file())
        for path in final_paths:
            if path.suffix == ".pdf":
                with pymupdf.open(path) as document:
                    self.assertTrue(any("TailMarker987" in page.get_text() for page in document))
            else:
                self.assertIn("TailMarker987", native_slide_text(path)[1])

    def test_reference_design_cli_saved_style_and_explicit_roles_survive_cleanup(self):
        # Written after the design-cli handoff: real CLI from a foreign CWD,
        # real reference/new-content PDFs, and actual library/cleanup behavior.
        import pymupdf
        spoken, retained = self.initialize_reviewed_fixture()
        reference = self.folder / "تصميم أصلي.pdf"
        document = pymupdf.open()
        for index in range(3):
            page = document.new_page(width=480, height=640)
            page.draw_rect(pymupdf.Rect(0, 0, 14, 640), fill=(0.36, 0.14, 0.55), color=None)
            page.insert_text((40, 45), "STYLE_ONLY_OLD_527", fontsize=11)
            page.insert_text((40, 100), "Original design reference paragraph.", fontsize=14)
            page.insert_text((420, 610), str(index + 1), fontsize=8)
        document.save(reference)
        document.close()
        digest = jobs.file_digest(reference)
        self.cli("design-extract", "--reference", reference, expected=2)
        self.cli("attach", "--path", reference, "--role", "design")
        self.cli("references", "--reference", reference, expected=2)
        extracted = self.cli("design-extract", "--reference", reference, "--destination", "work/design/proposed.json")
        proposal = Path(extracted["proposal_path"])
        self.assertTrue(proposal.is_file())
        self.assertTrue(extracted["reference_previews"])
        self.assertIsNone(extracted["content_import"])
        self.assertFalse((self.root / "work/references.json").exists(), "Design-only prose must not become lecture evidence")
        library = self.folder / "shared library عربي"
        common = ("--library", library, "--kind", "visual_style", "--name", "My Purple", "--style-path", proposal)
        self.cli("style-save", *common, expected=2)
        saved = self.cli("style-save", *common, "--user-agreed")
        style = Path(saved["reusable_style_path"])
        self.assertTrue(style.is_file())
        self.assertIsNone(jobs.load_job(self.root).get("preview"), "Saving a name must not approve output")
        self.cli("style-save", *common, "--user-agreed", expected=2)
        versioned = self.cli("style-save", *common, "--user-agreed", "--collision", "new_version")
        self.assertNotEqual(saved["saved"]["path"], versioned["saved"]["path"])
        loaded = self.cli("style-load", "--library", library, "--kind", "visual_style", "--name", "My Purple",
                          "--version", saved["saved"]["version"], with_root=False)
        self.assertEqual(Path(loaded["style_path"]), style)
        self.cli("design-check", "--style", style)
        content = self.content(spoken, retained)
        content["output_stem"] = "named-reference-cli"
        jobs.write_json(self.root / "work/content.json", content)
        preview = self.cli("preview", "--content", "work/content.json", "--style", style, "--format", "pdf")
        self.assertTrue(preview["reference_comparison"]["available"])
        self.assertTrue(preview["reference_comparison"]["images"])
        self.cli("render", "--content", "work/content.json", "--style", style, "--format", "pdf", expected=4)
        self.cli("accept", "--content", "work/content.json", "--style", style, "--user-accepted",
                 "--confirmed-by", "synthetic named-design CLI fixture")
        built = self.cli("render", "--content", "work/content.json", "--style", style, "--format", "pdf")
        final = Path(built["rendered"]["pdf"]["output"])
        with pymupdf.open(final) as output:
            self.assertAlmostEqual(output[0].rect.width, 480, delta=1)
            self.assertAlmostEqual(output[0].rect.height, 640, delta=1)
            text = "\n".join(page.get_text() for page in output)
            self.assertIn("TailMarker987", text)
            self.assertNotIn("STYLE_ONLY_OLD_527", text)
        target = ARTIFACTS / "named-design-cli"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(final, target / final.name)
        for index, path in enumerate(preview["reference_comparison"]["images"]):
            shutil.copyfile(path, target / f"comparison-{index+1}.png")
        self.cli("deliver", "--output", final, "--no-render")
        plan = self.cli("cleanup-request")
        self.assertIn(str(proposal.resolve()), plan["delete"])
        self.cli("cleanup-confirm", "--token", plan["confirmation_token"])
        self.assertFalse(proposal.exists())
        self.assertTrue(style.is_file())
        self.assertEqual(jobs.file_digest(reference), digest)
        # A later job reuses the saved JSON even though the first proposal is gone.
        self.root = self.folder / "later job"
        later_spoken, later_retained = self.initialize_reviewed_fixture()
        content = self.content(later_spoken, later_retained)
        jobs.write_json(self.root / "work/content.json", content)
        reused = self.cli("preview", "--content", "work/content.json", "--style", style, "--format", "pdf")
        self.assertTrue(reused["reference_comparison"]["available"])
        both = self.cli("design-extract", "--reference", reference, "--role", "both")
        self.assertEqual(set(both["attachment_roles"]), {"content", "design"})
        self.assertTrue((self.root / "work/references.json").is_file())

    def test_teaching_profile_cli_requires_consent_and_keeps_example_permission_separate(self):
        self.cli("init", "--source", self.video, "--preferences", self.prefs_path)
        library = self.folder / "profile library"
        profile = {"tone": "Egyptian Arabic", "step_size": "short steps", "terminology": "Keep English technical terms"}
        arguments = ("--library", library, "--kind", "teaching_profile", "--name", "My Steps",
                     "--profile-json", json.dumps(profile))
        self.cli("style-save", *arguments, expected=2)
        saved = self.cli("style-save", *arguments, "--user-agreed")
        loaded = self.cli("style-load", "--library", library, "--kind", "teaching_profile", "--name", "My Steps",
                          with_root=False)
        self.assertEqual(loaded["document"]["preferences"], profile)
        self.assertFalse(jobs.load_job(self.root)["preferences"]["allow_examples"])
        listing = self.cli("style-list", "--library", library, "--kind", "teaching_profile", with_root=False)
        self.assertTrue(listing["profiles"]["teaching_profile"])
        self.assertTrue(Path(saved["profile_path"]).is_file())

    def test_cli_missing_choices_rejects_init_before_creating_job(self):
        jobs.write_json(self.prefs_path, {"image_mode": "text"})
        report = self.cli("init", "--source", self.video, "--preferences", self.prefs_path, expected=2)
        self.assertFalse(report["ok"])
        self.assertFalse((self.root / "job.json").exists())


@unittest.skipUnless(os.name == "nt", "Real local ASR smoke uses Windows SAPI-generated English speech")
class ReviewerRealAsrCase(unittest.TestCase):
    def test_real_offline_model_transcribes_synthesized_english_speech(self):
        model = PROJECT / ".models/whisper-small"
        if not (model / "model.bin").is_file():
            self.skipTest("No local Whisper-small files; no model download is permitted in tests")
        import pythoncom
        import win32com.client
        with tempfile.TemporaryDirectory(prefix="tmp-reviewer-real-asr-", dir=Path(__file__).parent) as scratch:
            folder = Path(scratch).resolve()
            wav = folder / "synthetic speech.wav"
            pythoncom.CoInitialize()
            stream = None
            try:
                voice = win32com.client.Dispatch("SAPI.SpVoice")
                voices = voice.GetVoices("Language=409")
                if voices.Count == 0:
                    self.skipTest("No English SAPI voice is available for the synthetic source")
                voice.Voice = voices.Item(0)
                voice.Rate = -1
                stream = win32com.client.Dispatch("SAPI.SpFileStream")
                stream.Open(str(wav), 3, False)
                voice.AudioOutputStream = stream
                voice.Speak("An entity has attributes. A primary key is unique. Each student has a student identifier.")
                stream.Close()
                stream = None
                voice = None
            finally:
                if stream is not None:
                    stream.Close()
                pythoncom.CoUninitialize()
            video = folder / "synthetic spoken lecture.mkv"
            subprocess.run([media.tool_path("ffmpeg"), "-v", "error", "-i", str(wav), "-f", "lavfi", "-i",
                            "color=white:s=320x180:r=2:d=30", "-map", "1:v:0", "-map", "0:a:0", "-shortest",
                            "-c:v", "ffv1", "-c:a", "pcm_s16le", str(video)], check=True)
            root = folder / "job"
            jobs.create_job(root, str(video), {"image_mode": "text"})
            report = speech.transcribe_local(root, {"model_dir": str(model), "language": "en", "device": "cpu",
                                                     "compute_type": "int8", "cpu_threads": 2, "beam_size": 3})
            self.assertEqual(report["status"], "transcribed")
            records = jobs.read_json(root / "work/transcript.json")
            text = " ".join(record["text"] for record in records).casefold()
            self.assertIn("key", text)
            self.assertIn("unique", text)
            for record in records:
                self.assertGreaterEqual(record["start"], 0)
                self.assertGreater(record["end"], record["start"])
            target = ARTIFACTS / "real-asr-smoke"
            target.mkdir(parents=True, exist_ok=True)
            jobs.write_json(target / "asr-report.json", report)
            shutil.copyfile(root / "work/transcript.txt", target / "synthetic-english-transcript.txt")


if __name__ == "__main__":
    unittest.main()
