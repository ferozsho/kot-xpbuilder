# Reports

Provisioners that build curated datasets, charts, and dashboards in a running
XPBuilder stack through the Superset REST API. Unlike `../demo`, these reports
are aimed at real client data, so they are strictly read-only against the
connected database: the dataset is a **virtual dataset** defined as SQL, and no
table is created, altered, or dropped.

## Moodle user report

`provision_moodle_user_report.py` builds a **User Report** dashboard on an
existing Superset connection that holds Moodle tables (`mdl_user`,
`mdl_role_assignments`, `mdl_user_enrolments`, `mdl_enrol`, `mdl_role`).

The dataset is one row per non-deleted, non-guest user, with the primary role,
the full role list, activity status, and course enrolment count derived in SQL.
Because the grain is one row per user, summing a `*_flag` column gives a user
count directly.

Contents:

- six KPI tiles: total users, learners, active in the last 30 days, never logged
  in, new in the last 30 days, with course enrolment
- users by role, new users per month, users by activity status, users by
  authentication method, never logged in by role, users by account status
- a user detail table
- dashboard-wide filters: signup date, role, activity, account status, auth
  method

```bash
python3 reports/provision_moodle_user_report.py \
    --base-url https://kot5.example.com \
    --username admin --password '<admin password>' \
    --database <Superset connection name>
```

Without `--base-url`, the script reads `XPBUILDER_HOST_PORT`,
`SUPERSET_ADMIN_USERNAME`, and `SUPERSET_ADMIN_PASSWORD` from `--env-file`
(default `./.env`). `--schema` overrides the connection's default schema.

The script is idempotent: it deletes and recreates only the artifacts it owns,
matched by the dashboard slug `moodle-user-report` and the
`XPBuilder Moodle user report` marker in chart descriptions.

Notes:

- Roles are ranked in the SQL so that a user with several roles is counted once,
  under their highest-privilege role. A user with no role at all is reported as
  `No role` rather than dropped.
- A site that renames the custom `open_*` columns only needs the SQL edited.
- `gender_code` appears in the detail table but is charted nowhere: its numeric
  codes are a site-specific customization with no lookup table in the database.
- Dates are derived from Moodle's Unix timestamps, which are stored in UTC.
