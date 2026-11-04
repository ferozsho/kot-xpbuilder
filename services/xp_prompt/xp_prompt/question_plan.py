"""Clarify a question before SQL is written (P4-F01).

The intent router (P2-F04) decides *what kind* of turn this is. This module decides
whether the question is answerable at all: which measure, which dimension, which time
grain, which filter, and whether something in the question does not exist in the
schema.

Two rules keep it honest:

* **deterministic, not clever.** No model call: matching is over the authorized
  slice's own names plus a small vocabulary of analytical words. A clarification that
  is wrong costs the user a turn, so the bar for interrupting is high.
* **only material ambiguity blocks.** Asking "how many students?" when the schema has
  one obvious count is not ambiguous. Blocking is limited to two cases: the question
  names something that is not in the schema (``unknown_reference``), and it asks for
  an aggregate while several unrelated numeric measures could answer it with nothing
  in the question to choose between them (``measure_ambiguous``). Everything else
  becomes a *note* in the prompt, not a refusal.

The plan is also the prompt's outline: it tells the model which columns the user
actually mentioned, which is what keeps a generated statement close to the question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from xp_prompt.schema_slice import Dataset, SchemaSlice

#: Words that ask for a row count. "How many students" is a COUNT, never a SUM of
#: whatever numeric column happens to exist.
COUNT_PHRASES: tuple[str, ...] = (
    "how many",
    "count",
    "number of",
    "distribution",
    "breakdown",
)

#: Words that ask for a value, which is the only case where a numeric column is a
#: reasonable thing to infer when the question does not name one.
VALUE_PHRASES: tuple[str, ...] = (
    "how much",
    "total",
    "sum",
    "average",
    "avg",
    "mean",
    "median",
    "rate",
    "percentage",
    "percent",
)

#: Kept for callers that only want "is this an aggregate question".
AGGREGATE_WORDS: frozenset[str] = frozenset(COUNT_PHRASES + VALUE_PHRASES)

#: Time vocabulary -> the grain a query should group by.
GRAINS: tuple[tuple[str, str], ...] = (
    ("per day", "day"),
    ("daily", "day"),
    ("by day", "day"),
    ("per week", "week"),
    ("weekly", "week"),
    ("by week", "week"),
    ("per month", "month"),
    ("monthly", "month"),
    ("by month", "month"),
    ("per quarter", "quarter"),
    ("quarterly", "quarter"),
    ("by quarter", "quarter"),
    ("per year", "year"),
    ("yearly", "year"),
    ("annual", "year"),
    ("by year", "year"),
)

COMPARISON_WORDS: frozenset[str] = frozenset(
    {"compare", "compared", "versus", "vs", "against", "trend", "growth"}
)

#: Suffixes that look like a metric a user might name, used to spot a reference the
#: schema cannot satisfy ("how much revenue" with no revenue column).
METRIC_SUFFIXES: tuple[str, ...] = (
    "count",
    "total",
    "amount",
    "revenue",
    "cost",
    "price",
    "rate",
    "score",
    "progress",
    "duration",
    "spend",
)

MAX_CANDIDATES = 6
MAX_WORD_CHARS = 60
MAX_BLOCKING = 2

MEASURE_AMBIGUOUS = "measure_ambiguous"
UNKNOWN_REFERENCE = "unknown_reference"

#: Codes that stop the turn and ask the user, instead of guessing.
BLOCKING_CODES: frozenset[str] = frozenset({MEASURE_AMBIGUOUS, UNKNOWN_REFERENCE})

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,%d}" % MAX_WORD_CHARS)


@dataclass(frozen=True, slots=True)
class QuestionPlan:
    """What the question asks for, as far as the schema can tell."""

    measures: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    grain: str = ""
    filters: tuple[str, ...] = ()
    comparison: str = ""
    ambiguous: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: A short list of what does exist, used to make a clarification concrete.
    hint: str = ""

    @property
    def blocking(self) -> tuple[str, ...]:
        return tuple(code for code in self.ambiguous if code in BLOCKING_CODES)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "measures": list(self.measures),
            "dimensions": list(self.dimensions),
            "grain": self.grain,
            "filters": list(self.filters),
            "comparison": self.comparison,
            "ambiguous": list(self.ambiguous),
            "blocking": list(self.blocking),
            "hint": self.hint,
        }

    def prompt_block(self) -> str:
        """The outline a candidate prompt includes, or an empty string."""
        lines: list[str] = []
        if self.measures:
            lines.append("Measures mentioned: " + ", ".join(self.measures))
        if self.dimensions:
            lines.append("Dimensions mentioned: " + ", ".join(self.dimensions))
        if self.grain:
            lines.append(f"Time grain: {self.grain}")
        if self.filters:
            lines.append("Filters mentioned: " + "; ".join(self.filters))
        if self.comparison:
            lines.append(f"Comparison: {self.comparison}")
        if self.notes:
            lines.extend(self.notes)
        if not lines:
            return ""
        return "\n".join(f"- {line}" for line in lines)

    def clarification(self) -> str:
        """One question back to the user, naming what could not be resolved."""
        if UNKNOWN_REFERENCE in self.ambiguous:
            return (
                "I could not match something in that question to the available "
                f"data. Did you mean one of: {self.hint or 'the available columns'}?"
            )
        if MEASURE_AMBIGUOUS in self.ambiguous:
            return (
                "Which number should I measure? The data has several: "
                + ", ".join(self.measures)
                + "."
            )
        return "Could you rephrase the question with the metric and grouping you want?"


def analyse(question: str, schema: SchemaSlice) -> QuestionPlan:
    """Extract the plan for one question. Pure: no model, no database."""
    text = " ".join((question or "").lower().split())
    if not text or not schema.datasets:
        return QuestionPlan()

    words = set(_WORD.findall(text))
    measures, dimensions, unknown = _match_names(schema, text, words)
    grain = _grain(text)
    filters = _filters(text, dimensions)
    comparison = "previous_period" if words & COMPARISON_WORDS else ""

    ambiguous: list[str] = []
    notes: list[str] = []

    if unknown:
        ambiguous.append(UNKNOWN_REFERENCE)
        notes.append("Columns available: " + _column_sample(schema))
        # A reference we cannot resolve blocks the turn; do not also guess a measure.
        return QuestionPlan(
            dimensions=tuple(dimensions[:MAX_CANDIDATES]),
            grain=grain,
            filters=tuple(filters[:MAX_CANDIDATES]),
            comparison=comparison,
            ambiguous=tuple(ambiguous[:MAX_BLOCKING]),
            notes=tuple(notes[:MAX_CANDIDATES]),
            hint=_column_sample(schema),
        )

    asks_count = any(phrase in text for phrase in COUNT_PHRASES)
    asks_value = any(phrase in text for phrase in VALUE_PHRASES)
    if asks_count and not measures:
        notes.append("A row count answers this question; do not sum a value column.")
    elif asks_value and not measures:
        numeric = _numeric_columns(schema)
        if len(numeric) > 1:
            ambiguous.append(MEASURE_AMBIGUOUS)
            measures = numeric[:MAX_CANDIDATES]
        elif len(numeric) == 1:
            measures = (numeric[0],)
            notes.append(f"Only one numeric column is available: {numeric[0]}.")

    if grain and not _has_time_column(schema):
        notes.append(
            "No date column is available, so the time grain cannot be applied."
        )
    if comparison and not _has_time_column(schema):
        notes.append("No date column is available, so a period comparison is not possible.")

    return QuestionPlan(
        measures=tuple(measures[:MAX_CANDIDATES]),
        dimensions=tuple(dimensions[:MAX_CANDIDATES]),
        grain=grain,
        filters=tuple(filters[:MAX_CANDIDATES]),
        comparison=comparison,
        ambiguous=tuple(ambiguous[:MAX_BLOCKING]),
        notes=tuple(notes[:MAX_CANDIDATES]),
    )


def _match_names(
    schema: SchemaSlice, text: str, words: set[str]
) -> tuple[list[str], list[str], list[str]]:
    """(measures, dimensions, unknown metric-like references)."""
    measures: list[str] = []
    dimensions: list[str] = []
    known_tokens: set[str] = set()
    for dataset in schema.datasets:
        known_tokens.update(_tokens(dataset.name))
        for column in dataset.columns:
            tokens = _tokens(column.name)
            known_tokens.update(tokens)
            if not tokens:
                continue
            mentioned = (
                " ".join(tokens) in text
                if len(tokens) > 1
                else tokens[0] in words
            )
            if not mentioned:
                continue
            if _is_numeric(column.type):
                if column.name not in measures:
                    measures.append(column.name)
            elif column.name not in dimensions:
                dimensions.append(column.name)

    unknown: list[str] = []
    if not measures:
        for word in sorted(words):
            if word in known_tokens:
                continue
            if any(word.endswith(suffix) for suffix in METRIC_SUFFIXES):
                # "revenue" with no revenue column: the user named a metric we do
                # not have, which is worth asking about rather than guessing.
                if word not in AGGREGATE_WORDS:
                    unknown.append(word)
    return measures, dimensions, unknown


def _grain(text: str) -> str:
    for phrase, grain in GRAINS:
        if phrase in text:
            return grain
    return ""


def _filters(text: str, dimensions: list[str]) -> list[str]:
    """Only explicit ``<dimension> is/of <value>`` shapes, which are unambiguous."""
    found: list[str] = []
    for dimension in dimensions[:MAX_CANDIDATES]:
        # A column is written ``completion_status`` and spoken "completion status",
        # so the pattern accepts either separator between its words.
        name = r"[\s_]+".join(re.escape(token) for token in _tokens(dimension))
        if not name:  # pragma: no cover - defensive
            continue
        pattern = re.compile(
            rf"\b{name}\b\s+(?:is|are|=|of|for)\s+"
            rf"([A-Za-z0-9_\- ]{{1,{MAX_WORD_CHARS}}})"
        )
        match = pattern.search(text)
        if match:
            found.append(f"{dimension} = {match.group(1).strip()}")
    return found


def _is_numeric(type_name: str) -> bool:
    upper = str(type_name or "").upper()
    return any(
        hint in upper
        for hint in ("INT", "FLOAT", "DOUBLE", "REAL", "DECIMAL", "NUMERIC", "NUMBER")
    )


def _tokens(name: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", (name or "").lower()) if token]


def _numeric_columns(schema: SchemaSlice) -> list[str]:
    seen: list[str] = []
    for dataset in schema.datasets:
        for column in dataset.columns:
            if _is_numeric(column.type) and column.name not in seen:
                seen.append(column.name)
    return seen


def _has_time_column(schema: SchemaSlice) -> bool:
    for dataset in schema.datasets:
        for column in dataset.columns:
            if column.temporal:
                return True
    return False


def _column_sample(schema: SchemaSlice, limit: int = 8) -> str:
    """A short, de-duplicated list of real column names for a clarification."""
    names: list[str] = []
    for dataset in schema.datasets:
        for column in dataset.columns:
            if column.name not in names:
                names.append(column.name)
            if len(names) >= limit:
                return ", ".join(names)
    return ", ".join(names) if names else "no columns"


def datasets_named(question: str, schema: SchemaSlice) -> tuple[Dataset, ...]:
    """Datasets whose name the question mentions (used by prompts and audit)."""
    text = " ".join((question or "").lower().split())
    matched = [
        dataset
        for dataset in schema.datasets
        if dataset.name.lower() in text
    ]
    return tuple(matched[:MAX_CANDIDATES])
