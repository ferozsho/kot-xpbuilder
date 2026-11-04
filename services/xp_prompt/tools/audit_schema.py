#!/usr/bin/env python3
"""Audit the AI schema and its migrations (P11-F01, F02, F03).

Three questions, each with a failure that only shows up in production:

* **is every migration reversible?** An upgrade with no ``downgrade`` is a
  one-way door; a ``create_index`` without a matching ``drop_index`` leaves the
  rollback half-done.
* **is the chain honest?** One head, linear, and the head the code expects
  (``SCHEMA_REVISION``) — a mismatch is what makes the API report
  ``postgres: unavailable`` after a deploy.
* **are the tenant-filtered queries indexed?** A missing leading ``tenant_id``
  index is invisible on an empty database and expensive on a real one.

Run it in CI with no arguments for the static checks, and against a live database
with ``--live`` for the applied revision and index coverage:

    python tools/audit_schema.py
    python tools/audit_schema.py --live
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "migrations" / "versions"

REVISION = re.compile(r'^revision:\s*str\s*=\s*"([^"]+)"', re.MULTILINE)
DOWN_REVISION = re.compile(
    r'^down_revision:\s*str\s*\|\s*None\s*=\s*(.+)$', re.MULTILINE
)
SCHEMA_REVISION = re.compile(r'^SCHEMA_REVISION\s*=\s*"([^"]+)"', re.MULTILINE)
CREATE_TABLE = re.compile(r'op\.create_table\(\s*"([^"]+)"')
CREATE_INDEX = re.compile(r'op\.create_index\(\s*\n?\s*"([^"]+)"')
ADD_COLUMN = re.compile(r'op\.add_column\(\s*\n?\s*"([^"]+)"')
DROP_TABLE = re.compile(r'op\.drop_table\(\s*\n?\s*"([^"]+)"')
DROP_INDEX = re.compile(r'op\.drop_index\(\s*\n?\s*"([^"]+)"')
DROP_COLUMN = re.compile(r'op\.drop_column\(\s*\n?\s*"([^"]+)"')

#: Tables whose reads always start with the tenant. A leading ``tenant_id`` index
#: is what keeps those reads bounded as the deployment grows (P11-F03).
TENANT_TABLES = (
    "ai_conversation",
    "ai_message",
    "ai_job",
    "ai_artifact",
    "ai_sql_audit",
    "ai_outbox_event",
    "ai_provider_attempt",
    "ai_setting_audit",
    "ai_dataset_draft",
    "ai_chart_draft",
    "ai_dashboard_publish",
)


def read_migrations() -> dict[str, tuple[str, str, str]]:
    """Every migration as ``revision -> (down_revision, name, source)``."""
    found: dict[str, tuple[str, str, str]] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        revision = REVISION.search(source)
        if revision is None:
            raise SystemExit(f"{path.name} declares no revision")
        down = DOWN_REVISION.search(source)
        parent = "" if down is None else down.group(1).strip().strip('"')
        if parent in {"None", ""}:
            parent = ""
        found[revision.group(1)] = (parent, path.name, source)
    return found


def check_reversible(found: dict[str, tuple[str, str, str]]) -> list[str]:
    problems: list[str] = []
    for revision, (_parent, name, source) in sorted(found.items()):
        if "def downgrade(" not in source:
            problems.append(f"{name}: no downgrade()")
            continue
        downgrade = source[source.index("def downgrade(") :]
        for created in CREATE_TABLE.findall(source):
            if created not in DROP_TABLE.findall(downgrade):
                problems.append(f"{name}: creates {created} but never drops it")
        for created in CREATE_INDEX.findall(source):
            if created not in DROP_INDEX.findall(downgrade):
                problems.append(f"{name}: index {created} is never dropped")
        for added in ADD_COLUMN.findall(source):
            if added not in DROP_COLUMN.findall(downgrade):
                problems.append(f"{name}: column added to {added} is never dropped")
    return problems


def check_chain(found: dict[str, tuple[str, str, str]]) -> tuple[list[str], str]:
    problems: list[str] = []
    children: dict[str, list[str]] = {}
    for revision, (parent, name, _source) in found.items():
        children.setdefault(parent, []).append(revision)
        if parent and parent not in found:
            problems.append(f"{name}: parent {parent} does not exist")
    for parent, kids in children.items():
        if parent and len(kids) > 1:
            problems.append(f"{parent} has {len(kids)} children; the chain forks")
    heads = [revision for revision in found if revision not in {
        parent for parent, _n, _s in found.values() if parent
    }]
    roots = children.get("", [])
    if len(roots) != 1:
        problems.append(f"expected one root revision, found {len(roots)}")
    if len(heads) != 1:
        problems.append(f"expected one head revision, found {len(heads)}: {heads}")
    head = heads[0] if len(heads) == 1 else ""
    declared = SCHEMA_REVISION.search(
        (ROOT / "xp_prompt" / "schema.py").read_text(encoding="utf-8")
    )
    if declared is None:
        problems.append("schema.py declares no SCHEMA_REVISION")
    elif declared.group(1) != head:
        problems.append(
            f"SCHEMA_REVISION is {declared.group(1)} but the head is {head}"
        )
    return problems, head


def check_indexes_live(settings) -> list[str]:  # noqa: ANN001 - Settings
    """Every tenant-filtered table must have a leading tenant_id index."""
    from xp_prompt.db import connect

    problems: list[str] = []
    wanted = ", ".join(f"'{table}'" for table in TENANT_TABLES)
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT c.relname AS table_name
            FROM pg_class c
            WHERE c.relkind = 'r'
              AND c.relnamespace = 'public'::regnamespace
              AND c.relname IN ({wanted})
              AND NOT EXISTS (
                SELECT 1
                FROM pg_index x
                JOIN pg_attribute a
                  ON a.attrelid = x.indrelid AND a.attnum = x.indkey[0]
                WHERE x.indrelid = c.oid AND a.attname = 'tenant_id'
              )
            ORDER BY 1
            """
        )
        problems = [
            f"{row['table_name']}: no index leads with tenant_id"
            for row in cursor.fetchall()
        ]
        cursor.execute("SELECT version_num FROM alembic_version")
        row = cursor.fetchone()
        applied = str((row or {}).get("version_num") or "")
    if not applied:
        problems.append("alembic_version is empty: the database was never migrated")
    return problems


def check_applied_live(settings, head: str) -> list[str]:  # noqa: ANN001
    from xp_prompt.db import connect

    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        row = cursor.fetchone()
    applied = str((row or {}).get("version_num") or "")
    if applied and applied != head:
        return [f"database is at {applied}, the code expects {head}"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the AI schema and migrations")
    parser.add_argument(
        "--live", action="store_true", help="also check the running database"
    )
    arguments = parser.parse_args()

    found = read_migrations()
    problems = check_reversible(found)
    chain_problems, head = check_chain(found)
    problems.extend(chain_problems)

    if arguments.live:
        from xp_prompt.config import Settings

        settings = Settings.from_env()
        problems.extend(check_applied_live(settings, head))
        problems.extend(check_indexes_live(settings))

    for problem in problems:
        print(f"FAIL {problem}")
    print(
        f"{len(found)} migrations, head {head or 'unknown'}, "
        f"{len(problems)} problem(s)"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
