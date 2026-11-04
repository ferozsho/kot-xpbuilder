"""Tests for the typed chart draft (P7-F05/F09) and the optional AI ranking (P7-F04)."""

from __future__ import annotations

import unittest
from unittest import mock

from xp_prompt import chart_catalog, chart_draft, chart_ranking, chart_rules
from xp_prompt.config import Settings
from xp_prompt.providers.types import (
    AttemptSummary,
    ProviderResponse,
    RoutedResponse,
    TokenUsage,
)

ROOT_SECRET = "unit-test-root-secret-value"
VIZ = [
    "echarts_timeseries_line",
    "echarts_timeseries_bar",
    "pie",
    "heatmap_v2",
    "big_number_total",
    "table",
]


def settings() -> Settings:
    return Settings.from_env(
        {
            "XP_PROMPT_POSTGRES_HOST": "127.0.0.1",
            "XP_PROMPT_POSTGRES_PORT": "5432",
            "XP_PROMPT_POSTGRES_USER": "xprompt",
            "XP_PROMPT_POSTGRES_PASSWORD": "database-secret",
            "XP_PROMPT_POSTGRES_DB": "xprompt",
            "XP_PROMPT_REDIS_HOST": "127.0.0.1",
            "XP_PROMPT_REDIS_PORT": "6379",
            "XP_PROMPT_REDIS_PASSWORD": "redis-secret",
            "XP_PROMPT_REDIS_DB": "2",
            "XP_PROMPT_QDRANT_URL": "http://xp-prompt-qdrant:6333",
            "XP_PROMPT_SERVICE_SECRET": ROOT_SECRET,
        }
    )


def catalog() -> chart_catalog.Catalog:
    return chart_catalog.Catalog.from_payload({"viz_types": VIZ})


def draft(**overrides) -> chart_draft.ChartDraft:
    fields = {
        "id": "11111111-1111-1111-1111-111111111111",
        "viz_type": "echarts_timeseries_line",
        "title": "score over submitted_at",
        "dataset_id": 65,
        "dataset_name": "Assessment Analytics",
        "database": "File uploads",
        "status": chart_draft.DRAFT,
        "version": 1,
        "form_data": {"viz_type": "echarts_timeseries_line", "x_axis": "submitted_at"},
        "rationale": "time axis with 24 values",
        "candidate_id": "trend:echarts_timeseries_line",
        "ranking_source": "rules",
        "superset_user_id": 1,
    }
    fields.update(overrides)
    return chart_draft.ChartDraft(**fields)


class ValidationTest(unittest.TestCase):
    def test_a_valid_configuration_is_stored_as_given(self) -> None:
        body = chart_draft.validate_form(
            catalog(),
            "echarts_timeseries_line",
            {"x_axis": "d", "metrics": ["m"]},
        )

        self.assertEqual(body["x_axis"], "d")
        self.assertEqual(body["metrics"], ["m"])

    def test_an_unsupported_control_is_refused(self) -> None:
        with self.assertRaises(chart_draft.ChartDraftError) as caught:
            chart_draft.validate_form(
                catalog(), "pie", {"groupby": ["c"], "metric": "m", "series_limit": 3}
            )

        self.assertEqual(caught.exception.code, "invalid_configuration")
        self.assertIn("series_limit", str(caught.exception))

    def test_an_unavailable_viz_type_is_refused(self) -> None:
        with self.assertRaises(chart_draft.ChartDraftError):
            chart_draft.validate_form(
                chart_catalog.Catalog.from_payload({}),
                "pie",
                {"groupby": ["c"], "metric": "m"},
            )

    def test_an_oversized_configuration_is_refused(self) -> None:
        body = {"x_axis": "d", "metrics": ["m"] * 3_000}

        with self.assertRaises(chart_draft.ChartDraftError) as caught:
            chart_draft.validate_form(catalog(), "echarts_timeseries_line", body)
        self.assertIn(caught.exception.code, {"configuration_too_large", "invalid_configuration"})

    def test_the_catalog_fingerprint_is_stable_and_content_free(self) -> None:
        first = chart_draft.catalog_fingerprint(catalog())
        second = chart_draft.catalog_fingerprint(catalog())

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)


