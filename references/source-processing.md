# Source processing and durable state

This document describes the source-processing half of the skill: one source,
one job, timestamped evidence, checkpointed extraction, and a two-step cleanup.
All state is ordinary UTF-8 JSON in the job directory. Documents are written to
a temporary file and atomically replaced.

The output lane consumes these files:

- `work/transcript.json`
- `work/screen.json`
- `work/references.json`
- `work/external.json` (only when the host has expressly permitted external evidence)
- `work/frames-state.json`
- `work/assets.json`

Do not invent a parallel evidence schema.

## Job directory

```text
<job>/
  job.json
  work/
    transcript.json
    transcript.txt
    frames-state.json
    frames/
    screen.json
    references.json
    references/
    speech-state.json
    captions-meta.json
    audio/
    design/                # proposed style/report/reference previews
    reference-previews/    # renderer-regenerated reference images
    reference-comparison/  # side-by-side comparisons/reports
    content.json          # authored by the host/output lane
    preview.pdf           # output lane
  outputs/
    <final>.pdf
    <final>.pptx
```

`outputs/` is not cleanup work. Original videos/attachments stay outside the
job. A job-owned downloaded/staged source copy lives under `work/source/` and is
registered as generated.

## `job.json`

Required fields (schema version 1):

| Field | Meaning |
|---|---|
| `schema_version` | `1` |
| `source` | Absolute local path or one URL |
| `source_fingerprint` | Local SHA-256/size/mtime identity at creation |
| `preferences` | Per-job choices; never silently invented |
| `attachments` | Original files with roles and hashes |
| `media` | Prepared source/range/stream metadata and caption candidates |
| `stages` | Durable per-stage status (`source`, `speech`, `frames`, `screen`, `references`, `delivery`) |
| `generated` | Job-owned files under `work/` eligible for confirmed cleanup |
| `outputs` | Delivered final-output relative paths and hashes |
| `preview` | Output-lane preview binding/acceptance |
| `delivered` | Final outputs were verified and recorded |
| `cleanup` | `null`, `awaiting_confirmation`, `retained`, `partially_cleaned`, or `cleaned` |

Missing preference fields do not default to user-visible choices. The host asks.
`init` requires output language, image mode, formats, and the extra-examples
choice unless the operator explicitly accepts an incomplete job.

## Source identity and range

`media.prepare_source` validates:

- the local file exists and has a usable video stream;
- duration is positive and finite;
- `0 <= start < end <= duration` with a small rounding tolerance;
- requested and actual decoded times are distinguished;
- a local source fingerprint binds derivatives to the exact bytes.

`media` records path, SHA-256, size, duration, selected start/end, time origin,
frame rate, dimensions, codecs, audio-stream metadata, probe output, and caption
candidates/files.

If the local file changes at the same path:

- normal `prepare` raises `SourceChangedError`; stale frame/speech/transcript
  evidence is not reused;
- `prepare --accept-source-change` is an explicit reset. It removes dependent
  generated speech/frame/screen artifacts, clears dependent stages and preview,
  and prepares the new bytes. Final outputs are not deleted, but the job is
  marked undelivered.

Changing the selected interval, model, chunk settings, or frame interval is also
an explicit invalidation boundary. Use `reset-frames` / `reset-speech` or the
documented prepare reset.

### YouTube

- One video only; `--no-playlist`; no cookie extraction or authentication bypass.
- Install the official `yt-dlp[default]>=2026.10.21` extra, which supplies
  `yt-dlp-ejs`. Stream extraction also needs a local supported JavaScript
  runtime. yt-dlp's installed help documents priority `deno`, `node`, `quickjs`,
  `bun`; only Deno is enabled by default. This tool detects and validates them in
  that order using installed yt-dlp minimums (`deno>=2.3.0`, `node>=22.0.0`,
  `quickjs>=2023.12.9`, `bun>=1.2.11`), skipping failed or unsupported probes,
  and passes the selected runtime explicitly.
- yt-dlp is invoked with user/system config ignored, plugin directories cleared,
  cookie/browser-cookie loading disabled, exec hooks disabled, filesystem cache
  disabled, and remote components disallowed. Intermediate files stay under the
  job's `work/source/` directory. This keeps the documented anonymous
  single-video behavior scoped to this tool without editing global config or
  downloading arbitrary solver components at runtime.
