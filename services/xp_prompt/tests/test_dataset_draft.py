"""Tests for dataset drafts (P6-F05/F08/F09).

The database round trip is exercised on a scratch database (see runtime-evidence
§7.35); these tests pin the parts that must not drift: what a draft exposes, what the
conflict and state errors mean, and how bounded the free-form fields are.
"""

from __future__ import annotations

import unittest

from xp_prompt import dataset_draft


def draft(**overrides) -> dataset_draft.DatasetDraft:
    fields = {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "users_by_month",
        "kind": "virtual",
        "database": "lms",
        "status": dataset_draft.DRAFT,
        "version": 1,
        "spec": {"name": "users_by_month", "grain": ["id"]},
        "quality": {"ok": True, "codes": []},
        "quality_ok": True,
        "fingerprint": "a" * 64,
        "schema_fingerprint": "b" * 64,
        "sql_sha256": "c" * 64,
        "owners": ("admin",),
        "superset_user_id": 7,
    }
    fields.update(overrides)
    return dataset_draft.DatasetDraft(**fields)


class RedactionTest(unittest.TestCase):
    def test_the_statement_is_hidden_by_default(self) -> None:
        payload = draft(sql="select 1").to_public_dict()
        self.assertNotIn("sql", payload)
        self.assertTrue(payload["sql_hidden"])

    def test_the_statement_is_returned_on_request(self) -> None:
        payload = draft(sql="select 1").to_public_dict(include_sql=True)
        self.assertEqual(payload["sql"], "select 1")
        self.assertFalse(payload["sql_hidden"])

    def test_lineage_is_visible(self) -> None:
        payload = draft(
            conversation_id="22222222-2222-2222-2222-222222222222",
            correlation_id="corr-1",
            superset_dataset_id=42,
        ).to_public_dict()
        self.assertEqual(payload["conversation_id"], "22222222-2222-2222-2222-222222222222")
        self.assertEqual(payload["correlation_id"], "corr-1")
        self.assertEqual(payload["superset_dataset_id"], 42)
        self.assertEqual(payload["fingerprint"], "a" * 64)
        self.assertEqual(payload["schema_fingerprint"], "b" * 64)

    def test_a_replay_is_flagged_so_a_caller_does_not_count_it_twice(self) -> None:
        self.assertTrue(draft(replayed=True).to_public_dict()["replayed"])
        self.assertFalse(draft().to_public_dict()["replayed"])

    def test_only_an_open_draft_is_editable(self) -> None:
        self.assertTrue(draft().is_open)
        self.assertFalse(draft(status=dataset_draft.SAVED).is_open)
        self.assertFalse(draft(status=dataset_draft.SCRAPPED).is_open)


class ConflictTest(unittest.TestCase):
    def test_a_conflict_names_both_versions(self) -> None:
        error = dataset_draft.DraftConflict(expected=2, actual=5)
        self.assertEqual(error.expected, 2)
        self.assertEqual(error.actual, 5)
        self.assertIn("2", str(error))
        self.assertIn("5", str(error))

    def test_eventual_and_permanent_errors_are_distinguishable(self) -> None:
        self.assertTrue(issubclass(dataset_draft.DraftNotFound, LookupError))
        self.assertTrue(issubclass(dataset_draft.DraftConflict, RuntimeError))
        self.assertEqual(
            dataset_draft.DraftInputError("bad", "nope").code, "bad"
        )
        self.assertEqual(
            dataset_draft.DraftStateError("not_open", "nope").code, "not_open"
        )


class BoundsTest(unittest.TestCase):
    def test_owners_are_deduplicated_and_bounded(self) -> None:
        owners = dataset_draft._owners(["admin", "admin", " " + "x" * 200])
        self.assertEqual(owners[0], "admin")
        self.assertEqual(len(owners[1]), 128)
        self.assertEqual(len(owners), 2)

    def test_a_non_list_of_owners_is_empty(self) -> None:
        self.assertEqual(dataset_draft._owners("admin"), [])
        self.assertEqual(dataset_draft._owners(None), [])

    def test_an_absent_statement_has_no_digest(self) -> None:
        self.assertEqual(dataset_draft._sha256("   "), "")

    def test_the_same_statement_has_one_digest(self) -> None:
        self.assertEqual(
            dataset_draft._sha256("select 1"), dataset_draft._sha256("select 1")
        )


class StatusVocabularyTest(unittest.TestCase):
    def test_the_statuses_are_the_three_documented_ones(self) -> None:
        self.assertEqual(
            dataset_draft.STATUSES, ("draft", "saved", "scrapped")
        )


if __name__ == "__main__":
    unittest.main()
