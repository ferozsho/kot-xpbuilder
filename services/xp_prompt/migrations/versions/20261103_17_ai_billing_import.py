"""Billed amounts and the lines they came from, kept apart from our estimate.

The ledger's cost is a computation; an invoice is a fact. Storing them in the same
place would invite one to overwrite the other, so a billing import is its own
record: one row per export, one row per line, and a nullable link to the attempt a
line was matched to (P9-F10).

The link is nullable on purpose. A line nobody could match is information — it is
how "the invoice says more than we recorded" becomes visible — and a NOT NULL
relationship would force a wrong guess instead.

``fingerprint`` is a digest of the normalised rows so re-importing the same export
is idempotent; a partial unique index only enforces it when a fingerprint exists,
which keeps the column useful without forbidding manual rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261103_17"
down_revision: str | None = "20261103_16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "ai_billing_import",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("ai_tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source", sa.String(64), nullable=False, server_default=""),
        sa.Column("row_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_actual_micros", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "period_end > period_start", name="ck_ai_billing_import_period"
        ),
        sa.CheckConstraint(
            "length(currency) = 3", name="ck_ai_billing_import_currency"
        ),
        sa.CheckConstraint(
            "total_actual_micros >= 0", name="ck_ai_billing_import_total"
        ),
    )
    op.create_index(
        "uq_ai_billing_import_fingerprint",
        "ai_billing_import",
        ["tenant_id", "provider", "period_start", "period_end", "fingerprint"],
        unique=True,
        postgresql_where=sa.text("fingerprint <> ''"),
    )

    op.create_table(
        "ai_billing_line",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "import_id",
            UUID,
            sa.ForeignKey("ai_billing_import.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("provider_request_id", sa.String(255), nullable=True),
        sa.Column("actual_micros", sa.BigInteger, nullable=False),
        sa.Column("input_tokens", sa.BigInteger, nullable=True),
        sa.Column("output_tokens", sa.BigInteger, nullable=True),
        sa.Column("total_tokens", sa.BigInteger, nullable=True),
        # Nullable: an unmatched line is the finding, not an error (P9-F10).
        sa.Column(
            "matched_attempt_id",
            UUID,
            sa.ForeignKey("ai_provider_attempt.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("match_kind", sa.String(16), nullable=False, server_default="none"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "actual_micros >= 0", name="ck_ai_billing_line_actual"
        ),
        sa.CheckConstraint(
            "match_kind IN ('request_id', 'none')", name="ck_ai_billing_line_match"
        ),
    )
    op.create_index(
        "ix_ai_billing_line_request",
        "ai_billing_line",
        ["import_id", "provider_request_id"],
    )
    op.create_index(
        "ix_ai_billing_line_model", "ai_billing_line", ["import_id", "model"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_billing_line_model", table_name="ai_billing_line")
    op.drop_index("ix_ai_billing_line_request", table_name="ai_billing_line")
    op.drop_table("ai_billing_line")
    op.drop_index(
        "uq_ai_billing_import_fingerprint", table_name="ai_billing_import"
    )
    op.drop_table("ai_billing_import")
