"""The typed settings registry: which settings exist, and what they may be.

A settings page that accepts arbitrary key/value pairs is not configuration, it is
an injection surface. This module is the authority: a key that is not registered
cannot be written, a value outside its declared range is refused, and a setting
whose ceiling is marked non-overridable cannot be raised above the platform's hard
limit by any tenant.

Four ideas carry the phase:

* **typed and bounded (P10-F01).** Every setting declares a kind, a default,
  bounds, the scopes allowed to hold it, and whether it is dynamic or needs a
  restart. Nothing is inferred from a string.
* **hard ceilings are not settings (P10-F06).** A SQL row cap has an absolute
  maximum the registry enforces even when the stored value is edited directly, so
  a security ceiling is never merely a default that someone can raise.
* **a tenant may tighten, the platform decides the floor.** A tenant override wins
  over the global value for settings that permit it, and never for the ones that
  only make sense globally.
* **every write is versioned and audited (P10-F10).** A write names the version it
  expected, so two admins cannot silently overwrite each other, and the before and
  after values land in an append-only audit row that rollback reads.

No entry here may store a secret. Provider keys are sealed in ``ai_provider``
(ADR-0002); a registry key whose name looks like a credential is refused outright,
so a future contributor cannot quietly add one.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from xp_prompt.config import Settings
from xp_prompt.db import connect

logger = logging.getLogger(__name__)

INT = "int"
BOOL = "bool"
ENUM = "enum"
TEXT = "text"

GLOBAL = "global"
TENANT = "tenant"
SCOPES: tuple[str, ...] = (GLOBAL, TENANT)

MAX_KEY_CHARS = 96
MAX_TEXT_CHARS = 512
MAX_REASON_CHARS = 512
MAX_ACTOR_CHARS = 128
MAX_HISTORY = 200

#: A setting whose name contains a credential *segment* may never be stored here.
#: Matched per dot-separated segment so ``generation.max_output_tokens`` (LLM
#: tokens) is allowed while ``provider.api_key`` (a credential) is not — a loose
#: ``token`` match would have banned the very settings this phase adds.
_SECRET_SEGMENT = re.compile(
    r"^(secret|secrets|password|passwd|credential|credentials|api[_-]?key|"
    r"apikey|access[_-]?token|auth[_-]?token|refresh[_-]?token|bearer|"
    r"private[_-]?key)$",
    re.IGNORECASE,
)


class SettingError(ValueError):
    """A refused configuration write; ``code`` is stable and safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FeatureDisabled(RuntimeError):
    """A capability the tenant has switched off (P10-F08).

    Its own type rather than a bare ``RuntimeError`` so the API can answer 403 for
    every module at once: a feature that is off is a decision, not a fault, and a
    caller must not have to guess which module said no.
    """

    code = "feature_disabled"

    def __init__(self, flag: str, message: str = "") -> None:
        super().__init__(message or f"{flag} is switched off for this tenant")
        self.flag = flag


@dataclass(frozen=True, slots=True)
class Setting:
    """One known setting, with the bounds that make it safe to change."""

    key: str
    kind: str
    default: Any
    owner: str
    summary: str
    minimum: Any = None
    maximum: Any = None
    allowed: tuple[str, ...] = ()
    scopes: tuple[str, ...] = (GLOBAL, TENANT)
    #: Dynamic settings take effect on the next request; the rest need a restart,
    #: and saying so is better than pretending a change applied (P10-F11).
    dynamic: bool = True
    #: ``True`` means ``maximum`` is a platform ceiling, not a preference: a value
    #: above it is refused however the row got there (P10-F06).
    ceiling: bool = False

    @property
    def restart_required(self) -> bool:
        return not self.dynamic

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "default": self.default,
            "owner": self.owner,
            "summary": self.summary,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "allowed": list(self.allowed),
            "scopes": list(self.scopes),
            "dynamic": self.dynamic,
            "restart_required": self.restart_required,
            "ceiling": self.ceiling,
        }

    def coerce(self, value: Any) -> Any:
        """Validate and normalise one value, or raise :class:`SettingError`."""
        if self.kind == BOOL:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in {"true", "1"}:
                return True
            if isinstance(value, str) and value.strip().lower() in {"false", "0"}:
                return False
            raise SettingError(
                "invalid_type", f"{self.key} must be true or false"
            )
        if self.kind == INT:
            if isinstance(value, bool) or not isinstance(value, int | float | str):
                raise SettingError("invalid_type", f"{self.key} must be an integer")
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise SettingError(
                    "invalid_type", f"{self.key} must be an integer"
                ) from exc
            if self.minimum is not None and number < int(self.minimum):
                raise SettingError(
                    "out_of_range",
                    f"{self.key} must be at least {self.minimum}",
                )
            if self.maximum is not None and number > int(self.maximum):
                label = "the platform ceiling" if self.ceiling else "the maximum"
                raise SettingError(
                    "out_of_range",
                    f"{self.key} must be at most {self.maximum} ({label})",
                )
            return number
        if self.kind == ENUM:
            text = str(value or "").strip()
            if text not in self.allowed:
                raise SettingError(
                    "invalid_choice",
                    f"{self.key} must be one of {', '.join(self.allowed)}",
                )
            return text
        if self.kind == TEXT:
            text = " ".join(str(value or "").split())
            if len(text) > MAX_TEXT_CHARS:
                raise SettingError(
                    "too_long", f"{self.key} must be at most {MAX_TEXT_CHARS} chars"
                )
            return text
        raise SettingError("invalid_type", f"{self.key} has an unknown kind")


