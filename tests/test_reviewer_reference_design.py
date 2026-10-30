"""Independent reference-design acceptance, written after design-handoff.

All references, lecture text and approval transitions are reviewer-created.
"""

import copy
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lecture_materials import authoring, jobs, render_pdf, render_pptx, styles
from test_reviewer_pipeline import ARTIFACTS, native_slide_text


class ReviewerReferenceDesignCase(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="tmp-reviewer-design-", dir=Path(__file__).parent)
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name).resolve()
        self.root, self.content = self.ready_job("job")

    def ready_job(self, name):
        from PIL import Image
        root = self.folder / name
        source = self.folder / (name + "-synthetic-source.mp4")
        source.write_bytes(b"Reviewer style-unit source identity; no real lecture")
        jobs.create_job(root, str(source), {"image_mode": "text", "allow_examples": False})
        job = jobs.load_job(root)
        job["media"] = {"start": 0, "end": 10, "sha256": jobs.file_digest(source)}
        job["stages"]["speech"] = {"status": "transcribed"}
        jobs.save_job(root, job)
        jobs.write_json(root / "work/transcript.json", [{"id": "audio-1", "kind": "spoken", "start": 0,
                                                        "end": 10, "text": "Fresh content about an entity and its attributes."}])
        (root / "work/frames").mkdir()
        Image.new("RGB", (320, 180), "white").save(root / "work/frames/screen-1.png")
        jobs.write_json(root / "work/frames-state.json", {"complete": True, "interval": 5, "requested_end": 10,
                                                        "frames": [{"requested": t, "frame_id": "screen-1"} for t in (0, 5, 10)]})
        jobs.write_json(root / "work/screen.json", [{"id": "screen-1", "kind": "screen", "status": "reviewed",
                                                   "text": "Synthetic style fixture: a blank white board."}])
        content = {"title": "New reviewer lecture", "language": "en", "direction": "ltr",
                   "output_stem": "new-reviewer-lecture", "sections": [{"id": "fresh", "title": "Fresh entities",
                   "blocks": [{"type": "paragraph", "text": "FRESH_CONTENT_92583: an entity has attributes.",
                               "evidence": ["audio-1", "screen-1"]},
                              {"type": "table", "header": ["Entity", "Attribute"],
                               "rows": [["Student", "Identifier"], ["Course", "Code"]], "evidence": ["audio-1"]}]}],
                   "omissions": [], "uncertainties": []}
        jobs.write_json(root / "work/content.json", content)
        return root, content

    def pptx_reference(self, master_fact=False):
        from PIL import Image
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE
        from pptx.opc.constants import RELATIONSHIP_TYPE as RT
        from pptx.oxml.ns import qn
        from pptx.util import Inches, Pt
        deck = Presentation()
        deck.slide_width, deck.slide_height = Inches(12), Inches(7)
        master = deck.slide_master
        master.background.fill.solid()
        master.background.fill.fore_color.rgb = RGBColor.from_string("FFF4DC")
        layout = deck.slide_layouts[1]
        for placeholder in layout.placeholders:
            if placeholder.placeholder_format.idx == 0:
                placeholder.left, placeholder.top = Inches(1), Inches(0.5)
                placeholder.width, placeholder.height = Inches(10), Inches(0.8)
            elif placeholder.placeholder_format.idx == 1:
                placeholder.left, placeholder.top = Inches(1), Inches(1.6)
                placeholder.width, placeholder.height = Inches(10), Inches(4.5)
        first = None
        for index in range(2):
            slide = deck.slides.add_slide(layout)
            first = first or slide
            slide.shapes.title.text = f"OLD_REFERENCE_TOPIC_84712 {index}"
            slide.placeholders[1].text = "Original lecture words must not become new lecture content."
            for paragraph in slide.shapes.title.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.name, run.font.size = "Arial", Pt(28)
            for paragraph in slide.placeholders[1].text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.name, run.font.size = "Arial", Pt(18)

        def move_to_master(shape):
            element = copy.deepcopy(shape._element)
            identifiers = [int(node.get("id")) for node in master.shapes._spTree.iter(qn("p:cNvPr"))]
            node = element.find(".//" + qn("p:cNvPr"))
            node.set("id", str(max(identifiers or [0]) + 1))
            master.shapes._spTree.insert_element_before(element, "p:extLst")
            first.shapes._spTree.remove(shape._element)
            return element

        bar = first.shapes.add_shape(MSO_AUTO_SHAPE_TYPE.RECTANGLE, 0, 0, Inches(0.3), Inches(7))
        bar.fill.solid()
        bar.fill.fore_color.rgb = RGBColor.from_string("5C248C")
        bar.line.fill.background()
        move_to_master(bar)
        logo = io.BytesIO()
        Image.new("RGB", (40, 40), "red").save(logo, format="PNG")
        logo.seek(0)
        picture = first.shapes.add_picture(logo, Inches(11.3), Inches(6.4), width=Inches(0.4))
        blip = picture._element.find(".//" + qn("a:blip"))
        part = first.part.related_part(blip.get(qn("r:embed")))
        relationship = master.part.relate_to(part, RT.IMAGE)
        copied = move_to_master(picture)
        copied.find(".//" + qn("a:blip")).set(qn("r:embed"), relationship)
        if master_fact:
            box = first.shapes.add_textbox(Inches(1.2), Inches(2.5), Inches(7), Inches(0.8))
            box.text = "OLD_MASTER_FACT_84712: an old scientific explanation"
            move_to_master(box)
        path = self.folder / ("original-master-fact.pptx" if master_fact else "original-template.pptx")
        deck.save(path)
        return path

    def archive(self, name, reference, preview, final=None):
        target = ARTIFACTS / name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(reference, target / reference.name)
        for key in ("preview_pdf", "preview_pptx", "preview_png"):
            value = preview.get(key)
            if value and Path(value).is_file():
                shutil.copyfile(value, target / Path(value).name)
        for index, value in enumerate(preview.get("reference_comparison", {}).get("images", [])):
            shutil.copyfile(value, target / f"comparison-{index + 1}.png")
        for index, value in enumerate(preview.get("preview_images", [])):
            shutil.copyfile(value, target / f"new-slide-{index + 1}.png")
        if final:
            shutil.copyfile(final, target / Path(final).name)
        jobs.write_json(target / "reviewer-preview-report.json", preview)

    def accept(self, style_path):
        authoring.record_preview(self.root, self.content, style_path)
        authoring.accept_preview(self.root, self.content, style_path, confirmed_by="synthetic reviewer design fixture")

    def test_native_template_keeps_design_and_removes_original_master_content(self):
        reference = self.pptx_reference(master_fact=True)
        digest = jobs.file_digest(reference)
        proposal = self.root / "work/proposed-style.json"
        report = styles.extract_style(reference, proposal)
        self.assertTrue(report["reference_previews"])
        preview = render_pptx.render_preview(self.root, "work/content.json", proposal)
        self.assertTrue(preview["reference_comparison"]["available"])
        self.accept(proposal)
        final = render_pptx.render_final(self.root, "work/content.json", proposal)
        self.archive("native-reference", reference, preview, final["output"])
        deck, text = native_slide_text(final["output"])
        self.assertAlmostEqual(deck.slide_width.inches, 12, places=2)
        self.assertAlmostEqual(deck.slide_height.inches, 7, places=2)
        self.assertIn("FRESH_CONTENT_92583", text)
        with zipfile.ZipFile(final["output"]) as package:
            xml = "\n".join(package.read(name).decode("utf-8", errors="replace")
                            for name in package.namelist() if name.endswith(".xml"))
            self.assertNotIn("OLD_REFERENCE_TOPIC_84712", xml)
            self.assertNotIn("OLD_MASTER_FACT_84712", xml, "Master content must not leak old lecture facts into every new slide")
            self.assertFalse(any(name.startswith("ppt/media/") for name in package.namelist()))
        bars = [shape for master in deck.slide_masters for shape in master.shapes
                if abs(shape.left.inches) < 0.01 and abs(shape.width.inches - 0.3) < 0.02
                and abs(shape.height.inches - 7) < 0.02]
        self.assertTrue(bars, "Native master decoration was replaced with the hard-coded default")
        self.assertEqual(jobs.file_digest(reference), digest)

    def test_saved_template_excludes_old_thumbnail_and_extended_document_metadata(self):
        # Real Office files can carry old slide pixels and titles outside ppt/.
        from lxml import etree
        reference = self.pptx_reference()
        with zipfile.ZipFile(reference) as package:
            entries = {name: package.read(name) for name in package.namelist()}
        ns = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
        vt = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
        app = etree.fromstring(entries["docProps/app.xml"])
        for name in ("Company", "Manager"):
            for old in app.findall("{" + ns + "}" + name):
                app.remove(old)
            etree.SubElement(app, "{" + ns + "}" + name).text = "OLD_PRIVATE_METADATA_711"
        for node in app.findall(".//{" + vt + "}lpstr"):
            node.text = "OLD_PRIVATE_METADATA_711"
        entries["docProps/app.xml"] = etree.tostring(app, xml_declaration=True, encoding="UTF-8", standalone=True)
        from PIL import Image
        thumbnail = io.BytesIO()
        Image.new("RGB", (160, 90), "red").save(thumbnail, format="JPEG")
        entries["docProps/thumbnail.jpeg"] = thumbnail.getvalue()
        relationships = etree.fromstring(entries["_rels/.rels"])
        relns = "http://schemas.openxmlformats.org/package/2006/relationships"
        kind = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"
        if not any(node.get("Type") == kind for node in relationships):
            etree.SubElement(relationships, "{" + relns + "}Relationship", Id="rIdReviewerThumbnail",
                             Type=kind, Target="docProps/thumbnail.jpeg")
        entries["_rels/.rels"] = etree.tostring(relationships, xml_declaration=True, encoding="UTF-8", standalone=True)
        with zipfile.ZipFile(reference, "w", zipfile.ZIP_DEFLATED) as package:
            for name, body in entries.items():
                package.writestr(name, body)
        digest = jobs.file_digest(reference)
        proposal = self.root / "work/metadata-style.json"
        styles.extract_style(reference, proposal)
        template = styles.resolve_design_template(proposal)
        preview = render_pptx.render_preview(self.root, "work/content.json", proposal)
        self.assertTrue(preview["reference_comparison"]["available"])
        self.accept(proposal)
        final = render_pptx.render_final(self.root, "work/content.json", proposal)
        self.archive("metadata-reference", reference, preview, final["output"])
        for path in (template, final["output"]):
            with zipfile.ZipFile(path) as package:
                self.assertFalse(any("thumbnail" in name.lower() for name in package.namelist()),
                                 "Old thumbnail pixels must not survive as hidden saved-style or final-output media")
                metadata = "\n".join(package.read(name).decode("utf-8", errors="replace")
                                     for name in package.namelist() if name.endswith(".xml") or name.endswith(".rels"))
                self.assertNotIn("OLD_PRIVATE_METADATA_711", metadata)
                self.assertNotIn("metadata/thumbnail", metadata)
        self.assertEqual(jobs.file_digest(reference), digest)

    def test_old_master_chart_and_embedded_workbook_are_excluded_from_design(self):
        # Master charts carry old scientific facts and an embedded workbook;
        # removing only visible text/pictures must not leave their package data.
        from pptx import Presentation
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE
        from pptx.opc.constants import RELATIONSHIP_TYPE as RT
        from pptx.oxml.ns import qn
        from pptx.util import Inches
        reference = self.pptx_reference()
        deck = Presentation(reference)
        slide = deck.slides[0]
        master = deck.slide_master
        data = CategoryChartData()
        data.categories = ["OLD_CHART_FACT_62834", "Old experiment"]
        data.add_series("Old study measurements", (817, 924))
        chart = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(2), Inches(2),
                                       Inches(5), Inches(3), data)
        element = copy.deepcopy(chart._element)
        c = element.find(".//" + qn("c:chart"))
        part = slide.part.related_part(c.get(qn("r:id")))
        c.set(qn("r:id"), master.part.relate_to(part, RT.CHART))
        identifiers = [int(node.get("id")) for node in master.shapes._spTree.iter(qn("p:cNvPr"))]
        element.find(".//" + qn("p:cNvPr")).set("id", str(max(identifiers) + 1))
        master.shapes._spTree.insert_element_before(element, "p:extLst")
        slide.shapes._spTree.remove(chart._element)
        deck.save(reference)
        digest = jobs.file_digest(reference)
        proposal = self.root / "work/chart-reference-style.json"
        styles.extract_style(reference, proposal)
        template = styles.resolve_design_template(proposal)
        with zipfile.ZipFile(template) as package:
            self.assertFalse(any(name.startswith(("ppt/charts/", "ppt/embeddings/")) for name in package.namelist()),
                             "Old source charts/workbooks must not become saved design assets")
            text = "\n".join(package.read(name).decode("utf-8", errors="replace")
                             for name in package.namelist() if name.endswith(".xml"))
            self.assertNotIn("OLD_CHART_FACT_62834", text)
        self.assertEqual(jobs.file_digest(reference), digest)

    def test_saved_template_survives_job_cleanup_and_detects_changed_source(self):
        reference = self.pptx_reference()
        proposal = self.root / "work/proposed-style.json"
        extracted = styles.extract_style(reference, proposal)
        saved = styles.save_named_profile(self.folder / "library", {"kind": "visual_style", "name": "Reviewer Purple",
                                          "user_agreed": True, "style": extracted["style"], "style_source_path": str(proposal)})
        frozen = Path(styles.resolve_design_template(saved["path"]))
        self.assertTrue(frozen.is_file())
        self.assertFalse(frozen.is_relative_to(self.root))
        preview = render_pptx.render_preview(self.root, "work/content.json", proposal)
        self.accept(proposal)
        final = render_pptx.render_final(self.root, "work/content.json", proposal)
        for path in (self.root / "work").rglob("*"):
            if path.is_file():
                jobs.register_generated(self.root, path)
        jobs.mark_delivered(self.root, [final["output"]])
        plan = jobs.request_cleanup(self.root)
        jobs.confirm_cleanup(self.root, plan["confirmation_token"])
        self.assertFalse(proposal.exists())
        self.assertTrue(frozen.is_file())
        with reference.open("ab") as stream:
            stream.write(b"changed original identity")
        identity = styles.verify_style_design(saved["path"])
        self.assertTrue(identity["source_changed"])
        fresh_root, _ = self.ready_job("later-job")
        reused = render_pptx.render_preview(fresh_root, "work/content.json", saved["path"])
        self.assertTrue(reused["template_reused"])
        self.assertFalse(reused["reference_comparison"]["available"])
        self.archive("saved-reference-reuse", reference, reused)
        frozen.write_bytes(frozen.read_bytes() + b"tampered frozen template")
        with self.assertRaises(ValueError):
            styles.resolve_design_template(saved["path"])

    def test_pdf_reference_reuses_vectors_and_compares_new_content_without_old_labels(self):
        import pymupdf
        reference = self.folder / "original-design.pdf"
        source = pymupdf.open()
        colour = (0.36, 0.14, 0.55)
        for index in range(3):
            page = source.new_page(width=480, height=640)
            page.draw_rect(pymupdf.Rect(0, 0, 14, 640), color=None, fill=colour)
            page.draw_line(pymupdf.Point(40, 55), pymupdf.Point(440, 55), color=colour, width=2)
            page.insert_text((40, 40), "OLD_REFERENCE_HEADER", fontsize=9)
            page.insert_text((40, 95), "OLD_REFERENCE_TOPIC_84712", fontsize=20)
            for row in range(6):
                page.insert_text((40, 135 + row * 22), "Original reference body paragraph and its scientific explanation.", fontsize=10)
            page.insert_text((40, 610), "OLD_REFERENCE_FOOTER", fontsize=8)
            page.insert_text((420, 610), f"{index + 1} / 3", fontsize=8)
        source.save(reference)
        source.close()
        digest = jobs.file_digest(reference)
        proposal = self.root / "work/proposed-style.json"
        result = styles.extract_style(reference, proposal)
        self.assertEqual(len(result["reference_previews"]), 3)
        preview = render_pdf.render_preview(self.root, "work/content.json", proposal)
        self.assertTrue(preview["reference_comparison"]["available"])
        self.accept(proposal)
        final = render_pdf.render_final(self.root, "work/content.json", proposal)
        self.archive("pdf-reference", reference, preview, final["output"])
        with pymupdf.open(final["output"]) as document:
            self.assertAlmostEqual(document[0].rect.width, 480, delta=1)
            self.assertAlmostEqual(document[0].rect.height, 640, delta=1)
            text = "\n".join(page.get_text() for page in document)
            self.assertIn("FRESH_CONTENT_92583", text)
            for old in ("OLD_REFERENCE_HEADER", "OLD_REFERENCE_FOOTER", "OLD_REFERENCE_TOPIC_84712"):
                self.assertNotIn(old, text)
            stripes = [drawing for page in document for drawing in page.get_drawings()
                       if drawing.get("fill") and drawing["rect"].x0 < 2
                       and drawing["rect"].width < 20 and drawing["rect"].height > 500]
            self.assertTrue(stripes, "The recurring reference sidebar was not applied to new content")
            self.assertEqual(sum(len(page.get_images(full=True)) for page in document), 0)
        self.assertEqual(jobs.file_digest(reference), digest)

    def test_flattened_reference_is_estimate_and_renaming_pptx_is_not_conversion(self):
        import pymupdf
        from PIL import Image
        raster = io.BytesIO()
        Image.new("RGB", (300, 400), "navy").save(raster, format="PNG")
        document = pymupdf.open()
        page = document.new_page(width=300, height=400)
        page.insert_image(page.rect, stream=raster.getvalue())
        reference = self.folder / "flattened.pdf"
        document.save(reference)
        document.close()
        result = styles.extract_style(reference, self.root / "work/flattened-style.json")
        self.assertEqual(result["design"]["method"], "visual-estimate")
        self.assertTrue(result["unsupported"] or result["approximated"])
        mislabeled = self.folder / "not-a-conversion.ppt"
        shutil.copyfile(self.pptx_reference(), mislabeled)
        with self.assertRaises(ValueError):
            styles.extract_style(mislabeled, self.root / "work/mislabeled-style.json")


if __name__ == "__main__":
    unittest.main()
