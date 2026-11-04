#!/usr/bin/env python
"""Grant the XP Prompt workspace permissions to the authenticated roles.

Routes guarded by FAB's ``@has_access`` / ``@has_access_api`` need the matching
``can_<method>`` permission on the ``AIPrompt`` view. Flask-AppBuilder creates
those rows only while synchronizing roles and never grants them to existing
roles afterwards, so a metadata database whose roles predate this view answers
**403 "Access is Denied"** even for an administrator.

Unlike the AI Provider settings page (Admin only, because it holds provider
keys), the conversational workspace is for every signed-in analyst, so the grant
covers the built-in Admin, Alpha and Gamma roles.

Idempotent — safe to re-run. Apply to a running stack with no image rebuild:

    docker exec -i <instance>_superset /app/.venv/bin/python - \
        < docker/ensure_ai_prompt_permissions.py
"""

from __future__ import annotations

from sqlalchemy import text
from superset.app import create_app

from superset import db

#: ``can_<method name>`` permissions declared by AIPromptView, plus the
#: ``menu_access`` permission that makes FAB render the top-level entry.
PERMISSIONS = (
    "menu_access",
    "can_workspace",
    "can_list",
    "can_api_conversations",
    "can_api_conversation_create",
    "can_api_conversation_detail",
    "can_api_conversation_message",
    "can_api_conversation_archive",
    "can_api_conversation_reset_context",
    "can_api_conversation_jobs",
    "can_api_job_detail",
    "can_api_job_action",
    "can_api_conversation_artifacts",
    "can_api_artifact_action",
    "can_api_schema_slice",
    "can_api_sql_execute",
    "can_api_conversation_sql",
    "can_api_conversation_audit",
    "can_api_repair_metrics",
    "can_api_repair_patterns",
    "can_api_repair_pattern_action",
    "can_api_dataset_equivalents",
    "can_api_dataset_drafts",
    "can_api_dataset_draft_create",
    "can_api_dataset_draft_save",
    "can_api_dataset_draft_scrap",
    "can_api_charts_recommend",
    "can_api_chart_draft_create",
    "can_api_chart_preview",
    "can_api_chart_draft_save",
    "can_api_chart_draft_scrap",
    "can_api_dashboard_plan",
    "can_api_dashboard_layout",
    "can_api_dashboard_preview",
    "can_api_dashboard_publish",
)

#: Usage data is cross-user spend, so these are granted to Admin only. Handing a
#: Gamma user a view of every colleague's token consumption would be a privacy
#: defect dressed up as a dashboard.
ADMIN_ONLY_PERMISSIONS: tuple[str, ...] = (
    "can_api_usage_summary",
    "can_api_usage_attempts",
    "can_api_usage_reconciliation",
    "can_api_usage_prices",
    "can_api_usage_budgets",
    "can_api_usage_budget_delete",
    "can_api_usage_alerts",
    "can_api_usage_alerts_deliver",
    "can_api_retention",
    "can_api_tenant_offboard",
    "can_api_usage_outbox",
    "can_api_usage_outbox_drain",
    "can_api_usage_billing_imports",
    "can_api_usage_billing_variance",
    "can_api_usage_billing_source",
    "can_api_usage_billing_fetch",
    "can_api_settings",
    "can_api_settings_health",
    "can_api_settings_global",
    "can_api_settings_update",
    "can_api_settings_history",
    "can_api_settings_rollback",
)
VIEW_MENU = "AIPrompt"

#: Page shells that live beside the workspace. Their data is still fetched
#: through the Admin-only ``AIPrompt`` API permissions, so exposing the shell is
#: a routing fact, not a disclosure: a non-admin can open the page but every
#: request behind it answers 403. Granted to Admin only, like that data.
ADMIN_PAGES: dict[str, tuple[str, ...]] = {
    "AIUsage": ("menu_access", "can_workspace", "can_list"),
    "AISettings": ("menu_access", "can_workspace", "can_list"),
}

#: Every signed-in role may chat; ownership is enforced per conversation.
ROLES = ("Admin", "Alpha", "Gamma")
ADMIN_ROLE = "Admin"


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

        for permission in ADMIN_ONLY_PERMISSIONS:
            pvm = sm.find_permission_view_menu(permission, VIEW_MENU)
            if pvm is None:
                sm.add_permission_view_menu(permission, VIEW_MENU)
                pvm = sm.find_permission_view_menu(permission, VIEW_MENU)
                print(f"created permission {permission} on {VIEW_MENU}")
            if pvm is None:
                raise SystemExit(
                    f"could not create {permission} on {VIEW_MENU} — check migrations"
                )
            role = sm.find_role(ADMIN_ROLE)
            if role is None:
                print(f"role {ADMIN_ROLE}: not found, skipped")
                continue
            if role_has_permission(int(role.id), int(pvm.id)):
                print(f"permission {permission}: {ADMIN_ROLE} already granted")
                continue
            sm.add_permission_role(role, pvm)
            print(f"permission {permission}: {ADMIN_ROLE} granted")

        for view_name, page_permissions in ADMIN_PAGES.items():
            for permission in page_permissions:
                pvm = sm.find_permission_view_menu(permission, view_name)
                if pvm is None:
                    sm.add_permission_view_menu(permission, view_name)
                    pvm = sm.find_permission_view_menu(permission, view_name)
                    print(f"created permission {permission} on {view_name}")
                if pvm is None:
                    raise SystemExit(
                        f"could not create {permission} on {view_name} — check the view"
                    )
                role = sm.find_role(ADMIN_ROLE)
                if role is None:
                    print(f"role {ADMIN_ROLE}: not found, skipped")
                    continue
                if role_has_permission(int(role.id), int(pvm.id)):
                    print(f"permission {permission} on {view_name}: already granted")
                    continue
                sm.add_permission_role(role, pvm)
                print(f"permission {permission} on {view_name}: {ADMIN_ROLE} granted")


if __name__ == "__main__":
    main()
