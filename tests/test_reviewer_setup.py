"""Black-box Windows setup failure behavior; no network or global installs."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "PowerShell setup is Windows-specific")
class ReviewerSetupCase(unittest.TestCase):
    def test_failed_dependency_install_never_reports_success(self):
        # A real isolated venv without pip makes every dependency command fail.
        # This exercises the setup script's native-command exit handling.
        with tempfile.TemporaryDirectory(prefix="tmp-reviewer-setup-", dir=Path(__file__).parent) as scratch:
            root = Path(scratch).resolve()
            source = Path(__file__).resolve().parents[1] / "setup.ps1"
            shutil.copyfile(source, root / "setup.ps1")
            (root / "requirements.txt").write_text("reviewer-does-not-exist-offline==0\n", encoding="utf-8")
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")],
                           check=True, capture_output=True, timeout=60)
            environment = dict(os.environ, PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
            completed = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                        "-File", str(root / "setup.ps1"), "-SkipYoutube"],
                                       cwd=root, env=environment, capture_output=True, timeout=60)
            stdout = completed.stdout.decode("utf-8", errors="replace")
            stderr = completed.stderr.decode("utf-8", errors="replace")
            self.assertNotEqual(completed.returncode, 0, f"Setup reported success despite failed pip commands:\n{stdout}\n{stderr}")


if __name__ == "__main__":
    unittest.main()
