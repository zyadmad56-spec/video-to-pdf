# CLI reference

Entry point: `video-to-pdf/scripts/lecture.py`. It works from any
working directory. Pass job paths explicitly with `--root`.

```text
python "<package>/scripts/lecture.py" <command> [options]
```

The CLI prints UTF-8 JSON to stdout. Errors print JSON to stderr and return a
nonzero status. It never prints a successful final-output path for an incomplete
stage.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Command completed and the requested artifact/status is valid |
| `2` | Invalid input, missing job/dependency, rejected state, or actionable error |
| `3` | Explicit incomplete speech outcome (`no_speech_detected`) |
| `4` | Rendering or verification failed |
| `5` | Cleanup was only partially completed |

A partial frame batch returns `0` with `"complete": false`; that is normal batch
progress, not full coverage. `readiness` and final rendering still fail until the
end is processed.

## Common conventions

- `--root` is a job directory containing `job.json`.
- Paths may contain spaces and non-ASCII characters.
- Relative paths passed to output-lane commands are resolved by that module
  against `--root`; source paths are resolved against the current directory.
- `--output` is repeatable and each value is one path. `--outputs` accepts a
  comma-separated convenience list only when paths contain no commas.
- `--force` is only for explicitly replacing a prior review/checkpoint after you
  inspected the actual evidence.
- No command downloads an ASR model, calls a paid API, or extracts cookies.

## Source-time visibility

Ask once whether the user wants section video intervals shown (for example
17:18–18:18). Supply `show_timestamps` as a JSON boolean in initial preferences,
or inspect/record it later:

```text
lecture.py output-intake --root "<job>"
lecture.py output-intake --root "<job>" --show-timestamps no --apply
```

Use `yes` to show them. Without `--apply`, the command only previews the choice;
without an answer it reports the question and does not choose for the user.
`init` and `status` also return `output_intake`. This question is independent of
image policy. Old jobs remain renderable without this newly introduced field;
the host must ask before making a new preview. Labels use reviewed section
`source_range` fields. Original transcript/evidence timestamps remain intact.

## Capability report

```text
lecture.py capabilities [--quick] [--strict] [--model-dir PATH]
```

Reports the interpreter, package availability/versions, FFmpeg/FFprobe, the
local Whisper model files, and the actual Chromium PDF probe. `--quick` skips
the live Chromium launch. `--strict` returns nonzero when core dependencies are
missing. PowerPoint automation is reported honestly as "not probed"; a
PPTX-render result comes from `preview`, `render`, or `verify`.

YouTube readiness is a separate top-level `youtube` object, not part of
local-video core readiness:

```json
{
  "ready": true,
  "metadata_ready": true,
  "stream_ready": true,
  "yt_dlp": {"available": true, "version": "2026.10.21", "supported": true, "minimum_tested": "2026.10.21"},
  "ejs": {"available": true, "version": "0.8.0", "package": "yt-dlp-ejs"},
  "js_runtime": {"name": "deno", "path": "...", "version": "deno 2.9.8 ...", "minimum_version": "2.3.0"},
  "remote_components_allowed": false,
  "setup_hints": []
}
```

`capabilities.youtube` retains a boolean convenience value equivalent to
`stream_ready`; `capabilities.youtube_metadata` reports metadata readiness. A
missing extra/runtime produces setup hints but does not disable local video,
caption, frame, or ASR work. Runtime minimums follow installed yt-dlp
(`deno>=2.3.0`, `node>=22.0.0`, `quickjs>=2023.12.9`, `bun>=1.2.11`); unsupported
higher-priority runtimes are skipped in favor of a supported lower-priority one,
and failed probes are not treated as ready.

## Job lifecycle

### `init`

```text
lecture.py init --root "<job>" --source "<local-video-or-youtube-url>" \
  --preferences prefs.json [--attachment role=path ...] [--allow-partial]
```

Required preferences are `output_language` (or `language`), `image_mode`,
`formats`, and `allow_examples`. Missing choices fail unless `--allow-partial` is
explicitly used. `formats` is an array such as `["pdf"]` or `["pdf","pptx"]`.

