"""Independent native-renderer boundary behavior after output handoff."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lecture_materials import jobs, render_pptx


@unittest.skipUnless(os.name == "nt", "Physical PowerPoint export is Windows-specific")
class ReviewerRenderingCase(unittest.TestCase):
    def test_unavailable_renderer_does_not_leave_a_previous_successful_preview(self):
        from pptx import Presentation
        from PIL import Image
        # A host may lose its optional Office binding after a successful run.
        with tempfile.TemporaryDirectory(prefix="tmp-reviewer-stale-", dir=Path(__file__).parent) as scratch:
            folder = Path(scratch).resolve()
            original = folder / "source.pptx"
            deck = Presentation()
            deck.slides.add_slide(deck.slide_layouts[6])
            deck.save(original)
            target = folder / "previews"
            target.mkdir()
            stale = target / "Slide1.PNG"
            Image.new("RGB", (20, 20), "blue").save(stale)
            # Dependency import and executable discovery are external boundaries.
            with patch.dict(sys.modules, {"win32com.client": None}), patch("shutil.which", return_value=None):
                report = render_pptx.export_rendered_slides(original, target, limit=1)
            self.assertFalse(report["available"])
            self.assertFalse(stale.exists(), "An unavailable current render must not expose an old preview as current")

    def test_office_security_configuration_failure_never_opens_original(self):
        class RefusingOfficeBoundary:
            def __init__(self):
                self.Presentations = self
                self.open_attempted = False
            @property
            def AutomationSecurity(self):
                return None
            @AutomationSecurity.setter
            def AutomationSecurity(self, value):
                raise RuntimeError("Office refused macro security configuration")
            def Open(self, *args, **kwargs):
                self.open_attempted = True
                raise RuntimeError("Unsafe Open should never be reached")
            def Quit(self):
                pass
        office = RefusingOfficeBoundary()
        with tempfile.TemporaryDirectory(prefix="tmp-reviewer-security-", dir=Path(__file__).parent) as scratch:
            folder = Path(scratch).resolve()
            with patch("win32com.client.DispatchEx", return_value=office):
                render_pptx.export_rendered_slides(folder / "original.pptx", folder / "previews", limit=1)
        self.assertFalse(office.open_attempted, "A failed macro-security setting cannot fall through to Open")

    def test_reference_automation_disables_macros_before_opening_powerpoint(self):
        # ReadOnly alone does not disable Office macros in an original reference.
        # The external automation boundary refuses Open until security is set.
        from pptx import Presentation
        with tempfile.TemporaryDirectory(prefix="tmp-reviewer-office-", dir=Path(__file__).parent) as scratch:
            folder = Path(scratch).resolve()
            source = folder / "reference.pptx"
            deck = Presentation()
            deck.slides.add_slide(deck.slide_layouts[6])
            deck.save(source)

            class OfficeBoundary:
                AutomationSecurity = None
                def __init__(self):
                    self.Presentations = self
                    self.opened_safely = False
                    self.closed = False
                def Open(self, *args, **kwargs):
                    if self.AutomationSecurity != 3:
                        raise RuntimeError("Original document opened without ForceDisable macro security")
                    self.opened_safely = True
                    return self
                def Export(self, destination, *args):
                    from PIL import Image
                    Path(destination).mkdir(parents=True, exist_ok=True)
                    Image.new("RGB", (20, 20), "white").save(Path(destination) / "Slide1.PNG")
                def Close(self):
                    self.closed = True
                def Quit(self):
                    pass

            office = OfficeBoundary()
            with patch("win32com.client.DispatchEx", return_value=office):
                render_pptx.export_rendered_slides(source, folder / "previews", limit=1)
            self.assertTrue(office.opened_safely, "Original document Open must run with macros force-disabled")
            self.assertTrue(office.closed)

    def test_bounded_slide_export_does_not_leave_unreported_slide_images(self):
        # Unreported preview files would escape generated-work ownership/cleanup.
        from pptx import Presentation
        from pptx.util import Inches
        with tempfile.TemporaryDirectory(prefix="tmp-reviewer-export-", dir=Path(__file__).parent) as scratch:
            folder = Path(scratch).resolve()
            deck = Presentation()
            for index in range(4):
                slide = deck.slides.add_slide(deck.slide_layouts[6])
                slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1)).text = f"Reviewer slide {index + 1}"
            original = folder / "source.pptx"
            deck.save(original)
            digest = jobs.file_digest(original)
            target = folder / "previews"
            report = render_pptx.export_rendered_slides(original, target, limit=2)
            self.assertTrue(report["available"], report.get("error"))
            physical = [path for path in target.iterdir() if path.suffix.lower() == ".png"]
            self.assertEqual(len(report["paths"]), 2)
            self.assertEqual(len(physical), 2, "Unreported images would escape work ownership/cleanup")
            self.assertEqual(jobs.file_digest(original), digest)


if __name__ == "__main__":
    unittest.main()
