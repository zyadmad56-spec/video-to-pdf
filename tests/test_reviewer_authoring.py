"""Independent acceptance/regressions written after output-lane handoff.

All files and approval transitions below are synthetic reviewer fixtures.
They never record acceptance of real user lecture material.
"""

import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lecture_materials import authoring, jobs, styles, verification


def readable_pdf(path, text="Synthetic reviewer preview"):
    import pymupdf
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((40, 60), text)
    document.save(path)
    document.close()


class ReviewerAuthoringCase(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="tmp-reviewer-output-", dir=Path(__file__).parent)
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name)
        self.source = self.folder / "original.mp4"
        self.source.write_bytes(b"synthetic unit source, not decoded")
        self.root = self.folder / "job"
        jobs.create_job(self.root, str(self.source), {"image_mode": "user", "allow_examples": False})
        jobs.write_json(self.root / "work/transcript.json", [{
            "id": "audio-1", "kind": "spoken", "text": "An entity has attributes.", "start": 0, "end": 5,
        }])
        self.content = {"title": "Reviewer fixture", "language": "en", "direction": "ltr",
                        "sections": [{"id": "entities", "title": "Entities", "blocks": [{
                            "type": "paragraph", "text": "An entity has attributes.", "evidence": ["audio-1"],
                        }]}], "omissions": [], "uncertainties": []}

    def image(self):
        from PIL import Image
        path = self.root / "work/diagram.png"
        Image.new("RGB", (60, 40), "white").save(path)
        self.asset = {"id": "diagram", "origin": "user", "state": "accepted",
                      "path": "work/diagram.png", "original_path": str(path),
                      "candidate_review": {"inspected": True, "decision": "relevant", "reason": "Entity diagram inspected."},
                      "placements": {"entities": {"inspected_after_placement": True,
                                                   "decision": "relevant", "reason": "Fits the entity explanation."}}}
        self.save_asset()
        self.content["sections"][0]["blocks"].append({"type": "image", "asset_id": "diagram",
                                                     "caption": "Entity diagram", "evidence": ["audio-1"]})
        return path

    def save_asset(self):
        jobs.write_json(self.root / "work/assets.json", [self.asset])

    def accept_synthetic(self):
        readable_pdf(self.root / "work/preview.pdf")
        authoring.record_preview(self.root, self.content, "scholar-ink")
        authoring.accept_preview(self.root, self.content, "scholar-ink", confirmed_by="synthetic reviewer fixture")
        authoring.require_accepted_preview(self.root, self.content, "scholar-ink")

    def test_changed_job_relative_image_invalidates_acceptance(self):
        # R-D001: relative paths previously lost their byte hash.
        from PIL import Image
        path = self.image()
        self.accept_synthetic()
        Image.new("RGB", (60, 40), "black").save(path)
        with self.assertRaises(ValueError):
            authoring.require_accepted_preview(self.root, self.content, "scholar-ink")

    def test_relative_asset_uses_job_bytes_from_another_working_directory(self):
        # R-D003: caller CWD previously selected a same-named foreign image.
        from PIL import Image
        expected = self.image()
        foreign = self.folder / "foreign"
        (foreign / "work").mkdir(parents=True)
        Image.new("RGB", (10, 10), "red").save(foreign / "work/diagram.png")
        previous = Path.cwd()
        try:
            os.chdir(foreign)
            selected = authoring.image_asset(self.root, "diagram", "entities")
        finally:
            os.chdir(previous)
        self.assertEqual(Path(selected["resolved_path"]), expected.resolve())

    def test_missing_actual_inspection_does_not_authorize_an_image(self):
        # R-D004: absence of either explicit inspection flag must fail closed.
        self.image()
        for missing in ("candidate", "placement"):
            with self.subTest(missing=missing):
                original = copy.deepcopy(self.asset)
                if missing == "candidate":
                    self.asset["candidate_review"].pop("inspected")
                else:
                    self.asset["placements"]["entities"].pop("inspected_after_placement")
                self.save_asset()
                with self.assertRaises(ValueError):
                    authoring.validate_content(self.root, self.content)
                self.asset = original

    def test_exclusion_overrides_old_positive_reviews(self):
        self.image()
        self.asset.update(state="excluded", exclusion_reason="Different notation than this lecture.")
        self.save_asset()
        with self.assertRaises(ValueError):
            authoring.validate_content(self.root, self.content)
        self.assertTrue(Path(self.asset["original_path"]).is_file())

    def test_unrelated_excluded_asset_does_not_invalidate_accepted_preview(self):
        self.accept_synthetic()
        jobs.write_json(self.root / "work/assets.json", [{"id": "unused", "state": "excluded",
                                                         "origin": "user", "exclusion_reason": "Unrelated photograph."}])
        self.assertTrue(authoring.require_accepted_preview(self.root, self.content, "scholar-ink")["accepted"])

    def test_partial_or_failed_speech_checkpoint_cannot_authorize_final_output(self):
        # R-D005: nonempty partial transcript is not complete ASR.
        for status in ("partial", "running", "failed"):
            with self.subTest(status=status):
                job = jobs.load_job(self.root)
                job["stages"]["speech"] = {"status": status, "complete": False}
                jobs.save_job(self.root, job)
                jobs.write_json(self.root / "work/speech-state.json", {"status": status, "complete": False})
                with self.assertRaises(ValueError):
                    authoring.require_audio_review(self.root)

    def test_duplicated_timestamp_count_cannot_hide_missing_screen_coverage(self):
        # R-D006: counting candidates alone does not prove the sampling schedule.
        job = jobs.load_job(self.root)
        job["media"] = {"start": 0, "end": 100}
        jobs.save_job(self.root, job)
        jobs.write_json(self.root / "work/screen.json", [{"id": "frame-1", "kind": "screen",
                                                         "status": "reviewed", "text": "Static entity board."}])
        complete = {"complete": True, "interval": 5, "requested_end": 100,
                    "frames": [{"requested": t, "frame_id": "frame-1"} for t in range(0, 101, 5)]}
        jobs.write_json(self.root / "work/frames-state.json", complete)
        self.assertTrue(authoring.require_screen_review(self.root)["coverage_complete"])
        complete["frames"] = [{"requested": 0, "frame_id": "frame-1"}] * 20 + [{"requested": 100, "frame_id": "frame-1"}]
        jobs.write_json(self.root / "work/frames-state.json", complete)
        with self.assertRaises(ValueError):
            authoring.require_screen_review(self.root)

    def test_invalid_picture_policy_cannot_produce_an_ok_verification(self):
        # R-D002: invalid policy previously silently became image-enabled.
        output = self.root / "outputs/reviewer.pdf"
        readable_pdf(output, "Valid selectable output Contents")
        job = jobs.load_job(self.root)
        job["preferences"]["image_mode"] = "mistyped-policy"
        jobs.save_job(self.root, job)
        try:
            report = verification.inspect_outputs(self.root, [output], render=False)
        except ValueError:
            return
        self.assertFalse(report["summary"]["ok"], "An unverified image policy must not be reported as OK")

    def test_normalized_style_round_trip_keeps_the_same_identity(self):
        first = styles.load_builtin("clarity-grid")
        second = styles.normalize_style(first)
        self.assertEqual(first, second)

    def test_teaching_profile_needs_agreement_and_versions_preserve_prior_bytes(self):
        library = self.folder / "library"
        request = {"kind": "teaching_profile", "name": "Small steps", "profile": {"tone": "calm"}}
        with self.assertRaises(ValueError):
            styles.save_named_profile(library, request)
        self.assertEqual(list(library.rglob("*.json")), [])
        request["user_agreed"] = True
        first = styles.save_named_profile(library, request)
        before = Path(first["path"]).read_bytes()
        request.update(collision="new_version", profile={"tone": "direct"})
        second = styles.save_named_profile(library, request)
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(Path(first["path"]).read_bytes(), before)
        self.assertEqual(styles.load_named_profile(library, "teaching_profile", "Small steps")["document"]["preferences"]["tone"], "direct")


if __name__ == "__main__":
    unittest.main()
