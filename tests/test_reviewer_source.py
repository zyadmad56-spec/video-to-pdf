"""Independent source acceptance after source-handoff/result.json.

FFmpeg, filesystem and reference files are real. Whisper SDK is replaced only
in the explicitly named SDK-boundary tests; these do not measure ASR accuracy.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lecture_materials import authoring, evidence, jobs, media, speech
from test_reviewer_authoring import readable_pdf


def scripted_whisper(responses):
    """Control only the third-party ASR boundary, keeping actual persistence/audio."""
    class ScriptedWhisper:
        def __init__(self, *args, **kwargs):
            self.responses = iter(responses)

        def transcribe(self, *args, **kwargs):
            response = next(self.responses)
            if isinstance(response, Exception):
                raise response
            start, end, text = response
            segment = SimpleNamespace(start=start, end=end, text=text, avg_logprob=-0.1,
                                      compression_ratio=1.0, no_speech_prob=0.0)
            return iter([segment]), SimpleNamespace(language="en", language_probability=0.99)
    return ScriptedWhisper


class ReviewerSourceCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="tmp-reviewer-source-", dir=Path(__file__).parent)
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.shared = Path(cls.scratch.name)
        cls.video = cls.shared / "lecture عربي.mkv"
        subprocess.run([media.tool_path("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                        "color=white:s=320x180:r=1:d=55", "-f", "lavfi", "-i",
                        "sine=frequency=400:sample_rate=16000:duration=55", "-c:v", "ffv1",
                        "-c:a", "pcm_s16le", "-shortest", str(cls.video)], check=True)

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="case-", dir=self.shared)
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name)
        self.root = self.folder / "job"
        jobs.create_job(self.root, str(self.video), {"image_mode": "text", "allow_examples": False})

    def sdk_settings(self):
        # Deliberately synthetic model files: the SDK boundary is mocked here.
        directory = self.folder / "synthetic-sdk-model"
        directory.mkdir(exist_ok=True)
        for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
            (directory / name).write_text("{}", encoding="utf-8")
        return {"model_dir": str(directory), "language": "en", "chunk_seconds": 30, "overlap_seconds": 5}

    def delivered_work(self):
        final = self.root / "outputs/final.pdf"
        readable_pdf(final, "Synthetic final remains readable after cleanup")
        jobs.mark_delivered(self.root, [final])
        first = self.root / "work/a.txt"
        second = self.root / "work/b.txt"
        first.write_text("generated a", encoding="utf-8")
        second.write_text("generated b", encoding="utf-8")
        jobs.register_generated(self.root, first)
        jobs.register_generated(self.root, second)
        return final, first, second

    def test_invalid_finite_ranges_are_rejected_before_sampling(self):
        for start, end in ((float("nan"), 10), (0, float("inf")), (-1, 5), (10, 5), (0, 100)):
            with self.subTest(start=start, end=end):
                job = jobs.load_job(self.root)
                job["preferences"].update(start=start, end=end)
                jobs.save_job(self.root, job)
                with self.assertRaises(ValueError):
                    media.prepare_source(self.root)
        self.assertFalse((self.root / "work/frames-state.json").exists())

    def test_replaced_source_is_not_reused_with_old_evidence(self):
        original = self.folder / "replaceable.mkv"
        shutil.copyfile(self.video, original)
        root = self.folder / "replacement-job"
        jobs.create_job(root, str(original), {"image_mode": "text"})
        media.prepare_source(root)
        with original.open("ab") as stream:
            stream.write(b"changed source identity")
        with self.assertRaises(ValueError):
            media.prepare_source(root)

    def test_real_lossless_board_retains_one_pixel_changes_across_resume(self):
        from PIL import Image
        frames = self.folder / "input-frames"
        frames.mkdir()
        for index in range(12):
            image = Image.new("RGB", (320, 180), "white")
            if index >= 5:
                image.putpixel((100, 100), (0, 0, 0))
            if index >= 10:
                image.putpixel((101, 100), (0, 0, 0))
            image.save(frames / f"frame-{index:03}.png")
        video = self.folder / "tiny writing.mkv"
        subprocess.run([media.tool_path("ffmpeg"), "-v", "error", "-framerate", "1", "-i",
                        str(frames / "frame-%03d.png"), "-c:v", "ffv1", str(video)], check=True)
        root = self.folder / "board-job"
        # This fixture's one-pixel transitions occur at 5s and 10s.
        jobs.create_job(root, str(video), {"image_mode": "text", "frame_interval_seconds": 5})
        first = media.sample_frames(root, limit=1)
        self.assertFalse(first["complete"])
        before = jobs.read_json(root / "work/frames-state.json")["frames"]
        finished = media.sample_frames(root)
        self.assertTrue(finished["complete"])
        self.assertEqual(finished["retained"], 3)
        state = jobs.read_json(root / "work/frames-state.json")
        self.assertEqual(state["frames"][:1], before)
        self.assertEqual([frame["requested"] for frame in state["frames"]], [0, 5, 10, 12])
        self.assertLessEqual(state["frames"][-1]["decoded"], 11.001)
        self.assertEqual(len(list((root / "work/frames").glob("*.png"))), 3)

    def test_review_packets_do_not_approve_and_unknown_ids_cannot_merge(self):
        media.sample_frames(self.root)
        packet = evidence.review_packet(self.root, offset=0, limit=1)
        self.assertEqual(len(packet["items"]), 1)
        with self.assertRaises(ValueError):
            authoring.require_screen_review(self.root)
        review = self.folder / "unknown-review.json"
        jobs.write_json(review, [{"id": "invented-frame", "status": "reviewed", "observations": "A diagram."}])
        with self.assertRaises(ValueError):
            evidence.import_screen_review(self.root, review)
        self.assertFalse((self.root / "work/screen.json").exists())

    def test_vector_pdf_diagrams_require_visual_review_even_without_bitmaps(self):
        # R-S003: PDF vector diagrams are not embedded raster images.
        import pymupdf
        reference = self.folder / "vector ERD.pdf"
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((40, 40), "Student and Course entities are related through Enrollment records.")
        page.draw_rect(pymupdf.Rect(50, 90, 180, 150))
        page.draw_rect(pymupdf.Rect(330, 90, 460, 150))
        page.draw_line(pymupdf.Point(180, 120), pymupdf.Point(330, 120))
        page.insert_text((60, 120), "Student")
        page.insert_text((340, 120), "Course")
        document.save(reference)
        document.close()
        original = reference.read_bytes()
        evidence.import_reference(self.root, reference)
        record = jobs.read_json(self.root / "work/references.json")[0]
        self.assertTrue(record["requires_visual_review"], "Vector-only relationships must be inspected visually")
        self.assertTrue((self.root / record["rendered_path"]).is_file())
        with self.assertRaises(ValueError):
            authoring.require_reference_review(self.root)
        self.assertEqual(reference.read_bytes(), original)

    def test_sdk_overlap_preserves_changed_scientific_terms(self):
        # R-S004: similar token sets are not equivalent scientific statements.
        settings = self.sdk_settings()
        responses = [(20, 30, "The primary key is student id"),
                     (0, 10, "The primary key is course id")]
        with patch("faster_whisper.WhisperModel", scripted_whisper(responses)):
            speech.transcribe_local(self.root, settings)
        records = jobs.read_json(self.root / "work/transcript.json")
        self.assertEqual([record["text"] for record in records], [response[2] for response in responses])
        self.assertEqual([record["start"] for record in records], [20, 25])

    def test_sdk_interruption_resumes_matching_completed_chunks(self):
        settings = self.sdk_settings()
        with patch("faster_whisper.WhisperModel", scripted_whisper([(5, 10, "Retain first concept"), RuntimeError("SDK interrupted")])):
            with self.assertRaises(RuntimeError):
                speech.transcribe_local(self.root, settings)
        with patch("faster_whisper.WhisperModel", scripted_whisper([(5, 10, "Recovered second concept")])):
            report = speech.transcribe_local(self.root, settings)
        self.assertEqual(report["chunks_completed"], 2)
        records = jobs.read_json(self.root / "work/transcript.json")
        self.assertEqual([record["text"] for record in records], ["Retain first concept", "Recovered second concept"])
        self.assertEqual([record["start"] for record in records], [5, 30])

    def test_sdk_interruption_does_not_allow_unconfirmed_chunk_settings_change(self):
        # R-S005: no final speech-state exists yet, but partial checkpoints have an identity.
        settings = self.sdk_settings()
        with patch("faster_whisper.WhisperModel", scripted_whisper([(5, 10, "First concept"), RuntimeError("SDK interrupted")])):
            with self.assertRaises(RuntimeError):
                speech.transcribe_local(self.root, settings)
        settings["chunk_seconds"] = 40
        with patch("faster_whisper.WhisperModel", scripted_whisper([(1, 3, "Changed run one"), (1, 3, "Changed run two")])):
            with self.assertRaises(ValueError):
                speech.transcribe_local(self.root, settings)

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_cleanup_does_not_follow_an_unregistered_work_junction(self):
        # R-S001: empty directory sweeping must not enter another job.
        final, first, second = self.delivered_work()
        outside = self.folder / "other-job"
        keep = outside / "empty-keep"
        keep.mkdir(parents=True)
        (outside / "keep.txt").write_text("Other job", encoding="utf-8")
        junction = self.root / "work/unregistered-junction"
        quote = lambda path: str(path).replace("'", "''")
        command = "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path '{}' -Target '{}' | Out-Null".format(quote(junction), quote(outside))
        created = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True)
        self.assertEqual(created.returncode, 0, created.stderr.decode("utf-8", errors="replace"))
        try:
            plan = jobs.request_cleanup(self.root)
            jobs.confirm_cleanup(self.root, plan["confirmation_token"])
            self.assertTrue(keep.is_dir(), "Another job's empty directory was removed through a junction")
            self.assertTrue((outside / "keep.txt").is_file())
            self.assertTrue(final.is_file())
        finally:
            if junction.exists():
                junction.rmdir()

    @unittest.skipUnless(os.name == "nt", "Windows open-file deletion semantics")
    def test_partial_cleanup_reports_locked_target_and_preserves_final(self):
        final, locked, unlocked = self.delivered_work()
        plan = jobs.request_cleanup(self.root)
        with locked.open("rb"):
            report = jobs.confirm_cleanup(self.root, plan["confirmation_token"])
        self.assertEqual(report["state"], "partially_cleaned")
        self.assertTrue(locked.exists())
        self.assertFalse(unlocked.exists())
        self.assertTrue(final.is_file())
        new_plan = jobs.request_cleanup(self.root)
        self.assertEqual(new_plan["delete_count"], 1)
        self.assertEqual(jobs.confirm_cleanup(self.root, new_plan["confirmation_token"])["state"], "cleaned")

    def test_cleanup_plan_discloses_the_retained_job_control_record(self):
        # The user sees the actual keep/delete scope before reconfirming removal.
        self.delivered_work()
        plan = jobs.request_cleanup(self.root)
        self.assertIn(str((self.root / "job.json").resolve()), plan["keep"])


if __name__ == "__main__":
    unittest.main()
