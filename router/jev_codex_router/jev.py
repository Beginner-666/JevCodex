import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import os
from typing import Any

import httpx

from .policy import build_criteria, build_instructions, build_state
from .protocol import RouteRequest


DEFAULT_GATEWAY_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_GATEWAY_MODEL = "~typesafe/jev-latest"


@dataclass(frozen=True)
class JevAnswer:
    choice: str
    confidence: float
    probabilities: dict[str, float]


class JevClient:
    """Async adapter for Jev's native OpenRouter Decisions API."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        configured_model = os.environ.get(
            "JEV_CODEX_ROUTER_MODEL", DEFAULT_GATEWAY_MODEL
        )
        # Keep existing router installations working after the provider switch.
        self._model = (
            DEFAULT_GATEWAY_MODEL
            if configured_model == "typesafe-ai/jev"
            else configured_model
        )
        self._url = os.environ.get("JEV_CODEX_ROUTER_URL", DEFAULT_GATEWAY_URL)
        attempt_timeout_ms = int(
            os.environ.get("JEV_CODEX_ROUTER_ATTEMPT_TIMEOUT_MS", "1200")
        )
        total_deadline_ms = int(
            os.environ.get("JEV_CODEX_ROUTER_TOTAL_DEADLINE_MS", "2500")
        )
        self._attempt_timeout_seconds = max(attempt_timeout_ms, 1) / 1000
        self._total_deadline_seconds = max(total_deadline_ms, 1) / 1000
        self._max_retries = max(
            int(os.environ.get("JEV_CODEX_ROUTER_MAX_RETRIES", "1")), 0
        )

    async def __aenter__(self) -> "JevClient":
        # Keep startup fail-open. The HTTP client and credential are only needed when
        # Codex submits a new turn that actually reaches the router.
        return self

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._client is not None:
            await self._client.aclose()

    @staticmethod
    def _authorization() -> str:
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if api_key:
            return api_key
        raise RuntimeError("missing_openrouter_api_key")

    async def route(self, request: RouteRequest) -> JevAnswer:
        credential = self._authorization()
        client = await self._ensure_client()
        body = {
            "model": self._model,
            "state": build_state(request),
            "questions": {
                "route": {
                    "type": "choice",
                    "instructions": build_instructions(),
                    "criteria": build_criteria(request),
                }
            },
        }
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
        }
        response: httpx.Response | None = None
        last_error: BaseException | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._total_deadline_seconds
        for attempt in range(self._max_retries + 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("total_deadline_exceeded") from last_error
            attempt_budget = min(self._attempt_timeout_seconds, remaining)
            try:
                response = await asyncio.wait_for(
                    client.post(
                        self._url,
                        json=body,
                        headers=headers,
                        timeout=httpx.Timeout(attempt_budget),
                    ),
                    timeout=attempt_budget,
                )
                response.raise_for_status()
                break
            except Exception as exc:
                last_error = exc
                if attempt == self._max_retries or not _retryable(exc):
                    raise
        if response is None:
            raise TimeoutError("total_deadline_exceeded") from last_error
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("OpenRouter returned a non-object response")
        answers = payload.get("answers")
        if not isinstance(answers, Mapping) or "route" not in answers:
            raise ValueError("OpenRouter response did not contain route answer")
        answer = answers["route"]
        if not isinstance(answer, Mapping) or answer.get("type") != "choice":
            raise ValueError("OpenRouter returned a non-choice route answer")
        choice = answer.get("choice")
        raw_probabilities = answer.get("probabilities")
        if not isinstance(choice, str) or not isinstance(raw_probabilities, Mapping):
            raise ValueError("OpenRouter returned an incomplete route answer")
        probabilities = {
            str(key): float(value) for key, value in raw_probabilities.items()
        }
        confidence = _route_confidence(answer)
        if choice not in request.profiles:
            raise ValueError("OpenRouter returned an unknown profile")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("OpenRouter returned invalid confidence")
        if set(probabilities) != set(request.profiles):
            raise ValueError("OpenRouter returned an incomplete probability distribution")
        return JevAnswer(choice, confidence, probabilities)


def _route_confidence(answer: Mapping[str, Any]) -> float:
    confidence = answer.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ValueError("OpenRouter response omitted route confidence")
    return float(confidence)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, httpx.RequestError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 409, 429} or exc.response.status_code >= 500
    return False
