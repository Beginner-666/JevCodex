import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

from .benchmark import EXPERIMENTS, Experiment, _model_only, _prompt_only
from .jev import JevAnswer, JevClient
from .protocol import RouteRequest


DEFAULT_EXPERIMENTS = (
    "always_luna_medium",
    "always_sol_medium",
    "jev_execution_model_effort",
)
PROFILE_RANK = {
    "luna_medium": 0,
    "luna_high": 1,
    "sol_medium": 2,
    "sol_high": 3,
}
DOWNGRADE_SELECTED_PROBABILITY = 0.65
DOWNGRADE_PROBABILITY_MARGIN = 0.25
DOWNGRADE_MIN_CONFIDENCE = 0.80
UPGRADE_MIN_CONFIDENCE = 0.60
DOWNGRADE_MAX_CONTEXT_TOKENS = 20_000
IGNORED_WORKSPACE_PARTS = {".git", ".mypy_cache", ".pytest_cache", "__pycache__"}


@dataclass(frozen=True)
class VerificationCommand:
    argv: tuple[str, ...]
    timeout_seconds: int = 30


@dataclass(frozen=True)
class E2ECase:
    name: str
    difficulty: str
    quick: bool
    turns: tuple[str, ...]
    workspace: Path
    case_dir: Path
    timeout_seconds: int
    allowed_changes: tuple[str, ...]
    verification: tuple[VerificationCommand, ...]
    route_request: RouteRequest


@dataclass(frozen=True)
class RouteDecision:
    profile: str
    model: str
    effort: str
    jev_choice: str | None
    confidence: float | None
    probabilities: dict[str, float] | None
    reason: str
    latency_ms: int
    error: str | None = None


def _read_cases(path: Path, suite: str) -> list[E2ECase]:
    cases = []
    case_dir = path.resolve().parent
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            raw = json.loads(line)
            name = raw.get("case")
            difficulty = raw.get("difficulty")
            prompt = raw.get("prompt")
            if not all(isinstance(value, str) and value for value in (name, difficulty, prompt)):
                raise ValueError(f"invalid case metadata at line {line_number}")
            raw_turns = raw.get("turns", [prompt])
            if (
                not isinstance(raw_turns, list)
                or len(raw_turns) < 2
                or not all(isinstance(value, str) and value for value in raw_turns)
            ):
                raise ValueError(
                    f"case {name} must contain at least two non-empty turns"
                )
            quick = raw.get("quick", False)
            if not isinstance(quick, bool):
                raise ValueError(f"quick must be boolean at line {line_number}")
            if suite == "quick" and not quick:
                continue
            workspace_value = raw.get("workspace")
            if not isinstance(workspace_value, str) or not workspace_value:
                raise ValueError(f"workspace is required at line {line_number}")
            workspace = (case_dir / workspace_value).resolve()
            if not workspace.is_dir():
                raise ValueError(f"workspace not found at line {line_number}: {workspace}")
            timeout_seconds = raw.get("timeout_seconds", 180)
            if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
                raise ValueError(f"invalid timeout_seconds at line {line_number}")
            allowed_changes = raw.get("allowed_changes")
            if not isinstance(allowed_changes, list) or not allowed_changes or not all(
                isinstance(value, str) and value for value in allowed_changes
            ):
                raise ValueError(f"allowed_changes is required at line {line_number}")
            raw_verification = raw.get("verification")
            if not isinstance(raw_verification, list) or not raw_verification:
                raise ValueError(
                    f"case {name} has no verifier; success would be undefined"
                )
            verification = []
            for command in raw_verification:
                argv = command.get("argv") if isinstance(command, dict) else None
                command_timeout = (
                    command.get("timeout_seconds", 30)
                    if isinstance(command, dict)
                    else None
                )
                if (
                    not isinstance(argv, list)
                    or not argv
                    or not all(isinstance(value, str) and value for value in argv)
                    or not isinstance(command_timeout, int)
                    or command_timeout <= 0
                ):
                    raise ValueError(f"invalid verifier for case {name}")
                verification.append(
                    VerificationCommand(tuple(argv), command_timeout)
                )
            cases.append(
                E2ECase(
                    name=name,
                    difficulty=difficulty,
                    quick=quick,
                    turns=tuple(raw_turns),
                    workspace=workspace,
                    case_dir=case_dir,
                    timeout_seconds=timeout_seconds,
                    allowed_changes=tuple(allowed_changes),
                    verification=tuple(verification),
                    route_request=RouteRequest.from_json(raw),
                )
            )
    if not cases:
        raise ValueError(f"suite {suite!r} selected no cases")
    return cases


