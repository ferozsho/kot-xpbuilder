"""The canonical usage ledger: one row per provider attempt, priced at the time.

Phase 3 already reported every attempt, but only as a log line and only to a
health callback. That answers "is the provider braking" and not "what did this
month cost, which module spent it, and does the sum survive a restart". This
module is the durable answer.

The rules, one per feature:

* **one logical request, many attempts, no double counting (P9-F01).** Every
  attempt of one request shares a ``correlation_id``; the attempt ordinal, the
  provider and the ordinal together identify the row, and a partial unique index
  makes recording idempotent. A retry is a different ordinal and a fallback is a
  different provider, so neither is a duplicate — and a replayed delivery is.
* **the dimensions are recorded, not inferred (P9-F02).** Provider, model,
  tenant, user, module, operation, every token category, latency, outcome,
  provider request id and the fallback flag are columns, so a question about any
  of them is a query rather than a log grep.
* **reported is not estimated (P9-F03).** ``usage_source`` says which. When the
  provider reports nothing the token columns stay NULL and the cost stays NULL:
  an unknown count is not zero, and inventing one would make the total wrong in
  the confident direction.
* **money is integer micros (P9-F04/F05).** Rates live in ``ai_model_price``
  versions with an effective interval; an attempt snapshots the version it used,
  so changing a price never rewrites history.

Recording is best effort by design: a ledger write that fails must not fail the
user's answer, so :func:`recorder` swallows and logs. Losing a datapoint is
recoverable; losing the response is not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from xp_prompt import outbox
from xp_prompt.config import Settings
from xp_prompt.db import connect

logger = logging.getLogger(__name__)

#: The outbox event this module produces and consumes (P9-F06).
USAGE_AGGREGATE = "ai_provider_attempt"
USAGE_EVENT = "usage.attempt"

#: What started the call. A closed set, so a dashboard groups rather than splinters.
MODULES: tuple[str, ...] = (
    "chat",
    "sql",
    "repair",
    "dataset",
    "chart",
    "dashboard",
    "provider_test",
    "other",
)

REPORTED = "reported"
MISSING = "missing"

MAX_MODULE_CHARS = 32
MAX_FEATURE_CHARS = 64
MAX_CORRELATION_CHARS = 64
MAX_ERROR_DETAIL_CHARS = 512
MAX_SOURCE_CHARS = 64
MAX_SOURCE_URL_CHARS = 255
MICROS_PER_UNIT = 1_000_000
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 500
MAX_GROUP_VALUES = 8

#: Dimensions a summary may be grouped by. Deliberately small: a group-by over a
#: free-form column is how an admin dashboard becomes an unindexed table scan.
GROUP_BY: tuple[str, ...] = ("provider", "model", "module", "status", "currency")


class UsageError(ValueError):
    """A refused ledger operation; ``code`` is stable and safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def usage_error(code: str, message: str) -> UsageError:
    """Raise-by-value, so a consumer can fail with a stable code."""
    return UsageError(code, message)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """One version of one model's price list, valid for one interval."""

    provider: str
    model: str
    version: int
    currency: str
    unit_tokens: int = MICROS_PER_UNIT
    input_micros_per_unit: int = 0
    output_micros_per_unit: int = 0
    #: ``None`` means the provider does not discount cached input, which is not
    #: the same as discounting it to zero.
    cached_input_micros_per_unit: int | None = None
    reasoning_micros_per_unit: int | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    source: str = ""
    source_url: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "version": self.version,
            "currency": self.currency,
            "unit_tokens": self.unit_tokens,
            "input_micros_per_unit": self.input_micros_per_unit,
            "cached_input_micros_per_unit": self.cached_input_micros_per_unit,
            "output_micros_per_unit": self.output_micros_per_unit,
            "reasoning_micros_per_unit": self.reasoning_micros_per_unit,
            "effective_from": _isoformat(self.effective_from),
            "effective_to": _isoformat(self.effective_to),
            "source": self.source,
            "source_url": self.source_url,
        }


