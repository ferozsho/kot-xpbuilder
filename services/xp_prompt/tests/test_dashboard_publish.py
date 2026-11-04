"""Tests for dashboard publishing: modes, access, drift and the ledger.

The phase rules are tested one at a time, because each of them is the thing that
stops a specific accident:

* P8-F08 — a mode is stated, and a replayed request cannot publish twice;
* P8-F09 — access the caller may not grant is refused before anything exists;
* P8-F10 — what an attempt created, and what became of it, is always recorded;
* P8-F11 — a manual edit is never overwritten by accident.
"""

from __future__ import annotations

import unittest
from unittest import mock

from xp_prompt import dashboard_publish
from xp_prompt.config import Settings

ROOT_SECRET = "unit-test-root-secret-value"
TENANT_ID = "11111111-1111-1111-1111-111111111111"
PUBLISH_ID = "22222222-2222-2222-2222-222222222222"
POSITION = {
    "DASHBOARD_VERSION_KEY": "v2",
    "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
    "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": ["ROW-1"]},
}


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


def payload(**overrides) -> dict:
    body = {
        "mode": "create",
        "title": "Admissions overview",
        "goal": "see which programmes are converting",
        "slots": [{"id": "slot-1", "chart_id": 7, "dataset_id": 3}],
        "visibility": "private",
        "idempotency_key": "publish-1",
    }
    body.update(overrides)
    return body


def row(**overrides) -> dict:
    base = {
        "id": PUBLISH_ID,
        "mode": "create",
        "status": "pending",
        "title": "Admissions overview",
        "goal": "",
        "dashboard_id": None,
        "dashboard_slug": "",
        "target_dashboard_id": None,
        "owners": "[]",
        "roles": "[]",
        "visibility": "private",
        "guest_access": False,
        "slot_count": 1,
        "expected_fingerprint": "",
        "position_fingerprint": "",
        "created_objects": "[]",
        "cleanup": "[]",
        "error_code": "",
        "error_message": "",
        "correlation_id": "",
        "created_at": None,
        "published_at": None,
        "failed_at": None,
    }
    base.update(overrides)
    return base


class _FakeCursor:
    """Returns one scripted row per statement, and keeps the SQL for assertions."""

    def __init__(self, rows: list[dict | None] | None = None) -> None:
        self._rows = list(rows or [])
        self.statements: list[str] = []
        self.params: list[object] = []
        self.row: dict | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append(sql)
        self.params.append(params)
        self.row = self._rows.pop(0) if self._rows else None

    def fetchone(self) -> dict | None:
        return self.row

    def fetchall(self) -> list:
        return []


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return self._cursor


def run(module_rows: list[dict | None]):
    cursor = _FakeCursor(module_rows)
    return cursor, mock.patch.object(
        dashboard_publish, "connect", return_value=_FakeConnection(cursor)
    )


class ParseTests(unittest.TestCase):
    """A mode is stated, never inferred (P8-F08)."""

    def test_create_takes_no_dashboard_id(self) -> None:
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            dashboard_publish.Request.parse(payload(dashboard_id=12))
        self.assertEqual(caught.exception.code, "target_not_allowed")

    def test_update_needs_its_target(self) -> None:
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            dashboard_publish.Request.parse(payload(mode="update"))
        self.assertEqual(caught.exception.code, "target_required")

    def test_an_unknown_mode_is_refused(self) -> None:
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            dashboard_publish.Request.parse(payload(mode="upsert"))
        self.assertEqual(caught.exception.code, "invalid_mode")

    def test_a_title_is_required(self) -> None:
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            dashboard_publish.Request.parse(payload(title="   "))
        self.assertEqual(caught.exception.code, "title_required")

    def test_a_repeated_slot_is_refused(self) -> None:
        slots = [{"id": "slot-1"}, {"id": "slot-1"}]
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            dashboard_publish.Request.parse(payload(slots=slots))
        self.assertEqual(caught.exception.code, "duplicate_slot")

    def test_the_slot_budget_is_enforced(self) -> None:
        slots = [{"id": f"slot-{index}"} for index in range(9)]
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            dashboard_publish.Request.parse(payload(slots=slots))
        self.assertEqual(caught.exception.code, "slot_budget")

    def test_an_empty_dashboard_is_refused(self) -> None:
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            dashboard_publish.Request.parse(payload(slots=[]))
        self.assertEqual(caught.exception.code, "empty_dashboard")

    def test_the_idempotency_key_is_bounded(self) -> None:
        request = dashboard_publish.Request.parse(payload(idempotency_key="x" * 200))
        self.assertEqual(len(request.idempotency_key), 96)


