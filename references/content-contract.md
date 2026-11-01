# Content and evidence contract

Schema version: 1. Owner: authoring/rendering lane. This document is the agent-facing contract for `content.json`, source-review readiness, image assets, and preview acceptance. It is data-only JSON; no document executes code from these files.

The validator proves **structure and coverage**, not scientific truth. A prose block must still be checked against the evidence it cites, and a human must still review explanations, diagrams, equations, numbers, and terminology.

## Source evidence files

Read from `<job>/work/`:

| File | Purpose |
|---|---|
| `transcript.json` | Spoken evidence: unique `id`, `kind: "spoken"`, absolute `start`/`end`, `text`. |
| `screen.json` | One record per retained screen representative: `id`, `status` (`reviewed`/`unreadable`), observation text, uncertainty for unreadable. |
| `references.json` | Original-material evidence with file/page/slide provenance and visual-review state. |
| `external.json` | Extra factual evidence only when the job expressly permits it. |
| `frames-state.json` | Sampling configuration and ordered candidate-to-representative mappings. |
| `assets.json` | Image candidates, actual inspection, licensing, acceptance, exclusions, and per-section placement reviews. |

Evidence IDs must be unique across all four evidence files. A missing file means no records and is not itself approval.

### Non-substantive evidence

Every substantive record must be cited by content or listed as an explicit omission. A record may opt out only with an explicit marker:

```json
{"id": "screen-000042", "kind": "screen", "substantive": false,
 "coverage": "metadata_only", "note": "duplicate mapping only"}
```

This is for metadata only. Do not use it to hide lecture content.

## `content.json`

```json
{
  "schema_version": 1,
  "title": "Entities and attributes: lecture notes",
  "subtitle": "A complete explanation with technical terms and numbers (2026).",
  "language": "en",
  "direction": "ltr",
  "source_note": "Selected interval: 00:10:00–00:42:00",
  "output_stem": "entity-attributes-notes",
  "sections": [
    {
      "id": "entities",
      "title": "Entities and attributes",
      "summary": "What is an entity, and how do a row and a primary key differ?",
      "blocks": []
    }
  ],
  "omissions": [],
  "uncertainties": []
}
```

Required: `title`, `language`, `direction` (`rtl`/`ltr`), at least one section. Section IDs must be unique; titles and at least one supported block per section are required.

`document_mode` is optional: `study_notes` preserves legacy output; use
`software_tutorial` for application lessons with the paired instructional layout.
In that mode, output pictures must be `instruction` blocks, not standalone images.
Text-only tutorials can still use paragraph, bullet, table, and equation blocks.
The full omission ledger remains in `content.json`; only material source
uncertainties appear in the software tutorial's PDF/PPTX notes.

### Optional section video interval

Ask whether the user wants visible source times. Store the answer in job
preferences as `"show_timestamps": true` or `false`. Store a reviewed interval
separately from teaching text:

```json
{"id": "rotation", "title": "Repair the tree",
 "source_range": {"start": 1038, "end": 1098}, "blocks": []}
```

`source_range` is optional; when present it requires finite numeric seconds
with `0 <= start < end` (booleans are invalid). The host must verify these
absolute video times against cited evidence. Rendering does not invent a range
for a section without one. With visibility enabled, PDF preview/final section
headings and native PPTX content-slide headers show `17:18–18:18`. Fractional
bounds round outward to whole seconds; hour-long positions use `HH:MM:SS`.

With visibility disabled, these generated labels are absent. Do not put
navigation timestamps in free-form titles, summaries, body or cover notes;
those strings are not automatically edited. Instructional numbers and original
timestamped transcripts/evidence are preserved. Missing `show_timestamps`
retains legacy rendering and leaves the intake question unanswered. Changing
the preference invalidates prior preview acceptance through the existing
preference binding, so make and accept a fresh preview before final rendering.

### Paragraph

```json
{"type": "paragraph",
 "text": "An entity has attributes. Each row represents one instance.",
 "evidence": ["audio-000001", "screen-000012"],
 "origin": "lecture"}
```

`origin` is `lecture` (default), `explanation`/`agent_explanation`, or `example`. A complete explanation may paraphrase and teach, but cannot claim support that the cited evidence does not provide.

### Bullets

