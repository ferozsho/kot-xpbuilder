#!/usr/bin/env python
"""Grant the Report Designer's guarded API permissions to the standard roles.

Routes guarded by FAB's ``@has_access_api`` need the matching view-menu
permission on ``ReportDesigner`` (``can_<method name>``):

* ``POST /reportdesigner/api/sync-tables/`` → ``can_api_sync_tables`` (the
  Report Designer's *Sync tables* action, only registered when
  ``XPBUILDER_MOODLE_INTEGRATION=true``);
* ``POST /reportdesigner/api/prepare_table/`` → ``can_api_prepare_table`` (the
  Moodle "Sync DB" pass creates each staging table from the source schema
  before any rows are copied).

Flask-AppBuilder creates those rows only when the permission sync runs, and it
never grants them to existing roles afterwards — so on a deployment whose role
rows predate the route (or were built from an older image) the endpoint answers
**403 "Access is Denied"** for every user, Admin included, and the reporting
tables can never be registered as datasets:

    $ curl -X POST .../reportdesigner/api/sync-tables/ -d '{"database_id": 2}'
    {"message":"Access is Denied","severity":"danger"}

This script creates the missing permission-view-menu rows and grants them to
the roles that already hold the other Report Designer API permissions
(Admin/Alpha/Gamma, matching ``can_api_datasets`` & friends).

Idempotent — safe to re-run. Apply to a running stack with no image rebuild:

    docker exec -i <instance>_superset /app/.venv/bin/python - \
        < docker/ensure_report_designer_permissions.py

The grant lives in the Superset metadata DB, so it survives container
recreations; a rebuilt image only changes the script's availability inside the
container (``/opt/xpbuilder/bin/``).

See docs/configuration.md ("Syncing the reporting database") for the operator
view.
"""

from __future__ import annotations

from sqlalchemy import text

from superset import db
from superset.app import create_app

#: Permissions declared by ReportDesignerView (see its base_permissions) and the
#: view they belong to.
PERMISSIONS = (
    "can_api_sync_tables",
    "can_api_prepare_table",
)
VIEW_MENU = "ReportDesigner"

#: Roles that hold every other Report Designer API permission today.
ROLES = ("Admin", "Alpha", "Gamma")


def role_has_permission(role_id: int, permission_view_id: int) -> bool:
    """Is the permission-view-menu already associated with the role?

    Read straight from FAB's association table: ``Role.permissions`` /
    ``SecurityManager.get_role_permissions()`` changed shape between
    Flask-AppBuilder majors (tuples vs objects), the table did not.
    """
    row = db.session.execute(
        text(
            "SELECT 1 FROM ab_permission_view_role "
            "WHERE role_id = :role AND permission_view_id = :pvm"
        ),
        {"role": role_id, "pvm": permission_view_id},
    ).first()
    return row is not None


def main() -> None:
    app = create_app()
    with app.app_context():
        sm = app.appbuilder.sm

        for permission in PERMISSIONS:
            pvm = sm.find_permission_view_menu(permission, VIEW_MENU)
            if pvm is None:
                sm.add_permission_view_menu(permission, VIEW_MENU)
                pvm = sm.find_permission_view_menu(permission, VIEW_MENU)
                print(f"created permission {permission} on {VIEW_MENU}")
            if pvm is None:
                raise SystemExit(
                    f"could not create {permission} on {VIEW_MENU} — check migrations"
                )
            print(f"permission {permission} on {VIEW_MENU} is present (id={pvm.id})")

            for name in ROLES:
                role = sm.find_role(name)
                if role is None:
                    print(f"role {name}: not found, skipped")
                    continue
                if role_has_permission(int(role.id), int(pvm.id)):
                    print(f"role {name}: already granted")
                    continue
                sm.add_permission_role(role, pvm)
                print(f"role {name}: granted")


if __name__ == "__main__":
    main()
