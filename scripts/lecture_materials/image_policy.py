"""Image-language intake, label validation, and durable search budgeting.

Two independent concerns live here:

* the requested *label language* of inserted images (``image_language``), which
  is separate from the existing ``image_mode`` web/user source policy;
* a durable ledger for the host's real image searches and their 600-second
  active search budget.

Search time is measured over explicit intervals, not over the whole lifetime of
a request.  :func:`start_search` opens an interval, and finishing any recorded
attempt closes it.  Polling only moves the clock while an interval is open, so
authoring, user waiting, and other idle time are never charged.  A request whose
interval is closed remains resumable with its accumulated budget intact.

The host performs external searches and actual semantic inspection.  Nothing
here contacts a provider, invents search results, or turns a completed query
into relevance/language approval.
"""

from __future__ import annotations

import copy
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from .jobs import load_job, read_json, register_generated, save_job, write_json

DEFAULT_SEARCH_BUDGET_SECONDS = 600.0
IMAGE_LANGUAGES = ("ar", "en", "mixed")
OBSERVED_IMAGE_LANGUAGES = ("ar", "en", "mixed", "none", "unreadable")
SEARCH_ACTIONS = ("continue_search", "use_arabic", "use_english", "no_images")
SEARCH_STATE_FILE = "image-search.json"

_TEXT_IMAGE_MODES = {
    "text",
    "none",
    "text_only",
    "text-only",
    "no_images",
    "no-images",
}
_LIVE_SEARCH_STATUSES = {"searching", "awaiting_decision"}
_TERMINAL_SEARCH_STATUSES = {"found", "no_images"}
_VALUE_ALIASES = {
    "ar": "ar",
    "arabic": "ar",
    "ar-ar": "ar",
    "العربية": "ar",
    "عربي": "ar",
    "en": "en",
    "english": "en",
    "en-en": "en",
    "mixed": "mixed",
    "bilingual": "mixed",
    "both": "mixed",
    "multilingual": "mixed",
}
_SEARCH_QUESTION = {
    "language": "ar",
    "reason": "search_budget_exhausted",
    "text": (
        "لم يُعثر على صورة مناسبة خلال مدة البحث. هل تريد متابعة البحث، "
        "أم استخدام صور بالعربية، أم استخدام صور بالإنجليزية، أم عدم استخدام صور؟"
    ),
    "choices": [
        {"value": "continue_search", "label": "متابعة البحث"},
        {"value": "use_arabic", "label": "استخدام صور عربية"},
        {"value": "use_english", "label": "استخدام صور إنجليزية"},
        {"value": "no_images", "label": "عدم استخدام الصور"},
    ],
}


# ---------------------------------------------------------------------------
# Language preference helpers
# ---------------------------------------------------------------------------


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _normalized_language(value, field_name="image_language"):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be ar, en, or mixed")
    token = value.strip().lower()
    alias = _VALUE_ALIASES.get(token)
    if alias is not None:
        return alias
    words = {part for part in re.split(r"[^a-z]+", token) if part}
    if "mixed" in words or "bilingual" in words or "multilingual" in words:
        return "mixed"
    has_arabic = "ar" in words or "arabic" in words
    has_english = "en" in words or "english" in words
    if has_arabic and has_english:
        return "mixed"
    if has_arabic:
        return "ar"
    if has_english:
        return "en"
    raise ValueError(f"{field_name} must be ar, en, or mixed, not {value!r}")


def _text_only(preferences):
    raw = preferences.get("image_mode")
    if raw is None:
        return False
    if not isinstance(raw, str):
        raise ValueError("preferences.image_mode must be a string")
    return raw.strip().lower() in _TEXT_IMAGE_MODES


def _document_language(preferences):
    raw = None
    for key in ("output_language", "language", "document_language"):
        value = preferences.get(key)
        if _nonempty(value):
            raw = value
            break
    if raw is None:
        return None
    token = raw.strip().lower()
    if token in {"mixed", "bilingual", "both", "multilingual"}:
        return "mixed"
    words = {part for part in re.split(r"[^a-z]+", token) if part}
    if "mixed" in words or "bilingual" in words or "multilingual" in words:
        return "mixed"
    has_arabic = "ar" in words or "arabic" in words
    has_english = "en" in words or "english" in words
    if has_arabic and has_english:
        return "mixed"
    if has_arabic:
        return "ar"
    if has_english:
        return "en"
    return None


def _image_language_question(preferences):
    document = _document_language(preferences)
    direction = str(preferences.get("direction") or "").strip().lower()
    arabic = direction == "rtl" or (direction != "ltr" and document != "en")
    if arabic:
        return {
            "id": "image_language",
            "question": (
                "المستند يجمع بين العربية والإنجليزية. ما لغة العناوين داخل الصور: "
                "العربية، الإنجليزية، أم العربية والإنجليزية معًا؟"
            ),
            "options": [
                {"value": "ar", "label": "العربية"},
                {"value": "en", "label": "الإنجليزية"},
                {"value": "mixed", "label": "العربية والإنجليزية معًا"},
            ],
        }
    return {
        "id": "image_language",
        "question": (
            "This document uses more than one language. Should the labels inside "
            "images be Arabic, English, or both (mixed)?"
        ),
        "options": [
            {"value": "ar", "label": "Arabic"},
            {"value": "en", "label": "English"},
            {"value": "mixed", "label": "Both Arabic and English"},
        ],
    }


