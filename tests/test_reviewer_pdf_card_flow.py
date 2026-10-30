"""Actual printed pagination checks for the new flowing side-card layout."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from lecture_materials import render_pdf, styles


class PdfCardFlowCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser, _ = render_pdf._launch(cls.playwright)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_long_ltr_and_rtl_cards_preserve_each_paragraph_and_table_cell(self):
        import pymupdf
        style = styles.load_builtin('clarity-grid')
        style['layout'].update(cover='panel', section='side-cards')
        paragraphs = [dict(type='paragraph', text=f'CardMarker{index:03} ' +
                      'Maintain balance after each insertion. ' * 18, evidence=[]) for index in range(15)]
        table = dict(type='table', header=['Node', 'Factor'],
                     rows=[[f'NodeMarker{index}', str(-index)] for index in range(12)], evidence=[])
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as scratch:
            folder = Path(scratch)
            for direction in ('ltr', 'rtl'):
                with self.subTest(direction=direction):
                    blocks = paragraphs + [table]
                    content = dict(title='AVL: ' + ('الاتزان' if direction == 'rtl' else 'Balance'),
                        language='mixed' if direction == 'rtl' else 'en', direction=direction,
                        sections=[dict(id='long', title='A long section', blocks=blocks)])
                    document = render_pdf._html_document(folder, content, style)
                    context = self.browser.new_context(offline=True)
                    try:
                        page = context.new_page()
                        page.set_content(document, wait_until='load')
                        page.emulate_media(media='print')
                        self.assertFalse(render_pdf._audit(page)['horizontalOverflow'])
                        pdf_path = folder / f'{direction}.pdf'
                        page.pdf(path=str(pdf_path), format='A4', print_background=True)
                    finally:
                        context.close()
                    with pymupdf.open(pdf_path) as pdf:
                        self.assertGreater(len(pdf), 3)
                        printed = '\n'.join(p.get_text() for p in pdf)
                        self.assertTrue(all(page.get_text().strip() for page in pdf),
                                        'Decoration must not create an empty continuation page')
                        for index in range(15):
                            self.assertIn(f'CardMarker{index:03}', printed)
                        for index in range(12):
                            self.assertIn(f'NodeMarker{index}', printed)
                        for page in pdf:
                            for word in page.get_text('words'):
                                self.assertLessEqual(word[2], page.rect.width + 1)
                                self.assertLessEqual(word[3], page.rect.height + 1)
                    self.assertEqual(render_pdf._section_pages(pdf_path, content), {'long': 3},
                                     'Wrapped side headings must receive real contents-page numbers')

    def test_identical_mixed_headings_and_unicode_ids_use_distinct_section_destinations(self):
        import pymupdf
        style = styles.load_builtin('clarity-grid')
        style['layout']['section'] = 'side-cards'
        title = 'قياس الاتزان | A deliberately wrapped heading'
        content = dict(title='Lecture', language='mixed', direction='rtl', sections=[
            dict(id='first', title=title, blocks=[dict(type='paragraph', text='FIRST_BODY', evidence=[])]),
            dict(id='اتزان-2', title=title, blocks=[dict(type='paragraph', text='SECOND_BODY', evidence=[])])])
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as scratch:
            pdf_path = Path(scratch) / 'destinations.pdf'
            context = self.browser.new_context(offline=True)
            try:
                page = context.new_page()
                page.set_content(render_pdf._html_document(Path(scratch), content, style))
                page.pdf(path=str(pdf_path), format='A4')
            finally:
                context.close()
            mapping = render_pdf._section_pages(pdf_path, content)
            with pymupdf.open(pdf_path) as pdf:
                for identifier, marker in [('first', 'FIRST_BODY'), ('اتزان-2', 'SECOND_BODY')]:
                    expected = next(index + 1 for index, page in enumerate(pdf) if marker in page.get_text())
                    self.assertEqual(mapping[identifier], expected)
            self.assertLess(mapping['first'], mapping['اتزان-2'])


if __name__ == '__main__':
    unittest.main()
