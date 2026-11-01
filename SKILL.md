---
name: video-to-pdf
description: Turn one YouTube lecture URL or local lecture video, plus optional original materials, into complete evidence-backed study notes as PDF and/or editable PPTX while preserving the original timestamped transcript. Use when a user asks to convert a lecture/video into study material. Do not use for playlists, batch channels, paid transcription services, or when the user only wants a quick summary without source review.
---

# Video to PDF

For a requested Blueframe design, use `assets/styles/blueframe-lecture.json`.
Its design-only reference is
`assets/references/system-analysis-design-reference.pdf`; read
`assets/references/README.md` before using it. Offer the bundled style during
design intake when appropriate. Keep any design choice already supplied.

Produce a complete, source-grounded study document from one lecture video. The
Python scripts perform repeatable extraction and durable bookkeeping. **You**,
the host agent, perform semantic speech/screen/page understanding, teaching,
and image relevance review.

The scripts do not understand a diagram, prove that an evidence ID supports a
sentence, or know that a symbol is scientifically correct. Never present
structural IDs, a contact sheet, or a successful script exit as semantic review.

## What you need before ingest

Read the user's request and keep choices already supplied. Ask only for what is
missing, in ordinary language, **before** starting a long ingest:

1. **Source and scope:** one YouTube video URL or one local video; whole lecture
   or a selected start/end interval. Batching and playlists are out of scope.
2. **Output:** PDF, editable PPTX, or both. One complete main file per format.
3. **Language:** output language and direction; whether English technical terms
   must stay in Arabic prose. Source speech language may be auto-detected, but
   do not assume English availability means the lecture is in English.
4. **Teaching approach:** stepwise, example-led, Q&A, the user's own style, or a
   saved teaching profile; whether additional explanatory examples are allowed
   or only the lecturer's material may be used. If the user wants a particular
   explanation style or supplies an example, ask for a short representative
   sample paragraph in their own words unless they already supplied one. Judge
   whether it demonstrates enough detail, tone, step size, and terminology
   handling (including English terms inside Arabic). Ask one targeted additional
   sample only when a specific gap remains, such as equations or tables; do not
   repeat choices already supplied or keep asking after the style is clear.
5. **Images:** text only, relevant web images, user-supplied images, or an
   explicitly requested combination. For software tutorials (Power BI, Excel,
   or another application), follow
   [software tutorial intake](references/software-tutorials.md) before long
   ingest or authoring. Keep existing answers; ask only about missing detail
   level, screenshot source, and instructional page layout. Apply this branch
   to Office, Power BI, Excel, RStudio, GitHub, and other application tutorials;
   choose suitable diagrams or equations for theoretical lessons. Text-only output contains no picture
   objects, but the lecture screen must still be reviewed. Record each choice as
   `image_mode`; it is only the source policy (`mixed` means web + user
   sources). When the output language mixes two languages and images are
   wanted, additionally ask which label language the requested images must
   contain — Arabic, English, or both — and record it as `image_language`
   (`ar`, `en`, or `mixed`; "mixed" means bilingual labels inside the used
   images). A pure Arabic or English job records its own language as the image
   language automatically; override it explicitly only when the user asked for
   something else. A missing mixed-document image-language answer blocks
   `init`/`prepare` with `preferences_incomplete` before any long ingest;
   `init` lists anything still unanswered in `image_intake.questions`.
6. **Design and placement:** a saved/built-in visual style or a supplied design
   reference, plus automatic or user-specified image placement.
7. **Original materials:** PDF/PPTX/slides and attachments with roles
   (`content`, `design`, `images`, or multiple roles).
8. **Source times:** ask whether to show each section's video interval, for
   example **17:18–18:18**, or hide it. In Arabic: "تحب نعرض وقت الجزء من
   الفيديو مع كل قسم، ولا نخفيه؟" Record the explicit answer as boolean
   `show_timestamps`; keep an existing answer and do not ask again. `init`,
   `status` and `output-intake` expose unanswered questions in `output_intake`.
   Ask before authoring the preview; a legacy job with no answer still renders
   compatibly, but that fallback is not user consent.

