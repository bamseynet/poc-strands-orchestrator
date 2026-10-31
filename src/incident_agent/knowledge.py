"""Read-only access to the bundled sample data: logs, runbooks, service map, comms policy.

Plain functions with no agent framework in sight, so the data logic can be tested directly and the
workers' tools stay thin. Names arriving here come from a model, so they are only ever checked
against what is bundled and never used to build a path.
"""

import json
import re
from collections import Counter, deque
from functools import lru_cache
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any

_LOG_LINE = re.compile(
    r"^(?P<ts>\S+) (?P<level>[A-Z]+) (?P<service>\S+) \[(?P<component>[^\]]+)\] (?P<message>.*)$"
)
# Values that differ between otherwise identical log lines.
_VOLATILE = re.compile(r"\b(request_id=)\S+|\b(\w+=)\d+")
# Numbers that identify a failure rather than vary between occurrences, so are never masked.
_MEANINGFUL_KEYS = ("status=", "code=", "http_status=", "exit_code=", "errno=")
_WORD = re.compile(r"[a-z0-9]+")
_MAX_LINES = 200
# Common words that would otherwise let a long free-text query score every runbook alike.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "not",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "respond",
        "incident",
    ]
)


class UnknownEntityError(LookupError):
    """A model asked for a service, runbook or severity that does not exist."""


def _data() -> Traversable:
    return resources.files("incident_agent") / "data"


@lru_cache
def _log_lines() -> dict[str, tuple[str, ...]]:
    return {
        path.name.removesuffix(".log"): tuple(path.read_text(encoding="utf-8").splitlines())
        for path in (_data() / "logs").iterdir()
        if path.name.endswith(".log")
    }


@lru_cache
def _runbooks() -> dict[str, str]:
    return {
        path.name.removesuffix(".md"): path.read_text(encoding="utf-8")
        for path in (_data() / "runbooks").iterdir()
        if path.name.endswith(".md")
    }


@lru_cache
def _services() -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = json.loads(
        (_data() / "services.json").read_text(encoding="utf-8")
    )
    return loaded


@lru_cache
def _comms_policy() -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = json.loads(
        (_data() / "comms_policy.json").read_text(encoding="utf-8")
    )
    return loaded


def _lines_for(service: str) -> tuple[str, ...]:
    logs = _log_lines()
    if service not in logs:
        raise UnknownEntityError(f"No logs for service {service!r}. Available: {sorted(logs)}")
    return logs[service]


def list_log_sources() -> dict[str, dict[str, Any]]:
    """Services with bundled logs, with their line counts."""
    return {service: {"lines": len(lines)} for service, lines in _log_lines().items()}


def search_logs(
    service: str, level: str | None = None, contains: str | None = None, limit: int = _MAX_LINES
) -> dict[str, Any]:
    """Log lines for a service, optionally filtered; `total_matches` ignores `limit`."""
    matches: list[str] = []
    skipped = 0
    for line in _lines_for(service):
        parsed = _LOG_LINE.match(line)
        if parsed is None:
            # Lines in no standard format (stack-trace continuations) have no level. Keep them
            # unless a level filter was asked for, and count any that the filter had to drop.
            if level is None and (contains is None or contains.lower() in line.lower()):
                matches.append(line)
            elif level is not None:
                skipped += 1
        elif (level is None or parsed["level"] == level.upper()) and (
            contains is None or contains.lower() in line.lower()
        ):
            matches.append(line)
    return {
        "service": service,
        "total_matches": len(matches),
        "unparsed_lines_skipped": skipped,
        "lines": matches[: max(0, min(limit, _MAX_LINES))],
    }


def _mask(match: re.Match[str]) -> str:
    if (key := match[2]) and key.lower() in _MEANINGFUL_KEYS:
        return match[0]
    return f"{match[1] or match[2]}#"


