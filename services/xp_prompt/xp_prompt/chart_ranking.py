"""Optional AI ranking of chart candidates (P7-F04).

The rule engine already produces a usable order. A model is allowed to do exactly one
extra thing: say which of *these* candidates answers the question best, and why. It cannot
add a candidate, change a viz type, or invent a control — the response is a list of ids
that are intersected with the candidates that were offered.

If there is no provider, the provider is down (this deployment's key answers ``http_402``),
or the answer is malformed, the deterministic order stands and the result says so. Ranking
is an improvement, never a dependency.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from xp_prompt import chart_rules, usage
from xp_prompt.config import Settings
from xp_prompt.providers import registry
from xp_prompt.providers.structured import StructuredOutputError, parse_json_object
from xp_prompt.providers.types import Message, ProviderError, ProviderRequest

logger = logging.getLogger(__name__)

RULES = "rules"
MODEL = "model"

RANKING_OPERATION = "chart.ranking"
RANKING_MAX_TOKENS = 512
RANKING_TEMPERATURE = 0.0
MAX_QUESTION_CHARS = 500
MAX_NOTES_CHARS = 400

RANKING_SYSTEM_PROMPT = (
    "You order candidate charts for a business question.\n"
    "Rules:\n"
    "- Only use the candidate ids you are given. Never invent one.\n"
    "- You cannot change a chart's type, fields, or settings.\n"
    "- Prefer the chart that answers the question most directly and honestly.\n"
    'Respond with one JSON object and nothing else:\n'
    '{{"order": ["<candidate id>", ...], "rationale": "<one or two sentences>"}}'
)


@dataclass(frozen=True, slots=True)
class Ranking:
    """The chosen order, where it came from, and why."""

    order: tuple[str, ...] = ()
    source: str = RULES
    notes: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {"order": list(self.order), "source": self.source, "notes": self.notes}


def rank(
    settings: Settings,
    *,
    tenant_key: str,
    candidates: tuple[chart_rules.ChartCandidate, ...],
    question: str = "",
) -> Ranking:
    """Ask the model to order these candidates; fall back to the rules on any doubt."""
    default = Ranking(order=tuple(candidate.id for candidate in candidates))
    if not candidates:
        return default
    try:
        router = registry.resolve_router(
            settings,
            tenant_key=tenant_key,
            context=usage.Context(
                tenant_key=tenant_key, module="chart", feature="ranking"
            ),
        )
    except registry.ProviderConfigError:
        return default
    request = build_request(candidates, question)
    try:
        routed = router.generate(request)
    except ProviderError as exc:
        logger.info("chart_ranking outcome=fallback provider_code=%s", exc.code)
        return Ranking(
            order=default.order, source=RULES, notes=f"provider:{exc.code}"[:MAX_NOTES_CHARS]
        )
    except Exception as exc:  # pragma: no cover - transport failure
        logger.info("chart_ranking outcome=fallback reason=%s", type(exc).__name__)
        return Ranking(order=default.order, source=RULES, notes="provider:error")

    try:
        document = parse_json_object(routed.response.content)
    except StructuredOutputError:
        logger.info("chart_ranking outcome=fallback reason=structured_output")
        return Ranking(
            order=default.order, source=RULES, notes="model:malformed_response"
        )
    ranking = clamp(document, candidates)
    logger.info("chart_ranking outcome=%s size=%d", ranking.source, len(ranking.order))
    return ranking


def build_request(
    candidates: tuple[chart_rules.ChartCandidate, ...], question: str
) -> ProviderRequest:
    """One small request: the question, and the candidates that already passed the rules."""
    lines = []
    for candidate in candidates:
        lines.append(
            f"- id={candidate.id} type={candidate.viz_type} title={candidate.title} "
            f"why={candidate.rationale}"
        )
    text = (question or "").strip()[:MAX_QUESTION_CHARS]
    sections = []
    if text:
        sections.append(f"Question: {text}")
    sections.append("Candidates:\n" + "\n".join(lines))
    return ProviderRequest(
        operation=RANKING_OPERATION,
        messages=(
            Message("system", RANKING_SYSTEM_PROMPT),
            Message("user", "\n\n".join(sections)),
        ),
        max_output_tokens=RANKING_MAX_TOKENS,
        temperature=RANKING_TEMPERATURE,
        json_output=True,
        json_required_keys=("order", "rationale"),
        reasoning_budget_tokens=0,
        correlation_id=uuid.uuid4().hex,
    )


def clamp(
    document: Any, candidates: tuple[chart_rules.ChartCandidate, ...]
) -> Ranking:
    """Keep only ids that were offered, in the order given; append the rest.

    This is the whole safety story of the model step: it may reorder, it may explain, and
    anything else it says is discarded.
    """
    offered = [candidate.id for candidate in candidates]
    known = set(offered)
    body = document if isinstance(document, dict) else {}
    requested = body.get("order")
    order: list[str] = []
    for entry in requested if isinstance(requested, (list, tuple)) else ():
        name = str(entry or "").strip()
        if name in known and name not in order:
            order.append(name)
    # Anything the model dropped keeps its deterministic place at the end.
    order.extend(candidate for candidate in offered if candidate not in order)
    notes = " ".join(str(body.get("rationale") or "").split())[:MAX_NOTES_CHARS]
    return Ranking(order=tuple(order), source=MODEL, notes=notes)


def apply_order(
    candidates: tuple[chart_rules.ChartCandidate, ...], ranking: Ranking
) -> tuple[chart_rules.ChartCandidate, ...]:
    """Reorder candidates by a ranking, without ever losing one."""
    by_id = {candidate.id: candidate for candidate in candidates}
    ordered = [by_id[name] for name in ranking.order if name in by_id]
    ordered.extend(
        candidate for candidate in candidates if candidate.id not in ranking.order
    )
    return tuple(ordered)