The image-label language is separate from `image_mode`: `image_mode` selects the
picture source policy (text/web/user, or mixed as web+user; it is never an image
label language). The canonical image-language preference is
`image_language: "ar" | "en" | "mixed"`. For a pure Arabic or English document
the CLI records the default (the document language) automatically at `init`.
For a mixed-language document whose image policy is not text-only, an explicit
`image_language` answer is a required choice: a missing answer fails `init` and
`prepare` with `preferences_incomplete` before any long ingest. Text-only jobs
skip the image-language questions entirely. Already-supplied answers always
persist and are never asked twice.

Example preferences:

```json
{
  "output_language": "mixed",
  "image_mode": "text",
  "formats": ["pdf", "pptx"],
  "allow_examples": false,
  "teaching_style": "stepwise",
  "visual_style": "scholar-ink",
  "image_placement": "auto",
  "start": 600,
  "end": 2520,
  "frame_interval_seconds": 5
}
```

An image-enabled mixed-language job additionally records the requested image
label language:

```json
{
  "output_language": "mixed",
  "direction": "rtl",
  "image_mode": "web",
  "image_language": "mixed",
  "formats": ["pdf", "pptx"],
  "allow_examples": false
}
```

`init` output additionally contains `image_intake`: the shared intake state with
`available`, `recorded` (the minimal defaults/normalizations actually written,
or `null` when nothing changed), and `questions` (an unanswered choice with its
ordinary-language text and options, for example the Arabic
Arabic/English/both image-label question for a mixed document). When the image
backend module cannot be imported, `image_intake.available` is `false` with the
import error and legacy behavior is preserved (no image-language gating);
downstream commands that need the backend fail with
`image_policy_unavailable` instead of faking success.

### `status`

```text
lecture.py status --root "<job>"
```

Shows source fingerprint, media range, per-stage status, speech/evidence counts,
image intake state (`image_intake`: available/recorded/questions), the parsed
`image_search_state` when search requests exist, delivery state, and cleanup
state. It is the safe command after a context reset.

### `prepare`

```text
lecture.py prepare --root "<job>" [--accept-source-change] [--allow-partial]
```

Probes/downloads one source, validates the range, records the fingerprint, and
returns caption candidates for YouTube. A changed file at the same path fails.
`--accept-source-change` is an explicit destructive invalidation of dependent
frame/speech evidence after the user confirms the new source. YouTube download is
one anonymous video with yt-dlp config/plugins/cookies/exec/remote-components/cache
explicitly disabled; it requires `yt-dlp[default]>=2026.10.21` (including
`yt-dlp-ejs`) and a detected Deno/Node/etc. runtime. Metadata- and video-stage
partial artifacts are registered for inspection. Missing YouTube readiness fails
with setup guidance; local-video work is unaffected.

### `attach`

```text
lecture.py attach --root "<job>" --path "<original-file>" --role content
```

Records an original attachment and hash with an explicit role: `content`,
`design`, `both`, or `images`. `both` records content and design roles explicitly.
Attachments are never cleanup targets. A design-only attachment is not lecture
content evidence.

## Image intake and the real-search ledger

These commands wire the shared `image_policy` backend (durable intake and
600-second search budgeting). They perform no external search themselves: the
host agent runs the actual web searches around the ledger, records every real
query with `image-search-attempt`, and never launches a search after the budget
without a recorded, user-confirmed decision. No paid search API, key, or provider
is used. State lives in `<job>/work/image-search.json` (a registered generated
file). See `references/image-intake-and-search.md` for the full workflow,
the language-review rules, and the real-video trial cases.

### `image-intake`

```text
lecture.py image-intake --root "<job>" \
  [--preferences prefs.json | --preferences-json '{...}'] \
  [--image-language ar|en|mixed] [--answer KEY=VALUE ...] [--apply]
lecture.py image-intake --root "<job>" --asset-id "<asset-id>"
lecture.py image-intake --root "<job>" --asset-file asset-record.json
```

