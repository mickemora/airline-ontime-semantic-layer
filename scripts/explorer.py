#!/usr/bin/env python3
"""Local web explorer for the deployed semantic layer.

Starts a small web server on http://127.0.0.1:8765 that:
  - serves the explorer UI (ui/index.html)
  - forwards GraphQL queries to the deployed AppSync API (the API key stays
    on this machine and is never sent to the browser)
  - fetches the Performance Lambda's CloudWatch logs so the UI can show
    real invocation counts, durations, cold starts and S3 reads
  - serves the schema, example queries and the actual resolver / Lambda
    source, so the UI shows the code that is really running

Requires the stack to be deployed and AWS credentials (the same ones you
used for `cdk deploy`). Uses boto3 from requirements-dev.txt.

Usage:
    python scripts/explorer.py [--port 8765] [--no-browser]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
UI_FILE = ROOT / "ui" / "index.html"
EXAMPLES_FILE = ROOT / "queries" / "examples.graphql"
SCHEMA_FILE = ROOT / "graphql" / "schema.graphql"
STACK_NAME = os.environ.get("STACK_NAME", "AirlineOntimeSemanticLayer")

# Only these files can be read through /api/source.
SOURCE_FILES = {
    "graphql/schema.graphql",
    "resolvers/getRoute.js",
    "resolvers/listRoutes.js",
    "lambda/performance/handler.py",
    "lambda/performance/calc.py",
    "airline_semantic_layer/stack.py",
}


# ---------------------------------------------------------------------------
# AWS discovery
# ---------------------------------------------------------------------------
def discover(stack_name):
    import boto3

    session = boto3.session.Session()
    cfn = session.client("cloudformation")
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}

    cfg = {
        "stackName": stack_name,
        "region": session.region_name,
        "apiUrl": outputs["GraphQLApiUrl"],
        "apiKey": outputs["GraphQLApiKey"],
        "apiId": outputs.get("GraphQLApiId"),
        "tableName": outputs.get("RoutesTableName"),
        "bucketName": outputs.get("DataBucketName"),
        "functionName": outputs.get("PerformanceFunctionName"),
        "logGroup": None,
        "months": [],
    }

    if cfg["functionName"]:
        try:
            fn = session.client("lambda").get_function_configuration(
                FunctionName=cfg["functionName"]
            )
            cfg["logGroup"] = (fn.get("LoggingConfig") or {}).get("LogGroup") or (
                f"/aws/lambda/{cfg['functionName']}"
            )
        except Exception as err:  # noqa: BLE001
            print(f"  note: could not read Lambda config ({err}); logs disabled")

    if cfg["bucketName"]:
        try:
            resp = session.client("s3").list_objects_v2(
                Bucket=cfg["bucketName"], Prefix="ontime/", Delimiter="/"
            )
            for p in resp.get("CommonPrefixes", []):
                m = re.search(r"month=(\d{4}-\d{2})", p["Prefix"])
                if m:
                    cfg["months"].append(m.group(1))
            cfg["months"].sort()
        except Exception as err:  # noqa: BLE001
            print(f"  note: could not list S3 months ({err})")

    cfg["_logs"] = session.client("logs") if cfg["logGroup"] else None
    return cfg


def public_config(cfg):
    key = cfg["apiKey"] or ""
    return {
        k: v for k, v in cfg.items() if not k.startswith("_") and k != "apiKey"
    } | {"apiKeyMasked": (key[:4] + "…" + key[-4:]) if len(key) > 8 else "set"}


# ---------------------------------------------------------------------------
# Example queries (single source: queries/examples.graphql)
# ---------------------------------------------------------------------------
def load_examples():
    text = EXAMPLES_FILE.read_text()
    lines = text.splitlines()
    examples = []
    i = 0
    while i < len(lines):
        m = re.match(r"^query (\w+)", lines[i])
        if not m:
            i += 1
            continue
        # Description: the comment block directly above the query.
        desc, j = [], i - 1
        while j >= 0 and lines[j].startswith("#"):
            desc.insert(0, lines[j].lstrip("# ").strip())
            j -= 1
        desc_text = re.sub(r"^\d+\.\s*", "", " ".join(desc)).strip()
        # Body: until braces balance.
        depth, body = 0, []
        for k in range(i, len(lines)):
            body.append(lines[k])
            depth += lines[k].count("{") - lines[k].count("}")
            if depth == 0 and "{" in "".join(body):
                i = k + 1
                break
        examples.append(
            {"name": m.group(1), "description": desc_text, "query": "\n".join(body)}
        )
    return examples


# ---------------------------------------------------------------------------
# Lambda logs
# ---------------------------------------------------------------------------
REPORT_RE = re.compile(
    r"REPORT RequestId: (?P<id>\S+)\s+Duration: (?P<duration>[\d.]+) ms\s+"
    r"Billed Duration: (?P<billed>\d+) ms\s+Memory Size: (?P<memory>\d+) MB\s+"
    r"Max Memory Used: (?P<used>\d+) MB(?:\s+Init Duration: (?P<init>[\d.]+) ms)?"
)
START_RE = re.compile(r"START RequestId: (\S+)")
PERF_RE = re.compile(r"performance (.*)")


def lambda_invocations(cfg, since_ms):
    """Group log events into one record per Lambda invocation."""
    client = cfg["_logs"]
    if client is None:
        return {"available": False, "invocations": [], "raw": []}

    events, token = [], None
    for _ in range(5):  # a demo produces few events; cap pagination
        kwargs = {"logGroupName": cfg["logGroup"], "startTime": int(since_ms)}
        if token:
            kwargs["nextToken"] = token
        resp = client.filter_log_events(**kwargs)
        events.extend(resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            break
    events.sort(key=lambda e: (e["logStreamName"], e["timestamp"]))

    by_id, current = {}, {}
    for e in events:
        msg = e["message"].strip()
        stream = e["logStreamName"]
        if m := START_RE.search(msg):
            current[stream] = m.group(1)
            by_id.setdefault(
                m.group(1),
                {"requestId": m.group(1), "stream": stream, "timestamp": e["timestamp"]},
            )
            continue
        req_id = current.get(stream)
        if not req_id:
            continue
        rec = by_id[req_id]
        if m := PERF_RE.search(msg):
            fields = dict(
                re.findall(r"(\w+)=(\([^)]*\)|[^\s]+(?: \([^)]*\))?)", m.group(1))
            )
            rec["fields"] = fields
        elif m := REPORT_RE.search(msg):
            rec.update(
                durationMs=float(m["duration"]),
                billedMs=int(m["billed"]),
                memoryMb=int(m["memory"]),
                maxMemoryUsedMb=int(m["used"]),
                initMs=float(m["init"]) if m["init"] else None,
            )
        elif "[ERROR]" in msg or "Traceback" in msg or "Error" in msg.split(":")[0]:
            rec.setdefault("errors", []).append(msg[:300])

    invocations = sorted(by_id.values(), key=lambda r: r["timestamp"])
    raw = [
        {"timestamp": e["timestamp"], "stream": e["logStreamName"][-8:], "message": e["message"].rstrip()}
        for e in sorted(events, key=lambda e: e["timestamp"])
    ][-200:]
    return {"available": True, "invocations": invocations, "raw": raw}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    cfg = None

    def log_message(self, fmt, *args):  # quieter console
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj))

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                return self._send(200, UI_FILE.read_bytes(), "text/html")
            if url.path == "/api/config":
                return self._json(200, public_config(self.cfg))
            if url.path == "/api/examples":
                return self._json(200, load_examples())
            if url.path == "/api/source":
                rel = (qs.get("path") or [""])[0]
                if rel not in SOURCE_FILES:
                    return self._json(404, {"error": f"{rel} is not available"})
                return self._send(200, (ROOT / rel).read_text(), "text/plain")
            if url.path == "/api/lambda-logs":
                since = int((qs.get("since") or [str(int(time.time() * 1000) - 60000)])[0])
                return self._json(200, lambda_invocations(self.cfg, since))
            return self._json(404, {"error": "not found"})
        except Exception as err:  # noqa: BLE001
            return self._json(500, {"error": f"{type(err).__name__}: {err}"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/graphql":
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        req = urllib.request.Request(
            self.cfg["apiUrl"],
            data=body,
            headers={"Content-Type": "application/json", "x-api-key": self.cfg["apiKey"]},
        )
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, payload = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, payload = err.code, err.read()
        except Exception as err:  # noqa: BLE001
            return self._json(502, {"error": f"Could not reach AppSync: {err}"})
        elapsed = round((time.time() - started) * 1000)
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = {"errors": [{"message": payload.decode(errors="replace")[:500]}]}
        return self._json(
            200,
            {"status": status, "elapsedMs": elapsed, "startedAt": int(started * 1000), "response": parsed},
        )


def main():
    parser = argparse.ArgumentParser(description="Local explorer for the semantic layer")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--stack", default=STACK_NAME)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    print(f"Reading stack '{args.stack}' …")
    try:
        cfg = discover(args.stack)
    except Exception as err:  # noqa: BLE001
        print(f"\nCould not read the stack: {err}")
        print("Check that `cdk deploy` finished and that your AWS credentials and")
        print("region (AWS_PROFILE / AWS_REGION) point at the same account.")
        sys.exit(1)

    Handler.cfg = cfg
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"  API      {cfg['apiUrl']}")
    print(f"  Region   {cfg['region']}")
    print(f"  Months   {', '.join(cfg['months']) or 'unknown'}")
    print(f"  Logs     {cfg['logGroup'] or 'unavailable'}")
    print(f"\nExplorer running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
