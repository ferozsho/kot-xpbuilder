"""Record whether an audited turn's statement came from the candidate cache (F11).

"Cache bypass is honoured and audited" needs the audit row to say which of the three
happened: a fresh `miss`, a replayed `hit`, or an explicit `bypass`. Storing it as a
column keeps the distinction queryable instead of inferable from logs.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261103_08"
down_revision: str | None = "20261103_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_sql_audit",
        sa.Column(
            "cache_status",
            sa.String(16),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_sql_audit", "cache_status")
