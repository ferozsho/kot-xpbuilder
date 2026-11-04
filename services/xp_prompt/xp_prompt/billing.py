"""Compare what we estimated against what the provider actually billed (P9-F10).

The ledger's cost is an estimate from a published price list. A billing export is
what actually left the account. The two will disagree — a rounding rule, a
discount, a cached-token policy — and the disagreement is the useful part, which
is why the estimate is never overwritten by the import.

What this module guarantees:

* **the estimate is preserved.** ``estimated_cost_micros`` on the attempt is
  untouched; the billed figure lives on its own row.
* **a re-import is idempotent.** A fingerprint over the normalised rows means
  importing the same export twice yields one import, not doubled revenue.
* **matching is explicit about how it matched.** A line is matched by the
  provider's request id when the export carries one, and reported as unmatched
  otherwise — a silently guessed match would make the variance meaningless.
* **unattributed attempts are counted too.** Spend that appears in our ledger but
  not on the invoice is a different problem from the reverse, and an operator
  needs both numbers.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse

from xp_prompt.config import Settings
from xp_prompt.db import connect

logger = logging.getLogger(__name__)

MATCH_REQUEST_ID = "request_id"
MATCH_NONE = "none"

MAX_ROWS = 20_000
MAX_SOURCE_CHARS = 64
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


class BillingError(ValueError):
    """A refused import; ``code`` is stable and safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Import:
    id: str
    provider: str
    period_start: datetime | None
    period_end: datetime | None
    currency: str
    source: str
    row_count: int
    total_actual_micros: int
    created_at: datetime | None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "period_start": _isoformat(self.period_start),
            "period_end": _isoformat(self.period_end),
            "currency": self.currency,
            "source": self.source,
            "row_count": self.row_count,
            "total_actual_micros": self.total_actual_micros,
            "created_at": _isoformat(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class Line:
    """One row of a billing export, normalised and bounded."""

    provider_request_id: str | None
    model: str
    actual_micros: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def parse(cls, row: Any) -> Line:
        if not isinstance(row, Mapping):
            raise BillingError("invalid_row", "each billing row must be an object")
        try:
            micros = int(row.get("actual_micros"))
        except (TypeError, ValueError) as exc:
            raise BillingError(
                "invalid_row", "each billing row needs integer actual_micros"
            ) from exc
        if micros < 0:
            raise BillingError("invalid_row", "actual_micros must not be negative")
        model = " ".join(str(row.get("model") or "").split())[:128]
        if not model:
            raise BillingError("invalid_row", "each billing row needs a model")
        return cls(
            provider_request_id=(
                str(row.get("provider_request_id") or "").strip()[:255] or None
            ),
            model=model,
            actual_micros=micros,
            input_tokens=_int_or_none(row.get("input_tokens")),
            output_tokens=_int_or_none(row.get("output_tokens")),
            total_tokens=_int_or_none(row.get("total_tokens")),
        )


def record_import(
    settings: Settings,
    *,
    tenant_key: str,
    provider: str,
    period_start: datetime,
    period_end: datetime,
    currency: str,
    rows: Iterable[Any],
    source: str = "",
) -> tuple[Import, bool]:
    """Store one billing export and match its lines; returns ``(import, replayed)``."""
    clean_provider = " ".join((provider or "").split())[:32]
    if not clean_provider:
        raise BillingError("provider_required", "a billing import needs a provider")
    code = (currency or "").strip().upper()
    if len(code) != 3:
        raise BillingError("invalid_currency", "currency must be a three-letter code")
    if period_end <= period_start:
        raise BillingError("invalid_period", "period_end must be after period_start")

    lines = [Line.parse(row) for row in rows or ()]
    if not lines:
        raise BillingError("empty_import", "a billing import needs at least one row")
    if len(lines) > MAX_ROWS:
        raise BillingError(
            "import_budget", f"a billing import may contain at most {MAX_ROWS} rows"
        )
    fingerprint = _fingerprint(clean_provider, lines)
    total = sum(line.actual_micros for line in lines)

    with connect(settings) as connection, connection.cursor() as cursor:
        tenant_id = _tenant_id(cursor, tenant_key)
        cursor.execute(
            """
            INSERT INTO ai_billing_import (
                tenant_id, provider, period_start, period_end, currency,
                source, row_count, total_actual_micros, fingerprint
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, provider, period_start, period_end, fingerprint)
                WHERE fingerprint <> ''
            DO NOTHING
            RETURNING id, provider, period_start, period_end, currency, source,
                      row_count, total_actual_micros, created_at
            """,
            (
                tenant_id,
                clean_provider,
                period_start,
                period_end,
                code,
                " ".join((source or "").split())[:MAX_SOURCE_CHARS],
                len(lines),
                total,
                fingerprint,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                """
                SELECT id, provider, period_start, period_end, currency, source,
                       row_count, total_actual_micros, created_at
                FROM ai_billing_import
                WHERE tenant_id = %s AND provider = %s AND period_start = %s
                  AND period_end = %s AND fingerprint = %s
                """,
                (tenant_id, clean_provider, period_start, period_end, fingerprint),
            )
            existing = cursor.fetchone()
            logger.info("billing_import replay import=%s", existing["id"])
            return _import(existing), True

        import_id = str(row["id"])
        for line in lines:
            cursor.execute(
                """
                INSERT INTO ai_billing_line (
                    import_id, provider, model, provider_request_id,
                    actual_micros, input_tokens, output_tokens, total_tokens
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    import_id,
                    clean_provider,
                    line.model,
                    line.provider_request_id,
                    line.actual_micros,
                    line.input_tokens,
                    line.output_tokens,
                    line.total_tokens,
                ),
            )
        matched = _match(cursor, import_id, clean_provider)

    logger.info(
        "billing_import stored import=%s provider=%s rows=%d matched=%d total=%d",
        import_id,
        clean_provider,
        len(lines),
        matched,
        total,
    )
    return _import(row), False


def fetch_export(
    settings: Settings,
    *,
    tenant_key: str,
    provider: str | None = None,
    currency: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    source_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Pull a billing export from the configured source and import it.

    This is what turns "an operator pastes rows" into "the export is fetched". It
    is deliberately narrow:

    * the source must be **https**, and the host must not be a loopback, private or
      metadata address — a server-side fetch of an admin-supplied URL is an SSRF
      primitive unless it is constrained (a hostname that later resolves to a
      private address is not caught here; the setting is operator-controlled and
      this is documented rather than pretended away);
    * the body must be JSON: either a list of rows, or an object with ``rows``;
    * a period is required, taken from the body or the arguments, and defaulted to
      the previous UTC day so a daily job needs no state;
    * the token is sent as a bearer header and never logged.

    Everything else is :func:`record_import`, so a fetched export is matched and
    stored exactly like a pasted one.
    """
    url = (source_url or settings.billing_source_url or "").strip()
    if not url:
        raise BillingError(
            "not_configured",
            "no billing source is configured; set XP_PROMPT_BILLING_SOURCE_URL",
        )
    _assert_fetchable(url)
    secret = token if token is not None else settings.billing_source_token
    body = _get_json(url, secret)

    rows = body.get("rows") if isinstance(body, Mapping) else body
    if not isinstance(rows, list):
        raise BillingError(
            "invalid_export", "the billing export must be a list of rows"
        )
    resolved_provider = (
        provider
        or (body.get("provider") if isinstance(body, Mapping) else None)
        or settings.billing_source_provider
    )
    if not resolved_provider:
        raise BillingError(
            "provider_required",
            "the export did not name a provider; set "
            "XP_PROMPT_BILLING_SOURCE_PROVIDER",
        )
    resolved_currency = (
        currency
        or (body.get("currency") if isinstance(body, Mapping) else None)
        or settings.billing_source_currency
    )
    start, end = _export_period(body, period_start, period_end)

    record, replayed = record_import(
        settings,
        tenant_key=tenant_key,
        provider=str(resolved_provider),
        period_start=start,
        period_end=end,
        currency=str(resolved_currency),
        rows=rows,
        source=_source_label(url),
    )
    logger.info(
        "billing_fetch provider=%s rows=%d import=%s replayed=%s",
        resolved_provider,
        len(rows),
        record.id,
        replayed,
    )
    return {
        "import": record.to_public_dict(),
        "replayed": replayed,
        "rows": len(rows),
        "source": _source_label(url),
    }


def source_status(settings: Settings) -> dict[str, Any]:
    """Whether a billing source is configured, without revealing the URL or token."""
    url = (settings.billing_source_url or "").strip()
    return {
        "configured": bool(url),
        "provider": settings.billing_source_provider or "",
        "currency": settings.billing_source_currency,
        "host": _host(url) if url else "",
    }


def list_imports(
    settings: Settings, *, tenant_key: str, limit: int = DEFAULT_LIST_LIMIT
) -> list[Import]:
    bounded = max(1, min(int(limit), MAX_LIST_LIMIT))
    with connect(settings) as connection, connection.cursor() as cursor:
        tenant_id = _tenant_id(cursor, tenant_key)
        cursor.execute(
            """
            SELECT id, provider, period_start, period_end, currency, source,
                   row_count, total_actual_micros, created_at
            FROM ai_billing_import
            WHERE tenant_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (tenant_id, bounded),
        )
        rows = cursor.fetchall()
    return [_import(row) for row in rows]


def variance(settings: Settings, import_id: str, *, tenant_key: str) -> dict[str, Any]:
    """Estimated versus billed, with the unmatched counts that explain the gap."""
    with connect(settings) as connection, connection.cursor() as cursor:
        tenant_id = _tenant_id(cursor, tenant_key)
        cursor.execute(
            """
            SELECT id, provider, period_start, period_end, currency, source,
                   row_count, total_actual_micros, created_at
            FROM ai_billing_import
            WHERE id = %s AND tenant_id = %s
            """,
            (str(import_id), tenant_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise BillingError("import_not_found", "billing import not found")
        record = _import(row)

        cursor.execute(
            """
            SELECT
                COUNT(*) AS lines,
                SUM(actual_micros) AS actual_micros,
                SUM(CASE WHEN matched_attempt_id IS NULL THEN 1 ELSE 0 END) AS unmatched,
                COUNT(matched_attempt_id) AS matched
            FROM ai_billing_line
            WHERE import_id = %s
            """,
            (record.id,),
        )
        totals = cursor.fetchone() or {}

        cursor.execute(
            """
            SELECT COALESCE(SUM(a.estimated_cost_micros), 0) AS estimated_micros,
                   COUNT(a.id) AS matched_attempts,
                   SUM(CASE WHEN a.estimated_cost_micros IS NULL THEN 1 ELSE 0 END)
                       AS unpriced_attempts
            FROM ai_billing_line l
            JOIN ai_provider_attempt a ON a.id = l.matched_attempt_id
            WHERE l.import_id = %s
            """,
            (record.id,),
        )
        estimated = cursor.fetchone() or {}

        cursor.execute(
            """
            SELECT COUNT(*) AS unattributed
            FROM ai_provider_attempt a
            WHERE a.tenant_id = %s
              AND a.provider = %s
              AND a.started_at >= %s AND a.started_at < %s
              AND a.provider_request_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM ai_billing_line l
                  WHERE l.import_id = %s
                    AND l.provider_request_id = a.provider_request_id
              )
            """,
            (
                tenant_id,
                record.provider,
                record.period_start,
                record.period_end,
                record.id,
            ),
        )
        unattributed = cursor.fetchone() or {}

        cursor.execute(
            """
            SELECT l.model,
                   SUM(l.actual_micros) AS actual_micros,
                   COALESCE(SUM(a.estimated_cost_micros), 0) AS estimated_micros,
                   COUNT(l.id) AS lines,
                   COUNT(l.matched_attempt_id) AS matched
            FROM ai_billing_line l
            LEFT JOIN ai_provider_attempt a ON a.id = l.matched_attempt_id
            WHERE l.import_id = %s
            GROUP BY l.model
            ORDER BY actual_micros DESC
            """,
            (record.id,),
        )
        models = cursor.fetchall()

    actual_micros = int(totals.get("actual_micros") or 0)
    estimated_micros = int(estimated.get("estimated_micros") or 0)
    difference = actual_micros - estimated_micros
    return {
        "import": record.to_public_dict(),
        "lines": int(totals.get("lines") or 0),
        "matched_lines": int(totals.get("matched") or 0),
        "unmatched_lines": int(totals.get("unmatched") or 0),
        "matched_attempts": int(estimated.get("matched_attempts") or 0),
        "unpriced_attempts": int(estimated.get("unpriced_attempts") or 0),
        "unattributed_attempts": int(unattributed.get("unattributed") or 0),
        "actual_micros": actual_micros,
        "estimated_micros": estimated_micros,
        "difference_micros": difference,
        "difference_percent": (
            round(difference * 100 / estimated_micros, 2)
            if estimated_micros
            else None
        ),
        "estimate_is_preserved": True,
        "models": [
            {
                "model": entry["model"],
                "lines": int(entry.get("lines") or 0),
                "matched": int(entry.get("matched") or 0),
                "actual_micros": int(entry.get("actual_micros") or 0),
                "estimated_micros": int(entry.get("estimated_micros") or 0),
                "difference_micros": int(entry.get("actual_micros") or 0)
                - int(entry.get("estimated_micros") or 0),
            }
            for entry in models
        ],
    }


def _match(cursor: Any, import_id: str, provider: str) -> int:
    """Attach each line to the attempt with the same provider request id.

    Only an exact request id is treated as a match. A guessed match by model and
    day would hide the very disagreement this report exists to show.
    """
    cursor.execute(
        """
        UPDATE ai_billing_line l
        SET matched_attempt_id = a.id,
            match_kind = 'request_id'
        FROM ai_provider_attempt a
        WHERE l.import_id = %s
          AND l.provider_request_id IS NOT NULL
          AND a.provider = %s
          AND a.provider_request_id = l.provider_request_id
          AND l.matched_attempt_id IS NULL
        RETURNING l.id
        """,
        (str(import_id), provider),
    )
    return len(cursor.fetchall() or [])


#: Addresses a billing source may never point at. Fetching an admin-supplied URL
#: server-side is an SSRF primitive, so the obvious targets are refused outright.
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata",
        "instance-data",
    }
)
FETCH_TIMEOUT_SECONDS = 20
MAX_EXPORT_BYTES = 8 * 1024 * 1024


def _assert_fetchable(url: str) -> None:
    """Refuse anything that is not a plain https call to a public host."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise BillingError(
            "unsafe_source", "the billing source must use https"
        )
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise BillingError("unsafe_source", "the billing source has no host")
    if host in _BLOCKED_HOSTS:
        raise BillingError(
            "unsafe_source", "the billing source may not be an internal host"
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return  # a name, not a literal: nothing more can be checked here
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    ):
        raise BillingError(
            "unsafe_source", "the billing source may not be a private address"
        )


def _get_json(url: str, token: str | None) -> Any:
    """One bounded GET, parsed as JSON; errors stay content-free."""
    headers = {"Accept": "application/json", "User-Agent": "xp-prompt-billing/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request_ = urllib_request.Request(url, headers=headers, method="GET")
    try:
        with urllib_request.urlopen(
            request_, timeout=FETCH_TIMEOUT_SECONDS
        ) as response:
            raw = response.read(MAX_EXPORT_BYTES + 1)
    except urllib_error.HTTPError as exc:
        raise BillingError(
            "fetch_failed", f"the billing source answered HTTP {exc.code}"
        ) from exc
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        raise BillingError(
            "fetch_failed", f"the billing source could not be reached "
            f"({type(exc).__name__})"
        ) from exc
    if len(raw) > MAX_EXPORT_BYTES:
        raise BillingError("export_too_large", "the billing export is too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BillingError(
            "invalid_export", "the billing export is not valid JSON"
        ) from exc


def _export_period(
    body: Any,
    period_start: datetime | None,
    period_end: datetime | None,
) -> tuple[datetime, datetime]:
    """The period the export covers, defaulted to the previous UTC day."""
    start = period_start or (_iso(body, "period_start") if body else None)
    end = period_end or (_iso(body, "period_end") if body else None)
    if start is None or end is None:
        end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=1)
    if end <= start:
        raise BillingError("invalid_period", "period_end must be after period_start")
    return start, end


def _iso(body: Any, key: str) -> datetime | None:
    if not isinstance(body, Mapping):
        return None
    value = body.get(key)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A naive timestamp in a cost report is UTC. Using the host's local
        # timezone would shift the billing window by wherever the worker runs.
        return parsed.replace(tzinfo=UTC)
    return parsed


def _source_label(url: str) -> str:
    """A storable description of where an export came from: host, never the URL."""
    host = _host(url)
    return f"fetch:{host}"[:MAX_SOURCE_CHARS] if host else "fetch"


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "")[:128]
    except ValueError:
        return ""


def _fingerprint(provider: str, lines: list[Line]) -> str:
    """Identity of an export, insensitive to row order."""
    body = json.dumps(
        {
            "provider": provider,
            "lines": sorted(
                [
                    [
                        line.provider_request_id or "",
                        line.model,
                        line.actual_micros,
                        line.input_tokens,
                        line.output_tokens,
                        line.total_tokens,
                    ]
                    for line in lines
                ]
            ),
        },
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _import(row: Mapping[str, Any]) -> Import:
    return Import(
        id=str(row["id"]),
        provider=row["provider"],
        period_start=row.get("period_start"),
        period_end=row.get("period_end"),
        currency=row["currency"],
        source=row.get("source") or "",
        row_count=int(row.get("row_count") or 0),
        total_actual_micros=int(row.get("total_actual_micros") or 0),
        created_at=row.get("created_at"),
    )


def _tenant_id(cursor: Any, tenant_key: str) -> str:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    if row is None:
        raise BillingError("unknown_tenant", "tenant is not provisioned")
    return str(row["id"])


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
