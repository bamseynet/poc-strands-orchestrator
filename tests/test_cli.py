"""`python -m incident_agent`: run the pattern from a terminal, with no API or UI."""

import json

import pytest

from incident_agent.__main__ import main
from tests.helpers import LOGS


@pytest.fixture(autouse=True)
def fast_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODE", "fake")
    monkeypatch.setenv("FAKE_LLM_DELAY_SCALE", "0")


def test_lists_the_samples(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--list"]) == 0
    assert "checkout-5xx" in capsys.readouterr().out


def test_runs_a_sample_and_shows_plan_workers_and_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--sample", "batch-disk-full"]) == 0
    out = capsys.readouterr().out
    assert "plan" in out.lower()
    assert LOGS in out
    assert "probable cause" in out.lower()
    assert "$" in out


def test_json_mode_emits_one_parseable_event_per_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--sample", "batch-disk-full", "--json"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "report_ready"


def test_can_simulate_a_worker_failure(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--sample", "batch-disk-full", "--simulate-failure", LOGS]) == 0
    assert "degraded" in capsys.readouterr().out.lower()


def test_an_unknown_sample_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--sample", "nope"])
    assert exit_info.value.code == 2
    assert "nope" in capsys.readouterr().err


def test_a_failed_run_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from incident_agent import __main__ as cli
    from incident_agent.llm.scripted import ScriptedModel
    from tests.helpers import Team

    team = Team([RuntimeError("bedrock unavailable")])
    assert isinstance(team.orchestrator, ScriptedModel)
    monkeypatch.setattr(cli, "build_models", lambda *_, **__: team)
    assert main(["--sample", "batch-disk-full"]) == 1
    assert "bedrock unavailable" in capsys.readouterr().out


def test_a_misconfigured_llm_is_a_clean_error_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from incident_agent import __main__ as cli

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("no credentials")

    monkeypatch.setattr(cli, "build_models", boom)
    assert main(["--sample", "checkout-5xx"]) == 1
    assert "no credentials" in capsys.readouterr().err