def _profile_for_current(request: RouteRequest) -> str:
    for profile_id, profile in request.profiles.items():
        if (
            profile.kind == "route"
            and profile.model == request.current_model
            and profile.effort == request.current_effort
        ):
            return profile_id
    return "keep_current"


def _prepare_jev_request(request: RouteRequest) -> tuple[RouteRequest, str]:
    current_profile = _profile_for_current(request)
    profiles = dict(request.profiles)
    if current_profile != "keep_current":
        profiles.pop(current_profile, None)
    return replace(request, profiles=profiles), current_profile


def _aggregate_probabilities(
    probabilities: dict[str, float], current_profile: str
) -> dict[str, float]:
    aggregated: dict[str, float] = {}
    for profile, probability in probabilities.items():
        final_action = current_profile if profile == "keep_current" else profile
        aggregated[final_action] = aggregated.get(final_action, 0.0) + probability
    return aggregated


def _downgrade_signal_count(
    answer: JevAnswer, choice: str, probabilities: dict[str, float]
) -> int:
    selected = probabilities.get(choice, 0.0)
    runner_up = max(
        (value for profile, value in probabilities.items() if profile != choice),
        default=0.0,
    )
    return sum(
        (
            answer.confidence >= DOWNGRADE_MIN_CONFIDENCE,
            selected >= DOWNGRADE_SELECTED_PROBABILITY,
            selected - runner_up >= DOWNGRADE_PROBABILITY_MARGIN,
        )
    )


def _apply_router_policy(
    routed: RouteRequest,
    current_profile: str,
    answer: JevAnswer,
) -> tuple[str, dict[str, float], str]:
    probabilities = _aggregate_probabilities(answer.probabilities, current_profile)
    if answer.choice == "keep_current":
        return current_profile, probabilities, "keep-current-recommended"
    target_rank = PROFILE_RANK.get(answer.choice)
    current_rank = PROFILE_RANK.get(current_profile)
    if target_rank is None:
        return current_profile, probabilities, "invalid-profile"
    if current_rank is None:
        if answer.confidence >= UPGRADE_MIN_CONFIDENCE:
            return answer.choice, probabilities, "jev-accepted"
        return current_profile, probabilities, "low-confidence-upgrade-rejected"
    if target_rank > current_rank:
        if answer.confidence < UPGRADE_MIN_CONFIDENCE:
            return current_profile, probabilities, "low-confidence-upgrade-rejected"
        return answer.choice, probabilities, "jev-accepted"
    if target_rank < current_rank:
        if _downgrade_signal_count(answer, answer.choice, probabilities) < 2:
            return current_profile, probabilities, "low-confidence-downgrade-rejected"
        context_tokens = routed.metrics.get("context_tokens")
        if (
            isinstance(context_tokens, int)
            and context_tokens > DOWNGRADE_MAX_CONTEXT_TOKENS
        ):
            return current_profile, probabilities, "downgrade-blocked-by-context"
        return answer.choice, probabilities, "jev-accepted"
    return current_profile, probabilities, "keep-current-recommended"


