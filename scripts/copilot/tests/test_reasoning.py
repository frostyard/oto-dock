"""Reasoning controls expose reviewed literals without changing provider defaults."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.catalog import CopilotCatalogError, normalize_models  # noqa: E402
from core.layers.copilot.reasoning import REASONING_EFFORTS, valid_reasoning_effort  # noqa: E402


def inventory(**fields):
    return {"models": [{"id": "model", "name": "Model", "capabilities": {
        "supports": {"reasoningEffort": True}}, **fields}]}


@pytest.mark.parametrize("value", [None, "low", "medium", "high", "xhigh", "max"])
def test_reviewed_effort_values(value):
    assert valid_reasoning_effort(value)


@pytest.mark.parametrize("value", ["", "HIGH", " high", "minimal", True, False, 0, 1, [], {}, ["high"]])
def test_unreviewed_effort_values_fail_closed(value):
    assert not valid_reasoning_effort(value)


def test_known_levels_and_default_are_projected_without_other_metadata():
    row = normalize_models(inventory(supportedReasoningEfforts=["low", "medium", "high", "xhigh", "max"],
                                     defaultReasoningEffort="medium"))[0]
    assert set(row["reasoning_efforts"]) == REASONING_EFFORTS
    assert row["default_reasoning_effort"] == "medium"
    assert "capabilities" not in row


@pytest.mark.parametrize("default,expected", [("future", None), ("high", "high")])
def test_future_levels_are_never_selectable(default, expected):
    row = normalize_models(inventory(supportedReasoningEfforts=["low", "future", "high"],
                                     defaultReasoningEffort=default))[0]
    assert row["reasoning_efforts"] == ["low", "high"]
    assert row["default_reasoning_effort"] == expected


@pytest.mark.parametrize("capabilities", [{}, {"supports": {}}, {"supports": {"reasoningEffort": False}},
                                           {"supports": {"reasoningEffort": True}}])
def test_absent_advertisement_leaves_only_provider_default(capabilities):
    row = normalize_models(inventory(capabilities=capabilities))[0]
    assert row["reasoning_efforts"] == [] and row["default_reasoning_effort"] is None
    assert row["available"]


@pytest.mark.parametrize("fields", [
    {"capabilities": {"supports": None}}, {"capabilities": {"supports": {"reasoningEffort": "true"}}},
    {"capabilities": {"supports": {"reasoningEffort": 1}}},
    {"capabilities": {}, "supportedReasoningEfforts": ["high"]},
    {"capabilities": {"supports": {"reasoningEffort": False}}, "supportedReasoningEfforts": ["high"]},
    {"supportedReasoningEfforts": None}, {"supportedReasoningEfforts": "high"},
    {"supportedReasoningEfforts": ["high", "high"]}, {"supportedReasoningEfforts": [1]},
    {"supportedReasoningEfforts": ["bad\nlevel"]}, {"supportedReasoningEfforts": ["x" * 257]},
    {"supportedReasoningEfforts": [str(index) for index in range(33)]},
    {"supportedReasoningEfforts": ["high"], "defaultReasoningEffort": "low"},
    {"defaultReasoningEffort": "high"}, {"defaultReasoningEffort": None},
    {"supportedReasoningEfforts": ["high"], "defaultReasoningEffort": True},
])
def test_malformed_or_contradictory_metadata_rejects_inventory(fields):
    with pytest.raises(CopilotCatalogError):
        normalize_models(inventory(**fields))
