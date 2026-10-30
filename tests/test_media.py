import importlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class MediaCase(unittest.TestCase):
    def setUp(self):
        try:
            self.media = importlib.import_module("lecture_materials.media")
        except ModuleNotFoundError:
            self.fail("Streaming lecture-frame extraction is not implemented")

    def test_tiny_meaningful_change_is_not_deduplicated(self):
        from PIL import Image
        first = Image.new("RGB", (1280, 720), "white")
        changed = first.copy()
        for x in range(10, 13):
            for y in range(10, 13):
                changed.putpixel((x, y), (0, 0, 0))
        self.assertFalse(self.media.same_content(first, changed))
        self.assertTrue(self.media.same_content(first, first.copy()))

    def test_real_video_deduplicates_without_losing_the_end(self):
        from lecture_materials.jobs import create_job, read_json
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as scratch:
            folder = Path(scratch)
            video = folder / "lecture video.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "color=white:s=640x360:r=10:d=12", "-vf",
                            "drawbox=x=20:y=20:w=4:h=4:color=black:t=fill:enable='gte(t,6)'",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
            job = folder / "job"
            # Keep this five-second deduplication/EOF regression explicit;
            # the new default is exercised by independent adaptive media tests.
            create_job(job, str(video), {"image_mode": "text", "frame_interval_seconds": 5})
            self.media.prepare_source(job)
            report = self.media.sample_frames(job)
            self.assertEqual(report["candidates"], 4)
            self.assertEqual(report["retained"], 2)
            self.assertEqual(report["last_requested_time"], 12)
            repeated = self.media.sample_frames(job)
            self.assertEqual(repeated["retained"], 2)
            self.assertEqual(len(list((job / "work" / "frames").glob("*.png"))), 2)


if __name__ == "__main__":
    unittest.main()