@dataclass(frozen=True, slots=True)
class Attempt:
    """One provider attempt, as the ledger stores it."""

    provider: str
    model: str
    operation: str
    attempt_ordinal: int
    status: str = "succeeded"
    correlation_id: str = ""
    module: str = "other"
    feature: str = ""
    error_class: str | None = None
    error_detail: str = ""
    fallback: bool = False
    provider_request_id: str | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    usage_source: str = MISSING
    duration_ms: int | None = None
    price_version: int | None = None
    currency: str = ""
    estimated_cost_micros: int | None = None
    price_source: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "operation": self.operation,
            "attempt": self.attempt_ordinal,
            "status": self.status,
            "correlation_id": self.correlation_id,
            "module": self.module,
            "feature": self.feature,
            "error_class": self.error_class,
            "error_detail": self.error_detail,
            "fallback": self.fallback,
            "provider_request_id": self.provider_request_id,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "usage_source": self.usage_source,
            "duration_ms": self.duration_ms,
            "price_version": self.price_version,
            "currency": self.currency,
            "estimated_cost_micros": self.estimated_cost_micros,
            "price_source": self.price_source,
        }


@dataclass(frozen=True, slots=True)
class Context:
    """Who and what a set of attempts belongs to.

    Passed to :func:`recorder` once per logical request, so the router can record
    without knowing anything about tenants, users or modules.
    """

    tenant_key: str = "default"
    module: str = "other"
    feature: str = ""
    superset_user_id: int | None = None
    conversation_id: str | None = None
    job_id: str | None = None
    step_id: str | None = None

    def normalized(self) -> Context:
        module = (self.module or "other").strip().lower()
        if module not in MODULES:
            module = "other"
        return Context(
            tenant_key=self.tenant_key or "default",
            module=module,
            feature=_text(self.feature, MAX_FEATURE_CHARS),
            superset_user_id=self.superset_user_id,
            conversation_id=self.conversation_id,
            job_id=self.job_id,
            step_id=self.step_id,
        )


