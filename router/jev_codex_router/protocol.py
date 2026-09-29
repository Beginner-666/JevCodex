from dataclasses import dataclass
from typing import Any


class ProtocolError(ValueError):
    """Raised when an NDJSON message violates the router protocol."""


@dataclass(frozen=True)
class RouteProfile:
    kind: str
    model: str | None = None
    effort: str | None = None
    model_description: str | None = None
    effort_description: str | None = None
    routing_semantics: str | None = None

    @classmethod
    def from_json(cls, value: Any) -> "RouteProfile":
        if not isinstance(value, dict):
            raise ProtocolError("profile must be an object")
        kind = value.get("kind", "route")
        if kind == "keep":
            return cls(kind="keep")
        if kind != "route":
            raise ProtocolError("profile kind must be 'keep' or 'route'")
        model = value.get("model")
        effort = value.get("effort")
        if not isinstance(model, str) or not model:
            raise ProtocolError("routed profile requires a non-empty model")
        if not isinstance(effort, str) or not effort:
            raise ProtocolError("routed profile requires a non-empty effort")
        return cls(
            kind=kind,
            model=model,
            effort=effort,
            model_description=_optional_string(value, "model_description"),
            effort_description=_optional_string(value, "effort_description"),
            routing_semantics=_optional_string(value, "routing_semantics"),
        )


@dataclass(frozen=True)
class RouteRequest:
    id: int
    prompt: str
    current_model: str
    current_effort: str
    previous_turn_status: str
    metrics: dict[str, Any]
    profiles: dict[str, RouteProfile]

    @classmethod
    def from_json(cls, value: Any) -> "RouteRequest":
        if not isinstance(value, dict):
            raise ProtocolError("request must be an object")
        request_id = value.get("id")
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            raise ProtocolError("id must be an integer")
        if value.get("type") != "route":
            raise ProtocolError("type must be 'route'")
        prompt = _required_string(value, "prompt")
        current_model = _required_string(value, "current_model")
        current_effort = _required_string(value, "current_effort")
        previous_turn_status = value.get("previous_turn_status", "unknown")
        if previous_turn_status not in {"success", "failed", "unknown"}:
            raise ProtocolError("invalid previous_turn_status")
        metrics = value.get("metrics", {})
        if not isinstance(metrics, dict):
            raise ProtocolError("metrics must be an object")
        _validate_metrics(metrics)
        raw_profiles = value.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise ProtocolError("profiles must be a non-empty object")
        profiles = {
            _profile_id(profile_id): RouteProfile.from_json(profile)
            for profile_id, profile in raw_profiles.items()
        }
        if "keep_current" not in profiles:
            raise ProtocolError("profiles must contain keep_current")
        return cls(
            id=request_id,
            prompt=prompt,
            current_model=current_model,
            current_effort=current_effort,
            previous_turn_status=previous_turn_status,
            metrics=metrics,
            profiles=profiles,
        )


@dataclass(frozen=True)
class RouteResponse:
    id: int | None
    ok: bool
    choice: str | None = None
    confidence: float | None = None
    probabilities: dict[str, float] | None = None
    latency_ms: int | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {"id": self.id, "ok": self.ok}
        if self.ok:
            value.update(
                choice=self.choice,
                confidence=self.confidence,
                probabilities=self.probabilities,
                latency_ms=self.latency_ms,
            )
        else:
            value["error"] = self.error or "unknown"
        return value


def _profile_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ProtocolError("profile ids must be non-empty strings up to 64 characters")
    return value


def _required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ProtocolError(f"{key} must be a string")
    return item


def _optional_string(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is not None and not isinstance(item, str):
        raise ProtocolError(f"{key} must be a string or null")
    return item


def _validate_metrics(metrics: dict[str, Any]) -> None:
    integer_fields = (
        "context_tokens",
        "failure_count",
        "files_touched",
        "diff_lines",
        "tool_count",
    )
    for key in integer_fields:
        item = metrics.get(key)
        if item is not None and (
            not isinstance(item, int) or isinstance(item, bool) or item < 0
        ):
            raise ProtocolError(f"metrics.{key} must be a non-negative integer or null")
    previous_turn_failed = metrics.get("previous_turn_failed", False)
    if not isinstance(previous_turn_failed, bool):
        raise ProtocolError("metrics.previous_turn_failed must be a boolean")