```json
{"type": "bullets", "ordered": false,
 "items": ["The primary key is unique.", "A column represents an attribute."],
 "evidence": ["audio-000002"]}
```

Items may not be empty. Splitting a lecture list across bullets must retain substantive items, order, and units.

### Table

```json
{"type": "table",
 "header": ["Item", "Meaning", "Unit"],
 "rows": [["row", "A record in the table", "—"], ["primary key", "A unique identifier", "1"]],
 "caption": "Basic table terminology.",
 "evidence": ["audio-000002"]}
```

Rows must be nonempty and rectangular. Header (`header` or `columns`) must match the column count. Cells are nonempty strings or finite numbers. Preserve row labels and units; the renderer never drops a column to fit.

### Equation

```json
{"type": "equation",
 "text": "KEY(row_i) ≠ KEY(row_j) for i ≠ j",
 "symbols": "KEY = primary key; i, j = row indices",
 "units": "rows",
 "description": "The primary key is unique for each row.",
 "evidence": ["audio-000002"]}
```

`text` is required and rendered in an isolated left-to-right block inside RTL documents. Do not invent a formula from an unreadable board; record the uncertainty instead.

#### Optional structured `math`

An equation block may add `"math"`: a small validated tree that the PDF renders as native display-style MathML — numerator and denominator keep full size (no script shrink), subscripts are genuinely lowered and smaller, superscripts raised — instead of the monospace plain-text fallback. `text` stays required; it remains the readable plain representation of the same formula. Content hashing covers `math`, so accepted previews are bound to the exact tree.

Supported node types and their exact child keys (unknown types/fields are rejected):

| Node type | Child keys | Leaf rules |
|---|---|---|
| `row` | `children` | nonempty list of math nodes |
| `identifier` | `name` | 1–24 letters (italic math identifier) |
| `number` | `value` | string like `12` or `-3.04`, or a finite real number |
| `operator` | `symbol` | one of `= + - − × ÷ · ≈ ≠ < > ≤ ≥ ( ) % ∝ , °` |
| `text` | `value` | 1–120 printable characters (upright labels such as `out`) |
| `fraction` | `numerator`, `denominator` | two math-node objects |
| `subscript` | `base`, `sub` | two math-node objects |
| `superscript` | `base`, `sup` | two math-node objects |
| `sqrt` | `radicand` | one math-node object (square root) |

Limits: depth ≤ 20 and ≤ 128 nodes total. Every leaf string is HTML-escaped on serialization, so document data can never introduce active markup, extra attributes, scripts, external fonts, or network access. The PDF result remains selectable vector text; `text`-only equation blocks keep working unchanged. PPTX cannot preserve the tree as native editable math and fails explicitly instead of flattening it (see `rendering.md`).

The reference shape for the lecture's efficiency law:

```json
{"type": "equation",
 "text": "η = P_out / P_in",
 "math": {"type": "row", "children": [
   {"type": "identifier", "name": "η"},
   {"type": "operator", "symbol": "="},
   {"type": "fraction",
    "numerator": {"type": "subscript",
                  "base": {"type": "identifier", "name": "P"},
                  "sub": {"type": "text", "value": "out"}},
    "denominator": {"type": "subscript",
                    "base": {"type": "identifier", "name": "P"},
                    "sub": {"type": "text", "value": "in"}}}]},
 "evidence": ["audio-000002"]}
```

Real lecture trigonometry/proportionality shapes are supported the same way:

```json
{"type": "row", "children": [
  {"type": "identifier", "name": "cos"},
  {"type": "number", "value": "30"},
  {"type": "operator", "symbol": "°"},
  {"type": "operator", "symbol": "="},
  {"type": "fraction",
   "numerator": {"type": "sqrt", "radicand": {"type": "number", "value": "3"}},
   "denominator": {"type": "number", "value": "2"}}]}
```

Single-letter identifiers render math-italic; multi-letter names such as `cos` render upright as function names, per MathML rules. Proportionality `∝`, the comma, and the degree mark `°` are ordinary whitelist operators. Prefer the source's exact values (such as `√3/2`) over decimal approximations unless the source itself stated the decimal.

