"""Add the artifact lifecycle table (P2-F07).

One shared lifecycle for every artifact type: a producer creates a ``draft``, the
user ``confirm``s it, and only a publisher may move it to ``published`` (with the
native object it created). Nothing publishes from a message alone.

The payload is sealed like message bodies and job requests: a draft chart or
dataset specification can contain SQL and column names, which must not be
readable from a database dump.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261103_06"
down_revision: str | None = "20261103_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "ai_artifact",
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
        sa.Column(
            "job_id",
            UUID,
            sa.ForeignKey("ai_job.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("superset_user_id", sa.Integer, nullable=False),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("payload_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("published_object_type", sa.String(32), nullable=True),
        sa.Column("published_object_id", sa.String(64), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discarded_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "artifact_type IN ('dataset', 'chart', 'dashboard')",
            name="ck_ai_artifact_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'confirmed', 'published', 'discarded')",
            name="ck_ai_artifact_status",
        ),
        sa.CheckConstraint(
            "published_object_id IS NULL OR published_object_type IS NOT NULL",
            name="ck_ai_artifact_published_pair",
        ),
    )
    op.create_index(
        "ix_ai_artifact_conversation_created",
        "ai_artifact",
        ["tenant_id", "conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_artifact_conversation_created", table_name="ai_artifact")
    op.drop_table("ai_artifact")
