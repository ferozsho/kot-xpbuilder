"""Record repair attempts so a failed fix is not tried twice (P5-F07).

One row per attempt, keyed by the *failure* fingerprint and carrying the *fix*
fingerprint that was tried. The pair is what makes suppression possible: "this
failure, with this fix, already failed" is a lookup rather than a memory.

The table is deliberately narrow — no SQL, no error text, only fingerprints and
outcomes — so the repair history cannot become a second copy of the transcript.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261103_10"
down_revision: str | None = "20261103_09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "ai_repair_attempt",
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
            "failure_fingerprint", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column("kind", sa.String(32), nullable=False, server_default=""),
        sa.Column("stage", sa.String(24), nullable=False, server_default=""),
        # attempted | succeeded | failed | suppressed
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("fix_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_ai_repair_attempt_failure",
        "ai_repair_attempt",
        ["tenant_id", "failure_fingerprint", "outcome"],
    )
    op.create_index(
        "ix_ai_repair_attempt_created", "ai_repair_attempt", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_repair_attempt_created", table_name="ai_repair_attempt")
    op.drop_index("ix_ai_repair_attempt_failure", table_name="ai_repair_attempt")
    op.drop_table("ai_repair_attempt")
