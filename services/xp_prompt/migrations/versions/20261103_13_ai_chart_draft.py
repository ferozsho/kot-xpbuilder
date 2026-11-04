"""Store typed chart drafts, their rationale and their lineage (P7-F05/F09).

One row per proposed chart: the validated configuration, the rationale it came from, the
dataset it targets, the ranking source (rules or model), and the fingerprints it was
validated against — plus the Superset chart id once it exists.

Three constraints carry the rules:

* ``idempotency_key`` with a partial unique index — a replayed create returns the same
  draft, so a double-click cannot produce two charts;
* ``version`` — every write is optimistic, so a stale tab is refused instead of
  overwriting a newer edit (P7-F08);
* ``status`` — ``draft``/``saved``/``scrapped``, so a failed save is closed explicitly
  rather than left ambiguous (P7-F10).

``form_data`` holds a configuration, not data: the chart's own controls, no result rows.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261103_13"
down_revision: str | None = "20261103_12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "ai_chart_draft",
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
        sa.Column("viz_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        # The Superset dataset this chart reads; the id is Superset's, not a copy of it.
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("dataset_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("database_name", sa.String(128), nullable=False, server_default=""),
        sa.Column(
            "form_data",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("rationale", sa.String(600), nullable=False, server_default=""),
        sa.Column("candidate_id", sa.String(64), nullable=False, server_default=""),
        # rules | model — how the order was decided (P7-F04)
        sa.Column("ranking_source", sa.String(16), nullable=False, server_default=""),
        sa.Column("description", sa.String(256), nullable=False, server_default=""),
        sa.Column(
            "profile_fingerprint", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column(
            "catalog_fingerprint", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "idempotency_key", sa.String(96), nullable=False, server_default=""
        ),
        sa.Column("superset_slice_id", sa.Integer(), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
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
            name="ck_ai_chart_draft_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_ai_chart_draft_version"),
    )
    op.create_index(
        "uq_ai_chart_draft_idempotency",
        "ai_chart_draft",
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key <> ''"),
    )
    op.create_index(
        "ix_ai_chart_draft_owner",
        "ai_chart_draft",
        ["tenant_id", "superset_user_id", "status"],
    )
    op.create_index(
        "ix_ai_chart_draft_dataset", "ai_chart_draft", ["tenant_id", "dataset_id"]
    )
    op.create_index(
        "ix_ai_chart_draft_conversation",
        "ai_chart_draft",
        ["tenant_id", "conversation_id"],
    )
    op.create_index("ix_ai_chart_draft_updated", "ai_chart_draft", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_chart_draft_updated", table_name="ai_chart_draft")
    op.drop_index("ix_ai_chart_draft_conversation", table_name="ai_chart_draft")
    op.drop_index("ix_ai_chart_draft_dataset", table_name="ai_chart_draft")
    op.drop_index("ix_ai_chart_draft_owner", table_name="ai_chart_draft")
    op.drop_index("uq_ai_chart_draft_idempotency", table_name="ai_chart_draft")
    op.drop_table("ai_chart_draft")