Runs the shared intake read-only first: lists `missing_preferences`
(`/`missing ``choices``), `questions` (ungiven user choices with ordinary text
and options — for a new mixed-language image-enabled job this is the
yes/no-images follow-up and the Arabic/English/both label question), and
`recorded`. With answers supplied, re-runs intake and — only with `--apply` —
persists the resolved preferences into `job.json`; without `--apply` nothing is
written. `--image-language` is the conventional flag for the canonical
`image_language` answer; `--answer image_mode=user` style pairs record any other
missing choice (VALUE parses as JSON when valid, else stays a string). Policies
stay: text-only jobs are not asked image-language questions; already-recorded
answers are kept, not re-asked.

Output:

```json
{
  "ok": true,
  "missing_preferences": ["image_language"],
  "image_intake": {"available": true, "recorded": null, "questions": [{"id": "image_language", "question": "المستند يجمع بين العربية والإنجليزية. ما لغة العناوين داخل الصور: العربية، الإنجليزية، أم العربية والإنجليزية معًا؟", "options": [{"value": "ar", "label": "العربية"}, {"value": "en", "label": "الإنجليزية"}, {"value": "mixed", "label": "العربية والإنجليزية معًا"}]}]},
  "answered": ["image_language"],
  "recorded": {"image_language": "mixed"},
  "asset_language": null,
  "asset_validated": false,
  "preferences_updated": true,
  "preferences": {"…": "all job preferences after the intakes"}
}
```

`--asset-id <id>` / `--asset-file f.json` / `--asset-json '{…}'` validate one
image asset's actual label review against the job's requested language
(`image_language`, when present). Errors fail closed:

```text
"Image <id> has en labels but the job requested mixed; a caption or search title
does not change the labels in the pixels"        (asset_language_invalid)
"Image <id> labels are unreadable; ask before substituting an unlabeled or
unreadable asset for requested en labels"        (asset_language_invalid)
```

`--asset-id` reads the record from `work/assets.json` (unknown IDs →
`unknown_asset_record`). A language review record inside an asset is
`{"inspected": true, "language": "ar|en|mixed|none|unreadable", "reason": "<actual observation>"}`.

### `image-search-start`

```text
lecture.py image-search-start --root "<job>" \
  --request request.json
lecture.py image-search-start --root "<job>" --request-id "sec-power-1" \
  --section "efficiency" --query "لوحة شرح القدرة والكفاءة" [--budget 600]
```

Creates or explicitly resumes one durable search request for one section.
`request.json`/`--request-json` is an object with `request_id`, `section_id`,
`query` (all nonempty; `budget`/`budget_seconds` optional, default 600). The
target image language always comes from the job's own preferences, never a flag.
Resuming a live request never restarts its clock or budget; a request whose
language changed is rejected until reconciliation. A section with a still-live
request raises `search_request_invalid` (one live budget per section). Starting
or resuming opens one explicit charging interval in the durable state
(`search_interval_open`); when a `searching` request's interval is closed
(`ready_for_search` true), this call is the explicit start/resume that re-opens
it without restarting clock or budget. The response `request` field is the
backend's durable summary (every following command returns the same shape):

```json
{
  "request_id": "sec-power-1",
  "section_id": "efficiency",
  "query": "لوحة شرح القدرة والكفاءة",
  "language": "mixed",
  "status": "searching",
  "search_interval_open": true,
  "ready_for_search": false,
  "budget_seconds": 600.0,
  "elapsed_seconds": 0.0,
  "remaining_seconds": 600.0,
  "total_elapsed_seconds": 0.0,
  "round": 1,
  "attempts": [],
  "attempt_count": 0,
  "decisions": [],
  "decision_count": 0,
  "last_decision": null,
  "question": null,
  "choices": [],
  "requires_decision": false,
  "launch_allowed": true,
  "search_complete": false,
  "relevance_approved": false,
  "language_approved": false,
  "approval_note": "A completed query only identifies candidates. Relevance and image-language labels still require the existing per-asset review.",
  "stale_language": false,
  "current_language": "mixed",
  "images_disabled": false,
  "requires_reconciliation": false,
  "allowed_actions": [],
  "requires_content_regeneration": false,
  "requires_preview": false,
  "created_at": "…", "updated_at": "…", "round_started_at": "…",
  "awaiting_since": null, "budget_exhausted_at": null, "completed_at": null
}
```