def _image_mode_question(preferences):
    document = _document_language(preferences)
    direction = str(preferences.get("direction") or "").strip().lower()
    arabic = direction == "rtl" or (direction != "ltr" and document != "en")
    if arabic:
        return {
            "id": "image_mode",
            "question": (
                "هل تريد تضمين صور توضيحية في المادة؟ اختر مصدر الصور أو عدم "
                "استخدام الصور."
            ),
            "options": [
                {"value": "web", "label": "نعم، صور من الويب"},
                {"value": "user", "label": "نعم، سأضيف صورًا من عندي"},
                {"value": "mixed", "label": "نعم، من الويب ومن صوري"},
                {"value": "text", "label": "لا، بدون صور"},
            ],
        }
    return {
        "id": "image_mode",
        "question": (
            "Do you want explanatory images in the materials? Choose a picture "
            "source, or choose no images."
        ),
        "options": [
            {"value": "web", "label": "Yes, search the web"},
            {"value": "user", "label": "Yes, I will supply images"},
            {"value": "mixed", "label": "Yes, web and my own images"},
            {"value": "text", "label": "No images"},
        ],
    }


def intake_requirements(preferences):
    """Normalise image-language intake without mutating the caller's object.

    ``normalized`` is a deep copy.  The decision is staged: while ``image_mode``
    is absent, only the picture-presence/source question is returned and no
    image-language default or question is produced.  Once a source is supplied,
    a pure Arabic/English document defaults the image language to the document
    language, while a mixed-language document with no recorded choice is
    reported as missing together with one user-facing language question.
    Text-only jobs need no language answer.
    """

    if not isinstance(preferences, dict):
        raise ValueError("preferences must be an object")
    normalized = copy.deepcopy(preferences)
    raw = preferences.get("image_language")
    if raw is not None and not isinstance(raw, str):
        raise ValueError("preferences.image_language must be ar, en, or mixed")
    supplied = _nonempty(raw)
    if supplied:
        normalized["image_language"] = _normalized_language(raw)
    elif "image_language" in normalized:
        normalized.pop("image_language")

    text_only = _text_only(preferences)
    if not _nonempty(preferences.get("image_mode")):
        return {
            "normalized": normalized,
            "missing": ["image_mode"],
            "questions": [_image_mode_question(preferences)],
        }
    if text_only:
        return {"normalized": normalized, "missing": [], "questions": []}
    if supplied:
        return {"normalized": normalized, "missing": [], "questions": []}

    document = _document_language(preferences)
    if document in {"ar", "en"}:
        normalized["image_language"] = document
        return {"normalized": normalized, "missing": [], "questions": []}

    return {
        "normalized": normalized,
        "missing": ["image_language"],
        "questions": [_image_language_question(preferences)],
    }


def validate_asset_language(preferences, asset):
    """Check an asset's actual label review against the requested language.

    Jobs without ``image_language`` keep their legacy/source-policy contract and
    are not language-gated.  Text-only jobs cannot use images at all.  For a
    language-aware job the review must be explicitly inspected, readable, and
    an exact match for the requested labels.  A caption, title, or search query
    is never accepted as label evidence.
    """

    if not isinstance(preferences, dict):
        raise ValueError("preferences must be an object")
    raw = preferences.get("image_language")
    if raw is not None and not isinstance(raw, str):
        raise ValueError("preferences.image_language must be ar, en, or mixed")
    if not _nonempty(raw):
        return {
            "required": False,
            "requested": None,
            "observed": None,
            "matched": None,
            "reason": "No image_language preference; legacy/source-policy validation only.",
        }
    requested = _normalized_language(raw)
    if _text_only(preferences):
        return {
            "required": False,
            "requested": requested,
            "observed": None,
            "matched": None,
            "reason": "Text-only job; inserted images are not permitted.",
        }
    if not isinstance(asset, dict):
        raise ValueError("Image asset must be an object")
    identifier = asset.get("id")
    label = identifier if _nonempty(identifier) else "asset"
    review = asset.get("language_review")
    if not isinstance(review, dict):
        raise ValueError(
            f"Image {label} lacks a language_review record; inspect the actual image "
            "labels and record ar, en, mixed, none, or unreadable"
        )
    if review.get("inspected") is not True:
        raise ValueError(f"Image {label} language labels were not actually inspected")
    observed = review.get("language")
    if not isinstance(observed, str) or observed.strip().lower() not in OBSERVED_IMAGE_LANGUAGES:
        raise ValueError(
            f"Image {label} language_review.language must be one of "
            f"{', '.join(OBSERVED_IMAGE_LANGUAGES)}"
        )
    observed = observed.strip().lower()
    if not _nonempty(review.get("reason")):
        raise ValueError(
            f"Image {label} language_review needs a concrete observation of the actual labels"
        )
    if observed in {"none", "unreadable"}:
        raise ValueError(
            f"Image {label} labels are {observed}; ask before substituting an unlabeled "
            f"or unreadable asset for requested {requested} labels"
        )
    if observed != requested:
        raise ValueError(
            f"Image {label} has {observed} labels but the job requested {requested}; "
            "a caption or search title does not change the labels in the pixels"
        )
    return {
        "required": True,
        "requested": requested,
        "observed": observed,
        "matched": True,
        "reason": review["reason"].strip(),
    }


