"""`LLM_MODE=fake`: rule-based stand-ins for the LLMs, so anyone can run the demo without AWS.

There is no intelligence here. Keyword rules choose the workers and templates write the prose. But
these fakes sit behind the real `Model` interface: the Strands agents, the concurrency and the tools
all run for real, and every finding is read from the bundled data by a real tool call.
"""

import asyncio
import json
import re
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import Any

from incident_agent import knowledge
from incident_agent.llm.base import ModelSet
from incident_agent.llm.scripted import ScriptedModel, ScriptedRequest, ToolCall, Turn, TurnOutcome
from incident_agent.orchestrator import RECORD_PLAN_TOOL, SYNTHESIS_TOOL

_LOGS, _RUNBOOK, _IMPACT, _COMMS = (
    "log_analysis",
    "runbook_lookup",
    "impact_assessment",
    "comms_drafter",
)

_FAILING_TERMS = ("fail", "error", "5xx", "503", "timeout", "crash", "outage", "latency")
# Whole words only: "down" must not match "download" or "shutdown".
_FAILING_WORDS = ("down",)
_ALL_QUIET_TERMS = ("nothing is failing", "nothing is broken")
_PROCEDURE_TERMS = ("certificate", "renewal", "procedure", "expir")
_IMPACT_TERMS = ("who is affected", "which services", "blast radius", "affected", "impact")
_COMMS_TERMS = ("announcement", "stakeholder", "status page", "notify")
_URGENT = ("sev1", "sev2")
_NO_LOG_EVIDENCE = "The brief names no service I have logs for."

# Seconds of simulated thinking per model response, before `delay_scale`.
_THINK_S = {"orchestrator": 0.8, _LOGS: 1.2, _RUNBOOK: 0.7, _IMPACT: 0.9, _COMMS: 1.0}
_CHUNK_DELAY_S = 0.012


def choose_workers(incident: dict[str, Any]) -> set[str]:
    """The fake orchestrator's "judgement": keyword rules over the incident text."""
    text = f"{incident['title']} {incident['description']}".lower()
    urgent = incident["severity"] in _URGENT

    def mentions(terms: Iterable[str], *, whole_word: bool = False) -> bool:
        end = r"\b" if whole_word else ""
        return any(re.search(rf"\b{re.escape(term)}{end}", text) for term in terms)

    failing = (
        mentions(_FAILING_TERMS) or mentions(_FAILING_WORDS, whole_word=True)
    ) and not mentions(_ALL_QUIET_TERMS)
    chosen = set()
    if failing:
        chosen.add(_LOGS)
    if failing or mentions(_PROCEDURE_TERMS):
        chosen.add(_RUNBOOK)
    if urgent or mentions(_IMPACT_TERMS):
        chosen.add(_IMPACT)
    if urgent or mentions(_COMMS_TERMS):
        chosen.add(_COMMS)
    return chosen or {_RUNBOOK}


@dataclass(frozen=True)
class _Result:
    tool: str
    ok: bool
    text: str


def _results(request: ScriptedRequest) -> list[_Result]:
    """Every tool result so far, oldest first, matched back to the tool that was called."""
    names = {
        block["toolUse"]["toolUseId"]: block["toolUse"]["name"]
        for message in request.messages
        for block in message["content"]
        if "toolUse" in block
    }
    return [
        _Result(
            tool=names[result["toolUseId"]],
            ok=result.get("status") != "error",
            text="".join(part.get("text", "") for part in result["content"]).strip(),
        )
        for message in request.messages
        for block in message["content"]
        if (result := block.get("toolResult"))
    ]


def _prompt(request: ScriptedRequest) -> str:
    return "".join(block.get("text", "") for block in request.messages[0]["content"])


def _known_services(text: str) -> list[str]:
    return [name for name in knowledge.list_services() if name in text]


def _orchestrate(request: ScriptedRequest) -> TurnOutcome:
    match = re.search(r"<incident>\s*(\{.*\})\s*</incident>", _prompt(request), re.DOTALL)
    if match is None:
        raise ValueError("The orchestrator prompt has no <incident> block")
    incident = json.loads(match[1])

    chosen = choose_workers(incident)
    results = _results(request)
    called = {result.tool for result in results}

    if not called - {RECORD_PLAN_TOOL}:
        first_batch = sorted(chosen - {_COMMS}) or [_COMMS]
        return Turn(
            tool_calls=[
                ToolCall(RECORD_PLAN_TOOL, {"rationale": _rationale(chosen, first_batch)}),
                *(
                    ToolCall(worker, {"input": _brief(worker, incident, results)})
                    for worker in first_batch
                ),
            ]
        )
    if _COMMS in chosen and _COMMS not in called:
        rationale = "Simulated plan: the findings are in, so comms can state confirmed facts."
        return Turn(
            tool_calls=[
                ToolCall(RECORD_PLAN_TOOL, {"rationale": rationale}),
                ToolCall(_COMMS, {"input": _brief(_COMMS, incident, results)}),
            ]
        )
    return Turn(tool_calls=[ToolCall(SYNTHESIS_TOOL, _synthesise(incident, results))])


