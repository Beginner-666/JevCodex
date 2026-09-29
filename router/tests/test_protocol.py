import unittest

from jev_codex_router.policy import build_criteria, build_state
from jev_codex_router.protocol import ProtocolError, RouteRequest


def request_json() -> dict:
    return {
        "id": 42,
        "type": "route",
        "prompt": "Fix the typo",
        "current_model": "gpt-5.6-sol",
        "current_effort": "medium",
        "previous_turn_status": "success",
        "metrics": {
            "context_tokens": 8400,
            "previous_turn_failed": False,
            "failure_count": 0,
        },
        "profiles": {
            "keep_current": {"kind": "keep"},
            "luna_medium": {
                "kind": "route",
                "model": "gpt-5.6-luna",
                "effort": "medium",
            },
        },
    }


class ProtocolTests(unittest.TestCase):
    def test_parses_route_request_and_builds_typed_choice_inputs(self) -> None:
        request = RouteRequest.from_json(request_json())
        self.assertEqual(
            build_state(request),
            {
                "user_prompt": "Fix the typo",
                "current": {
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "medium",
                },
                "previous_turn": {"status": "success"},
                "execution": {
                    "context_tokens": 8400,
                    "previous_turn_failed": False,
                    "failure_count": 0,
                },
            },
        )
        self.assertEqual(
            build_criteria(request)["keep_current"],
            "Keep the current Codex model and reasoning effort.",
        )
        self.assertIsInstance(build_criteria(request)["luna_medium"], str)

    def test_rejects_missing_keep_current(self) -> None:
        raw = request_json()
        del raw["profiles"]["keep_current"]
        with self.assertRaisesRegex(ProtocolError, "keep_current"):
            RouteRequest.from_json(raw)


if __name__ == "__main__":
    unittest.main()