Inline formulas inside prose (bullets, cells, captions) must be written as inline code spans (``like this``) so the renderer isolates them left-to-right; plain Arabic prose would display a formula span in reversed reading order — never leave an inline formula as plain text. Code spans only isolate a simple inline string (a symbol, a name, one short value): they do NOT provide real math layout — no stacked fractions, no lowered subscripts, no raised superscripts, no roots. Whenever a formula needs a fraction, subscript, superscript, square root, or proportionality, write it as an equation block with the optional `math` tree; do not fake complex math with backticks.

### Image

```json
{"type": "image",
 "asset_id": "supplied-table",
 "section": "entities",
 "caption": "A three-column table with the primary key marked.",
 "placement": "auto",
 "evidence": ["screen-000012", "audio-000002"]}
```

The asset must be `accepted`, actually inspected, and reviewed **for this exact section**. See `styles-and-images.md`.

### Instruction: screenshot, actions, and result (v0.1)

For a paired software tutorial, author each complete teaching unit explicitly:

```json
{
  "type": "instruction",
  "title": "Choose the columns to expand",
  "text": "Expand the nested table to bring its selected fields into the query.",
  "asset_id": "expand-dialog",
  "caption": "The expansion dialog with the required fields selected.",
  "steps": ["Open the expand control on the nested table column.",
            "Select the demonstrated fields and confirm."],
  "result": "The selected fields appear as columns in the query.",
  "layout": "side-by-side",
  "callouts": [{"step": 1, "box": [0.1, 0.1, 0.25, 0.2]}],
  "evidence": ["screen-000012", "audio-000002"]
}
```

Title, text, caption, result, and a nonempty list of step strings are required.
The image follows the same acceptance, label-language, section-placement, source,
and byte-binding checks as an image block. Every statement still needs supporting
evidence; the sample coordinates are illustrative, not coordinates for a real UI.

`layout` is `auto` (default), `side-by-side`, or `stacked`. Auto stacks images
whose width/height ratio is at least 2.4; otherwise it pairs them horizontally.
The complete image is fitted without cropping. The author chooses the correct
crop before registering/reviewing the asset. `callouts` defaults to an empty list;
each entry has exactly `step` (a one-based step number) and `box` (normalized
`[left, top, width, height]`, wholly inside the image). The PDF draws vector
marks; PPTX creates editable shapes and number labels. Use a clean crop with
these callouts for editable annotations, rather than baking marks into pixels.

One instruction occupies one PDF page / PPTX slide with all its teaching text.
Content that cannot fit raises an actionable error: split it at a meaningful
substep and repeat the appropriate screenshot/context in the next instruction.
The renderers do not silently discard steps or indefinitely shrink the text.
The preview selects the first two complete instruction units; author a typical
unit and a dense unit first to test both compositions before full assembly.

The asset record may optionally carry `source_name` (plain text, 1–48 printable characters, no control characters). It is the compact citation label shown in the rendered source line — a source brand name such as JoAcademy is fine even in an Arabic document — while the link still points to the real `source_url`/`source_page`, which are never replaced or removed. The preview binding includes `source_name`, so changing it invalidates an existing accepted preview.

## Omissions and uncertainties

Omission kinds: `repetition`, `noninstructional`, `unreadable`, `duplicate`, `outside_scope`, `superseded`, `redundant_visual`. Each omission needs a specific reason of at least 8 characters.

```json
{"evidence_id": "audio-000031", "kind": "repetition",
 "reason": "The lecturer repeated the same definition while changing slides."}
```

An `unreadable` omission requires a matching uncertainty:

```json
{"evidence_id": "screen-000018",
 "location": "00:42:17",
 "description": "The equation's exponent is unreadable; only the left side can be transcribed."}
```

Uncertainties require an existing evidence ID, a description, and a source location/timestamp. Content with unreadable evidence is allowed to continue, but the gap stays visible and is printed in the source-notes appendix.

A made-up exclusion reason is a review failure even if the validator cannot detect it. The reviewer/host must compare omissions with the actual source.

## Examples and `allow_examples`

Additional explanatory examples are a job-level choice. A block with `origin: "example"` is rejected unless the job's `preferences.allow_examples` is true. If content sets `examples_permitted: false`, example blocks are rejected. Examples remain visibly labelled in PDF and PPTX and must not be attributed to the lecturer. Permission for examples is not permission to invent unsupported claims.

## Image policy summary

`preferences.image_mode` accepts `text`, `web`, `user`, `mixed`/`both`, or explicit combinations such as `web+user`. Lecture captures/reference images require the job to include `lecture`/`source` or `allow_source_images: true`.

