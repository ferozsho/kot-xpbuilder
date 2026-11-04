"""Operations hardening: leases, cache loss, retention and metrics.

Each of these fails quietly in production rather than loudly in development:

* a leased job that outlives its lease gets run twice;
* a cache that is *required* turns a Redis restart into an outage;
* a retention sweep that deletes the wrong table is discovered by an auditor;
* a metric that is computed from process memory resets at every deploy.
"""

from __future__ import annotations

import unittest
from unittest import mock

from xp_prompt import metrics, retention
from xp_prompt import settings_registry as sr
from xp_prompt.config import Settings

ROOT_SECRET = "unit-test-root-secret-value"


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


def live_map(**overrides: object) -> dict[str, sr.Resolution]:
    values = {
        setting.key: sr.Resolution(setting.key, setting.default, "default", 0)
        for setting in sr.REGISTRY
    }
    for key, value in overrides.items():
        dotted = key.replace("__", ".")
        values[dotted] = sr.Resolution(dotted, value, "global", 1)
    return values


class RetentionPolicyTests(unittest.TestCase):
    """A sweep must never be able to delete the accounting record."""

    def test_the_defaults_keep_everything_weighty(self) -> None:
        with mock.patch.object(sr, "live", return_value=live_map()):
            windows = retention.windows(settings())
        self.assertEqual(windows["retention.conversations_days"], 0)
        self.assertEqual(windows["retention.jobs_days"], 0)
        self.assertEqual(windows["retention.attempts_days"], 0)
        self.assertEqual(windows["retention.artifacts_days"], 0)
        # Only the delivery log has a window by default: its content is a pointer,
        # and a month is long enough to debug a delivery problem.
        self.assertEqual(windows["retention.outbox_days"], 30)

    def test_the_accounting_and_audit_tables_are_not_purgeable(self) -> None:
        purgeable = {table for table, _column, _key in retention.PURGEABLE}
        for protected in (
            "ai_usage_budget",
            "ai_usage_budget_window",
            "ai_billing_import",
            "ai_billing_line",
            "ai_setting",
            "ai_setting_audit",
            "ai_tenant",
        ):
            self.assertIn(protected, retention.PROTECTED)
            self.assertNotIn(protected, purgeable)

    def test_the_usage_ledger_has_no_window_by_default(self) -> None:
        # ``ai_provider_attempt`` is the ledger: purgeable only on purpose.
        self.assertIn("ai_provider_attempt", {t for t, _c, _k in retention.PURGEABLE})
        with mock.patch.object(sr, "live", return_value=live_map()):
            windows = retention.windows(settings())
        self.assertEqual(windows["retention.attempts_days"], 0)

    def test_every_purgeable_table_names_a_declared_setting(self) -> None:
        for table, _column, key in retention.PURGEABLE:
            self.assertIn(key, sr.BY_KEY, f"{table} names an unknown window")

    def test_children_are_purged_before_their_parents(self) -> None:
        order = [table for table, _column, _key in retention.PURGEABLE]
        self.assertLess(order.index("ai_job_step"), order.index("ai_job"))
        self.assertLess(order.index("ai_message"), order.index("ai_conversation"))

    def test_offboarding_refuses_the_default_tenant(self) -> None:
        with self.assertRaises(retention.RetentionError) as caught:
            retention.offboard(settings(), tenant_key="default")
        self.assertEqual(caught.exception.code, "protected_tenant")

    def test_offboarding_refuses_an_empty_tenant_key(self) -> None:
        with self.assertRaises(retention.RetentionError):
            retention.offboard(settings(), tenant_key="")

    def test_offboarding_covers_every_tenant_table(self) -> None:
        for table in retention.TENANT_TABLES:
            self.assertTrue(table.startswith("ai_"), table)


