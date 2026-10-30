"""Source ingest, metadata, audio cutting, and streaming frame sampling.

Frame extraction decodes one candidate at a time, compares it against retained
representatives, and only persists representatives.  Duplicate candidates keep
their timestamp mappings in ``work/frames-state.json``; no generated duplicate
copy is left on disk.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlparse

import numpy as np
from PIL import Image

from .adaptive import (
    DEFAULT_INTERVAL,
    frame_interval,
    plan_refinement,
    validate_refinement_level,
)
from .jobs import (
    SourceChangedError,
    file_digest,
    json_digest,
    load_job,
    read_json,
    register_generated,
    save_job,
    unregister_generated,
    write_json,
)
from .timing import sample_times


COMPARISON_VERSION = "rgb-local-v2"
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

_TOOL_CACHE = {}


def tool_path(name):
    """Resolve an external tool from PATH, then the current Windows user PATH.

    A long-running host process can inherit a stale PATH after a package update.
    Reading the configured user/machine PATH is a non-mutating capability check;
    it does not install or change anything.
    """

    if name in _TOOL_CACHE:
        return _TOOL_CACHE[name]
    found = shutil.which(name)
    if not found and os.name == "nt":
        directories = []
        try:
            import winreg

            for hive, key in (
                (winreg.HKEY_CURRENT_USER, "Environment"),
                (
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                ),
            ):
                try:
                    with winreg.OpenKey(hive, key) as handle:
                        value, _ = winreg.QueryValueEx(handle, "Path")
                    directories.extend(str(value).split(";"))
                except OSError:
                    continue
        except Exception:
            directories = []
        directories.extend(os.environ.get("PATH", "").split(";"))
        for directory in directories:
            directory = directory.strip().strip('"')
            if not directory:
                continue
            for extension in (".exe", ".cmd", ".bat", ""):
                candidate = Path(directory) / f"{name}{extension}"
                if candidate.is_file():
                    found = str(candidate)
                    break
            if found:
                break
    _TOOL_CACHE[name] = found or name
    return _TOOL_CACHE[name]


YOUTUBE_MIN_YTDLP = "2026.10.21"
JS_RUNTIME_ORDER = (
    ("deno", "deno"),
    ("node", "node"),
    ("quickjs", "qjs"),
    ("bun", "bun"),
)
_RUNTIME_MIN_FALLBACK = {
    "deno": (2, 3, 0),
    "node": (22, 0, 0),
    "quickjs": (2023, 12, 9),
    "bun": (1, 2, 11),
}
_JS_RUNTIME_CACHE = None
_JS_RUNTIME_REJECTED = []
_RUNTIME_VERSION_PATTERNS = {
    # These mirror yt-dlp's own runtime probes: deno/node/bun print their
    # version directly, while QuickJS has no --version and returns help text
    # with a nonzero status.  Patterns are searched line by line because the
    # version line is not necessarily the first line of QuickJS --help output.
    "deno": r"^deno\s+(\S+)",
    "node": r"^v?(\d[\w.-]*)",
    "quickjs": r"^QuickJS(?:-ng)?\s+version\s+(\S+)",
    "bun": r"^(\d[\w.-]*)",
}


def _version_tuple(value):
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts)


def _version_at_least(actual, minimum):
    left = tuple(actual or ())
    right = tuple(minimum or ())
    size = max(len(left), len(right))
    return left + (0,) * (size - len(left)) >= right + (0,) * (size - len(right))


def _display_version(value):
    return ".".join(str(part) for part in value or ())


def _runtime_minimum(name):
    try:
        from yt_dlp.globals import supported_js_runtimes

        cls = supported_js_runtimes.value.get(name)
        minimum = getattr(cls, "MIN_SUPPORTED_VERSION", None)
        if minimum:
            return tuple(minimum)
    except Exception:
        pass
    return _RUNTIME_MIN_FALLBACK.get(name)


def _runtime_version(name, output):
    """Extract the version token from the runtime's own output.

    Only a numeric version counts.  A successful exit status alone is not
    enough to establish support.
    """

    pattern = _RUNTIME_VERSION_PATTERNS.get(name)
    if not pattern:
        return None
    match = re.search(pattern, output or "", re.MULTILINE)
    if not match:
        return None
    version = match.group(1).strip()
    return version if _version_tuple(version) else None


def _runtime_version_line(output, version):
    for line in (output or "").splitlines():
        if version in line:
            return line.strip()
    return version


def _probe_js_runtime(name, executable):
    """Return ``(runtime, rejection)`` for one locally installed candidate."""

    path = tool_path(executable)
    candidate = Path(path)
    if not candidate.is_file():
        return None, None
    minimum = _runtime_minimum(name)
    if not minimum:
        return None, {
            "name": name,
            "path": str(candidate),
            "version": None,
            "minimum_version": None,
            "reason": "installed yt-dlp exposes no version contract",
        }
    command = [str(candidate), "--help" if name == "quickjs" else "--version"]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return None, {
            "name": name,
            "path": str(candidate),
            "version": None,
            "minimum_version": _display_version(minimum),
            "reason": "version probe failed",
        }
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    version = _runtime_version(name, output)
    if completed.returncode != 0 and name != "quickjs":
        return None, {
            "name": name,
            "path": str(candidate),
            "version": _runtime_version_line(output, version) if version else None,
            "minimum_version": _display_version(minimum),
            "reason": f"version probe exited with status {completed.returncode}",
        }
    if not version:
        return None, {
            "name": name,
            "path": str(candidate),
            "version": None,
            "minimum_version": _display_version(minimum),
            "reason": "version output was unreadable",
        }
    version_tuple = _version_tuple(version)
    # QuickJS-ng uses its own release numbering; yt-dlp only requires a
    # positive ng version, while classic QuickJS uses date-based versions.
    variant = "quickjs-ng" if name == "quickjs" and "QuickJS-ng" in output else None
    effective_minimum = (0,) if variant else minimum
    supported = (
        version_tuple > effective_minimum
        if variant
        else _version_at_least(version_tuple, effective_minimum)
    )
    if not supported:
        return None, {
            "name": name,
            "path": str(candidate),
            "version": _runtime_version_line(output, version),
            "minimum_version": _display_version(effective_minimum),
            "reason": (
                "requires a positive QuickJS-ng version"
                if variant
                else f"requires {_display_version(effective_minimum)} or newer"
            ),
        }
    runtime = {
        "name": name,
        "path": str(candidate),
        "version": _runtime_version_line(output, version),
        "minimum_version": _display_version(effective_minimum),
        "supported": True,
    }
    if variant:
        runtime["variant"] = variant
    return runtime, None


def js_runtime():
    """Return the highest-priority supported local JS runtime yt-dlp can use.

    yt-dlp's installed help documents deno, node, quickjs, and bun in that
    priority order. Runtime minimum versions are read from the installed
    yt-dlp runtime classes when available. Candidates that are absent, fail
    their own version probe, expose no parseable version, or fall below the
    installed contract are skipped; nothing is downloaded or installed.
    """

    global _JS_RUNTIME_CACHE
    if _JS_RUNTIME_CACHE is not None:
        return _JS_RUNTIME_CACHE if _JS_RUNTIME_CACHE else None
    _JS_RUNTIME_REJECTED.clear()
    for name, executable in JS_RUNTIME_ORDER:
        runtime, rejection = _probe_js_runtime(name, executable)
        if rejection:
            _JS_RUNTIME_REJECTED.append(rejection)
            continue
        if runtime:
            _JS_RUNTIME_CACHE = runtime
            return runtime
    _JS_RUNTIME_CACHE = {}
    return None


def youtube_readiness():
    """Report YouTube-only readiness separately from local-video readiness."""

    yt_dlp_version = None
    try:
        yt_dlp_version = importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        yt_dlp_version = None
    ejs_version = None
    try:
        ejs_version = importlib.metadata.version("yt-dlp-ejs")
    except importlib.metadata.PackageNotFoundError:
        ejs_version = None
    runtime = js_runtime()
    yt_dlp_supported = bool(
        yt_dlp_version
        and _version_at_least(_version_tuple(yt_dlp_version), _version_tuple(YOUTUBE_MIN_YTDLP))
    )
    metadata_ready = yt_dlp_supported
    stream_ready = bool(yt_dlp_supported and ejs_version and runtime)
    hints = []
    if not yt_dlp_version:
        hints.append(
            "Install the official YouTube extra from the package index: "
            f'pip install "yt-dlp[default]>={YOUTUBE_MIN_YTDLP}"'
        )
    elif not yt_dlp_supported:
        hints.append(
            f"yt-dlp {yt_dlp_version} is below the required {YOUTUBE_MIN_YTDLP} for the "
            "safe config-free YouTube invocation; upgrade with "
            f'pip install --upgrade "yt-dlp[default]>={YOUTUBE_MIN_YTDLP}"'
        )
    if yt_dlp_version and not ejs_version:
        hints.append(
            "The official yt-dlp EJS component is missing; reinstall the default extra: "
            f'pip install --upgrade "yt-dlp[default]>={YOUTUBE_MIN_YTDLP}"'
        )
    if runtime is None:
        hints.append(
            "No supported local JavaScript runtime is available for YouTube stream "
            "extraction. Install Deno >=2.3.0 (preferred), Node.js >=22.0.0, "
            "Bun >=1.2.11, or QuickJS >=2023-12-09; runtimes are never downloaded "
            "automatically."
        )
        if _JS_RUNTIME_REJECTED:
            rejected = "; ".join(
                f"{item['name']} {item.get('version') or 'unknown'} ({item['reason']})"
                for item in _JS_RUNTIME_REJECTED
            )
            hints.append(f"Detected but unusable local runtimes: {rejected}.")
    return {
        "ready": stream_ready,
        "metadata_ready": metadata_ready,
        "stream_ready": stream_ready,
        "yt_dlp": {
            "available": bool(yt_dlp_version),
            "version": yt_dlp_version,
            "supported": yt_dlp_supported,
            "minimum_tested": YOUTUBE_MIN_YTDLP,
        },
        "ejs": {
            "available": bool(ejs_version),
            "version": ejs_version,
            "package": "yt-dlp-ejs",
            "provided_by": 'yt-dlp[default] extra',
        },
        "js_runtime": runtime,
        "supported_js_runtimes": [name for name, _ in JS_RUNTIME_ORDER],
        "anonymous_single_video": True,
        "config_free": True,
        "remote_components_allowed": False,
        "invocation_flags": [
            "--ignore-config",
            "--no-config-locations",
            "--no-plugin-dirs",
            "--no-cookies",
            "--no-cookies-from-browser",
            "--no-cache-dir",
            "--no-exec",
            "--no-remote-components",
            "--no-js-runtimes",
            "--js-runtimes <detected>:<path>",
            "--no-playlist",
        ],
        "setup_hints": hints,
    }


def run_command(arguments, timeout=None):
    """Run an argument list without a shell and return stdout bytes."""

    if not isinstance(arguments, (list, tuple)) or not arguments:
        raise ValueError("Commands must be nonempty argument lists")
    completed = subprocess.run(
        [str(item) for item in arguments],
        check=True,
        capture_output=True,
        timeout=timeout,
    )
    return completed.stdout


def run_text(arguments, timeout=None):
    completed = subprocess.run(
        [str(item) for item in arguments],
        check=True,
        capture_output=True,
        timeout=timeout,
    )
    return completed.stdout.decode("utf-8", errors="replace")


def _failure(arguments, error):
    stderr = ""
    if isinstance(error, subprocess.CalledProcessError) and error.stderr:
        stderr = error.stderr.decode("utf-8", errors="replace").strip()
    detail = stderr.splitlines()[-1] if stderr else str(error)
    return ValueError(f"Command failed: {Path(str(arguments[0])).name}: {detail}")


def probe(video):
    video = Path(video)
    if not video.is_file():
        raise ValueError(f"Media file not found: {video}")
    try:
        output = run_text(
            [
                tool_path("ffprobe"),
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(video),
            ],
            timeout=120,
        )
    except Exception as error:
        raise _failure([tool_path("ffprobe"), str(video)], error) from error
    metadata = json.loads(output)
    if not isinstance(metadata, dict) or not metadata.get("format"):
        raise ValueError(f"ffprobe returned no format metadata for {video}")
    return metadata


def _streams(metadata, codec_type):
    return [
        stream
        for stream in metadata.get("streams", [])
        if stream.get("codec_type") == codec_type
    ]


def has_audio(metadata):
    return bool(_streams(metadata, "audio"))


def _duration(metadata):
    candidates = [metadata.get("format", {}).get("duration")]
    candidates.extend(stream.get("duration") for stream in metadata.get("streams", []))
    for value in candidates:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            return number
    raise ValueError("Could not establish a positive finite media duration")


def _finite_range(start, end, duration):
    for value in (start, end):
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Selected start/end must be finite numbers")
    if start < 0 or end <= start:
        raise ValueError("Require 0 <= start < end")
    tolerance = max(0.05, duration * 1e-6)
    if end > duration + tolerance:
        raise ValueError(
            f"Selected end {end:.3f}s is outside the media duration {duration:.3f}s"
        )
    if start >= duration:
        raise ValueError(f"Selected start {start:.3f}s is outside the media duration")
    return float(start), float(min(end, duration))


def _requested_range(job, duration):
    preferences = job.get("preferences", {})
    prepared = job.get("media") if isinstance(job.get("media"), dict) else {}
    start = preferences.get("start", prepared.get("start", 0))
    end = preferences.get("end", prepared.get("end", duration))
    if end is None:
        end = duration
    try:
        start = float(start)
        end = float(end)
    except (TypeError, ValueError) as error:
        raise ValueError("preferences.start and preferences.end must be numbers") from error
    return _finite_range(start, end, duration)


def _ytdlp_arguments(root):
    """Explicit anonymous, single-video yt-dlp invocation.

    Ignore user/system config, clear plugin directories, disable cookies,
    browser-cookie loading, exec hooks, and cache writes.  Intermediate files
    stay inside the job's source directory.
    """

    destination = Path(root) / "work" / "source"
    temporary = destination / "tmp"
    destination.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    runtime = js_runtime()
    arguments = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-config-locations",
        "--no-plugin-dirs",
        "--no-cookies",
        "--no-cookies-from-browser",
        "--no-cache-dir",
        "--no-exec",
        "--no-remote-components",
        "--no-js-runtimes",
    ]
    if runtime:
        arguments.extend(["--js-runtimes", f"{runtime['name']}:{runtime['path']}"])
    arguments.extend([
        "--no-playlist",
        "--no-write-playlist-metafiles",
        "--paths",
        f"temp:{temporary}",
    ])
    return arguments


def _register_source_artifacts(root, destination):
    if not destination.is_dir():
        return
    for created in sorted(destination.rglob("*")):
        if created.is_file():
            register_generated(root, created)


def _youtube_metadata(root, url):
    destination = Path(root) / "work" / "source"
    destination.mkdir(parents=True, exist_ok=True)
    arguments = _ytdlp_arguments(root) + [
        "--skip-download",
        "--write-info-json",
        "-o",
        str(destination / "lecture.%(ext)s"),
        "--",
        url,
    ]
    try:
        output = run_text(arguments)
    except Exception as error:
        _register_source_artifacts(root, destination)
        raise _failure(["yt-dlp", url], error) from error
    _register_source_artifacts(root, destination)
    info_files = sorted(destination.glob("lecture.info.json"))
    if not info_files:
        raise ValueError(
            "YouTube metadata download produced no info JSON; check access and yt-dlp support"
        )
    info_path = info_files[-1]
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except Exception as error:
        raise ValueError(f"YouTube metadata is not valid JSON: {error}") from error
    candidates = _caption_candidates(info)
    return info, info_path, candidates, output


def _caption_candidates(info):
    candidates = []
    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    source_language = info.get("language")
    for language, formats in sorted(manual.items()):
        for entry in formats:
            name = str(entry.get("name") or language)
            candidates.append(
                {
                    "language": language,
                    "name": name,
                    "kind": "manual",
                    "translated": bool(
                        "from " in name.lower() or "tlang=" in str(entry.get("url", ""))
                    ),
                    "extensions": sorted(
                        {
                            str(item.get("ext"))
                            for item in formats
                            if isinstance(item, dict) and item.get("ext")
                        }
                    ),
                }
            )
            break
    for language, formats in sorted(automatic.items()):
        for entry in formats:
            name = str(entry.get("name") or language)
            candidates.append(
                {
                    "language": language,
                    "name": name,
                    "kind": "automatic",
                    "translated": bool(
                        "from " in name.lower() or "tlang=" in str(entry.get("url", ""))
                    ),
                    "extensions": sorted(
                        {
                            str(item.get("ext"))
                            for item in formats
                            if isinstance(item, dict) and item.get("ext")
                        }
                    ),
                }
            )
            break
    return {
        "source_language_hint": source_language,
        "candidates": candidates,
        "ambiguous": len({item["language"] for item in candidates}) > 1,
    }


def download_youtube(
    root,
    url,
    caption_languages="ar.*,en.*,.*-orig",
    include_metadata=True,
):
    """Download one video (never a playlist) plus caption candidates."""

    host = urlparse(url).hostname
    if host not in YOUTUBE_HOSTS:
        raise ValueError(
            "Only single YouTube video URLs are supported; provide a local video for other sources"
        )
    if isinstance(caption_languages, (list, tuple, set)):
        caption_languages = ",".join(str(item) for item in caption_languages if str(item).strip())
    if not str(caption_languages).strip():
        caption_languages = "ar.*,en.*,.*-orig"
    candidates = {"candidates": [], "ambiguous": False, "source_language_hint": None}
    info_path = None
    if include_metadata:
        info, info_path, candidates, _ = _youtube_metadata(root, url)
        if (
            str(caption_languages).strip() == "ar.*,en.*,.*-orig"
            and candidates.get("source_language_hint")
        ):
            hint = str(candidates["source_language_hint"]).strip()
            if hint:
                caption_languages = f"{hint}.*,ar.*,en.*,.*-orig"
    readiness = youtube_readiness()
    if not readiness["stream_ready"]:
        details = " ".join(readiness["setup_hints"]) or "YouTube stream readiness is incomplete."
        raise ValueError(
            "YouTube stream extraction needs yt-dlp with the official default extra "
            f"and a local JavaScript runtime. {details}"
        )
    destination = Path(root) / "work" / "source"
    destination.mkdir(parents=True, exist_ok=True)
    arguments = _ytdlp_arguments(root) + [
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-format",
        "vtt",
        "--sub-langs",
        caption_languages,
        "--merge-output-format",
        "mp4",
        "-o",
        str(destination / "lecture.%(ext)s"),
        "--print",
        "after_move:filepath",
        "--",
        url,
    ]
    stderr = ""
    try:
        completed = subprocess.run(
            arguments, check=True, capture_output=True, text=True, encoding="utf-8"
        )
        stdout = completed.stdout
        stderr = completed.stderr
    except Exception as error:
        detail = ""
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            detail = error.stderr.strip().splitlines()[-1]
        raise ValueError(
            f"YouTube download failed: {detail or error}. Partial files remain registered for inspection."
        ) from error
    finally:
        # Register useful partial artifacts as well as completed ones.
        _register_source_artifacts(root, destination)
    video_candidates = [
        Path(line.strip())
        for line in stdout.splitlines()
        if line.strip() and Path(line.strip()).is_file()
    ]
    video_candidates = [
        item
        for item in video_candidates
        if item.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
    ]
    if not video_candidates:
        raise ValueError(
            "YouTube download produced no usable video file; inspect the download error"
        )
    video = max(video_candidates, key=lambda item: item.stat().st_size)
    captions = sorted(
        path
        for path in destination.iterdir()
        if path.is_file() and path.suffix.lower() in {".vtt", ".srt"}
    )
    for path in captions:
        register_generated(root, path)
    job = load_job(root)
    media = job.get("media")
    if not isinstance(media, dict):
        media = {}
    media["caption_candidates"] = candidates
    media["caption_files"] = [
        str(path.relative_to(Path(root)).as_posix()) for path in captions
    ]
    media["download_stderr_tail"] = (
        stderr.strip().splitlines()[-5:] if stderr.strip() else []
    )
    job["media"] = media
    save_job(root, job)
    return {
        "video": video,
        "captions": captions,
        "info": info_path,
        "caption_candidates": candidates,
    }


def _invalidate_derived(root, job):
    """Remove stale speech/frame evidence after an explicitly accepted change."""

    prefixes = (
        "work/frames/",
        "work/frames-state.json",
        "work/screen.json",
        "work/transcript.json",
        "work/transcript.txt",
        "work/speech-state.json",
        "work/captions-meta.json",
        "work/audio/",
    )
    stale = [
        relative
        for relative in job.get("generated", [])
        if relative.startswith(prefixes)
    ]
    for relative in stale:
        path = Path(root) / relative
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
        unregister_generated(root, path)
    job = load_job(root)
    for name in ("speech", "frames", "screen"):
        job.get("stages", {}).pop(name, None)
    job["preview"] = None
    job["delivered"] = False
    job["outputs"] = {}
    job["cleanup"] = None
    job.pop("media", None)
    save_job(root, job)


def prepare_source(root, allow_source_change=False):
    """Probe/download one source, validate the selected range, and bind derivatives."""

    root = Path(root).resolve()
    job = load_job(root)
    source = job["source"]
    existing = job.get("media")
    if isinstance(existing, dict) and existing.get("path") and existing.get("sha256"):
        video = Path(existing["path"])
        if not video.is_file():
            raise SourceChangedError(
                "The prepared source file is missing. Restore it or prepare the source again "
                "with an explicit source-change reset."
            )
        current = file_digest(video)
        if current != existing["sha256"]:
            if not allow_source_change:
                raise SourceChangedError(
                    "Source video changed since preparation; stale evidence will not be reused. "
                    "Re-run with an explicit source-change reset after confirming the new file."
                )
            _invalidate_derived(root, job)
            job = load_job(root)
            existing = None
        else:
            duration = float(existing["duration"])
            start, end = _requested_range(job, duration)
            if existing.get("start") is None or existing.get("end") is None:
                existing["start"] = start
                existing["end"] = end
                existing.setdefault("requested_start", start)
                existing.setdefault("requested_end", end)
                job["media"] = existing
                save_job(root, job)
            elif (
                abs(float(existing["start"]) - start) > 1e-6
                or abs(float(existing["end"]) - end) > 1e-6
            ):
                raise ValueError(
                    "Selected interval changed after preparation; reset dependent evidence "
                    "before extracting frames or audio for the new range"
                )
            metadata = probe(video)
            if not _streams(metadata, "video"):
                raise ValueError("Source has no video stream")
            return existing

    if source.startswith(("https://", "http://")):
        downloaded = download_youtube(
            root,
            source,
            caption_languages=job.get("preferences", {}).get(
                "caption_languages", "ar.*,en.*,.*-orig"
            ),
        )
        video = downloaded["video"]
        existing_media = load_job(root).get("media", {})
    else:
        video = Path(source)
        if not video.is_file():
            raise ValueError(f"Local source video not found: {video}")
        existing_media = job.get("media", {})

    metadata = probe(video)
    if not _streams(metadata, "video"):
        raise ValueError("Source has no video stream")
    duration = _duration(metadata)
    start, end = _requested_range(load_job(root), duration)
    video_stream = _streams(metadata, "video")[0]
    audio_streams = _streams(metadata, "audio")
    format_start = metadata.get("format", {}).get("start_time", 0)
    try:
        time_origin = float(format_start)
    except (TypeError, ValueError):
        time_origin = 0.0
    rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0/1"
    try:
        numerator, denominator = (int(part) for part in str(rate).split("/", 1))
        fps = numerator / denominator if denominator else None
    except (TypeError, ValueError, ZeroDivisionError):
        fps = None
    job = load_job(root)
    media = {
        "path": str(video.resolve()),
        "sha256": file_digest(video),
        "size": video.stat().st_size,
        "duration": duration,
        "start": start,
        "end": end,
        "requested_start": start,
        "requested_end": end,
        "time_origin": time_origin,
        "fps": fps,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "video_codec": video_stream.get("codec_name"),
        "has_audio": bool(audio_streams),
        "audio_streams": [
            {
                "index": stream.get("index"),
                "codec": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "sample_rate": stream.get("sample_rate"),
                "language": (stream.get("tags") or {}).get("language"),
            }
            for stream in audio_streams
        ],
        "probe": metadata,
        "prepared_at": job.get("created_at"),
        "caption_candidates": existing_media.get("caption_candidates"),
        "caption_files": existing_media.get("caption_files"),
    }
    job["media"] = media
    job.setdefault("stages", {})["source"] = {
        "status": "prepared",
        "duration": duration,
        "start": start,
        "end": end,
        "sha256": media["sha256"],
    }
    job["cleanup"] = None
    save_job(root, job)
    return media


def same_content(first, candidate):
    """Conservative comparison that keeps small but local content changes.

    A tiny isolated encoding difference is treated as duplicate noise.  A small
    symbol, a new label, or a thin added line creates a local cluster and is
    retained.  Ambiguous comparisons therefore favour retention.
    """

    if first.size != candidate.size:
        return False
    left = np.asarray(first.convert("RGB"), dtype=np.int16)
    right = np.asarray(candidate.convert("RGB"), dtype=np.int16)
    difference = np.abs(left - right)
    max_difference = int(difference.max())
    if max_difference <= 6:
        return True
    # A single high-contrast content pixel (for example a decimal point or a
    # newly written mark) is a real change, not noise.  This deliberately
    # over-retains occasional lossy-codec edge pixels: storage is cheaper than
    # silently losing scientific content.
    if max_difference >= 96:
        return False
    changed = difference.max(axis=2) > 24
    count = int(changed.sum())
    if count == 0:
        return True
    total = changed.size
    if count / total >= 0.00035:
        return False
    # Localized clusters protect small symbols and short strokes.
    try:
        from numpy.lib.stride_tricks import sliding_window_view

        local = sliding_window_view(changed, (3, 3)).sum(axis=(2, 3))
        if int(local.max()) >= 4:
            return False
    except Exception:
        pass
    rows, columns = np.nonzero(changed)
    if count >= 8:
        height = int(rows.max() - rows.min() + 1)
        width = int(columns.max() - columns.min() + 1)
        density = count / max(1, height * width)
        if (height >= 8 or width >= 8) and density >= 0.5:
            return False
    return True


def _coarse(image):
    small = image.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
    return list(small.tobytes())


def frame_at(media, requested):
    """Decode the nearest available frame; return ``(image, decoded_seconds)``."""

    if not isinstance(requested, (int, float)) or not math.isfinite(requested):
        raise ValueError("Requested frame time must be a finite number")
    duration = float(media["duration"])
    origin = float(media.get("time_origin") or 0)
    base = min(float(requested), max(0.0, duration - 0.1))
    attempts = [base]
    for offset in (0.5, 1.0, 2.0):
        value = max(0.0, base - offset)
        if value not in attempts:
            attempts.append(value)
    last_error = None
    for instant in attempts:
        arguments = [
            tool_path("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "info",
            "-copyts",
            "-ss",
            f"{instant:.3f}",
            "-i",
            media["path"],
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-frames:v",
            "1",
            "-vf",
            "showinfo",
            "-f",
            "image2pipe",
            "-c:v",
            "png",
            "pipe:1",
        ]
        try:
            completed = subprocess.run(arguments, check=False, capture_output=True)
        except OSError as error:
            raise ValueError(
                "FFmpeg is not available; install it and make it available on PATH"
            ) from error
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0 or not completed.stdout:
            last_error = stderr.strip().splitlines()[-1] if stderr.strip() else "no frame returned"
            continue
        matches = re.findall(r"pts_time:([\d.eE+-]+)", stderr)
        if not matches:
            last_error = "FFmpeg did not report the decoded frame timestamp"
            continue
        try:
            pts = float(matches[0])
        except ValueError:
            last_error = f"Unreadable decoded timestamp: {matches[0]!r}"
            continue
        image = Image.open(io.BytesIO(completed.stdout)).convert("RGB")
        return image, max(0.0, pts - origin)
    raise ValueError(f"Could not decode a frame at {requested:.3f}s: {last_error}")


def frame_configuration(job, interval=None):
    media = job.get("media")
    if not isinstance(media, dict) or not media.get("sha256"):
        raise ValueError("Prepare the source before sampling frames")
    preferences = job.get("preferences")
    if preferences is None:
        preferences = {}
    if not isinstance(preferences, dict):
        raise ValueError("job preferences must be an object")
    if interval is None:
        interval = preferences.get("frame_interval_seconds")
    interval = frame_interval(interval)
    return {
        "source": media["sha256"],
        "start": media["start"],
        "end": media["end"],
        "time_origin": media.get("time_origin"),
        "interval": interval,
        "comparison": COMPARISON_VERSION,
    }


def _seed_representatives(state, root):
    representatives = state.get("representatives")
    if isinstance(representatives, list) and (representatives or not state.get("frames")):
        return representatives
    representatives = []
    seen = {}
    for frame in [*state.get("frames", []), *(state.get("refinements") or [])]:
        identifier = frame.get("frame_id")
        if not identifier:
            continue
        if identifier not in seen:
            path = frame.get("path")
            coarse = []
            candidate = Path(root) / path if path else None
            if candidate and candidate.is_file():
                try:
                    coarse = _coarse(Image.open(candidate).convert("RGB"))
                except Exception:
                    coarse = []
            item = {
                "frame_id": identifier,
                "path": path,
                "first_requested": frame.get("requested"),
                "occurrences": [],
                "coarse": coarse,
            }
            seen[identifier] = item
            representatives.append(item)
        seen[identifier]["occurrences"].append(
            {
                "requested": frame.get("requested"),
                "decoded": frame.get("timestamp"),
                "path": frame.get("path"),
                "duplicate": bool(frame.get("duplicate")),
            }
        )
    state["representatives"] = representatives
    return representatives


def _match_representative(root, candidate, representatives):
    if not representatives:
        return None
    latest = representatives[-1]
    latest_path = Path(root) / latest["path"]
    if latest_path.is_file():
        try:
            if same_content(Image.open(latest_path).convert("RGB"), candidate):
                return latest
        except Exception:
            pass
    coarse = _coarse(candidate)
    for representative in reversed(representatives[:-1]):
        stored = representative.get("coarse")
        if not isinstance(stored, list) or len(stored) != len(coarse):
            continue
        distance = max(abs(int(a) - int(b)) for a, b in zip(coarse, stored))
        if distance > 18:
            continue
        path = Path(root) / representative["path"]
        if not path.is_file():
            continue
        try:
            if same_content(Image.open(path).convert("RGB"), candidate):
                return representative
        except Exception:
            continue
    return None


def _reviewed_ids(root):
    path = Path(root) / "work" / "screen.json"
    if not path.is_file():
        return set()
    try:
        records = read_json(path)
    except Exception:
        return set()
    if not isinstance(records, list):
        return set()
    return {
        record.get("id")
        for record in records
        if isinstance(record, dict)
        and (record.get("review_status") or record.get("status")) in {"reviewed", "unreadable"}
    }


def _inserted_count(root):
    path = Path(root) / "work" / "content.json"
    if not path.is_file():
        return 0
    try:
        content = read_json(path)
    except Exception:
        return 0
    count = 0
    for section in content.get("sections", []) if isinstance(content, dict) else []:
        for block in section.get("blocks", []) if isinstance(section, dict) else []:
            if isinstance(block, dict) and block.get("type") == "image":
                count += 1
    return count


def _next_screen_id(representatives):
    highest = len(representatives)
    for item in representatives:
        match = re.fullmatch(r"screen-(\d+)", str(item.get("frame_id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"screen-{highest + 1:06}"


def _frame_counts(state, baseline_total):
    frames = state.get("frames") or []
    refinements = state.get("refinements") or []
    representatives = state.get("representatives") or []
    base_dups = sum(1 for item in frames if item.get("duplicate"))
    ref_dups = sum(1 for item in refinements if item.get("duplicate"))
    return {
        "candidates": baseline_total,
        "extracted": len(frames),
        "retained": len(representatives),
        "baseline_candidates": baseline_total,
        "baseline_extracted": len(frames),
        "baseline_duplicates": base_dups,
        "refinement_candidates": len(refinements),
        "refinement_extracted": len(refinements),
        "refinement_duplicates": ref_dups,
        "unique_retained_representatives": len(representatives),
        "duplicate_candidates": base_dups + ref_dups,
    }


def _invalidate_output_bindings(root):
    job = load_job(root)
    changed = False
    for key, empty in (("preview", None), ("delivered", False), ("outputs", {}), ("cleanup", None)):
        if job.get(key) != empty:
            job[key] = empty
            changed = True
    if job.pop("delivered_at", None) is not None:
        changed = True
    if job.get("stages", {}).pop("delivery", None) is not None:
        changed = True
    if changed:
        save_job(root, job)
    return changed


def _finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _same_window(record, start, end):
    ws, we = record.get("window_start"), record.get("window_end")
    if isinstance(ws, bool) or isinstance(we, bool):
        return False
    if not isinstance(ws, (int, float)) or not isinstance(we, (int, float)):
        return False
    return abs(float(ws) - start) <= 1e-6 and abs(float(we) - end) <= 1e-6


def _require_unchanged_prepared_source(root):
    job = load_job(root)
    media = job.get("media")
    if (
        not isinstance(media, dict)
        or not isinstance(media.get("path"), str)
        or not media["path"].strip()
        or not isinstance(media.get("sha256"), str)
        or not media["sha256"].strip()
    ):
        raise ValueError("Prepare the source before refining frames")
    video = Path(media["path"])
    if not video.is_file():
        raise SourceChangedError("The prepared source file is missing; restore or re-prepare it.")
    current = file_digest(video)
    if current != media["sha256"]:
        raise SourceChangedError("Source video changed since preparation; stale evidence rejected.")
    fingerprint = job.get("source_fingerprint")
    source_value = job.get("source")
    if isinstance(source_value, str) and not source_value.startswith(("https://", "http://")):
        source_path = Path(source_value)
        if not source_path.is_file():
            raise SourceChangedError("Original local source file is missing.")
        source_sha = current if source_path.resolve() == video.resolve() else file_digest(source_path)
        if isinstance(fingerprint, dict) and fingerprint.get("sha256") and source_sha != fingerprint["sha256"]:
            raise SourceChangedError("Original local source file changed since job creation.")
    duration = _finite_number(media.get("duration"), "media.duration")
    if duration <= 0:
        raise ValueError("Prepared media duration must be positive")
    start = _finite_number(media.get("start"), "media.start")
    end = _finite_number(media.get("end"), "media.end")
    expected_start, expected_end = _requested_range(job, duration)
    if abs(start - expected_start) > 1e-6 or abs(end - expected_end) > 1e-6:
        raise ValueError("Selected interval changed after preparation; reset dependent evidence")
    return job, media


def sample_frames(root, limit=0, interval=None):
    """Extract baseline frame candidates in bounded batches with durable resume."""

    root = Path(root).resolve()
    prepare_source(root)
    job = load_job(root)
    settings = frame_configuration(job, interval)
    if limit is None:
        limit = 0
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a nonnegative integer")
    state_path = root / "work" / "frames-state.json"
    key = json_digest(settings)
    if state_path.exists():
        state = read_json(state_path)
        if not isinstance(state, dict) or state.get("key") != key:
            raise ValueError(
                "Sampling settings or source changed; stale frame evidence will not be reused. "
                "Reset the frame stage explicitly before sampling the new interval."
            )
    else:
        state = {
            "schema_version": 1,
            "key": key,
            "settings": settings,
            "requested_end": settings["end"],
            "start": settings["start"],
            "end": settings["end"],
            "interval": settings["interval"],
            "frames": [],
            "representatives": [],
            "refinements": [],
            "adaptive_windows": {},
            "complete": False,
            "created_at": job.get("created_at"),
        }
    state.setdefault("settings", settings)
    state.setdefault("frames", [])
    state.setdefault("representatives", [])
    state.setdefault("refinements", [])
    state.setdefault("adaptive_windows", {})
    state["requested_end"] = settings["end"]
    state["complete"] = False
    representatives = _seed_representatives(state, root)
    directory = root / "work" / "frames"
    directory.mkdir(parents=True, exist_ok=True)
    candidates = list(sample_times(settings["start"], settings["end"], settings["interval"]))
    completed = len(state["frames"])
    if completed > len(candidates):
        raise ValueError("Frame state has more candidates than the current schedule; reset it")
    stop = min(len(candidates), completed + limit) if limit else len(candidates)
    for index in range(completed, stop):
        requested = candidates[index]
        image, decoded = frame_at(job["media"], requested)
        representative = _match_representative(root, image, representatives)
        duplicate = representative is not None
        if duplicate:
            identifier = representative["frame_id"]
            path = representative["path"]
            occurrence = len(representative.setdefault("occurrences", []))
            representative["occurrences"].append(
                {"requested": requested, "decoded": decoded, "duplicate": True, "path": path}
            )
        else:
            identifier = _next_screen_id(representatives)
            path = f"work/frames/{identifier}.png"
            image.save(root / path)
            register_generated(root, root / path)
            occurrence = 0
            representative = {
                "frame_id": identifier,
                "path": path,
                "first_requested": requested,
                "first_decoded": decoded,
                "coarse": _coarse(image),
                "occurrences": [
                    {"requested": requested, "decoded": decoded, "duplicate": False, "path": path}
                ],
            }
            representatives.append(representative)
            _invalidate_output_bindings(root)
        state["frames"].append(
            {
                "index": index,
                "requested": requested,
                "timestamp": decoded,
                "decoded": decoded,
                "frame_id": identifier,
                "path": path,
                "duplicate": duplicate,
                "occurrence": occurrence,
            }
        )
        state["counts"] = _frame_counts(state, len(candidates))
        write_json(state_path, state)
        register_generated(root, state_path)
        if index % 25 == 0:
            print(
                json.dumps(
                    {
                        "stage": "frames",
                        "processed": index + 1,
                        "total": len(candidates),
                        "retained": len(representatives),
                    }
                ),
                file=sys.stderr,
            )
    state["complete"] = len(state["frames"]) == len(candidates)
    counts = _frame_counts(state, len(candidates))
    state["counts"] = counts
    write_json(state_path, state)
    register_generated(root, state_path)
    reviewed = _reviewed_ids(root)
    retained_ids = {item["frame_id"] for item in representatives}
    last = state["frames"][-1] if state["frames"] else None
    return {
        **counts,
        "reviewed": len(retained_ids & reviewed),
        "inserted_output_images": _inserted_count(root),
        "inserted": _inserted_count(root),
        "duplicate_candidates": counts["baseline_duplicates"],
        "complete": state["complete"],
        "remaining": max(0, len(candidates) - len(state["frames"])),
        "last_requested_time": last["requested"] if last else None,
        "last_decoded_time": last["timestamp"] if last else None,
        "interval": settings["interval"],
        "requested_end": settings["end"],
    }


def refine_frames(root, window_start, window_end, interval, reason):
    """Decode bounded 10s/5s interior recovery candidates from the prepared video."""

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Refinement requires a nonempty string reason from visual inspection")
    reason_text = reason.strip()
    plan_refinement(window_start, window_end, interval, ())
    start_value = float(window_start)
    end_value = float(window_end)
    level = int(float(interval))

    root = Path(root).resolve()
    job, media = _require_unchanged_prepared_source(root)
    state_path = root / "work" / "frames-state.json"
    if not state_path.is_file():
        raise ValueError("Extract baseline frames before refining a window")
    state = read_json(state_path)
    if not isinstance(state, dict) or not isinstance(state.get("frames"), list):
        raise ValueError("work/frames-state.json must contain a frames array")
    raw_settings = state.get("settings")
    if not isinstance(raw_settings, dict) or raw_settings.get("interval") is None:
        raise ValueError("work/frames-state.json lacks valid baseline sampling settings")
    baseline_interval = frame_interval(raw_settings.get("interval"))
    if state.get("interval") is not None:
        if abs(frame_interval(state.get("interval")) - baseline_interval) > 1e-6:
            raise ValueError("work/frames-state.json has inconsistent baseline interval metadata")
    expected_settings = frame_configuration(job, interval=baseline_interval)
    if state.get("key") != json_digest(expected_settings):
        raise ValueError("Sampling settings or source changed; reset frames before refining")

    baseline_candidates = list(
        sample_times(expected_settings["start"], expected_settings["end"], baseline_interval)
    )
    frames = state["frames"]
    if len(frames) < 2:
        raise ValueError("Extract at least two adjacent baseline frames before refining a window")
    if len(frames) > len(baseline_candidates):
        raise ValueError("Frame state has more candidates than the current schedule; reset it")
    for idx, frame in enumerate(frames):
        if not isinstance(frame, dict) or not frame.get("frame_id"):
            raise ValueError(f"Baseline frame {idx} in frames-state.json is invalid")
        req = _finite_number(frame.get("requested"), f"frames[{idx}].requested")
        if abs(req - baseline_candidates[idx]) > 1e-6:
            raise ValueError(f"Baseline frame {idx} does not match the configured schedule")
    window_index = next(
        (
            idx
            for idx in range(len(frames) - 1)
            if abs(float(frames[idx]["requested"]) - start_value) <= 1e-6
            and abs(float(frames[idx + 1]["requested"]) - end_value) <= 1e-6
        ),
        None,
    )
    if window_index is None:
        raise ValueError("Refinement endpoints must match two adjacent extracted baseline times")
    actual_start = float(frames[window_index]["requested"])
    actual_end = float(frames[window_index + 1]["requested"])

    refinements = state.get("refinements")
    refinements = [] if refinements is None else refinements
    if not isinstance(refinements, list):
        raise ValueError("work/frames-state.json refinements must be a list")
    for idx, item in enumerate(refinements):
        if not isinstance(item, dict) or not item.get("frame_id"):
            raise ValueError(f"Refinement record {idx} in frames-state.json is invalid")
        for field in ("requested", "interval", "window_start", "window_end"):
            _finite_number(item.get(field), f"refinements[{idx}].{field}")
    adaptive_windows = state.get("adaptive_windows")
    adaptive_windows = {} if adaptive_windows is None else adaptive_windows
    if not isinstance(adaptive_windows, dict):
        raise ValueError("work/frames-state.json adaptive_windows must be an object")
    wkey = f"{actual_start:.6f}:{actual_end:.6f}"
    window_entry = adaptive_windows.get(wkey)
    if window_entry is not None and not isinstance(window_entry, dict):
        raise ValueError("Adaptive window entry must be an object")
    completed_raw = (window_entry or {}).get("completed_intervals", [])
    if not isinstance(completed_raw, list):
        raise ValueError("Adaptive window completed_intervals must be a list")
    validate_refinement_level(completed_raw, level)

    prior_window = [item for item in refinements if _same_window(item, actual_start, actual_end)]
    other_requested = [
        actual_start,
        actual_end,
        *(float(r["requested"]) for r in prior_window if float(r["interval"]) > level),
    ]
    level_schedule = list(plan_refinement(actual_start, actual_end, level, other_requested))
    existing_level = [r for r in prior_window if float(r["interval"]) == level]
    already_completed = any(float(v) == level for v in completed_raw)
    reused_times = [float(r["requested"]) for r in existing_level]
    newly_decoded_times, new_unique_ids = [], []
    invalidated = False
    if already_completed:
        if any(
            not any(abs(float(r["requested"]) - pt) <= 1e-6 for r in existing_level)
            for pt in level_schedule
        ):
            raise ValueError("Refinement state is inconsistent: completed level lacks records")
        representatives = _seed_representatives(state, root)
        completed_intervals = list(completed_raw)
        level_records = existing_level
    else:
        all_existing = [actual_start, actual_end, *(float(r["requested"]) for r in prior_window)]
        remaining_schedule = list(plan_refinement(actual_start, actual_end, level, all_existing))
        state["refinements"] = refinements
        state["adaptive_windows"] = adaptive_windows
        representatives = _seed_representatives(state, root)
        (root / "work" / "frames").mkdir(parents=True, exist_ok=True)
        for requested in remaining_schedule:
            image, decoded = frame_at(media, requested)
            representative = _match_representative(root, image, representatives)
            duplicate = representative is not None
            occurrence_entry = {
                "requested": requested,
                "decoded": decoded,
                "duplicate": duplicate,
                "source": "refinement",
                "interval": float(level),
                "window_start": actual_start,
                "window_end": actual_end,
            }
            if duplicate:
                identifier, path = representative["frame_id"], representative["path"]
                occurrence_entry["path"] = path
                occurrence = len(representative.setdefault("occurrences", []))
                representative["occurrences"].append(occurrence_entry)
            else:
                identifier = _next_screen_id(representatives)
                path = f"work/frames/{identifier}.png"
                image.save(root / path)
                register_generated(root, root / path)
                occurrence_entry["path"] = path
                occurrence = 0
                representatives.append({
                    "frame_id": identifier,
                    "path": path,
                    "origin": "refinement",
                    "first_requested": requested,
                    "first_decoded": decoded,
                    "coarse": _coarse(image),
                    "occurrences": [occurrence_entry],
                })
                new_unique_ids.append(identifier)
                invalidated = _invalidate_output_bindings(root) or invalidated
            refinements.append({
                "index": len(refinements),
                "window_start": actual_start,
                "window_end": actual_end,
                "interval": float(level),
                "requested": requested,
                "timestamp": decoded,
                "decoded": decoded,
                "frame_id": identifier,
                "path": path,
                "duplicate": duplicate,
                "occurrence": occurrence,
                "reason": reason_text,
            })
            newly_decoded_times.append(requested)
            state["counts"] = _frame_counts(state, len(baseline_candidates))
            write_json(state_path, state)
            register_generated(root, state_path)
        completed_intervals = [*completed_raw, level]
        history = [*((window_entry or {}).get("history") or [])]
        history.append({
            "interval": float(level),
            "reason": reason_text,
            "planned_requested": level_schedule,
        })
        adaptive_windows[wkey] = {
            "window_start": actual_start,
            "window_end": actual_end,
            "baseline_index": window_index,
            "completed_intervals": completed_intervals,
            "history": history,
        }
        state["counts"] = _frame_counts(state, len(baseline_candidates))
        write_json(state_path, state)
        register_generated(root, state_path)
        level_records = [
            r for r in refinements
            if _same_window(r, actual_start, actual_end) and float(r["interval"]) == level
        ]

    counts = _frame_counts(state, len(baseline_candidates))
    reviewed = _reviewed_ids(root)
    retained_ids = {item["frame_id"] for item in representatives}
    return {
        "status": "reused" if already_completed else "refined",
        "reused": already_completed,
        "window_start": actual_start,
        "window_end": actual_end,
        "interval": float(level),
        "baseline_interval": baseline_interval,
        "reason": reason_text,
        "completed_intervals": completed_intervals,
        "level_complete": True,
        "planned_times": level_schedule,
        "newly_decoded_times": newly_decoded_times,
        "reused_times": reused_times,
        "new_unique_frame_ids": new_unique_ids,
        "records": level_records,
        "invalidated_output_bindings": invalidated,
        "window_candidates": len(level_schedule),
        **counts,
        "duplicates": counts["duplicate_candidates"],
        "reviewed": len(retained_ids & reviewed),
        "pending_review": len(retained_ids - reviewed),
        "note": (
            "Direct-video refinement extracted/reused bounded interior candidates only. "
            "Extraction does not certify visual clarity or mark any frame reviewed."
        ),
    }


def reset_frames(root):
    """Explicitly discard frame evidence after a sampling/source change."""

    root = Path(root).resolve()
    job = load_job(root)
    stale = [
        relative
        for relative in job.get("generated", [])
        if relative.startswith("work/frames/") or relative == "work/frames-state.json"
    ]
    for relative in stale:
        path = root / relative
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
        unregister_generated(root, path)
    for extra in (root / "work" / "frames-state.json", root / "work" / "screen.json"):
        if extra.is_file():
            extra.unlink()
            unregister_generated(root, extra)
    job = load_job(root)
    job.get("stages", {}).pop("frames", None)
    job.get("stages", {}).pop("screen", None)
    job["preview"] = None
    save_job(root, job)
    return {"reset": True, "removed": len(stale)}


def extract_audio(root, interval=None, force=False):
    """Cut the selected interval to mono 16 kHz PCM before ASR processing."""

    root = Path(root).resolve()
    media = prepare_source(root)
    if not media.get("has_audio"):
        raise ValueError("Source has no audio stream; review screen-only content explicitly")
    start, end = (
        (float(interval[0]), float(interval[1])) if interval else (media["start"], media["end"])
    )
    start, end = _finite_range(start, end, float(media["duration"]))
    if end <= start:
        raise ValueError("Audio interval must have positive duration")
    destination = root / "work" / "audio" / f"selected-{start:.3f}-{end:.3f}.wav"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        if destination.stat().st_size > 44:
            register_generated(root, destination)
            return {
                "path": str(destination),
                "start": start,
                "end": end,
                "reused": True,
            }
        destination.unlink()
    arguments = [
        tool_path("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        media["path"],
        "-t",
        f"{end - start:.3f}",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(destination),
    ]
    try:
        run_command(arguments)
    except Exception as error:
        destination.unlink(missing_ok=True)
        raise _failure([tool_path("ffmpeg"), str(media["path"])], error) from error
    if not destination.is_file() or destination.stat().st_size <= 44:
        raise ValueError("FFmpeg produced no usable audio for the selected interval")
    register_generated(root, destination)
    return {
        "path": str(destination),
        "start": start,
        "end": end,
        "reused": False,
        "sha256": file_digest(destination),
    }


def extract_audio_chunk(root, media, start, end, destination, force=False):
    """Write one bounded audio chunk for checkpointed ASR."""

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force and destination.stat().st_size > 44:
        return destination
    arguments = [
        tool_path("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{float(start):.3f}",
        "-i",
        media["path"],
        "-t",
        f"{float(end) - float(start):.3f}",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(destination),
    ]
    try:
        run_command(arguments)
    except Exception as error:
        destination.unlink(missing_ok=True)
        raise _failure([tool_path("ffmpeg"), str(media["path"])], error) from error
    if not destination.is_file() or destination.stat().st_size <= 44:
        raise ValueError(f"FFmpeg produced no usable audio chunk at {start:.3f}s")
    register_generated(root, destination)
    return destination
