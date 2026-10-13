"""AppSync direct Lambda resolver for Route.performance.

AppSync passes:
  event["arguments"] -> {"month": "2026-08", "thresholdMinutes": 15}
  event["source"]    -> the parent Route item already read from DynamoDB

The function reads that month's flight records from S3, filters them to the
route, and returns governed on-time metrics.
"""

import csv
import io
import os
import re

from calc import compute_performance, validate_threshold

BUCKET = os.environ.get("DATA_BUCKET", "")
PREFIX = os.environ.get("DATA_PREFIX", "ontime/")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

_s3 = None
_month_cache = {}  # reused across warm invocations


def _client():
    global _s3
    if _s3 is None:
        import boto3  # provided by the Lambda runtime

        _s3 = boto3.client("s3")
    return _s3


def _available_months():
    resp = _client().list_objects_v2(Bucket=BUCKET, Prefix=PREFIX, Delimiter="/")
    months = []
    for p in resp.get("CommonPrefixes", []):
        part = p["Prefix"][len(PREFIX):].strip("/")
        if part.startswith("month="):
            months.append(part[len("month="):])
    return sorted(months)


def _read_month(month):
    """Return the CSV text for a month, or None if there is no data."""
    from botocore.exceptions import ClientError

    key = f"{PREFIX}month={month}/flights.csv"
    try:
        obj = _client().get_object(Bucket=BUCKET, Key=key)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    return obj["Body"].read().decode("utf-8")


def load_rows(month):
    if month not in _month_cache:
        text = _read_month(month)
        if text is None:
            available = ", ".join(_available_months()) or "none"
            raise ValueError(
                f"No on-time data for month {month}. Available months: {available}"
            )
        _month_cache[month] = list(csv.DictReader(io.StringIO(text)))
    return _month_cache[month]


def handler(event, context):
    args = event.get("arguments") or {}
    route = event.get("source") or {}

    month = args.get("month", "")
    if not MONTH_RE.match(month):
        raise ValueError("month must be in YYYY-MM format, e.g. 2026-08")
    threshold = validate_threshold(args.get("thresholdMinutes"))

    rows = load_rows(month)
    result = compute_performance(
        rows,
        carrier=route["carrier"],
        origin=route["origin"],
        destination=route["destination"],
        threshold=threshold,
    )
    result["routeId"] = route["routeId"]
    result["month"] = month
    print(
        f"performance route={route['routeId']} month={month} "
        f"threshold={threshold} scheduled={result['flightsScheduled']}"
    )
    return result
