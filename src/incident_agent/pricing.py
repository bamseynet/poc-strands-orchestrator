"""Rough cost estimates for the report.

These are Anthropic's list prices, used as an approximation: Bedrock bills these models through AWS
Marketplace and its rates can differ, so check https://aws.amazon.com/bedrock/pricing/ before
quoting a figure. Prompt-caching discounts and Haiku's higher rate above 100K input tokens are not
modelled.
"""

import re

from incident_agent.schemas import CostEstimate, CostLine, Usage

ESTIMATE_BASIS = (
    "Estimate from Anthropic list prices; AWS Bedrock rates may differ. "
    "With LLM_MODE=fake the token counts are simulated."
)

# USD per million tokens: (input, output).
_PRICES_PER_MTOK = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
_MODEL_NAME = re.compile(r"claude-[a-z0-9-]+")


def usd_for(model_id: str, usage: Usage) -> float | None:
    """Price `usage` on any id form (bare, or geo/global profile). None if the model is unknown."""
    match = _MODEL_NAME.search(model_id)
    prices = _PRICES_PER_MTOK.get(match[0]) if match else None
    if prices is None:
        return None
    return (usage.input_tokens * prices[0] + usage.output_tokens * prices[1]) / 1_000_000


def estimate(lines: list[tuple[str, str, Usage]]) -> CostEstimate:
    """Build the estimate from (label, model id, usage) rows."""
    priced = [
        CostLine(
            label=label,
            model_id=model_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            usd=usd_for(model_id, usage),
        )
        for label, model_id, usage in lines
    ]
    costs = [line.usd for line in priced]
    total = None if None in costs else sum(cost for cost in costs if cost is not None)
    return CostEstimate(lines=priced, total_usd=total, basis=ESTIMATE_BASIS)
