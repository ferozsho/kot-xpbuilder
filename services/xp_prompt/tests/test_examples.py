"""Tests for the validated-example library (P4-F03).

Retrieval is exercised against the real database in runtime-evidence §7.31; these
tests pin the two properties that must not drift: the similarity measure and the
prompt block's shape.
"""

from __future__ import annotations

import unittest

from xp_prompt import examples


class SimilarityTest(unittest.TestCase):
    def test_the_same_question_scores_one(self) -> None:
        self.assertEqual(
            examples.similarity("How many students?", "how many students?"), 1.0
        )

    def test_stopwords_do_not_create_similarity(self) -> None:
        """Two questions made only of stopwords share no content, so no score."""
        self.assertEqual(examples.similarity("how many of the", "what is it for"), 0.0)

    def test_a_related_question_scores_between(self) -> None:
        score = examples.similarity(
            "How many students per course?",
            "Students per course",
        )

        self.assertGreater(score, 0.5)
        self.assertLess(score, 1.0)

    def test_an_unrelated_question_scores_zero(self) -> None:
        self.assertEqual(
            examples.similarity("How many students?", "Revenue by region"), 0.0
        )

    def test_empty_input_is_not_similar(self) -> None:
        self.assertEqual(examples.similarity("", "anything"), 0.0)


class TokenTest(unittest.TestCase):
    def test_short_words_are_dropped(self) -> None:
        self.assertEqual(examples.tokens("a of how many students"), {"many", "students"})


class PromptBlockTest(unittest.TestCase):
    def example(self, question="How many students?", sql="SELECT COUNT(*) FROM t"):
        return examples.Example(
            question=question, sql=sql, dialect="postgres", score=0.9
        )

    def test_an_empty_library_produces_no_block(self) -> None:
        self.assertEqual(examples.prompt_block([]), "")

    def test_the_block_contains_the_question_and_the_statement(self) -> None:
        block = examples.prompt_block([self.example()])

        self.assertIn("How many students?", block)
        self.assertIn("SELECT COUNT(*) FROM t", block)

    def test_the_block_is_bounded(self) -> None:
        many = [
            self.example(question=f"Question {index}", sql="SELECT " + "x" * 400)
            for index in range(examples.MAX_LIMIT)
        ]

        block = examples.prompt_block(many)

        self.assertLessEqual(len(block), examples.MAX_BLOCK_CHARS + 120)


class ConfigurationTest(unittest.TestCase):
    def test_defaults_are_small(self) -> None:
        self.assertLessEqual(examples.DEFAULT_LIMIT, examples.MAX_LIMIT)
        self.assertGreaterEqual(examples.MIN_SCORE, 0.3)


if __name__ == "__main__":
    unittest.main()
