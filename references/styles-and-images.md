# Styles, teaching profiles, and images

Visual design and teaching approach are separate records. Both are ordinary UTF-8 JSON and neither is ever executed.

The image-label-language contract lives in `references/image-intake-and-search.md`.

## Built-in visual styles

`assets/styles/` contains two tested style records plus `style.schema.json`:

| ID | Character | Latin / Arabic families | Palette |
|---|---|---|---|
| `scholar-ink` | Book-like study notes, generous margins, serif Latin headings | Georgia / Segoe UI | Warm paper, deep teal |
| `clarity-grid` | Compact high-contrast study layout, 16:9 slides | Segoe UI / Segoe UI | White, indigo, amber |

Both support `ltr` and `rtl`, PDF and PPTX, and both output languages. They are deliberately distinct in typography, colour, spacing, and rules; a preview must show the actual difference.

Load with:

```python
from lecture_materials import styles
style = styles.load_builtin("scholar-ink")        # or resolve_style(path/name/dict)
styles.require_style_direction(style, "rtl")
```

## Style record

Required keys: `schema_version`, `kind: "visual_style"`, `id`, `name`, `version`, `fonts`, `palette`, `page`, `slides`, `rules`. Optional: `direction_support`, `languages`, `layout`, `font_assets`, `description`, `provenance`, `fingerprint`.

- `fonts`: `latin`, `arabic`, `display`, `mono`, each `{family, fallbacks}`.
- `palette`: 13 `#RRGGBB` tokens (background, text, accent, table colours, example colours).
- `page`: mm dimensions, margins, line height, and typography sizes.
- `slides`: inches dimensions, margins, title bar, and typography sizes.
- `layout`: cover/section family names, image alignment, TOC columns.
- `rules`: image max ratios, table zebra/header behaviour, equation direction/alignment.
- `provenance`: source, method, reference hash/name, preview identity.
- `fingerprint`: SHA-256 of the normalized data.

`normalize_style` fills defaults and validates numbers, colours, directions, and font groups. Invalid values fail instead of silently producing an unusable document.

### Panel cover, side cards and local fonts in PDF

`layout.cover: "panel"` selects the orange/navy title panel. `layout.section: "side-cards"` selects a side heading, pale-gray flowing cards, and blue rules. RTL output mirrors the heading/body sides. Body cards use 15 pt text; equation notes use 11 pt. Long content continues onto additional pages without fixed-height clipping. These are PDF layout families; the native PPTX engine keeps its own editable slide layout and uses the style's measured tokens.

Optional `font_assets` records contain exactly `family`, `path`, and `sha256`. The family must be a plain font-family name. A path must be a relative `.ttf` file under the style JSON directory, without links or junctions; PDF rendering checks its SHA-256, 16 MiB size limit, TrueType signature and actual decoding before embedding it as a data URI. Missing or changed assets fail explicitly. The manifest is data only; external CSS, URLs and remote font fetching are unsupported.

Load an asset-bearing style by JSON path, or retain `_source_path` from `load_named_profile(...)["document"]`. `save_named_profile` freezes verified fonts under the new library version's `assets/` directory using `style_source_path`/`style_path` as the source hint. Copy that directory with the saved JSON when transferring a style. Shared fonts are not job cleanup targets.

A subset font supplies only its embedded glyphs; declared font stacks supply missing characters. PDF embedding does not install the font on the system or embed it in PPTX. Grandview subset reuse does not imply complete Grandview coverage or pixel-identical reproduction of every reference page.

## Reference extraction

```python
result = styles.extract_style("References/design.pdf", "work/proposed-style.json")
```

Supported references: PDF, PPTX/PPTM, legacy binary `.ppt`, and raster images. The reference is opened read-only and hashed before and after inspection; it is never modified or overwritten. The original stays in place.

The result contains the compatible `style`/`proposed_style`/`tokens`, `analysis`, and `differences` keys plus the design-support fields below:

```text
proposal_path, design_directory, design_report_path
reference_format, reference_sha256, source_preserved, conversion
reference_previews[]              # rendered pages/slides for host inspection
design                          # structured reference design (also stored in style.design)
support{supported, preserved, approximated, unsupported, ambiguous, excluded}
supported[], preserved[], approximated[], unsupported[], ambiguous[], excluded[]
preview_required, next_steps
```

`design_report_path` is a JSON report containing the original identity, conversion record, rendered-reference metadata, the design object and the support lists. Inspect the rendered previews and the report before accepting the design.

