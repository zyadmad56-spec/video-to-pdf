"""Timeline sampling and subtitle parsing.

All timestamps are absolute values in the original media timeline.  Caption
parsing keeps the source languages and raw cue text; it does not translate or
turn a lecture into study prose.
"""

from __future__ import annotations

import html
import math
import re


def sample_times(start, end, interval):
    """Yield ordered requested sample times, always including the interval end.

    ``end`` is requested, not proof that a decodable frame exists there.  Frame
    extraction clamps an EOF request to the last available instant and records
    both the requested and decoded times.
    """

    values = (start, end, interval)
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
        raise ValueError("Require finite 0 <= start < end and a positive finite interval")
    if interval <= 0 or start < 0 or end <= start:
        raise ValueError("Require 0 <= start < end and a positive finite interval")
    count = math.ceil((end - start) / interval)
    for index in range(count):
        yield start + index * interval
    yield end


def seconds(timestamp):
    """Parse ``HH:MM:SS.mmm``, ``MM:SS.mmm``, or SRT comma variants."""

    if not isinstance(timestamp, str):
        raise ValueError("Timestamps must be strings")
    cleaned = timestamp.strip().replace(",", ".")
    if not cleaned:
        raise ValueError("Empty timestamp")
    fields = cleaned.split(":")
    if len(fields) not in {2, 3}:
        raise ValueError(f"Unsupported timestamp: {timestamp!r}")
    total = 0.0
    for field in fields:
        if not re.fullmatch(r"\d+(?:\.\d+)?", field):
            raise ValueError(f"Unsupported timestamp: {timestamp!r}")
        total = total * 60 + float(field)
    if not math.isfinite(total) or total < 0:
        raise ValueError(f"Unsupported timestamp: {timestamp!r}")
    return total


def timestamp(seconds_count):
    if not isinstance(seconds_count, (int, float)) or not math.isfinite(seconds_count):
        raise ValueError("Timestamp seconds must be finite")
    if seconds_count < 0:
        raise ValueError("Timestamp seconds cannot be negative")
    milliseconds = round(seconds_count * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02}.{milliseconds:03}"


def _clean_cue_text(lines):
    text = " ".join(lines)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return " ".join(text.split())


def _caption_lines(caption_text):
    normalized = caption_text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    metadata = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        boundary = index == 0 or not lines[index - 1].strip()
        if boundary and re.match(r"^(NOTE(?:\s|$)|STYLE$|REGION$)", stripped):
            metadata = True
        if metadata:
            if not stripped:
                metadata = False
            yield ""
        else:
            yield line


def _caption_cues(caption_text):
    lines = list(_caption_lines(caption_text))
    is_srt = not caption_text.lstrip("\ufeff \r\n").startswith("WEBVTT")
    boundaries = [index for index, line in enumerate(lines) if "-->" in line]
    for position, index in enumerate(boundaries):
        stop = boundaries[position + 1] if position + 1 < len(boundaries) else len(lines)
        if stop < len(lines) and stop > index + 1 and lines[stop - 1].strip():
            separated_id = not lines[stop - 2].strip() and any(
                line.strip() for line in lines[index + 1:stop - 1]
            )
            srt_id = is_srt and lines[stop - 1].strip().isdigit()
            if separated_id or srt_id:
                stop -= 1
        yield index + 1, lines[index], lines[index + 1:stop]


def _cue_times(line_number, timing_line):
    try:
        begin, finish = timing_line.split("-->", 1)
        start = seconds(begin)
        end = seconds(finish.strip().split()[0])
    except (IndexError, ValueError) as error:
        raise ValueError(
            f"Malformed caption timing at line {line_number}: {timing_line!r}"
        ) from error
    if end <= start:
        raise ValueError(
            f"Caption cue ends before it starts at line {line_number}: {timing_line!r}"
        )
    return start, end


def _caption_record(ordinal, times, text, raw_text):
    return dict(id=f"audio-{ordinal:06}", kind="spoken", start=times[0],
                end=times[1], text=text, raw_text=raw_text)


def parse_captions(caption_text):
    """Parse VTT/SRT text into timestamped speech records.

    Temporary rolling-caption repetition is removed only when the cues actually
    overlap in time.  A phrase repeated later in the lecture remains a separate
    record.  Malformed timing lines fail instead of silently dropping cues.
    """

    if not isinstance(caption_text, str):
        raise ValueError("Caption input must be text")
    segments = []
    previous_text = ""
    previous_end = -10.0
    for line_number, timing_line, payload in _caption_cues(caption_text):
        start, end = _cue_times(line_number, timing_line)
        raw_text = _clean_cue_text(payload)
        if not raw_text:
            previous_text, previous_end = "", end
            continue
        text = raw_text
        # Only temporal overlap justifies removing rolling repetition.
        if start < previous_end - 1e-6:
            text = remove_overlap(previous_text, text)
        if text:
            segments.append(_caption_record(len(segments) + 1, (start, end), text, raw_text))
        previous_text, previous_end = raw_text, end
    return segments


def remove_overlap(previous, current):
    """Remove a prefix of ``current`` that repeats a suffix of ``previous``."""

    before, after = previous.split(), current.split()
    for length in range(min(len(before), len(after)), 0, -1):
        if before[-length:] == after[:length]:
            return " ".join(after[length:])
    return current
