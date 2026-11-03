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
from typing import Any

from xp_prompt import responses, sql_execution, sql_guard
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
CANDIDATE_MAX_TOKENS = 2048
CANDIDATE_TEMPERATURE = 0.0
MAX_QUESTION_CHARS = 2_000
MAX_RATIONALE_CHARS = 600
MAX_ASSUMPTIONS = 8
MAX_ASSUMPTION_CHARS = 300
REQUIRED_KEYS = ("sql", "rationale", "assumptions")

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
            "blocks": list(self.blocks),
        }


def build_request(
    question: str,
    schema: SchemaSlice,
    *,
    dialect: str,
    max_rows: int,
    correlation_id: str | None = None,
) -> ProviderRequest:
    """The one provider request this pipeline makes, built only from the slice."""
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

    return ProviderRequest(
        operation="sql.candidate",
        messages=(
            Message(
                "system",
                SYSTEM_PROMPT.format(
                    max_rows=max(1, int(max_rows)), dialect=normal_dialect
                ),
            ),
            Message("user", f"Question: {text}\n\nAvailable data:\n{schema_text}"),
        ),
        max_output_tokens=CANDIDATE_MAX_TOKENS,
        temperature=CANDIDATE_TEMPERATURE,
        json_output=True,
        json_required_keys=REQUIRED_KEYS,
        # SQL generation is a transcription task: thinking only competes with the
        # JSON budget, and a budget-starved answer is truncated mid-string.
        reasoning_budget_tokens=0,
        correlation_id=correlation_id or uuid.uuid4().hex,
    )


def generate(
    settings: Settings,
    *,
    question: str,
    schema: SchemaSlice,
    tenant_key: str = registry.DEFAULT_TENANT_KEY,
    database: str | None = None,
    dialect: str | None = None,
    max_rows: int = sql_guard.MAX_ROWS,
) -> SqlCandidate:
    """Ask for one candidate, validate it, and return it with its blocks."""
    resolved_dialect = _resolve_dialect(schema, database=database, dialect=dialect)
    request = build_request(
        question, schema, dialect=resolved_dialect, max_rows=max_rows
    )

    try:
        router = registry.resolve_router(settings, tenant_key=tenant_key)
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
    guarded = _guard(sql_text, resolved_dialect, schema, database, max_rows)

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
    logger.info(
        "sql_candidate correlation_id=%s provider=%s model=%s tables=%d limit=%d "
        "outcome=succeeded",
        correlation_id,
        routed.response.provider,
        routed.response.model,
        len(guarded.tables),
        guarded.limit,
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
    )


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
) -> sql_guard.GuardResult:
    allowlist = schema.allowed_tables(database=database)
    try:
        return sql_guard.check(
            sql_text,
            dialect=dialect,
            allowed_tables=allowlist,
            max_rows=max_rows,
            connection_names=schema.connection_names(),
        )
    except sql_guard.SqlPolicyError as exc:
        logger.info("sql_candidate outcome=blocked guard_code=%s", exc.code)
        raise SqlCandidateError(
            "sql_blocked",
            "the generated statement was rejected by the SQL policy",
            guard_code=exc.code,
        ) from exc
