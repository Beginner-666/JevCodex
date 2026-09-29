import unittest
from pathlib import Path

from jev_codex_router.e2e_benchmark import _aggregate_probabilities
from jev_codex_router.e2e_benchmark import _apply_router_policy
from jev_codex_router.e2e_benchmark import _merge_results
from jev_codex_router.e2e_benchmark import _prepare_jev_request
from jev_codex_router.e2e_benchmark import _read_cases
from jev_codex_router.e2e_benchmark import _summarize
from jev_codex_router.e2e_benchmark import _thread_id
from jev_codex_router.e2e_benchmark import _usage_delta
from jev_codex_router.jev import JevAnswer
from jev_codex_router.protocol import RouteRequest


def request() -> RouteRequest:
    return RouteRequest.from_json(
        {
            "id": 1,
            "type": "route",
            "prompt": "small task",
            "current_model": "gpt-5.6-sol",
            "current_effort": "medium",
            "previous_turn_status": "unknown",
            "metrics": {"context_tokens": 1000},
            "profiles": {
                "keep_current": {"kind": "keep"},
                "luna_medium": {
                    "model": "gpt-5.6-luna",
                    "effort": "medium",
                },
                "sol_medium": {
                    "model": "gpt-5.6-sol",
                    "effort": "medium",
                },
                "sol_high": {
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                },
            },
        }
    )


class E2EBenchmarkTests(unittest.TestCase):
    def test_bundled_quick_suite_has_difficulty_gradient(self) -> None:
        cases_path = (
            Path(__file__).resolve().parents[1] / "examples" / "e2e" / "cases.jsonl"
        )
        cases = _read_cases(cases_path, "quick")
        self.assertEqual(
            [(case.name, case.difficulty) for case in cases],
            [
                ("slugify", "easy"),
                ("sliding_window", "medium"),
                ("atomic_order", "hard"),
            ],
        )
        self.assertEqual([len(case.turns) for case in cases], [2, 2, 2])

    def test_request_keeps_keep_current_and_removes_duplicate_current(self) -> None:
        routed, current = _prepare_jev_request(request())
        self.assertEqual(current, "sol_medium")
        self.assertEqual(
            set(routed.profiles), {"keep_current", "luna_medium", "sol_high"}
        )

    def test_downgrade_accepts_probability_and_margin_without_confidence(self) -> None:
        routed, current = _prepare_jev_request(request())
        answer = JevAnswer(
            "luna_medium",
            0.64,
            {"keep_current": 0.29, "luna_medium": 0.71, "sol_high": 0.0},
        )
        final_profile, probabilities, reason = _apply_router_policy(
            routed, current, answer
        )
        self.assertEqual(
            (final_profile, probabilities, reason),
            (
                "luna_medium",
                {"sol_medium": 0.29, "luna_medium": 0.71, "sol_high": 0.0},
                "jev-accepted",
            ),
        )

    def test_probability_aggregation_merges_duplicate_final_action(self) -> None:
        probabilities = _aggregate_probabilities(
            {"keep_current": 0.28, "luna_medium": 0.71, "sol_medium": 0.01},
            "sol_medium",
        )
        self.assertEqual(set(probabilities), {"sol_medium", "luna_medium"})
        self.assertAlmostEqual(probabilities["sol_medium"], 0.29)
        self.assertAlmostEqual(probabilities["luna_medium"], 0.71)

    def test_resumed_turn_usage_is_reported_as_delta(self) -> None:
        self.assertEqual(
            _usage_delta(
                {"input_tokens": 180, "cached_input_tokens": 80, "output_tokens": 30},
                {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10},
            ),
            {"input_tokens": 80, "cached_input_tokens": 60, "output_tokens": 20},
        )

    def test_thread_id_is_read_from_jsonl(self) -> None:
        events = "\n".join(
            (
                '{"type":"thread.started","thread_id":"thread-123"}',
                '{"type":"turn.completed","usage":{"input_tokens":12}}',
            )
        )
        self.assertEqual(_thread_id(events), "thread-123")

    def test_merge_replaces_only_matching_case_experiment_and_repetition(self) -> None:
        existing = [
            {"case": "a", "experiment": "x", "repetition": 1, "value": "old"},
            {"case": "b", "experiment": "x", "repetition": 1, "value": "keep"},
        ]
        replacements = [
            {"case": "a", "experiment": "x", "repetition": 1, "value": "new"}
        ]
        self.assertEqual(
            _merge_results(existing, replacements),
            [
                {"case": "a", "experiment": "x", "repetition": 1, "value": "new"},
                {"case": "b", "experiment": "x", "repetition": 1, "value": "keep"},
            ],
        )

    def test_summary_separates_strict_and_conditional_success(self) -> None:
        results = [
            {
                "experiment": "x",
                "success": True,
                "outcome": "passed",
                "agent": {"latency_ms": 10, "token_usage": {}},
                "route": {"profile": "luna_medium", "error": None},
            },
            {
                "experiment": "x",
                "success": False,
                "outcome": "verification_failed",
                "agent": {"latency_ms": 20, "token_usage": {}},
                "route": {"profile": "luna_medium", "error": None},
            },
            {
                "experiment": "x",
                "success": False,
                "outcome": "agent_timeout",
                "agent": {"latency_ms": 30, "token_usage": {}},
                "route": {"profile": "sol_medium", "error": "timeout"},
            },
        ]
        self.assertEqual(
            _summarize(results),
            {
                "x": {
                    "attempted": 3,
                    "passed": 1,
                    "strict_success_rate": 0.3333,
                    "verifiable_runs": 2,
                    "conditional_success_rate": 0.5,
                    "outcomes": {
                        "agent_timeout": 1,
                        "passed": 1,
                        "verification_failed": 1,
                    },
                    "mean_agent_latency_ms": 20,
                    "routing_fallbacks": 1,
                    "routing_switches": 0,
                    "profile_distribution": {"luna_medium": 2, "sol_medium": 1},
                    "token_usage_total": {},
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
