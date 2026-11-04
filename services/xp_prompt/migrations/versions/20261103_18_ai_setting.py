"""Typed configuration, versioned per scope, with an immutable audit trail.

Configuration held as arbitrary key/value pairs is a security problem wearing a
settings page: nothing validates it, nothing knows which values are safe, and
nobody can say what it was before. This pair of tables is deliberately narrow.

``ai_setting`` stores one row per *known* key per scope. The registry in
``settings_registry.py`` is the authority on which keys exist, their types, their
bounds and whether a change needs a restart; the table only stores the value an
operator chose. A key that is not in the registry can never be written, so there
are no surprise settings.

``ai_setting_audit`` is append-only. Every write records the before and after
values, the actor, the reason and the resulting version, which is what makes
rollback and accountability possible. Nothing here is ever a secret: provider keys
are sealed elsewhere (ADR-0002) and no registry entry may name one.

The unique constraint includes the scope so a tenant override coexists with the
global default instead of replacing it — the whole point of two scopes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261103_18"
down_revision: str | None = "20261103_17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "ai_setting",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        # Null for a global row; set for a tenant override.
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("ai_tenant.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("scope", sa.String(16), nullable=False, server_default="global"),
        sa.Column("key", sa.String(96), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("updated_by", sa.String(128), nullable=False, server_default=""),
        sa.Column("updated_reason", sa.String(512), nullable=False, server_default=""),
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
        sa.CheckConstraint(
            "scope IN ('global', 'tenant')", name="ck_ai_setting_scope"
        ),
        # A tenant row must name its tenant; a global row must not.
        sa.CheckConstraint(
            "(scope = 'global' AND tenant_id IS NULL) "
            "OR (scope = 'tenant' AND tenant_id IS NOT NULL)",
            name="ck_ai_setting_scope_tenant",
        ),
        sa.CheckConstraint("version >= 1", name="ck_ai_setting_version"),
    )
    # Two partial indexes rather than one with NULLs-not-distinct: the dialect arg
    # for that is not available in this SQLAlchemy, and a plain unique index over a
    # nullable column would let duplicate global rows through (NULL <> NULL).
    op.create_index(
        "uq_ai_setting_global",
        "ai_setting",
        ["scope", "key"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
    )
    op.create_index(
        "uq_ai_setting_tenant",
        "ai_setting",
        ["scope", "tenant_id", "key"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )

    op.create_table(
        "ai_setting_audit",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("ai_tenant.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("key", sa.String(96), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("before_value", postgresql.JSONB(), nullable=True),
        sa.Column("after_value", postgresql.JSONB(), nullable=True),
        sa.Column("actor", sa.String(128), nullable=False, server_default=""),
        sa.Column("reason", sa.String(512), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_ai_setting_audit_key",
        "ai_setting_audit",
        ["key", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_setting_audit_key", table_name="ai_setting_audit")
    op.drop_table("ai_setting_audit")
    op.drop_index("uq_ai_setting_tenant", table_name="ai_setting")
    op.drop_index("uq_ai_setting_global", table_name="ai_setting")
    op.drop_table("ai_setting")