- Best available video is downloaded; there is no 720p cap.
- Metadata lists manual, automatic, and potentially translated caption
  candidates. Do not equate an available English track with original speech.
- Download/access failures are errors; empty extraction is never successful
  ingest. Metadata-stage and video-stage partial artifacts are registered for
  inspection.
- Network access is required only for this explicit download step.
- YouTube readiness is separate from local-video/core readiness. The capability
  report identifies yt-dlp, EJS, the selected runtime, and versions independently;
  local file, caption, frame, and ASR work does not require the YouTube extra.

## Transcript

`work/transcript.json` is an array of records:

```json
{
  "id": "audio-000001",
  "kind": "spoken",
  "start": 61.0,
  "end": 64.2,
  "text": "شرح Primary key",
  "raw_text": "شرح Primary key",
  "source_language": "ar",
  "provenance": {"provider": "captions", "track": "manual", "cue_index": 2},
  "provider": "captions",
  "review_status": "unreviewed"
}
```

- Times are absolute in the original media timeline, even after interval
  selection.
- `text` is the current reviewed/cleaned text; `raw_text` preserves the source
  output. Do not overwrite the original spoken language with translated study
  prose.
- Caption import removes rolling repetition only when cue times overlap. A phrase
  repeated later in the lecture remains separate.
- ASR records also carry model identity, chunk index, `avg_logprob`,
  `compression_ratio`, `no_speech_prob`, and an uncertainty-reason list. Scores
  are review signals, not calibrated accuracy.
- `work/transcript.txt` is the human-readable original timestamped transcript.

### Caption provenance

- `manual`: uploaded/manual track.
- `automatic`: automatic captions.
- `translated`: a translated track.
- Source language comes from explicit input, a track suffix, or metadata; `und`
  means unresolved. Do not infer whole-lecture language from one short sample.

### ASR checkpoints

`work/audio/asr/chunk-NNNN.json`:

```json
{
  "schema_version": 1,
  "key": "<sha256 of source/range/model/settings/plan>",
  "index": 0,
  "start": 0.0,
  "end": 300.0,
  "language": "ar",
  "language_probability": 0.93,
  "segments": [],
  "model": {"path": "...", "files": {"model.bin": {"size": 0, "sha256": "..."}}},
  "settings": {"chunk_seconds": 300, "overlap_seconds": 5},
  "elapsed_seconds": 0.0,
  "completed_at": "..."
}
```

A checkpoint is written atomically only after a chunk finishes. A rerun reuses a
chunk only when its `key`, index, start, and end all match; otherwise it fails
explicitly until the operator resets. Chunk WAV names include their actual
interval, and failed unfinished WAVs are removed, so a cached WAV cannot be
silently reused for a different interval. Successful chunk WAVs are removed after
checkpointing; the selected source audio is never uploaded.

`work/speech-state.json` is written **before** model loading and chunk work with
`status: "in_progress"` and the run source/model/settings/plan identity. Interrupted
or failed runs update it to `partial` or `failed` with completed-chunk tracking.
A changed configuration is rejected until an explicit reset. The final state
records status, key, model identity, settings, plan, chunk counts, segment count,
languages, elapsed time, tested duration, and limitations.
`status: "no_speech_detected"` is incomplete until the host records a reviewed
`no_speech_reviewed` (or the source is confirmed silent/no-audio) with a reason.

## Frames

`work/frames-state.json`:

```json
{
  "schema_version": 1,
  "key": "<sha256 of source/start/end/interval/comparison>",
  "settings": {"source": "...", "start": 0, "end": 7200.25, "interval": 15,
               "comparison": "rgb-local-v2"},
  "requested_end": 7200.25,
  "frames": [
    {"index": 0, "requested": 0, "timestamp": 0, "decoded": 0,
     "frame_id": "screen-000001", "path": "work/frames/screen-000001.png",
     "duplicate": false, "occurrence": 0}
  ],
  "representatives": [
    {"frame_id": "screen-000001", "path": "work/frames/screen-000001.png",
     "first_requested": 0, "first_decoded": 0, "coarse": [],
     "occurrences": [{"requested": 0, "decoded": 0, "duplicate": false}]}
  ],
  "complete": true,
  "counts": {"candidates": 482, "extracted": 482, "retained": 37}
}
```

