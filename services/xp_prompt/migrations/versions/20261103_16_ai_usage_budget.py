"""Budgets and their per-period windows, so a limit can refuse *before* a call.

Two tables, because a limit and its running total are different things:

* ``ai_usage_budget`` is the definition — scope, period, the limit, the warning
  threshold, and whether exceeding it refuses or merely reports.
* ``ai_usage_budget_window`` is the running total for one period of one budget. It
  holds ``spent`` separately from ``reserved`` so a claim in flight is not
  mistaken for money already spent, and it is the row that is updated
  conditionally to make concurrent claims safe (P9-F09).

``warn_percent`` is a column rather than a hardcoded 80 because "warn me at 80%"
and "stop me" are separate decisions a tenant may want to make differently.

The period window is computed by the database (``date_trunc``) rather than by each
process, so a day boundary is one boundary everywhere.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261103_16"
down_revision: str | None = "20261103_15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TENANT = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "ai_usage_budget",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            TENANT,
            sa.ForeignKey("ai_tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(16), nullable=False, server_default="global"),
        # Empty for global; a module name or a user id otherwise.
        sa.Column("scope_ref", sa.String(64), nullable=False, server_default=""),
        sa.Column("period", sa.String(8), nullable=False, server_default="day"),
        sa.Column("cost_micros_limit", sa.BigInteger, nullable=True),
        sa.Column("tokens_limit", sa.BigInteger, nullable=True),
        sa.Column("warn_percent", sa.Integer, nullable=False, server_default="80"),
        sa.Column("hard_stop", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("currency", sa.String(3), nullable=False, server_default=""),
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
        sa.UniqueConstraint(
            "tenant_id", "scope", "scope_ref", "period", name="uq_ai_usage_budget"
        ),
        sa.CheckConstraint(
            "scope IN ('global', 'user', 'module')", name="ck_ai_usage_budget_scope"
        ),
        sa.CheckConstraint(
            "period IN ('day', 'month')", name="ck_ai_usage_budget_period"
        ),
        sa.CheckConstraint(
            "cost_micros_limit IS NOT NULL OR tokens_limit IS NOT NULL",
            name="ck_ai_usage_budget_limit_present",
        ),
        sa.CheckConstraint(
            "warn_percent > 0 AND warn_percent <= 100",
            name="ck_ai_usage_budget_warn_percent",
        ),
        # A scoped budget without a reference would silently apply to nothing.
        sa.CheckConstraint(
            "scope = 'global' OR scope_ref <> ''",
            name="ck_ai_usage_budget_scope_ref",
        ),
    )
    op.create_index(
        "ix_ai_usage_budget_tenant", "ai_usage_budget", ["tenant_id", "scope"]
    )

    op.create_table(
        "ai_usage_budget_window",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "budget_id",
            UUID,
            sa.ForeignKey("ai_usage_budget.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        # Committed spend and in-flight claims are kept apart: a claim that is
        # still running must not read as money already gone.
        sa.Column("spent_micros", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("spent_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("reserved_micros", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("reserved_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("blocked_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "budget_id", "period_start", name="uq_ai_usage_budget_window"
        ),
        sa.CheckConstraint(
            "spent_micros >= 0 AND spent_tokens >= 0 "
            "AND reserved_micros >= 0 AND reserved_tokens >= 0",
            name="ck_ai_usage_budget_window_non_negative",
        ),
    )
    op.create_index(
        "ix_ai_usage_budget_window_period",
        "ai_usage_budget_window",
        ["budget_id", "period_start"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_usage_budget_window_period", table_name="ai_usage_budget_window"
    )
    op.drop_table("ai_usage_budget_window")
    op.drop_index("ix_ai_usage_budget_tenant", table_name="ai_usage_budget")
    op.drop_table("ai_usage_budget")
