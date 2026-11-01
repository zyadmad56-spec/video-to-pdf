"""The OS releases the ASR writer lock even after an interrupted process."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from lecture_materials import jobs, speech


class SpeechLockCase(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="tmp-speech-lock-", dir=Path(__file__).parent)
        self.addCleanup(scratch.cleanup)
        source = Path(scratch.name) / "source.mp4"
        source.write_bytes(b"Not decoded in lock tests")
        self.root = Path(scratch.name) / "job"
        jobs.create_job(self.root, str(source), {})

    def test_live_owner_blocks_transcription_and_reset_without_touching_checkpoints(self):
        from lecture_materials.job_locks import speech_lock
        checkpoint = self.root / "work/speech-state.json"
        checkpoint.write_text('{"marker": "retain"}', encoding="utf-8")
        code = ("import sys; sys.path.insert(0, sys.argv[1]); "
                "from lecture_materials import speech; "
                "getattr(speech,sys.argv[3])(sys.argv[2])")
        with speech_lock(self.root):
            for operation in ("transcribe_local", "reset_transcription"):
                result = subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(self.root), operation],
                                        capture_output=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"already running", result.stderr)
                self.assertEqual(checkpoint.read_text(encoding="utf-8"), '{"marker": "retain"}')
        with speech_lock(self.root):
            pass

    def test_process_exit_releases_lock_without_deleting_another_owners_file(self):
        from lecture_materials.job_locks import speech_lock
        code = ("import sys,os; sys.path.insert(0,sys.argv[1]); "
                "from lecture_materials.job_locks import speech_lock; "
                "guard=speech_lock(sys.argv[2]); guard.__enter__(); os._exit(0)")
        subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(self.root)], check=True, timeout=15)
        with speech_lock(self.root):
            pass


if __name__ == "__main__":
    unittest.main()