Put reviewed per-section times in `section.source_range` as numeric absolute
`start`/`end` seconds. Do not bake navigation times into titles, summaries,
body text or cover notes: free-form instructional text is never scrubbed by
the visibility setting. When `show_timestamps` is false, omit source-navigation
times from those authored strings too. Preserve original transcript times and
internal evidence either way. This choice controls labels; it does not select
or trim the source interval. See `references/content-contract.md` and
`references/cli.md`.

Store those choices in the job's `preferences` JSON. Do not silently choose a
user-visible preference to keep the pipeline moving.

## Teaching calibration and saved profiles

- Use the user's sample explanation to judge detail level, tone, step size,
  terminology handling, and how examples are introduced. Do not infer a whole
  teaching style from a generic request such as "explain simply."
- If the sample is too short to judge a specific need, ask for one targeted
  additional paragraph about that need. Stop asking once the style is clear.
- Show a small representative explanation using the actual lecture content in
  the preview, alongside the design preview. The user judges clarity and level;
  revise from concrete feedback.
- After the preview is accepted, offer to save the teaching approach under a
  user-chosen name. Save only with explicit agreement:
  `lecture.py style-save --kind teaching_profile --name "<user name>"
  --profile profile.json --user-agreed --library "<shared-library>"`.
  Use the existing `styles.save_named_profile` API with
  `kind: "teaching_profile"`; see `references/styles-and-images.md`. Do not
  invent another backend or require a host-specific tool.
- List/load saved profiles with `style-list --kind teaching_profile` and
  `style-load --kind teaching_profile --name "<name>"`.
- Teaching-profile naming/saving is separate from visual-style naming/saving and
  from the job-level permission for extra explanatory examples. Granting one
  does not grant the others.

## Reference-design workflow

1. **Record the role explicitly.** Register the reference with `--attachment
   design=<path>` or `--role design` for `design-extract`. A design-only
   attachment is never imported as lecture content. Use `both` only when the
   user intends the file to be both design and content evidence.
2. **Extract into the job.** `lecture.py design-extract --root "<job>"
   --reference "<design.pdf>" --role design` writes the proposed style JSON,
   design report, and rendered reference previews under `work/design/`. The
   original file is preserved and hashed; generated derivatives are job-owned.
3. **Explain the actual support.** Open the rendered reference previews and read
   `supported`, `preserved`, `approximated`, `unsupported`, `excluded`,
   `conversion`, and `observed_differences`. Run
   `design-check --style "<proposal-or-saved.json>"` to inspect source/template
   identity and changed or missing assets. Report missing fonts, flattened pages,
   unavailable `.ppt` conversion, and unsupported native features honestly. For
   PPTX references, native master/layout/background reuse is through the
   backend's sanitized frozen template; inspect the actual slides and report
   unsupported elements. Never claim universal pixel-identical or cross-format
   native fidelity.
4. **Ask for a style name and explicit consent.** Only then:
   `lecture.py style-save --kind visual_style --name "<user name>"
   --style-path "<proposal.json>" --library "<shared-library>" --user-agreed`.
   A collision is rejected unless the user chooses `--collision new_version` or
   `--collision rename --new-name "<new name>"`. Never overwrite silently.
5. **Use the saved JSON path, not an unbound dict.** Render the new-content
   preview with `--style "<saved-style.json>"`. Open the new preview, the actual
   reference previews, and every `reference_comparison` image. Colour metrics
   are hints only; the user must visually compare both before separate preview
   acceptance.
6. **Naming is not final approval.** Saving a style does not approve the output.
   Accept the preview separately, then render/finalize. Reuse later with
   `style-list` and `style-load`; a changed/missing original or tampered frozen
   template must be reported explicitly.
