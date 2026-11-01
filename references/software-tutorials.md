# v0.1 - Software tutorials: screenshots and practical explanation

Feature title: Instructional videos for "Office" software and other programs,
accompanied by detailed explanations in PDF files.

Apply this branch to local or YouTube videos teaching Office, Power BI, Excel,
RStudio, GitHub, or another desktop/browser application. Keep PDF/PPTX, language,
examples, design, image policy, and timestamp choices from the main intake.
Theoretical lessons use diagrams, equations, and layouts appropriate to their
subject; application click targets are specific to this branch.

## Ask only for missing choices

Read the request and existing preferences first. A reference or explicit request
may already settle these choices. Ask the unanswered questions together, before
long ingest or document assembly, in the user's language. Recommendations are
options, not submitted answers.

1. **Detail level:** "Would you like every important teaching point, step, and
   example written concisely, with repetition and unrelated talk removed, or
   a near-verbatim explanation even if it takes more pages?"
   Recommend **all important teaching, written concisely**. The alternative is
   **near-verbatim teaching**. Concise means removing repetition, not dropping
   distinct explanations, useful instructional questions, examples, or results.
2. **Instructional layout:** "Would you like a clear annotated screenshot and
   its related steps on the same page, or do you have another layout or
   reference you would like me to follow?"
   Recommend **annotated screenshot and its steps together**. Keep a supplied
   layout/reference instead of asking again. Treat a request to match a design
   as a composition requirement, not just a palette/font choice.
3. **Screenshot source:** ask this only when pictures are requested and the
   source choice remains unanswered:

> Should I take screenshots from the recording, or open the application and
> reproduce the demonstrated steps? I recommend recording screenshots because
> they match the lecturer's interface and data.

Offer two choices: **From the recording (recommended)** and **From the application**. A
text-only request skips the layout's screenshot choice and this source question;
keep the agreed detail level and use clear text instructions. Wait for missing
answers while continuing independent, inexpensive inspection. Preserve choices
already provided; the skill owner's branch-design questions are not extra
questions to repeat to every end user.

Record these answers in the job's intake notes. They are host workflow choices,
not new CLI flags or automatic application-control features. Use the existing
source-image permission for lecture captures and preserve per-image provenance;
see `styles-and-images.md`.
Recording or application screenshots do not require a web-image search. Search
for additional web images only when the user requested them.

## Capture the demonstrated step

- **Recording chosen:** inspect original-resolution frames and crop the exact
  menu, dialog, formula or result. Try a clearer neighboring frame and the
  bounded recovery workflow before deciding the necessary control is unreadable.
  Crop irrelevant meeting chrome while retaining the context needed to follow
  the step. Crop and enlargement do not recreate missing detail.
- If an essential part remains unreadable, propose reproducing that specific
  part in the application. Keep recording-only choices unless the user agrees
  to this fallback. When the user already authorized an application fallback,
  use it only for the unresolved part. Continue independent sections meanwhile.
- **Application chosen:** check actual availability and capture the equivalent
  demonstrated state using the required data and version where available.
  If a required application or course file is missing, explain the specific gap
  and offer recording captures. Use a separate working copy for reproduction.
- Identify every image internally as a recording capture (original video time
  and crop) or a reproduced application view (application/version, state and
  differences). Label any material reproduction difference in the explanation.
  Never present a generic screenshot as the lecturer's actual result.

## Compose an instructional page

For the paired layout, compose each teaching unit before export: a focused title,
a short explanation, the relevant screenshot, ordered actions, and the expected
visible result. Place the screenshot beside its own steps, or above them when a
wide dialog needs more room, on the same page/slide. Numbered boxes or arrows
must identify the control, cell, code line, command, or result those steps name.
Keep PowerPoint text and callouts editable when PPTX is requested.

Use the smallest set of screenshots that demonstrates the distinct actions and
results. Add a screenshot when it teaches something the existing image cannot
show. Source-review frames are evidence, not a quota of pictures to insert.
Crop irrelevant chrome while retaining labels, values, and navigation context.

Split a dense unit at a meaningful substep and keep each resulting image with
its explanation. Avoid sentence-fragment continuation pages, image-only pages
whose instructions are elsewhere, and topic dividers that add no teaching.
Page count follows readable, complete teaching; a fixed count is not a reason
to remove material or shrink labels beyond legibility.

Omit unrelated chatter from the authored explanation and retain relevant
instructional questions. Preserve the original recording and raw evidence.
Distinguish observed errors, unfinished demonstrations and added corrections.
Keep each user's chosen language, format and timestamp visibility; this branch
does not make English, both formats or hidden times universal defaults.

## Preview and efficient review

Before assembling the full document, show a small real-content pilot containing
a typical unit and a dense dialog or multi-step unit in each requested format.
Inspect and have the user judge both the teaching and image/step pairing at
reading size. A cover or a colour match alone does not demonstrate the layout.

For the paired layout, set `document_mode: "software_tutorial"` in `content.json`
and use the [instruction block contract](content-contract.md#instruction-screenshot-actions-and-result-v01).
It renders each screenshot and its steps as one PDF page / PPTX slide, and keeps
the detailed omission ledger in working records. A style entry named
`image-with-text` alone does not select this behavior. Split an oversized unit
at a meaningful substep when the renderer reports it cannot fit; preserve every
teaching point and the corresponding image context.

Keep detailed evidence IDs, omission ledgers, noise/repetition lists, and resource
logs in working records. The study file should contain the teaching and only
source uncertainties that materially affect understanding. Do not present an
internal omission ledger as course data or a required teaching section.

Open every final page/slide and check screenshot legibility, target positions,
step accuracy, and text clipping. A script exit alone is not visual review.
After a correction, inspect the changed pages and any pages affected by reflow;
reuse review only for demonstrably unchanged rendered pages. Keep complete
source review and distinguish it from selecting output illustrations.

Reuse the existing transcript, frame observations, and valid matching checkpoints.
Before replacing an ASR process or resetting its state, confirm the previous
process has stopped. Delegate bounded, distinct review tasks when needed and
reuse their recorded findings rather than assigning duplicate full reviews.
