"""Candidate SQL generation (P4-F04).

One turn of the SQL pipeline, in order and with no shortcuts:

1. **prompt from the authorized slice** — the only table and column names the
   model ever sees are the ones P4-F02 validated;
2. **structured output** — the provider is asked for a JSON object with
   ``sql``/``rationale``/``assumptions``; the router already refuses prose, arrays,
   truncated JSON, and missing keys (P3-F10), and this module validates the *types*;
3. **guard** — the candidate goes through :func:`xp_prompt.sql_guard.check` with the
   slice's allowlist before anything else happens to it (P4-F05);
4. **blocks** — a ``sql_preview`` plus the assumptions become response blocks
   (P2-F06), always with the warning that nothing has been executed.

There is no execution here: P4-F09 owns running the statement through Superset's
query layer, and it must re-run the guard rather than trusting this result.

Failures carry a stable code (``no_schema``, ``dialect_ambiguous``,
``provider_error``, ``invalid_response``, ``sql_blocked``) and never include the
model's SQL text, so a refusal can be logged and stored safely.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from xp_prompt import (
    cache,
    examples,
    question_plan,
    responses,
    settings_registry,
    sql_execution,
    sql_guard,
    sql_semantics,
    usage,
)
from xp_prompt.config import Settings
from xp_prompt.providers import registry
from xp_prompt.providers.structured import StructuredOutputError, parse_json_object
from xp_prompt.providers.types import Message, ProviderError, ProviderRequest
from xp_prompt.schema_slice import SchemaSlice

logger = logging.getLogger(__name__)

#: A JSON answer with a query, a rationale, and a few assumptions needs far less,
#: but Gemini's thinking budget is charged against this limit: 1200 was observed to
#: truncate mid-string on a two-table question, which the router (correctly) reports
#: as ``structured_output_invalid`` because a partial document is unparseable.
#: The candidate asks for the whole generation ceiling by default; the ceiling
#: itself is ``generation.max_output_tokens`` and it can also lower this.
CANDIDATE_MAX_TOKENS = 2048
#: SQL generation stays deterministic whatever the chat temperature is: a repaired
#: statement has to be reproducible before it can be validated against the data.
CANDIDATE_TEMPERATURE = 0.0
MAX_QUESTION_CHARS = 2_000
MAX_RATIONALE_CHARS = 600
MAX_ASSUMPTIONS = 8
MAX_ASSUMPTION_CHARS = 300
REQUIRED_KEYS = ("sql", "rationale", "assumptions")
#: A repair note is bounded like every other free-form prompt section.
MAX_REPAIR_NOTE_CHARS = 1_600

SYSTEM_PROMPT = (
    "You write read-only analytical SQL for a Superset deployment.\n"
    "Rules:\n"
    "- Use only the tables and columns listed by the user. Never invent a name.\n"
    "- Reference a table exactly as written before the bracket: table or\n"
    "  schema.table. A 'connection' in the bracket is a Superset label, not a SQL\n"
    "  catalog — never prefix a table with it.\n"
    "- Emit exactly one SELECT statement (CTEs are allowed). No DDL, no DML, no\n"
    "  comments, no multiple statements.\n"
    "- Always include a LIMIT of {max_rows} rows or fewer.\n"
    "- Use {dialect} syntax.\n"
    'Respond with one JSON object and nothing else:\n'
    '{{"sql": "<statement>", "rationale": "<one or two sentences>", '
    '"assumptions": ["<assumption>", ...]}}'
)


class SqlCandidateError(RuntimeError):
    """A refused or failed candidate; ``code`` is stable and content-free."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        guard_code: str | None = None,
        provider_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.guard_code = guard_code
        #: The provider's own normalized code (e.g. ``structured_output_invalid``).
        self.provider_code = provider_code


@dataclass(frozen=True, slots=True)
class SqlCandidate:
    """A validated statement, ready for a caller that will re-check and execute it."""

    sql: str
    dialect: str
    rationale: str
    assumptions: tuple[str, ...]
    tables: tuple[str, ...]
    limit: int
    fingerprint: str
    provider: str
    model: str
    correlation_id: str
    blocks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    #: ``hit``, ``miss``, ``bypass`` or ``disabled`` — carried into the audit so a
    #: replayed statement is never mistaken for a fresh one.
    cache_status: str = cache.MISS

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "dialect": self.dialect,
            "rationale": self.rationale,
            "assumptions": list(self.assumptions),
            "tables": list(self.tables),
            "limit": self.limit,
            "schema_fingerprint": self.fingerprint,
            "provider": self.provider,
            "model": self.model,
            "correlation_id": self.correlation_id,
            "cache": self.cache_status,
            "blocks": list(self.blocks),
        }


