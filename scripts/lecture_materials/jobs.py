"""Persistent job records, generated-artifact ownership, and cleanup.

The job directory is the durable source of truth.  ``job.json`` is an ordinary
UTF-8 JSON document written atomically.  Nothing here decides that extracted
content is correct; it only records what exists, what this job generated, and
which operations the user actually confirmed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time
from datetime import datetime, timezone


SCHEMA_VERSION = 1


class SourceChangedError(ValueError):
    """The media at the recorded source path no longer matches the job."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, document):
    """Atomically write ordinary UTF-8 JSON with a trailing newline."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # Windows virus scanners and sibling readers can briefly hold the target.
    # Retry the atomic replace a few times, then surface the real error.
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (2**attempt))


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_digest(document):
    encoded = json.dumps(document, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_fingerprint(path):
    """Return the identity used to bind derivatives to one local file."""

    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Source is not a regular file: {path}")
    info = path.stat()
    return {
        "path": str(path),
        "sha256": file_digest(path),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
    }


def _is_local_source(source):
    return not str(source).startswith(("https://", "http://"))


def create_job(root, source, preferences):
    root = Path(root).resolve()
    if (root / "job.json").exists():
        raise FileExistsError("Job already exists; reuse it instead of overwriting")
    if not isinstance(preferences, dict):
        raise ValueError("preferences must be a JSON object")
    if _is_local_source(source):
        source_path = Path(source).resolve(strict=True)
        if not source_path.is_file():
            raise ValueError(f"Local source is not a regular file: {source_path}")
        source = str(source_path)
        if Path(source).is_relative_to(root):
            raise ValueError("The original source must be outside the generated job directory")
        fingerprint = source_fingerprint(source_path)
    else:
        fingerprint = None
    root.mkdir(parents=True, exist_ok=True)
    (root / "work").mkdir(exist_ok=True)
    (root / "outputs").mkdir(exist_ok=True)
    job = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "source_fingerprint": fingerprint,
        "preferences": preferences,
        "generated": [],
        "attachments": [],
        "stages": {},
        "outputs": {},
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "preview": None,
        "delivered": False,
        "cleanup": None,
    }
    save_job(root, job)
    return job


def load_job(root):
    path = Path(root) / "job.json"
    if not path.is_file():
        raise FileNotFoundError(f"No job.json in {Path(root).resolve()}; run init first")
    job = read_json(path)
    if not isinstance(job, dict) or job.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported job schema version")
    job.setdefault("generated", [])
    job.setdefault("attachments", [])
    job.setdefault("stages", {})
    job.setdefault("outputs", {})
    job.setdefault("preview", None)
    job.setdefault("delivered", False)
    job.setdefault("cleanup", None)
    return job


def save_job(root, job):
    if not isinstance(job, dict):
        raise ValueError("Job record must be an object")
    job["schema_version"] = SCHEMA_VERSION
    job["updated_at"] = utc_now()
    write_json(Path(root) / "job.json", job)


def set_stage(root, name, status, **details):
    """Record a durable per-stage status; never implies semantic review."""

    if not isinstance(name, str) or not name.strip():
        raise ValueError("Stage names must be nonempty strings")
    job = load_job(root)
    stage = {
        "status": str(status),
        "updated_at": utc_now(),
    }
    stage.update(details)
    job.setdefault("stages", {})[name] = stage
    save_job(root, job)
    return stage


def get_stage(root, name, default=None):
    return load_job(root).get("stages", {}).get(name, default)


def add_attachment(root, path, role):
    """Record an original user file and its role without copying or editing it."""

    root = Path(root).resolve()
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Attachment is not a regular file: {path}")
    if Path(path).is_relative_to(root):
        raise ValueError("Original attachments must stay outside the generated job directory")
    if not isinstance(role, str) or not role.strip():
        raise ValueError("Attachment role must be a nonempty string")
    job = load_job(root)
    item = {
        "path": str(path),
        "role": role.strip(),
        "sha256": file_digest(path),
        "size": path.stat().st_size,
    }
    existing = {
        (entry.get("path"), entry.get("role"))
        for entry in job["attachments"]
        if isinstance(entry, dict)
    }
    if (item["path"], item["role"]) not in existing:
        job["attachments"].append(item)
        save_job(root, job)
    return item


def checked_path(root, relative, area):
    """Resolve one managed relative path with containment and link checks."""

    root = Path(root).absolute()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Managed paths must be relative and cannot escape the job")
    if relative.parts[0] != area:
        raise ValueError(f"Expected a path inside {area}")
    candidate = root / relative
    for ancestor in [candidate, *candidate.parents]:
        if ancestor.exists() or ancestor.is_symlink():
            flags = ancestor.lstat()
            if ancestor.is_symlink() or getattr(
                flags, "st_file_attributes", 0
            ) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Managed artifacts cannot traverse symlinks or junctions")
        if ancestor == root:
            break
    if not candidate.resolve().is_relative_to((root / area).resolve()):
        raise ValueError("Artifact escapes the managed area")
    return candidate


def _relative_managed(root, path):
    """Return a job-relative path with Windows short/long forms canonicalized."""

    root = Path(root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_symlink():
        raise ValueError("Managed artifacts cannot be symlinks")
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as error:
        raise ValueError(f"Managed path cannot be resolved: {candidate}") from error
    try:
        return resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("Managed path escapes the job directory") from error


def register_generated(root, path):
    """Register a job-owned working artifact under ``work/`` for cleanup."""

    root = Path(root).resolve()
    relative = _relative_managed(root, path)
    checked_path(root, relative, "work")
    job = load_job(root)
    if relative.as_posix() not in job["generated"]:
        job["generated"].append(relative.as_posix())
        # A new artifact invalidates a previously displayed deletion scope.
        job["cleanup"] = None
        save_job(root, job)


def unregister_generated(root, path):
    root = Path(root).resolve()
    relative = _relative_managed(root, path).as_posix()
    job = load_job(root)
    if relative in job["generated"]:
        job["generated"] = [item for item in job["generated"] if item != relative]
        job["cleanup"] = None
        save_job(root, job)


def mark_delivered(root, outputs):
    """Record final PDF/PPTX deliverables and their current bytes."""

    root = Path(root).resolve()
    fingerprints = {}
    for output in outputs:
        candidate = Path(output)
        if not candidate.is_absolute():
            candidate = root / candidate
        relative = _relative_managed(root, candidate)
        candidate = checked_path(root, relative, "outputs")
        if candidate.suffix.lower() not in {".pdf", ".pptx"} or not candidate.is_file():
            raise ValueError("Delivery requires existing final PDF/PPTX files")
        fingerprints[relative.as_posix()] = file_digest(candidate)
    if not fingerprints:
        raise ValueError("No final deliverables supplied")
    job = load_job(root)
    job.update(
        delivered=True,
        delivered_at=utc_now(),
        outputs=fingerprints,
        cleanup=None,
    )
    job.setdefault("stages", {})["delivery"] = {
        "status": "delivered",
        "updated_at": utc_now(),
        "outputs": sorted(fingerprints),
    }
    save_job(root, job)
    return job


def _attachment_paths(job):
    paths = set()
    for entry in job.get("attachments", []):
        value = entry.get("path") if isinstance(entry, dict) else entry
        if isinstance(value, str) and value.strip():
            try:
                paths.add(Path(value).resolve())
            except OSError:
                pass
    return paths


def cleanup_targets(root, job):
    """Return existing job-generated files, with protection checks."""

    root = Path(root).resolve()
    protected = _attachment_paths(job)
    source = job.get("source")
    if isinstance(source, str) and _is_local_source(source):
        try:
            protected.add(Path(source).resolve())
        except OSError:
            pass
    final_outputs = {Path(item).resolve() for item in job.get("outputs", {})}
    targets = []
    seen = set()
    for relative in sorted(set(job.get("generated", []))):
        candidate = checked_path(root, relative, "work")
        resolved = candidate.resolve()
        if resolved in protected:
            raise ValueError("Original inputs cannot be cleanup targets")
        if resolved in final_outputs:
            raise ValueError("Final outputs cannot be cleanup targets")
        if not candidate.exists():
            continue
        if not candidate.is_file():
            raise ValueError(f"Generated artifact is not a regular file: {relative}")
        if resolved in seen:
            continue
        seen.add(resolved)
        targets.append({"path": relative, "sha256": file_digest(candidate)})
    return targets


def cleanup_protected_paths(root, job):
    """Describe what cleanup will deliberately preserve."""

    protected = [str((Path(root) / "job.json").resolve())]
    source = job.get("source")
    if isinstance(source, str) and _is_local_source(source):
        protected.append(str(Path(source).resolve()))
    protected.extend(sorted(str(path) for path in _attachment_paths(job)))
    protected.extend(
        str(checked_path(root, relative, "outputs").resolve())
        for relative in sorted(job.get("outputs", {}))
    )
    return protected


def cleanup_retained_control_record(root):
    """Disclose the small job-control record that survives cleanup."""

    return {
        "path": str((Path(root) / "job.json").resolve()),
        "retains": [
            "schema and timestamps",
            "original source path and fingerprint",
            "job preferences",
            "attachment paths, roles, and hashes",
            "final output paths and delivery hashes",
            "generated-work and cleanup receipt metadata",
            "prepared media/probe metadata and caption-track candidates",
        ],
        "does_not_retain": [
            "transcript text",
            "screen observations",
            "frame image content",
        ],
        "note": (
            "job.json is retained as the minimal control/delivery receipt. It does not contain "
            "deleted transcript or screen contents."
        ),
    }


def request_cleanup(root):
    """Non-destructive first step: show targets and return a bound token."""

    root = Path(root).resolve()
    job = load_job(root)
    if not job.get("delivered"):
        raise ValueError("Deliver and verify final files before offering cleanup")
    for relative, digest in job.get("outputs", {}).items():
        if file_digest(checked_path(root, relative, "outputs")) != digest:
            raise ValueError("Delivered file changed; verify delivery again")
    targets = cleanup_targets(root, job)
    if not targets:
        raise ValueError("This job has no remaining generated work files to delete")
    token = json_digest(
        {
            "root": str(root),
            "targets": targets,
            "outputs": job.get("outputs", {}),
        }
    )
    job["cleanup"] = {
        "state": "awaiting_confirmation",
        "token": token,
        "targets": targets,
        "requested_at": utc_now(),
    }
    save_job(root, job)
    delete = [str((root / item["path"]).resolve()) for item in targets]
    keep = cleanup_protected_paths(root, job)
    control_record = cleanup_retained_control_record(root)
    return {
        "state": "awaiting_confirmation",
        "confirmation_token": token,
        "delete": delete,
        "keep": keep,
        "delete_count": len(delete),
        "retained_control_record": control_record,
        "warning": (
            "The transcript is included where listed. This first choice is non-destructive. "
            "The job.json control record remains for delivery/cleanup identity and contains no "
            "deleted transcript or screen contents. Ask the user again and pass the token to "
            "cleanup-confirm before removing anything."
        ),
    }


def confirm_cleanup(root, token):
    """Apply only the previously displayed, still-identical target set."""

    root = Path(root).resolve()
    if not isinstance(token, str) or not token.strip():
        raise ValueError("A cleanup confirmation token is required")
    job = load_job(root)
    pending = job.get("cleanup")
    if (
        not isinstance(pending, dict)
        or pending.get("state") != "awaiting_confirmation"
        or pending.get("token") != token
    ):
        raise ValueError("No matching pending deletion confirmation")
    if pending.get("targets") is None:
        raise ValueError("The pending deletion plan is incomplete; request cleanup again")
    current = cleanup_targets(root, job)
    if current != pending["targets"]:
        raise ValueError("Work files changed; show a new cleanup plan and ask again")
    expected_token = json_digest(
        {
            "root": str(root),
            "targets": current,
            "outputs": job.get("outputs", {}),
        }
    )
    if token != expected_token:
        raise ValueError(
            "Cleanup confirmation is not bound to this job root and target set; request cleanup again"
        )
    for relative, digest in job.get("outputs", {}).items():
        if file_digest(checked_path(root, relative, "outputs")) != digest:
            raise ValueError("Final output changed; cleanup canceled")

    deleted = []
    failures = []
    for target in current:
        try:
            # Re-validate immediately before mutation, then re-check the bytes.
            candidate = checked_path(root, target["path"], "work")
            if not candidate.is_file() or file_digest(candidate) != target["sha256"]:
                raise ValueError("Target changed after the cleanup plan was displayed")
            candidate.unlink()
            deleted.append(target["path"])
        except Exception as error:  # report each concrete failure
            failures.append({"path": target["path"], "error": str(error)})

    deleted_set = set(deleted)
    job["generated"] = [
        item for item in job.get("generated", []) if item not in deleted_set
    ]
    # Do not sweep unknown directories.  Empty generated directories may remain;
    # only registered files are ever cleanup targets.
    if failures:
        remaining = [item["path"] for item in failures]
        job["cleanup"] = {
            "state": "partially_cleaned",
            "deleted_count": len(deleted),
            "failed_count": len(failures),
            "failures": failures,
            "remaining": remaining,
            "updated_at": utc_now(),
        }
    else:
        job["cleanup"] = {
            "state": "cleaned",
            "deleted_count": len(deleted),
            "deleted_at": utc_now(),
        }
        job["stages"] = {}
        job["preview"] = None
    save_job(root, job)
    return {
        "state": job["cleanup"]["state"],
        "deleted_count": len(deleted),
        "deleted": deleted,
        "failures": failures,
        "remaining_generated": job["generated"],
    }


def keep_work(root):
    """Retain generated work and cancel any pending deletion authority."""

    job = load_job(root)
    job["cleanup"] = {"state": "retained", "retained_at": utc_now()}
    save_job(root, job)
    return job["cleanup"]


def cleanup_status(root):
    job = load_job(root)
    return {
        "delivered": bool(job.get("delivered")),
        "cleanup": job.get("cleanup"),
        "generated_count": len(job.get("generated", [])),
        "outputs": sorted(job.get("outputs", {})),
    }
