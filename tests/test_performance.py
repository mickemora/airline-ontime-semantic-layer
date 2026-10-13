"""Unit tests for the performance Lambda. Run: python -m unittest discover tests"""

import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lambda" / "performance"))

import calc  # noqa: E402
import handler  # noqa: E402


def row(delay="", cancelled=0, diverted=0, carrier="UA", origin="ORD", dest="LGA"):
    return {
        "Reporting_Airline": carrier, "Origin": origin, "Dest": dest,
        "ArrDelay": delay, "Cancelled": f"{cancelled:.2f}", "Diverted": f"{diverted:.2f}",
    }


SAMPLE = [
    row("-10"), row("0"), row("14"), row("15"), row("45"),
    row(cancelled=1), row(diverted=1),
    row("100", carrier="AA"),  # different carrier: ignored
]


class CalcTests(unittest.TestCase):
    def test_counts_and_dot_threshold(self):
        r = calc.compute_performance(SAMPLE, "UA", "ORD", "LGA", 15)
        self.assertEqual(r["flightsScheduled"], 7)
        self.assertEqual(r["flightsArrived"], 5)
        self.assertEqual(r["flightsCancelled"], 1)
        self.assertEqual(r["flightsDiverted"], 1)
        self.assertEqual(r["flightsOnTime"], 3)  # -10, 0, 14 (15 is late)
        self.assertEqual(r["onTimePct"], round(3 / 7 * 100, 1))
        self.assertEqual(r["cancellationPct"], round(1 / 7 * 100, 1))

    def test_threshold_changes_result(self):
        strict = calc.compute_performance(SAMPLE, "UA", "ORD", "LGA", 1)
        relaxed = calc.compute_performance(SAMPLE, "UA", "ORD", "LGA", 60)
        self.assertEqual(strict["flightsOnTime"], 2)
        self.assertEqual(relaxed["flightsOnTime"], 5)

    def test_delay_statistics(self):
        r = calc.compute_performance(SAMPLE, "UA", "ORD", "LGA", 15)
        self.assertEqual(r["avgArrivalDelayMinutes"], round((-10 + 0 + 14 + 15 + 45) / 5, 1))
        self.assertEqual(r["medianArrivalDelayMinutes"], 14)
        self.assertEqual(r["p90ArrivalDelayMinutes"], 45)

    def test_no_flights(self):
        r = calc.compute_performance(SAMPLE, "DL", "ATL", "LAX", 15)
        self.assertEqual(r["flightsScheduled"], 0)
        self.assertIsNone(r["onTimePct"])
        self.assertIsNone(r["p90ArrivalDelayMinutes"])

    def test_threshold_validation(self):
        self.assertEqual(calc.validate_threshold(None), 15)
        for bad in (0, 181, -5, "15", 2.5, True):
            with self.assertRaises(ValueError):
                calc.validate_threshold(bad)

    def test_percentile_nearest_rank(self):
        values = list(range(1, 11))
        self.assertEqual(calc.percentile(values, 90), 9)
        self.assertEqual(calc.percentile(values, 100), 10)
        self.assertEqual(calc.percentile([7], 90), 7)


class HandlerTests(unittest.TestCase):
    ROUTE = {"routeId": "UA-ORD-LGA", "carrier": "UA", "origin": "ORD", "destination": "LGA"}

    def setUp(self):
        handler._month_cache.clear()
        self._orig_read = handler._read_month
        self._orig_avail = handler._available_months
        sample_file = ROOT / "data" / "ontime" / "month=2026-08" / "flights.csv"
        handler._read_month = lambda m: sample_file.read_text() if m == "2026-08" else None
        handler._available_months = lambda: ["2026-07", "2026-08", "2026-09"]

    def tearDown(self):
        handler._read_month = self._orig_read
        handler._available_months = self._orig_avail

    def event(self, **args):
        return {"arguments": args, "source": self.ROUTE}

    def test_returns_metrics_for_route(self):
        r = handler.handler(self.event(month="2026-08", thresholdMinutes=15), None)
        self.assertEqual(r["routeId"], "UA-ORD-LGA")
        self.assertEqual(r["month"], "2026-08")
        self.assertGreater(r["flightsScheduled"], 0)
        self.assertTrue(0 <= r["onTimePct"] <= 100)

    def test_default_threshold(self):
        r = handler.handler(self.event(month="2026-08"), None)
        self.assertEqual(r["thresholdMinutes"], 15)

    def test_bad_month_format(self):
        with self.assertRaises(ValueError):
            handler.handler(self.event(month="Aug 2026"), None)

    def test_missing_month_lists_available(self):
        with self.assertRaisesRegex(ValueError, "Available months: 2026-07, 2026-08"):
            handler.handler(self.event(month="2025-01"), None)


class SampleDataTests(unittest.TestCase):
    def test_every_route_has_data_each_month(self):
        import json

        routes = json.loads((ROOT / "data" / "routes.json").read_text())
        for path in (ROOT / "data" / "ontime").glob("month=*/flights.csv"):
            with path.open() as f:
                seen = {(r["Reporting_Airline"], r["Origin"], r["Dest"]) for r in csv.DictReader(f)}
            for route in routes:
                key = (route["carrier"], route["origin"], route["destination"])
                self.assertIn(key, seen, f"{route['routeId']} missing in {path}")


if __name__ == "__main__":
    unittest.main()
