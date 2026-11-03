"""Create the initial KOT5 XP Prompt control-plane schema."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261103_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
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
    )


def tenant_column() -> sa.Column:
    return sa.Column(
        "tenant_id",
        UUID,
        sa.ForeignKey("ai_tenant.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "ai_tenant",
        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("key", sa.String(128), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("policy_version", sa.Integer, nullable=False, server_default="1"),
        *timestamp_columns(),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'deleting', 'deleted')",
            name="ck_ai_tenant_status",
        ),
    )
    op.execute(
        """
        INSERT INTO ai_tenant (id, key, name)
        VALUES ('00000000-0000-0000-0000-000000000001', 'default', 'Default')
        """
    )

    op.create_table(
        "ai_conversation",
        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        tenant_column(),
        sa.Column("superset_user_id", sa.Integer, nullable=False),
        sa.Column("title", sa.String(255), nullable=False, server_default="New chat"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("context_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *timestamp_columns(),
        sa.CheckConstraint(
            "status IN ('active', 'archived', 'deleted')",
            name="ck_ai_conversation_status",
        ),
    )
    op.create_index(
        "ix_ai_conversation_owner_recent",
        "ai_conversation",
        ["tenant_id", "superset_user_id", "last_message_at"],
    )

    op.create_table(
        "ai_message",
        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        tenant_column(),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("ai_conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("content_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("redacted_excerpt", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "structured_content",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("retention_class", sa.String(32), nullable=False, server_default="standard"),
        *timestamp_columns(),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'tool', 'system')",
            name="ck_ai_message_role",
        ),
    )
    op.create_index(
        "ix_ai_message_conversation_created",
        "ai_message",
        ["tenant_id", "conversation_id", "created_at"],
    )

    op.create_table(
        "ai_job",
        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        tenant_column(),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("ai_conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_id", UUID, sa.ForeignKey("ai_message.id"), nullable=True),
        sa.Column("superset_user_id", sa.Integer, nullable=False),
        sa.Column("job_type", sa.String(48), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.Integer, nullable=False),
        sa.Column("authorization_version", sa.Integer, nullable=False),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.Text, nullable=True),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "superset_user_id",
            "idempotency_key",
            name="uq_ai_job_owner_idempotency",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'clarification', 'succeeded', 'failed', 'cancelled')",
            name="ck_ai_job_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_ai_job_attempt_nonnegative"),
        sa.CheckConstraint("max_attempts BETWEEN 1 AND 10", name="ck_ai_job_max_attempts"),
    )
    op.create_index("ix_ai_job_recovery", "ai_job", ["status", "lease_expires_at"])
    op.create_index(
        "ix_ai_job_conversation_created",
        "ai_job",
        ["tenant_id", "conversation_id", "created_at"],
    )

    op.create_table(
        "ai_job_step",
        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        tenant_column(),
        sa.Column("job_id", UUID, sa.ForeignKey("ai_job.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_key", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("input_hash", sa.String(64), nullable=True),
        sa.Column("output_ref", UUID, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.Text, nullable=True),
        *timestamp_columns(),
        sa.UniqueConstraint("job_id", "step_key", "ordinal", name="uq_ai_job_step_order"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'skipped')",
            name="ck_ai_job_step_status",
        ),
    )

    op.create_table(
        "ai_provider_attempt",
        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        tenant_column(),
        sa.Column("job_id", UUID, sa.ForeignKey("ai_job.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_id", UUID, sa.ForeignKey("ai_job_step.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("attempt_ordinal", sa.Integer, nullable=False),
        sa.Column("fallback_from_id", UUID, sa.ForeignKey("ai_provider_attempt.id"), nullable=True),
        sa.Column("provider_request_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_class", sa.String(64), nullable=True),
        sa.Column("input_tokens", sa.BigInteger, nullable=True),
        sa.Column("cached_input_tokens", sa.BigInteger, nullable=True),
        sa.Column("output_tokens", sa.BigInteger, nullable=True),
        sa.Column("reasoning_tokens", sa.BigInteger, nullable=True),
        sa.Column("total_tokens", sa.BigInteger, nullable=True),
        sa.Column("usage_source", sa.String(16), nullable=False, server_default="missing"),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *timestamp_columns(),
        sa.UniqueConstraint("job_id", "step_id", "attempt_ordinal", name="uq_ai_provider_attempt"),
        sa.CheckConstraint(
            "status IN ('started', 'succeeded', 'failed', 'cancelled')",
            name="ck_ai_provider_attempt_status",
        ),
        sa.CheckConstraint(
            "usage_source IN ('reported', 'estimated', 'missing')",
            name="ck_ai_provider_attempt_usage_source",
        ),
    )

    op.create_table(
        "ai_outbox_event",
        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        tenant_column(),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", UUID, nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        *timestamp_columns(),
        sa.CheckConstraint(
            "status IN ('pending', 'delivering', 'delivered', 'dead_letter')",
            name="ck_ai_outbox_status",
        ),
    )
    op.create_index(
        "ix_ai_outbox_delivery",
        "ai_outbox_event",
        ["status", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_outbox_delivery", table_name="ai_outbox_event")
    op.drop_table("ai_outbox_event")
    op.drop_table("ai_provider_attempt")
    op.drop_table("ai_job_step")
    op.drop_index("ix_ai_job_conversation_created", table_name="ai_job")
    op.drop_index("ix_ai_job_recovery", table_name="ai_job")
    op.drop_table("ai_job")
    op.drop_index("ix_ai_message_conversation_created", table_name="ai_message")
    op.drop_table("ai_message")
    op.drop_index("ix_ai_conversation_owner_recent", table_name="ai_conversation")
    op.drop_table("ai_conversation")
    op.drop_table("ai_tenant")

