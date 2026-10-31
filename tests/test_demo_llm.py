"""`LLM_MODE=fake`: the rule-based stand-in that lets anyone run the demo without AWS."""

from typing import Any

import pytest

from incident_agent import knowledge
from incident_agent.config import Settings
from incident_agent.llm.factory import build_models
from incident_agent.schemas import Incident, IncidentReport
from tests.helpers import COMMS, IMPACT, LOGS, RUNBOOK, report_from, run


def settings() -> Settings:
    return Settings(_env_file=None, llm_mode="fake", fake_llm_delay_scale=0)


def sample(sample_id: str) -> Incident:
    return Incident.model_validate(
        next(s for s in knowledge.sample_incidents() if s["id"] == sample_id)
    )


async def run_sample(sample_id: str, **kwargs: Any) -> IncidentReport:
    return report_from(await run(build_models(settings(), **kwargs), sample(sample_id)))


@pytest.mark.parametrize(
    ("sample_id", "expected"),
    [
        ("checkout-5xx", {LOGS, RUNBOOK, IMPACT, COMMS}),
        ("batch-disk-full", {LOGS, RUNBOOK}),
        ("auth-cert-expiry", {RUNBOOK, IMPACT}),
        ("orders-db-maintenance", {IMPACT, COMMS}),
    ],
)
async def test_each_sample_gets_its_own_team(sample_id: str, expected: set[str]) -> None:
    report = await run_sample(sample_id)
    assert {f.worker for f in report.worker_findings} == expected
    assert report.status == "complete"
    assert report.synthesis_source == "orchestrator"


async def test_workers_answer_from_the_bundled_data_through_their_real_tools() -> None:
    report = await run_sample("checkout-5xx")
    outputs = {f.worker: f.output for f in report.worker_findings}
    assert "connection pool exhausted" in outputs[LOGS]
    assert "db.pool.max_size=5" in outputs[LOGS], "the config change is the key clue"
    assert "orders-db: no errors" in outputs[LOGS], "the database is ruled out"
    assert "payments-5xx" in outputs[RUNBOOK]
    assert "checkout-web" in outputs[IMPACT]
    assert "status page" in outputs[COMMS].lower()


async def test_each_sample_finds_the_right_runbook() -> None:
    for sample_id, runbook in [("batch-disk-full", "disk-full"), ("auth-cert-expiry", "tls-cert")]:
        report = await run_sample(sample_id)
        output = next(f.output for f in report.worker_findings if f.worker == RUNBOOK)
        assert runbook in output


async def test_comms_waits_for_the_other_findings_and_uses_them() -> None:
    models = build_models(settings())
    events = await run(models, sample("checkout-5xx"))
    runs = {f.worker: f for f in report_from(events).worker_findings}
    assert runs[COMMS].turn == 2
    assert {runs[w].turn for w in (LOGS, RUNBOOK, IMPACT)} == {1}
    assert "checkout-web" in runs[COMMS].task, "the brief carries the confirmed impact"
    assert "take a payment" in runs[COMMS].output, "the whole impact reaches the draft"


async def test_the_synthesis_draws_on_the_workers_and_says_it_is_simulated() -> None:
    report = await run_sample("checkout-5xx")
    assert "simulated" in report.summary.lower()
    assert "connection pool exhausted" in report.probable_cause
    assert any("deployctl rollback" in a.action for a in report.recommended_actions)
    assert report.comms_draft
    assert report.unacknowledged_workers == []


async def test_a_simulated_failure_degrades_the_run_without_breaking_it() -> None:
    report = await run_sample("checkout-5xx", simulate_failures=[LOGS])
    by_worker = {f.worker: f for f in report.worker_findings}
    assert by_worker[LOGS].status == "failed"
    assert "Simulated failure" in (by_worker[LOGS].error or "")
    assert by_worker[RUNBOOK].status == "ok"
    assert report.status == "degraded"
    assert report.confidence == "low"
    assert report.comms_draft, "comms does not depend on the log worker"
    assert "not established" in report.probable_cause.lower()


async def test_free_text_incident_without_a_known_service_still_completes() -> None:
    incident = Incident(
        title="Something odd",
        severity="sev3",
        description="Users report errors but nobody knows where from.",
    )
    report = report_from(await run(build_models(settings()), incident))
    assert report.worker_findings, "a failing incident still gets investigated"
    assert report.synthesis_source == "orchestrator"
    assert "not established" in report.probable_cause.lower(), "no logs were read, so no claim"


async def test_an_incident_with_no_signals_still_gets_a_runbook_search() -> None:
    incident = Incident(title="Question", severity="sev4", description="Just checking in.")
    report = report_from(await run(build_models(settings()), incident))
    assert [f.worker for f in report.worker_findings] == [RUNBOOK]


async def test_runs_are_deterministic() -> None:
    first, second = [await run_sample("batch-disk-full") for _ in range(2)]
    assert first.summary == second.summary
    assert [f.output for f in first.worker_findings] == [f.output for f in second.worker_findings]


async def test_simulated_token_counts_are_non_zero() -> None:
    report = await run_sample("batch-disk-full")
    assert report.cost.total_usd is not None
    assert report.cost.total_usd > 0


async def test_models_can_be_reused_across_runs() -> None:
    models = build_models(settings())
    for _ in range(2):
        assert report_from(await run(models, sample("batch-disk-full"))).status == "complete"


def test_keywords_match_whole_words_not_fragments() -> None:
    from incident_agent.llm.demo import choose_workers

    quiet = {
        "title": "Planned shutdown of reporting-batch",
        "description": "Scheduled maintenance. Users will download reports later.",
        "severity": "sev4",
    }
    assert "log_analysis" not in choose_workers(quiet)
    assert "log_analysis" in choose_workers({**quiet, "title": "Service is down"})


def test_a_whole_word_down_at_the_very_end_still_counts() -> None:
    from incident_agent.llm.demo import choose_workers

    incident = {"title": "Checkout is down", "description": "", "severity": "sev4"}
    assert "log_analysis" in choose_workers(incident)
