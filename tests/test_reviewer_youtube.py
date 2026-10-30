"""Reviewer readiness contracts after the YouTube dependency handoff.

Only executable/version probes and package metadata are simulated. These tests
do not establish network availability or real stream success.
"""

from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lecture_materials import media


@unittest.skipUnless(
    all(Path(media.tool_path(name)).is_file() for name in ("deno", "node")),
    "Runtime-probe fixture needs installed Deno and Node paths; tests never install them",
)
class ReviewerYoutubeReadinessCase(unittest.TestCase):
    def setUp(self):
        # A fresh capability probe, without leaking cached results across cases.
        self.old_runtime = media._JS_RUNTIME_CACHE
        media._JS_RUNTIME_CACHE = None
        self.addCleanup(setattr, media, "_JS_RUNTIME_CACHE", self.old_runtime)

    def probe(self, deno, node="v26.12.1\n", failed_deno=False):
        def execute(arguments, **kwargs):
            name = Path(arguments[0]).stem.lower()
            if name == "deno":
                return subprocess.CompletedProcess(arguments, 1 if failed_deno else 0, deno, "")
            if name == "node":
                return subprocess.CompletedProcess(arguments, 0, node, "")
            return subprocess.CompletedProcess(arguments, 1, "", "Unavailable fixture runtime")
        return patch("subprocess.run", side_effect=execute)

    def test_unsupported_preferred_runtime_falls_back_to_supported_node(self):
        # Installed yt-dlp runtime contract: Deno >=2.3, Node >=22.
        with self.probe("deno 1.0.0\n"):
            selected = media.js_runtime()
        self.assertIsNotNone(selected)
        self.assertEqual(selected["name"], "node", "Unsupported Deno must not shadow a supported Node")

    def test_failed_runtime_probe_is_not_ready(self):
        with self.probe("", node="v18.0.0\n", failed_deno=True):
            selected = media.js_runtime()
        self.assertIsNone(selected, "Failed/unsupported local executables cannot establish readiness")

    def test_downloader_below_required_version_is_not_stream_ready(self):
        def version(package):
            if package == "yt-dlp":
                return "2024.1.1"
            if package == "yt-dlp-ejs":
                return "0.8.0"
            raise media.importlib.metadata.PackageNotFoundError(package)
        with self.probe("deno 2.9.8\n"), patch("importlib.metadata.version", side_effect=version):
            report = media.youtube_readiness()
        self.assertFalse(report["stream_ready"], "Old yt-dlp lacks required safe invocation/component support")
        self.assertTrue(report["setup_hints"])


if __name__ == "__main__":
    unittest.main()