def _feature(flag: str, summary: str) -> Setting:
    return Setting(
        key=f"feature.{flag}",
        kind=BOOL,
        default=True,
        owner="features",
        summary=summary,
    )


#: Every setting the platform understands. Adding a key here is the only way to
#: make it settable, which is what keeps "unknown key" a refusal rather than a
#: silent no-op.
REGISTRY: tuple[Setting, ...] = (
    # --- providers and routing (P10-F02) -------------------------------
    Setting(
        key="provider.fallback_enabled",
        kind=BOOL,
        default=True,
        owner="providers",
        summary="Try one fallback provider when the primary fails retryably.",
    ),
    Setting(
        key="provider.max_attempts_per_provider",
        kind=INT,
        default=2,
        minimum=1,
        maximum=5,
        owner="providers",
        summary="Attempts against one provider inside one logical request.",
    ),
    Setting(
        key="provider.breaker_cooldown_seconds",
        kind=INT,
        default=60,
        minimum=5,
        maximum=3600,
        owner="providers",
        summary="How long an open circuit breaker skips a provider.",
    ),
    # --- generation controls (P10-F03) ---------------------------------
    Setting(
        key="generation.max_output_tokens",
        kind=INT,
        default=2_048,
        minimum=1,
        maximum=32_768,
        owner="generation",
        summary="Ceiling for a generated answer.",
    ),
    Setting(
        key="generation.temperature_basis_points",
        kind=INT,
        default=20,
        minimum=0,
        maximum=200,
        owner="generation",
        summary=(
            "Sampling temperature in hundredths (0-200 = 0.00-2.00). SQL "
            "generation keeps its own 0.00 so a validated statement is "
            "reproducible."
        ),
    ),
    Setting(
        key="generation.thinking_budget_tokens",
        kind=INT,
        default=0,
        minimum=0,
        maximum=8_192,
        owner="generation",
        summary="Tokens a model may spend thinking before answering.",
    ),
    # --- retry and self-healing (P10-F04/F05) --------------------------
    Setting(
        key="repair.max_attempts",
        kind=INT,
        default=2,
        minimum=0,
        maximum=3,
        owner="repair",
        summary="Repairs attempted for one failing statement; 0 disables repair.",
    ),
    Setting(
        key="repair.pattern_auto_promote",
        kind=BOOL,
        default=True,
        owner="repair",
        summary="Allow a validated correction to become reusable knowledge.",
    ),
    Setting(
        key="repair.pattern_evidence_threshold",
        kind=INT,
        default=2,
        minimum=0,
        maximum=5,
        owner="repair",
        summary="Independent successes required before promotion; 0 disables it.",
    ),
    # --- SQL safeguards (P10-F06) --------------------------------------
    Setting(
        key="sql.max_rows",
        kind=INT,
        default=10_000,
        minimum=1,
        maximum=100_000,
        owner="sql",
        summary="Rows a generated statement may return.",
        ceiling=True,
    ),
    Setting(
        key="sql.timeout_ms",
        kind=INT,
        default=20_000,
        minimum=1_000,
        maximum=120_000,
        owner="sql",
        summary="Statement timeout in milliseconds.",
        ceiling=True,
    ),
    Setting(
        key="sql.max_concurrent",
        kind=INT,
        default=4,
        minimum=1,
        maximum=16,
        owner="sql",
        summary="Concurrent AI-issued statements per deployment.",
        ceiling=True,
    ),
    Setting(
        key="sql.allow_metadata_schemas",
        kind=BOOL,
        default=False,
        owner="sql",
        summary="Permit statements to read metadata schemas. Off by default.",
        ceiling=True,
    ),
    # --- usage, budgets and alerts (P10-F07) ---------------------------
    Setting(
        key="usage.cost_is_estimated",
        kind=BOOL,
        default=True,
        owner="usage",
        summary="Label every cost as an estimate rather than a billed amount.",
    ),
    Setting(
        key="usage.default_budget_warn_percent",
        kind=INT,
        default=80,
        minimum=1,
        maximum=100,
        owner="usage",
        summary="Warning threshold a new budget starts with.",
    ),
    Setting(
        key="usage.alert_email",
        kind=TEXT,
        default="",
        owner="usage",
        summary="Where budget and ledger alerts are sent. Empty disables them.",
        scopes=(GLOBAL,),
    ),
    Setting(
        key="usage.alert_webhook_url",
        kind=TEXT,
        default="",
        owner="usage",
        summary=(
            "https endpoint that receives a breach report. Empty disables webhook "
            "delivery."
        ),
        scopes=(GLOBAL,),
    ),
    Setting(
        key="sql.max_bytes",
        kind=INT,
        default=5_000_000,
        minimum=1_000,
        maximum=50_000_000,
        owner="sql",
        summary="Bytes one result preview may carry back to the browser.",
        ceiling=True,
    ),
    Setting(
        key="sql.max_plan_cost",
        kind=INT,
        default=1_000_000,
        minimum=0,
        maximum=1_000_000_000,
        owner="sql",
        summary="Planner cost ceiling for one statement; 0 disables the check.",
        ceiling=True,
    ),
    Setting(
        key="sql.extra_denied_functions",
        kind=TEXT,
        default="",
        owner="sql",
        summary=(
            "Comma-separated function names to refuse on top of the built-in "
            "denial list. Tightens only."
        ),
    ),
    # --- routing and transport (P10-F02/F04) ---------------------------
    Setting(
        key="provider.primary",
        kind=TEXT,
        default="",
        owner="providers",
        summary="Provider to try first, by name. Empty uses the stored priority.",
    ),
    Setting(
        key="provider.fallback",
        kind=TEXT,
        default="",
        owner="providers",
        summary="Provider to try after the primary. Empty uses the next by priority.",
    ),
    Setting(
        key="provider.request_timeout_seconds",
        kind=INT,
        default=30,
        minimum=5,
        maximum=120,
        owner="providers",
        summary="Socket timeout for one provider request.",
    ),
    Setting(
        key="generation.structured_retries",
        kind=INT,
        default=1,
        minimum=0,
        maximum=3,
        owner="generation",
        summary="Extra attempts when a provider returns malformed structured output.",
    ),
    Setting(
        key="repair.min_confidence_percent",
        kind=INT,
        default=60,
        minimum=0,
        maximum=100,
        owner="repair",
        summary="Confidence a pattern needs before promotion counts for review.",
    ),
    Setting(
        key="repair.require_review",
        kind=BOOL,
        default=False,
        owner="repair",
        summary="Hold a generated correction for operator review before reuse.",
    ),
    # --- feature flags (P10-F08) ---------------------------------------
    _feature("chat", "Conversational turns."),
    _feature("sql", "SQL candidate generation and execution."),
    _feature("dataset", "Automated dataset drafts."),
    _feature("chart", "Chart recommendation and drafts."),
    _feature("dashboard", "Dashboard planning and publishing."),
    _feature("history", "Conversation history."),
    _feature("self_healing", "Repair loop and pattern reuse."),
    _feature("usage", "Usage ledger, budgets and billing surfaces."),
    _feature("diagnostics", "Stored-statement diagnostics."),
)

