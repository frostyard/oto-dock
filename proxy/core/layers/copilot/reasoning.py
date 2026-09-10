"""Reviewed SDK reasoning controls; absence preserves the provider's default."""

REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def valid_reasoning_effort(value) -> bool:
    return value is None or (type(value) is str and value in REASONING_EFFORTS)


def model_reasoning(model) -> tuple[list[str], str | None]:
    """Validate raw model metadata, exposing only reviewed effort literals."""
    supports = model["capabilities"].get("supports", {})
    if type(supports) is not dict or type(supports.get("reasoningEffort", False)) is not bool:
        raise ValueError("Invalid Copilot reasoning metadata")
    enabled = supports.get("reasoningEffort", False)
    levels = model.get("supportedReasoningEfforts", [])
    if (type(levels) is not list or len(levels) > 32
            or any(type(level) is not str or not 0 < len(level) <= 256
                   or level != level.strip() or not level.isprintable() for level in levels)
            or len(set(levels)) != len(levels)):
        raise ValueError("Invalid Copilot reasoning metadata")
    default = model.get("defaultReasoningEffort")
    if ("defaultReasoningEffort" in model and (type(default) is not str or default not in levels)):
        raise ValueError("Invalid Copilot reasoning metadata")
    if not enabled and (levels or default is not None):
        raise ValueError("Contradictory Copilot reasoning metadata")
    return [level for level in levels if level in REASONING_EFFORTS], default if default in REASONING_EFFORTS else None
