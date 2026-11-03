"""Admin-managed provider configuration stored in the XP Prompt database.

Provider API keys entered through the *AI Provider* settings page live in
``ai_provider.api_key_ciphertext`` sealed by :class:`xp_prompt.security.SecretBox`.
The plaintext key never leaves this module except as provider credentials handed
straight to an adapter, and never appears in an HTTP response, log record, or
``repr``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from xp_prompt.config import Settings
from xp_prompt.db import connect
from xp_prompt.providers.anthropic import AnthropicProvider
from xp_prompt.providers.deepseek import DeepSeekProvider
from xp_prompt.providers.gemini import GeminiProvider
from xp_prompt.providers.openai_compatible import (
    AzureOpenAIProvider,
    OpenAICompatibleProvider,
)
from xp_prompt.providers.router import ProviderRouter
from xp_prompt.providers.transport import validate_provider_endpoint
from xp_prompt.providers.types import (
    ProviderCredentials,
    ProviderRequest,
    ProviderResponse,
)
from xp_prompt.security import SecretBox, key_fingerprint, key_last_four

DEFAULT_TENANT_KEY = "default"
MAX_NAME_LENGTH = 128
MAX_MODEL_LENGTH = 128
MAX_TIMEOUT_SECONDS = 600
MAX_TOKENS_CEILING = 131_072
MIN_PRIORITY = -1000
MAX_PRIORITY = 1000

_NAME_RE = re.compile(r"^[\w][\w .:/@()+-]*$")


@dataclass(frozen=True, slots=True)
class ProviderTypeSpec:
    """Non-secret metadata describing one supported provider type."""

    key: str
    label: str
    default_base_url: str
    default_model: str
    requires_base_url: bool
    supports_api_version: bool = False
    model_placeholder: str = ""


PROVIDER_TYPES: dict[str, ProviderTypeSpec] = {
    "openai_compatible": ProviderTypeSpec(
        key="openai_compatible",
        label="OpenAI-compatible",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        requires_base_url=False,
        model_placeholder="gpt-4o-mini",
    ),
    "anthropic": ProviderTypeSpec(
        key="anthropic",
        label="Anthropic",
        default_base_url="https://api.anthropic.com/v1",
        default_model="claude-3-5-sonnet-latest",
        requires_base_url=False,
        model_placeholder="claude-3-5-sonnet-latest",
    ),
    "gemini": ProviderTypeSpec(
        key="gemini",
        label="Google Gemini",
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        default_model="gemini-2.0-flash",
        requires_base_url=False,
        model_placeholder="gemini-2.0-flash",
    ),
    "azure_openai": ProviderTypeSpec(
        key="azure_openai",
        label="Azure OpenAI",
        default_base_url="",
        default_model="gpt-4o-mini",
        requires_base_url=True,
        supports_api_version=True,
        model_placeholder="deployment name",
    ),
    "deepseek": ProviderTypeSpec(
        key="deepseek",
        label="DeepSeek",
        default_base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
        requires_base_url=False,
        model_placeholder="deepseek-chat",
    ),
}


class ProviderConfigError(ValueError):
    """A rejected provider configuration (safe to return to the admin UI)."""


@dataclass(frozen=True, slots=True)
class ProviderInput:
    """Validated, non-secret provider settings."""

    name: str
    provider_type: str
    base_url: str
    default_model: str
    config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    is_default: bool = False
    priority: int = 0

    @property
    def temperature(self) -> float:
        return float(self.config.get("temperature", 0.7))

    @property
    def max_tokens(self) -> int:
        return int(self.config.get("max_tokens", 1024))

    @property
    def timeout_seconds(self) -> int:
        return int(self.config.get("timeout_seconds", 45))

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "ProviderInput":
        """Validate an untrusted JSON payload from the settings page."""
        if not isinstance(payload, dict):
            raise ProviderConfigError("Provider payload must be a JSON object")

        name = _clean_text(payload.get("name"), "Display name", MAX_NAME_LENGTH)
        if not _NAME_RE.match(name):
            raise ProviderConfigError(
                "Display name may only contain letters, digits, spaces and "
                "._:/@()+-"
            )

        provider_type = _clean_text(
            payload.get("provider_type"), "Provider type", 32
        ).lower()
        spec = PROVIDER_TYPES.get(provider_type)
        if spec is None:
            raise ProviderConfigError(
                "Provider type must be one of: "
                + ", ".join(sorted(PROVIDER_TYPES))
            )

        raw_base_url = str(payload.get("base_url") or "").strip()
        if not raw_base_url:
            if spec.requires_base_url:
                raise ProviderConfigError("Base URL is required for this provider type")
            raw_base_url = spec.default_base_url
        try:
            base_url = validate_provider_endpoint(
                raw_base_url, spec.label, allow_local_http=True
            )
        except ValueError as exc:
            raise ProviderConfigError(str(exc)) from exc

        default_model = _clean_text(
            payload.get("default_model") or spec.default_model,
            "Default model",
            MAX_MODEL_LENGTH,
        )

        raw_config = payload.get("config") or {}
        if not isinstance(raw_config, dict):
            raise ProviderConfigError("Advanced config must be a JSON object")

        temperature = _bounded_number(
            raw_config.get("temperature", 0.7),
            "Temperature",
            minimum=0.0,
            maximum=2.0,
        )
        max_tokens = int(
            _bounded_number(
                raw_config.get("max_tokens", 1024),
                "Max tokens",
                minimum=1,
                maximum=MAX_TOKENS_CEILING,
            )
        )
        timeout_seconds = int(
            _bounded_number(
                raw_config.get("timeout_seconds", 45),
                "Timeout (s)",
                minimum=1,
                maximum=MAX_TIMEOUT_SECONDS,
            )
        )

        config: dict[str, Any] = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout_seconds": timeout_seconds,
        }
        if spec.supports_api_version:
            api_version = str(raw_config.get("api_version") or "").strip()
            if api_version:
                config["api_version"] = _clean_text(api_version, "API version", 32)

        priority = int(
            _bounded_number(
                payload.get("priority", 0),
                "Priority",
                minimum=MIN_PRIORITY,
                maximum=MAX_PRIORITY,
            )
        )

        return cls(
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            default_model=default_model,
            config=config,
            enabled=bool(payload.get("enabled", True)),
            is_default=bool(payload.get("is_default", False)),
            priority=priority,
        )


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    """A stored provider configuration without its key material."""

    id: str
    tenant_key: str
    name: str
    provider_type: str
    base_url: str
    default_model: str
    config: dict[str, Any]
    enabled: bool
    is_default: bool
    priority: int
    api_key_fingerprint: str
    api_key_last_four: str
    last_tested_at: datetime | None
    last_test_status: str | None
    last_test_error: str | None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def label(self) -> str:
        spec = PROVIDER_TYPES.get(self.provider_type)
        return spec.label if spec else self.provider_type

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize for the admin API — never includes the API key."""
        return {
            "id": self.id,
            "tenant": self.tenant_key,
            "name": self.name,
            "provider_type": self.provider_type,
            "provider_label": self.label,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "config": dict(self.config),
            "enabled": self.enabled,
            "is_default": self.is_default,
            "priority": self.priority,
            "api_key": {
                "configured": bool(self.api_key_fingerprint),
                "fingerprint": self.api_key_fingerprint,
                "last_four": self.api_key_last_four,
            },
            "last_test": {
                "status": self.last_test_status,
                "error": self.last_test_error,
                "at": _isoformat(self.last_tested_at),
            },
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
        }

    def to_router_dict(self) -> dict[str, Any]:
        """Serialize the routing-relevant subset for provider selection."""
        return {
            "id": self.id,
            "name": self.name,
            "provider_type": self.provider_type,
            "default_model": self.default_model,
            "enabled": self.enabled,
            "is_default": self.is_default,
            "priority": self.priority,
        }