class PublicShapeTest(unittest.TestCase):
    def test_lineage_is_visible(self) -> None:
        payload = draft(
            conversation_id="22222222-2222-2222-2222-222222222222",
            correlation_id="corr-1",
            profile_fingerprint="p" * 64,
            superset_slice_id=41,
            run_count=3,
        ).to_public_dict()

        self.assertEqual(payload["conversation_id"], "22222222-2222-2222-2222-222222222222")
        self.assertEqual(payload["correlation_id"], "corr-1")
        self.assertEqual(payload["profile_fingerprint"], "p" * 64)
        self.assertEqual(payload["superset_slice_id"], 41)
        self.assertEqual(payload["run_count"], 3)

    def test_a_replay_is_flagged(self) -> None:
        self.assertTrue(draft(replayed=True).to_public_dict()["replayed"])

    def test_only_an_open_draft_is_editable(self) -> None:
        self.assertTrue(draft().is_open)
        self.assertFalse(draft(status=chart_draft.SAVED).is_open)
        self.assertFalse(draft(status=chart_draft.SCRAPPED).is_open)

    def test_the_configuration_is_returned(self) -> None:
        payload = draft().to_public_dict()

        self.assertEqual(payload["form_data"]["x_axis"], "submitted_at")


class ErrorTest(unittest.TestCase):
    def test_a_conflict_names_both_versions(self) -> None:
        error = chart_draft.ChartDraftConflict(expected=1, actual=4)

        self.assertEqual(error.expected, 1)
        self.assertEqual(error.actual, 4)
        self.assertIn("1", str(error))
        self.assertIn("4", str(error))

    def test_the_statuses_are_the_three_documented_ones(self) -> None:
        self.assertEqual(chart_draft.STATUSES, ("draft", "saved", "scrapped"))

    def test_a_missing_draft_is_a_lookup_error(self) -> None:
        self.assertTrue(issubclass(chart_draft.ChartDraftNotFound, LookupError))


# --- ranking (P7-F04) ------------------------------------------------------


def candidate(candidate_id: str, viz: str = "echarts_timeseries_line") -> chart_rules.ChartCandidate:
    return chart_rules.ChartCandidate(
        id=candidate_id,
        viz_type=viz,
        family="trend",
        title=f"title {candidate_id}",
        form_data={
            "viz_type": viz,
            "x_axis": "d",
            "metrics": [chart_rules.adhoc_metric("m")],
        },
        rationale="because",
        score=0.5,
    )


def routed(content: str) -> RoutedResponse:
    return RoutedResponse(
        response=ProviderResponse(
            provider="gemini",
            model="gemini-2.5-flash",
            content=content,
            finish_reason="stop",
            request_id="req-1",
            usage=TokenUsage(10, 0, 5, 0, 15, "reported"),
        ),
        attempts=(
            AttemptSummary(
                provider="gemini",
                model="gemini-2.5-flash",
                outcome="succeeded",
                attempt=1,
                correlation_id="corr-1",
                operation=chart_ranking.RANKING_OPERATION,
            ),
        ),
    )


CANDIDATES = (candidate("a"), candidate("b"), candidate("c"))


