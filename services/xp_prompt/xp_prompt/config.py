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
        }

