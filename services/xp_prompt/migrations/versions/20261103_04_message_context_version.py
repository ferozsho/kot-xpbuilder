"""Stamp every message with the conversation context version it belongs to.

P2-F03 requires an explicit context reset. Resetting must not delete the
transcript, so the turn only replays messages whose ``context_version`` matches
the conversation's current one; everything older stays visible in the UI but is
never sent to a provider again.

Existing rows default to ``1``, which is what ``ai_conversation.context_version``
already holds, so an in-flight conversation keeps its context after the upgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261103_04"
down_revision: str | None = "20261103_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_message",
        sa.Column(
            "context_version",
            sa.Integer,
            nullable=False,
            server_default="1",
        ),
    )
    # Replay reads one conversation's current version in creation order; the
    # existing (tenant, conversation, created_at) index already prunes it, so
    # only the version is added to that prefix.
    op.create_index(
        "ix_ai_message_context",
        "ai_message",
        ["conversation_id", "context_version", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_message_context", table_name="ai_message")
    op.drop_column("ai_message", "context_version")