def _resolve_profile(request: RouteRequest, profile_id: str) -> tuple[str, str]:
    if profile_id == "keep_current":
        return request.current_model, request.current_effort
    profile = request.profiles.get(profile_id)
    if profile is None or profile.kind != "route" or not profile.model or not profile.effort:
        if profile_id == _profile_for_current(request):
            return request.current_model, request.current_effort
        raise ValueError(f"profile unavailable: {profile_id}")
    return profile.model, profile.effort


async def _route(
    route_request: RouteRequest, experiment: Experiment, client: JevClient
) -> RouteDecision:
    started = time.perf_counter()
    if experiment.fixed_profile is not None:
        model, effort = _resolve_profile(route_request, experiment.fixed_profile)
        return RouteDecision(
            experiment.fixed_profile,
            model,
            effort,
            None,
            None,
            None,
            "fixed-baseline",
            0,
        )
    request = (
        route_request
        if experiment.execution_aware
        else _prompt_only(route_request)
    )
    if experiment.model_only:
        request = _model_only(request)
    routed, current_profile = _prepare_jev_request(request)
    try:
        answer = await client.route(routed)
        final_profile, probabilities, reason = _apply_router_policy(
            routed, current_profile, answer
        )
        model, effort = _resolve_profile(request, final_profile)
        return RouteDecision(
            final_profile,
            model,
            effort,
            answer.choice,
            answer.confidence,
            probabilities,
            reason,
            round((time.perf_counter() - started) * 1000),
        )
    except Exception as exc:
        model, effort = request.current_model, request.current_effort
        return RouteDecision(
            current_profile,
            model,
            effort,
            None,
            None,
            None,
            "jev-unavailable-keep-current",
            round((time.perf_counter() - started) * 1000),
            f"{type(exc).__name__}:{exc}",
        )


def _snapshot(root: Path) -> dict[str, str]:
    result = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative_path = path.relative_to(root)
        if (
            any(part in IGNORED_WORKSPACE_PARTS for part in relative_path.parts)
            or path.suffix in {".pyc", ".pyo"}
        ):
            continue
        result[relative_path.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )


def _allowed(path: str, patterns: tuple[str, ...]) -> bool:
    return any(Path(path).match(pattern) for pattern in patterns)


def _expand_argv(command: VerificationCommand, case: E2ECase, workspace: Path) -> list[str]:
    values = {
        "python": sys.executable,
        "case_dir": str(case.case_dir),
        "workspace": str(workspace),
    }
    return [value.format_map(values) for value in command.argv]


def _token_usage(events_path: Path) -> dict[str, int]:
    best: dict[str, int] = {}
    if not events_path.exists():
        return best
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, int) and not isinstance(value, bool):
                best[key] = max(best.get(key, 0), value)
    return best


def _thread_id(events: str) -> str | None:
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        thread_id = event.get("thread_id")
        if event.get("type") == "thread.started" and isinstance(thread_id, str):
            return thread_id
    return None


def _usage_delta(
    cumulative: dict[str, int], previous: dict[str, int]
) -> dict[str, int]:
    return {
        key: max(value - previous.get(key, 0), 0)
        for key, value in cumulative.items()
    }


