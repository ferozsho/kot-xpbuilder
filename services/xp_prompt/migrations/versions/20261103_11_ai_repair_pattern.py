"""Store validated repair patterns, and the evidence behind them (P5-F02/F08/F09).

A pattern is a correction for one identified failure, scoped to a tenant, a dialect, a
schema/policy fingerprint and an intent. The row carries its standing in the lifecycle
(``candidate → validating → validated → promoted → deprecated/quarantined``), the
evidence that earned it, and the version it supersedes.

Two deliberate choices:

* the correction is stored **sealed** with a SHA-256 identity, like every other
  statement in this service (ADR-0016), so the library is not a second copy of the
  transcript;
* ``evidence`` is a bounded list of the *sources* that validated it, because
  promotion must count independent validations rather than one conversation
  retrying itself.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261103_11"
down_revision: str | None = "20261103_10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "ai_repair_pattern",
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
        # candidate | validating | validated | promoted | deprecated | quarantined
        sa.Column("state", sa.String(16), nullable=False, server_default="candidate"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("kind", sa.String(32), nullable=False, server_default=""),
        sa.Column("stage", sa.String(24), nullable=False, server_default=""),
        sa.Column(
            "failure_fingerprint", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column("dialect", sa.String(32), nullable=False, server_default=""),
        sa.Column(
            "schema_fingerprint", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column("intent", sa.String(32), nullable=False, server_default=""),
        sa.Column("correction_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "correction_sha256", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column(
            "tables",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("successes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "confidence",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # generated | operator
        sa.Column("origin", sa.String(16), nullable=False, server_default="generated"),
        # automatic | operator
        sa.Column(
            "promotion_policy", sa.String(16), nullable=False, server_default=""
        ),
        sa.Column(
            "validator_version", sa.String(32), nullable=False, server_default=""
        ),
        sa.Column("policy_version", sa.String(32), nullable=False, server_default=""),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "superseded_by",
            UUID,
            sa.ForeignKey("ai_repair_pattern.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.String(64), nullable=False, server_default=""),
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
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "failure_fingerprint",
            "dialect",
            "schema_fingerprint",
            "intent",
            "correction_sha256",
            name="uq_ai_repair_pattern_scope",
        ),
    )
    op.create_index(
        "ix_ai_repair_pattern_lookup",
        "ai_repair_pattern",
        ["tenant_id", "failure_fingerprint", "state"],
    )
    op.create_index(
        "ix_ai_repair_pattern_state", "ai_repair_pattern", ["tenant_id", "state"]
    )
    op.create_index(
        "ix_ai_repair_pattern_updated", "ai_repair_pattern", ["updated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_repair_pattern_updated", table_name="ai_repair_pattern")
    op.drop_index("ix_ai_repair_pattern_state", table_name="ai_repair_pattern")
    op.drop_index("ix_ai_repair_pattern_lookup", table_name="ai_repair_pattern")
    op.drop_table("ai_repair_pattern")
