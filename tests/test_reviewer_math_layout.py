"""Independent regressions after GLM's frozen 2026-10-27 handoff.

All source/preview fixtures are synthetic. These tests cannot accept the real
lecture or establish that its audio, screen, or final document was reviewed.
"""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lecture_materials import authoring, jobs, render_pdf, render_pptx, styles


def eta_tree():
    def power(label):
        return {"type": "subscript", "base": {"type": "identifier", "name": "P"},
                "sub": {"type": "text", "value": label}}
    return {"type": "row", "children": [
        {"type": "identifier", "name": "η"}, {"type": "operator", "symbol": "="},
        {"type": "fraction", "numerator": power("out"), "denominator": power("in")},
    ]}


class ReviewerMathFixture(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="tmp-reviewer-math-", dir=Path(__file__).parent)
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name).resolve()
        source = self.folder / "synthetic.mp4"
        source.write_bytes(b"synthetic fixture, not actual lecture media")
        self.root = self.folder / "job"
        jobs.create_job(self.root, str(source), {"image_mode": "user", "allow_examples": False})
        jobs.write_json(self.root / "work/transcript.json", [
            {"id": "audio-1", "kind": "spoken", "text": "Synthetic efficiency explanation", "start": 0, "end": 1},
        ])
        self.block = {"type": "equation", "text": "η = P_out / P_in", "math": eta_tree(), "evidence": ["audio-1"]}
        self.content = {"title": "Synthetic reviewer math", "language": "ar", "direction": "rtl",
                        "sections": [{"id": "efficiency", "title": "الكفاءة", "blocks": [self.block]}],
                        "omissions": [], "uncertainties": []}
        self.style = styles.resolve_style("scholar-ink")

    def accept_synthetic(self):
        import pymupdf
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((40, 60), "Synthetic reviewer acceptance fixture")
            document.save(self.root / "work/preview.pdf")
        authoring.record_preview(self.root, self.content, self.style)
        authoring.accept_preview(self.root, self.content, self.style, confirmed_by="synthetic reviewer fixture only")
        authoring.require_accepted_preview(self.root, self.content, self.style)

    def asset(self):
        from PIL import Image
        path = self.root / "work/diagram.png"
        Image.new("RGB", (60, 40), "white").save(path)
        record = {"id": "diagram", "origin": "user", "state": "accepted", "path": "work/diagram.png",
                  "original_path": str(path), "source_name": "Example source", "source_url": "https://example.org/original?a=1&b=2",
                  "candidate_review": {"inspected": True, "decision": "relevant", "reason": "Synthetic inspected diagram"},
                  "placements": {"efficiency": {"inspected_after_placement": True, "decision": "relevant",
                                                "reason": "Synthetic relevant placement"}}}
        self.content["sections"][0]["blocks"].append({"type": "image", "asset_id": "diagram",
                                                    "caption": "Synthetic diagram", "evidence": ["audio-1"]})
        jobs.write_json(self.root / "work/assets.json", [record])
        return record