class AccessTests(unittest.TestCase):
    """Access is assigned by policy, and unpermitted access is refused (P8-F09)."""

    def decide(self, request, policy=None, **kwargs):
        return dashboard_publish.decide(
            request,
            policy or dashboard_publish.Policy(),
            caller_id=99,
            **kwargs,
        )

    def test_publishing_may_be_refused_outright(self) -> None:
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            self.decide(
                dashboard_publish.Request.parse(payload()),
                dashboard_publish.Policy(can_publish=False),
            )
        self.assertEqual(caught.exception.code, "not_permitted")

    def test_the_caller_stays_an_owner(self) -> None:
        decision = self.decide(
            dashboard_publish.Request.parse(payload(owners=[4, 5]))
        )
        self.assertEqual(decision.owners, (4, 5, 99))

    def test_the_caller_is_the_only_owner_by_default(self) -> None:
        decision = self.decide(dashboard_publish.Request.parse(payload()))
        self.assertEqual(decision.owners, (99,))

    def test_owners_are_deduplicated(self) -> None:
        decision = self.decide(
            dashboard_publish.Request.parse(payload(owners=[4, 4, 99]))
        )
        self.assertEqual(decision.owners, (4, 99))

    def test_role_assignment_needs_the_capability(self) -> None:
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            self.decide(dashboard_publish.Request.parse(payload(roles=[2])))
        self.assertEqual(caught.exception.code, "role_not_allowed")

    def test_a_role_outside_the_allowlist_is_refused(self) -> None:
        policy = dashboard_publish.Policy(
            can_assign_roles=True, allowed_role_ids=frozenset({2})
        )
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            self.decide(
                dashboard_publish.Request.parse(payload(roles=[2, 3])), policy
            )
        self.assertEqual(caught.exception.code, "role_not_allowed")

    def test_a_permitted_role_is_kept(self) -> None:
        policy = dashboard_publish.Policy(
            can_assign_roles=True, allowed_role_ids=frozenset({2})
        )
        decision = self.decide(
            dashboard_publish.Request.parse(payload(roles=[2, 2])), policy
        )
        self.assertEqual(decision.roles, (2,))

    def test_public_sharing_needs_the_capability(self) -> None:
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            self.decide(dashboard_publish.Request.parse(payload(visibility="public")))
        self.assertEqual(caught.exception.code, "share_not_allowed")

    def test_guest_access_needs_embedding(self) -> None:
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            self.decide(
                dashboard_publish.Request.parse(
                    payload(visibility="tenant", guest_access=True)
                )
            )
        self.assertEqual(caught.exception.code, "embed_not_allowed")

    def test_guest_access_cannot_sit_on_a_private_dashboard(self) -> None:
        policy = dashboard_publish.Policy(can_embed=True)
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            self.decide(
                dashboard_publish.Request.parse(payload(guest_access=True)), policy
            )
        self.assertEqual(caught.exception.code, "embed_requires_share")

    def test_guest_access_on_a_shared_dashboard_is_allowed(self) -> None:
        policy = dashboard_publish.Policy(can_embed=True)
        decision = self.decide(
            dashboard_publish.Request.parse(
                payload(visibility="tenant", guest_access=True)
            ),
            policy,
        )
        self.assertTrue(decision.guest_access)
        self.assertEqual(decision.visibility, "tenant")

    def test_the_policy_payload_is_parsed_without_assuming_capabilities(self) -> None:
        policy = dashboard_publish.Policy.from_payload({"can_embed": True})
        self.assertTrue(policy.can_embed)
        self.assertFalse(policy.can_assign_roles)
        self.assertFalse(policy.can_share_publicly)


