# Image intake, label language, and the real-search ledger

This is the operational contract for image choices in new jobs: the durable
`image_language` preference, the per-asset language review, and the durable
search ledger with its 600-second budget. Command syntax and JSON shapes are in
`references/cli.md`; `scripts/lecture_materials/image_policy.py` is the backend
that owns the rules. The CLI only marshals durable JSON and flags — the host
agent performs the actual external image searches and the actual semantic
inspection.

## The two preference fields

- `image_mode`: the picture source policy. `text`, `web`, `user`, or an explicit
  combination; `mixed` means **web + user sources**. It says nothing about the
  labels inside the pictures.
- `image_language: "ar" | "en" | "mixed"`: the requested language of the labels
  inside inserted images, when the job wants images at all.

Intake rules (applied by `init`/`prepare` and `image-intake`):

1. The staging order is `image_mode` first, then `image_language`: a
   mixed-language document is asked whether images are wanted only when that
   choice is still absent (the legacy `image_mode` requirement), and only a
   positive answer is followed by the image-label language question
   (Arabic, English, or both). If the mode answer is text/no, the
   image-language question is skipped entirely.
2. Already-supplied answers persist and are never asked twice.
3. A pure Arabic or pure English document defaults `image_language` to the
   document language, recorded automatically at `init`; the user can explicitly
   override it before `init` or with `image-intake … --image-language … --apply`.
4. New image-enabled CLI jobs must record an `image_language` choice; a
   mixed-language job with a missing choice fails `init` and `prepare` with
   `preferences_incomplete` before any long ingest.
5. Legacy jobs without `image_language` keep their historical contract.

The backend records a localized question for an unanswered mixed-document
image-language choice. Its meaning in English is:

> The document combines Arabic and English. Should labels inside its images
> be Arabic, English, or bilingual? (Options: Arabic / English / both.)

Ask it in the user's conversation language when it appears in `questions`;
translate display text while preserving question IDs and option values. Record the
answer with `image-intake --root "<job>" --image-language <value> --apply`.

## The per-image language review

For every inserted image in a language-aware job (`image_language` present and
not text-only), the existing candidate review and per-section placement review
are joined by a language review kept on the asset record:

```json
"language_review": {
  "inspected": true,
  "language": "ar",
  "reason": "Concrete observation of the actual image labels."
}
```

`language` is what the **pixels** actually contain, not the search page: one of
`ar`, `en`, `mixed`, `none`, `unreadable`. Hard rules:

- Requested language must match actual reviewed labels. Missing, uninspected,
  unreadable (`none`/`unreadable`), or mismatched reviews reject insertion for
  language-aware jobs (`validate`, `image-intake --asset-id`, and the content
  validation in the authoring lane all fail closed).
- `mixed` means a genuinely bilingual image (both Arabic and English labels
  inside the picture pixels). An Arabic caption under English-only pixels is not
  bilingual. An Arabic picture next to a different English picture is not one
  bilingual image — do not submit the pair as `mixed` silently; ask the user.
- A language-neutral unlabeled photo or an unreadable scan does not silently
  satisfy a requested labeled-diagram language; ask before substituting it.
- Mathematical symbols, SI units, and universal scientific notation are not
  natural-language labels by themselves.
- No OCR guess, search-title, caption, or filename assertion becomes semantic
  approval. The review needs a concrete observation of the actual image.
- Changing `image_language`, used image bytes, or a language review invalidates
  preview acceptance; re-preview and ask again (the ordinary preview binding).

`image-intake --asset-id <id>` shows a summary when labels match:

```json
{
  "required": true,
  "requested": "mixed",
  "observed": "mixed",
  "matched": true,
  "reason": "Both Arabic and English axis labels are present in the diagram."
}
```

and fails with `asset_language_invalid` otherwise — that rejection exit code is
the feature working, never something to route around.

## The real-search ledger and the ten-minute budget

The host performs real web searches with its own tools. The ledger
(`<job>/work/image-search.json`) records queries, providers/outcomes, candidate
IDs, timestamps, active time, decisions, and the per-request budget.

Operational loop for one section's image need:

1. **Check before searching.** Run `image-search-status`; run
   `image-search-start` only when no live request exists for that section
   (one live budget per section). Re-running
   `image-search-start --request-id … --section … --query …` on your own live
   request resumes and re-checks it — it never restarts its clock or budget.
   Starting or resuming opens one explicit charging interval
   (`search_interval_open: true`); repeating the call while that interval is
   open never restarts it. Status polls during unrelated authoring never open
   an interval and must not charge active time.
2. **Run actual searches.** Only while the returned `status` is `searching`
   and `search_interval_open` is true. When `ready_for_search` is true
   instead — an authorized `searching` round whose interval is closed — an
   explicit start/resume is required to open the charging interval before
   more real searches.
3. **Record every query.** After each real query/outcome:
   `image-search-attempt --request-id … --query … --outcome … --elapsed-seconds …`.
   Include concrete IDs for anything you actually retrieved (`--candidate`,
   repeatable) and a concrete note (language of retrieved labels, why rejected).
   Recording the attempt closes the charging interval; begin the next real
   query with a fresh explicit start/resume.