class ReviewerMathContractCase(ReviewerMathFixture):
    def test_malformed_document_math_is_rejected_at_authoring_boundary(self):
        malformed = [
            [], {}, {"type": "script", "src": "https://example.org/x"},
            {"type": "identifier", "name": "P", "onclick": "alert(1)"},
            {"type": "identifier", "name": "P_out"},
            {"type": "fraction", "numerator": {"type": "number", "value": "1"}},
            {"type": "row", "children": []}, {"type": "row", "children": "not nodes"},
            {"type": "operator", "symbol": "<img>"},
            {"type": "number", "value": True}, {"type": "number", "value": float("nan")},
            {"type": "number", "value": float("inf")}, {"type": "number", "value": "1/2"},
            {"type": "text", "value": ""}, {"type": "text", "value": "a\x00b"},
            {"type": "text", "value": "x" * 121}, {"type": "sqrt", "radicand": []},
        ]
        for tree in malformed:
            with self.subTest(tree=tree):
                block = dict(self.block, math=tree)
                with self.assertRaises(ValueError):
                    authoring.validate_block(self.root, self.content["sections"][0], block)

    def test_recursive_and_oversized_document_math_fail_without_recursion_crash(self):
        cycle = {"type": "row"}
        cycle["children"] = [cycle]
        many = {"type": "row", "children": [{"type": "number", "value": "1"} for _ in range(129)]}
        for tree in (cycle, many):
            with self.subTest(kind="cycle" if tree is cycle else "oversized"):
                with self.assertRaises(ValueError):
                    authoring.validate_block(self.root, self.content["sections"][0], dict(self.block, math=tree))

    def test_equation_still_requires_plain_inventory_text(self):
        for text in (None, "", "  "):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    authoring.validate_block(self.root, self.content["sections"][0], dict(self.block, text=text))

    def test_changed_math_invalidates_accepted_preview_even_when_plain_text_is_same(self):
        self.accept_synthetic()
        self.block["math"]["children"][2]["numerator"]["sub"]["value"] = "changed"
        with self.assertRaises(ValueError):
            authoring.require_accepted_preview(self.root, self.content, self.style)

    def test_each_changed_rendered_citation_field_invalidates_accepted_preview(self):
        record = self.asset()
        for field, changed in {
            "source_name": "Different source", "source_url": "https://example.org/swapped",
            "source_page": "https://example.org/page", "attribution": "Different credit", "creator": "Another creator",
            "license": "Unknown license", "licensing": "Another license record", "timestamp": 999,
            "original_path": str(self.folder / "renamed.png"),
        }.items():
            with self.subTest(field=field):
                jobs.write_json(self.root / "work/assets.json", [record])
                self.accept_synthetic()
                changed_record = dict(record, **{field: changed})
                jobs.write_json(self.root / "work/assets.json", [changed_record])
                with self.assertRaises(ValueError):
                    authoring.require_accepted_preview(self.root, self.content, self.style)

    def test_unused_image_metadata_does_not_invalidate_unchanged_used_preview(self):
        record = self.asset()
        self.accept_synthetic()
        jobs.write_json(self.root / "work/assets.json", [record, {"id": "unused", "state": "excluded", "source_url": "https://example.org/unused"}])
        authoring.require_accepted_preview(self.root, self.content, self.style)

    def test_editable_powerpoint_refuses_structured_math_without_flattening(self):
        with self.assertRaisesRegex(ValueError, "editable.*math|math.*editable"):
            render_pptx.DeckBuilder(self.content, self.style, self.root).build()
        legacy = copy.deepcopy(self.content)
        del legacy["sections"][0]["blocks"][0]["math"]
        deck = render_pptx.DeckBuilder(legacy, self.style, self.root).build()
        deck.save(self.folder / "legacy.pptx")
        from pptx import Presentation
        reopened = Presentation(self.folder / "legacy.pptx")
        texts = " ".join(shape.text for slide in reopened.slides for shape in slide.shapes if shape.has_text_frame)
        self.assertIn("P_out / P_in", texts)


