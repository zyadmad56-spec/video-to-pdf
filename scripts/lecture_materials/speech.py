"""Caption import and real local speech transcription.

No canned transcript or network model download is used.  A configured local
faster-whisper model is validated before inference, audio is cut into bounded
chunks, and every completed chunk is checkpointed atomically so an interrupted
long lecture resumes without repeating finished work.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone

from . import media as media_module
from .job_locks import speech_lock
from .jobs import (
    SourceChangedError,
    file_digest,
    json_digest,
    load_job,
    read_json,
    register_generated,
    save_job,
    set_stage,
    unregister_generated,
    write_json,
)
from .timing import parse_captions, timestamp


SCHEMA_VERSION = 1
DEFAULT_CHUNK_SECONDS = 300.0
DEFAULT_OVERLAP_SECONDS = 5.0
MODEL_FILES = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")
REVIEWED_NO_SPEECH = {"no_audio", "no_speech_reviewed", "not_applicable", "no_speech"}


class SpeechUnavailableError(ValueError):
    """A real local model or dependency is unavailable; nothing was fabricated."""

    code = "speech_unavailable"


class NoAudioError(ValueError):
    code = "no_audio"


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _caption_metadata(path):
    name = Path(path).name
    match = re.search(r"\.([a-z]{2,3}(?:-[A-Za-z0-9]+)?)(?:\.(auto|orig|translated))?\.(?:vtt|srt)$", name, re.I)
    language = match.group(1).lower() if match else None
    flag = match.group(2).lower() if match and match.group(2) else None
    if language:
        language = re.sub(r"-(?:orig|auto)$", "", language)
    if language and language.endswith("-orig"):
        language = language[:-5]
    provenance = "automatic" if flag in {"auto", "orig"} else "translated" if flag == "translated" else "manual"
    if "auto" in name.lower():
        provenance = "automatic"
    if "translated" in name.lower():
        provenance = "translated"
    return language, provenance


def _resolve_range(job):
    media = job.get("media") if isinstance(job.get("media"), dict) else {}
    preferences = job.get("preferences", {})
    start = media.get("start", preferences.get("start", 0))
    end = media.get("end", preferences.get("end"))
    try:
        start = float(start)
    except (TypeError, ValueError) as error:
        raise ValueError("Selected start must be a finite number") from error
    if not math.isfinite(start) or start < 0:
        raise ValueError("Selected start must be finite and nonnegative")
    if end is not None:
        try:
            end = float(end)
        except (TypeError, ValueError) as error:
            raise ValueError("Selected end must be a finite number") from error
        if not math.isfinite(end) or end <= start:
            raise ValueError("Require 0 <= start < end")
    return start, end


def _normalize_records(segments, language, provenance, range_start, range_end):
    records = []
    for cue_index, segment in enumerate(segments, start=1):
        start = float(segment["start"])
        end = float(segment["end"])
        if end <= start:
            continue
        if range_end is not None and start >= range_end:
            continue
        if end <= range_start:
            continue
        original = {"start": start, "end": end}
        start = max(start, range_start)
        if range_end is not None:
            end = min(end, range_end)
        if end <= start:
            continue
        records.append(
            {
                "id": f"audio-{len(records) + 1:06}",
                "kind": "spoken",
                "start": start,
                "end": end,
                "text": segment["text"],
                "raw_text": segment.get("raw_text", segment["text"]),
                "source_language": language or "und",
                "language": language or "und",
                "provenance": {
                    "provider": "captions",
                    "track": provenance,
                    "cue_index": cue_index,
                },
                "provider": "captions",
                "review_status": "unreviewed",
                "source_interval": original,
            }
        )
    return records


def _write_transcript(root, records, source_summary):
    root = Path(root)
    transcript_path = root / "work" / "transcript.json"
    write_json(transcript_path, records)
    register_generated(root, transcript_path)
    lines = [
        "# Original timestamped transcript",
        f"# Source: {source_summary}",
        "# Times are absolute in the original media timeline.",
        "",
    ]
    for record in records:
        lines.append(
            f"[{timestamp(record['start'])} --> {timestamp(record['end'])}] {record['text']}"
        )
    text_path = root / "work" / "transcript.txt"
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    register_generated(root, text_path)
    return transcript_path, text_path


def import_captions(root, caption_path, language=None, provenance=None):
    """Import one VTT/SRT sidecar, filtered to the selected interval."""

    root = Path(root).resolve()
    job = load_job(root)
    caption_path = Path(caption_path)
    if not caption_path.is_file():
        raise ValueError(f"Caption file not found: {caption_path}")
    caption_path = caption_path.resolve()
    try:
        caption_text = caption_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raw = caption_path.read_bytes()
        for encoding in ("utf-16", "cp1256", "cp1252"):
            try:
                caption_text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError(
                f"Caption file is not decodable as UTF-8/UTF-16/Arabic-Windows text: {caption_path}"
            )
    detected_language, detected_provenance = _caption_metadata(caption_path)
    language = language or detected_language
    provenance = provenance or detected_provenance
    media = job.get("media") if isinstance(job.get("media"), dict) else {}
    source_path = media.get("path")
    expected_digest = media.get("sha256")
    if source_path and expected_digest:
        candidate = Path(source_path)
        if not candidate.is_file():
            raise SourceChangedError(
                "Prepared source file is missing; captions cannot be bound to it. "
                "Restore the file or prepare the source again."
            )
        if file_digest(candidate) != expected_digest:
            raise SourceChangedError(
                "Source video changed since preparation; captions will not be bound to stale media. "
                "Prepare the source again with an explicit source-change reset."
            )
    relative = None
    try:
        relative = caption_path.relative_to(root).as_posix()
    except ValueError:
        relative = str(caption_path)
    start, end = _resolve_range(job)
    segments = parse_captions(caption_text)
    records = _normalize_records(segments, language, provenance, start, end)
    if not records:
        raise ValueError(
            "Caption file contains no usable speech in the selected interval; "
            "import a different language track or transcribe the audio locally"
        )
    digest = file_digest(caption_path)
    transcript_path, text_path = _write_transcript(
        root, records, f"{caption_path.name} ({provenance}, {language or 'und'})"
    )
    meta = {
        "schema_version": SCHEMA_VERSION,
        "source": relative,
        "source_absolute": str(caption_path),
        "sha256": digest,
        "language": language or "und",
        "provenance": provenance,
        "source_sha256": expected_digest,
        "interval": {"start": start, "end": end},
        "records": len(records),
        "imported_at": _utc(),
    }
    meta_path = root / "work" / "captions-meta.json"
    write_json(meta_path, meta)
    register_generated(root, meta_path)
    job = load_job(root)
    job.setdefault("stages", {})["speech"] = {
        "status": "transcribed",
        "source": "captions",
        "language": language or "und",
        "provenance": provenance,
        "records": len(records),
        "updated_at": _utc(),
    }
    job["cleanup"] = None
    save_job(root, job)
    return {
        "status": "transcribed",
        "source": "captions",
        "language": language or "und",
        "provenance": provenance,
        "records": len(records),
        "transcript": str(transcript_path),
        "transcript_text": str(text_path),
        "interval": {"start": start, "end": end},
        "note": "Original cue text and absolute times were preserved; no translation was performed.",
    }


def _explicit_model_value(root, settings):
    if settings.get("model_dir"):
        return str(settings["model_dir"])
    try:
        preferences = load_job(root).get("preferences", {})
    except (FileNotFoundError, ValueError):
        preferences = {}
    if preferences.get("asr_model_dir"):
        return str(preferences["asr_model_dir"])
    for variable in ("LECTURE_MATERIALS_WHISPER_MODEL", "WHISPER_MODEL_DIR"):
        value = os.environ.get(variable)
        if value:
            return value
    return None


def _default_model_paths(root):
    project = Path(__file__).resolve().parents[3]
    package_root = Path(__file__).resolve().parents[2]
    return [
        project / ".models" / "whisper-small",
        package_root / ".models" / "whisper-small",
        Path(root) / ".models" / "whisper-small",
    ]


def resolve_model(settings=None):
    """Find and validate a complete local model; never download one.

    An explicitly configured model (CLI/preferences/environment) must be valid.
    It is not silently replaced by a different model found in a default path.
    """

    settings = settings or {}
    root = Path(settings.get("root") or Path.cwd())
    explicit = _explicit_model_value(root, settings)
    if explicit:
        configured = Path(explicit).expanduser()
        candidates = (
            [configured]
            if configured.is_absolute()
            else [Path.cwd() / configured, root / configured]
        )
    else:
        candidates = _default_model_paths(root)
    checked = []
    for candidate in candidates:
        candidate = Path(candidate)
        checked.append(str(candidate))
        if not candidate.is_dir():
            continue
        missing = [name for name in MODEL_FILES if not (candidate / name).is_file()]
        if missing:
            raise SpeechUnavailableError(
                f"Whisper model directory is incomplete ({candidate}); missing: "
                + ", ".join(missing)
                + ". Provide a complete faster-whisper model directory; no download was attempted."
            )
        return candidate
    raise SpeechUnavailableError(
        "No local faster-whisper model found. Checked: "
        + "; ".join(checked)
        + ". Run the documented model setup step; hidden downloads are disabled."
    )


def model_identity(model_dir):
    model_dir = Path(model_dir)
    identity = {"path": str(model_dir.resolve()), "files": {}}
    for name in MODEL_FILES:
        path = model_dir / name
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        identity["files"][name] = {
            "size": path.stat().st_size,
            "sha256": digest.hexdigest(),
        }
    return identity


def _validate_settings(settings):
    chunk = float(settings.get("chunk_seconds", DEFAULT_CHUNK_SECONDS))
    overlap = float(settings.get("overlap_seconds", DEFAULT_OVERLAP_SECONDS))
    beam = int(settings.get("beam_size", 5))
    if not math.isfinite(chunk) or chunk < 30 or chunk > 3600:
        raise ValueError("chunk_seconds must be finite and between 30 and 3600")
    if not math.isfinite(overlap) or overlap < 0 or overlap >= chunk / 2:
        raise ValueError("overlap_seconds must be finite, nonnegative, and less than half a chunk")
    if beam < 1 or beam > 10:
        raise ValueError("beam_size must be between 1 and 10")
    language = settings.get("language")
    if language is not None and not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)?", str(language)):
        raise ValueError("language must be a short language code or null for automatic detection")
    return {
        "chunk_seconds": chunk,
        "overlap_seconds": overlap,
        "beam_size": beam,
        "language": language,
        "vad_filter": bool(settings.get("vad_filter", False)),
        "device": str(settings.get("device", "cpu")),
        "compute_type": str(settings.get("compute_type", "int8")),
        "cpu_threads": int(settings.get("cpu_threads", 0) or 0),
        "condition_on_previous_text": bool(settings.get("condition_on_previous_text", True)),
    }


def _chunk_plan(start, end, chunk_seconds, overlap):
    step = chunk_seconds - overlap
    plan = []
    cursor = start
    index = 0
    while cursor < end - 1e-6:
        chunk_end = min(cursor + chunk_seconds, end)
        plan.append({"index": index, "start": cursor, "end": chunk_end})
        index += 1
        cursor += step
        if chunk_end >= end - 1e-6:
            break
    if not plan:
        plan.append({"index": 0, "start": start, "end": end})
    return plan


def _speech_key(media, model, settings, plan):
    return json_digest(
        {
            "schema": SCHEMA_VERSION,
            "source": media.get("sha256"),
            "start": media.get("start"),
            "end": media.get("end"),
            "model": model,
            "settings": settings,
            "plan": plan,
        }
    )


def _chunk_path(root, index):
    return Path(root) / "work" / "audio" / "asr" / f"chunk-{index + 1:04}.json"


def _chunk_wav(root, index, start=None, end=None):
    suffix = ""
    if start is not None and end is not None:
        suffix = f"-{float(start):.3f}-{float(end):.3f}"
    return Path(root) / "work" / "audio" / "asr" / f"chunk-{index + 1:04}{suffix}.wav"


def _run_state(key, media, identity, options, plan, status, started_at, **extra):
    document = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "key": key,
        "source_sha256": media.get("sha256"),
        "interval": {"start": media.get("start"), "end": media.get("end")},
        "model": identity,
        "settings": options,
        "plan": plan,
        "chunks_total": len(plan),
        "chunks_completed": 0,
        "completed_chunks": [],
        "started_at": started_at,
        "updated_at": _utc(),
    }
    document.update(extra)
    return document


def _persist_run_state(root, state_path, document):
    document["updated_at"] = _utc()
    write_json(state_path, document)
    register_generated(root, state_path)


def reset_transcription(root):
    """Explicitly discard ASR checkpoints and transcript for a settings change."""

    with speech_lock(root):
        return _reset_transcription_unlocked(root)


def _reset_transcription_unlocked(root):

    root = Path(root).resolve()
    job = load_job(root)
    stale = [
        relative
        for relative in job.get("generated", [])
        if relative.startswith(("work/audio/", "work/transcript.json", "work/transcript.txt", "work/speech-state.json"))
    ]
    for relative in stale:
        path = root / relative
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
        unregister_generated(root, path)
    job = load_job(root)
    job.get("stages", {}).pop("speech", None)
    job["preview"] = None
    save_job(root, job)
    return {"reset": True, "removed": len(stale)}


def _uncertainty(segment, text):
    reasons = []
    if getattr(segment, "avg_logprob", 0.0) is not None and segment.avg_logprob < -1.0:
        reasons.append("low average log probability")
    if getattr(segment, "compression_ratio", 0.0) is not None and segment.compression_ratio > 2.4:
        reasons.append("high compression ratio")
    if getattr(segment, "no_speech_prob", 0.0) is not None and segment.no_speech_prob > 0.6:
        reasons.append("high no-speech probability")
    if not text.strip():
        reasons.append("empty text")
    return reasons


def _normalise_speech_text(text):
    return re.sub(r"\s+", " ", str(text).casefold()).strip()


def _merge_segments(chunks):
    merged = []
    for chunk in sorted(chunks, key=lambda item: item["index"]):
        for segment in chunk.get("segments", []):
            text = str(segment.get("text", "")).strip()
            if not text:
                continue
            start = float(segment["start"])
            end = float(segment["end"])
            if end <= start:
                continue
            segment = dict(segment)
            if merged:
                last = merged[-1]
                if start < last["end"] - 0.05:
                    if _normalise_speech_text(text) == _normalise_speech_text(last["text"]):
                        # Exact duplicate from the deliberate chunk overlap.
                        continue
                    # Similar wording is not semantic equivalence.  Keeping both
                    # protects a changed number, identifier, relation, or term.
                    segment.setdefault("overlap_notes", []).append(
                        "Different text in an overlapping ASR chunk; both records were preserved."
                    )
            merged.append(segment)
    for index, segment in enumerate(merged, start=1):
        segment["id"] = f"audio-{index:06}"
    return merged


def transcribe_local(root, settings=None, **overrides):
    """Run real faster-whisper ASR with completed-chunk resume."""

    with speech_lock(root):
        return _transcribe_local_unlocked(root, settings, **overrides)


def _transcribe_local_unlocked(root, settings=None, **overrides):

    root = Path(root).resolve()
    supplied = dict(settings or {})
    supplied.update({key: value for key, value in overrides.items() if value is not None})
    supplied["root"] = str(root)
    job = load_job(root)
    media = media_module.prepare_source(root)
    if not media.get("has_audio"):
        raise NoAudioError(
            "Source has no audio stream. Review the screen-only content and record a "
            "no_audio status explicitly instead of inventing a transcript."
        )
    try:
        model_dir = resolve_model({**supplied, "root": str(root)})
    except SpeechUnavailableError:
        raise
    identity = model_identity(model_dir)
    options = _validate_settings(supplied)
    plan = _chunk_plan(
        float(media["start"]),
        float(media["end"]),
        options["chunk_seconds"],
        options["overlap_seconds"],
    )
    key = _speech_key(media, identity, options, plan)
    state_path = root / "work" / "speech-state.json"
    previous = read_json(state_path) if state_path.exists() else None
    if isinstance(previous, dict) and previous.get("key") not in {None, key}:
        raise ValueError(
            "Speech source, model, or settings changed; old chunk checkpoints will not be reused. "
            "Reset transcription explicitly before running the new configuration."
        )
    force = bool(supplied.get("force", False))
    if force:
        _reset_transcription_unlocked(root)
        previous = None
        plan = _chunk_plan(
            float(media["start"]),
            float(media["end"]),
            options["chunk_seconds"],
            options["overlap_seconds"],
        )
    started = time.monotonic()
    started_at = _utc()
    completed = []
    run_state = _run_state(
        key,
        media,
        identity,
        options,
        plan,
        "in_progress",
        started_at,
    )
    _persist_run_state(root, state_path, run_state)
    set_stage(
        root,
        "speech",
        "in_progress",
        source="local-asr",
        key=key,
        chunks_completed=0,
        chunks_total=len(plan),
        model_path=str(model_dir),
        updated_at=_utc(),
    )
    try:
        from faster_whisper import WhisperModel
    except Exception as error:
        run_state.update(status="failed", error=str(error))
        _persist_run_state(root, state_path, run_state)
        set_stage(
            root,
            "speech",
            "failed",
            source="local-asr",
            error=str(error),
            updated_at=_utc(),
        )
        raise SpeechUnavailableError(
            "faster-whisper is not importable. Install the ASR dependency in the project "
            f"environment and retry: {error}"
        ) from error
    try:
        model = WhisperModel(
            str(model_dir),
            device=options["device"],
            compute_type=options["compute_type"],
            cpu_threads=options["cpu_threads"],
            local_files_only=True,
        )
    except Exception as error:
        run_state.update(status="failed", error=str(error))
        _persist_run_state(root, state_path, run_state)
        set_stage(
            root,
            "speech",
            "failed",
            source="local-asr",
            error=str(error),
            updated_at=_utc(),
        )
        raise SpeechUnavailableError(
            f"Could not load the configured local model at {model_dir}: {error}"
        ) from error
    for item in plan:
        checkpoint = _chunk_path(root, item["index"])
        if checkpoint.is_file() and not force:
            try:
                document = read_json(checkpoint)
            except Exception:
                document = None
            if (
                isinstance(document, dict)
                and document.get("key") == key
                and document.get("index") == item["index"]
                and document.get("start") is not None
                and document.get("end") is not None
                and abs(float(document["start"]) - float(item["start"])) <= 1e-6
                and abs(float(document["end"]) - float(item["end"])) <= 1e-6
            ):
                completed.append(document)
                run_state.update(
                    status="transcribing",
                    chunks_completed=len(completed),
                    completed_chunks=[entry.get("index") for entry in completed],
                )
                _persist_run_state(root, state_path, run_state)
                set_stage(
                    root,
                    "speech",
                    "transcribing",
                    source="local-asr",
                    chunks_completed=len(completed),
                    chunks_total=len(plan),
                    model_path=str(model_dir),
                    updated_at=_utc(),
                )
                continue
        wav = _chunk_wav(root, item["index"], item["start"], item["end"])
        try:
            media_module.extract_audio_chunk(
                root, media, item["start"], item["end"], wav, force=force
            )
            chunk_started = time.monotonic()
            segment_iterator, info = model.transcribe(
                str(wav),
                language=options["language"],
                task="transcribe",
                beam_size=options["beam_size"],
                vad_filter=options["vad_filter"],
                condition_on_previous_text=options["condition_on_previous_text"],
                word_timestamps=False,
            )
            segments = []
            for segment in segment_iterator:
                text = str(segment.text or "").strip()
                if not text:
                    continue
                absolute_start = float(item["start"]) + float(segment.start)
                absolute_end = float(item["start"]) + float(segment.end)
                absolute_end = min(absolute_end, float(media["end"]))
                if absolute_end <= absolute_start:
                    continue
                reasons = _uncertainty(segment, text)
                segments.append(
                    {
                        "chunk_index": item["index"],
                        "start": absolute_start,
                        "end": absolute_end,
                        "text": text,
                        "raw_text": text,
                        "source_language": (
                            options["language"] or getattr(info, "language", None) or "und"
                        ),
                        "language": (
                            options["language"] or getattr(info, "language", None) or "und"
                        ),
                        "provider": "faster-whisper",
                        "model": identity,
                        "avg_logprob": getattr(segment, "avg_logprob", None),
                        "compression_ratio": getattr(segment, "compression_ratio", None),
                        "no_speech_prob": getattr(segment, "no_speech_prob", None),
                        "uncertainty": reasons,
                        "review_status": "unreviewed",
                    }
                )
            document = {
                "schema_version": SCHEMA_VERSION,
                "key": key,
                "index": item["index"],
                "start": item["start"],
                "end": item["end"],
                "language": options["language"] or getattr(info, "language", None),
                "language_probability": getattr(info, "language_probability", None),
                "segments": segments,
                "model": identity,
                "settings": options,
                "elapsed_seconds": round(time.monotonic() - chunk_started, 3),
                "completed_at": _utc(),
            }
            write_json(checkpoint, document)
            register_generated(root, checkpoint)
            completed.append(document)
        except Exception as error:
            # The unfinished WAV is not evidence: remove it so a changed or
            # mismatched interval can never be reused silently.
            try:
                wav.unlink()
            except OSError:
                pass
            unregister_generated(root, wav)
            run_state.update(
                status="partial",
                chunks_completed=len(completed),
                completed_chunks=[entry.get("index") for entry in completed],
                error=str(error),
            )
            _persist_run_state(root, state_path, run_state)
            set_stage(
                root,
                "speech",
                "partial",
                source="local-asr",
                chunks_completed=len(completed),
                chunks_total=len(plan),
                error=str(error),
                updated_at=_utc(),
            )
            raise
        try:
            wav.unlink()
        except OSError:
            pass
        unregister_generated(root, wav)
        run_state.update(
            status="transcribing",
            chunks_completed=len(completed),
            completed_chunks=[entry.get("index") for entry in completed],
            error=None,
        )
        _persist_run_state(root, state_path, run_state)
        set_stage(
            root,
            "speech",
            "transcribing",
            source="local-asr",
            chunks_completed=len(completed),
            chunks_total=len(plan),
            model_path=str(model_dir),
            updated_at=_utc(),
        )
    merged = _merge_segments(completed)
    elapsed = round(time.monotonic() - started, 3)
    transcript_path, text_path = _write_transcript(
        root, merged, f"local faster-whisper ({model_dir.name})"
    )
    status = "transcribed" if merged else "no_speech_detected"
    language_counts = {}
    for record in merged:
        language = record.get("source_language") or "und"
        language_counts[language] = language_counts.get(language, 0) + 1
    state = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "key": key,
        "source_sha256": media.get("sha256"),
        "interval": {"start": media.get("start"), "end": media.get("end")},
        "model": identity,
        "settings": options,
        "plan": plan,
        "chunks_total": len(plan),
        "chunks_completed": len(completed),
        "completed_chunks": [document.get("index") for document in completed],
        "segments": len(merged),
        "languages": language_counts,
        "elapsed_seconds": elapsed,
        "tested_duration_seconds": float(media["end"]) - float(media["start"]),
        "started_at": started_at,
        "completed_at": _utc(),
        "limitations": [
            "Timestamps come from model segments, not calibrated word boundaries.",
            "Scores are review signals, not measured accuracy percentages.",
            "Automatic diarization is not implemented.",
            "A short smoke clip does not establish accuracy on a full code-switched lecture.",
        ],
    }
    write_json(state_path, state)
    register_generated(root, state_path)
    job = load_job(root)
    job.setdefault("stages", {})["speech"] = {
        "status": status,
        "source": "local-asr",
        "model_path": str(model_dir),
        "chunks_completed": len(completed),
        "chunks_total": len(plan),
        "segments": len(merged),
        "elapsed_seconds": elapsed,
        "updated_at": _utc(),
    }
    job["cleanup"] = None
    save_job(root, job)
    return {
        "status": status,
        "ok": status == "transcribed",
        "model": {
            "path": str(model_dir),
            "model_bin_sha256": identity["files"]["model.bin"]["sha256"],
        },
        "settings": options,
        "interval": {"start": media.get("start"), "end": media.get("end")},
        "chunks_total": len(plan),
        "chunks_completed": len(completed),
        "segments": len(merged),
        "languages": language_counts,
        "elapsed_seconds": elapsed,
        "transcript": str(transcript_path),
        "transcript_text": str(text_path),
        "note": (
            "Real local ASR was run. Review low-confidence segments and terminology; "
            "no transcript text was invented."
            if merged
            else "ASR completed but produced no speech segments. Review the audio and record "
            "no_speech_reviewed explicitly if that is correct."
        ),
    }


def mark_no_speech(root, status, reason, evidence=None):
    """Record a host-reviewed no-audio/no-speech outcome explicitly."""

    if status not in REVIEWED_NO_SPEECH:
        raise ValueError(
            "status must be one of: " + ", ".join(sorted(REVIEWED_NO_SPEECH))
        )
    if not isinstance(reason, str) or len(reason.strip()) < 8:
        raise ValueError("A specific review reason is required")
    root = Path(root).resolve()
    job = load_job(root)
    transcript = Path(root) / "work" / "transcript.json"
    if transcript.is_file():
        try:
            records = read_json(transcript)
        except Exception:
            records = []
        if isinstance(records, list) and any(
            isinstance(record, dict) and str(record.get("text", "")).strip()
            for record in records
        ):
            raise ValueError("A nonempty transcript already exists; do not hide spoken evidence")
    state = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason": reason.strip(),
        "evidence": evidence,
        "reviewed_at": _utc(),
        "reviewed_by_host": True,
    }
    path = root / "work" / "speech-state.json"
    write_json(path, state)
    register_generated(root, path)
    job.setdefault("stages", {})["speech"] = {
        "status": status,
        "reason": reason.strip(),
        "updated_at": _utc(),
    }
    job["cleanup"] = None
    save_job(root, job)
    return {"status": status, "reason": reason.strip(), "state": str(path)}


def speech_status(root):
    root = Path(root).resolve()
    job = load_job(root)
    transcript = Path(root) / "work" / "transcript.json"
    records = []
    if transcript.is_file():
        try:
            records = read_json(transcript)
        except Exception:
            records = []
    state_path = Path(root) / "work" / "speech-state.json"
    state = read_json(state_path) if state_path.is_file() else None
    return {
        "stage": job.get("stages", {}).get("speech"),
        "state": state,
        "records": len(records) if isinstance(records, list) else 0,
        "has_speech": bool(
            isinstance(records, list)
            and any(
                isinstance(record, dict) and str(record.get("text", "")).strip()
                for record in records
            )
        ),
    }
