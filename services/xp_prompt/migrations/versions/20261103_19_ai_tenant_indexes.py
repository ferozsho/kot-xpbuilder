"""Indexes for the queries every tenant filter runs (P11-F03)

Three read paths filter by tenant and then by a status, a conversation or a key,
and none of them had a leading ``tenant_id`` index: the planner could only scan
the whole table and throw the other tenants away afterwards. That is invisible on
an empty deployment and expensive on a real one, which is why it is a migration
rather than a note.

The indexes are created without ``CONCURRENTLY`` on purpose: Alembic runs inside a
transaction, and ``CONCURRENTLY`` cannot. These tables are small enough today that
the lock is momentary, and the runbook in the P11 evidence records what to do if a
deployment has grown past that.

Revision ID: 20261103_19
Revises: 20261103_18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261103_19"
down_revision: str | None = "20261103_18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # The diagnostics read: one conversation, one user, newest first.
    (
        "ix_ai_sql_audit_tenant_lookup",
        "ai_sql_audit",
        ("tenant_id", "conversation_id", "superset_user_id", "created_at"),
    ),
    # Queue depth and dead letters, both asked per tenant and per status.
    (
        "ix_ai_outbox_event_tenant_status",
        "ai_outbox_event",
        ("tenant_id", "status", "available_at"),
    ),
    # Configuration history, asked per tenant and per key.
    (
        "ix_ai_setting_audit_tenant_key",
        "ai_setting_audit",
        ("tenant_id", "key", "created_at"),
    ),
)


def upgrade() -> None:
    for name, table, columns in INDEXES:
        op.create_index(name, table, list(columns))


def downgrade() -> None:
    for name, _table, _columns in INDEXES:
        op.drop_index(name, table_name=_table)
