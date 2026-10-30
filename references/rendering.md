# Rendering, previews, and output verification

The rendering lane consumes validated content and a visual style. It never re-extracts the lecture, never bypasses source readiness, and never records user approval by itself.

The user's `show_timestamps` choice controls the reviewed `source_range` label
at each PDF section heading (preview and final) and native PPTX content-slide
header. Hidden labels do not remove transcript/evidence timestamps or edit
free-form teaching text. Store navigation ranges separately; see
[the content contract](content-contract.md#optional-section-video-interval).

## Entry points

```python
from lecture_materials import authoring, render_pdf, render_pptx, verification

# 1. Structural/evidence validation
report = authoring.validate_content(root, content)

# 2. Preview (real artifact; no acceptance)
preview = render_pdf.render_preview(root, "work/content.json", "work/style.json")
# preview["preview_pdf"], preview["preview_png"], preview["binding_digest"]

# Or a representative editable deck / short slide preview:
slide_preview = render_pptx.render_preview(root, "work/content.json", "work/style.json")
# slide_preview["preview_pptx"], optional preview["preview_pdf"], preview["preview_images"]
# record with preview_path="work/preview-slides.pptx" when no PDF was exported.

# 3. Host shows the artifact, user accepts, then:
authoring.record_preview(root, content, "work/style.json")
authoring.accept_preview(root, content, "work/style.json",
                         confirmed_by="host-confirmed user acceptance")

# 4. Final output (all readiness + acceptance gates run again)
pdf = render_pdf.render_final(root, "work/content.json", "work/style.json")
pptx = render_pptx.render_final(root, "work/content.json", "work/style.json")

# 5. Read back
inspection = verification.inspect_outputs(root, [pdf["output"], pptx["output"]], render=True)
```

`content` is the same dictionary that was validated/rendered. `style` may be a style dictionary, a style JSON path (absolute, or relative to the job root), or a built-in name such as `"scholar-ink"`. Use the same form for render, record, accept, and final render; the binding hashes the raw style data.

## Output paths

| Artifact | Path |
|---|---|
| PDF | `<job>/outputs/<output_stem or title-slug>.pdf` |
| PPTX | `<job>/outputs/<output_stem or title-slug>.pptx` |
| PDF preview | `<job>/work/preview.pdf`, `<job>/work/preview.png` |
| PPTX preview | `<job>/work/preview-slides.pptx`, optional `work/preview.pdf`, slide PNGs |
| Preview metadata | `<job>/work/preview-meta.json` |
| PDF render report | `<job>/work/render/pdf/pdf-report.json` |
| PPTX render report | `<job>/work/render/pptx/pptx-report.json` |
| Verification report (optional) | `<job>/work/verification/inspection-report.json` |

Reference artifacts:

| Artifact | Path |
|---|---|
| Proposed style | chosen proposal path under `work/` |
| Design report | `<proposal-stem>-design/design-report.json` |
| Reference previews | `<proposal-stem>-design/reference-pages/` or `reference-slides/` |
| Frozen sanitized PPTX template | `<proposal-stem>-design/template.pptx` |
| Regenerated reference previews (saved style) | `<job>/work/reference-previews/` |
| Side-by-side comparison | `<job>/work/reference-comparison/<format>-<preview|final>/comparison-*.png` and `comparison-report.json` |

Final outputs live in `outputs/`, not `work/`, and are not cleanup targets. Working HTML/PDF passes, page PNGs, reports, and previews are registered through `jobs.register_generated` where a managed job exists. Do not register shared styles/profiles or original media.

## PDF

- Explicit `panel`/`side-cards` families and optional verified local TrueType assets are described in [styles-and-images.md](styles-and-images.md#panel-cover-side-cards-and-local-fonts-in-pdf). Their bottom rule is printed by the existing footer template, outside flowing chapter content, so decoration cannot strand an empty continuation page.
- Contents-page numbers come from the generated PDF's section destinations, not text matching. Wrapped, repeated and mixed-language headings retain distinct targets; a missing section destination fails rendering explicitly.
- Python builds HTML/CSS from content; a local Chromium renders the complete document.
- Cover, contents, topic divisions, page-number footer, repeated table headers, wrapped rows, selectable text, and a source-notes appendix are produced in one file.
- A PDF reference design contributes measured page size, margins, body/heading sizes and colours, cover title position/band, header/footer positions and styles, simple recurring vector bands/rules/page borders, and table treatment. Source page dimensions are preserved through CSS-pixel page sizing; Chromium can leave sub-point quantization (<0.1 pt), but no page is added or cropped. Header/footer template text is the new document's title/page number; old labels are not copied. Complex/flattened elements are reported as approximations rather than silently claimed.
- Images are embedded as data URIs, so deleting working images later does not break the PDF.
- Equation blocks with a validated structured `math` tree render inside the same equation box as native display-style MathML (`<math dir="ltr">` with `math-style: normal`, math font such as Cambria Math): numerator and denominator keep full size (not script-shrunk), subscripts are genuinely lowered and smaller, superscripts raised, square roots supported. The output stays selectable vector text, never a raster picture. Blocks without `math` keep the monospace plain-text fallback unchanged.
- An asset's optional `source_name` becomes the compact source label (`<span dir="ltr"><a href="full URL" title="full URL">label</a></span>`), so a long URL no longer dominates the caption; the `href`/`title` still carry the full URL and `source_url`/`source_page` are never replaced.
- Arabic/English mixtures use browser bidi shaping with real installed fonts (`Georgia`/`Segoe UI`/fallbacks by style). The document is not rasterised.
- Table-of-contents page numbers are a two-pass render: headings are located in the first PDF, numbers are fixed-width, then the document is rendered again. If a mapping shifts, a third pass is used.
- Page width overflow is a hard error; the render fails rather than clipping a table or long unbroken string.
- Text-only output must contain zero embedded image objects; read-back enforces this and removes an invalid output.
- Rendered reference pages and side-by-side comparison images are job-local inspection artifacts, not layers behind new prose.

Renderer selection tries `LECTURE_MATERIALS_BROWSER_CHANNEL`, Microsoft Edge, Chrome, bundled Playwright Chromium, then installed Chromium executables. Contexts are offline; no web fetch occurs during rendering. If no route works, `renderer_health()` reports the failure and final rendering raises with actionable text. There is no silent text-dump fallback.

## PPTX

- `python-pptx` generates the deck from the same content. All explanation, bullets, tables, equations, and captions remain native editable objects. Important content is not put in speaker notes; notes contain only evidence IDs and block types.
- An equation block's optional structured `math` tree cannot be preserved as native editable math in PPTX; the renderer fails explicitly with that message instead of silently flattening or dropping it (documented PDF-only limitation, no paid conversion API). Plain-text equations and all other blocks are unchanged.
- Dimensions come from the reference template/design when one is present, otherwise the style's `slides.width_in`/`height_in`. A reference deck's nondefault dimensions, masters, layouts, theme, placeholder geometry and master/layout vector decoration are reused through a **sanitized native template**; every original slide is removed before generation.
- Sanitization preserves text-free vector decoration and placeholder geometry/formatting, but removes arbitrary master/layout text, tables, charts, SmartArt/graphic frames, OLE/content objects, pictures and picture fills. Placeholder prompt/default text is cleared. Removed items and counts are exposed in `support`; the old data is not hidden in the package.
- Recurring text-free vector decoration that exists only on reference slides (for example a footer chip) is copied as validated native DrawingML. One-off body diagrams, pictures, tables, charts, old prose and other content objects are not copied; ambiguous shapes are exposed in `support.ambiguous` for host review.
- Template picture objects (including master/layout logos) are removed from the working deck. Text-only output must contain no pictures or media; other image modes still need explicit per-job permission for each image, so a template is not blanket permission. The report lists the excluded picture elements.
- Reference placeholder geometry drives cover/title/body/footer regions. Cover and section layouts are selected by layout role; missing roles fall back to the content layout. Empty placeholders are removed from generated slides, while the layouts remain available for native editing.
- Table treatment comes from the reference table's header fill/text, banding, border and font tokens where available. A referenced custom table-style package is approximated with explicit cell fills/borders.
- Arabic paragraphs set `rtl="1"` with right alignment. A mixed paragraph uses one real text run with both `a:latin` and `a:cs` typefaces, preserving spaces, punctuation, numbers, and bidi order better than splitting every script into separate runs.
- Long paragraphs are split at sentence/word boundaries and continue on new slides. Long tables repeat the header and continue on new slides; fonts are never reduced below the style sizes to hide overflow.
- Images are fitted proportionally with no crop. Caption and source/licence line stay adjacent.
- Footer adds static slide numbers and the document title at the measured/default footer position.

Actual slide rendering uses a separate, read-only PowerPoint automation instance when available (Windows), then LibreOffice headless conversion plus PyMuPDF page rendering. The report says `renderer: "Microsoft PowerPoint"` or `"LibreOffice headless + PyMuPDF"` only when image export succeeded; otherwise it records the error and warns that XML/read-back is not proof the deck was opened. The automation never opens a user's other presentation and never changes global application settings. Macro automation security is force-disabled before opening an original; failure to configure it aborts before `Open`. Bounded preview exports physically keep only the requested number of PNGs and delete unreported extras.

### Reference comparison

Both preview and final reports include `reference_design` (frozen/source identity and warnings), `reference_previews` (existing or regenerated previews) and `reference_comparison`. The comparison writer produces real side-by-side PNGs plus a JSON report with source/new image paths, sampled background/accent, aspect checks and actual differences. It never treats matching colours as proof of fidelity, and reference screenshots are not inserted behind new content.

A saved PPTX style's previews are regenerated from the intact original when its hash matches. If the original changed or is missing, the frozen template still renders, but the comparison is unavailable and the mismatch is reported instead of silently rebinding.

## Preview acceptance and final gate

`render_final` begins with `authoring.prepare_render`, which:

1. validates the content and image policy;
2. requires completed speech/no-speech review;
3. requires screen extraction to the requested end with reviewed representatives;
4. requires visual review records for imported diagrams/scanned pages;
5. requires a matching, still-existing accepted preview for the current content, style, preferences, cited evidence, and used image bytes.

A PPTX-only preview is recorded with:

```python
authoring.record_preview(root, content, style,
                         preview_path="work/preview-slides.pptx")
```

If `render_pptx.render_preview` managed to export `work/preview.pdf`, the default `record_preview(root, content, style)` accepts that PDF; both hashes are recorded in `preview-meta.json`.

Do not simulate acceptance to finish an ordinary job. Reviewer fixtures may exercise state transitions separately and are labelled tests.

## Verification

`verification.inspect_outputs(root, output_paths, content_path=None, render=True)` reads actual files and reports:

- file identity/size/hash;
- PDF pages, page sizes, fonts, extractable text, Arabic characters, links, image objects, empty pages, replacement characters, text outside the page, contents/page-number detection;
- PPTX slides, dimensions, native tables, table rows, picture count, RTL paragraph count, links, fonts (Latin and complex-script faces), notes, shape bounds, embedded media, external relationships;
- content-inventory token coverage against `content.json`;
- text-only picture policy;
- available rendered page/slide previews (or an honest rendering limitation);
- checks not performed: scientific correctness/completeness, full-size human Arabic/complex-page review, reference-style fidelity, licensing verification.

`inspect_outputs` does not claim that the output was opened by PowerPoint unless the renderer actually exported it. It does not replace the host's full-size visual review.

After confirmed cleanup of `work/`, `verification` can still read final PDF/PPTX files; it skips new previews when the work directory is absent. Embedded PDF images and PPTX package media remain intact.

## Cleanup and shared data

- Final PDF/PPTX are self-contained; image bytes and PPTX media are embedded.
- A style/profile library is not job work. Keep it outside the job and outside cleanup targets.
- Do not implement a second cleanup API. The source/CLI lane owns delivery, retain/delete, the second confirmation, and the exact deletion set.
- Register working render artifacts through `jobs.register_generated`; do not register final outputs there (`jobs.mark_delivered` owns final outputs).

## Host limitations to report

- Chromium availability and the selected channel.
- Missing or substituted fonts (`font_substitutions` in the PDF report; declared/run fonts in the PPTX report).
- PowerPoint automation unavailable or failed; XML inspection is not a render.
- Flattened/scanned style references produce visual estimates, not structural templates.
- Complex reference logos/vector artwork and header/footer labels are not copied automatically; they are listed as approximated/ambiguous for host review.
- Template picture objects are excluded, even when they look decorative. Text-only output contains none; other image modes require explicit per-image permission.
- A saved style's visual comparison needs the intact original file. If it changed or is missing, the frozen design still renders but the mismatch is reported and no new comparison is bound.
- Arabic visual order can differ from raw PDF text-extraction order; selectability/searchability and full-size visual review are both required.
- Different output services may approximate layout. Pixel-identical translation is not promised.

## Callable integration example for the source/CLI lane

The `scripts/lecture.py` launcher connects validation, preview acceptance,
rendering and verification. From the skill directory, use the configured Python
interpreter:

```powershell
.\.venv\Scripts\python.exe scripts\lecture.py validate --root JOB --content work/content.json
.\.venv\Scripts\python.exe scripts\lecture.py preview --root JOB --content work/content.json --style scholar-ink --format pdf
.\.venv\Scripts\python.exe scripts\lecture.py accept --root JOB --content work/content.json --style scholar-ink --user-accepted
.\.venv\Scripts\python.exe scripts\lecture.py render --root JOB --content work/content.json --style scholar-ink --format both
.\.venv\Scripts\python.exe scripts\lecture.py verify --root JOB --outputs "outputs/<stem>.pdf,outputs/<stem>.pptx"
```

`--style` may be a built-in ID (`scholar-ink`, `clarity-grid`), a built-in name, or a path to a style JSON file (absolute or job-relative). To use a saved shared-library style, pass its JSON **path**; the library lookup functions are `styles.load_named_profile`/`styles.list_named_profiles`. Passing a saved style by path is required when it has relative frozen template assets, so they resolve beside the style JSON.

To save a named reference style after extraction and preview acceptance:

```python
from pathlib import Path
from lecture_materials import styles

proposal = styles.extract_style("References/design.pptx", job_root / "work/proposed-style.json")
# Host inspects proposal["reference_previews"], proposal["design_report_path"],
# then a real new-content preview + side-by-side comparison.
saved = styles.save_named_profile(
    library="C:/shared/lecture-library",
    request={
        "kind": "visual_style",
        "name": "Reference Maroon Template",
        "user_agreed": True,
        "style": proposal["style"],
        "style_source_path": str(job_root / "work/proposed-style.json"),
        "reference_name": proposal["design"]["reference"]["original_name"],
        "reference_sha256": proposal["reference_sha256"],
    },
)
# Later render with saved["path"]; the frozen sanitized template and its hash
# are verified, and a changed source is reported instead of silently rebound.
```

```python
from pathlib import Path
from lecture_materials import authoring, render_pdf, render_pptx, styles
from lecture_materials.jobs import read_json

root = Path(job_root)
content = read_json(root / "work" / "content.json")       # or implementer's loader
style_path = root / "work" / "style.json"
style = styles.resolve_style(style_path)
styles.require_style_direction(style, content["direction"])

authoring.validate_content(root, content)
authoring.require_output_readiness(root, content)

preview = render_pdf.render_preview(root, "work/content.json", "work/style.json")
# show preview["preview_png"]/preview["preview_pdf"] to the user; stop here until acceptance
authoring.record_preview(root, content, style_path)
authoring.accept_preview(root, content, style_path)

pdf = render_pdf.render_final(root, "work/content.json", "work/style.json")
pptx = render_pptx.render_final(root, "work/content.json", "work/style.json")
```