BY_KEY: dict[str, Setting] = {setting.key: setting for setting in REGISTRY}


@dataclass(frozen=True, slots=True)
class StoredSetting:
    key: str
    scope: str
    value: Any
    version: int
    updated_by: str = ""
    updated_reason: str = ""
    updated_at: datetime | None = None

    def to_public_dict(self, setting: Setting | None = None) -> dict[str, Any]:
        body = {
            "key": self.key,
            "scope": self.scope,
            "value": self.value,
            "version": self.version,
            "updated_by": self.updated_by,
            "updated_reason": self.updated_reason,
            "updated_at": _isoformat(self.updated_at),
        }
        if setting is not None:
            body["restart_required"] = setting.restart_required
        return body


@dataclass(frozen=True, slots=True)
class Resolution:
    """The effective value of one key, and where it came from."""

    key: str
    value: Any
    source: str  # tenant | global | default
    version: int = 0

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "version": self.version,
        }


@dataclass(slots=True)
class _Cache:
    """A process-local view of the stored values, invalidated by version.

    Two workers must agree eventually, and "eventually" has to be bounded: the
    cache is keyed on the newest row version, so a write in one process is seen by
    the others on their next lookup rather than at restart (P10-F11).
    """

    values: dict[tuple[str, str], StoredSetting] = field(default_factory=dict)
    stamp: int = -1


