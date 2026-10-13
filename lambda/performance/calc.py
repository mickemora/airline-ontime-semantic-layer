"""Pure calculation logic for route on-time performance.

Kept free of AWS dependencies so it can be unit tested locally.
"""

import math
from typing import Iterable, Mapping, Optional

DEFAULT_THRESHOLD = 15
MIN_THRESHOLD = 1
MAX_THRESHOLD = 180


def definition_text(threshold: int) -> str:
    return (
        f"A flight is on time if it arrived less than {threshold} minutes after its "
        "scheduled arrival time. On-time % = on-time flights / all scheduled flights; "
        "cancelled and diverted flights count as not on time. Delay statistics cover "
        "arrived flights only, and early arrivals count as negative delay."
        + (" At 15 minutes this matches the US DOT definition." if threshold == 15 else "")
    )


def validate_threshold(threshold) -> int:
    if threshold is None:
        return DEFAULT_THRESHOLD
    if isinstance(threshold, bool) or not isinstance(threshold, int):
        raise ValueError("thresholdMinutes must be an integer")
    if not MIN_THRESHOLD <= threshold <= MAX_THRESHOLD:
        raise ValueError(
            f"thresholdMinutes must be between {MIN_THRESHOLD} and {MAX_THRESHOLD}"
        )
    return threshold


def _flag(value) -> bool:
    """BTS files encode flags as '1.00' / '0.00'; accept any numeric text."""
    try:
        return float(value or 0) >= 1
    except ValueError:
        return False


def percentile(sorted_values, pct: float) -> Optional[float]:
    """Nearest-rank percentile of an already sorted list."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def median(sorted_values) -> Optional[float]:
    n = len(sorted_values)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return sorted_values[mid]
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2


def _round(value, digits=1):
    return None if value is None else round(value, digits)


def compute_performance(
    rows: Iterable[Mapping[str, str]],
    carrier: str,
    origin: str,
    destination: str,
    threshold: int,
) -> dict:
    """Aggregate flight records for one carrier/origin/destination."""
    scheduled = cancelled = diverted = on_time = 0
    delays = []

    for row in rows:
        if (
            row.get("Reporting_Airline") != carrier
            or row.get("Origin") != origin
            or row.get("Dest") != destination
        ):
            continue
        scheduled += 1
        if _flag(row.get("Cancelled")):
            cancelled += 1
            continue
        if _flag(row.get("Diverted")):
            diverted += 1
            continue
        delay = float(row["ArrDelay"])
        delays.append(delay)
        if delay < threshold:
            on_time += 1

    delays.sort()
    arrived = len(delays)

    def pct(part):
        return _round(part / scheduled * 100) if scheduled else None

    return {
        "thresholdMinutes": threshold,
        "flightsScheduled": scheduled,
        "flightsArrived": arrived,
        "flightsCancelled": cancelled,
        "flightsDiverted": diverted,
        "flightsOnTime": on_time,
        "onTimePct": pct(on_time),
        "cancellationPct": pct(cancelled),
        "avgArrivalDelayMinutes": _round(sum(delays) / arrived) if arrived else None,
        "medianArrivalDelayMinutes": _round(median(delays)),
        "p90ArrivalDelayMinutes": _round(percentile(delays, 90)),
        "definition": definition_text(threshold),
    }
