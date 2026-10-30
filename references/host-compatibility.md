# Host compatibility

The package is one canonical English instruction set (`SKILL.md`), a
host-neutral Python core, and a CLI. No host-specific tool name is required for
extraction, state, or rendering.

## Capability contract

A host must be able to:

1. read `SKILL.md` and load references on demand;
2. execute the chosen Python interpreter with an argument list;
3. read/write ordinary files and keep a job directory durable across context
   resets;
4. open images/pages/slides for semantic review (its own vision/file tools);
5. run FFmpeg/FFprobe and, for PDF output, a local Chromium route;
6. show a real preview artifact to the user and ask for acceptance;
7. ask the user before any cleanup deletion.

If a host cannot read images, it may still run extraction, but it must not claim
screen/reference review. It should ask the user or hand the review packet to a
capable host.

## Invocation

Use an explicit interpreter and an absolute script path:

```bash
python "<package>/scripts/lecture.py" capabilities --quick
python "<package>/scripts/lecture.py" status --root "<job>"
```

The same command works from any current directory. Windows, spaces, and
non-ASCII paths are tested through argument lists.

Host adapters should only map their own discovery/installation conventions to
this command. Do not hardcode another host's paths in shared instructions.

## Discovery notes (verify locally; do not assume)

- **Codex:** `SKILL.md` plus optional `agents/openai.yaml`; skills live under a
  configured skills directory. Only the skill name/frontmatter is standardized
  here.
- **Claude Code:** skills are discovered from its configured skill locations;
  slash-command/skill expansion and any disable setting are host options.
- **ZCode:** the desktop/CLI may expose a skills directory or plugin mechanism.
  The earlier planning notes did not verify a path; use the installed host's
  current discovery settings.
- **Antigravity (agy):** its local help exposes slash-command/skill expansion and
  plugin/MCP management. Discovery directories were not verified.
- **Kimi:** its local help exposes `--skills-dir`; use it when present.
- **Other agents:** any host that can load a Markdown instruction file and run a
  Python CLI can use the core, subject to the capability contract.

Presence of `codex`, `claude`, `agy`, `kimi`, or a `zcode` desktop app is not proof
that a full workflow ran. Record each host as tested, partially tested, or not
tested based on an actual invocation.

## Validation scope

Run the release tests and `capabilities` in the actual host environment.
The tests cover synthetic source processing, saved state, preview acceptance,
rendering, reference handling, image policies and cleanup boundaries. Native
Office tests are Windows-specific. A real local speech smoke test needs an
existing model and skips when that model is absent.

A passing fixture does not prove transcript accuracy, complete screen coverage,
faithful reproduction of every design, or execution on every supported agent.
Record the tools used and the checks actually completed for each delivery.

## Optional Codex UI metadata

`agents/openai.yaml` declares only display text and an example prompt. It does not
add host dependencies. Other hosts ignore it.

## Model and tool setup

- Models are local directories. The CLI validates
  `model.bin`/`config.json`/`tokenizer.json`/`vocabulary.txt` and passes
  `local_files_only=True`. There is no hidden download.
- Set a model directory in preferences (`asr_model_dir`), on the command line
  (`--model-dir`), or through `LECTURE_MATERIALS_WHISPER_MODEL` /
  `WHISPER_MODEL_DIR`.
- FFmpeg, FFprobe, and yt-dlp are external tools; `capabilities` reports actual
  presence/version. On Windows, tool lookup falls back to the configured user and
  machine PATH when a long-running host inherited a stale PATH after an upgrade.
  Tests or scripts that invoke the tools directly still need a refreshed shell.
- YouTube stream extraction needs `yt-dlp[default]>=2026.10.21` (official
  `yt-dlp-ejs` included) and a supported local Deno/Node/etc. runtime. Installed
  yt-dlp minimums are `deno>=2.3.0`, `node>=22.0.0`, `quickjs>=2023.12.9`, and
  `bun>=1.2.11`. `capabilities` reports these separately; setup scripts do not
  install or download a runtime and never enable remote solver components.
- PDF rendering needs Playwright plus an available Chromium/Edge/Chrome route.
  `capabilities` launches a real probe unless `--quick` is used.
- PowerPoint automation is Windows-specific and optional. XML/read-back is not
  proof that PowerPoint opened a deck; the render report distinguishes them.

## Security and privacy boundaries

- No cookie extraction, authentication bypass, credential copying, global
  configuration edits, paid API calls, or cloud uploads are built in.
- YouTube download is an explicit user-requested network step.
- Original videos, attachments, and references are never cleanup targets.
- Shared style/profile libraries live outside a job and are never offered to
  per-job cleanup.
- Do not put secrets in preferences or job JSON.