_CACHE = _Cache()


def registry_payload() -> list[dict[str, Any]]:
    return [setting.to_public_dict() for setting in REGISTRY]


def _assert_storable(key: str) -> Setting:
    clean = str(key or "").strip()
    if not clean or len(clean) > MAX_KEY_CHARS:
        raise SettingError("invalid_key", "a setting key is 1-96 characters")
    if any(_SECRET_SEGMENT.match(segment) for segment in clean.split(".")):
        raise SettingError(
            "secret_not_allowed",
            "secrets are never stored here; provider keys live in AI Provider",
        )
    setting = BY_KEY.get(clean)
    if setting is None:
        raise SettingError("unknown_setting", f"{clean} is not a known setting")
    return setting


def resolve(key: str, stored: Iterable[StoredSetting]) -> Resolution:
    """Pick the tenant override, else the global value, else the default."""
    setting = _assert_storable(key)
    by_scope = {entry.scope: entry for entry in stored if entry.key == setting.key}
    tenant_row = by_scope.get(TENANT)
    if tenant_row is not None and TENANT in setting.scopes:
        return Resolution(setting.key, tenant_row.value, TENANT, tenant_row.version)
    global_row = by_scope.get(GLOBAL)
    if global_row is not None:
        return Resolution(setting.key, global_row.value, GLOBAL, global_row.version)
    return Resolution(setting.key, setting.default, "default", 0)


def load(
    settings: Settings, *, tenant_key: str = "default", refresh: bool = False
) -> dict[str, Resolution]:
    """Every setting resolved for one tenant, with the source of each value."""
    rows = _stored_rows(settings, tenant_key, refresh=refresh)
    return {setting.key: resolve(setting.key, rows) for setting in REGISTRY}


def effective(
    settings: Settings, *, tenant_key: str = "default", refresh: bool = False
) -> dict[str, Any]:
    """Just the values, which is what a caller usually wants."""
    return {
        key: resolution.value
        for key, resolution in load(settings, tenant_key=tenant_key, refresh=refresh).items()
    }


def feature_enabled(
    settings: Settings, flag: str, *, tenant_key: str = "default"
) -> bool:
    """Is a feature switched on? An unknown flag is off, not on by accident.

    Read through :func:`live`, so an unreadable table leaves the capability as the
    registry declares it rather than refusing work during an outage. The two
    failures are different: an unknown *name* is a programming error and stays off,
    an unreachable *database* is an incident and must not disable the product.
    """
    key = f"feature.{flag}"
    if key not in BY_KEY:
        return False
    resolved = live(settings, tenant_key=tenant_key).get(key)
    return bool(resolved.value) if resolved is not None else False


def require_feature(
    settings: Settings, flag: str, *, tenant_key: str = "default"
) -> None:
    """Refuse the work when the tenant has switched the capability off.

    Called where the work starts, so a route, a job and a scheduled task all
    honour the same switch without each of them remembering to look.
    """
    if not feature_enabled(settings, flag, tenant_key=tenant_key):
        raise FeatureDisabled(f"feature.{flag}")