class ConflictTests(unittest.TestCase):
    """An update aimed at an edited dashboard is refused (P8-F11)."""

    def decide(self, request, **kwargs):
        return dashboard_publish.decide(
            request, dashboard_publish.Policy(), caller_id=99, **kwargs
        )

    def update(self, **overrides):
        return dashboard_publish.Request.parse(
            payload(mode="update", dashboard_id=5, **overrides)
        )

    def test_a_stale_version_is_refused(self) -> None:
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            self.decide(self.update(expected_version=1), live_version=3)
        self.assertEqual(caught.exception.code, "stale_version")

    def test_a_changed_layout_is_refused(self) -> None:
        published = dashboard_publish.position_fingerprint(POSITION)
        edited = {**POSITION, "GRID_ID": {"type": "GRID", "id": "GRID_ID",
                                          "children": []}}
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            self.decide(
                self.update(expected_fingerprint=published),
                live_position=edited,
            )
        self.assertEqual(caught.exception.code, "dashboard_changed")

    def test_an_unchanged_layout_is_allowed(self) -> None:
        published = dashboard_publish.position_fingerprint(POSITION)
        decision = self.decide(
            self.update(expected_fingerprint=published), live_position=POSITION
        )
        self.assertEqual(decision.position_fingerprint, "")
        self.assertEqual(decision.owners, (99,))

    def test_force_overwrites_a_manual_edit(self) -> None:
        decision = self.decide(
            self.update(expected_version=1, force=True),
            live_position={"ROOT_ID": {"edited": True}},
            live_version=9,
        )
        self.assertEqual(decision.owners, (99,))

    def test_an_unknown_expectation_does_not_block(self) -> None:
        decision = self.decide(self.update(), live_position=POSITION, live_version=2)
        self.assertEqual(decision.owners, (99,))

    def test_the_fingerprint_ignores_key_order(self) -> None:
        left = dashboard_publish.position_fingerprint({"a": 1, "b": {"c": 2}})
        right = dashboard_publish.position_fingerprint({"b": {"c": 2}, "a": 1})
        self.assertEqual(left, right)
        self.assertEqual(len(left), 64)

    def test_no_layout_has_no_fingerprint(self) -> None:
        self.assertEqual(dashboard_publish.position_fingerprint(None), "")
        self.assertEqual(dashboard_publish.position_fingerprint({}), "")