Text-only means no inserted picture objects at all, including lecture captures and decorative images. Screen review is still mandatory and screen evidence may be cited in text.

`preferences.image_mode` is the picture-source policy only. The requested label language of inserted images lives in `preferences.image_language` (`ar`, `en`, or `mixed`); language-aware jobs additionally validate each used asset's `language_review` against it (see `references/image-intake-and-search.md`). New CLI jobs record it at intake; legacy jobs without it keep the source-policy contract and carry a validation warning when images are inserted.

## Final-output readiness

`authoring.require_output_readiness(root, content)` runs all of these gates. `authoring.require_screen_review(root)` is also public.

### Speech

Accepted when `work/transcript.json` contains at least one nonempty spoken text record. Otherwise an explicit reviewed no-speech/no-audio marker is required, in either:

```json
{"stages": {"speech": {"status": "no_speech_reviewed",
  "reason": "The imported video has no audio stream; the screen was reviewed."}}}
```

or `work/speech-state.json` with the same fields. A completed stage flag without evidence is not approval.

### Screen

`work/frames-state.json` must exist and contain a nonempty `frames` array. `frames[-1].requested` must reach the requested interval end (from `state.requested_end`, `job.media.end`, or `preferences.end`). `state.complete: false` blocks readiness.

Every unique retained `frame_id` needs a matching `screen.json` record with status `reviewed` or `unreadable` and a recorded observation. A status alone is not review. Unreadable records need an uncertainty description. Reviews for non-retained IDs are rejected. Duplicate candidates may map to one representative, but they count as separate candidates in the report. Candidate mappings must not be a single frame for a long interval: very sparse candidate lists are rejected even when a representative is reviewed.

A contact sheet, one reviewed frame, or a missing screen file does not prove a long lecture was reviewed.

### Reference material

Records marked `requires_visual_review`/`needs_visual_review` or of kinds `diagram`, `scanned_page`, `figure`, with extracted images, need `review_status` `reviewed`/`unreadable` plus an observation. Unreadable references need uncertainty. Text extraction is not visual understanding.

## Preview binding and acceptance

1. Render the preview with `render_pdf.render_preview` or `render_pptx.render_preview`.
2. Show the actual artifact (PDF/PNG/slides) to the user.
3. After explicit user acceptance: `authoring.record_preview(root, content, style)` then `authoring.accept_preview(root, content, style)`. For a PPTX-only preview pass `preview_path="work/preview-slides.pptx"` when no PDF was exported.
4. Final rendering refuses to run without `authoring.require_accepted_preview`.

The binding digest covers: content, style data, relevant job preferences, source identity/range, cited evidence records, and used asset IDs/bytes/reviews/placements/captions plus every rendered source-provenance field (source_url, source_page, source_name, attribution, creator, license, licensing, origin, path, original_path, timestamp). Unrelated unused/excluded assets stay excluded, so changing them does not invalidate an unchanged preview. Changing any rendered provenance field, content, style, preferences, cited evidence, or used image bytes does.

The preview file itself is hashed. Replacing or deleting it invalidates acceptance. When a renderer writes `work/preview-meta.json`, its binding digest and per-file hashes are checked; a stale preview cannot be adopted silently.

`accept_preview` records what the host reports. It cannot independently prove a human was shown the file; the host owns that honesty.

## Validation report and limits

`authoring.validate_content` returns counts, block types, omission/uncertainty details, image mode, proven checks, warnings, and `semantic_completeness_verified: false`. It raises `ValueError` with an actionable message for:

- unknown/duplicate/missing evidence IDs;
- duplicate section IDs, empty sections/blocks, malformed tables;
- unsupported block types or origins, empty text/equations;
- invalid `math` trees in equation blocks (unknown node type/field, malformed leaf value, oversized tree);
- disabled examples;
- uncovered substantive evidence;
- invalid omission kinds/reasons, unreadable evidence without uncertainty;
- text-only image blocks;
- image assets that are excluded/pending, uninspected, policy-mismatched, missing, unreadable, lacking section placement review, or using an unlabelled crop;
- invalid asset `source_name` (must be 1–48 printable characters).

It does not prove that a citation supports the prose, that the explanation is complete, or that a diagram was interpreted correctly.
