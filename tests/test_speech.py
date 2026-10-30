import importlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class CaptionsCase(unittest.TestCase):
    def test_caption_import_retains_original_language_and_range(self):
        try:
            speech = importlib.import_module("lecture_materials.speech")
        except ModuleNotFoundError:
            self.fail("Caption import and transcript persistence are not implemented")
        from lecture_materials.jobs import create_job, load_job, save_job, read_json
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as scratch:
            folder = Path(scratch)
            video = folder / "original.mp4"
            video.write_bytes(b"source")
            captions = folder / "original.ar.vtt"
            captions.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nشرح Entity\n\n00:01:01.000 --> 00:01:04.000\nشرح Primary key\n", encoding="utf-8")
            root = folder / "job"
            create_job(root, str(video), {})
            job = load_job(root)
            job["media"] = {"start": 60, "end": 80}
            save_job(root, job)
            speech.import_captions(root, captions)
            transcript = read_json(root / "work" / "transcript.json")
            self.assertEqual(len(transcript), 1)
            self.assertEqual(transcript[0]["start"], 61)
            self.assertEqual(transcript[0]["text"], "شرح Primary key")
            self.assertIn("00:01:01.000", (root / "work" / "transcript.txt").read_text(encoding="utf-8"))
            self.assertTrue(captions.exists())


if __name__ == "__main__":
    unittest.main()
