"""Failure classification, repair budgets, and the repair ledger (P5-F01/F06/F07).

Self-healing starts with agreeing on what went wrong. This module turns "the query
failed" into a stable category, a stable kind, and a stable fingerprint, so the same
failure is recognised the second time — and so a *policy* refusal is never mistaken
for something to repair.

Three rules shape it:

* **terminal outcomes are terminal.** A permission denial, a tenant-policy denial, a
  destructive intent, or a budget refusal is not an error to heal around; it is the
  system working. Those never become retryable, whatever else the message says.
* **fingerprints carry no content.** The signature is the SQLSTATE/errno when there is
  one, otherwise a *masked template* of the message (literals, digits and long tokens
  removed). That makes "the same failure" recognisable without storing what a user
  asked for.
* **budgets are enforced here, not by the caller.** Attempts, elapsed time and tokens
  are checked against a :class:`RepairBudget`; a caller cannot raise its own ceiling,
  the same way the cost guard owns its limits (ADR-0015).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from xp_prompt.config import Settings
from xp_prompt.db import connect

logger = logging.getLogger(__name__)

#: Where the failure happened. Matches the audit trail's stages (ADR-0016).
STAGES: tuple[str, ...] = (
    "candidate",
    "guard",
    "plan",
    "execute",
    "result",
    "store",
    "repair",
)

#: Top-level taxonomy (Phase 5 plan).
CATEGORIES: tuple[str, ...] = ("provider", "sql", "artifact", "system")

#: Fine-grained kinds. Stable strings: they are stored and compared.
TIMEOUT = "timeout"
RATE_LIMIT = "rate_limit"
QUOTA = "quota"
UNAVAILABLE = "unavailable"
MALFORMED_RESPONSE = "malformed_response"
SYNTAX_ERROR = "syntax_error"
UNKNOWN_IDENTIFIER = "unknown_identifier"
DIALECT_MISMATCH = "dialect_mismatch"
TYPE_ERROR = "type_error"
UNSAFE_STATEMENT = "unsafe_statement"
PERMISSION_DENIED = "permission_denied"
TENANT_POLICY_DENIED = "tenant_policy_denied"
DESTRUCTIVE_INTENT = "destructive_intent"
BUDGET_DENIED = "budget_denied"
EMPTY_RESULT = "empty_result"
WORKER_CRASH = "worker_crash"
VERSION_MISMATCH = "version_mismatch"
UNKNOWN = "unknown"

#: Kinds that must never be retried or rewritten: they are policy outcomes.
TERMINAL_KINDS: frozenset[str] = frozenset(
    {
        PERMISSION_DENIED,
        TENANT_POLICY_DENIED,
        DESTRUCTIVE_INTENT,
        BUDGET_DENIED,
    }
)

#: Kinds worth another attempt (cheap, and a different phrasing may succeed).
RETRYABLE_KINDS: frozenset[str] = frozenset(
    {
        TIMEOUT,
        RATE_LIMIT,
        UNAVAILABLE,
        MALFORMED_RESPONSE,
        SYNTAX_ERROR,
        UNKNOWN_IDENTIFIER,
        DIALECT_MISMATCH,
        TYPE_ERROR,
        EMPTY_RESULT,
        WORKER_CRASH,
    }
)

_KIND_CATEGORY: dict[str, str] = {
    TIMEOUT: "provider",
    RATE_LIMIT: "provider",
    QUOTA: "provider",
    UNAVAILABLE: "provider",
    MALFORMED_RESPONSE: "provider",
    SYNTAX_ERROR: "sql",
    UNKNOWN_IDENTIFIER: "sql",
    DIALECT_MISMATCH: "sql",
    TYPE_ERROR: "sql",
    UNSAFE_STATEMENT: "sql",
    PERMISSION_DENIED: "sql",
    TENANT_POLICY_DENIED: "sql",
    DESTRUCTIVE_INTENT: "sql",
    BUDGET_DENIED: "sql",
    EMPTY_RESULT: "sql",
    WORKER_CRASH: "system",
    VERSION_MISMATCH: "system",
    UNKNOWN: "system",
}

#: SQLSTATE classes that identify a failure kind, checked on the prefix.
_SQLSTATE_KINDS: tuple[tuple[str, str], ...] = (
    ("42501", PERMISSION_DENIED),
    ("42P01", UNKNOWN_IDENTIFIER),
    ("42703", UNKNOWN_IDENTIFIER),
    ("42P02", UNKNOWN_IDENTIFIER),
    ("42883", UNKNOWN_IDENTIFIER),
    ("42601", SYNTAX_ERROR),
    ("42000", SYNTAX_ERROR),
    ("57014", TIMEOUT),
    ("55P03", TIMEOUT),
    ("22", TYPE_ERROR),
    ("23", TYPE_ERROR),
    ("08", UNAVAILABLE),
    ("53", UNAVAILABLE),
)

#: MySQL/MariaDB errno values for the same shapes.
_ERRNO_KINDS: tuple[tuple[str, str], ...] = (
    ("1044", PERMISSION_DENIED),
    ("1045", PERMISSION_DENIED),
    ("1142", PERMISSION_DENIED),
    ("1054", UNKNOWN_IDENTIFIER),
    ("1146", UNKNOWN_IDENTIFIER),
    ("1064", SYNTAX_ERROR),
    ("1052", UNKNOWN_IDENTIFIER),
    ("1366", TYPE_ERROR),
    ("1292", TYPE_ERROR),
    ("1205", TIMEOUT),
    ("1213", TIMEOUT),
)

#: Provider codes this deployment raises itself.
_PROVIDER_CODES: dict[str, str] = {
    "timeout": TIMEOUT,
    "rate_limited": RATE_LIMIT,
    "rate_limit": RATE_LIMIT,
    "quota_exceeded": QUOTA,
    "unavailable": UNAVAILABLE,
    "connection_error": UNAVAILABLE,
    "structured_output_invalid": MALFORMED_RESPONSE,
    "invalid_response": MALFORMED_RESPONSE,
}

#: Message heuristics, used only when no code identifies the failure.
_MESSAGE_HINTS: tuple[tuple[str, str], ...] = (
    ("permission denied", PERMISSION_DENIED),
    ("not authorized", PERMISSION_DENIED),
    ("insufficient privilege", PERMISSION_DENIED),
    ("row-level security", TENANT_POLICY_DENIED),
    ("row level security", TENANT_POLICY_DENIED),
    ("rate limit", RATE_LIMIT),
    ("too many requests", RATE_LIMIT),
    ("quota", QUOTA),
    ("payment required", QUOTA),
    ("402", QUOTA),
    ("timed out", TIMEOUT),
    ("timeout", TIMEOUT),
    ("canceling statement", TIMEOUT),
    ("does not exist", UNKNOWN_IDENTIFIER),
    ("unknown column", UNKNOWN_IDENTIFIER),
    ("undefined", UNKNOWN_IDENTIFIER),
    ("syntax", SYNTAX_ERROR),
    ("parse error", SYNTAX_ERROR),
    ("could not parse", SYNTAX_ERROR),
    ("multiple statements", UNSAFE_STATEMENT),
    ("not read-only", UNSAFE_STATEMENT),
    ("read-only", UNSAFE_STATEMENT),
    ("group by", TYPE_ERROR),
    ("must appear in the group by", TYPE_ERROR),
    ("numeric", TYPE_ERROR),
    ("invalid input syntax", TYPE_ERROR),
    ("connection", UNAVAILABLE),
    ("unreachable", UNAVAILABLE),
    ("circuit", UNAVAILABLE),
    ("version", VERSION_MISMATCH),
)

#: Human hints per kind: what the caller can do about it.
_HINTS: dict[str, str] = {
    TIMEOUT: "The query or the provider took too long. Narrow the question.",
    RATE_LIMIT: "The provider is rate limiting. Wait a moment and try again.",
    QUOTA: "The provider refused for billing or quota reasons. Check the API key.",
    UNAVAILABLE: "A dependency was unreachable. Try again shortly.",
    MALFORMED_RESPONSE: "The provider answered in the wrong shape. Try again.",
    SYNTAX_ERROR: "The generated SQL did not parse. Ask again, or rephrase.",
    UNKNOWN_IDENTIFIER: "The statement referenced something that does not exist.",
    DIALECT_MISMATCH: "The statement used another dialect's syntax.",
    TYPE_ERROR: "The statement mixed incompatible types or groupings.",
    UNSAFE_STATEMENT: "The statement was refused by the SQL policy.",
    PERMISSION_DENIED: "You are not authorized to read this data.",
    TENANT_POLICY_DENIED: "Row-level rules exclude this data for your account.",
    DESTRUCTIVE_INTENT: "Only read-only questions are supported.",
    BUDGET_DENIED: "This query is too expensive for the workspace budget.",
    EMPTY_RESULT: "The query ran but returned no rows.",
    WORKER_CRASH: "A background step failed. Try again.",
    VERSION_MISMATCH: "The deployment is mid-upgrade. Try again shortly.",
    UNKNOWN: "The request failed. Try again, or rephrase the question.",
}

MAX_MESSAGE_CHARS = 400
MAX_SIGNATURE_CHARS = 120
MAX_FIX_CHARS = 400

DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_MAX_ELAPSED_MS = 30_000
DEFAULT_MAX_TOKENS = 4_000

_QUOTED = re.compile(r"'[^']{0,200}'|\"[^\"]{0,200}\"")
_NUMBER = re.compile(r"\b\d+\b")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


@dataclass(frozen=True, slots=True)
class FailureEvent:
    """One classified failure: stable, content-free, and actionable."""

    stage: str
    kind: str
    category: str
    fingerprint: str
    retryable: bool
    terminal: bool
    hint: str
    detail: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "kind": self.kind,
            "category": self.category,
            "fingerprint": self.fingerprint,
            "retryable": self.retryable,
            "terminal": self.terminal,
            "hint": self.hint,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RepairBudget:
    """What one repair loop may spend. Owned here, like every other budget."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_elapsed_ms: int = DEFAULT_MAX_ELAPSED_MS
    max_tokens: int = DEFAULT_MAX_TOKENS
    #: A fix that failed before is not tried again (P5-F07).
    suppress_failed_fixes: bool = True

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "max_elapsed_ms": self.max_elapsed_ms,
            "max_tokens": self.max_tokens,
            "suppress_failed_fixes": self.suppress_failed_fixes,
        }


