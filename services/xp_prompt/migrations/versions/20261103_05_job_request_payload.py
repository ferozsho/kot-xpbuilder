"""Store a job's request durably (sealed) and pin it to a context generation.

`ai_job` deliberately kept only a `request_hash`, which is right for audit but
means a worker restart cannot resume anything: the payload was never stored.
P2-F05 needs reconnect-safe recovery, so the request is now sealed with the same
`SecretBox` used for message bodies and provider keys — durable, but never
readable from a database dump without the root secret.

`context_version` pins a job to the conversation generation it was created in, so
a reset (ADR-0004) invalidates work that was planned against older context.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261103_05"
down_revision: str | None = "20261103_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_job",
        sa.Column("request_ciphertext", sa.LargeBinary, nullable=True),
    )
    op.add_column(
        "ai_job",
        sa.Column(
            "context_version",
            sa.Integer,
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_job", "context_version")
    op.drop_column("ai_job", "request_ciphertext")