def live(
    settings: Settings, *, tenant_key: str = "default"
) -> dict[str, Resolution]:
    """Resolve every setting for a request path, without ever failing it.

    Configuration is read on hot paths, so an unreadable table, a database outage
    or a tenant that has not been provisioned must not turn a chat turn into a
    500. The failure is logged and the documented defaults apply, which is the
    same state a fresh deployment is in (P10-F11).
    """
    try:
        return load(settings, tenant_key=tenant_key)
    except Exception:  # noqa: BLE001 - configuration may not fail a request
        logger.warning(
            "ai_settings_unavailable using_registry_defaults tenant=%s",
            tenant_key,
            exc_info=True,
        )
        return {
            setting.key: Resolution(setting.key, setting.default, "default", 0)
            for setting in REGISTRY
        }


def override(
    values: Mapping[str, Resolution], key: str
) -> Any | None:
    """The stored value for a key, or ``None`` when nothing has been written.

    An override wins over the value a module was configured with, and its absence
    leaves that configuration alone. That keeps a setting additive: a deployment
    tuned through its environment does not silently change behaviour because a
    new key exists.
    """
    resolution = values.get(key)
    if resolution is None or resolution.source == "default":
        return None
    return resolution.value


@dataclass(frozen=True, slots=True)
class SqlLimits:
    """The SQL safeguards in force, already held inside their ceilings."""

    max_rows: int
    timeout_ms: int
    max_concurrent: int
    allow_metadata_schemas: bool
    max_bytes: int = 5_000_000
    max_plan_cost: int = 0
    extra_denied_functions: tuple[str, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "max_rows": self.max_rows,
            "timeout_ms": self.timeout_ms,
            "max_concurrent": self.max_concurrent,
            "allow_metadata_schemas": self.allow_metadata_schemas,
            "max_bytes": self.max_bytes,
            "max_plan_cost": self.max_plan_cost,
            "extra_denied_functions": list(self.extra_denied_functions),
        }


def _clamped(values: Mapping[str, Resolution], key: str) -> int:
    """A stored value held inside its declared bounds, whatever the row says.

    ``ceiling=True`` means the maximum is a platform limit rather than a
    preference, so it is enforced here as well as at write time: a row edited
    directly in the database must still not raise a safeguard (P10-F06). The
    correction is logged, because silently ignoring the row an operator wrote is
    how a limit stops being believed.
    """
    setting = BY_KEY[key]
    resolution = values.get(key)
    value = setting.default if resolution is None else resolution.value
    try:
        number = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "ai_setting_unreadable key=%s default=%s", key, setting.default
        )
        return int(setting.default)
    allowed = int(clamp(key, number))
    if allowed != number:
        logger.warning(
            "ai_setting_above_ceiling key=%s stored=%s held=%s", key, number, allowed
        )
    return allowed


def clamp(key: str, value: Any) -> Any:
    """One value held inside its declared bounds, for any key."""
    setting = _assert_storable(key)
    if setting.kind != INT:
        return value
    try:
        number = int(value)
    except (TypeError, ValueError):
        return setting.default
    if setting.minimum is not None and number < int(setting.minimum):
        number = int(setting.minimum)
    if setting.maximum is not None and number > int(setting.maximum):
        number = int(setting.maximum)
    return number


def held_at_ceiling(
    settings: Settings, *, tenant_key: str = "default"
) -> list[dict[str, Any]]:
    """Stored values the platform is refusing to honour as written.

    Reported rather than silently corrected: an operator who wrote 999999 and sees
    100000 in force has learned something, while one who sees nothing has a bug to
    find later (P10-F09).
    """
    held: list[dict[str, Any]] = []
    for row in stored(settings, tenant_key=tenant_key):
        setting = BY_KEY.get(row.key)
        if setting is None or not setting.ceiling:
            continue
        allowed = clamp(row.key, row.value)
        if allowed != row.value:
            held.append(
                {"key": row.key, "stored": row.value, "in_force": allowed}
            )
    return held


