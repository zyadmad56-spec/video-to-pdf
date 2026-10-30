"""Adaptive lecture sampling planner.

Pure deterministic scheduling contract for 15 -> 10 -> 5 frame recovery.
"""

from __future__ import annotations

import math
from typing import Iterable

DEFAULT_INTERVAL = 15.0
MIN_INTERVAL = 5.0
_EPSILON = 1e-6


def _parse_finite_float(val: object, name: str) -> float:
    """Validate and parse a finite floating point value."""
    if isinstance(val, bool):
        raise TypeError(f"{name} cannot be a boolean")
    try:
        num = float(val)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be a valid number, got {val!r}")
    if not math.isfinite(num):
        raise ValueError(f"{name} must be finite, got {val!r}")
    return float(num)


def frame_interval(value: object = None) -> float:
    """Return 15.0 by default or validate a finite numeric interval >= 5.0."""
    if value is None:
        return DEFAULT_INTERVAL
    interval = _parse_finite_float(value, "frame_interval")
    if interval < MIN_INTERVAL:
        raise ValueError(
            f"frame_interval must be at least {MIN_INTERVAL}, got {interval}"
        )
    return interval


def _parse_refinement_level(interval: object) -> float:
    """Validate that interval is exactly 10.0 or 5.0."""
    val = _parse_finite_float(interval, "Refinement interval")
    if val in (10.0, 5.0):
        return val
    raise ValueError(f"Refinement interval must be 10 or 5, got {interval!r}")


def plan_refinement(
    start: object,
    end: object,
    interval: object,
    existing_requested: Iterable[object] | None,
) -> list[float]:
    """Plan strictly interior recovery points within a window <= 15s."""
    s = _parse_finite_float(start, "start")
    e = _parse_finite_float(end, "end")
    if s < 0.0:
        raise ValueError(f"start must be non-negative, got {s}")
    if s >= e:
        raise ValueError(f"start must be strictly less than end ({s} >= {e})")

    window_len = e - s
    if window_len > DEFAULT_INTERVAL + _EPSILON:
        raise ValueError(
            f"window length ({window_len:.6f}s) exceeds maximum {DEFAULT_INTERVAL}s"
        )

    step = _parse_refinement_level(interval)

    existing_set: list[float] = []
    if existing_requested is not None:
        try:
            items = iter(existing_requested)
        except TypeError:
            raise TypeError("existing_requested must be an iterable")
        for item in items:
            existing_set.append(_parse_finite_float(item, "existing timestamp"))

    new_points: list[float] = []
    k = 1
    while True:
        pt = s + k * step
        if pt >= e - _EPSILON:
            break
        # Exclude previously requested points within 1e-6 (absolute tolerance only)
        if not any(abs(pt - exp) <= _EPSILON for exp in existing_set):
            new_points.append(pt)
        k += 1

    new_points.sort()
    return new_points


def validate_refinement_level(
    completed_intervals: Iterable[object] | None,
    interval: object,
) -> None:
    """Validate refinement level progression (10 must precede 5)."""
    requested = _parse_refinement_level(interval)

    if completed_intervals is None:
        raise TypeError("completed_intervals must be an iterable")

    try:
        items = iter(completed_intervals)
    except TypeError:
        raise TypeError("completed_intervals must be an iterable")

    has_ten = False
    for item in items:
        lvl = _parse_refinement_level(item)
        if lvl == 10.0:
            has_ten = True
        elif lvl == 5.0:
            if not has_ten:
                raise ValueError(
                    "Invalid completed history: level 5 completed before level 10"
                )

    if requested == 5.0 and not has_ten:
        raise ValueError(
            "Cannot request 5-second refinement before completing 10-second refinement"
        )
