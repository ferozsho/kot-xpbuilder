"""Environment-backed configuration for the KOT5 XP Prompt services."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set through the deployment .env file")
    return value


def _integer(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _http_url(env: Mapping[str, str], name: str) -> str:
    value = _required(env, name).rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError(f"{name} must not contain credentials, query, or fragment")
    return value


def _optional(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated XP Prompt process settings.

    Secrets are retained in-memory only and are never included in health
    responses or repr/log output.
    """

    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str = field(repr=False)
    postgres_database: str
    redis_host: str
    redis_port: int
    redis_password: str = field(repr=False)
    redis_database: int
    qdrant_url: str
    dependency_timeout_seconds: int
    service_secret: str | None = field(default=None, repr=False)
    gemini_api_key: str | None = field(default=None, repr=False)
    gemini_model: str | None = None
    gemini_endpoint: str = "https://generativelanguage.googleapis.com/v1beta"
    deepseek_api_key: str | None = field(default=None, repr=False)
    deepseek_model: str = "deepseek-flash"
    deepseek_endpoint: str = "https://api.deepseek.com"
    #: Cost guard (P4-F08). Defaults are deliberately generous enough for a
    #: reporting question and small enough that a runaway join is refused.
    max_estimated_rows: int = 5_000_000
    max_estimated_cost: float = 1_000_000.0
    max_plan_nodes: int = 400
    max_runtime_ms: int = 20_000
    require_plan: bool = False
    #: Whether the audit trail may return a stored statement. Off by default: the
    #: SQL is sealed at rest precisely so it is not casually readable.
    allow_sql_diagnostics: bool = False
    #: Candidate cache (P4-F11). Results are never cached — only the SQL a question
    #: produced, keyed on the slice fingerprint and policy.
    candidate_cache_enabled: bool = True
    candidate_cache_ttl_seconds: int = 1_800
    #: Repair budget (P5-F06). One failure may be repaired at most twice, within
    #: thirty seconds and four thousand tokens, unless the deployment says otherwise.
    repair_max_attempts: int = 2
    repair_max_elapsed_ms: int = 30_000
    repair_max_tokens: int = 4_000
    #: Pattern promotion (P5-F08). A correction becomes reusable knowledge only after
    #: this many *independent* successful validations. Zero disables promotion
    #: entirely, so nothing is ever reused automatically.
    pattern_evidence_threshold: int = 2
    pattern_auto_promote: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        return cls(
            postgres_host=_required(source, "XP_PROMPT_POSTGRES_HOST"),
            postgres_port=_integer(
                source,
                "XP_PROMPT_POSTGRES_PORT",
                5432,
                minimum=1,
                maximum=65535,
            ),
            postgres_user=_required(source, "XP_PROMPT_POSTGRES_USER"),
            postgres_password=_required(source, "XP_PROMPT_POSTGRES_PASSWORD"),
            postgres_database=_required(source, "XP_PROMPT_POSTGRES_DB"),
            redis_host=_required(source, "XP_PROMPT_REDIS_HOST"),
            redis_port=_integer(
                source,
                "XP_PROMPT_REDIS_PORT",
                6379,
                minimum=1,
                maximum=65535,
            ),
            redis_password=_required(source, "XP_PROMPT_REDIS_PASSWORD"),
            redis_database=_integer(
                source,
                "XP_PROMPT_REDIS_DB",
                2,
                minimum=0,
                maximum=14,
            ),
            qdrant_url=_http_url(source, "XP_PROMPT_QDRANT_URL"),
            dependency_timeout_seconds=_integer(
                source,
                "XP_PROMPT_DEPENDENCY_TIMEOUT_SECONDS",
                3,
                minimum=1,
                maximum=30,
            ),
            service_secret=(
                _optional(source, "XP_PROMPT_SERVICE_SECRET")
                or _optional(source, "SUPERSET_SECRET_KEY")
            ),
            gemini_api_key=_optional(source, "GOOGLE_GEMINI_API_KEY"),
            gemini_model=_optional(source, "XP_PROMPT_GEMINI_MODEL"),
            gemini_endpoint=(
                _http_url(source, "XP_PROMPT_GEMINI_ENDPOINT")
                if _optional(source, "XP_PROMPT_GEMINI_ENDPOINT")
                else "https://generativelanguage.googleapis.com/v1beta"
            ),
            deepseek_api_key=_optional(source, "DEEPSEEK_API_KEY"),
            deepseek_model=(
                _optional(source, "XP_PROMPT_DEEPSEEK_MODEL") or "deepseek-flash"
            ),
            deepseek_endpoint=(
                _http_url(source, "XP_PROMPT_DEEPSEEK_ENDPOINT")
                if _optional(source, "XP_PROMPT_DEEPSEEK_ENDPOINT")
                else "https://api.deepseek.com"
            ),
            max_estimated_rows=_integer(
                source,
                "XP_PROMPT_MAX_ESTIMATED_ROWS",
                5_000_000,
                minimum=1_000,
                maximum=1_000_000_000,
            ),
            max_estimated_cost=_float(
                source, "XP_PROMPT_MAX_ESTIMATED_COST", 1_000_000.0
            ),
            max_plan_nodes=_integer(
                source,
                "XP_PROMPT_MAX_PLAN_NODES",
                400,
                minimum=10,
                maximum=10_000,
            ),
            max_runtime_ms=_integer(
                source,
                "XP_PROMPT_MAX_RUNTIME_MS",
                20_000,
                minimum=1_000,
                maximum=600_000,
            ),
            require_plan=_boolean(source, "XP_PROMPT_REQUIRE_PLAN", False),
            allow_sql_diagnostics=_boolean(
                source, "XP_PROMPT_ALLOW_SQL_DIAGNOSTICS", False
            ),
            candidate_cache_enabled=_boolean(
                source, "XP_PROMPT_CANDIDATE_CACHE", True
            ),
            candidate_cache_ttl_seconds=_integer(
                source,
                "XP_PROMPT_CANDIDATE_CACHE_TTL_SECONDS",
                1_800,
                minimum=0,
                maximum=86_400,
            ),
            repair_max_attempts=_integer(
                source, "XP_PROMPT_REPAIR_MAX_ATTEMPTS", 2, minimum=0, maximum=5
            ),
            repair_max_elapsed_ms=_integer(
                source,
                "XP_PROMPT_REPAIR_MAX_ELAPSED_MS",
                30_000,
                minimum=0,
                maximum=600_000,
            ),
            repair_max_tokens=_integer(
                source, "XP_PROMPT_REPAIR_MAX_TOKENS", 4_000, minimum=0, maximum=200_000
            ),
            pattern_evidence_threshold=_integer(
                source,
                "XP_PROMPT_PATTERN_EVIDENCE_THRESHOLD",
                2,
                minimum=0,
                maximum=10,
            ),
            pattern_auto_promote=_boolean(
                source, "XP_PROMPT_PATTERN_AUTO_PROMOTE", True
            ),
        )

    def safe_summary(self) -> dict[str, object]:
        """Return non-secret settings suitable for diagnostics."""
        return {
            "postgres_host": self.postgres_host,
            "postgres_port": self.postgres_port,
            "postgres_database": self.postgres_database,
            "redis_host": self.redis_host,
            "redis_port": self.redis_port,
            "redis_database": self.redis_database,
            "qdrant_url": self.qdrant_url,
            "dependency_timeout_seconds": self.dependency_timeout_seconds,
            "provider_admin_ready": bool(self.service_secret),
            "gemini_configured": bool(self.gemini_api_key and self.gemini_model),
            "gemini_model": self.gemini_model,
            "gemini_endpoint": self.gemini_endpoint,
            "deepseek_configured": bool(self.deepseek_api_key),
            "deepseek_model": self.deepseek_model,
            "deepseek_endpoint": self.deepseek_endpoint,
        }

