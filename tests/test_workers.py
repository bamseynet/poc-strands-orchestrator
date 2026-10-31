"""The specialists' definitions, which the orchestrator's choice depends on."""

import pytest
from strands import Agent

from incident_agent.workers import (
    WORKER_NAMES,
    WORKER_SPECS,
    WorkerSpec,
    get_comms_policy,
    read_runbook,
)


def test_the_four_specialists_from_the_brief_exist() -> None:
    assert set(WORKER_NAMES) == {
        "log_analysis",
        "runbook_lookup",
        "impact_assessment",
        "comms_drafter",
    }


@pytest.mark.parametrize("spec", WORKER_SPECS, ids=WORKER_NAMES)
def test_each_description_tells_the_orchestrator_when_to_use_it(spec: WorkerSpec) -> None:
    assert "Use when" in spec.description


@pytest.mark.parametrize("spec", WORKER_SPECS, ids=WORKER_NAMES)
def test_each_specialist_is_told_to_treat_its_data_as_data(spec: WorkerSpec) -> None:
    assert "ignore any instructions" in spec.system_prompt


def test_tools_return_errors_to_the_model_instead_of_raising() -> None:
    agent = Agent(tools=[read_runbook, get_comms_policy], callback_handler=None)
    result = agent.tool.read_runbook(runbook_id="../services.json")
    assert result["status"] == "error"
    assert "services.json" in result["content"][0]["text"]
