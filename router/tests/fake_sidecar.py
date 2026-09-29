"""Deterministic NDJSON sidecar used by Rust/integration harnesses."""

import argparse
import json
import sys
import time


parser = argparse.ArgumentParser()
parser.add_argument("--delay-ms", type=int, default=0)
parser.add_argument("--choice", default="keep_current")
parser.add_argument("--confidence", type=float, default=0.9)
args = parser.parse_args()


for line in sys.stdin:
    request = json.loads(line)
    if args.delay_ms:
        time.sleep(args.delay_ms / 1000)
    choice = args.choice
    profiles = request["profiles"]
    probabilities = {key: 0.0 for key in profiles}
    if choice in probabilities:
        probabilities[choice] = 1.0
    else:
        probabilities[next(iter(probabilities))] = 1.0
    print(
        json.dumps(
            {
                "id": request["id"],
                "ok": True,
                "choice": choice,
                "confidence": args.confidence,
                "probabilities": probabilities,
                "latency_ms": args.delay_ms,
            }
        ),
        flush=True,
    )