def provider_types_payload() -> list[dict[str, Any]]:
    """Catalog consumed by the settings page to prefill defaults."""
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "default_base_url": spec.default_base_url,
            "default_model": spec.default_model,
            "requires_base_url": spec.requires_base_url,
            "supports_api_version": spec.supports_api_version,
            "model_placeholder": spec.model_placeholder,
        }
        for spec in PROVIDER_TYPES.values()
    ]


def is_provider_id(value: str) -> bool:
    """Is this a syntactically valid provider identifier?"""
    import uuid

    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def build_provider(
    record: ProviderRecord,
    api_key: str,
    *,
    transport: Any | None = None,
) -> Any:
    """Instantiate the adapter that serves one stored provider record.

    ``allow_local_http`` is on because a stored record was already validated by
    :meth:`ProviderInput.parse`: plain HTTP is only reachable for loopback,
    private-range, and single-label hosts (a local Ollama/vLLM server), never
    for a public endpoint.
    """
    credentials = ProviderCredentials(api_key=api_key)
    timeout_seconds = int(record.config.get("timeout_seconds", 45))
    common = {
        "credentials": credentials,
        "model": record.default_model,
        "endpoint": record.base_url,
        "timeout_seconds": timeout_seconds,
        "allow_local_http": True,
    }
    if transport is not None:
        common["transport"] = transport

    if record.provider_type == "gemini":
        return GeminiProvider(**common)
    if record.provider_type == "deepseek":
        return DeepSeekProvider(**common)
    if record.provider_type == "anthropic":
        return AnthropicProvider(**common)
    if record.provider_type == "azure_openai":
        return AzureOpenAIProvider(
            **common,
            api_version=str(record.config.get("api_version") or ""),
        )
    if record.provider_type == "openai_compatible":
        return OpenAICompatibleProvider(**common)
    raise ProviderConfigError(f"Unsupported provider type {record.provider_type}")