- Baseline frame interval defaults to 15 seconds (minimum 5s; explicit legacy
  5s/10s intervals remain valid).
- `frames` retains the ordered baseline schedule. Extra recovery points are never
  appended into `frames`.
- Recovery attempts for adjacent baseline windows ($\le$ 15s) are recorded in
  `refinements` (window endpoints, requested/decoded times, representative ID,
  duplicate flag, interval, and host visual reason) and completed levels in
  `adaptive_windows` (10 must precede 5; retries reuse existing results).
- Direct-video extraction: recovery frames decode directly from the prepared
  original video, without requiring dense whole-lecture frame caches or 1-second probes.
- Distinct complementary views are preserved without fabricating a combined board.
  Once visual information is sufficient, resume the baseline 15-second schedule.
- Unresolved gaps at 5 seconds: if content remains unreadable at level 5, record
  uncertainty with timestamp and continue independent material; ask the user only
  if the gap affects a key explanation. Do not invent missing symbols.
- Every scheduled candidate has a mapping; duplicates do not keep a persistent
  copy.
- `requested` is the schedule time. `timestamp`/`decoded` is the actual decoded
  frame time, clamped at EOF/nonzero time origin.
- `complete` is true only after the final requested end was processed.
- Retained representatives use IDs that `screen.json` must match.
- Comparison is against retained representatives, not only the previous
  candidate, so gradual cumulative writing is not lost through a chain of small
  differences. A small localized cluster (including a 3x3 symbol) is retained.
  A single high-contrast content pixel (for example a decimal point) is also
  retained conservatively. Lossy-codec edge pixels can therefore cause some
  over-retention; storage is preferred over silently losing scientific content.
- Reappearing content may map to an earlier equivalent representative while
  preserving its new occurrence times.
- Counts remain distinct: candidates inspected, retained files, reviewed
  representatives, and inserted output images.

A limit produces partial progress with `complete: false`. No silent 100-frame cap
exists.

### `screen.json`

One record per retained representative:

```json
{
  "id": "screen-000001",
  "kind": "screen",
  "review_status": "reviewed",
  "status": "reviewed",
  "observations": "Board shows a three-column table; the middle row labels KEY.",
  "uncertainty": null,
  "frame_path": "<absolute path>",
  "occurrences": [{"requested": 120, "decoded": 120}],
  "reviewed_at": "..."
}
```

- `reviewed` needs a concrete observation.
- `unreadable` needs an uncertainty description and timestamp context.
- `pending` and `extraction_failed` never satisfy readiness.
- Reviews are merged only for known retained IDs. Unknown or conflicting IDs are
  rejected unless an explicit `--force` replacement follows actual re-inspection.
- A contact sheet or packet does not mark anything reviewed.

## Original materials

`work/references.json` is an array of page/slide evidence records:

```json
{
  "id": "ref-000001-p0002",
  "kind": "scanned_page",
  "source_file": "<absolute original path>",
  "source_sha256": "...",
  "page": 2,
  "text": "...",
  "text_path": "work/references/ref-000001-p0002.txt",
  "rendered_path": "work/references/ref-000001-p0002.png",
  "has_images": true,
  "requires_visual_review": true,
  "review_status": "pending",
  "review": {"visual_required": true, "status": "pending", "observations": null}
}
```

PPTX records use `slide`, `notes`, `table_count`, `chart_count`, and
`extracted_images`. Raster records use `kind: "image"`.

- The original file is never modified. It is hashed before and after extraction.
- Text extraction is not visual understanding.
- Scanned pages, diagrams, image/table/chart-bearing slides, and low-text slides
  require visual review.
- Reference review merges set top-level `review_status` and observation fields
  that the output lane's `authoring.require_reference_review` reads. The output
  lane needs no schema amendment for this integration.
- Unreadable evidence records uncertainty and a page/slide location instead of a
  guessed diagram or equation.

`work/external.json` is optional and only for host-permitted external evidence;
it is not created by these scripts.

