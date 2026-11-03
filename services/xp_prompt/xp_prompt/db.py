"""PostgreSQL access helpers for the KOT5 XP Prompt control plane."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from xp_prompt.config import Settings


@contextmanager
def connect(settings: Settings) -> Iterator["object"]:
    """Yield a committed-or-rolled-back psycopg2 connection.

    The connection is opened per unit of work so a recycled worker process never
    holds a stale socket; the dedicated XP Prompt database is local to the
    stack, so the extra handshake is cheap.
    """
    import psycopg2
    from psycopg2.extras import RealDictCursor

    connection = psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        dbname=settings.postgres_database,
        connect_timeout=settings.dependency_timeout_seconds,
        cursor_factory=RealDictCursor,
    )
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