def _rationale(chosen: set[str], first_batch: list[str]) -> str:
    text = f"Simulated plan: rule-based selection chose {', '.join(sorted(chosen))}."
    if len(first_batch) > 1:
        text += f" Running {', '.join(first_batch)} in parallel because they are independent."
    if _COMMS in chosen and first_batch != [_COMMS]:
        text += " Comms waits for their findings, because it must state confirmed facts."
    return text


def _brief(worker: str, incident: dict[str, Any], results: list[_Result]) -> str:
    title = incident["title"]
    service = incident.get("service") or next(iter(_known_services(title)), None)
    if worker == _LOGS:
        if service is None:
            return f"Check the available logs for: {title}"
        dependencies = knowledge.known_dependencies(service)
        return f"Examine the logs of {' and '.join([service, *dependencies])} for: {title}"
    if worker == _RUNBOOK:
        return f"Find the runbook for: {title}. {incident['description']}"
    if worker == _IMPACT:
        return f"Assess the blast radius of {service or 'the affected service'} for: {title}"
    impact = next((r.text for r in results if r.tool == _IMPACT and r.ok), "not yet assessed")
    return f"Severity: {incident['severity']}\nIncident: {title}\nConfirmed impact: {impact}"


def _synthesise(incident: dict[str, Any], results: list[_Result]) -> dict[str, Any]:
    workers = [r for r in results if r.tool != RECORD_PLAN_TOOL]
    by_tool = {r.tool: r for r in workers if r.ok}
    logs = by_tool.get(_LOGS)
    if logs and logs.text == _NO_LOG_EVIDENCE:
        logs = None
    impact = by_tool.get(_IMPACT)
    runbook = by_tool.get(_RUNBOOK)
    comms = by_tool.get(_COMMS)

    summary = f"Simulated synthesis: {incident['title']} ({incident['severity']})."
    if impact and (first_line := impact.text.strip().splitlines()[:1]):
        summary += f" {first_line[0]}"
    actions = _NUMBERED_STEP.findall(runbook.text) if runbook else []
    return {
        "summary": summary,
        "probable_cause": (
            f"From the logs: {logs.text}"
            if logs
            else "Not established: no log evidence is available for this report."
        ),
        "confidence": "low" if any(not r.ok for r in workers) else "medium",
        "recommended_actions": [
            {"action": step.strip(), "priority": "now" if index == 0 else "soon"}
            for index, step in enumerate(actions)
        ],
        "comms_draft": comms.text if comms else None,
        "worker_contributions": [
            {
                "worker": r.tool,
                "key_finding": r.text.splitlines()[0] if r.text else "Returned nothing.",
            }
            for r in workers
        ],
    }


_NUMBERED_STEP = re.compile(r"^\d+\.\s+(.*)$", re.MULTILINE)


def _analyse_logs(request: ScriptedRequest) -> TurnOutcome:
    results = _results(request)
    if not results:
        services = _known_services(_prompt(request))
        if not services:
            return Turn(text=_NO_LOG_EVIDENCE)
        return Turn(
            text="Summarising errors for each service in the brief.",
            tool_calls=[ToolCall("summarise_log_errors", {"service": s}) for s in services],
        )
    return Turn(text="\n".join(_log_finding(result) for result in results))


def _log_finding(result: _Result) -> str:
    if not result.ok:
        return f"Could not read logs: {result.text}"
    summary = json.loads(result.text)
    service = summary["service"]
    if not summary["error_groups"]:
        return f"{service}: no errors logged."
    lines = [
        f"{service}: {group['count']}x {group['message']} "
        f"({group['first_seen']} to {group['last_seen']})"
        for group in summary["error_groups"][:2]
    ]
    lines.extend(f"{service} warning: {warning}" for warning in summary["warnings"][:2])
    return "\n".join(lines)


