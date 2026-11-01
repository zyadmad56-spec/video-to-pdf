# Video to PDF

```bash
npx skills add zyadmad56-spec/video-to-pdf
```

Turn a lecture video into complete study notes as a PDF, an editable PowerPoint, or both. Give the agent a YouTube link or a local recording, choose how you want the lesson explained, and review a short preview before it builds the full files.

The skill works with spoken explanations, lecture screens, and optional original slides. For application lessons such as Power BI or Excel, it uses screenshots beside the instructions and marks the controls you need to click.

![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![License](https://img.shields.io/badge/code-MIT-blue.svg)
![Tests](https://github.com/zyadmad56-spec/video-to-pdf/actions/workflows/tests.yml/badge.svg)

The [Skills CLI](https://github.com/vercel-labs/skills) installs the skill files. Python libraries, FFmpeg, a PDF renderer, and any local speech model need separate setup. See [local setup](#4-local-setup).

For Codex across projects:

```bash
npx skills add zyadmad56-spec/video-to-pdf --agent codex --global
```

## 1. Start with your video

Attach the recording or send one YouTube URL and ask the agent to use `$video-to-pdf`:

```text
Use $video-to-pdf with this Power BI recording.
Create an English PDF and an editable PowerPoint with step-by-step instructions.
Take screenshots from the video and mark the buttons I should click.
Leave out unrelated conversation, keep useful student questions, and hide
video timestamps in the explanation. Use the bundled Blueframe design.
```

The agent keeps choices you have already supplied and asks for the missing ones:

| Setting | Your choices |
| --- | --- |
| Source | One local video or one YouTube video, in full or over a selected interval. |
| Output | PDF, editable PPTX, or both. |
| Language | Arabic, English, or mixed explanation. Technical terms can stay in English. |
| Explanation | Step by step, examples, questions and answers, or a supplied teaching sample. |
| Extra examples | Allow added examples or use the lecturer's examples only. |
| Images | Text only, approved screenshots, supplied images, web images, or a requested combination. |
| Image labels | Arabic, English, or bilingual labels when images are requested. |
| Design | A built-in style, the bundled Blueframe adaptation, or your own reference. |
| Video times | Show section intervals such as `17:18-18:18`, or hide them in the study files. |

For Office, Power BI, Excel, RStudio, GitHub, and other application tutorials, the agent asks only for missing choices about detail level, page layout, and screenshot source. It recommends preserving all important teaching in concise wording, placing annotated screenshots with their own steps, and taking screenshots from the recording. A supplied answer or reference is kept. Text-only requests skip screenshot questions. See the [software tutorial branch](references/software-tutorials.md).

When a necessary control is unreadable, the agent checks nearby frames and crops before proposing an application capture. If you have already allowed that fallback, it reproduces only the unclear part. Application captures require access to the program and the data needed for that step. Any important difference from the recording is explained.

## 2. What the study files contain

The explanation follows the lesson's substantive material, including definitions, worked steps, tables, equations, and useful questions. Application instructions show what to select, which command or value to use, and what result to check. Screenshots sit beside the relevant step, with boxes or arrows identifying click targets.

Unrelated conversation can be omitted from the explanation while useful questions remain. An incorrect result in the recording stays identifiable as an error. A suggested correction does not become something the lecturer completed.

The original timestamped transcript and evidence records stay separate from the explanation. Hiding section times affects the study files; it does not remove times from the transcript or edit the original video.

Before the full export, you review a representative preview of the design and explanation. The agent checks the completed files for missing content, clipped text, unreadable screenshots, and incorrect placements. PowerPoint text remains editable. Native slide rendering needs an available renderer, and the agent reports when that check cannot run.

### Blueframe and the included PDF

The repository includes a privacy-edited [System Analysis design reference](assets/references/system-analysis-design-reference.pdf) and a portable [Blueframe style](assets/styles/blueframe-lecture.json). Instructor names and document metadata have been removed from the published PDF. Blueframe uses a landscape page, navy headings, blue rules, and an orange cover panel with flowing content cards in the PDF renderer.

The reference PDF guides layout. Its lecture text and diagrams do not become content for your new subject. You can supply a different PDF or PowerPoint as the design reference, or use `scholar-ink` or `clarity-grid`.

The bundled adaptation names Arial because the extracted Grandview font contains only some characters. Font files are not included. Your installed fonts can change the rendered appearance. PDF and editable PowerPoint layouts also differ; the skill does not promise an exact copy of every reference page.

Pass the Blueframe JSON path to commands that accept `--style`:

```bash
python scripts/lecture.py preview --root path/to/job --content work/content.json --style assets/styles/blueframe-lecture.json --format pdf
```

### Images from outside the recording

Web images need a direct source record and an actual relevance check. The agent checks the labels inside each image against your chosen image language. An English picture with an Arabic caption does not count as a bilingual picture.

After ten minutes of unsuccessful active searching, it asks whether to keep searching, switch the requested label language, or omit the image. Recording screenshots do not need a web search.

## 3. How the workflow fits together

The agent reads the source, writes the explanation, and checks whether the evidence supports it. The Python helpers handle repeatable extraction, validation, rendering, and saved job state.

1. Check the available tools and record your choices.
2. Prepare the video and preserve its source identity and selected interval.
3. Read usable captions or transcribe with an existing local Whisper model.
4. Review the lecture screen and any content attachments.
5. Write the complete explanation with source evidence and reviewed images.
6. Show the preview, record your acceptance, then render and inspect the final files.

Screen extraction starts at a fifteen-second interval. When the agent sees an occluded control, partial writing, or a transition, it can inspect that window at ten seconds and then five seconds if needed. Brief content can still fall between samples, so this is a review workflow rather than a completeness guarantee.

Job state supports resuming work after interruptions. Transcription chunks are checkpointed. Changed source files, invalid references, or missing approvals produce an explicit error or incomplete state.

After delivery, the agent asks whether to retain working files for edits. Cleanup requires a separate confirmation and preserves originals, final outputs, shared styles, and unknown files outside the approved generated-work inventory.

## 4. Local setup

The Skills CLI needs Node.js 22.20 or newer. It lets you select your coding agent during installation:

```bash
npx skills add zyadmad56-spec/video-to-pdf --list
npx skills add zyadmad56-spec/video-to-pdf --agent claude-code
npx skills add zyadmad56-spec/video-to-pdf --agent cursor
```

Run the following setup from the installed skill folder or a local clone:

```bash
git clone https://github.com/zyadmad56-spec/video-to-pdf.git
cd video-to-pdf
```

You need Python 3.10 or newer and FFmpeg with FFprobe on `PATH`. For PDF output, use an installed Edge/Chrome browser or install Playwright's Chromium. The agent must also be able to open images and review rendered documents.

### Windows PowerShell

```powershell
./setup.ps1
./.venv/Scripts/python.exe scripts/lecture.py capabilities
```

Optional setup switches:

```powershell
./setup.ps1 -Asr -Browser -PowerPoint
```

`-Asr` installs the local speech library. `-Browser` downloads Chromium. `-PowerPoint` installs the Python binding for an existing Windows PowerPoint installation. The setup does not install Microsoft Office or download a speech model.

### macOS and Linux

```bash
bash setup.sh
./.venv/bin/python scripts/lecture.py capabilities
```

Use `bash setup.sh --asr --browser` for the optional speech library and Chromium. Linux browser installations may also need system libraries listed by Playwright. Native PowerPoint automation is Windows-specific.

### Local speech and YouTube

For local transcription, provide an existing compatible faster-whisper model directory containing `model.bin`, `config.json`, `tokenizer.json`, and `vocabulary.txt`. Pass it with `--model-dir`. The transcription command does not silently download a model or send the video to a paid service.

YouTube support uses `yt-dlp[default]` and a compatible Deno or Node.js runtime. The setup installs the Python YouTube dependencies by default; use `-SkipYoutube` on Windows or `--no-youtube` on macOS/Linux to skip them. It does not install the JavaScript runtime.

YouTube availability depends on the video and the current platform restrictions. The downloader handles one anonymous video and does not use your browser cookies. Local video processing remains available when YouTube is unavailable.

## 5. Technical guide

Run the CLI from the skill folder:

```bash
python scripts/lecture.py --help
python scripts/lecture.py capabilities --quick
```

Use your configured Python interpreter, such as `./.venv/Scripts/python.exe` on Windows or `./.venv/bin/python` on macOS/Linux. From another directory, pass an absolute path to `scripts/lecture.py`.

The skill's public name is `video-to-pdf`. The Python module remains `lecture_materials`, and the CLI launcher remains `scripts/lecture.py`.

| File or directory | Purpose |
| --- | --- |
| [SKILL.md](SKILL.md) | Agent instructions, intake, source review, and preview workflow. |
| [scripts/lecture.py](scripts/lecture.py) | CLI entry point. |
| `scripts/lecture_materials/` | Source processing, durable jobs, validation, and PDF/PPTX renderers. |
| [references/cli.md](references/cli.md) | Commands and JSON contracts. |
| [references/software-tutorials.md](references/software-tutorials.md) | Screenshot choices and application lessons. |
| [references/source-processing.md](references/source-processing.md) | Captions, speech, frames, and review records. |
| [references/content-contract.md](references/content-contract.md) | Evidence-backed document structure. |
| [references/styles-and-images.md](references/styles-and-images.md) | Visual styles, saved profiles, and image review. |
| [assets/references/README.md](assets/references/README.md) | Bundled PDF identity and design-only role. |
| `tests/` | Source, rendering, workflow, and regression tests. |

### Tests

After installing the runtime libraries and FFmpeg, install the speech library used by the SDK-boundary tests:

```bash
python -m pip install -r requirements-asr.txt
```

This installs the library without downloading a model. Run the suite from the repository root:

```bash
python -X utf8 -m unittest discover -s tests -q
```

Renderer tests need an available Chromium route. PowerPoint reference comparisons need a slide renderer: LibreOffice Impress on Linux, or Microsoft PowerPoint with the relevant bindings on Windows. Windows-specific Office tests require Windows. The local ASR smoke test skips when its model is absent and never downloads one. The CI workflow runs on Ubuntu with Chromium, FFmpeg, LibreOffice Impress, and the speech library; passing it does not establish native Microsoft Office rendering or transcript accuracy on every lecture.

### Current limits

- One video per job. Playlists and channel batches are outside this workflow.
- Speech recognition can mishear dialects, technical terms, numbers, and overlapping speakers. The agent must review uncertain material.
- Screenshot interpretation and evidence matching require the host agent's visual review. Extraction alone does not verify a lesson.
- The bundled CLI does not control Excel, Power BI, or other application interfaces. Reproducing a step needs suitable tools in the host environment.
- Structured equation layout uses MathML in PDF. The PPTX path rejects those structured math blocks explicitly; it needs an appropriate supported representation before export.
- Design extraction supports selected layout and font properties. Missing fonts and complex native slide features can change the output.

### Version checkpoints

`the-firest-vergen` marks the original version before the `v0.1` instructional
video guidance. The feature branch is retained and merged into `main` with a
merge commits, so its changes remain identifiable. This increment adds software
tutorial intake, paired PDF/PPTX instruction units, editable callout shapes,
study-file notes separated from internal omission ledgers, and exclusive ASR
writer/reset locking. The agent asks questions in the user's conversation
language; the repository instructions and examples are English.

To inspect the original version while keeping both branches:

```bash
git switch --detach the-firest-vergen
git switch main
```

To remove the feature from `main` while preserving history, identify its merges
with `git log --oneline --merges` and revert the v0.1 merges **newest first** using
`git revert -m 1 <merge-commit>` from a clean `main` checkout. Multiple integration
merges may exist. The baseline tag always identifies the exact original version;
keep that tag and the `v0.1` branch for later comparison.

The code and original instructions use the [MIT license](LICENSE). The bundled course PDF retains its original authorship and is outside that license; see [NOTICE](NOTICE).
