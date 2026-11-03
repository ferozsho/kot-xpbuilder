"""Add admin-managed AI provider configuration.

Providers are configured from the XPBuilder *AI Provider* settings page instead
of hand-editing ``.env``. API keys are stored as Fernet ciphertext produced from
the root service secret, so the database never holds a plaintext key and the
API never returns one.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261103_02"
down_revision: str | None = "20261103_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "ai_provider",
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
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("provider_type", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("api_key_ciphertext", sa.Text, nullable=False),
        sa.Column("api_key_fingerprint", sa.String(64), nullable=False),
        sa.Column("api_key_last_four", sa.String(4), nullable=False, server_default=""),
        sa.Column("default_model", sa.String(128), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "is_default",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_status", sa.String(32), nullable=True),
        sa.Column("last_test_error", sa.String(255), nullable=True),
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
        sa.UniqueConstraint("tenant_id", "name", name="uq_ai_provider_tenant_name"),
        sa.CheckConstraint(
            "provider_type IN ("
            "'openai_compatible', 'anthropic', 'gemini', 'azure_openai', 'deepseek'"
            ")",
            name="ck_ai_provider_type",
        ),
        sa.CheckConstraint(
            "priority BETWEEN -1000 AND 1000",
            name="ck_ai_provider_priority",
        ),
        sa.CheckConstraint(
            "char_length(api_key_ciphertext) > 0",
            name="ck_ai_provider_key_present",
        ),
    )
    # Exactly one default provider per tenant.
    op.create_index(
        "uq_ai_provider_default",
        "ai_provider",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )


def downgrade() -> None:
    op.drop_index("uq_ai_provider_default", table_name="ai_provider")
    op.drop_table("ai_provider")
