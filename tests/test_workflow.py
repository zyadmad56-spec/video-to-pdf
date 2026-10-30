import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class JobCase(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name)
        try:
            self.core = importlib.import_module("lecture_materials.jobs")
        except ModuleNotFoundError:
            self.fail("The persistent job workflow has not been implemented")
        self.source = self.folder / "original video.mp4"
        self.source.write_bytes(b"original input")
        self.root = self.folder / "job"
        self.job = self.core.create_job(self.root, str(self.source), {"image_mode": "text"})

    def generated(self, name, contents=b"generated"):
        path = self.root / "work" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        self.core.register_generated(self.root, path)
        return path

    def delivered(self):
        final = self.root / "outputs" / "lecture.pdf"
        final.parent.mkdir(exist_ok=True)
        final.write_bytes(b"%PDF-1.7 final output")
        self.core.mark_delivered(self.root, [final])
        return final

    def test_source_and_preferences_survive_a_fresh_read(self):
        self.assertEqual(self.core.load_job(self.root)["source"], str(self.source.resolve()))
        self.assertEqual(self.core.load_job(self.root)["preferences"]["image_mode"], "text")

    def test_job_creation_does_not_overwrite_an_existing_job(self):
        with self.assertRaises(FileExistsError):
            self.core.create_job(self.root, str(self.source), {})

    def test_cleanup_cannot_be_requested_before_delivery(self):
        self.generated("transcript.txt")
        with self.assertRaises(ValueError):
            self.core.request_cleanup(self.root)

    def test_delete_choice_does_not_delete_until_confirmation(self):
        transcript = self.generated("transcript.txt")
        self.delivered()
        plan = self.core.request_cleanup(self.root)
        self.assertTrue(transcript.exists())
        self.assertIn(str(transcript.resolve()), plan["delete"])
        self.assertTrue(plan["confirmation_token"])

    def test_confirmed_cleanup_preserves_original_and_final(self):
        transcript = self.generated("transcript.txt")
        self.generated("frames/one.png")
        final = self.delivered()
        plan = self.core.request_cleanup(self.root)
        self.core.confirm_cleanup(self.root, plan["confirmation_token"])
        self.assertFalse(transcript.exists())
        self.assertTrue(final.exists())
        self.assertEqual(self.source.read_bytes(), b"original input")

    def test_cleanup_rejects_content_changed_since_confirmation_request(self):
        transcript = self.generated("transcript.txt")
        self.delivered()
        plan = self.core.request_cleanup(self.root)
        transcript.write_text("changed", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.core.confirm_cleanup(self.root, plan["confirmation_token"])
        self.assertTrue(transcript.exists())

    def test_tampered_manifest_cannot_delete_an_outside_file(self):
        self.delivered()
        job = self.core.load_job(self.root)
        job["generated"] = ["../original video.mp4"]
        self.core.save_job(self.root, job)
        with self.assertRaises(ValueError):
            self.core.request_cleanup(self.root)
        self.assertTrue(self.source.exists())


class TimingCase(unittest.TestCase):
    def setUp(self):
        try:
            self.timing = importlib.import_module("lecture_materials.timing")
        except ModuleNotFoundError:
            self.fail("Full-duration timing and caption parsing have not been implemented")

    def test_sampling_covers_more_than_one_hundred_frames_and_endpoint(self):
        samples = list(self.timing.sample_times(0, 7200.25, 5))
        self.assertEqual(len(samples), 1442)
        self.assertEqual(samples[:3], [0, 5, 10])
        self.assertEqual(samples[-1], 7200.25)

    def test_selected_range_keeps_absolute_timestamps(self):
        self.assertEqual(list(self.timing.sample_times(60, 71, 5)), [60, 65, 70, 71])

    def test_invalid_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            list(self.timing.sample_times(0, 5, 0))

    def test_vtt_keeps_mixed_language_and_removes_rolling_repetition(self):
        captions = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nEntity معناها كيان\n\n00:00:02.500 --> 00:00:05.000\nEntity معناها كيان وليه attributes\n"
        segments = self.timing.parse_captions(captions)
        self.assertEqual(segments[0]["text"], "Entity معناها كيان")
        self.assertEqual(segments[1]["text"], "وليه attributes")
        self.assertEqual(segments[1]["start"], 2.5)

    def test_later_repeated_phrase_is_not_deleted(self):
        captions = "1\n00:00:01,000 --> 00:00:02,000\nPrimary key\n\n2\n00:01:01,000 --> 00:01:02,000\nPrimary key\n"
        self.assertEqual(len(self.timing.parse_captions(captions)), 2)


if __name__ == "__main__":
    unittest.main()
