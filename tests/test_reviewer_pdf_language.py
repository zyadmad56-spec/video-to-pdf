"""Independent regressions after the actual-pilot renderer fix was saved.

Synthetic text/layout fixtures only. No real lecture review or human approval
is manufactured by these rendering checks.
"""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lecture_materials import render_pdf, styles


class ReviewerPdfLanguageCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.runtime = sync_playwright().start()
        try:
            cls.browser, cls.browser_name = render_pdf._launch(cls.runtime)
        except Exception:
            cls.runtime.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="tmp-reviewer-pdf-language-", dir=Path(__file__).parent)
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name).resolve()
        self.style = styles.resolve_style("scholar-ink")
        self.content = {
            "title": "معاينة المراجع", "language": "ar", "direction": "rtl",
            "sections": [{"id": "internal-schema-id-do-not-show-7351",
                          "title": "حساب القدرة المفيدة",
                          "blocks": [{"type": "paragraph",
                                      "text": "نحسب القدرة: `14700 ÷ 8 = 1837.5 W` ثم نراجع النتيجة.",
                                      "evidence": ["synthetic-source"]}]}],
            "omissions": [], "uncertainties": [],
        }

    def page(self, document):
        context = self.browser.new_context(offline=True)
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_content(document, wait_until="load")
        page.emulate_media(media="print")
        page.evaluate("document.fonts.ready")
        return page

    def test_arabic_preview_keeps_schema_ids_out_of_visible_student_text(self):
        document = render_pdf._html_document(self.root, self.content, self.style, preview=True)
        page = self.page(document)
        visible = page.locator("body").inner_text()
        self.assertIn("حساب القدرة المفيدة", visible)
        self.assertNotIn("internal-schema-id-do-not-show-7351", visible)
        self.assertEqual(page.locator("section[id]").count(), 1, "Internal navigation must still work")

    def test_rtl_prose_keeps_whole_inline_division_in_left_to_right_reading_order(self):
        document = render_pdf._html_document(self.root, self.content, self.style, preview=True)
        page = self.page(document)
        geometry = page.locator("code").evaluate("""element => {
            const node = element.firstChild;
            const tokens = ['14700', '÷', '8', '=', '1837.5', 'W'];
            let from = 0;
            const positions = tokens.map(token => {
                const index = node.textContent.indexOf(token, from);
                if (index < 0) throw new Error('Missing expected rendered token');
                const range = document.createRange();
                range.setStart(node, index); range.setEnd(node, index + token.length);
                from = index + token.length;
                return range.getBoundingClientRect().x;
            });
            return { positions, proseDirection: getComputedStyle(element.closest('p')).direction };
        }""")
        self.assertEqual(geometry["proseDirection"], "rtl")
        positions = geometry["positions"]
        self.assertTrue(all(left < right for left, right in zip(positions, positions[1:])),
                        f"The printed formula reverses operands/operators: {positions}")

    def test_section_kicker_stays_on_the_same_printed_page_as_its_heading(self):
        import pymupdf
        page_spec = self.style["page"]
        usable = page_spec["height_mm"] - page_spec["margin_mm"]["top"] - page_spec["margin_mm"]["bottom"]
        css = render_pdf._css(self.style, "ltr", render_pdf.ENGLISH_LABELS)
        document = (
            '<html lang="en"><meta charset="utf-8"><style>' + css + '</style><body>'
            f'<div style="height:{usable - 6:.2f}mm"></div>'
            '<section class="chapter" style="break-before:auto">'
            '<div class="section-kicker">Section 1</div>'
            '<h1>Kicker fixture heading</h1><p>One short explanation.</p></section></body></html>'
        )
        target = self.root / "synthetic-kicker.pdf"
        page = self.page(document)
        page.pdf(path=str(target), width=f"{page_spec['width_mm']}mm",
                 height=f"{page_spec['height_mm']}mm", print_background=True,
                 margin={side: f"{value}mm" for side, value in page_spec["margin_mm"].items()})
        with pymupdf.open(target) as pdf:
            kickers = [n for n, p in enumerate(pdf) if p.search_for("Section 1")]
            headings = [n for n, p in enumerate(pdf) if p.search_for("Kicker fixture heading")]
            self.assertEqual(len(kickers), 1)
            self.assertEqual(len(headings), 1)
            self.assertEqual(kickers, headings, "A section number must not be stranded on the previous page")


if __name__ == "__main__":
    unittest.main()