class LedgerTests(unittest.TestCase):
    """An attempt records what it created, and what became of it (P8-F10)."""

    def start(self, rows):
        cursor, patcher = run(rows)
        with patcher:
            record, replayed = dashboard_publish.start(
                settings(),
                dashboard_publish.Request.parse(payload()),
                dashboard_publish.Decision(
                    owners=(99,),
                    roles=(),
                    visibility="private",
                    guest_access=False,
                    position_fingerprint="",
                ),
                tenant_key="default",
                superset_user_id=99,
            )
        return record, replayed, cursor

    def test_a_new_attempt_is_recorded_as_pending(self) -> None:
        # tenant lookup, no replay, then the insert returning the new row
        record, replayed, cursor = self.start([{"id": TENANT_ID}, None, row()])

        self.assertFalse(replayed)
        self.assertEqual(record["status"], "pending")
        self.assertIn("INSERT INTO ai_dashboard_publish", cursor.statements[-1])

    def test_a_replay_returns_the_open_row_without_inserting(self) -> None:
        existing = row(status="published", dashboard_id=41)
        record, replayed, cursor = self.start([{"id": TENANT_ID}, existing])

        self.assertTrue(replayed)
        self.assertEqual(record["dashboard_id"], 41)
        self.assertEqual(len(cursor.statements), 2)
        self.assertNotIn("INSERT", cursor.statements[-1])

    def test_a_duplicate_key_is_checked_before_anything_is_written(self) -> None:
        _, _, cursor = self.start([{"id": TENANT_ID}, None, row()])

        self.assertIn("idempotency_key = %s", cursor.statements[1])
        self.assertIn("superset_user_id", cursor.statements[1])
        self.assertIn("INSERT", cursor.statements[-1])

    def test_an_unprovisioned_tenant_is_refused(self) -> None:
        cursor, patcher = run([None])
        with patcher, self.assertRaises(dashboard_publish.DashboardPublishError) as \
                caught:
            dashboard_publish.start(
                settings(),
                dashboard_publish.Request.parse(payload()),
                dashboard_publish.Decision((99,), (), "private", False, ""),
                tenant_key="ghost",
                superset_user_id=99,
            )
        self.assertEqual(caught.exception.code, "unknown_tenant")
        self.assertEqual(len(cursor.statements), 1)

    def complete(self, rows, **kwargs):
        cursor, patcher = run(rows)
        with patcher:
            record = dashboard_publish.complete(
                settings(),
                PUBLISH_ID,
                tenant_key="default",
                superset_user_id=99,
                **kwargs,
            )
        return record, cursor

    def test_completion_records_the_dashboard_and_objects(self) -> None:
        record, cursor = self.complete(
            [row(status="published", dashboard_id=41)],
            dashboard_id=41,
            dashboard_slug="admissions",
            position=POSITION,
            created=[{"kind": "chart", "id": 7, "action": "create"}],
        )

        self.assertEqual(record["status"], "published")
        statement = cursor.statements[0]
        self.assertIn("status = 'published'", statement)
        self.assertIn("AND p.status = 'pending'", statement)
        self.assertIn("RETURNING p.*", statement)
        # The fingerprint is written here, so the next update can detect drift.
        self.assertIn("position_fingerprint", statement)
        self.assertEqual(cursor.params[0][2], dashboard_publish.position_fingerprint(
            POSITION
        ))

    def test_completion_needs_a_dashboard_id(self) -> None:
        with self.assertRaises(dashboard_publish.DashboardPublishError) as caught:
            self.complete([row()], dashboard_id=None)
        self.assertEqual(caught.exception.code, "dashboard_required")

    def test_a_closed_attempt_cannot_be_completed_again(self) -> None:
        with self.assertRaises(dashboard_publish.DashboardPublishStateError):
            self.complete([None], dashboard_id=41)

    def fail(self, rows, **kwargs):
        cursor, patcher = run(rows)
        with patcher:
            record = dashboard_publish.fail(
                settings(),
                PUBLISH_ID,
                tenant_key="default",
                superset_user_id=99,
                **kwargs,
            )
        return record, cursor

    def test_a_failure_records_the_cleanup_outcomes(self) -> None:
        record, cursor = self.fail(
            [row(status="failed", error_code="layout_invalid")],
            error_code="layout_invalid",
            error_message="row 1 exceeds the grid",
            created=[{"kind": "chart", "id": 7}],
            cleanup=[{"kind": "chart", "id": 7, "outcome": "deleted"}],
        )

        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_code"], "layout_invalid")
        self.assertIn("status = 'failed'", cursor.statements[0])
        self.assertIn("AND p.status = 'pending'", cursor.statements[0])

    def test_a_closed_attempt_cannot_fail_again(self) -> None:
        with self.assertRaises(dashboard_publish.DashboardPublishStateError):
            self.fail([None], error_code="publish_failed")

    def test_cleanup_entries_keep_their_outcome(self) -> None:
        stored = (
            '[{"kind":"chart","id":7,"action":"create","outcome":"deleted",'
            '"detail":""}]'
        )
        record, _ = self.fail(
            [row(status="failed", cleanup=stored)], error_code="publish_failed"
        )
        self.assertEqual(record["cleanup"][0]["outcome"], "deleted")

    def test_an_object_without_an_outcome_is_still_recorded(self) -> None:
        _, cursor = self.fail(
            [row(status="failed")],
            error_code="publish_failed",
            created=[{"kind": "chart", "id": 7}],
        )
        written = cursor.params[0][2]
        self.assertIn('"id":7', written)
        self.assertIn('"outcome":""', written)


if __name__ == "__main__":
    unittest.main()
