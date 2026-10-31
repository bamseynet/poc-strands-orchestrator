"""The read-only data layer the workers' tools sit on. No LLM involved."""

import json

import pytest

from incident_agent import knowledge


class TestLogs:
    def test_unparseable_lines_are_reported_not_silently_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lines = ("2026-01-01T00:00:00Z ERROR svc [x] boom", "    at Trace.continuation(foo)")
        monkeypatch.setattr(knowledge, "_lines_for", lambda service: lines)
        everything = knowledge.search_logs("svc")
        assert lines[1] in everything["lines"]
        assert knowledge.search_logs("svc", level="ERROR")["unparsed_lines_skipped"] == 1
        assert knowledge.summarise_log_errors("svc")["unparsed_lines_skipped"] == 1

    def test_lists_the_bundled_sources(self) -> None:
        sources = knowledge.list_log_sources()
        assert "payments-api" in sources
        assert sources["payments-api"]["lines"] > 0

    def test_search_filters_by_level_and_substring(self) -> None:
        result = knowledge.search_logs("payments-api", level="WARN", contains="pool")
        assert result["total_matches"] == 1
        assert "db.pool.max_size=5" in result["lines"][0]

    def test_search_limit_caps_lines_but_reports_the_true_total(self) -> None:
        result = knowledge.search_logs("payments-api", level="ERROR", limit=3)
        assert len(result["lines"]) == 3
        assert result["total_matches"] > 3

    def test_summary_groups_repeated_errors_ignoring_volatile_values(self) -> None:
        summary = knowledge.summarise_log_errors("payments-api")
        top = summary["error_groups"][0]
        assert "connection pool exhausted" in top["message"]
        assert top["count"] == 26
        assert top["first_seen"] == "2026-10-30T14:03:00Z"

    def test_summary_surfaces_config_warnings_near_the_first_error(self) -> None:
        summary = knowledge.summarise_log_errors("payments-api")
        assert any("db.pool.max_size=5" in line for line in summary["warnings"])

    def test_unknown_service_is_a_clear_error(self) -> None:
        with pytest.raises(knowledge.UnknownEntityError, match="no-such-service"):
            knowledge.search_logs("no-such-service")

    @pytest.mark.parametrize("name", ["../services", "..\\secrets", "/etc/passwd", "a/b"])
    def test_log_lookup_cannot_escape_the_data_directory(self, name: str) -> None:
        with pytest.raises(knowledge.UnknownEntityError):
            knowledge.search_logs(name)


class TestRunbooks:
    def test_search_ranks_the_most_relevant_runbook_first(self) -> None:
        hits = knowledge.search_runbooks("connection pool exhausted")
        assert hits[0]["runbook_id"] == "db-connection-exhaustion"

    def test_search_with_no_matching_terms_returns_nothing(self) -> None:
        assert knowledge.search_runbooks("zebra quartz") == []

    def test_read_returns_the_markdown(self) -> None:
        assert "certctl renew" in knowledge.read_runbook("tls-cert-expiry")

    @pytest.mark.parametrize("name", ["../services.json", "../../pyproject", "/etc/passwd"])
    def test_read_cannot_escape_the_runbook_directory(self, name: str) -> None:
        with pytest.raises(knowledge.UnknownEntityError):
            knowledge.read_runbook(name)