7. **Shared assets survive job cleanup.** The saved library is outside the job
   and is never a cleanup target. Reference preview images are inspection
   artifacts; if they are job-local, regenerate them from the intact original
   for a later comparison.

## Portable invocation

Use the Python environment documented in `README.md`. Run the CLI from any
working directory with an absolute path:

```text
python /path/to/video-to-pdf/scripts/lecture.py <command> --root "/path/to/job" ...
```

Windows paths with spaces and non-ASCII characters are supported because the CLI
uses argument lists, not shell interpolation. Every command prints JSON. Read
exit status: `0` success, nonzero error or explicit incomplete outcome.

Read `references/cli.md` for the command reference and
`references/source-processing.md` for the durable file contracts.

## Workflow

### 1. Report real capabilities

```text
lecture.py capabilities --quick
```

Check the model directory, FFmpeg/FFprobe, Playwright/Chromium, and Python
packages. Read the separate `youtube` readiness object for yt-dlp version,
`yt-dlp-ejs`, and the detected Deno/Node runtime; YouTube is optional and local
video/caption work must still function without it. Also run without `--quick`
when you need an actual PDF-render probe. Do not claim PowerPoint automation or
host vision support from installed application names alone.

### 2. Create the job

```text
lecture.py init --root "<job>" --source "<video-or-url>" --preferences prefs.json
```

Add original materials with `--attachment role=path`. The original video and
attachments stay outside generated work and are never cleanup targets. If
`missing_preferences` lists `image_language` (or anything else), collect that
exact choice from the user in ordinary language and re-run `init`, or record it
afterwards with `image-intake … --apply`; do not start ingest first. For mixed
documents the returned `image_intake.questions` carries the asked question with
its options, and for image-enabled jobs a missing Arabic/English/both answer
fails before `prepare` runs. Also see the search-asking note in
`references/image-intake-and-search.md`.

### 3. Prepare the source

```text
lecture.py prepare --root "<job>"
```

This probes duration, streams, resolution, and time origin, validates a finite
`0 <= start < end <= duration` range, and binds derivatives to the source
fingerprint. A changed file at the same path fails explicitly. Use
`--accept-source-change` only after the user confirms that the new file is the
intended source; that explicit reset discards dependent frame/speech evidence.

For YouTube it downloads one anonymous video, not a playlist, and records
manual, automatic, and translated caption candidates. It requires
`yt-dlp[default]>=2026.10.21` (official EJS extra) and a detected local Deno/Node
runtime at yt-dlp's supported minimum (Deno 2.3+, Node 22+; QuickJS/Bun also
recognized). Check the separate `youtube` readiness block from `capabilities`
before attempting a stream. yt-dlp is invoked with user/system config, plugins,
cookies, remote solver components, exec hooks, and cache disabled; partial
metadata/video artifacts stay registered in the job. Resolve ambiguous
caption-track choices from the candidates; do not assume the English track is
the original speech.

### 4. Obtain speech honestly

Use source captions when they are accurate:

```text
lecture.py captions --root "<job>" --caption "<file.ar.vtt>" --provenance manual
```

Otherwise run real local ASR:

```text
lecture.py transcribe --root "<job>" --model-dir "<local-whisper-model>" --language ar
```

The model must already exist locally. There is no hidden download. Chunks are
checkpointed after completion, so rerunning resumes matching completed chunks.
If the source has no audio, or ASR finds no speech, record that reviewed outcome
explicitly:

```text
lecture.py mark-speech --root "<job>" --status no_audio --reason "<what you checked>"
```

Do not invent transcript text. Review low-confidence segments and terminology;
keep the original raw text separate from any later correction.

### 5. Review the lecture screen in bounded packets

```text
lecture.py frames --root "<job>" --limit 40
lecture.py review-packet --root "<job>" --kind screen --limit 10 --offset 0
```

Repeat `frames` until `complete` is true. A batch limit is normal; never present
a partial batch as full coverage. The default interval is fifteen seconds (minimum
allowed interval is five seconds; explicit legacy 5s/10s intervals remain valid),
and the requested interval end is always included. Extraction may clamp the final EOF
request to the last decodable instant; both requested and decoded times are
recorded.