- **PDF**: page size, measured margins, body/heading typography, colour, header/footer geometry and simple recurring vector rectangles/lines. A cover title position is measured when a large heading is in the upper part of the first page. The report records complex or body drawings that were not reused. Scanned/flattened pages are marked `visual-estimate`; typography/margins are estimated from page ink and no structural text is claimed.
- **PPTX/PPTM**: a **sanitized native template** is frozen next to the proposal: all original slides are deleted, picture objects are removed, and masters/layouts/theme/placeholders/dimensions are retained. Arbitrary master/layout prose, tables, charts, SmartArt/graphic frames, OLE/content objects and pictures are removed; placeholder prompt/default text is cleared while placeholder geometry/formatting stays. Recurring text-free vector decoration from the slides is stored as validated DrawingML; pictures, old prose, tables, charts, diagrams and embedded workbooks are not copied. The report inventories what was preserved, approximated, excluded, or left ambiguous for the host.
- **Legacy `.ppt`**: detected by its OLE signature (not by suffix alone). It is converted through a documented local read-only route: PowerPoint automation `SaveAs` to OOXML, or LibreOffice headless `--convert-to pptx`. If neither is available the call fails with an actionable conversion requirement; renaming a `.ppt` to `.pptx` is rejected, never treated as conversion. `conversion` in the result/report records the method and that the original was preserved.
- **Raster**: colour/aspect estimates and a normalised preview only; no named layouts or exact fonts.

The proposal is not automatically named or saved. Ask the user for the style name through the host intake. Inspect a representative preview with real lecture content and report actual differences, including missing fonts or approximated properties. Do not promise pixel-identical output across PDF, PowerPoint, Slides, and Gamma.

### Public design helpers

```python
summary = styles.design_summary("work/proposed-style.json")          # identity + support + previews
previews = styles.reference_previews("work/proposed-style.json", kind="pdf")
template = styles.resolve_design_template(saved_style_path)          # verifies the frozen native PPTX
identity = styles.verify_style_design(saved_style_path)              # frozen/missing/changed report
```

`reference_previews` never returns a stale path silently. `resolve_design_template` verifies the frozen template hash and raises if it is missing or changed. Render previews/finals also verify the design identity and include `reference_design` in their reports.

### Structured design fields

A reference style keeps the design representation under `style.design` (the ordinary style tokens remain at the top level so existing renderers/CLI calls keep working):

| Field | Meaning |
|---|---|
| `schema_version`, `kind: reference_design`, `source_format`, `method` | Identity and extraction method (`native-template`, `measured`, `visual-estimate`). |
| `reference` | Original name/path/hash/bytes, inspected page/slide count, legacy conversion record. |
| `identity` | `mode` (`frozen` or `self-contained`), source path/hash, frozen path/hash. |
| `template` | For PPTX: sanitized native template path/hash, removed slide/picture/text/content-object counts and cleared placeholder prompts. |
| `previews` | Job-local rendered reference preview paths and renderer/limitation notes. |
| `support` | `supported`, `preserved`, `approximated`, `unsupported`, `ambiguous`, `excluded` lists with property/detail/renderer. |
| `report_path` | Job-local design-support report. |
| PDF fields | `page_size_mm`, `margins_mm`, `palette`, `fonts`, `typography`, `line_height`, `direction_support`, `body`, `headings`, `cover`, `header`, `footer` (items carry positions/sizes/colours and text hashes, never copied text), `vector_decor`, `table`, `slides`. |
| PPTX fields | `slide_width_in`, `slide_height_in`, `theme`, `layouts`, `roles`, `background`, `typography`, `headings`, `table`, `geometry` (margin/title/body/cover/header/footer), `recurring` (native shapes, ambiguities, excluded pictures, recurring-text hashes), `palette`, `fonts`. |

Header/footer items store `text_sha256`, character count and alignment but not the old text. The new document's own title/page numbers are drawn at the measured positions and in the measured style; the old labels are listed as ambiguous for host review. A reference text colour with insufficient contrast is adjusted and reported as an approximation.


## Saving a named style

```python
saved = styles.save_named_profile(
    library="C:/shared/lecture-library",
    request={
        "kind": "visual_style",
        "name": "Nile Teal Notes",
        "user_agreed": True,
        "style": style_document,
        "style_source_path": "work/proposed-style.json",  # needed for relative frozen assets
        "reference_name": "design.pdf",
        "reference_sha256": reference_digest,
        "collision": "new_version",     # or "rename" + "new_name"
    },
)
```