class TestServiceMap:
    def test_get_service_returns_owner_and_tier(self) -> None:
        service = knowledge.get_service("payments-api")
        assert service["team"] == "payments"
        assert service["tier"] == 1

    def test_blast_radius_walks_dependents_transitively_with_hop_counts(self) -> None:
        radius = knowledge.blast_radius("orders-db")
        hops = {d["service"]: d["hops"] for d in radius["dependents"]}
        assert hops["payments-api"] == 1
        assert hops["checkout-web"] == 2
        assert hops["notifications"] == 2

    def test_blast_radius_reports_customer_facing_impact_and_journeys(self) -> None:
        radius = knowledge.blast_radius("payments-api")
        assert radius["customer_facing_impacted"] == ["checkout-web"]
        assert "take a payment" in radius["journeys_at_risk"]
        assert "place an order" in radius["journeys_at_risk"]

    def test_leaf_service_has_no_dependents(self) -> None:
        radius = knowledge.blast_radius("reporting-batch")
        assert radius["dependents"] == []

    def test_blast_radius_terminates_on_a_dependency_cycle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cyclic = {
            "a": {"depends_on": ["b"], "tier": 1, "customer_facing": False, "journeys": []},
            "b": {"depends_on": ["a"], "tier": 1, "customer_facing": False, "journeys": []},
        }
        monkeypatch.setattr(knowledge, "_services", lambda: cyclic)
        radius = knowledge.blast_radius("a")
        assert [d["service"] for d in radius["dependents"]] == ["b"]


class TestCommsPolicy:
    def test_policy_varies_by_severity(self) -> None:
        assert knowledge.get_comms_policy("sev1")["status_page"] is True
        assert knowledge.get_comms_policy("sev3")["status_page"] is False

    def test_unknown_severity_is_a_clear_error(self) -> None:
        with pytest.raises(knowledge.UnknownEntityError, match="sev9"):
            knowledge.get_comms_policy("sev9")


class TestSampleIncidents:
    def test_samples_load_and_reference_known_services(self) -> None:
        samples = knowledge.sample_incidents()
        assert {s["id"] for s in samples} >= {"checkout-5xx", "batch-disk-full"}
        for sample in samples:
            assert knowledge.get_service(sample["service"])

    def test_every_tool_result_is_json_serialisable(self) -> None:
        json.dumps(knowledge.blast_radius("orders-db"))
        json.dumps(knowledge.summarise_log_errors("payments-api"))


def test_filler_words_in_a_long_query_do_not_decide_the_ranking() -> None:
    query = "the is of and to a in it that this was for on are with " + "disk full"
    assert knowledge.search_runbooks(query)[0]["runbook_id"] == "disk-full"


def test_different_status_codes_are_not_merged_into_one_error_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = tuple(
        f"2026-01-01T00:00:0{i}Z ERROR svc [x] upstream failed status={code} duration_ms={i}"
        for i, code in enumerate((502, 502, 503))
    )
    monkeypatch.setattr(knowledge, "_lines_for", lambda service: lines)
    groups = knowledge.summarise_log_errors("svc")["error_groups"]
    assert {g["count"] for g in groups} == {2, 1}


def test_a_short_but_meaningful_term_like_db_counts_in_runbook_ranking() -> None:
    assert knowledge.search_runbooks("db")[0]["runbook_id"] == "db-connection-exhaustion"


def test_an_unfiltered_log_search_returns_every_line_of_a_bundled_log() -> None:
    total = knowledge.list_log_sources()["payments-api"]["lines"]
    assert len(knowledge.search_logs("payments-api")["lines"]) == total


def test_fatal_lines_count_as_errors_and_decimals_group_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = (
        "2026-01-01T00:00:00Z FATAL svc [x] out of memory latency=1.5",
        "2026-01-01T00:00:01Z FATAL svc [x] out of memory latency=2.75",
    )
    monkeypatch.setattr(knowledge, "_lines_for", lambda service: lines)
    groups = knowledge.summarise_log_errors("svc")["error_groups"]
    assert [g["count"] for g in groups] == [2]


def test_searching_for_errors_also_finds_fatal_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = ("2026-01-01T00:00:00Z FATAL svc [x] out of memory",)
    monkeypatch.setattr(knowledge, "_lines_for", lambda service: lines)
    assert knowledge.search_logs("svc", level="ERROR")["lines"] == list(lines)


@pytest.mark.parametrize("level", ["warn", "WARNING", " WARN "])
def test_level_names_are_forgiving_and_stay_exact_for_warnings(level: str) -> None:
    result = knowledge.search_logs("payments-api", level=level)
    assert result["total_matches"] > 0
    assert all(" WARN " in line for line in result["lines"])