def sql_limits(settings: Settings, *, tenant_key: str = "default") -> SqlLimits:
    """The row cap, timeout, concurrency and metadata rule in force.

    Every value is clamped to its declared ceiling, so the object a guard or an
    executor receives can be trusted with the numbers it already validated.
    """
    values = live(settings, tenant_key=tenant_key)
    metadata = values.get("sql.allow_metadata_schemas")
    extra = values.get("sql.extra_denied_functions")
    return SqlLimits(
        max_rows=_clamped(values, "sql.max_rows"),
        timeout_ms=_clamped(values, "sql.timeout_ms"),
        max_concurrent=_clamped(values, "sql.max_concurrent"),
        allow_metadata_schemas=bool(
            metadata.value if metadata is not None else False
        ),
        max_bytes=_clamped(values, "sql.max_bytes"),
        max_plan_cost=_clamped(values, "sql.max_plan_cost"),
        extra_denied_functions=tuple(
            sorted(
                {
                    part.strip().lower()
                    for part in str(extra.value if extra else "").split(",")
                    if part.strip()
                }
            )
        ),
    )


def stored(settings: Settings, *, tenant_key: str = "default") -> list[StoredSetting]:
    """The rows that exist, for the admin view (defaults are not rows)."""
    return _stored_rows(settings, tenant_key, refresh=True)


def routing(
    settings: Settings, *, tenant_key: str = "default"
) -> tuple[str, str]:
    """The named primary and fallback providers, as stored (may be empty)."""
    values = live(settings, tenant_key=tenant_key)
    primary = values.get("provider.primary")
    fallback = values.get("provider.fallback")
    return (
        str(primary.value or "").strip() if primary else "",
        str(fallback.value or "").strip() if fallback else "",
    )


#: The keys whose *combination* is validated, so an unrelated write is not made to
#: pay for a configuration read.
_COMBINATION_KEYS = frozenset(
    {
        "provider.primary",
        "provider.fallback",
        "provider.fallback_enabled",
        "provider.max_attempts_per_provider",
    }
)

#: Worst-case attempts one logical request may spend across providers. The point
#: of the cap is the multiply: attempts x providers is what a retry storm costs.
MAX_TOTAL_ATTEMPTS = 6


def _validate_batch(
    settings: Settings,
    tenant_key: str,
    scope: str,
    prepared: list[tuple[Setting, Any]],
) -> None:
    """Refuse a batch that is individually valid but jointly unsafe.

    Two rules, both about combinations a form can produce and neither about a
    single field: routing that names a provider that is not there, and retry
    settings whose product is a storm (P10-F02, P10-F04).
    """
    touched = {setting.key for setting, _ in prepared}
    if not touched & _COMBINATION_KEYS:
        # The rules below are about combinations; a batch that changes none of the
        # keys they constrain does not need to read the effective configuration.
        return
    merged = {
        key: resolution.value
        for key, resolution in live(settings, tenant_key=tenant_key).items()
    }
    for setting, value in prepared:
        merged[setting.key] = value

    primary = str(merged.get("provider.primary") or "").strip()
    fallback = str(merged.get("provider.fallback") or "").strip()
    if primary and fallback and primary == fallback:
        raise SettingError(
            "invalid_routing",
            "the fallback provider must differ from the primary provider",
        )
    for role, name in (("primary", primary), ("fallback", fallback)):
        if name and not _provider_available(settings, tenant_key, name):
            raise SettingError(
                "invalid_routing",
                f"the {role} provider {name} is not an enabled provider",
            )
    if fallback and not merged.get("provider.fallback_enabled", True):
        raise SettingError(
            "invalid_routing",
            "a fallback provider is named but fallback is switched off",
        )

    attempts = int(merged.get("provider.max_attempts_per_provider") or 1)
    providers = 2 if merged.get("provider.fallback_enabled") else 1
    total = max(1, attempts) * providers
    if total > MAX_TOTAL_ATTEMPTS:
        raise SettingError(
            "retry_storm",
            f"{attempts} attempts x {providers} providers is {total}, above the "
            f"platform limit of {MAX_TOTAL_ATTEMPTS}",
        )


def _provider_available(settings: Settings, tenant_key: str, name: str) -> bool:
    """Is this an enabled provider row for the tenant? Unknown counts as no."""
    try:
        with connect(settings) as connection, connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            cursor.execute(
                "SELECT 1 AS present FROM ai_provider "
                "WHERE tenant_id = %s AND enabled = TRUE AND name = %s",
                (tenant_id, name),
            )
            return cursor.fetchone() is not None
    except Exception:  # noqa: BLE001 - an unreadable catalog must not block a write
        logger.warning(
            "ai_setting_routing_unverified provider=%s tenant=%s", name, tenant_key
        )
        return False


