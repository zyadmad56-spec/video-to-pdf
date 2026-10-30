"""Independent tests written after GLM's CLI handoff; all jobs are fixtures."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lecture_materials import cli, image_policy, jobs


class ReviewerImageCliCase(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="tmp-reviewer-image-cli-", dir=Path(__file__).parent)
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name).resolve()
        self.root = self.folder / "job with spaces"
        self.source = self.folder / "synthetic-source.mp4"
        self.source.write_bytes(b"Synthetic source, not decoded")
        self.prefs = {"output_language": "mixed", "image_mode": "web", "formats": ["pdf"], "allow_examples": False}

    def invoke(self, command, *args):
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main([command, "--root", str(self.root), *args])
        return code, json.loads(output.getvalue() or errors.getvalue())

    def create(self, **changes):
        prefs = {**self.prefs, **changes}
        jobs.create_job(self.root, str(self.source), prefs)

    def test_missing_mixed_image_choice_blocks_new_job_before_creation(self):
        code, result = self.invoke("init", "--source", str(self.source), "--preferences-json", json.dumps(self.prefs))
        self.assertNotEqual(code, 0)
        self.assertIn("image_language", result["error"])
        self.assertFalse(self.root.exists())

    def test_pure_languages_persist_default_and_do_not_ask_again(self):
        for language in ("ar", "en"):
            with self.subTest(language=language):
                self.root = self.folder / language
                prefs = {**self.prefs, "output_language": language}
                code, result = self.invoke("init", "--source", str(self.source), "--preferences-json", json.dumps(prefs))
                self.assertEqual(code, 0, result)
                self.assertEqual(jobs.load_job(self.root)["preferences"]["image_language"], language)
                code, result = self.invoke("image-intake")
                self.assertEqual(result["image_intake"]["questions"], [])
                self.assertIsNone(result["recorded"])

    def test_text_only_skips_image_language_and_preserves_choice(self):
        self.create(image_mode="text")
        before = (self.root / "job.json").read_bytes()
        code, result = self.invoke("image-intake")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["image_intake"]["questions"], [])
        self.assertNotIn("image_language", result["missing_preferences"])
        self.assertEqual((self.root / "job.json").read_bytes(), before)

    def test_answer_preview_is_read_only_then_apply_is_durable_and_repeat_free(self):
        self.create()
        before = (self.root / "job.json").read_bytes()
        code, result = self.invoke("image-intake", "--image-language", "ar")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["recorded"], {"image_language": "ar"})
        self.assertEqual((self.root / "job.json").read_bytes(), before)
        code, result = self.invoke("image-intake", "--image-language", "ar", "--apply")
        self.assertEqual(code, 0, result)
        self.assertEqual(jobs.load_job(self.root)["preferences"]["image_language"], "ar")
        code, result = self.invoke("image-intake")
        self.assertIsNone(result["recorded"])
        self.assertEqual(result["image_intake"]["questions"], [])

    def test_prepare_refuses_unanswered_choice_before_media_network_or_decode(self):
        self.create()
        with patch.object(cli.media, "prepare_source") as prepare:
            code, result = self.invoke("prepare")
            self.assertNotEqual(code, 0, result)
            prepare.assert_not_called()

    def test_missing_backend_is_an_honest_error_for_search_commands(self):
        self.create(image_language="mixed")
        with patch.object(cli, "image_policy", None), patch.object(cli, "_IMAGE_POLICY_IMPORT_ERROR", RuntimeError("fixture unavailable")):
            code, result = self.invoke("image-search-status")
            self.assertNotEqual(code, 0)
            self.assertEqual(result["code"], "image_policy_unavailable")
            self.assertFalse((self.root / "work/image-search.json").exists())

    def test_unknown_request_is_failure_and_no_decision_is_invented(self):
        self.create(image_language="mixed")
        code, result = self.invoke("image-search-status", "--request-id", "absent")
        self.assertNotEqual(code, 0)
        self.assertEqual(result["code"], "search_request_not_found")
        code, result = self.invoke("image-search-resolve", "--request-id", "absent", "--action", "no_images")
        self.assertNotEqual(code, 0)
        self.assertEqual(result["code"], "decision_not_confirmed")
        self.assertEqual(jobs.load_job(self.root)["preferences"]["image_mode"], "web")

    def test_found_candidate_is_recorded_but_mismatched_pixels_still_reject(self):
        self.create(image_language="ar")
        code, result = self.invoke("image-search-start", "--request-id", "work", "--section", "work", "--query", "قوة وإزاحة")
        self.assertEqual(code, 0, result)
        code, result = self.invoke("image-search-attempt", "--request-id", "work", "--query", "قوة وإزاحة", "--outcome", "found", "--candidate", "candidate-a", "--elapsed-seconds", "1")
        self.assertEqual(code, 0, result)
        self.assertFalse(result["request"]["language_approved"])
        asset = {"id": "candidate-a", "language_review": {"inspected": True, "language": "en", "reason": "Synthetic fixture: only English labels"}}
        code, result = self.invoke("image-intake", "--asset-json", json.dumps(asset))
        self.assertNotEqual(code, 0)
        self.assertEqual(result["code"], "asset_language_invalid")

    def test_image_presence_question_precedes_language_question(self):
        prefs = dict(self.prefs)
        prefs.pop("image_mode")
        jobs.create_job(self.root, str(self.source), prefs)
        code, result = self.invoke("image-intake")
        self.assertEqual(code, 0, result)
        self.assertIn("image_mode", result["missing_preferences"])
        self.assertFalse(any(q.get("id") == "image_language" for q in result["image_intake"]["questions"]),
                         "Do not ask which image language before the user chooses whether images are wanted")


if __name__ == "__main__":
    unittest.main()