class ReviewerNativeMathCase(ReviewerMathFixture):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.runtime = sync_playwright().start()
        try:
            cls.browser, _ = render_pdf._launch(cls.runtime)
        except Exception:
            cls.runtime.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def page(self):
        context = self.browser.new_context(offline=True)
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_content(render_pdf._html_document(self.root, self.content, self.style, preview=True), wait_until="load")
        page.emulate_media(media="print")
        page.evaluate("document.fonts.ready")
        return page

    def test_efficiency_is_full_size_stacked_fraction_with_real_lowered_subscripts(self):
        # Real user regression: eta must not become a slash/underscore string.
        page = self.page()
        geometry = page.locator("math").evaluate("""math => {
          const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,font:parseFloat(getComputedStyle(e).fontSize)}};
          const f=math.querySelector('mfrac'), n=f.firstElementChild, d=f.lastElementChild;
          return {eta:rect(math.querySelector('mi')), n:rect(n.firstElementChild), ns:rect(n.lastElementChild),
                  d:rect(d.firstElementChild), ds:rect(d.lastElementChild)};
        }""")
        self.assertLess(geometry["n"]["y"], geometry["d"]["y"])
        for base, sub in (("n", "ns"), ("d", "ds")):
            self.assertAlmostEqual(geometry[base]["font"], geometry["eta"]["font"], delta=0.5)
            self.assertGreater(geometry[sub]["y"], geometry[base]["y"] + 1)
            self.assertLess(geometry[sub]["font"], geometry[base]["font"] * .9)
        target = self.folder / "native-efficiency.pdf"
        page.pdf(path=str(target), format="A4", print_background=True)
        import pymupdf
        with pymupdf.open(target) as pdf:
            text = "".join(p.get_text() for p in pdf)
            self.assertNotIn("P_out", text)
            self.assertNotIn("P_in", text)
            self.assertIn("out", text)
            self.assertIn("in", text)
            self.assertEqual(sum(len(p.get_images()) for p in pdf), 0, "Native equations must not be raster pictures")
            bars = [shape for p in pdf for shape in p.get_drawings() if shape["rect"].height < 2 and shape["rect"].width > 8]
            self.assertTrue(bars, "The exported fraction needs a real horizontal bar")

    def test_radical_and_exponent_are_native_layout_in_an_rtl_document(self):
        self.block["math"] = {"type": "row", "children": [
            {"type": "identifier", "name": "T"}, {"type": "operator", "symbol": "∝"},
            {"type": "superscript", "base": {"type": "identifier", "name": "r"}, "sup": {"type": "number", "value": "2"}},
            {"type": "operator", "symbol": ","}, {"type": "identifier", "name": "cos"},
            {"type": "number", "value": "30"}, {"type": "operator", "symbol": "°"}, {"type": "operator", "symbol": "="},
            {"type": "fraction", "numerator": {"type": "sqrt", "radicand": {"type": "number", "value": "3"}},
             "denominator": {"type": "number", "value": "2"}},
        ]}
        page = self.page()
        geometry = page.locator("math").evaluate("""m=>{const p=m.querySelector('msup'),b=p.firstElementChild,s=p.lastElementChild;return {base:b.getBoundingClientRect().y,sup:s.getBoundingClientRect().y,bsize:parseFloat(getComputedStyle(b).fontSize),ssize:parseFloat(getComputedStyle(s).fontSize),radical:m.querySelector('msqrt').getBoundingClientRect().width,den:m.querySelector('mfrac').lastElementChild.getBoundingClientRect().y,num:m.querySelector('msqrt').getBoundingClientRect().y}}""")
        self.assertLess(geometry["sup"], geometry["base"])
        self.assertLess(geometry["ssize"], geometry["bsize"])
        self.assertGreater(geometry["radical"], 5)
        self.assertLess(geometry["num"], geometry["den"])
        self.assertEqual(page.locator("img").count(), 0)

    def test_math_leaf_markup_is_visible_text_and_never_an_active_element(self):
        literal = '<img src="https://example.org/x" onerror="alert(1)">'
        self.block["math"] = {"type": "text", "value": literal}
        page = self.page()
        self.assertEqual(page.locator("math").text_content(), literal)
        self.assertGreater(page.locator("math").bounding_box()["height"], 0)
        self.assertEqual(page.locator("math img, math script").count(), 0)

    def test_compact_source_keeps_exact_click_target_and_literal_escaped_label(self):
        record = self.asset()
        record["source_name"] = 'Source <script>alert(1)</script>'
        jobs.write_json(self.root / "work/assets.json", [record])
        page = self.page()
        link = page.locator(".source-line a")
        self.assertEqual(link.inner_text(), record["source_name"])
        self.assertEqual(link.get_attribute("href"), record["source_url"])
        self.assertNotIn(record["source_url"], page.locator(".source-line").inner_text())
        self.assertEqual(page.locator("script").count(), 0)


if __name__ == "__main__":
    unittest.main()
