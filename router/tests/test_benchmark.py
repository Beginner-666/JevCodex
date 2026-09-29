import asyncio
import unittest

from jev_codex_router.benchmark import EXPERIMENTS, _model_only, _prompt_only
from jev_codex_router.benchmark import benchmark_requests
from jev_codex_router.jev import JevAnswer
from jev_codex_router.protocol import RouteRequest


def request() -> RouteRequest:
    return RouteRequest.from_json(
        {
            "id": 7,
            "type": "route",
            "prompt": "Fix the failing integration test",
            "current_model": "gpt-5.6-luna",
            "current_effort": "medium",
            "previous_turn_status": "failed",
            "metrics": {
                "context_tokens": 1234,
                "previous_turn_failed": True,
                "failure_count": 2,
                "files_touched": 3,
                "diff_lines": 80,
                "tool_count": 4,
            },
            "profiles": {
                "keep_current": {"kind": "keep"},
                "luna_medium": {
                    "model": "gpt-5.6-luna",
                    "effort": "medium",
                },
                "luna_high": {"model": "gpt-5.6-luna", "effort": "high"},
                "sol_medium": {"model": "gpt-5.6-sol", "effort": "medium"},
                "sol_high": {"model": "gpt-5.6-sol", "effort": "high"},
            },
        }
    )


class FakeClient:
    def __init__(self) -> None:
        self.requests = []

    async def route(self, item: RouteRequest) -> JevAnswer:
        self.requests.append(item)
        probabilities = {key: 0.0 for key in item.profiles}
        probabilities["keep_current"] = 1.0
        return JevAnswer("keep_current", 1.0, probabilities)


class BenchmarkTests(unittest.TestCase):
    def test_model_only_filters_effort_while_prompt_only_filters_execution(self) -> None:
        original = request()
        model_only = _model_only(original)
        self.assertEqual(
            set(model_only.profiles),
            {"keep_current", "luna_medium", "sol_medium"},
        )
        prompt_only = _prompt_only(original)
        self.assertEqual(prompt_only.previous_turn_status, "unknown")
        self.assertEqual(prompt_only.metrics, {"context_tokens": 1234})

    def test_runner_emits_all_six_experiments(self) -> None:
        async def collect():
            client = FakeClient()
            results = [
                result
                async for result in benchmark_requests([("case-1", request())], client)
            ]
            return client, results

        client, results = asyncio.run(collect())
        self.assertEqual(
            [item["experiment"] for item in results],
            [experiment.name for experiment in EXPERIMENTS],
        )
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(
            set(client.requests[0].profiles),
            {"keep_current", "luna_medium", "sol_medium"},
        )
        self.assertEqual(client.requests[2].previous_turn_status, "failed")


if __name__ == "__main__":
    unittest.main()
