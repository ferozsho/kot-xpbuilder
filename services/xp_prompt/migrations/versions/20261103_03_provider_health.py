"""Add passive provider health state to ``ai_provider``.

Every attempt outcome folds into a redacted health state so routing can skip a
provider whose circuit is open and the settings page can explain why. Only the
normalized error *code* is stored — never a prompt, response body, or header.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261103_03"
down_revision: str | None = "20261103_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_provider",
        sa.Column(
            "consecutive_failures",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "ai_provider",
        sa.Column("circuit_open_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ai_provider",
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ai_provider",
        sa.Column("last_error_code", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_provider", "last_error_code")
    op.drop_column("ai_provider", "last_failure_at")
    op.drop_column("ai_provider", "circuit_open_until")
    op.drop_column("ai_provider", "consecutive_failures")