`status` is one of `searching`, `awaiting_decision`, `found`, `no_images`.
The ledger never fabricates approval: `relevance_approved`/`language_approved`
always start as `false`.

### `image-search-attempt`

```text
lecture.py image-search-attempt --root "<job>" --request-id "sec-power-1" \
  --query "power efficiency diagram arabic" --outcome none --elapsed-seconds 480 \
  --note "results were diagrams with English labels only"
lecture.py image-search-attempt --root "<job>" --request-id "sec-power-1" \
  --query "استخدام الطاقة في masks" --outcome found --candidate web-001 --candidate web-002
```

Records one real query/outcome made with host tools and accrues the active
search time it consumed (host-measured via `--elapsed-seconds`, a JSON update
with `started_at`/`finished_at`, or the ledger's own active-clock interval
otherwise). `outcome: "found"` requires at least one candidate ID and completes
the request; it does NOT approve relevance, label language, or placement — those
reviews stay mandatory. `found`/full budget may move `status`, and a search that
did not fit the budget anymore fails `search_attempt_invalid`. Also accepts
`--update file.json` / `--update-json` (keys: `request_id`, `query`,
`candidate_ids` (list), `outcome`, `note`, optionally `elapsed_seconds`,
`started_at`, `finished_at`); conventional flags merge into the JSON on top.
Finding candidates only marks `search_complete`; the request lands as
`awaiting_decision` only via budget exhaustion. Recording the attempt closes
the charging interval (`search_interval_open: false`) after its measured or
accrued time is reconciled; a subsequent real query starts from a fresh
explicit start/resume.

### `image-search-status`

```text
lecture.py image-search-status --root "<job>"
lecture.py image-search-status --root "<job>" --request-id "sec-power-1"
```

Reads and advances the durable state: `elapsed_seconds` grows exactly with
wall time spent inside an open charging interval (never restarted by polling;
waiting for the user is not charged). A poll that finds a stale-language or
text-only live request stops the clock (`search_interval_open: false`)
without erasing elapsed time; reconciliation must resolve that state
explicitly. Unknown ID returns exit `2` (`search_request_not_found`). Exceeding the
recorded budget moves `status` to `awaiting_decision` with `question` and
`choices`:

```json
"question": {
  "language": "ar",
  "reason": "search_budget_exhausted",
  "text": "لم يُعثر على صورة مناسبة خلال مدة البحث. هل تريد متابعة البحث، أم استخدام صور بالعربية، أم استخدام صور بالإنجليزية، أم عدم استخدام صور؟",
  "choices": [
    {"value": "continue_search", "label": "متابعة البحث"},
    {"value": "use_arabic", "label": "استخدام صور عربية"},
    {"value": "use_english", "label": "استخدام صور إنجليزية"},
    {"value": "no_images", "label": "عدم استخدام الصور"}
  ]
}
```

With `--request-id` the output is one request summary; without it, all requests
are returned as `requests`. Exit `0` even in `awaiting_decision` — the JSON
state is the truth and `ok` only reports the status call succeeded, never that
images were solved. While `status: "searching"` and `launch_allowed` is true the
host may run real searches within `remaining_seconds`; at
`awaiting_decision` it must ask the user and record a decision before any other
search. `attempt.in_flight_after_budget` marks a search that started before the
budget boundary. See `references/image-intake-and-search.md` for the
operational flow.

### `image-search-resolve`

```text
lecture.py image-search-resolve --root "<job>" --request-id "sec-power-1" \
  --action continue_search --user-confirmed
lecture.py image-search-resolve --root "<job>" --request-id "sec-power-1" \
  --action use_english --user-confirmed
lecture.py image-search-resolve --root "<job>" --request-id "sec-power-1" \
  --action no_images --user-confirmed
lecture.py image-search-resolve --root "<job>" --decision decision.json
```

