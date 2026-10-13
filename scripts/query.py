#!/usr/bin/env python3
"""Run a named query from queries/examples.graphql against the deployed API.

The API URL and key are read from the CloudFormation stack outputs (needs
boto3 and AWS credentials), or from APPSYNC_URL / APPSYNC_API_KEY.

Usage:
    python scripts/query.py                      # list available queries
    python scripts/query.py RouteWithPerformance
    python scripts/query.py ThresholdComparison
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STACK_NAME = os.environ.get("STACK_NAME", "AirlineOntimeSemanticLayer")
EXAMPLES = ROOT / "queries" / "examples.graphql"


def load_queries():
    text = EXAMPLES.read_text()
    queries = {}
    for match in re.finditer(r"^query (\w+)", text, re.MULTILINE):
        start = match.start()
        depth, i = 0, text.index("{", start)
        while True:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        queries[match.group(1)] = text[start : i + 1]
    return queries


def endpoint():
    url, key = os.environ.get("APPSYNC_URL"), os.environ.get("APPSYNC_API_KEY")
    if url and key:
        return url, key
    import boto3

    stack = boto3.client("cloudformation").describe_stacks(StackName=STACK_NAME)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Stacks"][0]["Outputs"]}
    return outputs["GraphQLApiUrl"], outputs["GraphQLApiKey"]


def run(query):
    url, key = endpoint()
    req = urllib.request.Request(
        url,
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json", "x-api-key": key},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    queries = load_queries()
    if len(sys.argv) < 2 or sys.argv[1] not in queries:
        print("Available queries:")
        for name in queries:
            print(f"  {name}")
        sys.exit(0 if len(sys.argv) < 2 else 1)
    print(json.dumps(run(queries[sys.argv[1]]), indent=2))


if __name__ == "__main__":
    main()
