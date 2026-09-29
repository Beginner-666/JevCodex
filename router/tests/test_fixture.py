import json
import unittest
from pathlib import Path


class FixtureTests(unittest.TestCase):
    def test_openrouter_decision_choice_shape(self) -> None:
        path = Path(__file__).parent / "fixtures" / "vercel_evaluation_choice.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        answer = payload["answers"]["route"]
        confidence = answer["confidence"]
        self.assertEqual(
            (answer["choice"], answer["probabilities"], confidence),
            ("sol_medium", {"luna_medium": 0.1, "sol_medium": 0.9}, 0.8),
        )


if __name__ == "__main__":
    unittest.main()