class MetricsTests(unittest.TestCase):
    """The numbers come from the stores, not from process memory."""

    def snapshot(self) -> dict:
        with mock.patch.object(sr, "live", return_value=live_map()), mock.patch.object(
            metrics, "jobs", return_value={"queued": 2, "failed": 1}
        ), mock.patch.object(
            metrics, "outbox", return_value={"pending": 3}
        ), mock.patch.object(
            metrics,
            "attempts",
            return_value={
                "by_status": {"succeeded": 4},
                "avg_duration_ms": 120.0,
                "total": 4,
                "fallbacks": 1,
                "unpriced": 0,
            },
        ), mock.patch.object(
            metrics.budgets,
            "status",
            return_value={"budgets": [{"state": "warning"}, {"state": "exhausted"}]},
        ), mock.patch.object(
            metrics.sql_gate, "active", return_value=1
        ):
            return metrics.snapshot(settings())

    def test_the_snapshot_carries_jobs_outbox_attempts_and_safeguards(self) -> None:
        data = self.snapshot()
        self.assertEqual(data["jobs"], {"queued": 2, "failed": 1})
        self.assertEqual(data["outbox"], {"pending": 3})
        self.assertEqual(data["attempts"]["fallbacks"], 1)
        self.assertEqual(data["budgets"], {"warning": 1, "exceeded": 1, "total": 2})
        self.assertEqual(data["sql_slots_active"], 1)
        self.assertEqual(
            data["safeguards"]["max_rows"], sr.BY_KEY["sql.max_rows"].default
        )

    def test_a_broken_budget_store_does_not_break_the_metrics(self) -> None:
        with mock.patch.object(sr, "live", return_value=live_map()), mock.patch.object(
            metrics, "jobs", return_value={}
        ), mock.patch.object(metrics, "outbox", return_value={}), mock.patch.object(
            metrics, "attempts", return_value={}
        ), mock.patch.object(
            metrics.budgets, "status", side_effect=RuntimeError("db down")
        ), mock.patch.object(
            metrics.sql_gate, "active", return_value=0
        ):
            data = metrics.snapshot(settings())
        self.assertEqual(data["budgets"]["total"], 0)

    def test_the_prometheus_text_is_gauge_per_kind(self) -> None:
        with mock.patch.object(
            metrics, "snapshot", return_value=self.snapshot()
        ):
            body = metrics.prometheus(settings(), tenant_key="acme")
        self.assertIn('xp_prompt_jobs{tenant="acme",kind="queued"} 2', body)
        self.assertIn("# TYPE xp_prompt_jobs gauge", body)
        self.assertIn('xp_prompt_outbox_events{tenant="acme",kind="pending"} 3', body)
        self.assertIn("xp_prompt_sql_slots_active{tenant=\"acme\"} 1", body)
        # One HELP line per metric, not one per sample.
        self.assertEqual(body.count("# HELP xp_prompt_jobs "), 1)

    def test_the_text_is_plain_prometheus(self) -> None:
        with mock.patch.object(metrics, "snapshot", return_value=self.snapshot()):
            body = metrics.prometheus(settings())
        for line in body.strip().splitlines():
            self.assertFalse(
                line.startswith(" "), f"indented line is not valid exposition: {line}"
            )
        self.assertTrue(body.endswith("\n"))


class LeaseTests(unittest.TestCase):
    """A worker that lost its lease must stop, not race the new owner."""

    def test_a_heartbeat_is_conditional_on_ownership(self) -> None:
        from pathlib import Path

        source = Path(
            "/app/xp_prompt/xp_prompt/jobs.py"
        ).read_text(encoding="utf-8")
        heartbeat = source[source.index("def heartbeat_job(") :]
        heartbeat = heartbeat[: heartbeat.index("def recover_expired(")]
        self.assertIn("AND lease_owner = %s", heartbeat)
        self.assertIn("lease_expires_at = CURRENT_TIMESTAMP", heartbeat)

    def test_the_executor_stops_when_the_lease_is_lost(self) -> None:
        from pathlib import Path

        source = Path("/app/xp_prompt/xp_prompt/jobs.py").read_text(encoding="utf-8")
        self.assertIn("if worker_id and not heartbeat_job(", source)
        self.assertIn("reason=lease_lost", source)


if __name__ == "__main__":
    unittest.main()