4. **Stop at the boundary.** When `elapsed_seconds` reaches
   `budget_seconds` (default 600), the request becomes `awaiting_decision`.
   A network call that was already in flight when the boundary passed may
   finish and be recorded (`in_flight_after_budget`), but **do not launch
   another search after the budget without a recorded decision**.
5. **Ask the user, really.** Relay the ledger's question in the user's
   conversation language: "No suitable image was found within the search time.
   Would you like to continue searching, use Arabic images, use English images,
   or use no images?" Then record the real answer:
   `image-search-resolve --action continue_search|use_arabic|use_english|no_images
   --user-confirmed`. Do not sleep to simulate searching and do not fabricate
   elapsed time — the ledger records what actually happened, or fails.
6. **Continue means one more round.** `continue_search` authorizes one fresh
   600-second round (all prior attempts/decisions preserved); it opens the
   next budget, not its clock — `ready_for_search` becomes true and the next
   explicit `image-search-start` opens the charging interval. Finding an
   acceptable candidate can finish a request early; a `found` outcome records
   candidate IDs only — it is not relevance or language approval.
7. **Language switch resets review**, not history. `use_arabic`/`use_english`
   changes the durable `image_language` preference and opens a fresh round;
   assets already selected must be re-validated against the new requested
   language and the preview must be re-made. Stale reuse is rejected while any
   request is live.
8. **No-images means real regeneration.** `no_images` switches the job to
   text-only picture policy; the authored picture blocks stay until the content
   is regenerated picture-free, a fresh preview is rendered and shown, and the
   user accepts it. The request state keeps
   `requires_content_regeneration`/`requires_preview` true until then. The ledger
   itself deletes and regenerates nothing, and no acceptance is ever recorded
   without the user actually accepting the shown artifact.
9. **Failed backend is not success.** If `image_policy` cannot import, every
   search/intake command fails with `image_policy_unavailable` (exit 2); handle
   it as an environment error. Never write your own duplicate ledger state.
10. **Other work continues while waiting.** Waiting for the user does not
    consume active search time, and independent content work may continue; the
    clock stops while `awaiting_decision` and only resumes after an explicit,
    confirmed round.

## The charging interval and the two status fields

An image-search round is explicit about when its clock runs:

- `search_interval_open` is true only while `status` is `searching` **and** the
  explicitly opened interval is active — the only state in which wall time is
  charged. The interval's `interval_started_at`/`interval_ended_at` frame it.
- `ready_for_search` is true when the status is `searching`, that interval is
  closed, and the request is neither stale-language nor text-only. It means an
  authorized round exists but nothing is charging: an explicit start/resume
  (`image-search-start`) is required to open the charging interval before
  further real searches.
- The interval closes when an attempt is recorded, when the budget is
  exhausted (`awaiting_decision`), when the search completes (`found`), on a
  resolved `no_images`, and when a status poll finds a stale or text-only live
  request (the clock stops and elapsed time is kept for reconciliation).
- Ledgers written before the interval marker default to
  `search_interval_open: false`: a fresh explicit start is required before
  they charge any more time.

## The five real-video trial cases

The authorized real lecture for the image-language increment is
`https://www.youtube.com/watch?v=cwnMByXnKB4` (Arabic Physics lecture, Power and
Efficiency, duration 5,872 s ≈ 97:52, Mr Moamen Mohamed). Metadata, title, or
description are not a transcript and do not prove any lecture content. The five
requested real trial cases are:

| Case | Document | Actual embedded labels | Direction |
|---|---|---|---|
| `ar-ar` | Arabic explanation/labels/captions | Arabic | RTL |
| `en-en` | English explanation/labels/captions | English | LTR |
| `mixed-ar` | Arabic explanation, English technical terms | Arabic | RTL with isolated Latin terms/equations |
| `mixed-en` | Arabic explanation, English technical terms | English | RTL with isolated Latin terms/equations |
| `mixed-mixed` | Arabic explanation, English technical terms | Both Arabic and English within the image | RTL with isolated Latin terms/equations |

Trial facts to keep honest:

- Each case has its own job preferences, assets, previews, approval, and output
  path. For the pre-authorized trial, the five language scenarios were already
  chosen by the user; ordinary, new jobs still ask for any missing choice.
- Translated English caption tracks are not the original Arabic transcript.
- "Mixed" for the image-labels review means the bilingual labels are observed in
  each used image by default; the per-image language review decides, and any
  uncertainty is surfaced instead of reinterpreted.
- No case is "done" because commands, previews, or tests executed: a case counts
  as produced only when its actual content was reviewed, its preview was shown
  and accepted by the user, and its final files exist. Nothing in this package
  fabricates that acceptance.

## Ownership and boundaries

- `image_policy.py` and the authoring-lane validation belong to the image-core
  implementer; this CLI and the skill/reference docs belong to the source/CLI
  lane. Do not duplicate the engine or invent a second schema.
- The ledger is a generated work file, registered for cleanup like other
  generated artifacts; the `job.json` control record keeps only the preferences
  and choices, not the attempt history.
- No paid search API, API key, provider credentials, or hidden network service
  is used anywhere in this feature: search is a host capability by design.