class ClampTest(unittest.TestCase):
    def test_the_model_may_reorder_what_it_was_given(self) -> None:
        ranking = chart_ranking.clamp(
            {"order": ["c", "a", "b"], "rationale": "c is clearest"}, CANDIDATES
        )

        self.assertEqual(ranking.order, ("c", "a", "b"))
        self.assertEqual(ranking.source, chart_ranking.MODEL)
        self.assertIn("clearest", ranking.notes)

    def test_an_invented_candidate_is_discarded(self) -> None:
        ranking = chart_ranking.clamp(
            {"order": ["a", "invented", "b"], "rationale": "x"}, CANDIDATES
        )

        self.assertEqual(ranking.order, ("a", "b", "c"))

    def test_a_dropped_candidate_is_appended_not_lost(self) -> None:
        ranking = chart_ranking.clamp({"order": ["b"], "rationale": ""}, CANDIDATES)

        self.assertEqual(sorted(ranking.order), ["a", "b", "c"])
        self.assertEqual(ranking.order[0], "b")

    def test_duplicates_collapse(self) -> None:
        ranking = chart_ranking.clamp({"order": ["a", "a", "b"]}, CANDIDATES)

        self.assertEqual(ranking.order, ("a", "b", "c"))

    def test_a_malformed_document_keeps_the_rules_order(self) -> None:
        ranking = chart_ranking.clamp("not a dict", CANDIDATES)

        self.assertEqual(ranking.order, ("a", "b", "c"))

    def test_the_notes_are_bounded(self) -> None:
        ranking = chart_ranking.clamp({"order": [], "rationale": "x" * 5_000}, CANDIDATES)

        self.assertLessEqual(len(ranking.notes), chart_ranking.MAX_NOTES_CHARS)

    def test_ordering_never_loses_a_candidate(self) -> None:
        ranking = chart_ranking.Ranking(order=("c", "a"), source=chart_ranking.MODEL)
        ordered = chart_ranking.apply_order(CANDIDATES, ranking)

        self.assertEqual([item.id for item in ordered], ["c", "a", "b"])


class ProviderTest(unittest.TestCase):
    def test_without_a_provider_the_rules_order_stands(self) -> None:
        with mock.patch.object(
            chart_ranking.registry,
            "resolve_router",
            side_effect=chart_ranking.registry.ProviderConfigError("no provider"),
        ):
            ranking = chart_ranking.rank(
                settings(), tenant_key="default", candidates=CANDIDATES
            )

        self.assertEqual(ranking.source, chart_ranking.RULES)
        self.assertEqual(ranking.order, ("a", "b", "c"))

    def test_a_provider_failure_keeps_the_rules_order(self) -> None:
        from xp_prompt.providers.types import ProviderError

        router = mock.Mock()
        router.generate.side_effect = ProviderError(
            provider="gemini", code="http_402", message="quota", retryable=False
        )
        with mock.patch.object(
            chart_ranking.registry, "resolve_router", return_value=router
        ):
            ranking = chart_ranking.rank(
                settings(), tenant_key="default", candidates=CANDIDATES
            )

        self.assertEqual(ranking.source, chart_ranking.RULES)
        self.assertIn("http_402", ranking.notes)

    def test_a_good_answer_is_used(self) -> None:
        router = mock.Mock()
        router.generate.return_value = routed(
            '{"order": ["b", "a", "c"], "rationale": "b answers it"}'
        )
        with mock.patch.object(
            chart_ranking.registry, "resolve_router", return_value=router
        ):
            ranking = chart_ranking.rank(
                settings(), tenant_key="default", candidates=CANDIDATES
            )

        self.assertEqual(ranking.source, chart_ranking.MODEL)
        self.assertEqual(ranking.order, ("b", "a", "c"))

    def test_a_malformed_answer_keeps_the_rules_order(self) -> None:
        router = mock.Mock()
        router.generate.return_value = routed("not json at all")
        with mock.patch.object(
            chart_ranking.registry, "resolve_router", return_value=router
        ):
            ranking = chart_ranking.rank(
                settings(), tenant_key="default", candidates=CANDIDATES
            )

        self.assertEqual(ranking.source, chart_ranking.RULES)
        self.assertEqual(ranking.order, ("a", "b", "c"))

    def test_the_request_carries_only_the_candidates(self) -> None:
        request = chart_ranking.build_request(CANDIDATES, "which chart?")

        self.assertTrue(request.json_output)
        self.assertEqual(request.json_required_keys, ("order", "rationale"))
        self.assertIn("id=a", request.messages[1].content)
        self.assertIn("which chart?", request.messages[1].content)

    def test_no_candidates_means_no_call(self) -> None:
        ranking = chart_ranking.rank(settings(), tenant_key="default", candidates=())

        self.assertEqual(ranking.order, ())
        self.assertEqual(ranking.source, chart_ranking.RULES)


if __name__ == "__main__":
    unittest.main()
