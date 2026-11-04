"""Make the provider-attempt table the canonical usage ledger, with priced history.

``ai_provider_attempt`` existed from the first migration but nothing wrote to it:
it required a job and a step, so an ordinary chat turn, a repair, a dataset draft
or a chart ranking had nowhere to go. That is the gap Phase 9 closes.

Three changes, one per rule:

* **every attempt fits (P9-F01/F02).** ``job_id`` and ``step_id`` become nullable,
  and the row gains the caller identity and the module that spent the tokens, so
  one row describes one provider attempt no matter what started it.
* **a replay cannot be counted twice (P9-F01).** A partial unique index on
  ``(tenant_id, correlation_id, provider, attempt_ordinal)`` makes an at-least-once
  delivery harmless; a retry keeps its ordinal, so it is a different attempt, and a
  fallback is a different provider, so it is too.
* **history cannot be repriced (P9-F04/F05).** ``ai_model_price`` is versioned with
  an effective interval, and every attempt snapshots the price version and the
  computed components it used. Editing a price creates a new version; it never
  rewrites what a past request cost.

Money is stored as integer micros of the listed currency, never a float, and an
unknown token count leaves the cost NULL rather than 0 — "we do not know" is not
"it was free".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261103_15"
down_revision: str | None = "20261103_14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- one canonical ledger, not one per entry point ------------------
    op.alter_column(
        "ai_provider_attempt", "job_id", existing_type=sa.dialects.postgresql.UUID(),
        nullable=True,
    )
    op.alter_column(
        "ai_provider_attempt", "step_id", existing_type=sa.dialects.postgresql.UUID(),
        nullable=True,
    )
    for definition in (
        sa.Column("superset_user_id", sa.Integer, nullable=True),
        sa.Column(
            "conversation_id",
            sa.dialects.postgresql.UUID(),
            sa.ForeignKey("ai_conversation.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "correlation_id", sa.String(64), nullable=False, server_default=""
        ),
        # Which AI-enabled module spent the tokens (chat|repair|dataset|chart|...).
        sa.Column("module", sa.String(32), nullable=False, server_default=""),
        sa.Column("feature", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "fallback", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "error_detail", sa.String(512), nullable=False, server_default=""
        ),
        # The price snapshot this attempt was costed with (P9-F05).
        sa.Column("price_version", sa.Integer, nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default=""),
        sa.Column("estimated_cost_micros", sa.BigInteger, nullable=True),
        sa.Column("price_source", sa.String(64), nullable=False, server_default=""),
    ):
        op.add_column("ai_provider_attempt", definition)

    op.create_index(
        "uq_ai_provider_attempt_correlation",
        "ai_provider_attempt",
        ["tenant_id", "correlation_id", "provider", "attempt_ordinal"],
        unique=True,
        postgresql_where=sa.text("correlation_id <> ''"),
    )
    op.create_index(
        "ix_ai_provider_attempt_recent",
        "ai_provider_attempt",
        ["tenant_id", "started_at"],
    )
    op.create_index(
        "ix_ai_provider_attempt_module",
        "ai_provider_attempt",
        ["tenant_id", "module", "started_at"],
    )

    # --- versioned prices (P9-F04) -------------------------------------
    op.create_table(
        "ai_model_price",
        sa.Column("id", sa.dialects.postgresql.UUID(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        # Bumped, never edited: a new interval leaves old attempts reproducible.
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        # Rates are quoted per this many tokens, so a cheap model stays integral.
        sa.Column("unit_tokens", sa.BigInteger, nullable=False, server_default="1000000"),
        sa.Column("input_micros_per_unit", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("cached_input_micros_per_unit", sa.BigInteger, nullable=True),
        sa.Column("output_micros_per_unit", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("reasoning_micros_per_unit", sa.BigInteger, nullable=True),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(64), nullable=False, server_default=""),
        sa.Column("source_url", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("provider", "model", "version", name="uq_ai_model_price"),
        sa.CheckConstraint("version >= 1", name="ck_ai_model_price_version"),
        sa.CheckConstraint("unit_tokens > 0", name="ck_ai_model_price_unit"),
        sa.CheckConstraint(
            "input_micros_per_unit >= 0 AND output_micros_per_unit >= 0",
            name="ck_ai_model_price_rates",
        ),
        sa.CheckConstraint(
            "cached_input_micros_per_unit IS NULL OR cached_input_micros_per_unit >= 0",
            name="ck_ai_model_price_cached",
        ),
        sa.CheckConstraint(
            "reasoning_micros_per_unit IS NULL OR reasoning_micros_per_unit >= 0",
            name="ck_ai_model_price_reasoning",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_ai_model_price_interval",
        ),
        sa.CheckConstraint("length(currency) = 3", name="ck_ai_model_price_currency"),
    )
    op.create_index(
        "ix_ai_model_price_lookup",
        "ai_model_price",
        ["provider", "model", "effective_from"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_model_price_lookup", table_name="ai_model_price")
    op.drop_table("ai_model_price")
    op.drop_index("ix_ai_provider_attempt_module", table_name="ai_provider_attempt")
    op.drop_index("ix_ai_provider_attempt_recent", table_name="ai_provider_attempt")
    op.drop_index(
        "uq_ai_provider_attempt_correlation", table_name="ai_provider_attempt"
    )
    for column in (
        "price_source",
        "estimated_cost_micros",
        "currency",
        "price_version",
        "error_detail",
        "fallback",
        "feature",
        "module",
        "correlation_id",
        "conversation_id",
        "superset_user_id",
    ):
        op.drop_column("ai_provider_attempt", column)
    op.alter_column(
        "ai_provider_attempt", "step_id",
        existing_type=sa.dialects.postgresql.UUID(), nullable=False,
    )
    op.alter_column(
        "ai_provider_attempt", "job_id",
        existing_type=sa.dialects.postgresql.UUID(), nullable=False,
    )
