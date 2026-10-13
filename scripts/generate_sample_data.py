#!/usr/bin/env python3
"""Generate synthetic flight records shaped like the US DOT BTS on-time dataset.

Output: data/ontime/month=YYYY-MM/flights.csv for each month, one row per
scheduled flight on the routes in data/routes.json. Columns use BTS names
(FlightDate, Reporting_Airline, Origin, Dest, ArrDelay, Cancelled, Diverted...)
so real BTS extracts can replace these files later without code changes.

The data is synthetic but follows realistic patterns: congested airports,
summer thunderstorm days, delays that build through the day, and carrier
differences. The generator is seeded, so output is reproducible.

Usage:
    python scripts/generate_sample_data.py [--months 2026-07 2026-08 2026-09] [--seed 42]
"""

import argparse
import calendar
import csv
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# Relative congestion: higher means more delays and cancellations.
AIRPORT_CONGESTION = {
    "ORD": 1.30, "LGA": 1.45, "SFO": 1.25, "JFK": 1.30, "DEN": 1.15, "ATL": 1.00,
    "DFW": 1.10, "SEA": 0.90, "LAX": 1.00, "MDW": 1.00, "MIA": 1.10,
}
# UTC offsets during daylight saving time (summer months).
AIRPORT_UTC_OFFSET = {
    "ORD": -5, "MDW": -5, "DFW": -5, "LGA": -4, "JFK": -4, "ATL": -4, "MIA": -4,
    "DEN": -6, "SFO": -7, "LAX": -7, "SEA": -7,
}
CARRIER_FACTOR = {"AS": 0.80, "DL": 0.85, "UA": 1.00, "AA": 1.05, "WN": 1.05, "B6": 1.25}
# Summer thunderstorm season is worst in July and eases by September.
MONTH_FACTOR = {6: 1.15, 7: 1.20, 8: 1.10, 9: 0.85, 10: 0.85}

FIELDS = [
    "FlightDate", "Reporting_Airline", "Flight_Number_Reporting_Airline",
    "Origin", "Dest", "CRSDepTime", "CRSArrTime", "ArrDelay", "Cancelled", "Diverted",
]


def hhmm(minutes_of_day: int) -> str:
    m = minutes_of_day % 1440
    return f"{m // 60:02d}{m % 60:02d}"


def departure_slots(freq: int):
    """Spread departures between 06:00 and 20:30 local time."""
    first, last = 6 * 60, 20 * 60 + 30
    if freq == 1:
        return [first + (last - first) // 2]
    step = (last - first) / (freq - 1)
    return [int(first + i * step) // 5 * 5 for i in range(freq)]


def generate_month(routes, year, month, rng):
    days = calendar.monthrange(year, month)[1]
    m_factor = MONTH_FACTOR.get(month, 1.0)

    # Bad-weather / ATC days per airport: more at congested airports.
    bad_days = {}
    for airport, cong in AIRPORT_CONGESTION.items():
        k = max(1, round(3 * cong * m_factor))
        bad_days[airport] = set(rng.sample(range(1, days + 1), k))

    rows = []
    for idx, r in enumerate(routes):
        carrier, origin, dest = r["carrier"], r["origin"], r["destination"]
        cong = (AIRPORT_CONGESTION[origin] + AIRPORT_CONGESTION[dest]) / 2
        c_factor = CARRIER_FACTOR.get(carrier, 1.0)
        tz_shift = (AIRPORT_UTC_OFFSET[dest] - AIRPORT_UTC_OFFSET[origin]) * 60
        slots = departure_slots(r["dailyFrequency"])

        for day in range(1, days + 1):
            bad = day in bad_days[origin] or day in bad_days[dest]
            for i, dep in enumerate(slots):
                arr = dep + r["scheduledBlockMinutes"] + tz_shift
                hours_into_day = (dep - 360) / 60

                p_cancel = 0.008 * cong * m_factor + (0.06 if bad else 0)
                p_divert = 0.002 + (0.004 if bad else 0)
                p_delay = (
                    0.12 * c_factor * cong * m_factor
                    + 0.015 * hours_into_day
                    + (0.35 if bad else 0)
                )
                p_delay = min(p_delay, 0.9)

                cancelled = diverted = 0
                arr_delay = ""
                roll = rng.random()
                if roll < p_cancel:
                    cancelled = 1
                elif roll < p_cancel + p_divert:
                    diverted = 1
                elif rng.random() < p_delay:
                    mean = 30 * c_factor * (1.6 if bad else 1.0)
                    arr_delay = str(round(5 + rng.expovariate(1 / mean)))
                else:
                    arr_delay = str(max(-45, round(rng.gauss(-9, 7))))

                rows.append({
                    "FlightDate": f"{year}-{month:02d}-{day:02d}",
                    "Reporting_Airline": carrier,
                    "Flight_Number_Reporting_Airline": 100 + idx * 20 + i,
                    "Origin": origin,
                    "Dest": dest,
                    "CRSDepTime": hhmm(dep),
                    "CRSArrTime": hhmm(arr),
                    "ArrDelay": arr_delay,
                    "Cancelled": f"{cancelled:.2f}",
                    "Diverted": f"{diverted:.2f}",
                })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--months", nargs="+", default=["2026-07", "2026-08", "2026-09"])
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    routes = json.loads((DATA_DIR / "routes.json").read_text())
    rng = random.Random(args.seed)

    for ym in args.months:
        year, month = (int(x) for x in ym.split("-"))
        rows = generate_month(routes, year, month, rng)
        out = DATA_DIR / "ontime" / f"month={ym}" / "flights.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {len(rows):>5} flights -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
