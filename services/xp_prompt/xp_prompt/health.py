"""Dependency health checks for the KOT5 XP Prompt runtime."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xp_prompt.config import Settings


@dataclass(frozen=True, slots=True)
class CheckResult:
    """A redacted dependency-check result."""

    name: str
    status: str
    duration_ms: int
    error_class: str | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _run_check(name: str, check: Callable[[], None]) -> CheckResult:
    started = time.monotonic()
    try:
        check()
    except Exception as exc:  # Health checks must convert failures to status.
        return CheckResult(
            name=name,
            status="unavailable",
            duration_ms=round((time.monotonic() - started) * 1000),
            error_class=type(exc).__name__,
        )
    return CheckResult(
        name=name,
        status="ready",
        duration_ms=round((time.monotonic() - started) * 1000),
    )


def check_postgres(settings: "Settings") -> CheckResult:
    """Check the dedicated XP Prompt PostgreSQL database."""

    def probe() -> None:
        import psycopg2

        connection = psycopg2.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            dbname=settings.postgres_database,
            connect_timeout=settings.dependency_timeout_seconds,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    raise RuntimeError("unexpected PostgreSQL readiness result")
        finally:
            connection.close()

    return _run_check("postgres", probe)


def check_redis(settings: "Settings") -> CheckResult:
    """Check the KOT5 Redis namespace used by XP Prompt."""

    def probe() -> None:
        import redis

        client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password,
            db=settings.redis_database,
            socket_connect_timeout=settings.dependency_timeout_seconds,
            socket_timeout=settings.dependency_timeout_seconds,
            decode_responses=True,
        )
        if client.ping() is not True:
            raise RuntimeError("unexpected Redis readiness result")

    return _run_check("redis", probe)


def check_qdrant(settings: "Settings") -> CheckResult:
    """Check the KOT5-local Qdrant readiness endpoint."""

    def probe() -> None:
        request = urllib.request.Request(
            f"{settings.qdrant_url}/readyz",
            method="GET",
            headers={"Accept": "text/plain"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=settings.dependency_timeout_seconds
            ) as response:
                if response.status != 200:
                    raise RuntimeError("unexpected Qdrant readiness status")
        except urllib.error.HTTPError as exc:
            raise RuntimeError("Qdrant readiness request failed") from exc

    return _run_check("qdrant", probe)


def readiness(
    settings: "Settings",
    checks: Mapping[str, Callable[["Settings"], CheckResult]] | None = None,
) -> tuple[bool, dict[str, dict[str, object]]]:
    """Run dependency checks and return aggregate readiness plus safe details."""
    selected = checks or {
        "postgres": check_postgres,
        "redis": check_redis,
        "qdrant": check_qdrant,
    }
    results = {name: check(settings) for name, check in selected.items()}
    return all(result.ready for result in results.values()), {
        name: result.to_dict() for name, result in results.items()
    }