def build_request(
    question: str,
    schema: SchemaSlice,
    *,
    dialect: str,
    max_rows: int,
    correlation_id: str | None = None,
    plan: question_plan.QuestionPlan | None = None,
    examples: Any = None,
    repair_note: str = "",
    live: dict[str, settings_registry.Resolution] | None = None,
) -> ProviderRequest:
    """The one provider request this pipeline makes, built only from the slice.

    ``live`` carries the resolved settings; without it the module constants apply.
    """
    text = (question or "").strip()
    if not text:
        raise SqlCandidateError("empty_question", "a question is required")
    if len(text) > MAX_QUESTION_CHARS:
        raise SqlCandidateError("question_too_long", "the question is too long")
    schema_text = schema.prompt_text()
    if not schema_text:
        raise SqlCandidateError("no_schema", "no authorized tables are available")
    normal_dialect = (dialect or "").strip().lower()
    if normal_dialect not in sql_guard.SUPPORTED_DIALECTS:
        raise SqlCandidateError(
            "unsupported_dialect", f"unsupported SQL dialect: {dialect}"
        )

    outline = plan.prompt_block() if plan is not None else ""
    example_block = examples or ""
    sections = [f"Question: {text}"]
    if outline:
        sections.append(f"What the question asks for:\n{outline}")
    if example_block:
        sections.append(example_block)
    # A repair is a second attempt at the *same* question, told what went wrong. The
    # note never contains rows: it carries the failure kind and the statement shape
    # (P5-F04).
    note = (repair_note or "").strip()[:MAX_REPAIR_NOTE_CHARS]
    if note:
        sections.append(f"Repair this attempt:\n{note}")
    sections.append(f"Available data:\n{schema_text}")
    return ProviderRequest(
        operation="sql.candidate",
        messages=(
            Message(
                "system",
                SYSTEM_PROMPT.format(
                    max_rows=max(1, int(max_rows)), dialect=normal_dialect
                ),
            ),
            Message("user", "\n\n".join(sections)),
        ),
        max_output_tokens=_max_output_tokens(live),
        temperature=CANDIDATE_TEMPERATURE,
        json_output=True,
        json_required_keys=REQUIRED_KEYS,
        # SQL generation is a transcription task: thinking only competes with the
        # JSON budget, and a budget-starved answer is truncated mid-string.
        reasoning_budget_tokens=0,
        correlation_id=correlation_id or uuid.uuid4().hex,
    )


def _max_output_tokens(live: dict[str, settings_registry.Resolution] | None) -> int:
    """The candidate budget, held under the platform's generation ceiling."""
    if live is None:
        return CANDIDATE_MAX_TOKENS
    ceiling = int(live["generation.max_output_tokens"].value)
    return max(1, min(CANDIDATE_MAX_TOKENS, ceiling))


