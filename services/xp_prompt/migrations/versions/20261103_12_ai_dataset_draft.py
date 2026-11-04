"""Store dataset drafts so a proposal survives a reload and a double submit (P6-F05).

A draft is the proposal *before* any Superset object exists: the specification, what was
measured about it, the fingerprints it was validated against, and the lineage back to the
conversation and statement that produced it.

Three columns carry the rules:

* ``idempotency_key`` with a partial unique index — a replayed create returns the same
  draft instead of making a second one;
* ``version`` — every write is optimistic, so a stale tab is refused rather than
  overwriting a newer edit (P6-F09);
* ``status`` — ``draft``/``saved``/``scrapped``, so a failed save can be closed with a
  compensating action instead of being left ambiguous (P6-F10).

The statement is sealed (ADR-0016) and pinned by SHA-256, so the row never becomes a
second copy of the query.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261103_12"
down_revision: str | None = "20261103_11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "ai_dataset_draft",
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
            sa.ForeignKey("ai_conversation.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "job_id",
            UUID,
            sa.ForeignKey("ai_job.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("superset_user_id", sa.Integer(), nullable=False),
        sa.Column(
            "correlation_id", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column("name", sa.String(128), nullable=False),
        # physical | virtual
        sa.Column("kind", sa.String(16), nullable=False, server_default="virtual"),
        sa.Column("database_name", sa.String(128), nullable=False),
        sa.Column("schema_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("table_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("sql_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("sql_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "spec",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "spec_fingerprint", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column(
            "schema_fingerprint", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column("policy_version", sa.String(32), nullable=False, server_default=""),
        sa.Column(
            "quality",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("quality_ok", sa.Boolean(), nullable=False, server_default="false"),
        # draft | saved | scrapped
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        # Optimistic concurrency: a stale writer is refused (P6-F09).
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "idempotency_key", sa.String(96), nullable=False, server_default=""
        ),
        sa.Column("superset_dataset_id", sa.Integer(), nullable=True),
        sa.Column(
            "owners",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scrapped_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'saved', 'scrapped')",
            name="ck_ai_dataset_draft_status",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_ai_dataset_draft_version"
        ),
    )
    # A replayed create must find the same draft, and only a real key participates.
    op.create_index(
        "uq_ai_dataset_draft_idempotency",
        "ai_dataset_draft",
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key <> ''"),
    )
    op.create_index(
        "ix_ai_dataset_draft_owner",
        "ai_dataset_draft",
        ["tenant_id", "superset_user_id", "status"],
    )
    op.create_index(
        "ix_ai_dataset_draft_conversation",
        "ai_dataset_draft",
        ["tenant_id", "conversation_id"],
    )
    op.create_index(
        "ix_ai_dataset_draft_updated", "ai_dataset_draft", ["updated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_dataset_draft_updated", table_name="ai_dataset_draft")
    op.drop_index("ix_ai_dataset_draft_conversation", table_name="ai_dataset_draft")
    op.drop_index("ix_ai_dataset_draft_owner", table_name="ai_dataset_draft")
    op.drop_index("uq_ai_dataset_draft_idempotency", table_name="ai_dataset_draft")
    op.drop_table("ai_dataset_draft")