For each packet, **open the actual retained frame**, inspect it at full size,
and write a concrete observation. Do not mark a frame reviewed because a contact
sheet exists. Preserve small symbols, equation signs, arrows, labels, table rows,
and gradual board changes. Record occurrence times; a slide that reappears keeps
its new occurrence and any distinct spoken explanation.

#### Targeted recovery on suspicious windows

When inspecting retained baseline frames, you may observe lecturer/hand occlusion,
partially written equations or code, missing symbols, or rapid slide transitions
between two adjacent baseline samples:

1. **Request 10-second recovery:** Call `refine-frames` for that specific adjacent
   baseline window ($\le$ 15s) with your concrete observation:
   ```text
   lecture.py refine-frames --root "<job>" --window-start 0 --window-end 15 --interval 10 --reason "observed occlusion over formula"
   ```
   Extraction decodes directly from the prepared original video without requiring
   prior dense frame collections. Do not ask the end user for routine permission
   for each recovery window. Same-level calls reuse existing extractions without
   duplicate decoding.
2. **Inspect and escalate to 5 seconds only if needed:** Open the new unique image(s).
   Retain distinct complementary views; do not fabricate a combined board image.
   Once the visual information in that window is sufficient, stop refining and
   resume the ordinary fifteen-second schedule outside that window. If still
   unresolved, request 5 seconds for the SAME window with a new concrete observation:
   ```text
   lecture.py refine-frames --root "<job>" --window-start 0 --window-end 15 --interval 5 --reason "formula still partly occluded"
   ```
3. **Handle unresolved gaps at 5 seconds:** If the content is still unreadable at
   5 seconds, do not invent missing symbols. Record `unreadable` with timestamp
   and uncertainty context, continue authoring independent material, and ask the
   user only if the remaining gap affects a key explanation.
4. **Prohibitions:** Never request 1-second probes or faster recovery schedules;
   never run whole-lecture recovery sweeps; never claim automated semantic
   completeness or token savings.
5. **Review all unique recovery IDs:** New unique recovery images enter
   `work/frames/` and `work/frames-state.json`. You must inspect and review each
   new ID in `review-packet` before final output readiness.

Merge only after inspecting:

```text
lecture.py merge-review --root "<job>" --kind screen --review screen-review.json
```

Review statuses: `reviewed`, `unreadable`, `pending`, `extraction_failed`.
`reviewed` needs an observation; `unreadable` and `extraction_failed` need an
uncertainty/reason. Unknown or conflicting IDs are rejected. A pending record
does not satisfy readiness.

### 6. Import and review original materials

```text
lecture.py references --root "<job>" --reference "<slides.pdf>"
lecture.py review-packet --root "<job>" --kind reference --limit 10
lecture.py merge-review --root "<job>" --kind reference --review reference-review.json
```

Original PDF/PPTX files are opened read-only. Page/slide identities, text, notes,
and readable page evidence are retained. Scanned pages, diagrams, and slides
with pictures/tables/charts are marked for actual visual review. Extraction is
not understanding. If a page cannot be read, record `unreadable` with a timestamp
and uncertainty instead of guessing.

Roles matter: a `design`-only attachment must use `design-extract` and must not be
imported here as lecture content. `both` intentionally permits content import and
design extraction. See the reference-design workflow above.

### 7. Author, validate, preview, accept, render

Read `references/content-contract.md`, `references/styles-and-images.md`, and
`references/rendering.md` (output-lane contract). Build `work/content.json` with
stable section IDs, every substantive evidence record cited or explicitly
omitted, and no invented source claims. Equation blocks may carry an optional
validated `math` tree that the PDF renders as native MathML (stacked fractions,
lowered subscripts); PPTX refuses such blocks explicitly — that is a documented
PDF-only limitation. Images may carry an optional short `source_name` used as
the compact citation label while the full `source_url`/`source_page` stays
unchanged and clickable.

