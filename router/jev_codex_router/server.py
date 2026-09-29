import asyncio
import json
import sys
import time
from typing import Any

from .jev import JevClient
from .protocol import RouteRequest, RouteResponse


async def handle(client: JevClient, raw: Any) -> RouteResponse:
    request_id = raw.get("id") if isinstance(raw, dict) else None
    started = time.monotonic()
    try:
        request = RouteRequest.from_json(raw)
        answer = await client.route(request)
        latency_ms = round((time.monotonic() - started) * 1000)
        return RouteResponse(
            id=request.id,
            ok=True,
            choice=answer.choice,
            confidence=answer.confidence,
            probabilities=answer.probabilities,
            latency_ms=latency_ms,
        )
    except Exception as exc:
        print(f"jev router request failed: {type(exc).__name__}", file=sys.stderr)
        return RouteResponse(id=request_id, ok=False, error=type(exc).__name__)


async def serve(client: JevClient) -> None:
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            return
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            response = RouteResponse(id=None, ok=False, error="JSONDecodeError")
        else:
            response = await handle(client, raw)
        print(json.dumps(response.to_json(), separators=(",", ":")), flush=True)


async def main() -> None:
    try:
        async with JevClient() as client:
            await serve(client)
    except Exception as exc:
        # stdout is reserved exclusively for NDJSON protocol messages.
        print(f"jev router startup failed: {type(exc).__name__}", file=sys.stderr)


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
