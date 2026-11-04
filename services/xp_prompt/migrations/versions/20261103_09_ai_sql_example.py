"""Add the validated-example library used as few-shot precedent (P4-F03).

One row per (tenant, dialect, statement): the question that produced it, the sealed
statement, and the schema fingerprint it was built against. The unique constraint on
``(tenant_id, dialect, sql_sha256)`` makes a repeated success count as a *use* rather
than a new row, so the library stays small and its order stays meaningful.

The tenant column is not decoration: every read filters on it, which is what makes
"no cross-tenant knowledge leakage" structural rather than a convention.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261103_09"
down_revision: str | None = "20261103_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "ai_sql_example",
        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("ai_tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dialect", sa.String(32), nullable=False),
        sa.Column("question", sa.String(500), nullable=False),
        sa.Column("sql_ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("sql_sha256", sa.String(64), nullable=False),
        sa.Column(
            "schema_fingerprint", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column(
            "tables",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("uses", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_unique_constraint(
        "uq_ai_sql_example_tenant_dialect_sql",
        "ai_sql_example",
        ["tenant_id", "dialect", "sql_sha256"],
    )
    op.create_index(
        "ix_ai_sql_example_lookup",
        "ai_sql_example",
        ["tenant_id", "dialect", "uses"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_sql_example_lookup", table_name="ai_sql_example")
    op.drop_constraint(
        "uq_ai_sql_example_tenant_dialect_sql",
        "ai_sql_example",
        type_="unique",
    )
    op.drop_table("ai_sql_example")
