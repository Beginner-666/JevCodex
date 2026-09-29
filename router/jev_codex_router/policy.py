from typing import Any

from .protocol import RouteRequest


ROUTING_RULES = [
    "The minimum allowed profile is GPT-5.6 Luna with medium reasoning.",
    "The maximum allowed profile is GPT-5.6 Sol with high reasoning.",
    "Choose keep_current when changing profile is unlikely to provide meaningful benefit.",
    "Use Luna medium for simple, local, mechanical, low-risk, or highly specified tasks.",
    "Use Luna high, when supplied, for moderately complex implementation, ordinary debugging, testing, or refactoring.",
    "Use Sol medium for difficult debugging, broader repository reasoning, ambiguity, multi-component interactions, or non-trivial design work.",
    "Use Sol high only for the hardest root-cause analysis, architecture-level work, highly ambiguous multi-step reasoning, or tasks where mistakes are expensive.",
    "Return exactly one of the supplied criteria options.",
]


def build_state(request: RouteRequest) -> dict[str, Any]:
    return {
        "user_prompt": request.prompt,
        "current": {
            "model": request.current_model,
            "reasoning_effort": request.current_effort,
        },
        "previous_turn": {"status": request.previous_turn_status},
        "execution": dict(request.metrics),
    }


def build_instructions() -> str:
    rules = " ".join(ROUTING_RULES)
    return (
        "Choose the most appropriate Codex routing profile for the next new turn. "
        "Use the lowest sufficient profile within the allowed range while preserving "
        f"a high probability of completing the task correctly. {rules}"
    )


def build_criteria(request: RouteRequest) -> dict[str, str]:
    criteria: dict[str, str] = {}
    for profile_id, profile in request.profiles.items():
        if profile.kind == "keep":
            criteria[profile_id] = "Keep the current Codex model and reasoning effort."
            continue
        details = [
            f"Use model {profile.model} with {profile.effort} reasoning effort."
        ]
        details.extend(
            value
            for value in (
                profile.routing_semantics,
                profile.model_description,
                profile.effort_description,
            )
            if value
        )
        criteria[profile_id] = " ".join(details)
    return criteria