def apply(
    settings: Settings,
    updates: Mapping[str, Any],
    *,
    tenant_key: str = "default",
    scope: str = GLOBAL,
    actor: str = "",
    reason: str = "",
    expected_version: int | None = None,
) -> list[Resolution]:
    """Validate and write a batch of settings, version-checked and audited.

    The whole batch is one transaction: a caller that submits five changes and has
    one rejected gets none of them, which is the only sane behaviour for a form.
    """
    clean_scope = (scope or GLOBAL).strip().lower()
    if clean_scope not in SCOPES:
        raise SettingError("invalid_scope", f"scope must be one of {', '.join(SCOPES)}")
    if not isinstance(updates, Mapping) or not updates:
        raise SettingError("empty_update", "no settings were supplied")

    prepared: list[tuple[Setting, Any]] = []
    for key, value in updates.items():
        setting = _assert_storable(str(key))
        if clean_scope not in setting.scopes:
            raise SettingError(
                "scope_not_allowed",
                f"{setting.key} may not be set at {clean_scope} scope",
            )
        prepared.append((setting, setting.coerce(value)))
    _validate_batch(settings, tenant_key, clean_scope, prepared)

    resolved: list[Resolution] = []
    with connect(settings) as connection, connection.cursor() as cursor:
        tenant_id = _tenant_id(cursor, tenant_key) if clean_scope == TENANT else None
        for setting, value in prepared:
            existing = _row(cursor, clean_scope, tenant_id, setting.key)
            current_version = int(existing["version"]) if existing else 0
            if expected_version is not None and current_version != expected_version:
                raise SettingError(
                    "version_conflict",
                    f"{setting.key} is at version {current_version}, "
                    f"not {expected_version}",
                )
            next_version = current_version + 1
            # Explicit update-or-insert rather than ON CONFLICT: the uniqueness
            # guard is two partial indexes, and inferring one of those from an
            # ON CONFLICT target is exactly the kind of dialect detail that breaks
            # on an upgrade. The read above already worked out which case it is.
            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO ai_setting (
                        tenant_id, scope, key, value, version, updated_by,
                        updated_reason
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        clean_scope,
                        setting.key,
                        _json(value),
                        next_version,
                        _text(actor, MAX_ACTOR_CHARS),
                        _text(reason, MAX_REASON_CHARS),
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE ai_setting
                    SET value = %s,
                        version = %s,
                        updated_by = %s,
                        updated_reason = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE scope = %s AND key = %s
                      AND tenant_id IS NOT DISTINCT FROM %s::uuid
                    """,
                    (
                        _json(value),
                        next_version,
                        _text(actor, MAX_ACTOR_CHARS),
                        _text(reason, MAX_REASON_CHARS),
                        clean_scope,
                        setting.key,
                        tenant_id,
                    ),
                )
            cursor.execute(
                """
                INSERT INTO ai_setting_audit (
                    tenant_id, scope, key, version, before_value, after_value,
                    actor, reason
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    clean_scope,
                    setting.key,
                    next_version,
                    _json(existing["value"]) if existing else None,
                    _json(value),
                    _text(actor, MAX_ACTOR_CHARS),
                    _text(reason, MAX_REASON_CHARS),
                ),
            )
            resolved.append(
                Resolution(setting.key, value, clean_scope, next_version)
            )
    _CACHE.stamp = -1
    logger.info(
        "settings_applied scope=%s tenant=%s keys=%d actor=%s",
        clean_scope,
        tenant_key if clean_scope == TENANT else "-",
        len(resolved),
        actor or "-",
    )
    return resolved


