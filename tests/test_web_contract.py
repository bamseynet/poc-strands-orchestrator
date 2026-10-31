"""Keeps the UI's replay fixtures honest: if the event shapes change, the UI tests must see it.

The fixtures are real runs through the real agent code. Regenerate them with `make web-fixtures`.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from incident_agent import knowledge
from incident_agent.config import Settings
from incident_agent.llm.factory import build_models
from incident_agent.schemas import Incident
from tests.helpers import LOGS, run

FIXTURES = Path(__file__).parent.parent / "web" / "src" / "test" / "fixtures"
SCENARIOS = {
    "run-complete": ("checkout-5xx", []),
    "run-degraded": ("checkout-5xx", [LOGS]),
}


def skeleton(value: Any) -> Any:
    """The shape of a JSON value: its keys and types, without its data."""
    if isinstance(value, dict):
        return {key: skeleton(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [skeleton(value[0])] if value else []
    return type(value).__name__


def shapes(lines: list[str]) -> dict[str, list[str]]:
    """Every distinct shape per event type, in a form that can be compared."""
    found: dict[str, set[str]] = {}
    for line in lines:
        event = json.loads(line)
        found.setdefault(event["type"], set()).add(json.dumps(skeleton(event), sort_keys=True))
    return {kind: sorted(variants) for kind, variants in found.items()}


async def generate(sample_id: str, failures: list[str]) -> list[str]:
    sample = next(s for s in knowledge.sample_incidents() if s["id"] == sample_id)
    settings = Settings(_env_file=None, llm_mode="fake", fake_llm_delay_scale=0)
    events = await run(
        build_models(settings, simulate_failures=failures), Incident.model_validate(sample)
    )
    return [event.model_dump_json() for event in events]


@pytest.mark.parametrize("name", SCENARIOS)
async def test_ui_fixtures_match_what_the_agent_emits(name: str) -> None:
    lines = await generate(*SCENARIOS[name])
    path = FIXTURES / f"{name}.jsonl"
    if os.environ.get("UPDATE_FIXTURES"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    committed = path.read_text(encoding="utf-8").splitlines()
    assert shapes(committed) == shapes(lines), "run `make web-fixtures` and update the UI types"
