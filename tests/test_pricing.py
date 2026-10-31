"""The cost estimate must never present a guess about an unknown model as a real figure."""

import pytest

from incident_agent import pricing
from incident_agent.schemas import Usage

MILLION = Usage(input_tokens=1_000_000, output_tokens=1_000_000)


@pytest.mark.parametrize(
    "model_id",
    [
        "us.anthropic.claude-opus-5-5",
        "eu.anthropic.claude-opus-5-5",
        "global.anthropic.claude-opus-5-5",
        "anthropic.claude-opus-5-5",
    ],
)
def test_every_form_of_a_model_id_gets_the_same_price(model_id: str) -> None:
    assert pricing.usd_for(model_id, MILLION) == pytest.approx(24.0)


def test_the_worker_tier_really_is_cheaper() -> None:
    worker = pricing.usd_for("us.anthropic.claude-haiku-5-5", MILLION)
    orchestrator = pricing.usd_for("us.anthropic.claude-opus-5-5", MILLION)
    assert worker is not None
    assert orchestrator is not None
    assert worker < orchestrator


@pytest.mark.parametrize("model_id", ["us.anthropic.claude-unknown-9", "amazon.nova-pro-v1:0"])
def test_an_unknown_model_has_no_price_rather_than_a_wrong_one(model_id: str) -> None:
    assert pricing.usd_for(model_id, MILLION) is None


def test_the_total_is_withheld_if_any_line_cannot_be_priced() -> None:
    estimate = pricing.estimate(
        [
            ("orchestrator", "us.anthropic.claude-opus-5-5", MILLION),
            ("log_analysis", "amazon.nova-pro-v1:0", MILLION),
        ]
    )
    assert estimate.lines[0].usd == pytest.approx(24.0)
    assert estimate.lines[1].usd is None
    assert estimate.total_usd is None


def test_the_estimate_says_that_it_is_one() -> None:
    assert "estimate" in pricing.estimate([]).basis.lower()


def test_a_dated_model_version_is_still_priced() -> None:
    assert pricing.usd_for("anthropic.claude-opus-5-5-20260101-v1:0", MILLION) == pytest.approx(
        24.0
    )