def list_providers(
    settings: Settings,
    *,
    tenant_key: str = DEFAULT_TENANT_KEY,
) -> list[ProviderRecord]:
    """List stored providers for a tenant, default first then by priority."""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.*, t.key AS tenant_key
                FROM ai_provider p
                JOIN ai_tenant t ON t.id = p.tenant_id
                WHERE t.key = %s
                ORDER BY p.is_default DESC, p.priority ASC, p.name ASC
                """,
                (tenant_key,),
            )
            return [_record_from_row(row) for row in cursor.fetchall()]


def get_provider(settings: Settings, provider_id: str) -> ProviderRecord | None:
    """Load one provider record by id."""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.*, t.key AS tenant_key
                FROM ai_provider p
                JOIN ai_tenant t ON t.id = p.tenant_id
                WHERE p.id = %s
                """,
                (provider_id,),
            )
            row = cursor.fetchone()
    return _record_from_row(row) if row else None


def create_provider(
    settings: Settings,
    payload: ProviderInput,
    api_key: str,
) -> ProviderRecord:
    """Create a provider with a sealed API key."""
    box = _secret_box(settings)
    ciphertext = box.encrypt(api_key)
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, DEFAULT_TENANT_KEY)
            if payload.is_default:
                _clear_default(cursor, tenant_id)
            cursor.execute(
                """
                INSERT INTO ai_provider (
                    tenant_id, name, provider_type, base_url,
                    api_key_ciphertext, api_key_fingerprint, api_key_last_four,
                    default_model, config, enabled, is_default, priority
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    tenant_id,
                    payload.name,
                    payload.provider_type,
                    payload.base_url,
                    ciphertext,
                    key_fingerprint(api_key),
                    key_last_four(api_key),
                    payload.default_model,
                    _json(payload.config),
                    payload.enabled,
                    payload.is_default,
                    payload.priority,
                ),
            )
            provider_id = str(cursor.fetchone()["id"])
    record = get_provider(settings, provider_id)
    if record is None:  # pragma: no cover - defensive
        raise RuntimeError("provider was created but cannot be read back")
    return record


def update_provider(
    settings: Settings,
    provider_id: str,
    payload: ProviderInput,
    api_key: str | None = None,
) -> ProviderRecord:
    """Update a provider; a blank ``api_key`` keeps the stored key."""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tenant_id FROM ai_provider WHERE id = %s",
                (provider_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise LookupError("provider not found")
            tenant_id = row["tenant_id"]
            if payload.is_default:
                _clear_default(cursor, tenant_id, exclude_id=provider_id)

            assignments = [
                "name = %s",
                "provider_type = %s",
                "base_url = %s",
                "default_model = %s",
                "config = %s",
                "enabled = %s",
                "is_default = %s",
                "priority = %s",
                "updated_at = CURRENT_TIMESTAMP",
            ]
            values: list[Any] = [
                payload.name,
                payload.provider_type,
                payload.base_url,
                payload.default_model,
                _json(payload.config),
                payload.enabled,
                payload.is_default,
                payload.priority,
            ]
            if api_key:
                box = _secret_box(settings)
                assignments.extend(
                    [
                        "api_key_ciphertext = %s",
                        "api_key_fingerprint = %s",
                        "api_key_last_four = %s",
                    ]
                )
                values.extend(
                    [
                        box.encrypt(api_key),
                        key_fingerprint(api_key),
                        key_last_four(api_key),
                    ]
                )
            values.append(provider_id)
            cursor.execute(
                f"UPDATE ai_provider SET {', '.join(assignments)} WHERE id = %s",
                tuple(values),
            )
    record = get_provider(settings, provider_id)
    if record is None:  # pragma: no cover - defensive
        raise LookupError("provider not found")
    return record


def delete_provider(settings: Settings, provider_id: str) -> bool:
    """Delete a provider record; returns ``False`` when it never existed."""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM ai_provider WHERE id = %s", (provider_id,))
            return cursor.rowcount > 0


def record_test_result(
    settings: Settings,
    provider_id: str,
    *,
    status: str,
    error: str | None = None,
) -> None:
    """Persist the outcome of a connection test (redacted message only)."""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ai_provider
                SET last_tested_at = CURRENT_TIMESTAMP,
                    last_test_status = %s,
                    last_test_error = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (status, (error or "")[:255] or None, provider_id),
            )


def decrypt_api_key(settings: Settings, provider_id: str) -> str:
    """Read and decrypt the stored key for one provider."""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT api_key_ciphertext FROM ai_provider WHERE id = %s",
                (provider_id,),
            )
            row = cursor.fetchone()
    if row is None:
        raise LookupError("provider not found")
    return _secret_box(settings).decrypt(row["api_key_ciphertext"])


def routing_chain(
    settings: Settings,
    *,
    tenant_key: str = DEFAULT_TENANT_KEY,
) -> list[ProviderRecord]:
    """Enabled providers ordered for primary/fallback routing."""
    return [record for record in list_providers(settings, tenant_key=tenant_key)
            if record.enabled]


def env_fallback_status(settings: Settings) -> dict[str, Any]:
    """Report the legacy ``.env`` provider keys so the UI can explain them."""
    return {
        "gemini_configured": bool(settings.gemini_api_key and settings.gemini_model),
        "deepseek_configured": bool(settings.deepseek_api_key),
        "note": (
            "Provider keys saved here take precedence. The .env keys are only "
            "used as a fallback while no enabled provider exists."
        ),
    }


@dataclass(frozen=True, slots=True)
class NamedProvider:
    """Adapter plus the admin-chosen display name used in attempt summaries."""

    name: str
    model: str
    adapter: Any = field(repr=False)

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        return self.adapter.generate(request)


def resolve_router(
    settings: Settings,
    *,
    tenant_key: str = DEFAULT_TENANT_KEY,
) -> ProviderRouter:
    """Build the primary/fallback chain every AI operation should use.

    Providers saved in *Settings -> AI Provider* always win; the legacy
    ``.env`` keys are only consulted while the settings page holds no usable
    provider, so an upgraded deployment keeps working until an admin saves one.

    A provider whose sealed key can no longer be decrypted (root secret
    rotation) is skipped instead of failing every AI call, and the admin sees
    the broken row in the settings page.
    """
    providers: list[NamedProvider] = []
    for record in _enabled_records(settings, tenant_key):
        try:
            api_key = decrypt_api_key(settings, record.id)
        except (LookupError, RuntimeError):
            continue
        providers.append(
            NamedProvider(
                name=record.name,
                model=record.default_model,
                adapter=build_provider(record, api_key),
            )
        )
    if not providers:
        providers = _env_fallback_providers(settings)
    if not providers:
        raise ProviderConfigError(
            "No AI provider is configured. Add one in Settings -> AI Provider."
        )

    fallback = next(
        (item for item in providers[1:] if item.name != providers[0].name), None
    )
    return ProviderRouter(primary=providers[0], fallback=fallback)


def _enabled_records(
    settings: Settings,
    tenant_key: str,
) -> list[ProviderRecord]:
    """Enabled provider records, tolerating an unmigrated database."""
    try:
        records = list_providers(settings, tenant_key=tenant_key)
    except Exception:  # pragma: no cover - database failure path
        return []
    return [record for record in records if record.enabled]


def _env_fallback_providers(settings: Settings) -> list[NamedProvider]:
    """Legacy ``.env`` providers, clearly labelled in attempt summaries."""
    providers: list[NamedProvider] = []
    if settings.gemini_api_key and settings.gemini_model:
        gemini = GeminiProvider(
            credentials=ProviderCredentials(api_key=settings.gemini_api_key),
            model=settings.gemini_model,
            endpoint=settings.gemini_endpoint,
        )
        providers.append(
            NamedProvider(
                name=f"{gemini.name} (.env)",
                model=gemini.model,
                adapter=gemini,
            )
        )
    if settings.deepseek_api_key:
        deepseek = DeepSeekProvider(
            credentials=ProviderCredentials(api_key=settings.deepseek_api_key),
            model=settings.deepseek_model,
            endpoint=settings.deepseek_endpoint,
        )
        providers.append(
            NamedProvider(
                name=f"{deepseek.name} (.env)",
                model=deepseek.model,
                adapter=deepseek,
            )
        )
    return providers


def _secret_box(settings: Settings) -> SecretBox:
    return SecretBox.from_root_secret(settings.service_secret)


def _clean_text(value: object, label: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProviderConfigError(f"{label} is required")
    if len(text) > maximum:
        raise ProviderConfigError(f"{label} must be at most {maximum} characters")
    return text


def _bounded_number(
    value: object,
    label: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProviderConfigError(f"{label} must be a number") from exc
    if not minimum <= number <= maximum:
        raise ProviderConfigError(f"{label} must be between {minimum} and {maximum}")
    return number


def _tenant_id(cursor: Any, tenant_key: str) -> str:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    if row is None:
        raise ProviderConfigError(f"Unknown tenant {tenant_key}")
    return str(row["id"])


def _clear_default(cursor: Any, tenant_id: str, *, exclude_id: str | None = None) -> None:
    if exclude_id is None:
        cursor.execute(
            "UPDATE ai_provider SET is_default = false WHERE tenant_id = %s "
            "AND is_default",
            (tenant_id,),
        )
        return
    cursor.execute(
        "UPDATE ai_provider SET is_default = false WHERE tenant_id = %s "
        "AND is_default AND id <> %s",
        (tenant_id, exclude_id),
    )


def _record_from_row(row: dict[str, Any]) -> ProviderRecord:
    config = row.get("config") or {}
    return ProviderRecord(
        id=str(row["id"]),
        tenant_key=str(row.get("tenant_key") or DEFAULT_TENANT_KEY),
        name=str(row["name"]),
        provider_type=str(row["provider_type"]),
        base_url=str(row["base_url"]),
        default_model=str(row["default_model"]),
        config=dict(config) if isinstance(config, dict) else {},
        enabled=bool(row["enabled"]),
        is_default=bool(row["is_default"]),
        priority=int(row["priority"]),
        api_key_fingerprint=str(row.get("api_key_fingerprint") or ""),
        api_key_last_four=str(row.get("api_key_last_four") or ""),
        last_tested_at=row.get("last_tested_at"),
        last_test_status=row.get("last_test_status"),
        last_test_error=row.get("last_test_error"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def validate_base_url_for_type(provider_type: str, base_url: str) -> str:
    """Validate a base URL against the provider type's endpoint rules."""
    spec = PROVIDER_TYPES.get(provider_type)
    label = spec.label if spec else provider_type
    try:
        return validate_provider_endpoint(base_url, label)
    except ValueError as exc:
        raise ProviderConfigError(str(exc)) from exc
