"""Intent routing for the XP Prompt workspace (P2-F04).

Every turn is classified *before* a provider is called, so the workspace can
ask a focused question instead of guessing, and so the model is told what kind
of answer is expected. Classification is deliberately **deterministic**:

* it costs no tokens, so routing happens even when no provider is configured;
* it is auditable — each decision carries the signal ids that produced it, and
  those ids are a fixed vocabulary, never a fragment of the user's text;
* it is testable without a network, so the routing table cannot regress
  silently.

The seven routable intents come from the phase plan: ``query``, ``dataset``,
``chart``, ``dashboard``, ``explanation``, ``modification`` and the
``clarification`` outcome produced when the request does not identify one of
them (or when a modification has nothing to modify).

Subsumption is what keeps this usable: a chart request is also a query, and a
dashboard request is also a chart and a query, so a tie between them resolves
to the more specific intent rather than asking the user to pick. A tie between
unrelated intents (say a dataset *and* a chart) is a genuine ambiguity and is
returned as ``clarification``.

Model-based classification is intentionally out of scope here: it would add a
paid round-trip to every turn and a second place that can invent an intent. If
it is ever added, it belongs in a later phase behind the usage ledger (P9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

QUERY = "query"
DATASET = "dataset"
CHART = "chart"
DASHBOARD = "dashboard"
EXPLANATION = "explanation"
MODIFICATION = "modification"
CLARIFICATION = "clarification"

#: Intents a turn can be routed to (``clarification`` is an outcome, not a route).
INTENTS: tuple[str, ...] = (
    QUERY,
    DATASET,
    CHART,
    DASHBOARD,
    EXPLANATION,
    MODIFICATION,
)
ALL_INTENTS: tuple[str, ...] = INTENTS + (CLARIFICATION,)

WEIGHT_STRONG = 2.0
WEIGHT_WEAK = 0.5
#: A route needs at least one strong signal; weak signals alone are ambiguous.
MIN_SCORE = 2.0
#: A short reply after a clarification ("yes", "the chart") adopts the intent
#: the clarification was parked on.
REPLY_WEIGHT = 2.0
MAX_REPLY_WORDS = 8

#: More specific intent -> the intents it contains. ``chart subsumes query``
#: because "show me a chart of revenue" is one request with one answer.
_SUBSUMES: dict[str, frozenset[str]] = {
    DASHBOARD: frozenset({CHART, QUERY, DATASET}),
    CHART: frozenset({QUERY}),
    DATASET: frozenset({QUERY}),
}
#: Ranking used when a tie survives subsumption but still needs an order.
_PRIORITY: tuple[str, ...] = (
    MODIFICATION,
    EXPLANATION,
    DASHBOARD,
    DATASET,
    CHART,
    QUERY,
)

_HUMAN: dict[str, str] = {
    QUERY: "a data answer",
    DATASET: "a dataset",
    CHART: "a chart",
    DASHBOARD: "a dashboard",
    EXPLANATION: "an explanation",
    MODIFICATION: "a change to an existing draft",
}

NO_SIGNAL_QUESTION = (
    "I can help with a data question, a dataset, a chart, or a dashboard. "
    "Which one do you need, and for which metric or time range?"
)
WEAK_SIGNAL_QUESTION = (
    "I could not tell what you want me to produce from that. Could you say "
    "whether you need a data answer, a dataset, a chart, or a dashboard?"
)
NO_CONTEXT_QUESTION = (
    "I do not have a draft in this conversation to change yet. Should I create "
    "{target} first?"
)
PICK_QUESTION = "Did you mean {first} or {second}? A one-word answer is enough."

#: Extra system-prompt guidance per routed intent. Kept beside the classifier so
#: the routing decision and the instruction it implies cannot drift apart.
PROMPT_GUIDANCE: dict[str, str] = {
    QUERY: (
        "This turn is a data question. XPBuilder executes the SQL you write "
        "against the tables under Available data and shows the user the real "
        "result, so write that SQL instead of saying you cannot query the "
        "database or that the user should run it themselves. State the metric, "
        "the filters and the time range, answer briefly, and never invent a "
        "result you were not given."
    ),
    EXPLANATION: (
        "This turn asks for an explanation. Explain the concept or the earlier "
        "result concisely and say when you are unsure."
    ),
    DATASET: (
        "This turn asks for a dataset. Describe the source table, joins, and "
        "columns you would propose, and make clear that nothing is saved until "
        "the user confirms."
    ),
    CHART: (
        "This turn asks for a visualisation. Recommend one chart type with the "
        "dimensions and measures it needs, and make clear that nothing is "
        "published."
    ),
    DASHBOARD: (
        "This turn asks for a dashboard. Outline the sections and charts it "
        "needs, and make clear that nothing is published."
    ),
    MODIFICATION: (
        "This turn changes an earlier draft. Restate which draft you are "
        "changing and what changes, and keep the rest of the draft intact."
    ),
}

_FLAGS = re.IGNORECASE

_PRIOR_REFERENCE = re.compile(
    r"\b(it|this|that|these|those|same|previous|earlier|again|instead)\b", _FLAGS
)
_EXPLICIT_EXPLANATION = re.compile(
    r"\b(explain|why|help me understand|define|difference between)\b", _FLAGS
)
_REPLY_START = re.compile(
    r"^(yes|yeah|yep|ok|okay|sure|please|go ahead|do it|the|a|an|"
    r"query|dataset|chart|dashboard|explanation)\b",
    _FLAGS,
)
_NEGATIVE_REPLY = re.compile(r"^(no|nope|not|never|none|don'?t)\b", _FLAGS)


def _pattern(regex: str) -> re.Pattern[str]:
    return re.compile(regex, _FLAGS)


#: (intent, ((compiled pattern, weight, stable signal id), ...))
_SIGNALS: tuple[tuple[str, tuple[tuple[re.Pattern[str], float, str], ...]], ...] = (
    (
        EXPLANATION,
        (
            (_pattern(r"\bwhat (is|are|does)\b"), WEIGHT_STRONG, "explain.what_is"),
            (_pattern(r"\bwhy\b"), WEIGHT_STRONG, "explain.why"),
            (
                _pattern(r"\bhow (does|do|did|is|are)\b[\w\s,'-]{0,40}\bwork"),
                WEIGHT_STRONG,
                "explain.mechanism",
            ),
            (_pattern(r"\bexplain\b"), WEIGHT_STRONG, "explain.explain"),
            (_pattern(r"\bmean(s|ing)?\b"), WEIGHT_STRONG, "explain.meaning"),
            (_pattern(r"\bdefine\b"), WEIGHT_STRONG, "explain.define"),
            (
                _pattern(r"\bdifference between\b"),
                WEIGHT_STRONG,
                "explain.difference",
            ),
            (
                _pattern(r"\bhelp me understand\b"),
                WEIGHT_STRONG,
                "explain.understand",
            ),
        ),
    ),
    (
        QUERY,
        (
            (_pattern(r"\bhow (many|much)\b"), WEIGHT_STRONG, "query.count"),
            (_pattern(r"\blist\b"), WEIGHT_STRONG, "query.list"),
            (_pattern(r"\bshow\b"), WEIGHT_WEAK, "query.show"),
            (_pattern(r"\bcount(s|ed)?\b"), WEIGHT_STRONG, "query.count_word"),
            (_pattern(r"\btrends?\b"), WEIGHT_STRONG, "query.trend"),
            (_pattern(r"\bover time\b"), WEIGHT_STRONG, "query.over_time"),
            (_pattern(r"\bcompare\b"), WEIGHT_STRONG, "query.compare"),
            (
                _pattern(r"\bbreak ?down\b|\bbreak it down\b"),
                WEIGHT_STRONG,
                "query.breakdown",
            ),
            (
                _pattern(
                    r"\b(by|per) (day|week|month|quarter|year|term|school|"
                    r"teacher|student|learner|course|class|program|level)\b"
                ),
                WEIGHT_STRONG,
                "query.group_by",
            ),
            (
                _pattern(r"\b(top|bottom) (\d+|ten|five)\b"),
                WEIGHT_STRONG,
                "query.top_n",
            ),
            (
                _pattern(
                    r"\b(total|sum|average|avg|median|mean|percentage|percent|"
                    r"rate)\b"
                ),
                WEIGHT_WEAK,
                "query.aggregate",
            ),
            (
                _pattern(r"\b(last|this|previous) (month|week|year|quarter|term)\b"),
                WEIGHT_WEAK,
                "query.period",
            ),
            (_pattern(r"\bdata for\b"), WEIGHT_WEAK, "query.data_for"),
        ),
    ),
    (
        CHART,
        (
            (_pattern(r"\bcharts?\b"), WEIGHT_STRONG, "chart.noun"),
            (_pattern(r"\bgraphs?\b"), WEIGHT_STRONG, "chart.graph"),
            (_pattern(r"\bplots?\b"), WEIGHT_STRONG, "chart.plot"),
            (
                _pattern(r"\bvisuali[sz](e|es|ed|ing|ation)\b"),
                WEIGHT_STRONG,
                "chart.visualise",
            ),
            (
                _pattern(
                    r"\b(bar|line|pie|donut|scatter|area|histogram|heatmap|"
                    r"bubble|funnel|treemap|gauge|waterfall|radar)\b"
                ),
                WEIGHT_STRONG,
                "chart.type",
            ),
        ),
    ),
    (
        DASHBOARD,
        (
            (_pattern(r"\bdashboards?\b"), WEIGHT_STRONG, "dashboard.noun"),
            (_pattern(r"\breport page\b"), WEIGHT_STRONG, "dashboard.report_page"),
            (_pattern(r"\bkpis?\b"), WEIGHT_WEAK, "dashboard.kpi"),
        ),
    ),
    (
        DATASET,
        (
            (_pattern(r"\bdata ?sets?\b"), WEIGHT_STRONG, "dataset.noun"),
            (
                _pattern(r"\b(data model|semantic layer|virtual table)\b"),
                WEIGHT_STRONG,
                "dataset.model",
            ),
            (_pattern(r"\bsaved (query|table)\b"), WEIGHT_STRONG, "dataset.saved"),
            (_pattern(r"\b(columns?|fields?)\b"), WEIGHT_WEAK, "dataset.columns"),
        ),
    ),
    (
        MODIFICATION,
        (
            (
                _pattern(r"\bmake (it|this|that)\b"),
                WEIGHT_STRONG,
                "modify.make_it",
            ),
            (
                _pattern(r"\bchang(e|es|ing)\b"),
                WEIGHT_STRONG,
                "modify.change",
            ),
            (
                _pattern(r"\bupdat(e|es|ing)\b"),
                WEIGHT_STRONG,
                "modify.update",
            ),
            (
                _pattern(r"\bmodif(y|ies|ied|ying)\b"),
                WEIGHT_STRONG,
                "modify.modify",
            ),
            (_pattern(r"\binstead\b"), WEIGHT_STRONG, "modify.instead"),
            (
                _pattern(r"\b(turn|convert) (it|this|that) into\b"),
                WEIGHT_STRONG,
                "modify.convert",
            ),
            (_pattern(r"\brename\b"), WEIGHT_STRONG, "modify.rename"),
            (_pattern(r"\bswap\b"), WEIGHT_STRONG, "modify.swap"),
            (
                _pattern(
                    r"\b(remove|drop) (the )?(filter|column|series|metric|"
                    r"dimension)\b"
                ),
                WEIGHT_STRONG,
                "modify.remove",
            ),
            (
                _pattern(
                    r"\badd (a |another )?(filter|column|series|metric|"
                    r"dimension)\b"
                ),
                WEIGHT_STRONG,
                "modify.add",
            ),
            (
                _pattern(r"\bcompare (this|that|it)\b"),
                WEIGHT_STRONG,
                "modify.compare",
            ),
            (
                _pattern(r"\bthe same (chart|query|dashboard|dataset)\b"),
                WEIGHT_STRONG,
                "modify.same",
            ),
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class IntentDecision:
    """The routing outcome for one user message.

    ``reasons`` is the audit trail: a fixed vocabulary of signal ids, never a
    copy of the message, so a decision can be logged safely.
    """

    intent: str
    confidence: float
    reasons: tuple[str, ...] = ()
    #: For ``modification``/``clarification``: the object the user is aiming at.
    target_intent: str | None = None
    #: Question to show when the router needs one more detail.
    clarifying_question: str | None = None
    #: Intent a follow-up answer should be re-classified into.
    pending_intent: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))

    @property
    def is_clarification(self) -> bool:
        return self.intent == CLARIFICATION


def classify(
    content: str,
    *,
    has_prior_answer: bool = False,
    pending_intent: str | None = None,
) -> IntentDecision:
    """Route one user message to an intent, asking when the request is unclear.

    ``has_prior_answer`` says whether this conversation already holds an
    assistant turn a modification could refer to; ``pending_intent`` is the
    intent carried by the previous clarification, so a one-word reply can
    continue where the router left off.
    """
    text = " ".join((content or "").split())
    if not text:
        return _clarification(NO_SIGNAL_QUESTION, ("empty",))

    if pending_intent in INTENTS and _NEGATIVE_REPLY.match(text):
        # A refused clarification must not silently become the parked intent.
        return _clarification(NO_SIGNAL_QUESTION, ("declined",))

    scores, reasons = _score(text)
    _adopt_pending_intent(scores, reasons, text, pending_intent)

    if not scores:
        return _clarification(NO_SIGNAL_QUESTION, ("no_signal",))

    top_score = max(scores.values())
    if top_score < MIN_SCORE:
        return _clarification(WEAK_SIGNAL_QUESTION, (*reasons, "weak_signal"))

    total = sum(scores.values()) or 1.0
    confidence = round(top_score / total, 2)

    # A question about cause or meaning is answered with prose even when it also
    # names an object ("explain the enrollment trend per school").
    if (
        scores.get(EXPLANATION, 0.0) >= MIN_SCORE
        and _EXPLICIT_EXPLANATION.search(text)
    ):
        return IntentDecision(
            intent=EXPLANATION,
            confidence=confidence,
            reasons=reasons,
            target_intent=EXPLANATION,
        )

    # A change cue that points back at earlier context ("make it a line chart",
    # "use a line chart instead") is a modification even when it also names the
    # target object, because the object is what changes, not what gets created.
    if (
        scores.get(MODIFICATION, 0.0) >= MIN_SCORE
        and _PRIOR_REFERENCE.search(text)
    ):
        return _modification(
            scores,
            reasons,
            confidence=confidence,
            has_prior_answer=has_prior_answer,
        )

    tier = sorted(
        (name for name, score in scores.items() if score == top_score),
        key=_priority,
    )

    # "what is this chart" has no explicit explain verb, so the tie decides.
    if EXPLANATION in tier and len(tier) > 1:
        return IntentDecision(
            intent=EXPLANATION,
            confidence=confidence,
            reasons=reasons,
            target_intent=EXPLANATION,
        )

    specific = _most_specific(tier)
    if len(specific) > 1:
        first, second = specific[0], specific[1]
        return _clarification(
            PICK_QUESTION.format(first=_HUMAN[first], second=_HUMAN[second]),
            (*reasons, "ambiguous"),
            pending_intent=None,
            target_intent=None,
            confidence=confidence,
        )

    route = specific[0]
    if route == MODIFICATION:
        return _modification(
            scores,
            reasons,
            confidence=confidence,
            has_prior_answer=has_prior_answer,
        )
    return IntentDecision(
        intent=route,
        confidence=confidence,
        reasons=reasons,
        target_intent=route,
    )


def _modification(
    scores: dict[str, float],
    reasons: tuple[str, ...],
    *,
    confidence: float,
    has_prior_answer: bool,
) -> IntentDecision:
    """Route a modification, or ask which draft to change when there is none."""
    target = _target_intent(scores)
    if not has_prior_answer:
        return _clarification(
            NO_CONTEXT_QUESTION.format(
                target=_HUMAN.get(target or "", "a new draft")
            ),
            (*reasons, "modify.no_context"),
            pending_intent=target,
            target_intent=target,
            confidence=confidence,
        )
    return IntentDecision(
        intent=MODIFICATION,
        confidence=confidence,
        reasons=reasons,
        target_intent=target,
    )


def _score(text: str) -> tuple[dict[str, float], list[str]]:
    """Sum the matched signal weights per intent, keeping the signal ids."""
    scores: dict[str, float] = {}
    reasons: list[str] = []
    for intent, signals in _SIGNALS:
        for pattern, weight, label in signals:
            if pattern.search(text):
                scores[intent] = scores.get(intent, 0.0) + weight
                reasons.append(label)
    return scores, reasons


def _adopt_pending_intent(
    scores: dict[str, float],
    reasons: list[str],
    text: str,
    pending_intent: str | None,
) -> None:
    """Let a short reply continue the intent its clarification was parked on."""
    if pending_intent not in INTENTS:
        return
    if scores.get(pending_intent, 0.0) >= MIN_SCORE:
        return
    if len(text.split()) > MAX_REPLY_WORDS or not _REPLY_START.match(text):
        return
    scores[pending_intent] = max(scores.get(pending_intent, 0.0), REPLY_WEIGHT)
    reasons.append(f"context.pending.{pending_intent}")


def _target_intent(scores: dict[str, float]) -> str | None:
    """Best non-modification intent in play, used to name the draft's type."""
    candidates = [
        (name, score)
        for name, score in scores.items()
        if name != MODIFICATION and name in INTENTS
    ]
    if not candidates:
        return None
    name, score = max(candidates, key=lambda item: (item[1], -_priority(item[0])))
    return name if score >= MIN_SCORE else None


def _most_specific(tier: list[str]) -> list[str]:
    """Drop any intent that a higher-scoring sibling in the tier contains."""
    return [
        name
        for name in tier
        if not any(name in _SUBSUMES.get(other, frozenset()) for other in tier)
    ]


def _priority(name: str) -> int:
    try:
        return _PRIORITY.index(name)
    except ValueError:  # pragma: no cover - every intent is ranked
        return len(_PRIORITY)


def _clarification(
    question: str,
    reasons: tuple[str, ...],
    *,
    pending_intent: str | None = None,
    target_intent: str | None = None,
    confidence: float = 0.0,
) -> IntentDecision:
    return IntentDecision(
        intent=CLARIFICATION,
        confidence=confidence,
        reasons=reasons,
        target_intent=target_intent,
        clarifying_question=question,
        pending_intent=pending_intent,
    )