## Reference design versus content evidence

Attachment roles are explicit and separate:

- `design`: use `design-extract`; never import the old subject text as lecture
  evidence.
- `content`: use `references`; text/notes/page evidence is lecture content.
- `both`: both operations are intentional. `design-extract --role both` extracts
  design and imports content; `references --role both` records both roles and
  imports content.
- `images`: supplied image role; separate from design and content.

`design-extract` writes to job `work/design/`:

- proposed style JSON (`proposal_path`/`style_path`)
- structured design report (`design_report_path`)
- rendered reference previews
- frozen relative design assets when applicable

These generated derivatives are registered as job work and are eligible for the
normal confirmed cleanup. The original attachment is preserved and hashed.
Shared saved styles/profiles live in the library outside the job and are never
cleanup targets; saved visual styles carry their own frozen assets. Reference
preview images may remain job-local inspection artifacts; regenerate them from
the intact original for a later comparison rather than assuming they survive
cleanup.

`design-check` returns `design_summary`, `verification`, `reference_previews`,
`template_path`, `support`, `design_report_path`, and `conversion`. A changed or
missing source is explicit; a missing or tampered frozen template fails rather
than silently rebinding. `preview` reports `reference_design`,
`reference_previews`, `reference_comparison`, `design_support`, and
`reference_differences`; the host must open the actual reference and comparison
images, not trust colour metrics.

## Readiness integration

`authoring.require_output_readiness` (output lane) is the final gate. It consumes:

- nonempty `transcript.json` or an explicit reviewed `speech-state.json` marker;
- complete `frames-state.json` through the requested end validating the baseline schedule;
- one reviewed/unreadable record for every unique retained representative (including
  both baseline and newly added recovery representatives);
- reviewed/unreadable `references.json` records where visual review is required.

Text-only output does not relax screen or reference review. Readiness proves
artifact coverage only; it does not prove scientific completeness.

## Cleanup

Sequence:

1. verified delivery;
2. retain/delete question;
3. non-destructive `cleanup-request` with exact generated targets and token;
4. separate explicit confirmation of that exact set;
5. `cleanup-confirm` deletes only the revalidated targets.

Protections:

- Only registered files under `work/` are candidates. Empty generated
  directories may remain intentionally; cleanup never sweeps unknown directory
  trees or reparse points.
- Originals, attachments, final outputs, shared styles/profiles, model caches,
  and other jobs are not candidates.
- `job.json` is retained as the minimal job-control/delivery receipt. The
  `cleanup-request` plan lists its resolved path in `keep` and returns a
  `retained_control_record` describing the metadata that remains: schema,
  timestamps, source path/fingerprint, preferences, attachment metadata, output
  hashes, media/probe metadata, and cleanup state. It does not retain transcript
  text, screen observations, or frame image content.
- Traversal, absolute injected paths, symlinks, junctions/reparse points, broad
  ancestor deletion, unknown/unregistered files, and changed hashes are rejected
  or preserved.
- The transcript is included where the plan lists it; the first choice never
  deletes it.
- The retained job.json control record contains no deleted transcript or screen
  contents; its cleanup receipt is limited to state/counts/timestamps.
- Partial failure reports concrete failures and remaining paths with exit code 5;
  it never widens scope.

After confirmed cleanup, later edits may require re-extraction or
retranscription. Final PDF/PPTX remain self-contained; verification can still
read them.

## Honest limits

- Baseline fifteen-second sampling (or legacy five-second sampling) can miss
  content that appears and disappears between samples. Targeted 10s/5s refinement
  recovers suspicious windows directly from source video without 1-second probes,
  whole-lecture sweeps, or claims of automated semantic completeness/token savings.
- Frame equivalence is conservative but not semantic OCR; the host must inspect
  retained frames and small symbols at full size.
- A short English synthetic smoke clip does not establish Egyptian Arabic or
  code-switching lecture accuracy.
- No six-hour real lecture or YouTube network run is claimed by the portable
  package documentation unless a specific run is recorded.
- No automatic diarization; speaker distinctions are not promised.
- No paid ASR/vision/cloud route is included.
- Cross-agent compatibility is designed through a shared CLI and documented
  capability checks; it is not proven on every host.
