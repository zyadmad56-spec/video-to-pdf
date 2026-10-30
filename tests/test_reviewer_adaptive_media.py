"""Independent media acceptance after Argon's frozen handoff.

The source video, PNGs and persistence are real. Only decoder/storage failures
and the forbidden decode on a retry are injected at those I/O boundaries.
"""

import copy
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lecture_materials import authoring, evidence, jobs, media


class AdaptiveMediaCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared_scratch = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        cls.addClassCleanup(cls.shared_scratch.cleanup)
        shared = Path(cls.shared_scratch.name)
        inputs = shared / "encoding-inputs"
        inputs.mkdir()
        for second in range(31):
            image = Image.new("RGB", (320, 180), "white")
            if second in (5, 10):
                image.putpixel((100, 100), (0, 0, 0))
            if second == 10:
                image.putpixel((101, 100), (0, 0, 0))
            image.save(inputs / f"{second:03}.png")
        cls.video = shared / "fresh board.mkv"
        subprocess.run([
            media.tool_path("ffmpeg"), "-v", "error", "-framerate", "1",
            "-i", str(inputs / "%03d.png"), "-c:v", "ffv1", str(cls.video),
        ], check=True, capture_output=True)
        # These only encoded the fixture; recovery must use the video itself.
        for image_path in inputs.iterdir():
            image_path.unlink()
        inputs.rmdir()

    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(scratch.cleanup)
        self.folder = Path(scratch.name)
        self.root = self.folder / "job"
        jobs.create_job(self.root, str(self.video), {"image_mode": "text"})
        self.state_path = self.root / "work/frames-state.json"

    def state(self):
        return jobs.read_json(self.state_path)

    def refine(self, interval, start=0, end=15):
        return media.refine_frames(
            self.root, start, end, interval, "Inspect the transient board detail"
        )

    def review_all_fixture_images(self):
        # Synthetic observations for generated test pixels, never real lectures.
        records = [{"id": item["id"], "status": "reviewed",
                    "observations": "Inspected the synthetic white board and test pixel marks."}
                   for item in evidence.review_packet(self.root, limit=10)["items"]]
        jobs.write_json(self.root / "work/screen.json", records)
        return records

    def test_fresh_default_and_direct_recovery_preserve_one_pixel_difference(self):
        baseline = media.sample_frames(self.root)
        self.assertEqual(baseline["interval"], 15)
        self.assertEqual(baseline["candidates"], 4)
        self.assertEqual(baseline["retained"], 1)
        baseline_rows = self.state()["frames"]
        ten = self.refine(10)
        five = self.refine(5)
        self.assertEqual(ten["newly_decoded_times"], [10])
        self.assertEqual(five["newly_decoded_times"], [5])
        state = self.state()
        self.assertEqual(state["frames"], baseline_rows)
        self.assertEqual([r["requested"] for r in state["refinements"]], [10, 5])
        self.assertEqual(len(state["representatives"]), 3)
        for record in state["refinements"]:
            self.assertAlmostEqual(record["decoded"], record["requested"], places=3)
            with Image.open(self.root / record["path"]) as image:
                self.assertEqual(image.convert("RGB").getpixel((100, 100)), (0, 0, 0))
                expected = (0, 0, 0) if record["requested"] == 10 else (255, 255, 255)
                self.assertEqual(image.convert("RGB").getpixel((101, 100)), expected)
        packet = evidence.review_packet(self.root, limit=10)
        self.assertEqual({r["id"] for r in packet["items"]},
                         {r["frame_id"] for r in state["representatives"]})
        self.assertTrue(all(r["review_status"] == "pending" for r in packet["items"]))
        counts = evidence.evidence_status(self.root)["frames"]
        self.assertEqual((counts["baseline_extracted"], counts["refinement_extracted"],
                          counts["retained"]), (4, 2, 3))

    def test_completed_retry_does_not_decode_write_or_duplicate_occurrences(self):
        media.sample_frames(self.root)
        self.refine(10)
        before = (self.state_path.read_bytes(), (self.root / "job.json").read_bytes())
        files = {p.name: jobs.file_digest(p) for p in (self.root / "work/frames").iterdir()}
        with patch.object(media, "frame_at", side_effect=AssertionError("Unexpected decoder I/O")):
            repeated = self.refine(10)
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["newly_decoded_times"], [])
        self.assertEqual(before, (self.state_path.read_bytes(), (self.root / "job.json").read_bytes()))
        self.assertEqual(files, {p.name: jobs.file_digest(p)
                                 for p in (self.root / "work/frames").iterdir()})

    def test_refinement_does_not_move_baseline_resume_position(self):
        partial = media.sample_frames(self.root, limit=2)
        self.assertFalse(partial["complete"])
        prefix = self.state()["frames"]
        self.refine(10)
        self.refine(5)
        finished = media.sample_frames(self.root)
        state = self.state()
        self.assertTrue(finished["complete"])
        self.assertEqual(state["frames"][:2], prefix)
        self.assertEqual([r["requested"] for r in state["frames"]], [0, 15, 30, 31])
        self.assertEqual([r["requested"] for r in state["refinements"]], [10, 5])

    def test_empty_short_window_levels_complete_without_decoding(self):
        media.sample_frames(self.root)
        with patch.object(media, "frame_at", side_effect=AssertionError("Empty plan decoded")):
            ten = self.refine(10, 30, 31)
            five = self.refine(5, 30, 31)
        self.assertEqual(ten["planned_times"], [])
        self.assertEqual(five["planned_times"], [])
        self.assertEqual(five["completed_intervals"], [10, 5])
        self.assertEqual(self.state()["refinements"], [])

    def test_duplicate_recovery_reuses_image_and_preserves_existing_binding(self):
        media.sample_frames(self.root)
        job = jobs.load_job(self.root)
        job["preview"] = {"accepted": True, "test_binding": "unchanged"}
        jobs.save_job(self.root, job)
        before_preview = job["preview"]
        self.refine(10, 15, 30)
        five = self.refine(5, 15, 30)
        self.assertEqual(five["retained"], 1)
        self.assertEqual(five["refinement_duplicates"], 2)
        self.assertEqual(len(list((self.root / "work/frames").glob("*.png"))), 1)
        self.assertEqual(jobs.load_job(self.root)["preview"], before_preview)

    def test_invalid_requests_leave_existing_job_and_state_unchanged(self):
        media.sample_frames(self.root)
        before = (self.state_path.read_bytes(), (self.root / "job.json").read_bytes())
        cases = (
            (0, 15, 5, "observation"), (0, 15, 1, "observation"),
            (0, 15, True, "observation"), (False, 15, 10, "observation"),
            (0, float("nan"), 10, "observation"), (0, 30, 10, "observation"),
            (1, 15, 10, "observation"), (0, 15, 10, " "),
            (0, 15, 10.0000005, "observation"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises((TypeError, ValueError)):
                    media.refine_frames(self.root, *arguments)
                self.assertEqual(before, (self.state_path.read_bytes(),
                                          (self.root / "job.json").read_bytes()))

    def test_failed_completion_checkpoint_resumes_without_redecoding_success(self):
        media.sample_frames(self.root)
        real_write = media.write_json

        def interrupted_completion(path, document):
            if Path(path) == self.state_path and document.get("adaptive_windows"):
                raise OSError("Injected disk failure at completion checkpoint")
            return real_write(path, document)

        with patch.object(media, "write_json", side_effect=interrupted_completion):
            with self.assertRaisesRegex(OSError, "Injected disk failure"):
                self.refine(10)
        self.assertEqual([r["requested"] for r in self.state()["refinements"]], [10])
        self.assertEqual(self.state()["adaptive_windows"], {})
        with patch.object(media, "frame_at", side_effect=AssertionError("Checkpoint was lost")):
            resumed = self.refine(10)
        self.assertEqual(resumed["newly_decoded_times"], [])
        self.assertEqual(resumed["completed_intervals"], [10])
        self.assertEqual(len(self.state()["refinements"]), 1)

    def test_changed_or_unprepared_source_cannot_refine(self):
        before = (self.root / "job.json").read_bytes()
        with self.assertRaises(ValueError):
            self.refine(10)
        self.assertEqual(before, (self.root / "job.json").read_bytes())
        replacement = self.folder / "replaceable.mkv"
        shutil.copyfile(self.video, replacement)
        second_root = self.folder / "replacement-job"
        jobs.create_job(second_root, str(replacement), {"image_mode": "text"})
        media.sample_frames(second_root)
        state_bytes = (second_root / "work/frames-state.json").read_bytes()
        with replacement.open("ab") as stream:
            stream.write(b"changed identity")
        with self.assertRaises(jobs.SourceChangedError):
            media.refine_frames(second_root, 0, 15, 10, "Observed occlusion")
        self.assertEqual(state_bytes, (second_root / "work/frames-state.json").read_bytes())

    def test_reset_removes_registered_recovery_and_preserves_originals_and_pdf(self):
        media.sample_frames(self.root)
        source_hash = jobs.file_digest(self.video)
        attachment = self.folder / "original-reference.pdf"
        attachment.write_bytes(b"original attachment sentinel")
        output = self.root / "outputs/saved.pdf"
        output.write_bytes(b"delivered PDF sentinel")
        job = jobs.load_job(self.root)
        job.update(preview={"accepted": True}, delivered=True,
                   outputs={"pdf": "outputs/saved.pdf"},
                   attachments=[{"path": str(attachment), "role": "content"}])
        jobs.save_job(self.root, job)
        self.refine(10)
        changed = jobs.load_job(self.root)
        self.assertIsNone(changed["preview"])
        self.assertFalse(changed["delivered"])
        self.assertEqual(changed["outputs"], {})
        screen = self.root / "work/screen.json"
        jobs.write_json(screen, [])
        jobs.register_generated(self.root, screen)
        media.reset_frames(self.root)
        self.assertFalse(self.state_path.exists())
        self.assertFalse(screen.exists())
        self.assertEqual(list((self.root / "work/frames").glob("*.png")), [])
        self.assertEqual(jobs.file_digest(self.video), source_hash)
        self.assertEqual(attachment.read_bytes(), b"original attachment sentinel")
        self.assertEqual(output.read_bytes(), b"delivered PDF sentinel")

    def test_new_recovery_requires_its_own_review_and_unreadable_reason(self):
        media.sample_frames(self.root)
        reviews = self.review_all_fixture_images()
        authoring.require_screen_review(self.root)
        extra_id = self.refine(10)["new_unique_frame_ids"][0]
        with self.assertRaises(ValueError):
            authoring.require_screen_review(self.root)
        reviews.append({"id": extra_id, "status": "unreadable"})
        jobs.write_json(self.root / "work/screen.json", reviews)
        with self.assertRaises(ValueError):
            authoring.require_screen_review(self.root)
        reviews[-1]["uncertainty"] = "Synthetic fixture detail could not be interpreted."
        jobs.write_json(self.root / "work/screen.json", reviews)
        report = authoring.require_screen_review(self.root)
        self.assertEqual(report["reviewed_representatives"], 2)
        self.assertEqual((report["baseline_candidates"], report["refinement_candidates"]), (4, 1))

    def test_corrupt_recovery_metadata_cannot_pass_final_readiness(self):
        media.sample_frames(self.root)
        self.refine(10)
        original = self.state()
        reviews = self.review_all_fixture_images()
        authoring.require_screen_review(self.root)
        outside = self.folder / "outside-job.png"
        shutil.copyfile(self.root / original["refinements"][0]["path"], outside)

        def outside_image(state):
            row = state["refinements"][0]
            row["path"] = str(outside)
            for representative in state["representatives"]:
                if representative["frame_id"] == row["frame_id"]:
                    representative["path"] = str(outside)

        def wrong_window_key(state):
            entry = next(iter(state["adaptive_windows"].values()))
            state["adaptive_windows"] = {"forged-key": entry}

        mutations = (
            ("nonfinite decoded", lambda s: s["refinements"][0].update(decoded=float("nan"))),
            ("negative decoded", lambda s: s["refinements"][0].update(decoded=-1)),
            ("missing actual decoded time", lambda s: (
                s["refinements"][0].pop("decoded"), s["refinements"][0].pop("timestamp"))),
            ("approximate level", lambda s: s["refinements"][0].update(interval=10.0000005)),
            ("missing representatives", lambda s: s.update(representatives=[])),
            ("wrong window key", wrong_window_key),
            ("outside image", outside_image),
            ("duplicate request", lambda s: s["refinements"].append(copy.deepcopy(s["refinements"][0]))),
        )
        for label, mutate in mutations:
            with self.subTest(corruption=label):
                state = copy.deepcopy(original)
                mutate(state)
                jobs.write_json(self.state_path, state)
                with self.assertRaises(ValueError):
                    authoring.require_screen_review(self.root)
        state = copy.deepcopy(original)
        state["refinements"] = []
        jobs.write_json(self.state_path, state)
        baseline_ids = {row["frame_id"] for row in state["frames"]}
        jobs.write_json(self.root / "work/screen.json", [r for r in reviews if r["id"] in baseline_ids])
        with self.assertRaises(ValueError):
            authoring.require_screen_review(self.root)

    def test_checkpointed_but_unfinished_recovery_blocks_final_readiness(self):
        media.sample_frames(self.root)
        real_write = media.write_json

        def incomplete_level(path, document):
            if Path(path) == self.state_path and document.get("adaptive_windows"):
                raise OSError("Completion write interrupted")
            return real_write(path, document)

        with patch.object(media, "write_json", side_effect=incomplete_level):
            with self.assertRaises(OSError):
                self.refine(10)
        self.review_all_fixture_images()
        with self.assertRaises(ValueError):
            authoring.require_screen_review(self.root)
        self.refine(10)
        self.assertTrue(authoring.require_screen_review(self.root)["coverage_complete"])

    def test_reviewed_recovery_does_not_hide_missing_baseline_or_missing_png(self):
        media.sample_frames(self.root)
        self.refine(10)
        self.refine(5)
        original = self.state()
        self.review_all_fixture_images()
        authoring.require_screen_review(self.root)
        broken_grid = copy.deepcopy(original)
        broken_grid["frames"].pop(1)
        jobs.write_json(self.state_path, broken_grid)
        with self.assertRaises(ValueError):
            authoring.require_screen_review(self.root)
        jobs.write_json(self.state_path, original)
        (self.root / original["refinements"][0]["path"]).unlink()
        with self.assertRaises(ValueError):
            authoring.require_screen_review(self.root)

    def test_interrupted_baseline_status_reports_actual_prefix(self):
        real_decode = media.frame_at

        def interrupted_decode(prepared_media, requested):
            if requested == 15:
                raise ValueError("Injected decoder failure")
            return real_decode(prepared_media, requested)

        with patch.object(media, "frame_at", side_effect=interrupted_decode):
            with self.assertRaisesRegex(ValueError, "Injected decoder failure"):
                media.sample_frames(self.root)
        self.assertEqual([r["requested"] for r in self.state()["frames"]], [0])
        status = evidence.evidence_status(self.root)["frames"]
        self.assertEqual((status["baseline_candidates"], status["baseline_extracted"]), (4, 1))


if __name__ == "__main__":
    unittest.main()