# ---------------------------------------------------------------------------
# Durable search ledger
# ---------------------------------------------------------------------------


def _utc_now():
    """Single clock seam; tests may patch this to simulate external boundaries."""

    return datetime.now(timezone.utc)


def _timestamp(value, field_name):
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError(f"{field_name} must be a finite timestamp")
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as error:
            raise ValueError(f"{field_name} is not an ISO timestamp: {value!r}") from error
    else:
        raise ValueError(f"{field_name} must be an ISO timestamp")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def _number(value, field_name, minimum=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{field_name} must be finite and at least {minimum}")
    return number


def _state_path(root):
    return Path(root) / "work" / SEARCH_STATE_FILE


def _load_state(root):
    path = _state_path(root)
    if not path.exists():
        return {"schema_version": 1, "requests": []}
    document = read_json(path)
    if not isinstance(document, dict) or not isinstance(document.get("requests"), list):
        raise ValueError("work/image-search.json must contain a requests array")
    for request in document["requests"]:
        if not isinstance(request, dict):
            raise ValueError("Every image-search request must be an object")
        if not _nonempty(request.get("request_id")):
            raise ValueError("Every image-search request needs a request_id")
        _default_legacy_interval(request)
    return document


def _default_legacy_interval(request):
    """Make old ledgers safe without erasing their recorded time or history.

    Earlier ledgers had no interval marker, so a ``searching`` entry could be
    either genuinely running or idle after an attempt.  Default to closed and
    require a fresh explicit :func:`start_search` before charging more time.
    """

    if "search_interval_open" not in request:
        request["search_interval_open"] = False
        request["interval_started_at"] = None
        request["interval_ended_at"] = None
        request["interval_accounted_seconds"] = 0.0
    if request.get("status") != "searching":
        request["search_interval_open"] = False


def _save_state(root, document):
    document["schema_version"] = 1
    document["updated_at"] = _iso(_utc_now())
    path = _state_path(root).resolve()
    write_json(path, document)
    register_generated(Path(root).resolve(), path)


def _preferences(root):
    job = load_job(root)
    preferences = job.get("preferences") or {}
    if not isinstance(preferences, dict):
        raise ValueError("job preferences must be an object")
    return preferences


def _target_language(preferences, required=False):
    normalized = intake_requirements(preferences)["normalized"]
    language = normalized.get("image_language")
    if _nonempty(language):
        return language
    if required:
        raise ValueError(
            "No image_language preference is available for image search; collect the "
            "Arabic/English/mixed choice before searching"
        )
    return None


def _find_request(document, request_id):
    if not _nonempty(request_id):
        raise ValueError("request_id must be a nonempty string")
    for request in document["requests"]:
        if request.get("request_id") == request_id:
            return request
    raise ValueError(f"Unknown image-search request: {request_id}")


def _request_label(request):
    return str(request.get("request_id") or "request")


def _current_round(request):
    rounds = request.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        raise ValueError(f"Image-search request {_request_label(request)} has no round record")
    return rounds[-1]


def _close_round(request, moment, status):
    rounds = request.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        return
    current = rounds[-1]
    if current.get("status") == "searching":
        current["status"] = status
        current["ended_at"] = _iso(moment)
        current["elapsed_seconds"] = round(float(request.get("elapsed_seconds", 0.0)), 6)


def _interval_open(request):
    return request.get("search_interval_open") is True


def _open_interval(request, moment):
    """Open one explicit active-search interval; never restart an open one."""

    if _interval_open(request):
        return False
    request["search_interval_open"] = True
    request["interval_started_at"] = _iso(moment)
    request["interval_ended_at"] = None
    request["interval_accounted_seconds"] = 0.0
    request["last_accounted_at"] = _iso(moment)
    return True


def _close_interval(request, moment):
    """Close the active interval so polling cannot keep charging idle time."""

    if not _interval_open(request):
        return False
    request["search_interval_open"] = False
    request["interval_ended_at"] = _iso(moment)
    request["last_accounted_at"] = _iso(moment)
    request["interval_accounted_seconds"] = 0.0
    return True


def _add_elapsed(request, seconds):
    seconds = float(seconds)
    if seconds <= 0:
        return 0.0
    elapsed = float(request.get("elapsed_seconds", 0.0)) + seconds
    total = float(request.get("total_elapsed_seconds", 0.0)) + seconds
    request["elapsed_seconds"] = round(elapsed, 6)
    request["total_elapsed_seconds"] = round(total, 6)
    current = _current_round(request)
    current["elapsed_seconds"] = request["elapsed_seconds"]
    return seconds


def _accrue(request, moment):
    """Charge wall time only while an explicitly opened interval is active."""

    if request.get("status") != "searching" or not _interval_open(request):
        return 0.0
    last_value = request.get("last_accounted_at")
    if _nonempty(last_value):
        last = _timestamp(last_value, "last_accounted_at")
    else:
        fallback = (
            request.get("interval_started_at")
            or request.get("round_started_at")
            or request.get("created_at")
        )
        last = _timestamp(fallback, "interval start")
    seconds = max(0.0, (moment - last).total_seconds())
    request["last_accounted_at"] = _iso(moment)
    if seconds <= 0:
        return 0.0
    already = float(request.get("interval_accounted_seconds", 0.0))
    request["interval_accounted_seconds"] = round(already + seconds, 6)
    return _add_elapsed(request, seconds)


def _charge_measured(request, seconds, moment, started=None, finished=None):
    """Apply a complete measured interval without double-charging polled time.

    ``seconds`` is the complete measured duration when it is known (an explicit
    elapsed value or a started_at/finished_at pair).  Only the part of that
    measured interval that falls after the last charged checkpoint is new.  If
    the measured interval begins after that checkpoint, subtracting the entire
    charged total would erase the untouched gap, so the overlap is clipped to
    the actual intersection of the measured interval and the charged interval.
    """

    measured = _number(seconds, "elapsed_seconds")
    if not _interval_open(request):
        return _add_elapsed(request, measured)
    last_value = request.get("last_accounted_at") or request.get("interval_started_at")
    last = _timestamp(last_value, "last_accounted_at")
    if started is not None:
        interval_start = _timestamp(
            request.get("interval_started_at") or last_value, "interval_started_at"
        )
        measured_start = max(started, interval_start)
        measured_end = finished if finished is not None else moment
        overlap_end = min(measured_end, last)
        charged_overlap = max(0.0, (overlap_end - measured_start).total_seconds())
    else:
        charged_overlap = float(request.get("interval_accounted_seconds", 0.0))
    additional = max(0.0, measured - charged_overlap)
    if additional <= 0:
        return 0.0
    request["interval_accounted_seconds"] = round(
        float(request.get("interval_accounted_seconds", 0.0)) + additional, 6
    )
    checkpoint = finished if finished is not None else moment
    if checkpoint > last:
        request["last_accounted_at"] = _iso(checkpoint)
    return _add_elapsed(request, additional)


def _charge_tail(request, seconds, endpoint):
    """Charge an uncharged tail from a partial timestamp.

    A lone ``started_at`` or ``finished_at`` does not describe a complete
    measured interval.  It only identifies an endpoint, so the uncharged wall
    time since the last checkpoint is added directly rather than reconciled
    against the whole interval total.
    """

    seconds = _number(seconds, "tail_seconds")
    if not _interval_open(request):
        return _add_elapsed(request, seconds)
    if seconds <= 0:
        return 0.0
    last_value = request.get("last_accounted_at") or request.get("interval_started_at")
    last = _timestamp(last_value, "last_accounted_at")
    request["interval_accounted_seconds"] = round(
        float(request.get("interval_accounted_seconds", 0.0)) + seconds, 6
    )
    if endpoint > last:
        request["last_accounted_at"] = _iso(endpoint)
    return _add_elapsed(request, seconds)


def _mark_awaiting_decision(request, moment):
    if request.get("status") != "searching":
        return
    _close_interval(request, moment)
    request["status"] = "awaiting_decision"
    request["question"] = copy.deepcopy(_SEARCH_QUESTION)
    request["awaiting_since"] = _iso(moment)
    request["budget_exhausted_at"] = _iso(moment)
    request["updated_at"] = _iso(moment)
    _close_round(request, moment, "exhausted")


def _mark_found(request, moment):
    _close_interval(request, moment)
    request["status"] = "found"
    request["question"] = None
    request["awaiting_since"] = None
    request["completed_at"] = _iso(moment)
    request["updated_at"] = _iso(moment)
    request["search_complete"] = True
    _close_round(request, moment, "found")


def _open_round(request, moment):
    _close_interval(request, moment)
    rounds = request.setdefault("rounds", [])
    number = int(request.get("round") or len(rounds) or 0) + 1
    request["round"] = number
    request["status"] = "searching"
    request["elapsed_seconds"] = 0.0
    request["round_started_at"] = _iso(moment)
    request["last_accounted_at"] = _iso(moment)
    request["search_interval_open"] = False
    request["interval_started_at"] = None
    request["interval_ended_at"] = None
    request["interval_accounted_seconds"] = 0.0
    request["question"] = None
    request["awaiting_since"] = None
    request["updated_at"] = _iso(moment)
    rounds.append(
        {
            "round": number,
            "started_at": _iso(moment),
            "elapsed_seconds": 0.0,
            "status": "searching",
        }
    )


def _live_preconditions(request, preferences):
    status = request.get("status")
    live = status in _LIVE_SEARCH_STATUSES
    text_only = _text_only(preferences)
    target = _target_language(preferences)
    language_stale = bool(
        live and target is not None and target != request.get("language")
    )
    missing_language = bool(live and target is None)
    stale = language_stale or missing_language
    return {
        "text_only": text_only,
        "target": target,
        "stale_language": stale,
        "requires_reconciliation": live and (stale or text_only),
    }


def _allowed_actions(request, stale, text_only):
    status = request.get("status")
    if status == "awaiting_decision":
        if text_only:
            return ["no_images"]
        if stale:
            return ["use_arabic", "use_english", "no_images"]
        return ["continue_search", "use_arabic", "use_english", "no_images"]
    if status == "searching" and (stale or text_only):
        if text_only:
            return ["no_images"]
        return ["use_arabic", "use_english", "no_images"]
    return []


def _summary(request, preferences):
    status = request.get("status")
    conditions = _live_preconditions(request, preferences)
    stale = conditions["stale_language"]
    text_only = conditions["text_only"]
    question = request.get("question")
    choices = question.get("choices") if isinstance(question, dict) else []
    budget = _request_budget(request)
    elapsed = float(request.get("elapsed_seconds", 0.0))
    decisions = request.get("decisions") or []
    interval_open = status == "searching" and _interval_open(request)
    return {
        "request_id": request.get("request_id"),
        "section_id": request.get("section_id"),
        "query": request.get("query"),
        "last_query": request.get("last_query"),
        "language": request.get("language"),
        "status": status,
        "search_interval_open": interval_open,
        "ready_for_search": (
            status == "searching" and not interval_open and not stale and not text_only
        ),
        "budget_seconds": round(budget, 3),
        "elapsed_seconds": round(elapsed, 3),
        "remaining_seconds": round(max(0.0, budget - elapsed), 3),
        "total_elapsed_seconds": round(float(request.get("total_elapsed_seconds", 0.0)), 3),
        "round": request.get("round"),
        "attempts": copy.deepcopy(request.get("attempts") or []),
        "attempt_count": len(request.get("attempts") or []),
        "decisions": copy.deepcopy(decisions),
        "decision_count": len(decisions),
        "last_decision": copy.deepcopy(decisions[-1]) if decisions else None,
        "question": copy.deepcopy(question) if isinstance(question, dict) else None,
        "choices": copy.deepcopy(choices),
        "requires_decision": status == "awaiting_decision",
        "launch_allowed": status == "searching" and not stale and not text_only,
        "search_complete": status == "found",
        "relevance_approved": False,
        "language_approved": False,
        "approval_note": (
            "A completed query only identifies candidates. Relevance and image-language "
            "labels still require the existing per-asset review."
        ),
        "stale_language": stale,
        "images_disabled": text_only,
        "requires_reconciliation": conditions["requires_reconciliation"],
        "current_language": conditions["target"],
        "allowed_actions": _allowed_actions(request, stale, text_only),
        "requires_content_regeneration": bool(request.get("requires_content_regeneration")),
        "requires_preview": bool(request.get("requires_preview")),
        "created_at": request.get("created_at"),
        "updated_at": request.get("updated_at"),
        "round_started_at": request.get("round_started_at"),
        "interval_started_at": request.get("interval_started_at"),
        "interval_ended_at": request.get("interval_ended_at"),
        "awaiting_since": request.get("awaiting_since"),
        "budget_exhausted_at": request.get("budget_exhausted_at"),
        "completed_at": request.get("completed_at"),
    }


def _request_budget(request):
    if "budget_seconds" in request and request.get("budget_seconds") is not None:
        value = request.get("budget_seconds")
    elif "budget" in request and request.get("budget") is not None:
        value = request.get("budget")
    else:
        value = DEFAULT_SEARCH_BUDGET_SECONDS
    return _number(value, "budget_seconds", minimum=0.001)


def _active_request_for_section(document, section_id, request_id):
    for other in document["requests"]:
        if other.get("request_id") == request_id:
            continue
        if other.get("section_id") == section_id and other.get("status") in _LIVE_SEARCH_STATUSES:
            return other
    return None


def start_search(root, request):
    """Create or explicitly resume one durable image-search request.

    A new request, or a request whose last attempt has completed, opens one
    active-search interval.  Repeating this call while that interval is open
    never restarts its clock or budget.  A request awaiting a decision stays
    paused until :func:`resolve_search` receives an explicitly confirmed action;
    resolving ``continue_search`` opens the next budget but not its clock.
    """

    if not isinstance(request, dict):
        raise ValueError("Image-search request must be an object")
    request_id = request.get("request_id")
    section_id = request.get("section_id")
    query = request.get("query")
    if not _nonempty(request_id):
        raise ValueError("request_id must be a nonempty string")
    if not _nonempty(section_id):
        raise ValueError("section_id must be a nonempty string")
    if not _nonempty(query):
        raise ValueError("query must be a nonempty string")
    preferences = _preferences(root)
    if _text_only(preferences):
        raise ValueError("Text-only jobs cannot start an image search")
    target = _target_language(preferences, required=True)
    document = _load_state(root)
    existing = None
    for item in document["requests"]:
        if item.get("request_id") == request_id:
            existing = item
            break
    moment = _utc_now()
    if existing is not None:
        if existing.get("section_id") != section_id:
            raise ValueError(
                f"Request {request_id} belongs to section {existing.get('section_id')!r}, "
                f"not {section_id!r}"
            )
        if existing.get("query") != query:
            raise ValueError(
                f"Request {request_id} was created for a different query; use a new "
                "request_id or record the query as an attempt"
            )
        if (
            ("budget_seconds" in request or "budget" in request)
            and _request_budget(request) != _request_budget(existing)
        ):
            raise ValueError("An existing search request has a fixed budget; start a new request")
        if existing.get("status") in _TERMINAL_SEARCH_STATUSES:
            return _summary(existing, preferences)
        conditions = _live_preconditions(existing, preferences)
        if conditions["stale_language"]:
            raise ValueError(
                f"Image language for request {request_id} is stale "
                f"({existing.get('language')!r} recorded, {conditions['target']!r} current); "
                "reconcile explicitly before searching"
            )
        if conditions["text_only"]:
            raise ValueError("Images are disabled for this job; resolve no_images or re-enable intake")
        _accrue(existing, moment)
        existing["updated_at"] = _iso(moment)
        if float(existing.get("elapsed_seconds", 0.0)) >= _request_budget(existing):
            _mark_awaiting_decision(existing, moment)
            _save_state(root, document)
            return _summary(existing, preferences)
        if existing.get("status") == "searching":
            _open_interval(existing, moment)
        _save_state(root, document)
        return _summary(existing, preferences)

    other = _active_request_for_section(document, section_id, request_id)
    if other is not None:
        raise ValueError(
            f"Section {section_id!r} already has live request {other.get('request_id')!r}; "
            "resolve it before starting another search budget"
        )
    budget = _request_budget(request)
    now_iso = _iso(moment)
    record = {
        "request_id": request_id,
        "section_id": section_id,
        "query": query,
        "language": target,
        "budget_seconds": budget,
        "status": "searching",
        "search_interval_open": True,
        "interval_started_at": now_iso,
        "interval_ended_at": None,
        "interval_accounted_seconds": 0.0,
        "round": 1,
        "rounds": [
            {
                "round": 1,
                "started_at": now_iso,
                "elapsed_seconds": 0.0,
                "status": "searching",
            }
        ],
        "elapsed_seconds": 0.0,
        "total_elapsed_seconds": 0.0,
        "round_started_at": now_iso,
        "last_accounted_at": now_iso,
        "attempts": [],
        "decisions": [],
        "question": None,
        "relevance_approved": False,
        "language_approved": False,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    document["requests"].append(record)
    _save_state(root, document)
    return _summary(record, preferences)


def _attempt_measurement(update, request, moment):
    """Return ``(seconds, kind, started, finished)`` for one attempt.

    ``kind`` is ``"measured"`` for an explicit elapsed value or a complete
    started_at/finished_at pair, and ``"tail"`` when only one timestamp is
    available.  A missing measurement returns ``None`` so the caller can use
    the active clock interval.
    """

    for key in ("elapsed_seconds", "duration_seconds", "active_seconds"):
        if key in update:
            return _number(update[key], key), "measured", None, None
    started = _timestamp(update["started_at"], "started_at") if "started_at" in update else None
    finished = _timestamp(update["finished_at"], "finished_at") if "finished_at" in update else None
    if started is not None and finished is not None:
        if finished < started:
            raise ValueError("finished_at cannot be earlier than started_at")
        return max(0.0, (finished - started).total_seconds()), "measured", started, finished
    last_value = (
        request.get("last_accounted_at")
        or request.get("interval_started_at")
        or request.get("round_started_at")
    )
    last = _timestamp(last_value, "last_accounted_at")
    if finished is not None:
        return max(0.0, (finished - last).total_seconds()), "tail", None, finished
    if started is not None:
        # A lone start is only an endpoint hint: the uncharged tail begins at
        # whichever is later, the reported start or the last charged checkpoint.
        # Starting at the checkpoint when the reported start is earlier avoids
        # charging the same interval twice; starting at the reported start when
        # it is later avoids charging the idle gap before the attempt.
        effective_start = max(started, last)
        return max(0.0, (moment - effective_start).total_seconds()), "tail", effective_start, moment
    return None


def _attempt_stamp(update):
    stamp = {}
    for key in ("started_at", "finished_at"):
        if key in update:
            stamp[key] = _iso(_timestamp(update[key], key))
    return stamp


def record_search_attempt(root, update):
    """Record one real host search and close its active interval.

    ``outcome='found'`` requires candidate IDs and completes the search request.
    It still does not approve relevance or image-language labels.  Optional
    ``elapsed_seconds``/``started_at``/``finished_at`` values record host-measured
    time; otherwise the open interval since the previous checkpoint is used.  A
    measured value is reconciled with time already charged by polling, so the
    same interval is never counted twice.  No search is performed here.
    """

    if not isinstance(update, dict):
        raise ValueError("Search attempt update must be an object")
    request_id = update.get("request_id")
    query = update.get("query")
    outcome = update.get("outcome")
    if not _nonempty(request_id):
        raise ValueError("request_id must be a nonempty string")
    if not _nonempty(query):
        raise ValueError("query must be a nonempty string")
    if not _nonempty(outcome):
        raise ValueError("outcome must be a nonempty string")
    outcome = outcome.strip().lower()
    candidates = update.get("candidate_ids", [])
    if candidates is None:
        candidates = []
    if not isinstance(candidates, list):
        raise ValueError("candidate_ids must be a list")
    for candidate in candidates:
        if not _nonempty(candidate):
            raise ValueError("candidate_ids must contain nonempty strings")
    if outcome == "found" and not candidates:
        raise ValueError("outcome 'found' requires at least one candidate ID")
    note = update.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be a string when supplied")
    preferences = _preferences(root)
    if _text_only(preferences):
        raise ValueError("Images are disabled for this job; no further searches are allowed")
    document = _load_state(root)
    request = _find_request(document, request_id)
    if request.get("status") in _TERMINAL_SEARCH_STATUSES:
        raise ValueError(
            f"Request {request_id} is already {request.get('status')}; start a new request "
            "if another search is genuinely required"
        )
    conditions = _live_preconditions(request, preferences)
    if conditions["stale_language"]:
        raise ValueError(
            f"Image language for request {request_id} changed; reconcile explicitly before "
            "recording another search"
        )
    moment = _utc_now()
    in_flight = False
    if request.get("status") == "awaiting_decision":
        started = _timestamp(update["started_at"], "started_at") if "started_at" in update else None
        exhausted = request.get("budget_exhausted_at")
        if started is None or not _nonempty(exhausted):
            raise ValueError(
                f"Request {request_id} is awaiting a user decision; resolve it before another search"
            )
        if started > _timestamp(exhausted, "budget_exhausted_at"):
            raise ValueError(
                f"Request {request_id} exhausted its budget before this search started; "
                "obtain an explicit decision first"
            )
        in_flight = True
        duration = 0.0
    else:
        before = float(request.get("elapsed_seconds", 0.0))
        if before >= _request_budget(request):
            _mark_awaiting_decision(request, moment)
            _save_state(root, document)
            raise ValueError(
                f"Request {request_id} already exhausted its budget; resolve it before searching"
            )
        duration = _attempt_measurement(update, request, moment)
        if duration is None:
            duration = _accrue(request, moment)
        else:
            duration, kind, started, finished = duration
            if kind == "measured":
                _charge_measured(
                    request, duration, moment, started=started, finished=finished
                )
            else:
                _charge_tail(request, duration, finished or moment)
    _close_interval(request, moment)

    attempt = {
        "query": query.strip(),
        "candidate_ids": list(candidates),
        "outcome": outcome,
        "note": note.strip() if isinstance(note, str) else None,
        "recorded_at": _iso(moment),
        "round": request.get("round"),
        "elapsed_seconds": round(duration, 6),
        "in_flight_after_budget": in_flight,
    }
    attempt.update(_attempt_stamp(update))
    request.setdefault("attempts", []).append(attempt)
    request["last_query"] = query.strip()
    request["updated_at"] = _iso(moment)
    if outcome == "found":
        _mark_found(request, moment)
    else:
        if float(request.get("elapsed_seconds", 0.0)) >= _request_budget(request):
            _mark_awaiting_decision(request, moment)
    _save_state(root, document)
    return _summary(request, preferences)


def search_status(root, request_id=None):
    """Accrue active time and return one request or the full request list.

    Polling moves the clock only while an explicitly opened interval is active;
    after an attempt has completed it reports idle time without charging it.
    Polling never restarts a round or resets its budget.  A request pauses at the
    budget with an explicit Arabic question/choices.  Waiting for the user is
    outside every interval, so it cannot be charged.
    """

    preferences = _preferences(root)
    had_state = _state_path(root).exists()
    document = _load_state(root)
    moment = _utc_now()
    if request_id is not None:
        request = _find_request(document, request_id)
        targets = [request]
    else:
        targets = list(document["requests"])
    for request in targets:
        if request.get("status") == "searching":
            conditions = _live_preconditions(request, preferences)
            if conditions["stale_language"] or conditions["text_only"]:
                # Stop the clock without erasing elapsed time; the host must reconcile.
                _close_interval(request, moment)
            else:
                _accrue(request, moment)
                if float(request.get("elapsed_seconds", 0.0)) >= _request_budget(request):
                    _mark_awaiting_decision(request, moment)
        request["last_checked_at"] = _iso(moment)
    if had_state or targets:
        _save_state(root, document)
    if request_id is not None:
        return _summary(targets[0], preferences)
    return [_summary(request, preferences) for request in targets]


def _remove_stale_question(request):
    request["question"] = None
    request["awaiting_since"] = None
    request["budget_exhausted_at"] = None


def resolve_search(root, decision):
    """Apply one explicitly user-confirmed timeout decision.

    ``continue_search`` opens a fresh 600-second round while preserving every
    prior attempt and decision, but does not start its clock: the host must call
    :func:`start_search` for the next real search.  ``use_arabic``/``use_english``
    also change the durable job preference and open a fresh round in the same
    stopped state.  ``no_images`` switches the job to text-only and closes the
    interval but deliberately leaves authored image blocks in place; the returned
    state discloses that picture-free content must be regenerated.
    """

    if not isinstance(decision, dict):
        raise ValueError("Search decision must be an object")
    request_id = decision.get("request_id")
    action = decision.get("action")
    if not _nonempty(request_id):
        raise ValueError("request_id must be a nonempty string")
    if not isinstance(action, str) or action.strip() not in SEARCH_ACTIONS:
        raise ValueError(
            "action must be one of " + ", ".join(SEARCH_ACTIONS)
        )
    action = action.strip()
    if decision.get("user_confirmed") is not True:
        raise ValueError("A timeout decision requires user_confirmed: true")
    note = decision.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be a string when supplied")
    preferences = _preferences(root)
    document = _load_state(root)
    request = _find_request(document, request_id)
    status = request.get("status")
    if status not in _LIVE_SEARCH_STATUSES:
        raise ValueError(
            f"Request {request_id} is {status}; it has no pending timeout decision"
        )
    conditions = _live_preconditions(request, preferences)
    stale = conditions["stale_language"]
    text_only = conditions["text_only"]
    moment = _utc_now()
    if action == "continue_search":
        if status != "awaiting_decision":
            raise ValueError("continue_search applies only after a full unsuccessful budget")
        if stale:
            raise ValueError(
                "The image language changed while the request was live; choose use_arabic, "
                "use_english, or no_images explicitly instead of reusing the old language"
            )
        if text_only:
            raise ValueError("Images are disabled for this job; choose no_images or re-enable intake")
        resulting_language = request.get("language")
        started_round = True
    elif action in {"use_arabic", "use_english"}:
        if status != "awaiting_decision" and not stale:
            raise ValueError(f"{action} applies only after a timeout or a language reconciliation")
        if text_only:
            raise ValueError(
                "Images are disabled for this job; re-enable an image source policy in intake "
                "before choosing an image language"
            )
        resulting_language = "ar" if action == "use_arabic" else "en"
        started_round = True
    else:  # no_images
        if status != "awaiting_decision" and not (stale or text_only):
            raise ValueError("no_images applies only after a timeout or a stale language decision")
        resulting_language = request.get("language")
        started_round = False

    previous_language = request.get("language")
    entry = {
        "action": action,
        "user_confirmed": True,
        "decided_at": _iso(moment),
        "previous_status": status,
        "previous_language": previous_language,
        "resulting_language": resulting_language,
        "started_round": started_round,
        "requires_content_regeneration": action == "no_images",
        "requires_preview": action == "no_images",
    }
    if note and note.strip():
        entry["note"] = note.strip()

    if action == "no_images":
        job_preferences = preferences
        job_preferences["image_mode"] = "text"
        job = load_job(root)
        job["preferences"] = job_preferences
        save_job(root, job)
        _close_interval(request, moment)
        if status == "searching":
            _close_round(request, moment, "no_images")
        request["status"] = "no_images"
        request["no_images"] = True
        request["requires_content_regeneration"] = True
        request["requires_preview"] = True
        request["closed_at"] = _iso(moment)
        _remove_stale_question(request)
        request["updated_at"] = _iso(moment)
    else:
        if action in {"use_arabic", "use_english"}:
            job = load_job(root)
            job_preferences = job.get("preferences") or {}
            if not isinstance(job_preferences, dict):
                raise ValueError("job preferences must be an object")
            job_preferences["image_language"] = resulting_language
            job["preferences"] = job_preferences
            save_job(root, job)
            request["language"] = resulting_language
            request["language_changed_at"] = _iso(moment)
        if status == "searching":
            _close_round(request, moment, "reconciled")
        _remove_stale_question(request)
        _open_round(request, moment)
        request["updated_at"] = _iso(moment)
    request.setdefault("decisions", []).append(entry)
    _save_state(root, document)
    return _summary(request, _preferences(root))