def _run_process(
    argv: list[str], cwd: Path, timeout_seconds: int
) -> tuple[int | None, str, str, bool, int]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        return (
            completed.returncode,
            completed.stdout,
            completed.stderr,
            False,
            round((time.perf_counter() - started) * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else exc.stderr
        return (
            None,
            stdout or "",
            stderr or "",
            True,
            round((time.perf_counter() - started) * 1000),
        )


def _route_record(decision: RouteDecision) -> dict[str, Any]:
    return {
        "profile": decision.profile,
        "model": decision.model,
        "effort": decision.effort,
        "jev_choice": decision.jev_choice,
        "confidence": decision.confidence,
        "probabilities": decision.probabilities,
        "reason": decision.reason,
        "latency_ms": decision.latency_ms,
        "error": decision.error,
    }


async def _execute_case(
    case: E2ECase,
    experiment: Experiment,
    client: JevClient,
    codex: Path,
    run_dir: Path,
) -> dict[str, Any]:
    workspace = run_dir / "workspace"
    shutil.copytree(case.workspace, workspace)
    before = _snapshot(workspace)
    current_model = case.route_request.current_model
    current_effort = case.route_request.current_effort
    previous_usage: dict[str, int] = {}
    thread_id = None
    turns = []
    total_agent_ms = 0
    for turn_number, prompt in enumerate(case.turns, start=1):
        metrics = {}
        initial_context_tokens = case.route_request.metrics.get("context_tokens")
        if isinstance(initial_context_tokens, int):
            metrics["context_tokens"] = initial_context_tokens
        if previous_usage:
            context_tokens = previous_usage.get("input_tokens")
            if context_tokens is not None:
                metrics["context_tokens"] = context_tokens
        route_request = replace(
            case.route_request,
            prompt=prompt,
            current_model=current_model,
            current_effort=current_effort,
            previous_turn_status="unknown",
            metrics=metrics,
        )
        decision = await _route(route_request, experiment, client)
        turn_prefix = f"turn-{turn_number:02d}"
        events_path = run_dir / f"{turn_prefix}-events.jsonl"
        stderr_path = run_dir / f"{turn_prefix}-stderr.txt"
        last_message_path = run_dir / f"{turn_prefix}-last-message.txt"
        common = [
            "--skip-git-repo-check",
            "--json",
            "-m",
            decision.model,
            "-c",
            f'model_reasoning_effort="{decision.effort}"',
            "-c",
            'approval_policy="never"',
            "-c",
            'sandbox_mode="workspace-write"',
            "-o",
            str(last_message_path),
        ]
        if turn_number == 1:
            argv = [
                str(codex),
                "exec",
                "--sandbox",
                "workspace-write",
                "--color",
                "never",
                "-C",
                str(workspace),
                *common,
                prompt,
            ]
        else:
            if thread_id is None:
                break
            argv = [
                str(codex),
                "exec",
                "resume",
                *common,
                thread_id,
                prompt,
            ]
        exit_code, stdout, stderr, timed_out, agent_ms = await asyncio.to_thread(
            _run_process, argv, workspace, case.timeout_seconds
        )
        total_agent_ms += agent_ms
        events_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        if turn_number == 1:
            thread_id = _thread_id(stdout)
        cumulative_usage = _token_usage(events_path)
        delta_usage = _usage_delta(cumulative_usage, previous_usage)
        turns.append(
            {
                "number": turn_number,
                "prompt": prompt,
                "route": _route_record(decision),
                "agent": {
                    "exit_code": exit_code,
                    "timed_out": timed_out,
                    "latency_ms": agent_ms,
                    "token_usage_delta": delta_usage,
                    "token_usage_cumulative": cumulative_usage,
                },
                "artifacts": {
                    "events": str(events_path),
                    "stderr": str(stderr_path),
                    "last_message": str(last_message_path),
                },
            }
        )
        previous_usage = cumulative_usage or previous_usage
        turn_succeeded = exit_code == 0 and not timed_out
        if not turn_succeeded:
            break
        current_model = decision.model
        current_effort = decision.effort
    after = _snapshot(workspace)
    changed = _changed_paths(before, after)
    unexpected = [path for path in changed if not _allowed(path, case.allowed_changes)]
    verification_results = []
    all_turns_completed = len(turns) == len(case.turns)
    last_agent = turns[-1]["agent"] if turns else {}
    exit_code = last_agent.get("exit_code")
    timed_out = bool(last_agent.get("timed_out", False))
    agent_succeeded = all_turns_completed and exit_code == 0 and not timed_out
    if agent_succeeded and not unexpected:
        for verification in case.verification:
            verify_argv = _expand_argv(verification, case, workspace)
            code, verify_stdout, verify_stderr, verify_timeout, elapsed_ms = _run_process(
                verify_argv, workspace, verification.timeout_seconds
            )
            verification_results.append(
                {
                    "argv": verify_argv,
                    "exit_code": code,
                    "timed_out": verify_timeout,
                    "latency_ms": elapsed_ms,
                    "stdout": verify_stdout[-4000:],
                    "stderr": verify_stderr[-4000:],
                }
            )
            if code != 0 or verify_timeout:
                break
    if timed_out:
        outcome = "agent_timeout"
    elif not agent_succeeded:
        outcome = "agent_error"
    elif unexpected:
        outcome = "unexpected_changes"
    elif not verification_results or any(
        result["exit_code"] != 0 or result["timed_out"]
        for result in verification_results
    ):
        outcome = "verification_failed"
    else:
        outcome = "passed"
    return {
        "case": case.name,
        "difficulty": case.difficulty,
        "experiment": experiment.name,
        "outcome": outcome,
        "success": outcome == "passed",
        "turns": turns,
        "route": turns[-1]["route"] if turns else None,
        "agent": {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "latency_ms": total_agent_ms,
            "thread_id": thread_id,
            "completed_turns": len(turns),
            "planned_turns": len(case.turns),
            "token_usage": previous_usage,
        },
        "changed_paths": changed,
        "unexpected_changes": unexpected,
        "verification": verification_results,
        "artifacts": str(run_dir),
    }


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        groups[result["experiment"]].append(result)
    summary = {}
    for experiment, items in groups.items():
        passed = sum(item["success"] for item in items)
        verifiable = sum(
            item["outcome"] in {"passed", "verification_failed", "unexpected_changes"}
            for item in items
        )
        token_totals: dict[str, int] = defaultdict(int)
        for item in items:
            for key, value in item["agent"]["token_usage"].items():
                token_totals[key] += value
        routes = []
        routing_switches = 0
        for item in items:
            item_routes = [turn["route"] for turn in item.get("turns", [])]
            if not item_routes and item.get("route") is not None:
                item_routes = [item["route"]]
            routes.extend(item_routes)
            routing_switches += sum(
                previous["profile"] != current["profile"]
                for previous, current in zip(item_routes, item_routes[1:])
            )
        input_tokens = token_totals.get("input_tokens")
        cached_input_tokens = token_totals.get("cached_input_tokens")
        if input_tokens is not None and cached_input_tokens is not None:
            token_totals["non_cached_input_tokens"] = max(
                input_tokens - cached_input_tokens, 0
            )
        summary[experiment] = {
            "attempted": len(items),
            "passed": passed,
            "strict_success_rate": round(passed / len(items), 4),
            "verifiable_runs": verifiable,
            "conditional_success_rate": (
                round(passed / verifiable, 4) if verifiable else None
            ),
            "outcomes": {
                outcome: sum(item["outcome"] == outcome for item in items)
                for outcome in sorted({item["outcome"] for item in items})
            },
            "mean_agent_latency_ms": round(
                sum(item["agent"]["latency_ms"] for item in items) / len(items)
            ),
            "routing_fallbacks": sum(
                route.get("error") is not None for route in routes
            ),
            "routing_switches": routing_switches,
            "profile_distribution": {
                profile: sum(route["profile"] == profile for route in routes)
                for profile in sorted({route["profile"] for route in routes})
            },
            "token_usage_total": dict(token_totals),
        }
    return summary


def _experiments(value: str) -> list[Experiment]:
    requested = [item.strip() for item in value.split(",") if item.strip()]
    known = {experiment.name: experiment for experiment in EXPERIMENTS}
    unknown = [item for item in requested if item not in known]
    if unknown:
        raise ValueError(f"unknown experiments: {', '.join(unknown)}")
    if not requested:
        raise ValueError("at least one experiment is required")
    return [known[item] for item in requested]


def _merge_results(
    existing: list[dict[str, Any]], replacements: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    def key(item: dict[str, Any]) -> tuple[str, str, int]:
        return (
            str(item["case"]),
            str(item["experiment"]),
            int(item.get("repetition", 1)),
        )

    merged = {key(item): item for item in existing}
    for item in replacements:
        merged[key(item)] = item
    return [merged[item_key] for item_key in sorted(merged)]


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    cases = _read_cases(args.cases, args.suite)
    if args.case:
        requested_cases = set(args.case)
        known_cases = {case.name for case in cases}
        unknown_cases = requested_cases - known_cases
        if unknown_cases:
            raise ValueError(f"unknown cases: {', '.join(sorted(unknown_cases))}")
        cases = [case for case in cases if case.name in requested_cases]
    experiments = _experiments(args.experiments)
    existing_report = None
    if args.merge_existing:
        if not args.output.is_file():
            raise ValueError(
                f"--merge-existing requires an existing report: {args.output}"
            )
        existing_report = json.loads(args.output.read_text(encoding="utf-8"))
        if not isinstance(existing_report, dict) or not isinstance(
            existing_report.get("results"), list
        ):
            raise ValueError(f"invalid existing report: {args.output}")
    planned_runs = len(cases) * len(experiments) * args.repetitions
    turns_per_repetition = sum(len(case.turns) for case in cases)
    planned_turns = turns_per_repetition * len(experiments) * args.repetitions
    if planned_runs > args.max_runs:
        raise ValueError(
            f"planned {planned_runs} Codex runs exceeds --max-runs {args.max_runs}; "
            "raise the limit explicitly to confirm the additional model usage"
        )
    if planned_turns > args.max_turns:
        raise ValueError(
            f"planned {planned_turns} Codex turns exceeds --max-turns "
            f"{args.max_turns}; raise the limit explicitly to confirm the "
            "additional model usage"
        )
    planned_jev_calls = (
        turns_per_repetition
        * sum(experiment.fixed_profile is None for experiment in experiments)
        * args.repetitions
    )
    print(
        f"Plan: {planned_runs} Codex sessions, {planned_turns} Codex turns, "
        f"{planned_jev_calls} Jev calls, "
        f"{len(cases)} cases x {len(experiments)} experiments x "
        f"{args.repetitions} repetitions",
        flush=True,
    )
    if args.dry_run:
        return {
            "schema_version": 1,
            "dry_run": True,
            "suite": args.suite,
            "cases": [case.name for case in cases],
            "experiments": [experiment.name for experiment in experiments],
            "repetitions": args.repetitions,
            "planned_codex_runs": planned_runs,
            "planned_codex_turns": planned_turns,
            "planned_jev_calls": planned_jev_calls,
        }
    args.artifacts.mkdir(parents=True, exist_ok=True)
    session_name = time.strftime("run-%Y%m%d-%H%M%S")
    session_dir = args.artifacts / session_name
    suffix = 1
    while session_dir.exists():
        session_dir = args.artifacts / f"{session_name}-{suffix}"
        suffix += 1
    session_dir.mkdir()
    results = []
    async with JevClient() as client:
        for repetition in range(1, args.repetitions + 1):
            for case in cases:
                for experiment in experiments:
                    run_dir = (
                        session_dir
                        / f"r{repetition:02d}-{case.name}-{experiment.name}"
                    )
                    run_dir.mkdir(parents=True, exist_ok=False)
                    result = await _execute_case(
                        case,
                        experiment,
                        client,
                        args.codex,
                        run_dir,
                    )
                    result["repetition"] = repetition
                    results.append(result)
                    print(
                        f"{case.name} [{experiment.name}] {result['outcome']}",
                        flush=True,
                    )
    report = {
        "schema_version": 1,
        "suite": args.suite,
        "cases": [case.name for case in cases],
        "experiments": [experiment.name for experiment in experiments],
        "repetitions": args.repetitions,
        "planned_codex_runs": planned_runs,
        "planned_codex_turns": planned_turns,
        "planned_jev_calls": planned_jev_calls,
        "artifact_root": str(session_dir),
        "results": results,
        "summary": _summarize(results),
    }
    if existing_report is not None:
        results = _merge_results(existing_report["results"], results)
        artifact_roots = list(existing_report.get("artifact_roots", []))
        previous_root = existing_report.get("artifact_root")
        if isinstance(previous_root, str) and previous_root not in artifact_roots:
            artifact_roots.append(previous_root)
        if str(session_dir) not in artifact_roots:
            artifact_roots.append(str(session_dir))
        experiment_by_name = {experiment.name: experiment for experiment in EXPERIMENTS}
        report = {
            "schema_version": 1,
            "suite": existing_report.get("suite", args.suite),
            "cases": sorted({item["case"] for item in results}),
            "experiments": sorted({item["experiment"] for item in results}),
            "repetitions": max(int(item.get("repetition", 1)) for item in results),
            "planned_codex_runs": len(results),
            "planned_codex_turns": sum(
                int(item["agent"].get("planned_turns", 1)) for item in results
            ),
            "planned_jev_calls": sum(
                len(item.get("turns", []))
                for item in results
                if experiment_by_name[item["experiment"]].fixed_profile is None
            ),
            "artifact_root": str(session_dir),
            "artifact_roots": artifact_roots,
            "last_run": {
                "cases": [case.name for case in cases],
                "experiments": [experiment.name for experiment in experiments],
                "repetitions": args.repetitions,
                "planned_codex_runs": planned_runs,
                "planned_codex_turns": planned_turns,
                "planned_jev_calls": planned_jev_calls,
            },
            "results": results,
            "summary": _summarize(results),
        }
    _write_report(args.output, report)
    return report


def run() -> None:
    parser = argparse.ArgumentParser(
        description="Run deterministic end-to-end Codex routing benchmarks."
    )
    package_cases = Path(__file__).resolve().parent / "bundled_e2e" / "cases.jsonl"
    source_cases = Path(__file__).resolve().parent.parent / "examples" / "e2e" / "cases.jsonl"
    default_cases = package_cases if package_cases.exists() else source_cases
    parser.add_argument("--cases", type=Path, default=default_cases)
    parser.add_argument(
        "--case",
        action="append",
        help="Run only this case name; repeat the option to select multiple cases",
    )
    parser.add_argument("--suite", choices=("quick", "full"), default="quick")
    parser.add_argument(
        "--experiments", default=",".join(DEFAULT_EXPERIMENTS)
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument(
        "--max-runs",
        type=int,
        default=12,
        help="Safety cap for paid Codex executions (default: 12)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=20,
        help="Safety cap for paid Codex turns across resumed sessions (default: 20)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the execution plan without calling Jev or Codex",
    )
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, default=Path("e2e-artifacts"))
    parser.add_argument("--output", type=Path, default=Path("e2e-report.json"))
    parser.add_argument(
        "--merge-existing",
        action="store_true",
        help="Atomically replace matching case/experiment/repetition results in --output",
    )
    args = parser.parse_args()
    if args.repetitions <= 0:
        parser.error("--repetitions must be positive")
    if args.max_runs <= 0:
        parser.error("--max-runs must be positive")
    if args.max_turns <= 0:
        parser.error("--max-turns must be positive")
    args.codex = args.codex.resolve()
    args.cases = args.cases.resolve()
    args.artifacts = args.artifacts.resolve()
    args.output = args.output.resolve()
    if not args.codex.is_file():
        parser.error(f"Codex executable not found: {args.codex}")
    try:
        report = asyncio.run(_run(args))
    except ValueError as exc:
        parser.error(str(exc))
    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