def summarise_log_errors(service: str) -> dict[str, Any]:
    """Group a service's errors by shape, with first and last seen, plus its warnings."""
    counts: Counter[str] = Counter()
    first_seen: dict[str, str] = {}
    last_seen: dict[str, str] = {}
    warnings: list[str] = []
    unparsed = 0
    for line in _lines_for(service):
        parsed = _LOG_LINE.match(line)
        if parsed is None:
            unparsed += 1
            continue
        if parsed["level"] == "WARN":
            warnings.append(line)
        elif parsed["level"] == "ERROR":
            shape = _VOLATILE.sub(_mask, parsed["message"])
            counts[shape] += 1
            first_seen.setdefault(shape, parsed["ts"])
            last_seen[shape] = parsed["ts"]
    return {
        "service": service,
        "error_groups": [
            {
                "message": shape,
                "count": count,
                "first_seen": first_seen[shape],
                "last_seen": last_seen[shape],
            }
            for shape, count in counts.most_common()
        ],
        "warnings": warnings,
        "unparsed_lines_skipped": unparsed,
    }


def search_runbooks(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Runbooks ranked by how many distinct query terms they contain."""
    terms = {t for t in _WORD.findall(query.lower()) if t not in _STOPWORDS and len(t) > 1}
    hits: list[dict[str, Any]] = []
    for runbook_id, body in _runbooks().items():
        score = len(terms & set(_WORD.findall(body.lower())))
        if score:
            hits.append({"runbook_id": runbook_id, "title": body.splitlines()[0], "score": score})
    hits.sort(key=lambda hit: (-hit["score"], hit["runbook_id"]))
    return hits[:limit]


def read_runbook(runbook_id: str) -> str:
    """The full markdown of one runbook."""
    runbooks = _runbooks()
    if runbook_id not in runbooks:
        raise UnknownEntityError(f"No runbook {runbook_id!r}. Available: {sorted(runbooks)}")
    return runbooks[runbook_id]


def get_service(service: str) -> dict[str, Any]:
    """Owner, tier and direct dependencies of one service."""
    services = _services()
    if service not in services:
        raise UnknownEntityError(f"No service {service!r}. Available: {sorted(services)}")
    return {"service": service, **services[service]}


def list_services() -> list[str]:
    """Every service in the dependency map."""
    return list(_services())


def known_dependencies(service: str) -> list[str]:
    """A service's direct dependencies that have bundled logs. Empty for an unknown service."""
    depends_on = _services().get(service, {}).get("depends_on", [])
    return [name for name in depends_on if name in _log_lines()]


def blast_radius(service: str) -> dict[str, Any]:
    """Everything that depends on `service`, directly or transitively, nearest first."""
    services = _services()
    if service not in services:
        raise UnknownEntityError(f"No service {service!r}. Available: {sorted(services)}")

    # Breadth-first over reversed edges; `hops` doubles as the visited set, so cycles terminate.
    hops = {service: 0}
    queue = deque([service])
    while queue:
        current = queue.popleft()
        for name, spec in services.items():
            if current in spec["depends_on"] and name not in hops:
                hops[name] = hops[current] + 1
                queue.append(name)

    impacted = sorted((name for name in hops if name != service), key=lambda n: (hops[n], n))
    journeys = [*services[service]["journeys"]]
    for name in impacted:
        journeys.extend(j for j in services[name]["journeys"] if j not in journeys)
    return {
        "service": service,
        "dependents": [
            {"service": name, "hops": hops[name], "tier": services[name]["tier"]}
            for name in impacted
        ],
        "customer_facing_impacted": [n for n in impacted if services[n]["customer_facing"]],
        "journeys_at_risk": journeys,
    }


def get_comms_policy(severity: str) -> dict[str, Any]:
    """Who must be told, how often, and in what tone, for a severity."""
    policy = _comms_policy()
    if severity not in policy:
        raise UnknownEntityError(f"No comms policy for {severity!r}. Available: {sorted(policy)}")
    return {"severity": severity, **policy[severity]}


def sample_incidents() -> list[dict[str, Any]]:
    """The incidents offered as one-click samples."""
    loaded: list[dict[str, Any]] = json.loads(
        (_data() / "incidents.json").read_text(encoding="utf-8")
    )
    return loaded