def generate(
    settings: Settings,
    *,
    question: str,
    schema: SchemaSlice,
    tenant_key: str = registry.DEFAULT_TENANT_KEY,
    database: str | None = None,
    dialect: str | None = None,
    max_rows: int = sql_guard.MAX_ROWS,
    bypass_cache: bool = False,
    repair_note: str = "",
    superset_user_id: int | None = None,
    conversation_id: str | None = None,
) -> SqlCandidate:
    """Ask for one candidate, validate it, and return it with its blocks.

    A candidate may come from the cache (P4-F11): same question, same slice
    fingerprint (which covers the datasets, their definitions and their row-level
    policies), same dialect, same row cap. Only the statement is cached, never a
    result, and a refusal is never stored — an explicit ``bypass_cache`` is honoured
    and reported.

    A ``repair_note`` means this is a second attempt at a question whose previous
    statement failed (P5-F04). The cached entry *is* that statement, so it is never
    loaded; a successful correction replaces it instead, because the cached answer
    was wrong.
    """
    resolved_dialect = _resolve_dialect(schema, database=database, dialect=dialect)
    live = settings_registry.live(settings, tenant_key=tenant_key)
    if not live["feature.sql"].value:
        raise SqlCandidateError(
            "feature_disabled", "SQL generation is switched off for this tenant"
        )
    # The row cap is a platform safeguard, so the caller's value can only narrow
    # it. It is clamped before the prompt is built, so the statement is asked for
    # under the same cap that will be enforced on it (P10-F06).
    limits = settings_registry.sql_limits(settings, tenant_key=tenant_key)
    max_rows = min(int(max_rows), limits.max_rows)
    plan = question_plan.analyse(question, schema)
    if plan.blocking:
        # Ask instead of guessing: an invented column or an arbitrary pick between
        # measures produces SQL that is plausible and wrong (P4-F01).
        logger.info("sql_candidate outcome=clarification codes=%s", ",".join(plan.blocking))
        raise SqlCandidateError("ambiguous_question", plan.clarification())
    key = cache.cache_key(
        tenant_key=tenant_key,
        question=question,
        schema=schema,
        dialect=resolved_dialect,
        max_rows=max_rows,
    )
    repairing = bool((repair_note or "").strip())
    if not cache.enabled(settings):
        cache_status = cache.DISABLED
    elif bypass_cache or repairing:
        cache_status = cache.BYPASS
    else:
        cached = cache.load(settings, key)
        if cached is not None:
            logger.info(
                "sql_candidate outcome=succeeded cache=hit provider=%s model=%s",
                cached.provider,
                cached.model,
            )
            return _from_cache(cached, schema, cache_status=cache.HIT)
        cache_status = cache.MISS

    request = build_request(
        question,
        schema,
        dialect=resolved_dialect,
        max_rows=max_rows,
        plan=plan,
        examples=_example_block(settings, question, tenant_key, resolved_dialect),
        repair_note=repair_note,
        live=live,
    )

    try:
        router = registry.resolve_router(
            settings,
            tenant_key=tenant_key,
            context=usage.Context(
                tenant_key=tenant_key,
                module="sql",
                feature="candidate_generation",
                superset_user_id=superset_user_id,
                conversation_id=conversation_id,
            ),
        )
    except registry.ProviderConfigError as exc:
        raise SqlCandidateError("no_provider", str(exc)) from exc

    try:
        routed = router.generate(request)
    except ProviderError as exc:
        logger.info(
            "sql_candidate correlation_id=%s outcome=failed error_code=%s",
            request.correlation_id,
            exc.code,
        )
        if exc.code == "structured_output_invalid":
            # The provider answered, but not with the required JSON object. That is
            # an upstream shape problem (502), not an availability problem (503).
            raise SqlCandidateError(
                "invalid_response",
                "the provider did not return the required JSON object",
                provider_code=exc.code,
            ) from exc
        raise SqlCandidateError(
            "provider_error",
            "the provider could not answer",
            provider_code=exc.code,
        ) from exc

    sql_text, rationale, assumptions = _parse_candidate(routed.response.content)
    guarded = _guard(
        sql_text,
        resolved_dialect,
        schema,
        database,
        max_rows,
        allow_metadata_schemas=limits.allow_metadata_schemas,
    )
    _check_semantics(guarded.model_sql or guarded.sql, resolved_dialect, schema)

    blocks = responses.normalize_blocks(
        [
            {
                "type": responses.SQL_PREVIEW,
                "sql": guarded.sql,
                "dialect": guarded.dialect,
            },
            {"type": responses.ASSUMPTIONS, "items": list(assumptions)},
            {
                "type": responses.WARNINGS,
                "items": ["Nothing has been executed yet."],
            },
        ]
    )

    correlation_id = (
        routed.attempts[0].correlation_id if routed.attempts else request.correlation_id
    ) or ""
    if cache_status == cache.MISS or repairing:
        # Only a statement that survived the guard *and* the semantic check is
        # stored: a cache must not make a refusal stick. A repair is stored even
        # though it bypassed the read, replacing the statement that failed.
        cache.store(
            settings,
            key,
            cache.CachedCandidate(
                sql=guarded.sql,
                dialect=guarded.dialect,
                rationale=rationale,
                assumptions=assumptions,
                tables=guarded.tables,
                limit=guarded.limit,
                fingerprint=schema.fingerprint,
                provider=routed.response.provider,
                model=routed.response.model,
                correlation_id=correlation_id,
                stored_at=datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
    logger.info(
        "sql_candidate correlation_id=%s provider=%s model=%s tables=%d limit=%d "
        "cache=%s outcome=succeeded",
        correlation_id,
        routed.response.provider,
        routed.response.model,
        len(guarded.tables),
        guarded.limit,
        cache_status,
    )
    # Validated (guard + semantics), so it is precedent worth offering next time.
    examples.record_example(
        settings,
        tenant_key=tenant_key,
        question=question,
        sql=guarded.sql,
        dialect=guarded.dialect,
        fingerprint=schema.fingerprint,
        tables=guarded.tables,
    )

    return SqlCandidate(
        sql=guarded.sql,
        dialect=guarded.dialect,
        rationale=rationale,
        assumptions=assumptions,
        tables=guarded.tables,
        limit=guarded.limit,
        fingerprint=schema.fingerprint,
        provider=routed.response.provider,
        model=routed.response.model,
        correlation_id=correlation_id,
        blocks=tuple(blocks),
        cache_status=cache_status,
    )


def _from_cache(
    cached: cache.CachedCandidate, schema: SchemaSlice, *, cache_status: str
) -> SqlCandidate:
    """Rebuild a candidate from a stored entry, with fresh blocks.

    The entry is replayed but not re-validated: it was validated before it was
    stored, and the key pins the slice fingerprint — a changed schema or policy
    produces a different key, so a stale entry cannot be reached. The blocks are
    rebuilt rather than stored so one owner (``responses``) decides their shape.
    """
    blocks = responses.normalize_blocks(
        [
            {
                "type": responses.SQL_PREVIEW,
                "sql": cached.sql,
                "dialect": cached.dialect,
            },
            {"type": responses.ASSUMPTIONS, "items": list(cached.assumptions)},
            {
                "type": responses.WARNINGS,
                "items": [
                    "Nothing has been executed yet.",
                    "This statement came from the candidate cache.",
                ],
            },
        ]
    )
    return SqlCandidate(
        sql=cached.sql,
        dialect=cached.dialect,
        rationale=cached.rationale,
        assumptions=cached.assumptions,
        tables=cached.tables,
        limit=cached.limit,
        fingerprint=schema.fingerprint,
        provider=cached.provider,
        model=cached.model,
        correlation_id=cached.correlation_id,
        blocks=tuple(blocks),
        cache_status=cache_status,
    )


def _example_block(
    settings: Settings, question: str, tenant_key: str, dialect: str
) -> str:
    """Few-shot precedent from this tenant's own validated statements (P4-F03)."""
    try:
        found = examples.find_examples(
            settings,
            tenant_key=tenant_key,
            question=question,
            dialect=dialect,
            limit=examples.DEFAULT_LIMIT,
        )
    except Exception as exc:  # pragma: no cover - best effort
        logger.info("sql_examples outcome=error kind=prompt reason=%s", type(exc).__name__)
        return ""
    return examples.prompt_block(found)


def _check_semantics(sql_text: str, dialect: str, schema: SchemaSlice) -> None:
    """Refuse a statement the schema cannot support (P4-F07).

    The guard proves the statement is safe; this proves it is meaningful. A model
    that invents a column or sums a string produces a query that would fail at
    execution with a database error — the user gets an actionable message instead.
    """
    try:
        result = sql_semantics.validate(sql_text, schema=schema, dialect=dialect)
    except sql_semantics.SemanticsError as exc:
        raise SqlCandidateError(exc.code, str(exc)) from exc
    if not result.ok:
        logger.info("sql_candidate outcome=blocked semantic_code=%s", result.code)
        raise SqlCandidateError("semantic_invalid", result.message())


def _resolve_dialect(
    schema: SchemaSlice, *, database: str | None, dialect: str | None
) -> str:
    """Pick the dialect to write in, refusing to guess between several."""
    if not schema.datasets:
        raise SqlCandidateError("no_schema", "no authorized tables are available")
    try:
        return sql_execution.resolve_dialect(
            schema, database=database, dialect=dialect
        )
    except sql_guard.SqlPolicyError as exc:
        raise SqlCandidateError(exc.code, str(exc)) from exc


def _parse_candidate(content: str) -> tuple[str, str, tuple[str, ...]]:
    """Validate the JSON body: the router checked the shape, this checks the types."""
    try:
        document = parse_json_object(content, required_keys=REQUIRED_KEYS)
    except StructuredOutputError as exc:
        raise SqlCandidateError("invalid_response", str(exc)) from exc

    sql_text = document.get("sql")
    if not isinstance(sql_text, str) or not sql_text.strip():
        raise SqlCandidateError("invalid_response", "the candidate has no SQL")
    rationale = document.get("rationale")
    assumptions = document.get("assumptions")
    if not isinstance(rationale, str):
        raise SqlCandidateError("invalid_response", "rationale must be a string")
    if not isinstance(assumptions, list | tuple):
        raise SqlCandidateError("invalid_response", "assumptions must be a list")

    clean_assumptions = tuple(
        " ".join(str(item).split())[:MAX_ASSUMPTION_CHARS]
        for item in assumptions
        if str(item).strip()
    )[:MAX_ASSUMPTIONS]
    return (
        sql_text.strip(),
        " ".join(rationale.split())[:MAX_RATIONALE_CHARS],
        clean_assumptions,
    )


def _guard(
    sql_text: str,
    dialect: str,
    schema: SchemaSlice,
    database: str | None,
    max_rows: int,
    *,
    allow_metadata_schemas: bool = False,
) -> sql_guard.GuardResult:
    allowlist = schema.allowed_tables(database=database)
    try:
        return sql_guard.check(
            sql_text,
            dialect=dialect,
            allowed_tables=allowlist,
            max_rows=max_rows,
            connection_names=schema.connection_names(),
            virtual_tables=schema.virtual_tables(database=database),
            rls_targets=schema.rls_targets(database=database),
            allow_metadata_schemas=allow_metadata_schemas,
        )
    except sql_guard.SqlPolicyError as exc:
        logger.info("sql_candidate outcome=blocked guard_code=%s", exc.code)
        raise SqlCandidateError(
            "sql_blocked",
            "the generated statement was rejected by the SQL policy",
            guard_code=exc.code,
        ) from exc