def estimate_cost_micros(
    price: ModelPrice | None,
    *,
    input_tokens: int | None,
    cached_input_tokens: int | None,
    output_tokens: int | None,
    reasoning_tokens: int | None,
) -> int | None:
    """Cost in integer micros, or ``None`` when it cannot be known.

    ``input_tokens`` is the provider's total prompt count; the cached part is
    already inside it, so it is priced at the cached rate and the remainder at the
    input rate. Anything the provider did not report contributes nothing *and*
    still yields a number only when at least one billable count is known — a
    completely unknown attempt returns ``None`` rather than a confident zero.
    """
    if price is None:
        return None
    counts = (input_tokens, cached_input_tokens, output_tokens, reasoning_tokens)
    if all(count is None for count in counts):
        return None

    unit = max(1, int(price.unit_tokens))
    cached = max(0, int(cached_input_tokens or 0))
    prompt = int(input_tokens or 0)
    # A provider that reports only a cached count still has that many prompt tokens.
    billable_prompt = max(0, prompt - cached) if input_tokens is not None else 0
    output = max(0, int(output_tokens or 0))
    reasoning = max(0, int(reasoning_tokens or 0))

    cached_rate = (
        price.cached_input_micros_per_unit
        if price.cached_input_micros_per_unit is not None
        else price.input_micros_per_unit
    )
    reasoning_rate = (
        price.reasoning_micros_per_unit
        if price.reasoning_micros_per_unit is not None
        else price.output_micros_per_unit
    )

    micros = (
        billable_prompt * price.input_micros_per_unit
        + cached * cached_rate
        + output * price.output_micros_per_unit
        + reasoning * reasoning_rate
    )
    # Half-up rounding on integer arithmetic: no float ever touches money.
    return (micros + unit // 2) // unit


def recorder(
    settings: Settings,
    context: Context,
) -> Callable[[Any], None]:
    """A callback that records every attempt the router reports.

    The router calls it once per attempt with its summary. A failure here is
    logged and dropped: the user's answer must not depend on the ledger.
    """
    settled = context.normalized()

    def on_attempt(summary: Any) -> None:
        try:
            record(settings, settled, summary)
        except Exception:  # pragma: no cover - ledger is best effort
            logger.warning("provider_usage_record_failed", exc_info=True)

    return on_attempt


def record(settings: Settings, context: Context, summary: Any) -> bool:
    """Durably queue one attempt, then try to apply it immediately.

    The event is committed before anything is applied, so a write that fails is
    retried by the outbox instead of disappearing (P9-F06). The inline delivery
    afterwards is what keeps the admin numbers current without waiting for a
    worker; if it fails, the event stays pending and the drain finishes the job.

    Returns ``True`` when a ledger row was written by this call.
    """
    attempt = _attempt_from_summary(summary, context)
    with connect(settings) as connection, connection.cursor() as cursor:
        tenant_id = _tenant_id(cursor, context.tenant_key)
    price = resolve_price(
        settings, attempt.provider, attempt.model, tenant_key=context.tenant_key
    )
    cost = estimate_cost_micros(
        price,
        input_tokens=attempt.input_tokens,
        cached_input_tokens=attempt.cached_input_tokens,
        output_tokens=attempt.output_tokens,
        reasoning_tokens=attempt.reasoning_tokens,
    )
    payload = _event_payload(
        tenant_id=tenant_id, context=context, attempt=attempt, price=price, cost=cost
    )
    try:
        event_id = outbox.emit(
            settings,
            tenant_id=tenant_id,
            aggregate_type=USAGE_AGGREGATE,
            event_type=USAGE_EVENT,
            payload=payload,
        )
    except Exception:  # pragma: no cover - outbox unavailable
        logger.warning("provider_usage_emit_failed", exc_info=True)
        return False

    outcome = outbox.deliver(
        settings,
        handlers={USAGE_EVENT: apply_event},
        event_id=event_id,
        limit=1,
    )
    written = outcome["delivered"] > 0
    logger.info(
        "provider_usage module=%s provider=%s model=%s status=%s tokens=%s "
        "cost_micros=%s recorded=%s",
        context.module,
        attempt.provider,
        attempt.model,
        attempt.status,
        attempt.total_tokens,
        cost,
        written,
    )
    return written


def apply_event(cursor: Any, payload: Any) -> bool:
    """The idempotent consumer: turn one queued attempt into one ledger row.

    ``ON CONFLICT DO NOTHING`` makes a redelivered event a no-op, which is the
    contract the outbox promises. Returns ``False`` when the row already existed.
    """
    if not isinstance(payload, Mapping):
        raise usage_error("invalid_event", "usage event payload must be an object")
    try:
        values = (
            payload["tenant_id"],
            payload.get("job_id"),
            payload.get("step_id"),
            payload.get("conversation_id"),
            payload.get("superset_user_id"),
            payload.get("correlation_id") or "",
            payload.get("module") or "other",
            payload.get("feature") or "",
            payload["provider"],
            payload["model"],
            payload.get("operation") or "unknown",
            int(payload.get("attempt_ordinal") or 1),
            bool(payload.get("fallback")),
            payload.get("provider_request_id"),
            payload.get("status") or "succeeded",
            payload.get("error_class"),
            payload.get("error_detail") or "",
            payload.get("input_tokens"),
            payload.get("cached_input_tokens"),
            payload.get("output_tokens"),
            payload.get("reasoning_tokens"),
            payload.get("total_tokens"),
            payload.get("usage_source") or MISSING,
            payload.get("duration_ms"),
            payload.get("price_version"),
            payload.get("currency") or "",
            payload.get("estimated_cost_micros"),
            payload.get("price_source") or "",
        )
    except KeyError as exc:
        raise usage_error("invalid_event", f"usage event is missing {exc}") from exc
    cursor.execute(
        """
        INSERT INTO ai_provider_attempt (
            tenant_id, job_id, step_id, conversation_id, superset_user_id,
            correlation_id, module, feature, provider, model, operation,
            attempt_ordinal, fallback, provider_request_id, status,
            error_class, error_detail, input_tokens, cached_input_tokens,
            output_tokens, reasoning_tokens, total_tokens, usage_source,
            duration_ms, price_version, currency, estimated_cost_micros,
            price_source, started_at, completed_at
        )
        VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        ON CONFLICT (tenant_id, correlation_id, provider, attempt_ordinal)
            WHERE correlation_id <> ''
        DO NOTHING
        RETURNING id
        """,
        values,
    )
    return cursor.fetchone() is not None


def drain(
    settings: Settings,
    *,
    limit: int = 25,
) -> dict[str, Any]:
    """Apply whatever the inline path could not, e.g. after an outage.

    Bounded on purpose: this is called from a periodic task and from an operator
    action, and neither should be able to turn into an unbounded backfill.
    """
    outcome = outbox.deliver(
        settings, handlers={USAGE_EVENT: apply_event}, limit=limit
    )
    logger.info(
        "provider_usage_drain claimed=%d delivered=%d duplicates=%d failed=%d",
        outcome["claimed"],
        outcome["delivered"],
        outcome["duplicates"],
        outcome["failed"],
    )
    return outcome


def tenant_identifier(settings: Settings, tenant_key: str) -> str:
    """The tenant row id behind a tenant key, for callers that need to scope a read."""
    with connect(settings) as connection, connection.cursor() as cursor:
        return _tenant_id(cursor, tenant_key)


def tenant_keys(settings: Settings) -> list[str]:
    """Every provisioned tenant key, so a sweep can cover all of them."""
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT key FROM ai_tenant ORDER BY key")
        rows = cursor.fetchall()
    return [str(row["key"]) for row in rows]


def cost_of(settings: Settings, attempts: Iterable[Any]) -> tuple[int, int]:
    """Total estimated cost in micros and total tokens for a set of attempts.

    Used by the budget guard, which must charge what was actually spent rather
    than the nominal claim. An attempt whose model has no published price charges
    nothing here and is reported by reconciliation instead; inventing a number
    would make the budget lie in the other direction.
    """
    micros = 0
    tokens = 0
    prices: dict[tuple[str, str], ModelPrice | None] = {}
    for attempt in attempts or ():
        usage = getattr(attempt, "usage", None)
        tokens += int(getattr(usage, "total_tokens", 0) or 0)
        key = (getattr(attempt, "provider", ""), getattr(attempt, "model", ""))
        if key not in prices:
            prices[key] = resolve_price(settings, key[0], key[1])
        cost = estimate_cost_micros(
            prices[key],
            input_tokens=getattr(usage, "input_tokens", None),
            cached_input_tokens=getattr(usage, "cached_input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            reasoning_tokens=getattr(usage, "reasoning_tokens", None),
        )
        micros += int(cost or 0)
    return micros, tokens


def resolve_price(
    settings: Settings,
    provider: str,
    model: str,
    *,
    tenant_key: str = "default",
    at: datetime | None = None,
) -> ModelPrice | None:
    """The price version in force at ``at``, or ``None`` when none is published."""
    moment = at or datetime.now(UTC)
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT provider, model, version, currency, unit_tokens,
                   input_micros_per_unit, cached_input_micros_per_unit,
                   output_micros_per_unit, reasoning_micros_per_unit,
                   effective_from, effective_to, source, source_url
            FROM ai_model_price
            WHERE provider = %s
              AND model = %s
              AND effective_from <= %s
              AND (effective_to IS NULL OR effective_to > %s)
            ORDER BY version DESC
            LIMIT 1
            """,
            (provider, model, moment, moment),
        )
        row = cursor.fetchone()
    return _price_from_row(row) if row else None


def publish_price(
    settings: Settings,
    price: ModelPrice,
    *,
    tenant_key: str = "default",
) -> ModelPrice:
    """Publish a new price version and close the previous interval.

    Closing the previous version rather than editing it is what keeps a past
    attempt's cost reproducible (P9-F05).
    """
    _validate_price(price)
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT version FROM ai_model_price
            WHERE provider = %s AND model = %s
            ORDER BY version DESC
            LIMIT 1
            """,
            (price.provider, price.model),
        )
        latest = cursor.fetchone()
        version = int(latest["version"]) + 1 if latest else max(1, price.version)
        effective_from = price.effective_from or datetime.now(UTC)
        cursor.execute(
            """
            UPDATE ai_model_price
            SET effective_to = %s
            WHERE provider = %s AND model = %s AND effective_to IS NULL
            """,
            (effective_from, price.provider, price.model),
        )
        cursor.execute(
            """
            INSERT INTO ai_model_price (
                provider, model, version, currency, unit_tokens,
                input_micros_per_unit, cached_input_micros_per_unit,
                output_micros_per_unit, reasoning_micros_per_unit,
                effective_from, effective_to, source, source_url
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                price.provider,
                price.model,
                version,
                price.currency.upper(),
                price.unit_tokens,
                price.input_micros_per_unit,
                price.cached_input_micros_per_unit,
                price.output_micros_per_unit,
                price.reasoning_micros_per_unit,
                effective_from,
                price.effective_to,
                _text(price.source, MAX_SOURCE_CHARS),
                _text(price.source_url, MAX_SOURCE_URL_CHARS),
            ),
        )
    logger.info(
        "model_price_published provider=%s model=%s version=%d currency=%s",
        price.provider,
        price.model,
        version,
        price.currency.upper(),
    )
    return ModelPrice(
        provider=price.provider,
        model=price.model,
        version=version,
        currency=price.currency.upper(),
        unit_tokens=price.unit_tokens,
        input_micros_per_unit=price.input_micros_per_unit,
        cached_input_micros_per_unit=price.cached_input_micros_per_unit,
        output_micros_per_unit=price.output_micros_per_unit,
        reasoning_micros_per_unit=price.reasoning_micros_per_unit,
        effective_from=effective_from,
        effective_to=price.effective_to,
        source=price.source,
        source_url=price.source_url,
    )


def list_prices(
    settings: Settings,
    *,
    tenant_key: str = "default",
    provider: str | None = None,
    model: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[ModelPrice]:
    bounded = max(1, min(int(limit), MAX_LIST_LIMIT))
    clauses = []
    params: list[Any] = []
    if provider:
        clauses.append("provider = %s")
        params.append(provider)
    if model:
        clauses.append("model = %s")
        params.append(model)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT provider, model, version, currency, unit_tokens,
                   input_micros_per_unit, cached_input_micros_per_unit,
                   output_micros_per_unit, reasoning_micros_per_unit,
                   effective_from, effective_to, source, source_url
            FROM ai_model_price
            {where}
            ORDER BY provider, model, version DESC
            LIMIT %s
            """,
            (*params, bounded),
        )
        rows = cursor.fetchall()
    return [_price_from_row(row) for row in rows]


def summary(
    settings: Settings,
    *,
    tenant_key: str = "default",
    group_by: Sequence[str] = (),
    since: datetime | None = None,
    module: str | None = None,
    superset_user_id: int | None = None,
) -> dict[str, Any]:
    """Totals for the admin cards, grouped by the dimensions that are asked for.

    Costs are returned in micros with an explicit ``estimated`` label: this is a
    computation from a published price list, not a provider invoice (P9-F08).
    """
    keys = [str(key).lower() for key in group_by if str(key).lower() in GROUP_BY]
    if len(keys) > MAX_GROUP_VALUES:
        raise UsageError("group_by_budget", "too many grouping dimensions")

    filters = ["t.key = %s"]
    params: list[Any] = [tenant_key]
    if since is not None:
        filters.append("a.started_at >= %s")
        params.append(since)
    if module:
        filters.append("a.module = %s")
        params.append(module)
    if superset_user_id is not None:
        filters.append("a.superset_user_id = %s")
        params.append(int(superset_user_id))
    where = " AND ".join(filters)

    selected = ", ".join(f"a.{key}" for key in keys) if keys else ""
    prefix = f"{selected}, " if selected else ""
    grouping = f"GROUP BY {selected} ORDER BY requests DESC" if keys else ""

    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT {prefix}
                   COUNT(*) AS requests,
                   SUM(CASE WHEN a.status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
                   SUM(CASE WHEN a.status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN a.usage_source = 'missing' THEN 1 ELSE 0 END) AS missing_usage,
                   SUM(CASE WHEN a.fallback THEN 1 ELSE 0 END) AS fallbacks,
                   COALESCE(SUM(a.input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(a.cached_input_tokens), 0) AS cached_input_tokens,
                   COALESCE(SUM(a.output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(a.reasoning_tokens), 0) AS reasoning_tokens,
                   COALESCE(SUM(a.total_tokens), 0) AS total_tokens,
                   SUM(a.estimated_cost_micros) AS cost_micros,
                   COUNT(a.estimated_cost_micros) AS priced_requests,
                   AVG(a.duration_ms)::numeric(12, 1) AS avg_duration_ms,
                   MAX(a.started_at) AS last_attempt
            FROM ai_provider_attempt a
            JOIN ai_tenant t ON t.id = a.tenant_id
            WHERE {where}
            {grouping}
            """,
            tuple(params),
        )
        rows = cursor.fetchall()

    groups = [
        {**{key: row.get(key) for key in keys}, **_totals(row)}
        for row in rows
    ]
    overall = (
        groups[0]
        if not keys and groups
        else _combine(groups, keys)
    )
    return {
        "tenant": tenant_key,
        "group_by": keys,
        "groups": groups,
        "totals": overall,
        "cost_is_estimated": True,
        "since": _isoformat(since),
    }


def attempts(
    settings: Settings,
    *,
    tenant_key: str = "default",
    correlation_id: str | None = None,
    module: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[dict[str, Any]]:
    """The newest attempts, for the ledger table in the admin view."""
    bounded = max(1, min(int(limit), MAX_LIST_LIMIT))
    filters = ["t.key = %s"]
    params: list[Any] = [tenant_key]
    if correlation_id:
        filters.append("a.correlation_id = %s")
        params.append(correlation_id)
    if module:
        filters.append("a.module = %s")
        params.append(module)
    where = " AND ".join(filters)
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT a.id, a.correlation_id, a.module, a.feature, a.provider,
                   a.model, a.operation, a.attempt_ordinal, a.fallback,
                   a.status, a.error_class, a.error_detail,
                   a.provider_request_id, a.superset_user_id,
                   a.input_tokens, a.cached_input_tokens, a.output_tokens,
                   a.reasoning_tokens, a.total_tokens, a.usage_source,
                   a.duration_ms, a.price_version, a.currency,
                   a.estimated_cost_micros, a.price_source, a.started_at
            FROM ai_provider_attempt a
            JOIN ai_tenant t ON t.id = a.tenant_id
            WHERE {where}
            ORDER BY a.started_at DESC
            LIMIT %s
            """,
            (*params, bounded),
        )
        rows = cursor.fetchall()
    return [
        {
            **{key: value for key, value in row.items() if key != "started_at"},
            "id": str(row["id"]),
            "started_at": _isoformat(row.get("started_at")),
        }
        for row in rows
    ]


def reconcile(
    settings: Settings,
    *,
    tenant_key: str = "default",
) -> dict[str, Any]:
    """Ledger quality checks, so "is this total trustworthy" has an answer.

    Each finding is a count, not a verdict: a missing price is expected on a fresh
    deployment, while negative tokens never are.
    """
    with connect(settings) as connection, connection.cursor() as cursor:
        tenant_id = _tenant_id(cursor, tenant_key)
        cursor.execute(
            """
            SELECT
                COUNT(*) AS attempts,
                SUM(CASE WHEN usage_source = 'missing' THEN 1 ELSE 0 END) AS missing_usage,
                SUM(CASE WHEN usage_source <> 'missing'
                          AND estimated_cost_micros IS NULL THEN 1 ELSE 0 END) AS unpriced,
                SUM(CASE WHEN COALESCE(input_tokens, 0) < 0
                           OR COALESCE(output_tokens, 0) < 0
                           OR COALESCE(cached_input_tokens, 0) < 0
                           OR COALESCE(reasoning_tokens, 0) < 0
                         THEN 1 ELSE 0 END) AS negative_tokens,
                SUM(CASE WHEN status = 'started' THEN 1 ELSE 0 END) AS unfinished,
                SUM(CASE WHEN total_tokens IS NOT NULL
                          AND input_tokens IS NOT NULL
                          AND output_tokens IS NOT NULL
                          AND total_tokens <> input_tokens + output_tokens
                         THEN 1 ELSE 0 END) AS inconsistent_totals
            FROM ai_provider_attempt
            WHERE tenant_id = %s
            """,
            (tenant_id,),
        )
        ledger = cursor.fetchone() or {}
        cursor.execute(
            """
            SELECT COUNT(*) AS providers
            FROM ai_provider
            WHERE tenant_id = %s AND enabled = true
            """,
            (tenant_id,),
        )
        enabled = cursor.fetchone() or {}
        cursor.execute(
            """
            SELECT DISTINCT a.provider, a.model
            FROM ai_provider_attempt a
            WHERE a.tenant_id = %s
              AND a.usage_source <> 'missing'
              AND NOT EXISTS (
                  SELECT 1 FROM ai_model_price p
                  WHERE p.provider = a.provider AND p.model = a.model
              )
            ORDER BY a.provider, a.model
            LIMIT 20
            """,
            (tenant_id,),
        )
        gaps = cursor.fetchall()

    findings = {
        "attempts": int(ledger.get("attempts") or 0),
        "missing_usage": int(ledger.get("missing_usage") or 0),
        "unpriced": int(ledger.get("unpriced") or 0),
        "negative_tokens": int(ledger.get("negative_tokens") or 0),
        "unfinished": int(ledger.get("unfinished") or 0),
        "inconsistent_totals": int(ledger.get("inconsistent_totals") or 0),
        "enabled_providers": int(enabled.get("providers") or 0),
        "price_gaps": [f"{row['provider']}/{row['model']}" for row in gaps],
        "tenant_key": tenant_key,
    }
    # Undelivered events are the difference between "the ledger is empty" and
    # "the ledger has not caught up", so they belong in the same report (F06).
    pending_events = outbox.pending(settings, tenant_id=tenant_id)
    findings["outbox_pending"] = pending_events.get(outbox.PENDING, 0)
    findings["outbox_dead_letter"] = pending_events.get(outbox.DEAD_LETTER, 0)
    findings["healthy"] = not any(
        findings[key]
        for key in (
            "negative_tokens",
            "unfinished",
            "inconsistent_totals",
            "outbox_dead_letter",
        )
    )
    return findings


def _event_payload(
    *,
    tenant_id: str,
    context: Context,
    attempt: Attempt,
    price: ModelPrice | None,
    cost: int | None,
) -> dict[str, Any]:
    """The whole ledger row, resolved now, so the consumer had no decisions left.

    Pricing happens before the event is queued: a retried delivery must cost what
    the attempt cost, not what the price list says by the time it is delivered.
    """
    return {
        "tenant_id": tenant_id,
        "job_id": _uuid_or_none(context.job_id),
        "step_id": _uuid_or_none(context.step_id),
        "conversation_id": _uuid_or_none(context.conversation_id),
        "superset_user_id": context.superset_user_id,
        "correlation_id": attempt.correlation_id,
        "module": context.module,
        "feature": context.feature,
        "provider": attempt.provider,
        "model": attempt.model,
        "operation": attempt.operation,
        "attempt_ordinal": attempt.attempt_ordinal,
        "fallback": bool(attempt.fallback),
        "provider_request_id": attempt.provider_request_id,
        "status": attempt.status,
        "error_class": attempt.error_class,
        "error_detail": attempt.error_detail,
        "input_tokens": attempt.input_tokens,
        "cached_input_tokens": attempt.cached_input_tokens,
        "output_tokens": attempt.output_tokens,
        "reasoning_tokens": attempt.reasoning_tokens,
        "total_tokens": attempt.total_tokens,
        "usage_source": attempt.usage_source,
        "duration_ms": attempt.duration_ms,
        "price_version": price.version if price else None,
        "currency": price.currency if price else "",
        "estimated_cost_micros": cost,
        "price_source": price.source if price else "",
    }


def _attempt_from_summary(summary: Any, context: Context) -> Attempt:
    """Map the router's summary onto a ledger row without guessing anything."""
    usage = getattr(summary, "usage", None)
    outcome = str(getattr(summary, "outcome", "") or "").lower()
    status = {
        "succeeded": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(outcome, "failed")
    source = getattr(usage, "source", MISSING) if usage is not None else MISSING
    return Attempt(
        provider=_text(getattr(summary, "provider", ""), 32) or "unknown",
        model=_text(getattr(summary, "model", ""), 128) or "unknown",
        operation=_text(getattr(summary, "operation", ""), 64) or "unknown",
        attempt_ordinal=max(1, int(getattr(summary, "attempt", 1) or 1)),
        status=status,
        correlation_id=_text(
            getattr(summary, "correlation_id", ""), MAX_CORRELATION_CHARS
        ),
        module=context.module,
        feature=context.feature,
        error_class=(
            _text(getattr(summary, "error_code", None), 64) or None
        ),
        error_detail=_text(getattr(summary, "error_detail", ""), MAX_ERROR_DETAIL_CHARS),
        fallback=bool(getattr(summary, "fallback", False)),
        provider_request_id=(
            _text(getattr(summary, "request_id", None), 255) or None
        ),
        input_tokens=_count(getattr(usage, "input_tokens", None)),
        cached_input_tokens=_count(getattr(usage, "cached_input_tokens", None)),
        output_tokens=_count(getattr(usage, "output_tokens", None)),
        reasoning_tokens=_count(getattr(usage, "reasoning_tokens", None)),
        total_tokens=_count(getattr(usage, "total_tokens", None)),
        usage_source=REPORTED if source == REPORTED else MISSING,
        duration_ms=_count(getattr(summary, "duration_ms", None)),
    )


def _totals(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "requests": int(row.get("requests") or 0),
        "succeeded": int(row.get("succeeded") or 0),
        "failed": int(row.get("failed") or 0),
        "missing_usage": int(row.get("missing_usage") or 0),
        "fallbacks": int(row.get("fallbacks") or 0),
        "input_tokens": int(row.get("input_tokens") or 0),
        "cached_input_tokens": int(row.get("cached_input_tokens") or 0),
        "output_tokens": int(row.get("output_tokens") or 0),
        "reasoning_tokens": int(row.get("reasoning_tokens") or 0),
        "total_tokens": int(row.get("total_tokens") or 0),
        # NULL, not 0: a group with no price known has no cost, not a free one.
        "estimated_cost_micros": (
            int(row["cost_micros"]) if row.get("cost_micros") is not None else None
        ),
        "priced_requests": int(row.get("priced_requests") or 0),
        "avg_duration_ms": (
            float(row["avg_duration_ms"])
            if row.get("avg_duration_ms") is not None
            else None
        ),
        "last_attempt": _isoformat(row.get("last_attempt")),
    }


def _combine(groups: Iterable[Mapping[str, Any]], keys: Sequence[str]) -> dict[str, Any]:
    """Sum the groups into one total without inventing a cost that was unknown."""
    items = list(groups)
    if not items:
        return _totals({})
    counted = {
        key: sum(int(item.get(key) or 0) for item in items)
        for key in (
            "requests",
            "succeeded",
            "failed",
            "missing_usage",
            "fallbacks",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "priced_requests",
        )
    }
    prices = [
        int(item["estimated_cost_micros"])
        for item in items
        if item.get("estimated_cost_micros") is not None
    ]
    durations = [
        float(item["avg_duration_ms"])
        for item in items
        if item.get("avg_duration_ms") is not None
    ]
    counted["cost_micros"] = sum(prices) if prices else None
    counted["avg_duration_ms"] = (
        sum(durations) / len(durations) if durations else None
    )
    counted["last_attempt"] = max(
        (item["last_attempt"] for item in items if item.get("last_attempt")),
        default=None,
    )
    return _totals(counted)


def _validate_price(price: ModelPrice) -> None:
    if not price.provider.strip() or not price.model.strip():
        raise UsageError("invalid_price", "a price needs a provider and a model")
    if len(price.currency.strip()) != 3:
        raise UsageError("invalid_price", "currency must be a three-letter code")
    if price.unit_tokens <= 0:
        raise UsageError("invalid_price", "unit_tokens must be positive")
    for label, rate in (
        ("input", price.input_micros_per_unit),
        ("output", price.output_micros_per_unit),
    ):
        if rate < 0:
            raise UsageError("invalid_price", f"{label} rate must not be negative")
    for label, rate in (
        ("cached_input", price.cached_input_micros_per_unit),
        ("reasoning", price.reasoning_micros_per_unit),
    ):
        if rate is not None and rate < 0:
            raise UsageError("invalid_price", f"{label} rate must not be negative")


def _price_from_row(row: Mapping[str, Any]) -> ModelPrice:
    return ModelPrice(
        provider=row["provider"],
        model=row["model"],
        version=int(row["version"]),
        currency=row["currency"],
        unit_tokens=int(row["unit_tokens"]),
        input_micros_per_unit=int(row["input_micros_per_unit"]),
        cached_input_micros_per_unit=(
            int(row["cached_input_micros_per_unit"])
            if row.get("cached_input_micros_per_unit") is not None
            else None
        ),
        output_micros_per_unit=int(row["output_micros_per_unit"]),
        reasoning_micros_per_unit=(
            int(row["reasoning_micros_per_unit"])
            if row.get("reasoning_micros_per_unit") is not None
            else None
        ),
        effective_from=row.get("effective_from"),
        effective_to=row.get("effective_to"),
        source=row.get("source") or "",
        source_url=row.get("source_url") or "",
    )


def _tenant_id(cursor: Any, tenant_key: str) -> str:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    if row is None:
        raise UsageError("unknown_tenant", "tenant is not provisioned")
    return str(row["id"])


def _count(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _uuid_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    import uuid

    try:
        return str(uuid.UUID(text))
    except (TypeError, ValueError):
        return None


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
