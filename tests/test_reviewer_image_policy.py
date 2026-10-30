"""Independent post-handoff language/search contracts, not OCR accuracy tests.

All source records, candidate reviews, time shifts and approvals are synthetic
reviewer fixtures. The actual lecture trial is tracked separately.
"""

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lecture_materials import authoring, image_policy, jobs
from test_reviewer_authoring import readable_pdf


class FixtureClock(datetime):
    current = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(cls.current.timestamp(), tz=tz or timezone.utc)


class ReviewerImagePolicyCase(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="tmp-reviewer-image-policy-", dir=Path(__file__).parent)
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name).resolve()
        self.source = self.folder / "synthetic-source.mp4"
        self.source.write_bytes(b"Synthetic unit source; not decoded")
        self.root = self.folder / "job"
        jobs.create_job(self.root, str(self.source), {"output_language": "mixed", "image_mode": "user",
                                                      "image_language": "mixed", "allow_examples": False})
        FixtureClock.current = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        self.clock_patch = patch("lecture_materials.image_policy.datetime", FixtureClock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.request = {"request_id": "efficiency-images", "section_id": "efficiency",
                        "query": "bilingual useful output input efficiency diagram"}

    def advance(self, seconds):
        FixtureClock.current += timedelta(seconds=seconds)

    def start(self):
        return image_policy.start_search(self.root, self.request)

    def exhaust(self):
        self.start()
        self.advance(600)
        return image_policy.search_status(self.root, self.request["request_id"])

    def preferences(self):
        return jobs.load_job(self.root)["preferences"]

    def test_intake_keeps_document_language_separate_from_image_source_mode(self):
        cases = [
            ({"output_language": "mixed", "image_mode": "web"}, ["image_language"], None),
            ({"output_language": "mixed", "image_mode": "text"}, [], None),
            ({"output_language": "ar", "image_mode": "web"}, [], "ar"),
            ({"output_language": "en", "image_mode": "user"}, [], "en"),
            ({"output_language": "mixed", "image_mode": "mixed", "image_language": "ar"}, [], "ar"),
        ]
        for prefs, missing, target in cases:
            with self.subTest(prefs=prefs):
                original = copy.deepcopy(prefs)
                report = image_policy.intake_requirements(prefs)
                self.assertEqual(prefs, original)
                self.assertEqual(report["missing"], missing)
                self.assertEqual(report["normalized"].get("image_language"), target)
                self.assertEqual(report["normalized"]["image_mode"], prefs["image_mode"])

    def test_only_matching_actually_inspected_label_attestations_are_accepted(self):
        for requested in ("ar", "en", "mixed"):
            for observed in ("ar", "en", "mixed", "none", "unreadable"):
                asset = {"id": "fixture", "language_review": {"inspected": True, "language": observed,
                                                                "reason": "Synthetic fixture label attestation"}}
                prefs = {"image_mode": "web", "image_language": requested}
                with self.subTest(requested=requested, observed=observed):
                    if requested == observed:
                        self.assertTrue(image_policy.validate_asset_language(prefs, asset)["matched"])
                    else:
                        with self.assertRaises(ValueError):
                            image_policy.validate_asset_language(prefs, asset)
        for review in (None, {"language": "mixed", "reason": "Known fixture"},
                       {"inspected": True, "language": "mixed", "reason": ""}):
            with self.subTest(review=review), self.assertRaises(ValueError):
                image_policy.validate_asset_language(self.preferences(), {"id": "fixture", "language_review": review})

    def test_boundary_pause_persists_and_unconfirmed_choice_does_not_resume(self):
        self.start()
        self.advance(599)
        before = image_policy.search_status(self.root, self.request["request_id"])
        self.assertEqual(before["status"], "searching")
        self.advance(1)
        paused = image_policy.search_status(self.root, self.request["request_id"])
        self.assertEqual(paused["status"], "awaiting_decision")
        self.assertEqual(paused["elapsed_seconds"], 600)
        self.assertTrue(paused["question"])
        self.advance(3600)
        self.assertEqual(image_policy.search_status(self.root, self.request["request_id"])["elapsed_seconds"], 600)
        with self.assertRaises(ValueError):
            image_policy.resolve_search(self.root, {"request_id": self.request["request_id"],
                                                   "action": "continue_search", "user_confirmed": False})
        self.assertEqual(image_policy.search_status(self.root, self.request["request_id"])["status"], "awaiting_decision")
        self.assertIn("work/image-search.json", jobs.load_job(self.root)["generated"])

    def test_repeat_start_and_polling_cannot_reset_an_active_round(self):
        self.start()
        self.advance(100)
        first = image_policy.search_status(self.root, self.request["request_id"])
        resumed = self.start()
        self.assertEqual(resumed["round"], 1)
        self.assertGreaterEqual(resumed["elapsed_seconds"], first["elapsed_seconds"])
        self.advance(500)
        self.assertEqual(image_policy.search_status(self.root, self.request["request_id"])["status"], "awaiting_decision")

    def test_finished_query_does_not_charge_independent_authoring_idle_time(self):
        # A five-second failed search followed by ten minutes writing notes is
        # five active search seconds, not permission to claim a ten-minute search.
        self.start()
        self.advance(5)
        image_policy.record_search_attempt(self.root, {"request_id": self.request["request_id"],
            "query": self.request["query"], "outcome": "not_found", "candidate_ids": [], "elapsed_seconds": 5})
        self.advance(600)
        status = image_policy.search_status(self.root, self.request["request_id"])
        self.assertEqual(status["elapsed_seconds"], 5, "Completed queries must close their active clock interval")
        self.assertEqual(status["status"], "searching")

    def test_confirmed_continue_starts_a_new_budget_and_retains_history(self):
        self.exhaust()
        self.advance(2400)
        resumed = image_policy.resolve_search(self.root, {"request_id": self.request["request_id"],
            "action": "continue_search", "user_confirmed": True})
        self.assertEqual(resumed["round"], 2)
        self.assertEqual(resumed["elapsed_seconds"], 0)
        self.assertEqual(resumed["total_elapsed_seconds"], 600)
        self.assertEqual(len(resumed["decisions"]), 1)

    def test_resume_after_completed_query_preserves_accumulated_search_time(self):
        self.start()
        self.advance(5)
        image_policy.record_search_attempt(self.root, {"request_id": self.request["request_id"],
            "query": self.request["query"], "outcome": "not_found", "elapsed_seconds": 5})
        self.advance(1000)
        image_policy.start_search(self.root, self.request)
        self.advance(12)
        status = image_policy.search_status(self.root, self.request["request_id"])
        self.assertEqual(status["elapsed_seconds"], 17)
        self.assertTrue(status["search_interval_open"])

    def test_polling_and_measured_completion_do_not_double_charge(self):
        self.start()
        self.advance(3)
        self.assertEqual(image_policy.search_status(self.root, self.request["request_id"])["elapsed_seconds"], 3)
        self.advance(2)
        result = image_policy.record_search_attempt(self.root, {"request_id": self.request["request_id"],
            "query": self.request["query"], "outcome": "not_found", "elapsed_seconds": 5})
        self.assertEqual(result["elapsed_seconds"], 5)
        self.assertFalse(result["search_interval_open"])

    def test_new_authorized_round_does_not_charge_before_next_query(self):
        self.exhaust()
        image_policy.resolve_search(self.root, {"request_id": self.request["request_id"],
            "action": "continue_search", "user_confirmed": True})
        self.advance(1200)
        status = image_policy.search_status(self.root, self.request["request_id"])
        self.assertEqual(status["elapsed_seconds"], 0)
        self.assertEqual(status["total_elapsed_seconds"], 600)
        self.assertEqual(status["status"], "searching")

    def test_timestamped_completion_charges_tail_after_an_intermediate_poll(self):
        started = FixtureClock.current.isoformat()
        self.start()
        self.advance(3)
        self.assertEqual(image_policy.search_status(self.root, self.request["request_id"])["elapsed_seconds"], 3)
        self.advance(2)
        result = image_policy.record_search_attempt(self.root, {"request_id": self.request["request_id"],
            "query": self.request["query"], "outcome": "not_found", "started_at": started,
            "finished_at": FixtureClock.current.isoformat()})
        self.assertEqual(result["elapsed_seconds"], 5,
                         "A completed five-second timestamped query must include its two seconds after polling")

    def test_legacy_ledger_preserves_history_without_inventing_active_time(self):
        self.start()
        self.advance(25)
        image_policy.search_status(self.root, self.request["request_id"])
        path = self.root / "work/image-search.json"
        state = jobs.read_json(path)
        for key in ("search_interval_open", "interval_started_at", "interval_ended_at", "interval_accounted_seconds"):
            state["requests"][0].pop(key, None)
        jobs.write_json(path, state)
        self.advance(10000)
        status = image_policy.search_status(self.root, self.request["request_id"])
        self.assertEqual(status["elapsed_seconds"], 25)
        self.assertFalse(status["search_interval_open"])

    def test_lone_start_timestamp_respects_a_start_later_than_last_poll(self):
        self.start()
        self.advance(3)
        image_policy.search_status(self.root, self.request["request_id"])
        self.advance(1)
        actual_started = FixtureClock.current.isoformat()
        self.advance(1)
        result = image_policy.record_search_attempt(self.root, {"request_id": self.request["request_id"],
            "query": self.request["query"], "outcome": "not_found", "started_at": actual_started})
        self.assertEqual(result["elapsed_seconds"], 4,
                         "Previously charged3 seconds plus the actual1-second tail; do not charge the gap")

    def test_language_fallbacks_require_confirmation_and_no_images_stays_picture_free(self):
        for action, language in (("use_arabic", "ar"), ("use_english", "en"), ("no_images", None)):
            with self.subTest(action=action):
                self.root = self.folder / action
                jobs.create_job(self.root, str(self.source), {"output_language": "mixed", "image_mode": "web",
                                                          "image_language": "mixed", "allow_examples": False})
                self.exhaust()
                report = image_policy.resolve_search(self.root, {"request_id": self.request["request_id"],
                    "action": action, "user_confirmed": True})
                prefs = self.preferences()
                if language:
                    self.assertEqual(prefs["image_language"], language)
                    self.assertEqual(report["language"], language)
                else:
                    self.assertEqual(authoring.image_mode(self.root), "text")
                    self.assertTrue(report["requires_content_regeneration"])

    def test_same_section_cannot_reset_budget_with_another_request_id(self):
        self.start()
        with self.assertRaises(ValueError):
            image_policy.start_search(self.root, {**self.request, "request_id": "reset-bypass"})

    def test_search_found_is_not_automatic_language_or_relevance_approval(self):
        self.start()
        update = {"request_id": self.request["request_id"], "query": self.request["query"],
                  "outcome": "found", "candidate_ids": []}
        with self.assertRaises(ValueError):
            image_policy.record_search_attempt(self.root, update)
        update["candidate_ids"] = ["synthetic-candidate"]
        result = image_policy.record_search_attempt(self.root, update)
        self.assertEqual(result["status"], "found")
        self.assertFalse(result["language_approved"])
        self.assertFalse(result["relevance_approved"])

    def test_external_language_change_needs_explicit_reconciliation(self):
        self.start()
        job = jobs.load_job(self.root)
        job["preferences"]["image_language"] = "en"
        jobs.save_job(self.root, job)
        status = image_policy.search_status(self.root, self.request["request_id"])
        self.assertTrue(status["requires_reconciliation"])
        with self.assertRaises(ValueError):
            image_policy.resolve_search(self.root, {"request_id": self.request["request_id"],
                "action": "continue_search", "user_confirmed": True})

    def test_used_language_review_changes_invalidate_the_shown_preview(self):
        from PIL import Image
        path = self.root / "work/fixture.png"
        Image.new("RGB", (100, 60), "white").save(path)
        jobs.write_json(self.root / "work/transcript.json", [{"id": "audio-1", "kind": "spoken", "start": 0,
                        "end": 5, "text": "Synthetic efficiency definition"}])
        asset = {"id": "diagram", "origin": "user", "state": "accepted", "path": "work/fixture.png",
                 "candidate_review": {"inspected": True, "decision": "relevant", "reason": "Synthetic matching concept"},
                 "language_review": {"inspected": True, "language": "mixed", "reason": "Synthetic label review version one"},
                 "placements": {"efficiency": {"inspected_after_placement": True, "decision": "relevant",
                                               "reason": "Synthetic section placement review"}}}
        jobs.write_json(self.root / "work/assets.json", [asset])
        content = {"title": "Synthetic language fixture", "language": "mixed", "direction": "rtl", "sections": [
            {"id": "efficiency", "title": "Efficiency", "blocks": [
                {"type": "paragraph", "text": "Synthetic definition", "evidence": ["audio-1"]},
                {"type": "image", "asset_id": "diagram", "caption": "Synthetic caption", "evidence": ["audio-1"]}]}],
            "omissions": [], "uncertainties": []}
        readable_pdf(self.root / "work/preview.pdf")
        authoring.record_preview(self.root, content, "scholar-ink")
        authoring.accept_preview(self.root, content, "scholar-ink", confirmed_by="synthetic reviewer fixture")
        asset["language_review"]["reason"] = "Synthetic revised label inspection after preview"
        jobs.write_json(self.root / "work/assets.json", [asset])
        with self.assertRaises(ValueError):
            authoring.require_accepted_preview(self.root, content, "scholar-ink")


if __name__ == "__main__":
    unittest.main()
