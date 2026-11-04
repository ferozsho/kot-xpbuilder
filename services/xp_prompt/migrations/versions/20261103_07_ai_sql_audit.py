"""Add the SQL turn audit table (P4-F12).

Every SQL turn leaves a row here: the candidate's dialect and tables, what the
guard did (inlined datasets, applied row-level predicates), the cost plan, and how
the turn ended. It is metadata, not content — the statement itself is sealed, and
a fingerprint is what is queryable.

Two rules shape the columns:

* **the SQL is not readable from a dump.** ``sql_ciphertext`` is sealed with the
  service secret, so a database backup does not become a catalogue of what every
  tenant asked for.
* **refusals are recorded too.** A refused expensive query or a blocked statement
  is the interesting case in an audit; storing only successes would hide exactly
  the attempts an operator wants to see.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261103_07"
down_revision: str | None = "20261103_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "ai_sql_audit",
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
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("ai_conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("superset_user_id", sa.Integer, nullable=False),
        sa.Column(
            "correlation_id", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column("intent", sa.String(32), nullable=False, server_default=""),
        # answered | refused | failed
        sa.Column("outcome", sa.String(24), nullable=False),
        # candidate | guard | plan | execute | result | store
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("code", sa.String(64), nullable=False, server_default=""),
        sa.Column("dialect", sa.String(32), nullable=False, server_default=""),
        sa.Column(
            "tables",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "inlined",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "rls_applied",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("plan_source", sa.String(16), nullable=True),
        sa.Column("plan_rows", sa.BigInteger, nullable=True),
        sa.Column("plan_cost", sa.Float, nullable=True),
        sa.Column("plan_nodes", sa.Integer, nullable=True),
        sa.Column("plan_code", sa.String(32), nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("row_count", sa.Integer, nullable=True),
        sa.Column("returned", sa.Integer, nullable=True),
        sa.Column("truncated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("sql_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("sql_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_ai_sql_audit_conversation_created",
        "ai_sql_audit",
        ["conversation_id", "created_at"],
    )
    op.create_index("ix_ai_sql_audit_correlation", "ai_sql_audit", ["correlation_id"])
    op.create_index("ix_ai_sql_audit_created", "ai_sql_audit", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_sql_audit_created", table_name="ai_sql_audit")
    op.drop_index("ix_ai_sql_audit_correlation", table_name="ai_sql_audit")
    op.drop_index("ix_ai_sql_audit_conversation_created", table_name="ai_sql_audit")
    op.drop_table("ai_sql_audit")