@dataclass(frozen=True, slots=True)
class RepairState:
    """What has already been spent on this failure."""

    attempts: int = 0
    elapsed_ms: int = 0
    tokens: int = 0

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
            "tokens": self.tokens,
        }


def classify(
    *,
    stage: str,
    kind_hint: str = "",
    error_class: str = "",
    sqlstate: str = "",
    errno: str = "",
    message: str = "",
) -> FailureEvent:
    """Classify one failure. Deterministic, ordered, and never raises."""
    where = _stage(stage)
    kind = _kind(kind_hint, sqlstate, errno, message, error_class)
    terminal = kind in TERMINAL_KINDS
    retryable = (not terminal) and kind in RETRYABLE_KINDS
    return FailureEvent(
        stage=where,
        kind=kind,
        category=_category(kind, where),
        fingerprint=fingerprint(
            stage=where, kind=kind, sqlstate=sqlstate, errno=errno, message=message
        ),
        retryable=retryable,
        terminal=terminal,
        hint=_HINTS.get(kind, _HINTS[UNKNOWN]),
        detail=_redact(message),
    )


def fingerprint(
    *, stage: str, kind: str, sqlstate: str = "", errno: str = "", message: str = ""
) -> str:
    """A stable identity for "this failure", with no content in it.

    A SQLSTATE/errno is used verbatim when present, because that *is* the identity.
    Otherwise the message is reduced to a template: quoted literals, numbers and long
    tokens are removed, so two users hitting the same mistake agree on the fingerprint
    and neither contributes data to it.
    """
    identity = (sqlstate or errno or _template(message)).strip().upper()
    material = json.dumps(
        {"stage": stage, "kind": kind, "identity": identity},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def can_repair(
    event: FailureEvent, state: RepairState, budget: RepairBudget
) -> tuple[bool, str]:
    """May a repair be attempted? ``(allowed, reason)`` with a stable reason code."""
    if event.terminal:
        return False, "terminal_policy"
    if not event.retryable:
        return False, "not_retryable"
    if state.attempts >= max(0, budget.max_attempts):
        return False, "attempts_exhausted"
    if budget.max_elapsed_ms and state.elapsed_ms >= budget.max_elapsed_ms:
        return False, "deadline_exceeded"
    if budget.max_tokens and state.tokens >= budget.max_tokens:
        return False, "token_budget_exhausted"
    return True, "allowed"


def fix_fingerprint(sql: str) -> str:
    """Identity of a *proposed fix*, so a failed one is not retried unchanged."""
    text = " ".join((sql or "").split())[:MAX_FIX_CHARS].lower()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def record_attempt(
    settings: Settings,
    *,
    tenant_key: str,
    fingerprint_value: str,
    kind: str,
    stage: str,
    outcome: str,
    fix_fingerprint_value: str = "",
    conversation_id: str | None = None,
) -> bool:
    """Record one repair attempt. Never raises: bookkeeping must not break a turn."""
    if outcome not in {"attempted", "succeeded", "failed", "suppressed"}:
        return False
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                tenant_id = _tenant_id(cursor, tenant_key)
                if tenant_id is None:
                    return False
                cursor.execute(
                    """
                    INSERT INTO ai_repair_attempt (
                        tenant_id, conversation_id, failure_fingerprint, kind,
                        stage, outcome, fix_sha256
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        conversation_id,
                        (fingerprint_value or "")[:64],
                        (kind or "")[:32],
                        (stage or "")[:24],
                        outcome,
                        (fix_fingerprint_value or "")[:64],
                    ),
                )
        return True
    except Exception as exc:  # pragma: no cover - best effort
        logger.info("repair_attempt outcome=error reason=%s", type(exc).__name__)
        return False


def failed_fixes(
    settings: Settings, *, tenant_key: str, fingerprint_value: str
) -> set[str]:
    """Fix fingerprints that already failed for this failure, in this tenant."""
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                tenant_id = _tenant_id(cursor, tenant_key)
                if tenant_id is None:
                    return set()
                cursor.execute(
                    """
                    SELECT DISTINCT fix_sha256
                    FROM ai_repair_attempt
                    WHERE tenant_id = %s AND failure_fingerprint = %s
                      AND outcome = 'failed' AND fix_sha256 <> ''
                    """,
                    (tenant_id, (fingerprint_value or "")[:64]),
                )
                rows = cursor.fetchall()
    except Exception as exc:  # pragma: no cover - best effort
        logger.info("repair_attempt outcome=error kind=failed_fixes reason=%s", type(exc).__name__)
        return set()
    return {row["fix_sha256"] for row in rows if row.get("fix_sha256")}


def _stage(value: str) -> str:
    stage = (value or "").strip().lower()
    return stage if stage in STAGES else "execute"


def _category(kind: str, stage: str) -> str:
    """Which layer failed. A timeout at execute is the database, not the model."""
    if kind in {TIMEOUT, UNAVAILABLE}:
        return "provider" if stage in {"candidate", "repair"} else "system"
    return _KIND_CATEGORY.get(kind, "system")


def _kind(
    kind_hint: str, sqlstate: str, errno: str, message: str, error_class: str
) -> str:
    hint = (kind_hint or "").strip().lower()
    if hint in _KIND_CATEGORY:
        return hint
    if hint in _PROVIDER_CODES:
        return _PROVIDER_CODES[hint]

    code = (sqlstate or "").strip()
    if code:
        for prefix, kind in _SQLSTATE_KINDS:
            if code.startswith(prefix):
                return kind
    number = (errno or "").strip()
    if number:
        for prefix, kind in _ERRNO_KINDS:
            if number == prefix:
                return kind

    text = (message or "").lower()
    for needle, kind in _MESSAGE_HINTS:
        if needle in text:
            return kind

    if (error_class or "").lower() in {"operationalerror", "interfaceerror"}:
        return UNAVAILABLE
    return UNKNOWN


def _template(message: str) -> str:
    """A content-free shape of an error message."""
    text = (message or "").strip()
    if not text:
        return ""
    text = _QUOTED.sub("?", text)
    text = _NUMBER.sub("#", text)
    text = _LONG_TOKEN.sub("#", text)
    return " ".join(text.split())[:MAX_SIGNATURE_CHARS].lower()


def _redact(message: str) -> str:
    """What a caller may show: the shape, with literals and numbers removed."""
    return _template(message)


def budget_from_settings(settings: Settings) -> RepairBudget:
    """The deployment's repair budget (env-tunable, bounded)."""
    return RepairBudget(
        max_attempts=int(getattr(settings, "repair_max_attempts", DEFAULT_MAX_ATTEMPTS)),
        max_elapsed_ms=int(
            getattr(settings, "repair_max_elapsed_ms", DEFAULT_MAX_ELAPSED_MS)
        ),
        max_tokens=int(getattr(settings, "repair_max_tokens", DEFAULT_MAX_TOKENS)),
    )


def _tenant_id(cursor: Any, tenant_key: str) -> Any:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    return row["id"] if row else None
