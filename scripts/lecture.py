#!/usr/bin/env python
"""Portable CLI launcher for the lecture-to-materials package.

Usage from any working directory:
    python /path/to/lecture-to-materials/scripts/lecture.py --help
"""

from __future__ import annotations

from pathlib import Path
import sys

PACKAGE_SCRIPTS = Path(__file__).resolve().parent
if str(PACKAGE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SCRIPTS))

from lecture_materials.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
