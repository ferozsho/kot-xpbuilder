"""XP Prompt database schema version shared by migrations and readiness.

Readiness compares ``alembic_version`` to this constant, so it must name the
newest applied migration. Forgetting to bump it turns the API container
``unhealthy`` even though the database is correct.
"""

SCHEMA_REVISION = "20261103_12"