Records one explicit user choice for a paused/stale request:
`continue_search` (fresh 600-second round; it opens the next budget, not its
clock — `ready_for_search` stays true until the next explicit start/resume
opens the charging interval; prior attempts/decisions preserved),
`use_arabic`/`use_english` (switches the durable `image_language` preference and
a fresh round), or `no_images` (job becomes text-only; picture blocks must be
removed/regenerated and a fresh preview made before any new acceptance — the
response's `requires_content_regeneration`/`requires_preview` stay true).
`--user-confirmed` is mandatory (or `user_confirmed: true` in the decision
JSON); a timeout is never an answer (`decision_not_confirmed`). An unconfirmed
or inapplicable action returns exit `2` and changes nothing. The response keeps
the request summary plus `decisions[]`/`last_decision` with the previous/next
state — the CLI itself deletes and regenerates nothing.

### Error codes

New codes join the existing table (exit `2` unless noted):

| Code | Meaning |
|---|---|
| `image_policy_unavailable` | shared image backend could not be imported |
| `invalid_preferences` | preferences rejected by the shared intake |
| `invalid_answer` | malformed `--answer`/conflicting language answer |
| `asset_record_required`/`invalid_asset_record` | asset lookup/record shape |
| `asset_records_not_found`/`unknown_asset_record` | no/unmatched `work/assets.json` record |
| `asset_language_invalid` | missing/uninspected/unreadable/mismatched labels vs requested |
| `search_request_required`/`search_request_invalid` | request absent or rejected (e.g. section busy, stale language) |
| `search_attempt_required`/`search_attempt_invalid` | attempt absent or rejected (outcome, budget, stale language) |
| `search_request_not_found`/`search_status_invalid` | unknown request ID or invalid status call |
| `search_decision_required`/`search_decision_invalid` | decision absent or inapplicable |
| `decision_not_confirmed` | no explicit `user_confirmed: true` (timeout is not an answer) |



## Reference design and saved profiles

All commands use argument lists and work from any current directory. Use absolute
paths for reference files and shared libraries when the host runs from a foreign
working directory; Windows spaces and non-ASCII names are supported.

### `design-extract`

```text
lecture.py design-extract --root "<job>" --reference "<design.pdf>" \
  --role design|both [--destination work/design/<name>.json] [--overwrite]
```

Requires an explicit design role (existing attachment or `--role`). Writes the
proposed style JSON, design report, and rendered reference previews under job
`work/`. `--role both` intentionally also imports the file as lecture content.
Output includes `proposal_path`/`style_path`, `design_report_path`,
`reference_previews`, `support`, `supported`, `approximated`, `unsupported`,
`excluded`, `conversion`, `observed_differences`, `design_summary`, and
`verification`. It never saves to the shared library or approves output. Legacy
`.ppt` uses the backend's documented local conversion route or returns an
actionable conversion error; renaming is not conversion.

### `design-check`

```text
lecture.py design-check --root "<job>" --style "<style.json-or-built-in>"
```

Returns `design_summary`, `verification`, `reference_previews`, `template_path`,
`support`, `design_report_path`, `conversion`, and `source_present`. A
changed/missing original is reported; missing or tampered frozen template assets
fail with nonzero status.

### `style-list`

```text
lecture.py style-list [--library "<shared-library>"] \
  [--kind all|visual_style|teaching_profile]
```

Lists built-in visual styles plus saved names/versions. The library defaults to
`styles.default_library()` (`LECTURE_MATERIALS_LIBRARY` or the package library).

### `style-load`

```text
lecture.py style-load [--library "<shared-library>"] \
  --kind visual_style|teaching_profile --name "<name>" [--version 1.0.0]
```

Returns `path`, `id`, `name`, `version`, `document`, and for visual styles
`style_path`, `design_summary`, `support`, `design_report_path`, `conversion`,
`reference_previews`, and `verification`. Pass the exact `style_path` to
`preview`/`accept`/`render`.

### `style-save`

```text
lecture.py style-save [--library "<shared-library>"] [--root "<job>"] \
  --kind visual_style --name "<user-chosen name>" \
  --style-path "<proposal.json>" --user-agreed \
  [--collision new_version | --collision rename --new-name "<new name>"]

lecture.py style-save [--library "<shared-library>"] [--root "<job>"] \
  --kind teaching_profile --name "<user-chosen name>" \
  --profile profile.json --user-agreed \
  [--collision new_version | --collision rename --new-name "<new name>"]
```

`--user-agreed` is mandatory; naming does not approve final output. Collisions
are rejected unless a new version or a new name is explicitly chosen. Visual
styles return `reusable_style_path` plus support/report/preview/verification
fields; teaching profiles return `profile_path`. Teaching profiles may pass
`--profile <file.json>` or `--profile-json '<object>'`. Keep the shared library
outside job work; saved library assets are not cleanup targets. `reference_previews`
uses saved previews when available and otherwise the proposal previews;
`proposal_reference_previews`/`saved_reference_previews` make the difference
explicit, and job-local inspection artifacts must be regenerated from the intact
original after cleanup. Teaching-profile saving does not grant extra-example
permission or visual-style consent.

### `references`

```text
lecture.py references --root "<job>" --reference "<file.pdf>" \
  [--role content|both] [--replace]
```

Design-only attachments are rejected (`design_only_reference`). Use
`design-extract` for design, or explicitly register `both` when the file is also
lecture content. `--role both` records both roles and then imports content.
Legacy `.ppt` content import requires a converted `.pptx`; `design-extract --role
both` uses the backend's converted copy when available.


## Speech

### `captions`

```text
lecture.py captions --root "<job>" --caption "<captions.vtt>" \
  [--language ar] [--provenance manual|automatic|translated]
```

Imports VTT/SRT into `work/transcript.json` and `work/transcript.txt`, filtered to
the selected interval. Original cue text, mixed-language terminology, punctuation,
and absolute times are preserved. Rolling-caption repetition is removed only when
cues temporally overlap; later repeated teaching phrases remain.

Empty/malformed input fails. It is not a reviewed no-speech result.

### `transcribe`

```text
lecture.py transcribe --root "<job>" \
  --model-dir "<complete-local-whisper-dir>" \
  [--language ar] [--chunk-seconds 300] [--overlap-seconds 5] \
  [--beam-size 5] [--vad] [--device cpu] [--compute-type int8] \
  [--force] [--reset] [--allow-partial]
```

Runs real `faster-whisper` inference with `local_files_only=True`. Required model
files are `model.bin`, `config.json`, `tokenizer.json`, and `vocabulary.txt`.
Missing dependencies/model produce an actionable error, never a downloaded model
or fabricated text. Audio is cut into bounded chunks from the selected range.
A run-identity `work/speech-state.json` is written with `status: "in_progress"`
before model loading or chunk work; interrupted/failed runs become `partial` or
`failed` with completed-chunk tracking. Each completed chunk is checkpointed with
source/model/settings/plan identity and matching start/end. Matching completed
chunks are reused. A changed configuration is rejected until `--reset` or
`--force` explicitly discards checkpoints. Conflicting overlapping ASR text is
preserved, not collapsed by token similarity.

`status: "no_speech_detected"` returns exit code `3`; inspect the audio, then use
`mark-speech` only if that outcome is genuinely reviewed.

### `mark-speech`

```text
lecture.py mark-speech --root "<job>" --status no_audio \
  --reason "<specific evidence review>" [--evidence "..."]
```

Allowed statuses: `no_audio`, `no_speech_reviewed`, `not_applicable`,
`no_speech`. A nonempty transcript cannot be hidden with this command.

### `reset-speech`

```text
lecture.py reset-speech --root "<job>"
```

Explicitly removes ASR checkpoints/transcript after a source, model, language, or
chunk-settings change.

## Frames and screen review

### `frames`

```text
lecture.py frames --root "<job>" [--limit 40] [--interval 15] [--reset]
```

Decodes candidates at the default fifteen-second interval (minimum allowed
interval 5s; explicit legacy 5s/10s intervals remain valid) over the selected
interval, including the requested end. Only retained representatives are written
under `work/frames/`;
duplicate candidates keep timestamp/occurrence mappings in
`work/frames-state.json`. Each call resumes after the last completed candidate.
`--limit` is a bounded batch, not a coverage cap. The report separates
`candidates`, `extracted`, `retained`, `reviewed`, and
`inserted_output_images`.

Frame comparison is conservative: a small localized symbol/stroke is retained,
and a single high-contrast content pixel is retained even though lossy-codec
edge noise may cause some over-retention. Exact/static frames still deduplicate.
A fifteen-second schedule cannot promise every transient event between samples;
use targeted `refine-frames` on suspicious adjacent windows.

### `refine-frames`

```text
lecture.py refine-frames --root "<job>" --window-start 0 --window-end 15 --interval 10 --reason "observed occlusion over formula"
lecture.py refine-frames --root "<job>" --window-start 0 --window-end 15 --interval 5 --reason "formula still partly occluded"
```

Targeted direct-video recovery for a single adjacent baseline window ($\le$ 15s).
Requires an existing prepared source video and valid baseline schedule. Extraction
decodes directly from the prepared video; no prior dense frame collection is
required.

- `--window-start` and `--window-end`: Numbers identifying two adjacent already-extracted
  baseline requested times. Window length cannot exceed 15 seconds. Whole-lecture
  ranges or arbitrary/non-adjacent timestamps are rejected.
- `--interval`: Refinement level; must be `10` or `5`. Level `10` must precede
  level `5`. Repeated calls for a completed level reuse existing results without
  re-decoding or duplicate records. One-second probes or intervals below 5s are
  strictly rejected.
- `--reason`: Required non-empty string stating the host's actual visual observation
  (e.g., lecturer occlusion, partially written equations, or page transitions).
  Automatic claims of semantic completeness or token savings must not be made.

Retain distinct complementary views; do not fabricate a combined board. Once the
visual information in that window is sufficient, resume the baseline fifteen-second
schedule outside that window. Do not ask the end user for routine permission for
every refinement; ask only if remaining uncertainty affects key understanding.

New unique representative images enter `work/frames/` and are recorded in
`work/frames-state.json` under `refinements` (and window completion in
`adaptive_windows`), leaving `state['frames']` intact as the ordered baseline
schedule. Newly retained recovery IDs must be reviewed in `review-packet` before
final output readiness.

### `review-packet`

```text
lecture.py review-packet --root "<job>" --kind screen \
  --offset 0 --limit 10 [--out work/packets/screen-00.json]
```

Returns at most 25 ordered items with retained frame path, occurrence times,
occurrence count, current review status, and bounded transcript context. Includes
newly retained recovery representatives alongside baseline representatives. For
`--kind reference`, returns pages/slides that need visual review.

A packet is navigation. It does not review anything.

### `merge-review`

```text
lecture.py merge-review --root "<job>" --kind screen --review review.json [--force]
lecture.py merge-review --root "<job>" --kind reference --review review.json [--force]
```

Review file shape: an array, or an object with `reviews`/`records`.

```json
[
  {
    "id": "screen-000012",
    "status": "reviewed",
    "observations": "Static board; the new row label KEY is visible under the table header."
  },
  {
    "id": "ref-000002-s0003",
    "status": "unreadable",
    "uncertainty": "The exponent in the equation is cut off at the slide edge; only the base is readable."
  }
]
```

Known retained screen IDs only; unknown IDs fail. `reviewed` requires an
observation, `unreadable` requires uncertainty, and `extraction_failed` requires
a reason. A changed prior reviewed observation needs `--force`.

## Original materials

`references` imports PDF/PPTX/raster files as **content evidence** with the role
rules above. It never extracts a design. The original is opened read-only and
hashed before/after. PDF page text/renders, PPTX text/notes/embedded images, and
page/slide identities are retained. Scanned/vector-diagram pages and slides with
pictures/tables/charts/native shapes are marked for visual review. A changed file
at the same path requires `--replace`.

## Readiness and output

### `validate`

```text
lecture.py validate --root "<job>" --content work/content.json
```

Runs the output lane's structural/evidence/image-policy validation. It cannot
prove scientific completeness or that a citation supports its prose.

### `readiness`

```text
lecture.py readiness --root "<job>" [--content work/content.json]
```

Fails closed on incomplete speech/no-speech review, unreviewed retained screen
representatives, and unreviewed reference pages/diagrams. Text-only does not
waive screen review.

### `preview`

```text
lecture.py preview --root "<job>" --content work/content.json \
  --style scholar-ink --format pdf|pptx
```

Renders a real representative preview and records no acceptance. Show the
returned PDF/PNG/slides to the user. The output includes `source_readiness`; a
preview may be rendered before full source review, but final rendering cannot
bypass it. For a reference style, `--style` must be the exact saved style JSON
path. Preview JSON also surfaces `reference_design`, `reference_previews`,
`reference_comparison`, `design_support`, and `reference_differences`; open both
the actual reference previews and every comparison image/report. Colour metrics
are hints, not proof of visual fidelity.

### `accept`

```text
lecture.py accept --root "<job>" --content work/content.json \
  --style scholar-ink --preview work/preview-slides.pptx \
  --user-accepted --confirmed-by "host-confirmed user acceptance"
```

`--user-accepted` is mandatory. Call this only after the user actually accepted
the shown artifact. For a reference style, pass the exact saved style JSON path
used for the preview. The binding covers content, style, relevant
preferences, source identity/range, cited evidence, used image bytes/reviews, and
the preview file hash. Any material change requires a new preview and acceptance.

### `render`

```text
lecture.py render --root "<job>" --content work/content.json \
  --style scholar-ink --format pdf|pptx|both
```

Runs readiness and accepted-preview gates again. Final files go to
`<job>/outputs/`. A failure is reported per format; a partial success does not
record delivery.

### `verify`

```text
lecture.py verify --root "<job>" \
  --output outputs/notes.pdf [--output outputs/notes.pptx] \
  [--content work/content.json] [--no-render]
```

Reads actual outputs back: page/slide counts, fonts, selectable text, embedded
images/media, table structure, text-only picture policy, and content token
coverage. `--no-render` skips PowerPoint page rendering; this is not equivalent
to visual inspection.

### `deliver`

```text
lecture.py deliver --root "<job>" \
  --output outputs/notes.pdf [--output outputs/notes.pptx] \
  [--content work/content.json] [--offer-cleanup]
```

Verifies the current outputs first, then records final-output hashes as delivered.
There is no skip-verification route: `--no-render` only avoids launching
PowerPoint/PDF page rendering, while structural read-back and the text-only policy
still run. Cleanup cannot be requested until verified delivery succeeds.

## Retention and cleanup

### `keep`

```text
lecture.py keep --root "<job>"
```

Retains work and cancels any pending deletion authority.

### `cleanup-request`

```text
lecture.py cleanup-request --root "<job>"
```

Non-destructive first step. Requires verified delivery and unchanged final
outputs. Returns `delete` (concrete generated work paths, including transcript
where applicable), `keep` (originals, attachments, final outputs, and the
retained `job.json` control record), `retained_control_record` metadata, a
`confirmation_token`, and a warning. Nothing is deleted. Empty generated
directories may remain after confirmation; cleanup never sweeps unknown
directories or junctions.

### `cleanup-confirm`

```text
lecture.py cleanup-confirm --root "<job>" --token "<token>"
```

Applies only the exact previously displayed target set, after revalidating:
job root, target paths/hashes, final-output hashes, containment, and rejection of
symlinks/junctions/traversal. Unknown/unregistered files, originals, attachments,
final outputs, shared styles/profiles, and other jobs remain. `job.json` remains
as the disclosed delivery/cleanup control record and contains no deleted
transcript or screen contents. Empty generated directories may remain. A partial
failure returns exit code `5` with concrete failures and remaining paths; it does
not widen the scope.

## Resume examples

```text
# Interrupted frame sampling
lecture.py status --root "<job>"
lecture.py frames --root "<job>" --limit 60
lecture.py frames --root "<job>"

# Interrupted ASR
lecture.py transcribe --root "<job>" --model-dir "<model>"
# rerun the same command; matching completed chunks are reused

# Fresh shell/agent after a context reset
lecture.py status --root "<job>"
lecture.py review-packet --root "<job>" --kind screen --offset 20 --limit 10

# Image search: always resume through the ledger, never by re-searching blind
lecture.py status --root "<job>"          # image_search_state + image_intake
lecture.py image-search-status --root "<job>"
# ... run the next real query with host tools only while status is searching,
# record it, and resolve an awaiting_decision question only with the user's answer
```
