"""One command-line entry point for the lecture-to-materials source pipeline.

The CLI never fabricates a successful stage.  It prints UTF-8 JSON and returns a
nonzero status for errors and for explicit incomplete outcomes.  All subprocess
work uses argument lists; job paths may contain spaces and non-ASCII characters.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from . import evidence, jobs, output_preferences
from .jobs import read_json, write_json

try:
    from . import media, speech
except Exception as _source_import_error:  # capability report should still run
    media = None
    speech = None
    _SOURCE_IMPORT_ERROR = _source_import_error
else:
    _SOURCE_IMPORT_ERROR = None

# The image intake/search backend is landed by a parallel implementer.  It is
# imported lazily so source/CLI work never creates a duplicate temporary
# backend; command wiring is against the shared public contract in the plan.
try:
    from . import image_policy
except Exception as _image_policy_import_error:
    image_policy = None
    _IMAGE_POLICY_IMPORT_ERROR = _image_policy_import_error
else:
    _IMAGE_POLICY_IMPORT_ERROR = None

DEFAULT_CHUNK_SECONDS = 300.0
DEFAULT_OVERLAP_SECONDS = 5.0
DEFAULT_SEARCH_BUDGET_SECONDS = 600
IMAGE_LANGUAGE_CHOICES = ("ar", "en", "mixed")
SEARCH_ACTIONS = ("continue_search", "use_arabic", "use_english", "no_images")
NO_SPEECH_STATUSES = (
    "no_audio",
    "no_speech_reviewed",
    "not_applicable",
    "no_speech",
)


_UNSET = object()


def _require_image_policy(feature):
    if image_policy is None:
        raise CliError(
            f"The image intake/search backend for {feature} is unavailable: "
            f"{_IMAGE_POLICY_IMPORT_ERROR}",
            "image_policy_unavailable",
        )
    return image_policy


def _image_intake_state(preferences, strict, baseline=None):
    """Run the shared image intake helper read-only.

    Returns ``(missing, questions, payload)``.  ``missing`` starts from the
    legacy list so the historical contract is preserved; backend answers,
    questions, and normalized defaults are added only when the shared backend
    is importable.  ``recorded`` is the minimal delta of values a durably
    applied intake would set or change against ``baseline``; an unanswered
    choice is never recorded as answered.  ``strict=True`` (init/prepare/
    intake) turns backend validation errors into CLI errors; status keeps
    reporting honestly instead.
    """

    baseline = preferences if baseline is None else baseline
    legacy_missing = _missing_preferences(preferences)
    questions = []
    if image_policy is None:
        payload = {
            "available": False,
            "error": f"image intake/search backend unavailable: {_IMAGE_POLICY_IMPORT_ERROR}",
            "behavior": (
                "legacy preferences only; image-language questions, defaults, and gates "
                "are not applied until the shared backend is importable"
            ),
            "recorded": None,
        }
        return legacy_missing, questions, payload
    try:
        intake = image_policy.intake_requirements(preferences)
    except Exception as error:  # a broken policy must never pass silently
        if strict:
            raise CliError(
                f"Image intake rejected the job preferences: {error}",
                "invalid_preferences",
            ) from error
        payload = {"available": True, "error": f"intake rejected the job preferences: {error}"}
        return legacy_missing, questions, payload
    missing = legacy_missing
    for item in (intake or {}).get("missing") or []:
        if item and item not in missing:
            missing.append(item)
    questions = list((intake or {}).get("questions") or [])
    normalized = (intake or {}).get("normalized")
    recorded = {}
    if isinstance(normalized, dict):
        for key, value in normalized.items():
            if value is None:  # an unanswered choice is not a recorded choice
                continue
            if baseline.get(key, _UNSET) != value:
                recorded[key] = value
    payload = {"available": True, "recorded": recorded or None}
    return missing, questions, payload


def _parse_answers(values):
    answers = {}
    for value in values or []:
        if "=" not in value:
            raise CliError("Answers use KEY=VALUE", "invalid_answer")
        key, raw = value.split("=", 1)
        key = key.strip()
        if not key:
            raise CliError("Answer keys must be nonempty ANSWER=VALUE items", "invalid_answer")
        if key in answers:
            raise CliError(f"Answer {key!r} was supplied more than once", "invalid_answer")
        raw = raw.strip()
        try:
            answers[key] = json.loads(raw)
        except json.JSONDecodeError:
            answers[key] = raw
    return answers


def _json_input(file_value, inline_value, file_flag, inline_flag, what):
    """Load one durable JSON document from a file argument or inline JSON."""

    if file_value and inline_value:
        raise CliError(f"Use either --{file_flag} or --{inline_flag}, not both", f"invalid_{what}")
    if file_value:
        path = Path(file_value).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.is_file():
            raise CliError(f"{what.capitalize()} file not found: {path}", f"{what}_not_found")
        try:
            return read_json(path)
        except json.JSONDecodeError as error:
            raise CliError(f"{what.capitalize()} JSON is invalid: {error}", f"invalid_{what}") from error
    if inline_value:
        try:
            return json.loads(inline_value)
        except json.JSONDecodeError as error:
            raise CliError(f"{what.capitalize()} JSON is invalid: {error}", f"invalid_{what}") from error
    return None


def _require_object(document, what):
    if document is None or isinstance(document, dict):
        return document
    raise CliError(f"The {what} JSON must contain one object", f"invalid_{what}")


def _search_state_path(root):
    return root / "work" / "image-search.json"


def _require_source_module(module, name):
    if module is None:
        raise CliError(
            f"Source-processing dependencies for {name} are unavailable: {_SOURCE_IMPORT_ERROR}",
            "dependency_unavailable",
        )
    return module


REQUIRED_PACKAGES = {
    "PIL": "Pillow",
    "numpy": "numpy",
    "pypdf": "pypdf",
    "pymupdf": "PyMuPDF",
    "pptx": "python-pptx",
}
OPTIONAL_PACKAGES = {
    "playwright": "playwright",
    "faster_whisper": "faster-whisper",
    "yt_dlp": "yt-dlp",
    "win32com": "pywin32",
}


class CliError(RuntimeError):
    def __init__(self, message, code="cli_error", exit_code=2):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def _emit(document):
    text = json.dumps(document, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _error(error):
    code = getattr(error, "code", error.__class__.__name__)
    payload = {
        "ok": False,
        "error": str(error),
        "code": str(code),
    }
    sys.stderr.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    sys.stderr.flush()
    return int(getattr(error, "exit_code", 2))


def _root(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    if not (path / "job.json").is_file():
        raise CliError(f"No job.json in {path}; run init first", "job_not_found")
    return path


def _load_preferences(args):
    if args.preferences and args.preferences_json:
        raise CliError("Use either --preferences or --preferences-json, not both")
    if args.preferences:
        path = Path(args.preferences).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.is_file():
            raise CliError(f"Preferences file not found: {path}", "preferences_not_found")
        preferences = read_json(path)
    elif args.preferences_json:
        try:
            preferences = json.loads(args.preferences_json)
        except json.JSONDecodeError as error:
            raise CliError(f"Preferences JSON is invalid: {error}", "invalid_preferences")
    else:
        preferences = {}
    if not isinstance(preferences, dict):
        raise CliError("Preferences must be a JSON object", "invalid_preferences")
    return preferences


def _missing_preferences(preferences):
    missing = []
    language = preferences.get("output_language") or preferences.get("language")
    if not language:
        missing.append("output_language")
    if not preferences.get("image_mode"):
        missing.append("image_mode")
    if not preferences.get("formats") and not preferences.get("output_formats"):
        missing.append("formats")
    if "allow_examples" not in preferences:
        missing.append("allow_examples")
    return missing


def _preference_warnings(preferences):
    warnings = []
    for key in ("teaching_style", "visual_style", "image_placement"):
        if not preferences.get(key):
            warnings.append(
                f"{key} is not recorded; ask the user if it affects this job before final output"
            )
    return warnings


def _check_prepare_preferences(root, allow_partial=False):
    job = jobs.load_job(root)
    preferences = job.get("preferences", {})
    missing, questions, _payload = _image_intake_state(preferences, strict=True)
    if missing and not allow_partial:
        raise CliError(
            "Collect the missing per-job choices before ingest: " + ", ".join(missing),
            "preferences_incomplete",
        )
    return missing


def _parse_attachment(values):
    attachments = []
    for value in values or []:
        if "=" not in value:
            raise CliError("Attachments use ROLE=PATH", "invalid_attachment")
        role, path = value.split("=", 1)
        attachments.append((role.strip(), path))
    return attachments


ROLE_ALIASES = {
    "content": ("content",),
    "design": ("design",),
    "images": ("images",),
    "both": ("content", "design"),
    "content+design": ("content", "design"),
    "design+content": ("content", "design"),
    "content,design": ("content", "design"),
    "design,content": ("content", "design"),
}


def _attachment_roles_for_value(value):
    key = str(value or "").strip().lower()
    if key not in ROLE_ALIASES:
        raise CliError(
            "Attachment role must be content, design, both, images, or an explicit "
            "content+design combination",
            "invalid_attachment_role",
        )
    return ROLE_ALIASES[key]


def _add_attachment_roles(root, path, role):
    items = []
    for resolved_role in _attachment_roles_for_value(role):
        items.append(jobs.add_attachment(root, path, resolved_role))
    return items


def _roles_for_path(root, path):
    resolved = Path(path).resolve()
    roles = set()
    for entry in jobs.load_job(root).get("attachments", []):
        if not isinstance(entry, dict):
            continue
        value = entry.get("path")
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            same = Path(value).resolve() == resolved
        except OSError:
            same = False
        if not same:
            continue
        role = str(entry.get("role", "")).strip().lower()
        if role in {"both", "content+design", "design+content"}:
            roles.update({"content", "design"})
        elif role:
            roles.add(role)
    return sorted(roles)


def _reference_content_gate(root, reference, role=None):
    if role:
        _add_attachment_roles(root, reference, role)
    roles = _roles_for_path(root, reference)
    if roles and "content" not in roles and "design" in roles:
        raise CliError(
            "This attachment is design-only. It will not be imported as lecture content; "
            "use design-extract, or explicitly register the role as both/content.",
            "design_only_reference",
        )
    return roles


def command_capabilities(args):
    packages = {}
    for module, name in {**REQUIRED_PACKAGES, **OPTIONAL_PACKAGES}.items():
        available = importlib.util.find_spec(module) is not None
        try:
            version = importlib.metadata.version(name) if available else None
        except importlib.metadata.PackageNotFoundError:
            version = None
        packages[module] = {"package": name, "available": available, "version": version}
    tools = {}
    for name in ("ffmpeg", "ffprobe", "yt-dlp"):
        resolved = media.tool_path(name) if media is not None else shutil.which(name)
        executable = resolved if resolved and Path(resolved).is_file() else shutil.which(name)
        version = None
        if name == "yt-dlp":
            command = [sys.executable, "-m", "yt_dlp", "--version"]
        elif executable:
            command = [executable, "-version"]
        else:
            command = None
        if command:
            try:
                completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
                if completed.returncode == 0:
                    version = (completed.stdout or completed.stderr).splitlines()[0]
            except Exception:
                version = None
        tools[name.replace("-", "_")] = {"path": executable, "version": version}
    tools["yt-dlp"] = tools["yt_dlp"]
    if media is not None:
        youtube = media.youtube_readiness()
    else:
        youtube = {
            "ready": False,
            "metadata_ready": False,
            "stream_ready": False,
            "setup_hints": [f"YouTube support unavailable: {_SOURCE_IMPORT_ERROR}"],
        }
    model = None
    try:
        model_dir = speech.resolve_model(
            {"model_dir": args.model_dir, "root": str(Path.cwd())}
        )
        model = {
            "path": str(model_dir),
            "complete": True,
            "files": {
                name: (model_dir / name).stat().st_size
                for name in speech.MODEL_FILES
                if (model_dir / name).is_file()
            },
            "downloaded": False,
        }
    except Exception as error:
        model = {"complete": False, "error": str(error), "downloaded": False}
    renderer = None
    if not args.quick:
        try:
            from . import render_pdf

            renderer = render_pdf.renderer_health()
        except Exception as error:
            renderer = {"available": False, "error": str(error)}
    powerpoint = {
        "pywin32_available": packages.get("win32com", {}).get("available", False),
        "actual_render_probe": "not run by capabilities; render/verify commands report the real result",
    }
    core_ready = (
        all(packages[name]["available"] for name in REQUIRED_PACKAGES)
        and tools["ffmpeg"]["path"] is not None
        and tools["ffprobe"]["path"] is not None
    )
    report = {
        "ok": core_ready,
        "python": {
            "executable": sys.executable,
            "version": sys.version,
            "platform": platform.platform(),
        },
        "package_root": str(Path(__file__).resolve().parents[2]),
        "working_directory": str(Path.cwd()),
        "packages": packages,
        "tools": tools,
        "youtube": youtube,
        "whisper_model": model,
        "renderers": {
            "pdf_chromium": renderer,
            "powerpoint": powerpoint,
        },
        "capabilities": {
            "json_state": True,
            "utf8": True,
            "local_asr": bool(model and model.get("complete") and packages["faster_whisper"]["available"]),
            "youtube": bool(youtube.get("stream_ready")),
            "youtube_metadata": bool(youtube.get("metadata_ready")),
            "pdf_rendering": bool(renderer and renderer.get("available")),
            "pptx_authoring": packages["pptx"]["available"],
        },
        "honesty": [
            "Installed applications are not proof that a render succeeded.",
            "YouTube stream readiness is reported separately from local-video/core readiness; no remote solver components are downloaded.",
            "This report does not test a full lecture, ASR accuracy, or every host agent.",
            "Designed compatibility with Claude Code, Codex, ZCode, Antigravity, Kimi, and others is not a proven run on each host.",
        ],
    }
    _emit(report)
    return 0 if report["ok"] or not args.strict else 1


def command_init(args):
    preferences = _load_preferences(args)
    output_intake = output_preferences.timestamp_intake(preferences)
    missing, questions, intake_payload = _image_intake_state(preferences, strict=True)
    if missing and not args.allow_partial:
        raise CliError(
            "Missing per-job choices: "
            + ", ".join(missing)
            + ". Collect them first or pass --allow-partial explicitly.",
            "preferences_incomplete",
        )
    # Normalized defaults from the shared intake (for example the image
    # language inherited from a pure-language document) are recorded so the
    # choice is durable and no question is ever asked twice.  Already-supplied
    # answers always win; an unanswered choice is never recorded as answered.
    stored = dict(preferences)
    stored.update(intake_payload.get("recorded") or {})
    root = Path(args.root).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    job = jobs.create_job(root, args.source, stored)
    attachments = []
    for role, path in _parse_attachment(args.attachment):
        attachments.extend(_add_attachment_roles(root, path, role))
    _emit(
        {
            "ok": True,
            "job_root": str(root.resolve()),
            "source": job["source"],
            "preferences": job["preferences"],
            "missing_preferences": missing,
            "image_intake": {**intake_payload, "questions": questions},
            "warnings": _preference_warnings(preferences),
            "output_intake": output_intake,
            "attachments": attachments,
            "next": "prepare",
        }
    )
    return 0


def command_output_intake(args):
    root = _root(args.root)
    job = jobs.load_job(root)
    preferences = dict(job.get("preferences", {}))
    if args.show_timestamps is not None:
        preferences["show_timestamps"] = args.show_timestamps == "yes"
    intake = output_preferences.timestamp_intake(preferences)
    if args.apply:
        job["preferences"] = preferences
        jobs.save_job(root, job)
    _emit({
        "ok": True, "output_intake": intake, "preferences": preferences,
        "preferences_updated": bool(args.apply),
    })
    return 0


def command_status(args):
    root = _root(args.root)
    job = jobs.load_job(root)
    preferences = job.get("preferences", {})
    missing, questions, intake_payload = _image_intake_state(preferences, strict=False)
    speech_status = (
        speech.speech_status(root)
        if speech is not None
        else {"status": "unavailable", "error": str(_SOURCE_IMPORT_ERROR)}
    )
    report = {
        "ok": True,
        "job_root": str(root),
        "source": job.get("source"),
        "source_fingerprint": job.get("source_fingerprint"),
        "preferences": preferences,
        "missing_preferences": missing,
        "image_intake": {**intake_payload, "questions": questions},
        "output_intake": output_preferences.timestamp_intake(preferences),
        "image_search_state": jobs.read_json(_search_state_path(root))
        if _search_state_path(root).is_file()
        else None,
        "stages": job.get("stages", {}),
        "media": {
            key: job["media"].get(key)
            for key in ("path", "sha256", "duration", "start", "end", "has_audio", "width", "height")
        }
        if isinstance(job.get("media"), dict)
        else None,
        "speech": speech_status,
        "evidence": evidence.evidence_status(root),
        "cleanup": jobs.cleanup_status(root),
        "delivered": bool(job.get("delivered")),
        "outputs": sorted(job.get("outputs", {})),
    }
    _emit(report)
    return 0


def command_prepare(args):
    root = _root(args.root)
    media_module = _require_source_module(media, "media")
    missing = _check_prepare_preferences(root, allow_partial=args.allow_partial)
    job = jobs.load_job(root)
    intake_missing, questions, intake_payload = _image_intake_state(
        job.get("preferences", {}), strict=True
    )
    media_info = media_module.prepare_source(root, allow_source_change=args.accept_source_change)
    candidates = {}
    if isinstance(job.get("media"), dict):
        candidates = job["media"].get("caption_candidates") or {}
    _emit(
        {
            "ok": True,
            "source": job.get("source"),
            "media": {
                key: media_info.get(key)
                for key in (
                    "path",
                    "sha256",
                    "duration",
                    "start",
                    "end",
                    "time_origin",
                    "fps",
                    "width",
                    "height",
                    "has_audio",
                    "video_codec",
                )
            },
            "audio_streams": media_info.get("audio_streams"),
            "caption_candidates": candidates,
            "missing_preferences": missing,
            "image_intake": {**intake_payload, "questions": questions},
            "warnings": _preference_warnings(job.get("preferences", {})),
            "next": "captions or transcribe",
        }
    )
    return 0


def command_captions(args):
    root = _root(args.root)
    speech_module = _require_source_module(speech, "speech")
    report = speech_module.import_captions(
        root,
        args.caption,
        language=args.language,
        provenance=args.provenance,
    )
    _emit({"ok": True, **report})
    return 0


def command_transcribe(args):
    root = _root(args.root)
    speech_module = _require_source_module(speech, "speech")
    missing = _check_prepare_preferences(root, allow_partial=True)
    if args.reset:
        speech_module.reset_transcription(root)
    settings = {
        "model_dir": (
            str((Path.cwd() / args.model_dir).resolve())
            if args.model_dir and not Path(args.model_dir).is_absolute()
            else args.model_dir
        ),
        "chunk_seconds": args.chunk_seconds,
        "overlap_seconds": args.overlap_seconds,
        "language": args.language,
        "beam_size": args.beam_size,
        "vad_filter": args.vad,
        "device": args.device,
        "compute_type": args.compute_type,
        "force": args.force,
    }
    report = speech_module.transcribe_local(root, settings)
    report["missing_preferences"] = missing
    _emit(report)
    if report.get("status") != "transcribed":
        return 3
    return 0


def command_mark_speech(args):
    root = _root(args.root)
    speech_module = _require_source_module(speech, "speech")
    report = speech_module.mark_no_speech(root, args.status, args.reason, evidence=args.evidence)
    _emit({"ok": True, **report})
    return 0


def command_frames(args):
    root = _root(args.root)
    media_module = _require_source_module(media, "media")
    missing = _check_prepare_preferences(root, allow_partial=True)
    if args.reset:
        media_module.reset_frames(root)
    report = media_module.sample_frames(root, limit=args.limit, interval=args.interval)
    _emit({"ok": True, "missing_preferences": missing, **report})
    return 0


def command_refine_frames(args):
    root = _root(args.root)
    media_module = _require_source_module(media, "media")
    report = media_module.refine_frames(
        root,
        args.window_start,
        args.window_end,
        args.interval,
        args.reason,
    )
    _emit({"ok": True, **report})
    return 0


def command_reset_frames(args):
    root = _root(args.root)
    media_module = _require_source_module(media, "media")
    _emit({"ok": True, **media_module.reset_frames(root)})
    return 0


def command_reset_speech(args):
    root = _root(args.root)
    speech_module = _require_source_module(speech, "speech")
    _emit({"ok": True, **speech_module.reset_transcription(root)})
    return 0


def command_audio(args):
    root = _root(args.root)
    media_module = _require_source_module(media, "media")
    interval = None
    if args.start is not None or args.end is not None:
        if args.start is None or args.end is None:
            raise CliError("--start and --end must be supplied together", "invalid_interval")
        interval = (args.start, args.end)
    report = media_module.extract_audio(root, interval=interval, force=args.force)
    _emit({"ok": True, **report})
    return 0


def command_references(args):
    root = _root(args.root)
    reference = Path(args.reference).expanduser()
    if not reference.is_absolute():
        reference = (Path.cwd() / reference).resolve()
    roles = _reference_content_gate(root, reference, role=args.role)
    report = evidence.import_reference(root, reference, replace=args.replace)
    _emit(
        {
            "ok": True,
            **report,
            "attachment_roles": roles,
            "design_imported": False,
            "note": (
                "Content evidence imported for review. Design extraction is a separate "
                "design-extract command; a design-only attachment is never imported here."
            ),
        }
    )
    return 0


def command_design_extract(args):
    from . import styles

    root = _root(args.root)
    reference = Path(args.reference).expanduser()
    if not reference.is_absolute():
        reference = (Path.cwd() / reference).resolve()
    if not reference.is_file():
        raise CliError(f"Design reference not found: {reference}", "reference_not_found")
    if args.role:
        _add_attachment_roles(root, reference, args.role)
    roles = _roles_for_path(root, reference)
    if "design" not in roles:
        raise CliError(
            "Design extraction requires an explicit design role. Register the reference "
            "with --role design or --role both before extracting.",
            "design_role_required",
        )
    if args.destination:
        raw = Path(args.destination).expanduser()
        if raw.is_absolute():
            try:
                relative = raw.resolve().relative_to(root)
            except ValueError as error:
                raise CliError(
                    "Design destination must stay inside the job directory",
                    "design_destination_outside_job",
                ) from error
        else:
            relative = raw
    else:
        stem = re.sub(r"[^\w .()&'\-]+", "-", reference.stem, flags=re.UNICODE).strip(" .-")
        relative = Path("work") / "design" / f"{stem or 'reference'}-proposed-style.json"
    destination = jobs.checked_path(root, relative, "work")
    result = styles.extract_style(
        str(reference), str(destination), overwrite=args.overwrite
    )
    content_import = None
    if "content" in roles:
        conversion = result.get("conversion") or {}
        content_path = conversion.get("converted_path") or reference
        content_import = evidence.import_reference(root, content_path)
    summary = styles.design_summary(result["style"], result["proposal_path"])
    previews = styles.reference_previews(result["style"], result["proposal_path"])
    verification = styles.verify_style_design(result["style"], result["proposal_path"])
    _emit(
        {
            "ok": True,
            "saved": False,
            "attachment_roles": roles,
            "reference": str(reference),
            "reference_sha256": result.get("reference_sha256"),
            "reference_format": result.get("reference_format"),
            "source_preserved": result.get("source_preserved"),
            "proposal_path": result.get("proposal_path"),
            "style_path": result.get("proposal_path"),
            "design_report_path": result.get("design_report_path"),
            "design_directory": result.get("design_directory"),
            "reference_previews": previews,
            "rendered_reference_previews": result.get("rendered_reference_previews"),
            "preview_renderer": result.get("preview_renderer"),
            "support": result.get("support"),
            "supported": result.get("supported"),
            "approximated": result.get("approximated"),
            "unsupported": result.get("unsupported"),
            "excluded": result.get("excluded"),
            "conversion": result.get("conversion"),
            "observed_differences": result.get("observed_differences"),
            "design_summary": summary,
            "verification": verification,
            "content_import": content_import,
            "next_steps": [
                "Open the rendered reference previews and report supported, approximated, and excluded features.",
                "Ask the user for the style name, then save only with explicit consent.",
                "Render a representative preview with real lecture content and open the side-by-side comparison before approval.",
            ],
        }
    )
    return 0


def command_design_check(args):
    from . import styles

    root = _root(args.root) if args.root else None
    style_value = Path(args.style).expanduser()
    if not style_value.is_absolute() and root is not None:
        style_value = root / style_value
    style_path = None
    if style_value.is_file():
        style_path = str(style_value.resolve())
        style = styles.resolve_style(style_path)
    else:
        style = styles.resolve_style(args.style)
    summary = styles.design_summary(style, style_path)
    verification = styles.verify_style_design(style, style_path)
    previews = styles.reference_previews(style, style_path)
    template_path = styles.resolve_design_template(style, style_path)
    design_record = style.get("design") if isinstance(style.get("design"), dict) else {}
    summary_reference = (summary or {}).get("reference") or {}
    source_value = summary_reference.get("original_path") or summary_reference.get("path")
    source_present = bool(source_value and Path(str(source_value)).is_file())
    ok = not verification.get("source_changed") and not verification.get("missing") and not verification.get("missing_previews")
    _emit(
        {
            "ok": ok,
            "style_path": style_path or args.style,
            "design_summary": summary,
            "verification": verification,
            "reference_previews": previews,
            "template_path": str(template_path) if template_path else None,
            "support": (design_record or {}).get("support"),
            "design_report_path": (design_record or {}).get("report_path"),
            "conversion": ((design_record or {}).get("reference") or {}).get("conversion"),
            "source_present": source_present,
        }
    )
    return 0 if ok else 2


def _library_path(args):
    from . import styles

    value = Path(args.library).expanduser() if args.library else styles.default_library()
    if not value.is_absolute():
        value = (Path.cwd() / value).resolve()
    return value


def command_style_list(args):
    from . import styles

    library = _library_path(args)
    kinds = (
        ["visual_style", "teaching_profile"]
        if args.kind == "all"
        else [args.kind]
    )
    profiles = {kind: styles.list_named_profiles(library, kind) for kind in kinds}
    builtin = [
        {"id": style.get("id"), "name": style.get("name"), "version": style.get("version")}
        for style in styles.builtin_styles()
    ]
    _emit(
        {
            "ok": True,
            "library": str(library),
            "profiles": profiles,
            "builtin_visual_styles": builtin,
            "kinds": ["visual_style", "teaching_profile"],
        }
    )
    return 0


def command_style_load(args):
    from . import styles

    library = _library_path(args)
    profile = styles.load_named_profile(
        library, args.kind, args.name, version=args.version
    )
    report = {
        "ok": True,
        "library": str(library),
        "kind": profile["kind"],
        "path": profile["path"],
        "id": profile.get("id"),
        "name": profile.get("name"),
        "version": profile.get("version"),
        "document": profile.get("document"),
    }
    if profile["kind"] == "visual_style":
        design_record = (
            profile["document"].get("design")
            if isinstance(profile["document"], dict)
            else {}
        ) or {}
        report.update(
            {
                "style_path": profile["path"],
                "design_summary": styles.design_summary(
                    profile["document"], profile["path"]
                ),
                "support": design_record.get("support"),
                "design_report_path": design_record.get("report_path"),
                "conversion": (design_record.get("reference") or {}).get("conversion"),
                "reference_previews": styles.reference_previews(
                    profile["document"], profile["path"]
                ),
                "verification": styles.verify_style_design(
                    profile["document"], profile["path"]
                ),
            }
        )
    _emit(report)
    return 0


def command_style_save(args):
    from . import styles

    if not args.user_agreed:
        raise CliError(
            "Saving requires explicit user consent; pass --user-agreed only after agreement",
            "save_consent_required",
        )
    library = _library_path(args)
    root = _root(args.root) if args.root else None
    request = {
        "kind": args.kind,
        "name": args.name,
        "user_agreed": True,
    }
    if args.version:
        request["version"] = args.version
    if args.collision:
        request["collision"] = args.collision
    if args.new_name:
        if args.collision != "rename":
            raise CliError("--new-name requires --collision rename", "invalid_collision")
        request["new_name"] = args.new_name

    def resolve(value):
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (root or Path.cwd()) / path
        return path.resolve()

    if args.kind == "visual_style":
        if not args.style_path:
            raise CliError("--style-path is required for a visual style", "style_path_required")
        style_path = resolve(args.style_path)
        if not style_path.is_file():
            raise CliError(f"Style JSON not found: {style_path}", "style_not_found")
        document = read_json(style_path)
        if not isinstance(document, dict):
            raise CliError("Style JSON must contain an object", "invalid_style")
        design = document.get("design") if isinstance(document.get("design"), dict) else {}
        reference = design.get("reference") if isinstance(design.get("reference"), dict) else {}
        provenance = document.get("provenance") if isinstance(document.get("provenance"), dict) else {}
        request.update(
            {
                "style": document,
                "style_source_path": str(style_path),
                "reference_name": reference.get("original_name")
                or provenance.get("reference_name"),
                "reference_sha256": reference.get("original_sha256")
                or provenance.get("reference_sha256"),
            }
        )
    else:
        if args.profile and args.profile_json:
            raise CliError("Use either --profile or --profile-json, not both", "invalid_profile")
        if args.style_path:
            raise CliError("--style-path is only for visual styles", "invalid_profile")
        if args.profile:
            profile_path = resolve(args.profile)
            if not profile_path.is_file():
                raise CliError(f"Profile JSON not found: {profile_path}", "profile_not_found")
            profile = read_json(profile_path)
        elif args.profile_json:
            try:
                profile = json.loads(args.profile_json)
            except json.JSONDecodeError as error:
                raise CliError(f"Profile JSON is invalid: {error}", "invalid_profile")
        else:
            raise CliError(
                "--profile or --profile-json is required for a teaching profile",
                "profile_required",
            )
        if not isinstance(profile, dict):
            raise CliError("Profile must be a JSON object", "invalid_profile")
        request["profile"] = profile

    source_design_record = {}
    if args.kind == "visual_style":
        source_design_record = (
            document.get("design") if isinstance(document.get("design"), dict) else {}
        )
    saved = styles.save_named_profile(library, request)
    saved_document = saved.get("document")
    design_extra = {}
    if saved["kind"] == "visual_style":
        summary = styles.design_summary(saved_document, saved["path"])
        design_record = saved_document.get("design") if isinstance(saved_document, dict) else {}
        source_previews = []
        for group in ("reference_pages", "reference_slides"):
            source_previews.extend(
                str(value)
                for value in ((source_design_record.get("previews") or {}).get(group) or [])
            )
        design_extra = {
            "design_summary": summary,
            "support": (design_record or {}).get("support"),
            "design_report_path": (design_record or {}).get("report_path")
            or (source_design_record or {}).get("report_path"),
            "conversion": ((design_record or {}).get("reference") or {}).get("conversion")
            or ((source_design_record or {}).get("reference") or {}).get("conversion"),
            "saved_reference_previews": styles.reference_previews(
                saved_document, saved["path"]
            ),
            "proposal_reference_previews": source_previews,
            "verification": styles.verify_style_design(saved_document, saved["path"]),
        }
        design_extra["reference_previews"] = (
            design_extra["saved_reference_previews"] or source_previews
        )
    _emit(
        {
            "ok": True,
            "library": str(library),
            "saved": {
                key: saved.get(key)
                for key in ("path", "id", "name", "kind", "version", "created_at", "collision_note")
            },
            "reusable_style_path": saved["path"] if saved["kind"] == "visual_style" else None,
            "profile_path": saved["path"] if saved["kind"] == "teaching_profile" else None,
            **design_extra,
            "explicit_consent": True,
            "note": (
                "Naming/saving does not approve final output. Render and compare a new-content preview, "
                "then obtain separate preview acceptance. A teaching profile does not grant extra-example "
                "permission."
            ),
        }
    )
    return 0



def command_review_packet(args):
    root = _root(args.root)
    packet = evidence.review_packet(root, offset=args.offset, limit=args.limit, kind=args.kind)
    if args.out:
        output = Path(args.out)
        if not output.is_absolute():
            output = root / output
        write_json(output, packet)
        packet["written_to"] = str(output)
    _emit({"ok": True, **packet})
    return 0


def command_merge_review(args):
    root = _root(args.root)
    if args.kind == "screen":
        report = evidence.import_screen_review(root, args.review, force=args.force)
    else:
        report = evidence.import_reference_review(root, args.review, force=args.force)
    _emit({"ok": True, **report})
    return 0


def _content_path(root, value):
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise CliError(f"Content file not found: {path}", "content_not_found")
    return path


def command_validate(args):
    root = _root(args.root)
    from . import authoring

    content_path = _content_path(root, args.content)
    content = read_json(content_path)
    report = authoring.validate_content(root, content)
    _emit({"ok": True, "content_file": str(content_path), "report": report})
    return 0


def command_readiness(args):
    root = _root(args.root)
    from . import authoring

    content = None
    content_path = None
    if args.content:
        content_path = _content_path(root, args.content)
        content = read_json(content_path)
    report = authoring.require_output_readiness(root, content)
    _emit(
        {
            "ok": True,
            "content_file": str(content_path) if content_path else None,
            "readiness": report,
            "speech": speech.speech_status(root),
            "evidence": evidence.evidence_status(root),
            "note": "Artifact readiness only; semantic completeness still needs host review.",
        }
    )
    return 0


def command_preview(args):
    root = _root(args.root)
    if args.format == "pptx":
        from . import render_pptx as renderer
    else:
        from . import render_pdf as renderer
    preview = renderer.render_preview(root, args.content, args.style)
    comparison = preview.get("reference_comparison") or {}
    _emit(
        {
            "ok": True,
            "format": args.format,
            "style": args.style,
            "preview": preview,
            "reference_design": preview.get("reference_design"),
            "reference_previews": preview.get("reference_previews"),
            "reference_comparison": comparison,
            "design_support": comparison.get("design_support")
            or ((preview.get("reference_design") or {}).get("support")),
            "reference_differences": comparison.get("differences") or [],
            "acceptance": "not recorded; show the artifact and ask the user before accept",
            "next_step": (
                "Open the new preview, the actual reference previews, and every comparison image. "
                "Do not infer visual fidelity from colour metrics alone; ask the user to inspect both "
                "before separate preview acceptance."
            ),
        }
    )
    return 0


def command_accept(args):
    root = _root(args.root)
    if not args.user_accepted:
        raise CliError(
            "Pass --user-accepted only after the user actually accepted the shown preview",
            "acceptance_not_confirmed",
        )
    from . import authoring

    content_path = _content_path(root, args.content)
    content = read_json(content_path)
    preview_path = args.preview
    recorded = authoring.record_preview(
        root, content, args.style, preview_path=preview_path, source="cli"
    )
    accepted = authoring.accept_preview(
        root,
        content,
        args.style,
        confirmed_by=args.confirmed_by,
        note=args.note,
    )
    _emit(
        {
            "ok": True,
            "content_file": str(content_path),
            "preview": recorded,
            "accepted": accepted,
        }
    )
    return 0


def command_render(args):
    root = _root(args.root)
    formats = ["pdf", "pptx"] if args.format == "both" else [args.format]
    reports = {}
    failures = []
    for output_format in formats:
        try:
            if output_format == "pdf":
                from . import render_pdf as renderer
            else:
                from . import render_pptx as renderer
            reports[output_format] = renderer.render_final(root, args.content, args.style)
        except Exception as error:
            failures.append({"format": output_format, "error": str(error)})
    _emit(
        {
            "ok": not failures,
            "formats": formats,
            "rendered": reports,
            "failures": failures,
            "note": "Final rendering runs readiness and accepted-preview gates again.",
        }
    )
    return 0 if not failures else 4


def _output_paths(root, args):
    values = list(args.output or [])
    if args.outputs:
        values.extend(item for item in args.outputs.split(",") if item.strip())
    if not values:
        raise CliError("Supply at least one --output path", "outputs_required")
    paths = []
    for value in values:
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        paths.append(str(path))
    return paths


def command_verify(args):
    root = _root(args.root)
    from . import verification

    outputs = _output_paths(root, args)
    content = args.content
    report = verification.inspect_outputs(
        root,
        outputs,
        content_path=content,
        render=not args.no_render,
        write_report=True,
    )
    _emit({"ok": bool(report["summary"]["ok"]), "report": report})
    return 0 if report["summary"]["ok"] else 4


def command_deliver(args):
    root = _root(args.root)
    outputs = _output_paths(root, args)
    from . import verification

    verification_report = verification.inspect_outputs(
        root,
        outputs,
        content_path=args.content,
        render=not args.no_render,
        write_report=True,
    )
    if not verification_report["summary"]["ok"]:
        raise CliError(
            "Verification did not pass; delivery was not recorded. Inspect the report.",
            "verification_failed",
            exit_code=4,
        )
    job = jobs.mark_delivered(root, outputs)
    jobs.set_stage(root, "delivery", "delivered", outputs=sorted(job["outputs"]))
    plan = None
    if args.offer_cleanup:
        try:
            plan = jobs.request_cleanup(root)
        except ValueError as error:
            plan = {"state": "nothing_to_delete", "note": str(error)}
    _emit(
        {
            "ok": True,
            "delivered": True,
            "outputs": job["outputs"],
            "verification": verification_report["summary"],
            "cleanup_offer": plan,
            "next": "ask the user whether to keep work files or delete generated work",
        }
    )
    return 0


def command_keep(args):
    root = _root(args.root)
    _emit({"ok": True, "cleanup": jobs.keep_work(root)})
    return 0


# ---------------------------------------------------------------------------
# Image intake and the real-search ledger
#
# The backend (scripts/lecture_materials/image_policy.py, owned by the image
# lane) holds the actual intake/budget state machine.  These commands only
# marshal durable JSON and conventional flags.  The host agent performs the
# real external image searches around the ledger; no search API lives here.
# ---------------------------------------------------------------------------


def command_image_intake(args):
    root = _root(args.root)
    module = _require_image_policy("image-intake")
    job = jobs.load_job(root)
    base = job.get("preferences", {})
    overlay = _load_preferences(args)
    if args.image_language and "image_language" in overlay:
        raise CliError(
            "--image-language conflicts with an image_language key already supplied in "
            "the preferences input",
            "invalid_answer",
        )
    answers = _parse_answers(args.answer)
    if args.image_language:
        answers["image_language"] = args.image_language
    working = dict(base)
    working.update(overlay)
    working.update(answers)
    missing, questions, intake_payload = _image_intake_state(
        working, strict=True, baseline=base
    )
    recorded = intake_payload.get("recorded") or {}
    asset_id = args.asset_id
    asset_file = args.asset_file
    asset_json = args.asset_json

    asset_review = None
    if args.asset_id or args.asset_file or args.asset_json:
        asset = _load_image_asset_record(root, args)
        try:
            asset_review = module.validate_asset_language(working, asset)
        except (ValueError, TypeError) as error:
            raise CliError(str(error), "asset_language_invalid") from error

    effective = dict(base)
    effective.update(recorded)
    if args.apply:
        job["preferences"] = effective
        jobs.save_job(root, job)
    _emit(
        {
            "ok": True,
            "job_root": str(root),
            "missing_preferences": missing,
            "image_intake": {**intake_payload, "questions": questions},
            "answered": sorted(answers) or None,
            "recorded": recorded or None,
            "asset_language": asset_review,
            "asset_validated": bool(asset_id or asset_file or asset_json),
            "preferences_updated": bool(args.apply),
            "preferences": effective,
            "next": (
                "Answer every listed question with the user's real choice, rerun with "
                "--apply to record it, and prepare again; unanswered mixed-language "
                "image choices block long ingest."
            ),
        }
    )
    return 0


def _load_image_asset_record(root, args):
    if args.asset_file and args.asset_json:
        raise CliError(
            "Use either --asset-file or --asset-json, not both", "invalid_asset_record"
        )
    if args.asset_id and (args.asset_file or args.asset_json):
        raise CliError(
            "Use either --asset-id or an asset record file, not both", "invalid_asset_record"
        )
    if args.asset_id:
        records_path = root / "work" / "assets.json"
        if not records_path.is_file():
            raise CliError(
                f"No asset records found at {records_path}", "asset_records_not_found"
            )
        records = read_json(records_path)
        if not isinstance(records, list):
            raise CliError(
                "work/assets.json must contain a JSON array of records", "invalid_asset_record"
            )
        for record in records:
            if isinstance(record, dict) and record.get("id") == args.asset_id:
                return record
        raise CliError(f"Unknown image asset ID: {args.asset_id}", "unknown_asset_record")
    if not (args.asset_file or args.asset_json):
        raise CliError(
            "Asset-language validation needs --asset-id, --asset-file, or --asset-json",
            "asset_record_required",
        )
    record = _require_object(
        _json_input(args.asset_file, args.asset_json, "asset-file", "asset-json", "asset record"),
        "asset record",
    )
    if record is None:
        raise CliError(
            "The asset record JSON must contain one object", "invalid_asset_record"
        )
    return record


def command_image_search_start(args):
    root = _root(args.root)
    module = _require_image_policy("image-search-start")
    payload = _require_object(
        _json_input(args.request, args.request_json, "request", "request-json", "search request"),
        "search request",
    )
    if payload is None and not (args.request_id or args.query or args.section):
        raise CliError(
            "Provide a search request via --request/--request-json, or --request-id with "
            "--section and --query",
            "search_request_required",
        )
    request = dict(payload or {})
    if args.request_id:
        request["request_id"] = args.request_id
    if args.section:
        request["section_id"] = args.section
    if args.query:
        request["query"] = args.query
    if args.budget is not None:
        if args.budget <= 0:
            raise CliError("--budget must be a positive number of seconds", "invalid_budget")
        request["budget"] = args.budget
    try:
        created = module.start_search(root, request)
    except (ValueError, TypeError) as error:
        raise CliError(str(error), "search_request_invalid") from error
    _emit(
        {
            "ok": True,
            "request": created,
            "state": str(_search_state_path(root)),
            "next": (
                "Run the actual external image search with your own host tools. Check the "
                "state with image-search-status before and after every search; never "
                "launch a search after the recorded budget is exhausted without a "
                "recorded user decision."
            ),
        }
    )
    return 0


def command_image_search_attempt(args):
    root = _root(args.root)
    module = _require_image_policy("image-search-attempt")
    payload = _require_object(
        _json_input(args.update, args.update_json, "update", "update-json", "search attempt"),
        "search attempt",
    )
    flag_candidates = []
    if args.candidate:
        flag_candidates.extend(value.strip() for value in args.candidate if value.strip())
    if args.candidates:
        flag_candidates.extend(part.strip() for part in args.candidates.split(",") if part.strip())
    if payload is None and not (args.request_id or args.outcome or args.query or flag_candidates):
        raise CliError(
            "Provide a search attempt via --update/--update-json, or --request-id with "
            "--query and --outcome",
            "search_attempt_required",
        )
    update = dict(payload or {})
    recorded_ids = update.get("candidate_ids") or []
    if not isinstance(recorded_ids, list):
        raise CliError("candidate_ids must be a JSON list", "invalid_search_attempt")
    candidate_ids = []
    for value in [*recorded_ids, *flag_candidates]:
        identifier = value if not isinstance(value, str) else value.strip()
        if not identifier or identifier in candidate_ids:
            continue
        candidate_ids.append(identifier)
    if recorded_ids or flag_candidates:
        update["candidate_ids"] = candidate_ids
    if args.request_id:
        update["request_id"] = args.request_id
    if args.query:
        update["query"] = args.query
    if args.outcome:
        update["outcome"] = args.outcome
    if args.elapsed_seconds is not None:
        update["elapsed_seconds"] = args.elapsed_seconds
    if args.note:
        update["note"] = args.note
    try:
        recorded = module.record_search_attempt(root, update)
    except (ValueError, TypeError) as error:
        raise CliError(str(error), "search_attempt_invalid") from error
    _emit(
        {
            "ok": True,
            "request": recorded,
            "state": str(_search_state_path(root)),
            "note": (
                "A recorded attempt is not approval: candidate relevance, actual label "
                "language (language_review), and per-section placement still require "
                "real inspection."
            ),
        }
    )
    return 0


def command_image_search_status(args):
    root = _root(args.root)
    module = _require_image_policy("image-search-status")
    try:
        result = module.search_status(root, request_id=args.request_id)
    except KeyError as error:
        raise CliError(f"Unknown or absent image-search request: {error}", "search_request_not_found") from error
    except ValueError as error:
        message = str(error)
        code = (
            "search_request_not_found"
            if args.request_id and "Unknown image-search request" in message
            else "search_status_invalid"
        )
        raise CliError(message, code) from error
    except TypeError as error:
        raise CliError(str(error), "search_status_invalid") from error
    note = (
        "searching: the host may still run real searches within the remaining budget. "
        "awaiting_decision: ask the recorded question in ordinary language (the ledger's "
        "default phrasing is Arabic), record an explicit user decision with "
        "image-search-resolve, and never launch another search before that decision."
    )
    if isinstance(result, list):
        _emit({"ok": True, "requests": result, "state": str(_search_state_path(root)), "note": note})
    else:
        _emit({"ok": True, "request": result, "state": str(_search_state_path(root)), "note": note})
    return 0


def command_image_search_resolve(args):
    root = _root(args.root)
    module = _require_image_policy("image-search-resolve")
    payload = _require_object(
        _json_input(args.decision, args.decision_json, "decision", "decision-json", "search decision"),
        "search decision",
    )
    if payload is None and not (args.request_id or args.action):
        raise CliError(
            "Provide a decision via --decision/--decision-json, or --request-id with --action",
            "search_decision_required",
        )
    decision = dict(payload or {})
    if args.request_id:
        decision["request_id"] = args.request_id
    if args.action:
        decision["action"] = args.action
    if args.note:
        decision["note"] = args.note
    if args.user_confirmed:
        if decision.get("user_confirmed") is False:
            raise CliError(
                "--user-confirmed conflicts with user_confirmed=false in the decision JSON",
                "invalid_search_decision",
            )
        decision["user_confirmed"] = True
    if decision.get("user_confirmed") is not True:
        raise CliError(
            "A decision requires explicit user confirmation: pass --user-confirmed only "
            "after the user actually chose, or user_confirmed: true in the decision JSON. "
            "An elapsed timeout is not an answer.",
            "decision_not_confirmed",
        )
    try:
        resolved = module.resolve_search(root, decision)
    except (ValueError, TypeError) as error:
        raise CliError(str(error), "search_decision_invalid") from error
    _emit(
        {
            "ok": True,
            "request": resolved,
            "state": str(_search_state_path(root)),
            "note": (
                "The ledger itself deletes and regenerates nothing. A no_images decision "
                "means picture blocks must be removed/regenerated and a fresh preview "
                "shown and accepted before any further rendering; a language switch "
                "changes the image_language preference and requires fresh validation."
            ),
        }
    )
    return 0


def command_cleanup_request(args):
    root = _root(args.root)
    plan = jobs.request_cleanup(root)
    _emit(
        {
            "ok": True,
            **plan,
            "next": (
                "This first choice deleted nothing. Show the exact delete list and ask a second explicit "
                "confirmation, then pass the token to cleanup-confirm."
            ),
        }
    )
    return 0


def command_cleanup_confirm(args):
    root = _root(args.root)
    report = jobs.confirm_cleanup(root, args.token)
    ok = report.get("state") == "cleaned"
    _emit({"ok": ok, **report})
    return 0 if ok else 5


def command_attach(args):
    root = _root(args.root)
    items = _add_attachment_roles(root, args.path, args.role)
    _emit(
        {
            "ok": True,
            "attachment": items[0] if len(items) == 1 else None,
            "attachments": items,
            "roles": _roles_for_path(root, args.path),
        }
    )
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="lecture.py",
        description="Lecture-to-materials source processing, job state, evidence, and output orchestration.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capabilities = subparsers.add_parser(
        "capabilities", aliases=["doctor"], help="Report real local dependencies and capabilities"
    )
    capabilities.add_argument("--model-dir")
    capabilities.add_argument("--quick", action="store_true", help="Skip the live Chromium probe")
    capabilities.add_argument("--strict", action="store_true", help="Nonzero when core dependencies are missing")
    capabilities.set_defaults(func=command_capabilities)

    init = subparsers.add_parser("init", aliases=["new"], help="Create a job from a local video or one YouTube URL")
    init.add_argument("--root", required=True)
    init.add_argument("--source", required=True)
    init.add_argument("--preferences")
    init.add_argument("--preferences-json")
    init.add_argument("--attachment", action="append", default=[], metavar="ROLE=PATH")
    init.add_argument("--allow-partial", action="store_true")
    init.set_defaults(func=command_init)

    status = subparsers.add_parser("status", help="Show durable job state")
    status.add_argument("--root", required=True)
    status.set_defaults(func=command_status)

    output_intake = subparsers.add_parser("output-intake", help="Ask or record source-time visibility")
    output_intake.add_argument("--root", required=True)
    output_intake.add_argument("--show-timestamps", choices=("yes", "no"))
    output_intake.add_argument("--apply", action="store_true")
    output_intake.set_defaults(func=command_output_intake)

    prepare = subparsers.add_parser("prepare", aliases=["source"], help="Probe/download the source and validate the interval")
    prepare.add_argument("--root", required=True)
    prepare.add_argument("--accept-source-change", action="store_true")
    prepare.add_argument("--allow-partial", action="store_true")
    prepare.set_defaults(func=command_prepare)

    captions = subparsers.add_parser("captions", help="Import VTT/SRT captions")
    captions.add_argument("--root", required=True)
    captions.add_argument("--caption", required=True)
    captions.add_argument("--language")
    captions.add_argument("--provenance", choices=("manual", "automatic", "translated"))
    captions.set_defaults(func=command_captions)

    transcribe = subparsers.add_parser("transcribe", aliases=["asr"], help="Run real local faster-whisper ASR")
    transcribe.add_argument("--root", required=True)
    transcribe.add_argument("--model-dir")
    transcribe.add_argument("--chunk-seconds", type=float, default=DEFAULT_CHUNK_SECONDS)
    transcribe.add_argument("--overlap-seconds", type=float, default=DEFAULT_OVERLAP_SECONDS)
    transcribe.add_argument("--language")
    transcribe.add_argument("--beam-size", type=int, default=5)
    transcribe.add_argument("--vad", action="store_true")
    transcribe.add_argument("--device", default="cpu")
    transcribe.add_argument("--compute-type", default="int8")
    transcribe.add_argument("--force", action="store_true")
    transcribe.add_argument("--reset", action="store_true")
    transcribe.add_argument("--allow-partial", action="store_true")
    transcribe.set_defaults(func=command_transcribe)

    mark_speech = subparsers.add_parser("mark-speech", help="Record a host-reviewed no-audio/no-speech outcome")
    mark_speech.add_argument("--root", required=True)
    mark_speech.add_argument("--status", required=True, choices=sorted(NO_SPEECH_STATUSES))
    mark_speech.add_argument("--reason", required=True)
    mark_speech.add_argument("--evidence")
    mark_speech.set_defaults(func=command_mark_speech)

    frames = subparsers.add_parser(
        "frames",
        aliases=["screens"],
        help="Extract/resume frame candidates (default 15s interval, minimum 5s)",
    )
    frames.add_argument("--root", required=True)
    frames.add_argument("--limit", type=int, default=0)
    frames.add_argument("--interval", type=float)
    frames.add_argument("--reset", action="store_true")
    frames.add_argument("--allow-partial", action="store_true")
    frames.set_defaults(func=command_frames)

    refine_frames = subparsers.add_parser(
        "refine-frames",
        help="Targeted direct-video frame recovery for a single adjacent baseline window (<= 15s)",
    )
    refine_frames.add_argument("--root", required=True)
    refine_frames.add_argument("--window-start", type=float, required=True)
    refine_frames.add_argument("--window-end", type=float, required=True)
    refine_frames.add_argument("--interval", type=float, required=True, choices=(10.0, 5.0))
    refine_frames.add_argument("--reason", required=True)
    refine_frames.set_defaults(func=command_refine_frames)

    reset_frames = subparsers.add_parser("reset-frames", help="Discard frame evidence after an explicit settings change")
    reset_frames.add_argument("--root", required=True)
    reset_frames.set_defaults(func=command_reset_frames)

    reset_speech = subparsers.add_parser("reset-speech", help="Discard ASR checkpoints after an explicit settings change")
    reset_speech.add_argument("--root", required=True)
    reset_speech.set_defaults(func=command_reset_speech)

    audio = subparsers.add_parser("audio", help="Cut the selected interval to mono 16 kHz WAV")
    audio.add_argument("--root", required=True)
    audio.add_argument("--start", type=float)
    audio.add_argument("--end", type=float)
    audio.add_argument("--force", action="store_true")
    audio.set_defaults(func=command_audio)

    references = subparsers.add_parser("references", aliases=["reference"], help="Import an original PDF/PPTX/image as evidence")
    references.add_argument("--root", required=True)
    references.add_argument("--reference", required=True)
    references.add_argument("--role", choices=("content", "design", "both"))
    references.add_argument("--replace", action="store_true")
    references.set_defaults(func=command_references)

    design_extract = subparsers.add_parser(
        "design-extract",
        aliases=["style-extract", "extract-design"],
        help="Extract a reference PDF/PPTX/image design into job work",
    )
    design_extract.add_argument("--root", required=True)
    design_extract.add_argument("--reference", required=True)
    design_extract.add_argument("--role", choices=("design", "both"))
    design_extract.add_argument("--destination")
    design_extract.add_argument("--overwrite", action="store_true")
    design_extract.set_defaults(func=command_design_extract)

    design_check = subparsers.add_parser(
        "design-check",
        aliases=["style-check", "design-verify"],
        help="Inspect design support and verify frozen/source identity",
    )
    design_check.add_argument("--root")
    design_check.add_argument("--style", required=True)
    design_check.set_defaults(func=command_design_check)

    style_list = subparsers.add_parser(
        "style-list",
        aliases=["library-list", "design-list"],
        help="List built-in and saved visual styles and teaching profiles",
    )
    style_list.add_argument("--library")
    style_list.add_argument(
        "--kind",
        choices=("all", "visual_style", "teaching_profile"),
        default="all",
    )
    style_list.set_defaults(func=command_style_list)

    style_load = subparsers.add_parser(
        "style-load",
        aliases=["library-load", "design-load"],
        help="Load a saved visual style or teaching profile",
    )
    style_load.add_argument("--library")
    style_load.add_argument(
        "--kind", required=True, choices=("visual_style", "teaching_profile")
    )
    style_load.add_argument("--name", required=True)
    style_load.add_argument("--version")
    style_load.set_defaults(func=command_style_load)

    style_save = subparsers.add_parser(
        "style-save",
        aliases=["library-save", "design-save"],
        help="Save a style/profile to the shared library with explicit consent",
    )
    style_save.add_argument("--library")
    style_save.add_argument("--root")
    style_save.add_argument(
        "--kind", required=True, choices=("visual_style", "teaching_profile")
    )
    style_save.add_argument("--name", required=True)
    style_save.add_argument("--user-agreed", "--user-approved", "--consent", dest="user_agreed", action="store_true")
    style_save.add_argument("--style-path", "--style", dest="style_path")
    style_save.add_argument("--profile", "--profile-path", dest="profile")
    style_save.add_argument("--profile-json")
    style_save.add_argument("--collision", choices=("new_version", "rename"))
    style_save.add_argument("--new-name")
    style_save.add_argument("--version")
    style_save.set_defaults(func=command_style_save)

    packet = subparsers.add_parser("review-packet", aliases=["packet"], help="Emit a bounded screen/reference review packet")
    packet.add_argument("--root", required=True)
    packet.add_argument("--kind", choices=("screen", "reference"), default="screen")
    packet.add_argument("--offset", type=int, default=0)
    packet.add_argument("--limit", type=int, default=10)
    packet.add_argument("--out")
    packet.set_defaults(func=command_review_packet)

    merge = subparsers.add_parser("merge-review", aliases=["merge"], help="Merge validated host review records")
    merge.add_argument("--root", required=True)
    merge.add_argument("--review", required=True)
    merge.add_argument("--kind", choices=("screen", "reference"), default="screen")
    merge.add_argument("--force", action="store_true")
    merge.set_defaults(func=command_merge_review)

    validate = subparsers.add_parser("validate", help="Validate authored content structure and coverage")
    validate.add_argument("--root", required=True)
    validate.add_argument("--content", required=True)
    validate.set_defaults(func=command_validate)

    readiness = subparsers.add_parser("readiness", help="Fail closed on incomplete speech/screen/reference review")
    readiness.add_argument("--root", required=True)
    readiness.add_argument("--content")
    readiness.set_defaults(func=command_readiness)

    preview = subparsers.add_parser("preview", help="Render one representative preview artifact")
    preview.add_argument("--root", required=True)
    preview.add_argument("--content", required=True)
    preview.add_argument("--style", required=True)
    preview.add_argument("--format", choices=("pdf", "pptx"), default="pdf")
    preview.set_defaults(func=command_preview)

    accept = subparsers.add_parser("accept", aliases=["approve"], help="Record explicit user acceptance of the shown preview")
    accept.add_argument("--root", required=True)
    accept.add_argument("--content", required=True)
    accept.add_argument("--style", required=True)
    accept.add_argument("--preview")
    accept.add_argument("--confirmed-by", default="host-confirmed user acceptance")
    accept.add_argument("--note")
    accept.add_argument("--user-accepted", action="store_true")
    accept.set_defaults(func=command_accept)

    render = subparsers.add_parser("render", help="Render final PDF/PPTX after all gates pass")
    render.add_argument("--root", required=True)
    render.add_argument("--content", required=True)
    render.add_argument("--style", required=True)
    render.add_argument("--format", choices=("pdf", "pptx", "both"), default="pdf")
    render.set_defaults(func=command_render)

    verify = subparsers.add_parser("verify", help="Read final outputs back and inspect them")
    verify.add_argument("--root", required=True)
    verify.add_argument("--output", action="append", default=[])
    verify.add_argument("--outputs")
    verify.add_argument("--content")
    verify.add_argument("--no-render", action="store_true")
    verify.set_defaults(func=command_verify)

    deliver = subparsers.add_parser("deliver", help="Verify and record final delivery")
    deliver.add_argument("--root", required=True)
    deliver.add_argument("--output", action="append", default=[])
    deliver.add_argument("--outputs")
    deliver.add_argument("--content")
    deliver.add_argument("--no-render", action="store_true")
    deliver.add_argument("--offer-cleanup", action="store_true")
    deliver.set_defaults(func=command_deliver)

    keep = subparsers.add_parser("keep", help="Keep generated work and cancel pending deletion")
    keep.add_argument("--root", required=True)
    keep.set_defaults(func=command_keep)

    cleanup_request = subparsers.add_parser("cleanup-request", aliases=["cleanup-plan"], help="Non-destructive first cleanup step")
    cleanup_request.add_argument("--root", required=True)
    cleanup_request.set_defaults(func=command_cleanup_request)

    cleanup_confirm = subparsers.add_parser("cleanup-confirm", aliases=["cleanup-apply"], help="Delete only the reconfirmed targets")
    cleanup_confirm.add_argument("--root", required=True)
    cleanup_confirm.add_argument("--token", required=True)
    cleanup_confirm.set_defaults(func=command_cleanup_confirm)

    attach = subparsers.add_parser("attach", help="Register an original attachment and role")
    attach.add_argument("--root", required=True)
    attach.add_argument("--path", required=True)
    attach.add_argument("--role", required=True)
    attach.set_defaults(func=command_attach)

    image_intake = subparsers.add_parser(
        "image-intake",
        help="Run the shared image preference intake and asset-language checks",
    )
    image_intake.add_argument("--root", required=True)
    image_intake.add_argument("--preferences")
    image_intake.add_argument("--preferences-json")
    image_intake.add_argument(
        "--image-language",
        choices=IMAGE_LANGUAGE_CHOICES,
        help="Record the answer to the image-label-language choice (ar, en, or mixed)",
    )
    image_intake.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Record one intake answer as KEY=VALUE (VALUE is parsed as JSON when valid)",
    )
    image_intake.add_argument(
        "--asset-id",
        help="Validate one work/assets.json record's actual language review",
    )
    image_intake.add_argument(
        "--asset-file",
        "--asset",
        dest="asset_file",
        default=None,
        metavar="ASSET_RECORD_JSON",
        help="Validate one asset record supplied as a JSON file path",
    )
    image_intake.add_argument(
        "--asset-json",
        dest="asset_json",
        default=None,
        metavar="JSON",
        help="Validate one asset record supplied as inline JSON",
    )
    image_intake.add_argument(
        "--apply",
        action="store_true",
        help="Persist the normalized preferences into job.json",
    )
    image_intake.set_defaults(func=command_image_intake)

    image_search_start = subparsers.add_parser(
        "image-search-start",
        help="Create or explicitly resume a durable image-search request ledger",
    )
    image_search_start.add_argument("--root", required=True)
    image_search_start.add_argument("--request")
    image_search_start.add_argument("--request-json")
    image_search_start.add_argument("--request-id")
    image_search_start.add_argument("--section", help="Section the image must serve (section_id)")
    image_search_start.add_argument("--query")
    image_search_start.add_argument(
        "--budget",
        type=int,
        default=None,
        help="Active-search budget in seconds for this request (default 600)",
    )
    image_search_start.set_defaults(func=command_image_search_start)

    image_search_attempt = subparsers.add_parser(
        "image-search-attempt",
        help="Record one real image-search attempt/outcome and accrue active search time",
    )
    image_search_attempt.add_argument("--root", required=True)
    image_search_attempt.add_argument("--update")
    image_search_attempt.add_argument("--update-json")
    image_search_attempt.add_argument("--request-id")
    image_search_attempt.add_argument("--query")
    image_search_attempt.add_argument(
        "--candidate",
        action="append",
        default=[],
        metavar="CANDIDATE_ID",
        help="One actual candidate ID returned by a real search",
    )
    image_search_attempt.add_argument(
        "--candidates",
        metavar="CANDIDATE_IDS",
        help="Comma-separated convenience list of candidate IDs (no commas inside IDs)",
    )
    image_search_attempt.add_argument(
        "--outcome", help="Real outcome of the attempt, for example found or none"
    )
    image_search_attempt.add_argument(
        "--elapsed-seconds",
        type=float,
        default=None,
        help=("Optional host-measured active search seconds for this attempt; "
              "otherwise the ledger's active clock interval is used"),
    )
    image_search_attempt.add_argument("--note", help="Concrete observation about the search")
    image_search_attempt.set_defaults(func=command_image_search_attempt)

    image_search_status = subparsers.add_parser(
        "image-search-status",
        help="Show durable image-search request state and any required user question",
    )
    image_search_status.add_argument("--root", required=True)
    image_search_status.add_argument("--request-id")
    image_search_status.set_defaults(func=command_image_search_status)

    image_search_resolve = subparsers.add_parser(
        "image-search-resolve",
        help="Record an explicit, user-confirmed decision for a paused search request",
    )
    image_search_resolve.add_argument("--root", required=True)
    image_search_resolve.add_argument("--decision")
    image_search_resolve.add_argument("--decision-json")
    image_search_resolve.add_argument("--request-id")
    image_search_resolve.add_argument(
        "--action",
        choices=SEARCH_ACTIONS,
        help="continue_search, use_arabic, use_english, or no_images",
    )
    image_search_resolve.add_argument("--note", help="Why the user chose this action")
    image_search_resolve.add_argument(
        "--user-confirmed",
        action="store_true",
        help="Pass only after the user actually chose an action",
    )
    image_search_resolve.set_defaults(func=command_image_search_resolve)

    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="strict")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except SystemExit:
        raise
    except Exception as error:
        return _error(error)


if __name__ == "__main__":
    raise SystemExit(main())
