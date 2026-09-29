import asyncio
import json
import os
import time
import unittest
from unittest.mock import patch

import httpx

from jev_codex_router.jev import DEFAULT_GATEWAY_MODEL, DEFAULT_GATEWAY_URL, JevClient
from jev_codex_router.protocol import RouteRequest

from test_protocol import request_json


def gateway_response() -> dict:
    return {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {
            "route": {
                "type": "choice",
                "choice": "luna_medium",
                "confidence": 0.8,
                "probabilities": {"keep_current": 0.1, "luna_medium": 0.9},
            }
        },
    }


class JevTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_openrouter_decision_request(self) -> None:
        seen = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=gateway_response())

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False):
            adapter = JevClient()
            adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                answer = await adapter.route(RouteRequest.from_json(request_json()))
            finally:
                await adapter._client.aclose()

        self.assertEqual(answer.choice, "luna_medium")
        self.assertEqual(answer.confidence, 0.8)
        self.assertEqual(
            answer.probabilities,
            {"keep_current": 0.1, "luna_medium": 0.9},
        )
        self.assertEqual(len(seen), 1)
        self.assertEqual(str(seen[0].url), DEFAULT_GATEWAY_URL)
        self.assertEqual(seen[0].headers["authorization"], "Bearer test-key")
        self.assertNotIn("ai-gateway-auth-method", seen[0].headers)
        body = json.loads(seen[0].content)
        self.assertEqual(body["model"], DEFAULT_GATEWAY_MODEL)
        self.assertEqual(body["questions"]["route"]["type"], "choice")
        self.assertIsInstance(body["questions"]["route"]["instructions"], str)
        self.assertEqual(
            set(body["questions"]["route"]["criteria"]),
            {"keep_current", "luna_medium"},
        )
        self.assertTrue(
            all(
                isinstance(value, str)
                for value in body["questions"]["route"]["criteria"].values()
            )
        )

    async def test_retries_retryable_gateway_status_only(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(529, json={"error": "overloaded"})
            return httpx.Response(200, json=gateway_response())

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False):
            adapter = JevClient()
            adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                answer = await adapter.route(RouteRequest.from_json(request_json()))
            finally:
                await adapter._client.aclose()
        self.assertEqual(answer.choice, "luna_medium")
        self.assertEqual(calls, 2)

    async def test_client_initialization_is_lazy(self) -> None:
        adapter = JevClient()
        async with adapter:
            self.assertIsNone(adapter._client)

    async def test_missing_key_fails_only_when_routing(self) -> None:
        with patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": ""},
            clear=False,
        ):
            adapter = JevClient()
            async with adapter:
                self.assertIsNone(adapter._client)
                with self.assertRaisesRegex(
                    RuntimeError, "missing_openrouter_api_key"
                ):
                    await adapter.route(RouteRequest.from_json(request_json()))

    async def test_total_deadline_caps_all_retries(self) -> None:
        class SlowClient:
            def __init__(self) -> None:
                self.calls = 0

            async def post(self, *args, **kwargs):
                self.calls += 1
                await asyncio.sleep(1)

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False):
            adapter = JevClient()
            adapter._client = SlowClient()
            adapter._attempt_timeout_seconds = 0.02
            adapter._total_deadline_seconds = 0.05
            adapter._max_retries = 10
            started = time.monotonic()

            with self.assertRaises((TimeoutError, asyncio.TimeoutError)):
                await adapter.route(RouteRequest.from_json(request_json()))

        self.assertLess(time.monotonic() - started, 0.15)
        self.assertLessEqual(adapter._client.calls, 3)


if __name__ == "__main__":
    unittest.main()
