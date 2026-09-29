import argparse
import asyncio
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, replace
import json
from pathlib import Path
import time
from typing import Any

from .jev import JevClient
from .protocol import ProtocolError, RouteRequest


@dataclass(frozen=True)
class Experiment:
    name: str
    fixed_profile: str | None = None
    model_only: bool = False
    execution_aware: bool = False


EXPERIMENTS = (
    Experiment("always_luna_medium", fixed_profile="luna_medium"),
    Experiment("always_sol_medium", fixed_profile="sol_medium"),
    Experiment("always_sol_high", fixed_profile="sol_high"),
    Experiment("jev_prompt_model_only", model_only=True),
    Experiment("jev_prompt_model_effort"),
    Experiment("jev_execution_model_effort", execution_aware=True),
)


def _prompt_only(request: RouteRequest) -> RouteRequest:
    # Context size remains part of the prompt-aware/cache-aware baseline. Execution
    # feedback is the isolated variable in the execution-aware experiment.
    metrics = {}
    if "context_tokens" in request.metrics:
        metrics["context_tokens"] = request.metrics["context_tokens"]
    return replace(request, previous_turn_status="unknown", metrics=metrics)


def _model_only(request: RouteRequest) -> RouteRequest:
    profiles = {
        profile_id: profile
        for profile_id, profile in request.profiles.items()
        if profile.kind == "keep" or profile.effort == "medium"
    }
    if len(profiles) == 1:
        raise ProtocolError("model-only benchmark requires a medium profile")
    return replace(request, profiles=profiles)


def _fixed_result(
    request: RouteRequest,
    experiment: Experiment,
    case: Any,
) -> dict[str, Any]:
    profile = experiment.fixed_profile
    if profile not in request.profiles:
        return {
            "case": case,
            "request_id": request.id,
            "experiment": experiment.name,
            "ok": False,
            "error": f"profile_unavailable:{profile}",
            "latency_ms": 0,
        }
    return {
        "case": case,
        "request_id": request.id,
        "experiment": experiment.name,
        "ok": True,
        "choice": profile,
        "confidence": None,
        "probabilities": None,
        "latency_ms": 0,
    }


async def benchmark_requests(
    requests: Iterable[tuple[Any, RouteRequest]],
    client: JevClient,
) -> AsyncIterator[dict[str, Any]]:
    for case, original in requests:
        for experiment in EXPERIMENTS:
            if experiment.fixed_profile is not None:
                yield _fixed_result(original, experiment, case)
                continue

            started = time.perf_counter()
            try:
                request = original if experiment.execution_aware else _prompt_only(original)
                if experiment.model_only:
                    request = _model_only(request)
                answer = await client.route(request)
                yield {
                    "case": case,
                    "request_id": request.id,
                    "experiment": experiment.name,
                    "ok": True,
                    "choice": answer.choice,
                    "confidence": answer.confidence,
                    "probabilities": answer.probabilities,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                }
            except Exception as exc:
                yield {
                    "case": case,
                    "request_id": request.id,
                    "experiment": experiment.name,
                    "ok": False,
                    "error": f"{type(exc).__name__}:{exc}",
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                }


def _read_requests(path: Path) -> list[tuple[Any, RouteRequest]]:
    requests = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                request = RouteRequest.from_json(raw)
                case = raw.get("case", line_number)
                requests.append((case, request))
            except (json.JSONDecodeError, ProtocolError) as exc:
                raise ValueError(f"invalid input at line {line_number}: {exc}") from exc
    return requests


async def _run(input_path: Path, output_path: Path) -> None:
    requests = _read_requests(input_path)
    async with JevClient() as client:
        with output_path.open("w", encoding="utf-8", newline="\n") as stream:
            async for result in benchmark_requests(requests, client):
                stream.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
                stream.write("\n")


def run() -> None:
    parser = argparse.ArgumentParser(
        description="Compare fixed, model-only, and model+effort Jev routing policies."
    )
    parser.add_argument("--input", required=True, type=Path, help="Route-request JSONL")
    parser.add_argument("--output", required=True, type=Path, help="Result JSONL")
    args = parser.parse_args()
    asyncio.run(_run(args.input, args.output))


if __name__ == "__main__":
    run()
