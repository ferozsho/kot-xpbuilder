"""Record every dashboard publish attempt, so nothing is created invisibly.

Publishing is the first step in Phase 8 that writes to Superset, which makes it
the first step that can leave something behind. One row per attempt answers the
questions that only become urgent after a failure:

* **what did this attempt create?** ``created_objects`` holds the id of every
  chart and dashboard it made, written before the next step ran;
* **what was done about it?** ``cleanup`` holds one outcome per object —
  ``deleted``, ``missing``, or the failure that blocked removal (P8-F10);
* **who may see the result?** ``owners``/``roles``/``visibility``/
  ``guest_access`` are the access the publish assigned, not the access the
  caller hoped for (P8-F09);
* **has the dashboard drifted?** ``position_fingerprint`` is the layout this
  publish produced, compared on the next update so a manual edit is refused
  rather than overwritten (P8-F11).

Three constraints carry the rules:

* ``idempotency_key`` with a partial unique index — a retried request returns
  the row it already opened instead of publishing twice (P8-F08);
* ``mode`` — ``create``/``update``/``duplicate`` is stated, never implied, and
  only the modes that act on a dashboard may carry one;
* ``status`` — ``pending``/``published``/``failed``, so an attempt in flight is
  distinguishable from one that finished and one that was cleaned up.

The row stores identifiers, counts, fingerprints and outcomes. It stores no
result rows, no chart configuration, and no rendered position blob.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261103_14"
down_revision: str | None = "20261103_13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "ai_dashboard_publish",
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
        sa.Column("correlation_id", sa.String(64), nullable=False, server_default=""),
        # create | update | duplicate — stated by the caller (P8-F08)
        sa.Column("mode", sa.String(16), nullable=False, server_default="create"),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending"
        ),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("goal", sa.String(600), nullable=False, server_default=""),
        # The dashboard this attempt wrote; null until it succeeded.
        sa.Column("dashboard_id", sa.Integer(), nullable=True),
        sa.Column("dashboard_slug", sa.String(128), nullable=False, server_default=""),
        # For update/duplicate: the dashboard the mode acted on.
        sa.Column("target_dashboard_id", sa.Integer(), nullable=True),
        sa.Column(
            "owners",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "roles",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "visibility", sa.String(16), nullable=False, server_default="private"
        ),
        sa.Column(
            "guest_access", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("slot_count", sa.Integer(), nullable=False, server_default="0"),
        # What the caller expected to find, and what this publish produced.
        sa.Column(
            "expected_fingerprint", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column(
            "position_fingerprint", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column(
            "idempotency_key", sa.String(96), nullable=False, server_default=""
        ),
        sa.Column(
            "created_objects",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "cleanup",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("error_code", sa.String(64), nullable=False, server_default=""),
        sa.Column("error_message", sa.String(512), nullable=False, server_default=""),
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
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "mode IN ('create', 'update', 'duplicate')",
            name="ck_ai_dashboard_publish_mode",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'published', 'failed')",
            name="ck_ai_dashboard_publish_status",
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'tenant', 'public')",
            name="ck_ai_dashboard_publish_visibility",
        ),
        sa.CheckConstraint("slot_count >= 0", name="ck_ai_dashboard_publish_slots"),
        # A mode that acts on an existing dashboard must say which one.
        sa.CheckConstraint(
            "mode = 'create' OR target_dashboard_id IS NOT NULL",
            name="ck_ai_dashboard_publish_target",
        ),
    )
    op.create_index(
        "uq_ai_dashboard_publish_idempotency",
        "ai_dashboard_publish",
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key <> ''"),
    )
    op.create_index(
        "ix_ai_dashboard_publish_dashboard",
        "ai_dashboard_publish",
        ["dashboard_id", "published_at"],
    )
    op.create_index(
        "ix_ai_dashboard_publish_user",
        "ai_dashboard_publish",
        ["tenant_id", "superset_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_dashboard_publish_user", table_name="ai_dashboard_publish")
    op.drop_index(
        "ix_ai_dashboard_publish_dashboard", table_name="ai_dashboard_publish"
    )
    op.drop_index(
        "uq_ai_dashboard_publish_idempotency", table_name="ai_dashboard_publish"
    )
    op.drop_table("ai_dashboard_publish")