- Names are NFC-normalized, limited to 80 characters, and reject traversal, absolute/drive paths, reserved Windows device names, path separators, control characters, and trailing dots/spaces.
- A collision is never overwritten. Omit `collision` to fail; use `new_version` to create `2.0.0`, or `rename` plus `new_name`.
- `user_agreed: true` is required for teaching profiles and rejected as `false` for any save; a visual style may be saved on the user's naming instruction.
- Saving a reference style freezes its necessary shared design assets: the sanitized native PPTX template is copied to `visual-styles/<slug>/assets/<slug>-v<version>/template.pptx` and the saved style stores a hash-checked relative path. A PDF/raster design is already a self-contained token record.
- Reference **screenshots are deliberately not copied into the library** because they may contain original lecture content. Preview paths are cleared on save; `reference_previews`/render previews regenerate them from the intact original when its hash still matches. If the source changed or is missing, the frozen design still renders but a new visual comparison reports the mismatch instead of binding to the changed file.
- The shared library is `LECTURE_MATERIALS_LIBRARY` if set, otherwise `<package>/library`. It is outside every generated job and must not be offered to per-job cleanup.
- Saved layout: `visual-styles/<slug>/<slug>-v<version>.json`, assets under `visual-styles/<slug>/assets/`, and `visual-styles/index.json`; profiles use `teaching-profiles/`.

## Teaching profiles

```python
saved = styles.save_named_profile(
    library="C:/shared/lecture-library",
    request={
        "kind": "teaching_profile",
        "name": "Stepwise Egyptian Arabic",
        "user_agreed": True,
        "profile": {
            "tone": "calm",
            "detail_level": "high",
            "step_size": "small",
            "example_type": "worked examples after definitions",
            "terminology": "keep English technical terms and explain them once",
        },
    },
)
```

Only preference fields are stored; unsupported keys are rejected so personal example prose is not silently persisted. `user_agreed` must be true, meaning the user accepted the explanation approach first. Saving is offered, never automatic.

Lookup/version selection (for the source/CLI lane):

```python
styles.list_named_profiles(library, "visual_style")
styles.load_named_profile(library, "visual_style", "Nile Teal Notes")
styles.load_named_profile(library, "visual_style", "Nile Teal Notes", version="2.0.0")
```

The loader returns `{path, kind, id, name, version, document}`; without `version` it selects the newest numeric version.

## Image policy

The job selects one of: text only, web images, user-supplied images, or an explicitly requested combination. Lecture captures/reference images are a separate addition, not an implied right.

For each `work/assets.json` candidate retain:

```json
{
  "id": "supplied-table",
  "origin": "user",
  "state": "accepted",
  "path": "C:/lectures/supplied/table.png",
  "original_path": "C:/lectures/supplied/table.png",
  "subject": "Table with rows, columns, and a marked primary key",
  "source_url": null,
  "source_name": null,
  "license": "user-supplied; permission for this job",
  "candidate_review": {
    "inspected": true,
    "decision": "relevant",
    "reason": "The labels match the section's table/primary-key explanation."
  },
  "placements": {
    "entities": {
      "decision": "relevant",
      "inspected_after_placement": true,
      "reason": "The diagram shows the exact terms used in this section.",
      "placement": "auto",
      "crop": null
    }
  }
}
```

Rules enforced by `authoring.image_asset` and `validate_content`:

1. Open and inspect the actual image; a search title or filename is not review.
2. Match the specific concept, version, direction, and labels to the lecture.
3. Match it again to the target section/slide after placement/cropping.
4. `excluded`, `pending`, missing, or uninspected assets cannot be inserted, even when older review fields look positive.
5. A generally relevant image is not valid for every section; placement review is per section.
6. Supplied images receive the same review as web images.
7. If a supplied image is unsuitable, explain the mismatch, set `state: "excluded"` with `exclusion_reason`, keep the original file, and continue. Do not silently restore it; re-review a replacement.
8. Text-only output contains no pictures at all. Do not add lecture captures or decorative images.
9. A hard-to-explain equation/diagram is not a reason to break text-only mode; explain the limitation in text and surface uncertainty.
10. Keep source/caption close to the image. Never crop away scientific labels to fit a style; a crop record must set `preserves_labels: true`.
11. An optional `source_name` (1–48 printable characters) is the compact citation label shown where the source link would print a full URL; the link still goes to the real `source_url`/`source_page`, and no attribution is removed.

Web images need a source URL/page and a licensing status (unknown is allowed). User images keep their original path. An optional `source_name` may be the source's brand name; the explanation keeps the user's chosen output language. The `subject`, reasons, and decisions are ordinary text; do not print invented confidence percentages. "Likely unrelated" is a judgment, not a measured probability.

The host performs search and semantic inspection with its own tools. The renderer consumes reviewed assets; it does not make unverified search results trustworthy and does not call a paid image API. `authoring.excluded_assets(root)` returns excluded/pending records with their recorded reasons so the host can explain a mismatch to the user.

For the required per-image `language_review`, the `image_language` preference, and the durable search ledger with its 600-second budget, see `references/image-intake-and-search.md` (this section covers the source-policy and review records, not the label-language rules).
