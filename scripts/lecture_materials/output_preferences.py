"""User choices for visible source navigation; evidence remains unchanged."""

from __future__ import annotations

import math

from .jobs import load_job


def timestamps_visible(preferences):
    visible = preferences.get("show_timestamps", True)
    if not isinstance(visible, bool):
        raise ValueError("preferences.show_timestamps must be true or false")
    return visible


def timestamp_intake(preferences):
    timestamps_visible(preferences)
    questions = []
    if "show_timestamps" not in preferences:
        questions.append({
            "id": "show_timestamps",
            "question": "تحب نعرض وقت الجزء من الفيديو مع كل قسم، زي 17:18–18:18، ولا نخفيه؟",
            "options": [
                {"value": True, "label": "إظهار وقت الجزء"},
                {"value": False, "label": "إخفاء وقت الجزء"},
            ],
        })
    return {"questions": questions, "answered": "show_timestamps" in preferences}


def section_source_range(section):
    # Missing is allowed; zero, fractions, reversed bounds and nonfinite values differ.
    if "source_range" not in section:
        return None
    interval = section["source_range"]
    if not isinstance(interval, dict):
        raise ValueError("section.source_range must contain numeric start and end seconds")
    bounds = [interval.get("start"), interval.get("end")]
    if any(isinstance(bound, bool) or not isinstance(bound, (int, float))
           or not math.isfinite(bound) for bound in bounds):
        raise ValueError("section.source_range requires finite numeric start and end seconds")
    start, end = bounds
    if not 0 <= start < end:
        raise ValueError("section.source_range requires 0 <= start < end")
    return start, end


def _clock(seconds):
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02}:{minutes:02}:{seconds:02}"
    return f"{minutes:02}:{seconds:02}"


def section_time_label(root, section):
    interval = section_source_range(section)
    if interval is None:
        return ""
    if not timestamps_visible(load_job(root).get("preferences", {})):
        return ""
    start, end = interval
    return f"{_clock(math.floor(start))}–{_clock(math.ceil(end))}"