def _find_runbook(request: ScriptedRequest) -> TurnOutcome:
    results = _results(request)
    if not results:
        return Turn(tool_calls=[ToolCall("search_runbooks", {"query": _prompt(request)})])
    failed = next((r for r in results if not r.ok), None)
    if failed is not None:
        return Turn(text=f"Could not read the runbooks: {failed.text}")
    hits = json.loads(results[0].text)
    if not hits:
        return Turn(text="No runbook matches these symptoms.")
    best = hits[0]["runbook_id"]
    if len(results) == 1:
        return Turn(tool_calls=[ToolCall("read_runbook", {"runbook_id": best})])
    return Turn(text=f"Best match: {best}\n\n{_applicable_steps(results[1].text)}")


def _applicable_steps(runbook: str) -> str:
    """The Mitigate section if the runbook has one, otherwise the whole of it."""
    match = re.search(r"^## Mitigate\n(.*?)(?=^## |\Z)", runbook, re.MULTILINE | re.DOTALL)
    return match[1].strip() if match else runbook


def _assess_impact(request: ScriptedRequest) -> TurnOutcome:
    results = _results(request)
    if not results:
        prompt = _prompt(request)
        # The brief names the failing service; others may only be mentioned in passing.
        named = re.search(r"blast radius of (\S+) for:", prompt)
        services = _known_services(named[1] if named else prompt)
        if not services:
            return Turn(text="The brief names no service I can find in the dependency map.")
        return Turn(tool_calls=[ToolCall("blast_radius", {"service": services[0]})])
    if not results[0].ok:
        return Turn(text=f"Could not read the dependency map: {results[0].text}")
    radius = json.loads(results[0].text)
    dependents = ", ".join(d["service"] for d in radius["dependents"]) or "none"
    customer = ", ".join(radius["customer_facing_impacted"]) or "none"
    journeys = ", ".join(radius["journeys_at_risk"]) or "none"
    return Turn(
        text=(
            f"{radius['service']} failing affects dependents: {dependents}.\n"
            f"Customer-facing services impacted: {customer}.\n"
            f"Journeys at risk: {journeys}."
        )
    )


def _draft_comms(request: ScriptedRequest) -> TurnOutcome:
    results = _results(request)
    prompt = _prompt(request)
    if not results:
        severity = re.search(r"Severity: (\S+)", prompt)
        return Turn(
            tool_calls=[
                ToolCall("get_comms_policy", {"severity": severity[1] if severity else "sev3"})
            ]
        )
    policy = json.loads(results[0].text)
    incident = re.search(r"Incident: (.*)", prompt)
    impact = re.search(r"Confirmed impact: (.*)", prompt, re.DOTALL)
    return Turn(
        text=(
            f"For: {', '.join(policy['audiences'])}\n\n"
            f"{incident[1] if incident else 'An incident'}\n"
            f"{impact[1] if impact else ''}\n"
            f"Our next update will follow within {policy['update_cadence_minutes']} minutes."
        )
    )


_WORKER_BRAINS: dict[str, Callable[[ScriptedRequest], TurnOutcome]] = {
    _LOGS: _analyse_logs,
    _RUNBOOK: _find_runbook,
    _IMPACT: _assess_impact,
    _COMMS: _draft_comms,
}


def _worker_model(
    name: str, model_id: str, delay_scale: float, simulate_failure: bool
) -> ScriptedModel:
    brain = _WORKER_BRAINS[name]

    def decide(request: ScriptedRequest) -> TurnOutcome:
        if simulate_failure:
            return RuntimeError(f"Simulated failure of {name}")
        return brain(request)

    return ScriptedModel(
        decide,
        model_id=model_id,
        chunk_delay_s=_CHUNK_DELAY_S * delay_scale,
        before_turn=_pause(_THINK_S[name] * delay_scale),
    )


def _pause(seconds: float) -> Callable[[], Any]:
    async def pause() -> None:
        await asyncio.sleep(seconds)

    return pause


def build_demo_models(
    *,
    orchestrator_model_id: str,
    worker_model_id: str,
    delay_scale: float,
    simulate_failures: Collection[str],
) -> ModelSet:
    """The rule-based team. Decisions come only from the request, so it is safe to reuse."""
    return ModelSet(
        orchestrator=ScriptedModel(
            _orchestrate,
            model_id=orchestrator_model_id,
            chunk_delay_s=_CHUNK_DELAY_S * delay_scale,
            before_turn=_pause(_THINK_S["orchestrator"] * delay_scale),
        ),
        workers={
            name: _worker_model(name, worker_model_id, delay_scale, name in simulate_failures)
            for name in _WORKER_BRAINS
        },
        orchestrator_model_id=orchestrator_model_id,
        worker_model_id=worker_model_id,
    )
