"""Tests for the artifact lifecycle (P2-F07)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from xp_prompt import artifacts
from xp_prompt.config import Settings

ROOT_SECRET = "unit-test-root-secret-value"


def settings() -> Settings:
    return Settings.from_env(
        {
            "XP_PROMPT_POSTGRES_HOST": "xp-prompt-db",
            "XP_PROMPT_POSTGRES_PORT": "5432",
            "XP_PROMPT_POSTGRES_USER": "xprompt",
            "XP_PROMPT_POSTGRES_PASSWORD": "database-secret",
            "XP_PROMPT_POSTGRES_DB": "xprompt",
            "XP_PROMPT_REDIS_HOST": "superset-redis",
            "XP_PROMPT_REDIS_PORT": "6379",
            "XP_PROMPT_REDIS_PASSWORD": "redis-secret",
            "XP_PROMPT_REDIS_DB": "2",
            "XP_PROMPT_QDRANT_URL": "http://xp-prompt-qdrant:6333",
            "XP_PROMPT_SERVICE_SECRET": ROOT_SECRET,
        }
    )


def artifact(
    *,
    status: str = "draft",
    artifact_type: str = "chart",
    published_object_type: str | None = None,
    published_object_id: str | None = None,
) -> artifacts.ArtifactRecord:
    return artifacts.ArtifactRecord(
        id="11111111-1111-1111-1111-111111111111",
        conversation_id="22222222-2222-2222-2222-222222222222",
        artifact_type=artifact_type,
        status=status,
        title="Monthly enrolment",
        payload={"chart_type": "line"},
        published_object_type=published_object_type,
        published_object_id=published_object_id,
        created_at=datetime(2026, 11, 4, tzinfo=timezone.utc),
    )


class _Cursor:
    def __init__(self, row=None):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cursor


class VocabularyTest(unittest.TestCase):
    def test_one_lifecycle_covers_every_type(self) -> None:
        self.assertEqual(
            artifacts.ARTIFACT_STATUSES,
            ("draft", "confirmed", "published", "discarded"),
        )
        self.assertEqual(
            artifacts.ARTIFACT_TYPES, ("dataset", "chart", "dashboard")
        )


class TransitionRulesTest(unittest.TestCase):
    def test_only_a_draft_can_be_confirmed(self) -> None:
        self.assertTrue(artifacts.can_confirm(artifact(status="draft")))
        for status in ("confirmed", "published", "discarded"):
            self.assertFalse(artifacts.can_confirm(artifact(status=status)))

    def test_only_a_confirmed_artifact_can_be_published(self) -> None:
        self.assertTrue(artifacts.can_publish(artifact(status="confirmed")))
        for status in ("draft", "published", "discarded"):
            self.assertFalse(artifacts.can_publish(artifact(status=status)))

    def test_published_artifacts_cannot_be_discarded(self) -> None:
        self.assertTrue(artifacts.can_discard(artifact(status="draft")))
        self.assertTrue(artifacts.can_discard(artifact(status="confirmed")))
        self.assertFalse(artifacts.can_discard(artifact(status="published")))


class ValidationTest(unittest.TestCase):
    def create(self, **overrides) -> None:
        arguments = {
            "conversation_id": "22222222-2222-2222-2222-222222222222",
            "tenant_key": "default",
            "superset_user_id": 1,
            "artifact_type": "chart",
            "title": "Monthly enrolment",
            "payload": {"chart_type": "line"},
        }
        arguments.update(overrides)
        artifacts.create_artifact(settings(), **arguments)

    def test_unknown_type_is_rejected(self) -> None:
        with self.assertRaises(artifacts.ArtifactInputError):
            self.create(artifact_type="wordcloud")

    def test_long_title_is_rejected(self) -> None:
        with self.assertRaises(artifacts.ArtifactInputError):
            self.create(title="t" * (artifacts.MAX_TITLE_CHARS + 1))

    def test_oversized_payload_is_rejected(self) -> None:
        with self.assertRaises(artifacts.ArtifactInputError):
            self.create(payload={"sql": "x" * (artifacts.MAX_PAYLOAD_BYTES + 1)})

    def test_non_serializable_payload_is_rejected(self) -> None:
        with self.assertRaises(artifacts.ArtifactInputError):
            self.create(payload={"bad": object()})

    def test_publish_rejects_a_url_as_object_id(self) -> None:
        with self.assertRaises(artifacts.ArtifactInputError):
            artifacts.publish_artifact(
                settings(),
                "11111111-1111-1111-1111-111111111111",
                tenant_key="default",
                superset_user_id=1,
                object_type="chart",
                object_id="https://evil.example/x",
            )

    def test_publish_rejects_an_unknown_object_type(self) -> None:
        with self.assertRaises(artifacts.ArtifactInputError):
            artifacts.publish_artifact(
                settings(),
                "11111111-1111-1111-1111-111111111111",
                tenant_key="default",
                superset_user_id=1,
                object_type="query",
                object_id="7",
            )


class TransitionTest(unittest.TestCase):
    def test_confirm_of_a_missing_artifact_is_not_found(self) -> None:
        with mock.patch.object(artifacts, "get_artifact", return_value=None):
            with self.assertRaises(artifacts.ArtifactNotFound):
                artifacts.confirm_artifact(
                    settings(),
                    "11111111-1111-1111-1111-111111111111",
                    tenant_key="default",
                    superset_user_id=1,
                )

    def test_confirm_of_a_confirmed_artifact_is_a_state_error(self) -> None:
        with mock.patch.object(
            artifacts, "get_artifact", return_value=artifact(status="confirmed")
        ):
            with self.assertRaises(artifacts.ArtifactStateError):
                artifacts.confirm_artifact(
                    settings(),
                    "11111111-1111-1111-1111-111111111111",
                    tenant_key="default",
                    superset_user_id=1,
                )

    def test_confirm_moves_a_draft_to_confirmed(self) -> None:
        confirmed = artifact(status="confirmed")
        with mock.patch.object(
            artifacts, "connect", return_value=_Connection(_Cursor({"id": confirmed.id}))
        ), mock.patch.object(
            artifacts, "get_artifact", side_effect=[artifact(status="draft"), confirmed]
        ):
            record = artifacts.confirm_artifact(
                settings(),
                confirmed.id,
                tenant_key="default",
                superset_user_id=1,
            )

        self.assertEqual(record.status, "confirmed")

    def test_publish_records_the_native_object(self) -> None:
        published = artifact(
            status="published", published_object_type="chart", published_object_id="42"
        )
        with mock.patch.object(
            artifacts, "connect", return_value=_Connection(_Cursor({"id": published.id}))
        ), mock.patch.object(
            artifacts,
            "get_artifact",
            side_effect=[artifact(status="confirmed"), published],
        ):
            record = artifacts.publish_artifact(
                settings(),
                published.id,
                tenant_key="default",
                superset_user_id=1,
                object_type="chart",
                object_id="42",
            )

        self.assertEqual(record.status, "published")
        self.assertEqual(
            record.native_link, {"entity_type": "chart", "entity_id": "42"}
        )


class PublicShapeTest(unittest.TestCase):
    def test_unpublished_artifact_has_no_link(self) -> None:
        self.assertIsNone(artifact(status="draft").native_link)
        self.assertIsNone(
            artifact(status="published", published_object_type="chart").native_link
        )

    def test_public_dict_carries_payload_and_link(self) -> None:
        payload = artifact(
            status="published", published_object_type="dataset", published_object_id="9"
        ).to_public_dict()

        self.assertEqual(payload["payload"], {"chart_type": "line"})
        self.assertEqual(
            payload["native_link"], {"entity_type": "dataset", "entity_id": "9"}
        )

    def test_undecryptable_payload_does_not_hide_the_record(self) -> None:
        row = {
            "id": "11111111-1111-1111-1111-111111111111",
            "conversation_id": "22222222-2222-2222-2222-222222222222",
            "artifact_type": "chart",
            "status": "draft",
            "title": "Monthly enrolment",
            "payload_ciphertext": b"not-a-fernet-token",
            "job_id": None,
        }

        record = artifacts._artifact_from_row(row, settings())

        self.assertEqual(record.payload, {})
        self.assertEqual(record.title, "Monthly enrolment")


class IdentifierTest(unittest.TestCase):
    def test_uuid_shape_is_accepted(self) -> None:
        self.assertTrue(artifacts.is_artifact_id("22222222-2222-2222-2222-222222222222"))

    def test_other_shapes_are_rejected(self) -> None:
        self.assertFalse(artifacts.is_artifact_id("nope"))
        self.assertFalse(artifacts.is_artifact_id(None))


if __name__ == "__main__":
    unittest.main()
