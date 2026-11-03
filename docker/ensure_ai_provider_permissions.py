#!/usr/bin/env python
"""Grant the AI Provider settings permissions to the administrator role.

Routes guarded by FAB's ``@has_access`` / ``@has_access_api`` need the matching
``can_<method>`` permission on the ``AIProvider`` view. Flask-AppBuilder creates
those rows only while synchronizing roles, and it never grants them to existing
roles afterwards — so on a metadata database whose roles predate this view the
settings page answers **403 "Access is Denied"** even for an administrator.

This script creates the missing permission-view-menu rows and grants them to the
``Admin`` role only: provider API keys are a platform-level secret, so Alpha and
Gamma intentionally receive no access.

Idempotent — safe to re-run. Apply to a running stack with no image rebuild:

    docker exec -i <instance>_superset /app/.venv/bin/python - \
        < docker/ensure_ai_provider_permissions.py

The grant lives in the Superset metadata DB, so it survives container
recreations; a rebuilt image only changes the script's availability inside the
container (``/opt/xpbuilder/bin/``).
"""

from __future__ import annotations

from sqlalchemy import text

from superset import db
from superset.app import create_app
#: ``can_<method name>`` permissions declared by AIProviderView, plus the
#: ``menu_access`` permission that makes FAB render the Settings -> Manage
#: entry. Without ``menu_access`` the route works but the link stays hidden.
PERMISSIONS = (
    "menu_access",
    "can_settings",
    "can_list",
    "can_api_providers",
    "can_api_provider_types",
    "can_api_provider_create",
    "can_api_provider_update",
    "can_api_provider_delete",
    "can_api_provider_test",
)
VIEW_MENU = "AIProvider"

#: Only platform administrators may read or change provider credentials.
ROLES = ("Admin",)


def role_has_permission(role_id: int, permission_view_id: int) -> bool:
    """Is the permission-view-menu already associated with the role?"""
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