def history(
    settings: Settings, *, key: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """The append-only record behind "who changed this, and to what"."""
    bounded = max(1, min(int(limit), MAX_HISTORY))
    with connect(settings) as connection, connection.cursor() as cursor:
        if key:
            cursor.execute(
                """
                SELECT id, scope, key, version, before_value, after_value, actor,
                       reason, created_at
                FROM ai_setting_audit
                WHERE key = %s
                ORDER BY created_at DESC, version DESC
                LIMIT %s
                """,
                (_assert_storable(key).key, bounded),
            )
        else:
            cursor.execute(
                """
                SELECT id, scope, key, version, before_value, after_value, actor,
                       reason, created_at
                FROM ai_setting_audit
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (bounded,),
            )
        rows = cursor.fetchall()
    return [
        {
            "id": str(row["id"]),
            "scope": row["scope"],
            "key": row["key"],
            "version": int(row["version"]),
            "before": _load(row.get("before_value")),
            "after": _load(row.get("after_value")),
            "actor": row.get("actor") or "",
            "reason": row.get("reason") or "",
            "created_at": _isoformat(row.get("created_at")),
        }
        for row in rows
    ]


def rollback(
    settings: Settings,
    audit_id: str,
    *,
    tenant_key: str = "default",
    actor: str = "",
    reason: str = "",
) -> Resolution:
    """Reinstate the value a previous audit row replaced.

    A rollback is a new version, not a deletion: the history keeps both the change
    and the undo, so the record is never rewritten.
    """
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, tenant_id, scope, key, before_value
            FROM ai_setting_audit
            WHERE id = %s
            """,
            (str(audit_id),),
        )
        entry = cursor.fetchone()
    if entry is None:
        raise SettingError("audit_not_found", "that change is not in the history")
    if entry["before_value"] is None:
        raise SettingError(
            "nothing_to_restore", "that change created the setting; there is no earlier value"
        )
    resolution = apply(
        settings,
        {entry["key"]: _load(entry["before_value"])},
        tenant_key=tenant_key,
        scope=entry["scope"],
        actor=actor,
        reason=_text(f"rollback of {audit_id}: {reason}", MAX_REASON_CHARS),
    )[0]
    logger.info("settings_rollback audit=%s key=%s", audit_id, entry["key"])
    return resolution


def _stored_rows(
    settings: Settings, tenant_key: str, *, refresh: bool
) -> list[StoredSetting]:
    """Read the rows, reusing the cache unless a newer version has appeared."""
    with connect(settings) as connection, connection.cursor() as cursor:
        tenant_id = _tenant_id(cursor, tenant_key)
        cursor.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS stamp FROM ai_setting
            WHERE scope = 'global' OR tenant_id = %s
            """,
            (tenant_id,),
        )
        stamp = int((cursor.fetchone() or {}).get("stamp") or 0)
        if not refresh and stamp == _CACHE.stamp and _CACHE.values:
            return list(_CACHE.values.values())
        cursor.execute(
            """
            SELECT key, scope, value, version, updated_by, updated_reason, updated_at
            FROM ai_setting
            WHERE scope = 'global' OR tenant_id = %s
            """,
            (tenant_id,),
        )
        rows = cursor.fetchall()
    values = {
        (row["scope"], row["key"]): StoredSetting(
            key=row["key"],
            scope=row["scope"],
            value=_load(row["value"]),
            version=int(row["version"]),
            updated_by=row.get("updated_by") or "",
            updated_reason=row.get("updated_reason") or "",
            updated_at=row.get("updated_at"),
        )
        for row in rows
    }
    _CACHE.values = values
    _CACHE.stamp = stamp
    return list(values.values())


def _row(cursor: Any, scope: str, tenant_id: str | None, key: str) -> Any:
    cursor.execute(
        """
        SELECT version, value FROM ai_setting
        WHERE scope = %s AND key = %s
          AND (tenant_id = %s OR (%s::uuid IS NULL AND tenant_id IS NULL))
        """,
        (scope, key, tenant_id, tenant_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    # The driver may hand back the raw JSON text rather than a parsed value, so it
    # is parsed here: writing ``_json(text)`` straight into the audit row would
    # record a JSON string containing JSON instead of the value itself.
    return {"version": row["version"], "value": _load(row["value"])}


def _tenant_id(cursor: Any, tenant_key: str) -> str:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    if row is None:
        raise SettingError("unknown_tenant", "tenant is not provisioned")
    return str(row["id"])


def _json(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load(value: Any) -> Any:
    """Decode one stored value without mistaking text for broken JSON.

    A driver that hands back the JSON *text* must be decoded, but a ``text``
    setting is itself a string: parsing ``ops@example.org`` as JSON fails, and
    returning ``None`` on that failure silently turned the only configured
    recipient into "nobody". Only a string that decodes to something else, or
    that was quoted as a JSON string, is treated as JSON text.
    """
    import json

    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return value
    if isinstance(decoded, str) and value[:1] != '"':
        return value
    return decoded


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def keys() -> Sequence[str]:
    """Every settable key, for documentation and tests."""
    return tuple(setting.key for setting in REGISTRY)