#### Images: real search, review, and the ten-minute rule

When the user has requested web images, run the search loop described in
`references/image-intake-and-search.md` for each section that needs one (its
`launch_allowed` request state is your permission to search, and real searches
run only while `search_interval_open` is true; with `ready_for_search` true,
run `image-search-start` again first — the explicit start/resume that opens
the charging interval):

```text
lecture.py image-search-status --root "<job>"
lecture.py image-search-start --root "<job>" --request-id "<id>" --section "<id>" --query "<initial-query>"
# ... run the actual searches with your own tools, then record every real query:
lecture.py image-search-attempt --root "<job>" --request-id "<id>" --query "<query>" \
  --outcome none|found --candidate "<retrieved-id>" --elapsed-seconds <seconds> --note "<actual observation>"
lecture.py image-search-resolve --root "<job>" --request-id "<id>" \
  --action continue_search|use_arabic|use_english|no_images --user-confirmed
```

Rules that are non-negotiable even under deadline pressure:

- Only the ledger says whether a search may run; never launch a search while
  `status` is not `searching`, never fabricate a query you did not run, and never
  invent elapsed time — pass your real seconds or the ledger's own clock applies.
  Real searches run only inside an open charging interval
  (`search_interval_open: true`); when `ready_for_search` is true instead, the
  next explicit `image-search-start` is required to open it first.
- After a full unsuccessful budget (600 s default) the request stops and asks
  the plan's Arabic question. Present it to the user, get an answer, and record
  it with `--user-confirmed`; a timeout is not an answer and skipping the ask is
  a defect, not a shortcut.
- Every used image needs candidate relevance, the actual-label
  `language_review`, and exact-section placement review — a found candidate ID
  approves nothing by itself. Verify the labels in the pixels against the job's
  `image_language` with `image-intake --asset-id <id>` before insertion; wrong,
  unreadable, or labelless assets fail and must be asked about, not swapped in
  silently. For `mixed`, the labels of both languages must genuinely be inside
  that image; an Arabic caption over English pixels, or an Arabic image paired
  with a different English one, is not a bilingual image.
- A `no_images` decision (user choice or a definitive language loss) switches
  the job to text-only; regenerate the content picture-free, render a fresh
  preview, and obtain real acceptance — never present old previews as valid.

#### Authoring, validation, and acceptance

Process evidence in batches and keep durable authored sections; never assume a
six-hour lecture fits one context. Preserve coverage across chunk boundaries and
keep definitions, equations, terminology, and cross-references attached to their
evidence.

```text
lecture.py validate  --root "<job>" --content work/content.json
lecture.py readiness --root "<job>" --content work/content.json
lecture.py preview   --root "<job>" --content work/content.json --style scholar-ink --format pdf
```

For a reference design, replace `scholar-ink` with the exact saved style JSON path
returned by `style-save`/`style-load` (for example
`"<shared-library>/visual-styles/<slug>/<slug>-v1.0.0.json"`). Renderers receive
that path so relative frozen assets resolve; do not pass an unbound dict.

Show the actual preview artifact to the user. It must contain a small
representative explanation in the calibrated teaching style as well as the design
sample, so clarity and level are judged together. When the style has a reference
design, open `reference_previews` and every `reference_comparison` image/report;
do not trust colour metrics alone. Only after the user really accepts it:

```text
lecture.py accept --root "<job>" --content work/content.json --style scholar-ink --user-accepted
lecture.py render --root "<job>" --content work/content.json --style scholar-ink --format both
lecture.py verify --root "<job>" --output "<final.pdf>" --output "<final.pptx>"
```

A content, style, used-image, preference, or cited-evidence change invalidates
the acceptance binding. Re-preview and ask again. Readiness fails closed on
missing speech, unreviewed retained frames, and unreviewed reference diagrams.

### 8. Deliver and offer cleanup

```text
lecture.py deliver --root "<job>" --output "<final.pdf>" --offer-cleanup
```

Delivery always inspects the current final files and records their hashes. There
is no skip-verification route; `--no-render` only avoids launching a renderer,
not the structural read-back. Then ask the user whether to keep the
transcript/distinct frames for later edits or delete the generated work.

Cleanup is a two-step operation:

1. `cleanup-request` shows the exact generated targets, the protected keep scope,
   and a token, and deletes nothing. The keep scope includes the resolved
   `job.json` control record and a `retained_control_record` disclosure listing
   the metadata that survives.
2. Only after a **separate explicit user confirmation** of that exact list:
   `cleanup-confirm --token "<token>"`.

`keep` cancels pending deletion. A stale token, changed target, changed final
output, missing confirmation, traversal attempt, symlink/junction, or unknown
file preserves work. Final PDF/PPTX, original videos/attachments, shared
styles/profiles, and other jobs are protected. The retained `job.json` contains
delivery/cleanup identity and no deleted transcript or screen contents. Empty
generated directories may remain intentionally; cleanup never sweeps unknown
directory trees. A partial failure is reported as partial and never widens the
deletion scope.

## Long lectures and context limits

- Work in bounded batches. `frames --limit`, `review-packet --limit`, and
  `transcribe` chunking exist because a six-hour lecture does not fit one model
  context.
- Durable stage status and checkpoints are the source of truth after a context
  reset or process restart. Use `status` to discover completed work.
- Do not create intermediate summaries that replace evidence IDs. Keep the
  original timestamped transcript and cite it.
- A fifteen-second baseline schedule (or legacy five-second schedule) does not
  guarantee capturing content that appears and disappears between samples. Report
  that limitation and use targeted 10s/5s refinement on suspicious adjacent
  windows rather than whole-lecture dense sampling.

## Non-negotiables

- Preserve the original timestamped transcript and source languages. Translation
  is a later authoring choice, not caption import.
- Treat design-only attachments as design, not lecture content. `both` must be
  explicit; a saved style name is not final-output approval.
- Text-only output still requires screen review; it only forbids inserted picture
  objects.
- Record real image-source choices (`image_mode`) and, for image-enabled
  mixed-language jobs, a real `image_language` choice before ingest; do not pick
  either silently for the user.
- Run image searches only through the durable ledger, record every real query
  and outcome, and never launch a search after the budget without a recorded
  user-confirmed decision; never sleep-search or fabricate elapsed time.
- Report image language from the actual inspected labels, never from a caption,
  title, search result, or the file name; wrong-language or unreadable assets
  must be asked about, not substituted silently.
- Do not attribute added explanatory examples to the lecturer.
- Do not fabricate unreadable equations, symbols, diagrams, or speech. Mark the
  location and uncertainty and ask only where it affects an important
  explanation.
- Do not claim scientific completeness from validator success or from an
  evidence-ID list.
- Do not launch 1-second probes or whole-lecture recovery sweeps; adaptive
  recovery is strictly bounded to adjacent 15-second baseline windows (`10s`, then
  `5s` if needed) with concrete visual reasons.
- Do not claim cross-host support, ASR accuracy, or PowerPoint rendering without
  an actual run. See `references/host-compatibility.md`.
- Do not run paid APIs, cloud uploads, cookie extraction, or credential copying
  without explicit user authorization.

## References

- `references/cli.md` — commands, JSON outputs, exit codes, resume.
- `references/source-processing.md` — jobs, transcript, frames, references,
  invalidation, ASR checkpoints, cleanup.
- `references/image-intake-and-search.md` — image language intake, the
  real-search ledger, the ten-minute rule, and the real-video trial cases.
- `references/host-compatibility.md` — portable host adaptation and honest
  capability reporting.
- `references/content-contract.md` — authored content and evidence contract.
- `references/styles-and-images.md` — styles, profiles, image review.
- `references/rendering.md` — previews, PDF/PPTX, verification.
