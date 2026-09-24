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

## Training programme dashboards (live Moodle data)

`provision_programme_dashboards.py` rebuilds the client's three Power BI pages
(**Teacher**, **Headmaster**, **Education Officer**) as three Superset
dashboards over one read-only virtual dataset, so every tile is aggregated live
from the connected Moodle database and changes as the data changes.

```bash
python3 reports/provision_programme_dashboards.py \
    --base-url https://kot5.example.com \
    --username admin --password '<admin password>' \
    --connection kefuat
```

The dataset is one row per *(programme participant × programme course ×
activity)*, so a single dataset feeds all three pages and every level stays
filterable (participant, school, district, course, course category, activity
kind). Programme scope is the course name/shortname filter in the SQL
(`trti`, `artefact`, `kshamata`, `ped-tech`, `fln`, `KSH*`, `%ART%`).

### Live mapping

The client's Power BI report reads a flat *Program Data* export that does not
exist in the Moodle database, so each column is derived from the closest live
equivalent:

| Report column | Live source |
| --- | --- |
| Teacher / participant | user with a programme role (`employee`, `editingteacher`, `trainer`, `teacher`) in a programme course |
| School, district, taluka | `mdl_local_school` through `mdl_user.open_school` (district/taluka are numeric IDs - the site has no lookup table) |
| Courses assigned | distinct programme courses the user is a member of |
| Courses completed | `mdl_course_completions` for those courses |
| Student enrolments | `mdl_enrol` + `mdl_user_enrolments`, counted once per course |
| Artefacts submitted | submissions of artefact activities (activity or course named *artefact/artifact*, or an `ART` course shortname) |
| Average artefact score | `mdl_assign_grades` as a percentage of the activity maximum |
| Microassessment score by concept | graded `Micro Assessment - <concept>` activities, grouped by the text after the dash |
| Classroom observations | `mdl_local_observations_users` plus *observation* activity submissions |
| BL / ML scores | baseline items (`baseline`, `pre test`, `pre course`, `pre survey`) and post items (`post`), as percentages |
| Student gain % | `(avg post − avg baseline) / avg baseline` |

### Known gaps (as of 2026-09-24, connection `kefuat`)

The dashboards are live and populated (112 programme courses, 523 participants,
1,948 student enrolments, 104 artefact submissions, 77 classroom observations,
baseline 81.0 % vs post 80.1 %), but three report areas have no data in that
database yet and therefore read empty or zero:

- **midline scores** - no item named *midline* exists; the report uses post-test
  items as the ML equivalent
- **microassessment concept scores** - the `Micro Assessment - Math/Science/
  Marathi` activities exist but only one ungraded submission, so the concept
  chart shows the concepts with no values
- **education-officer / headmaster hierarchy** - the database has no such
  records (`open_designation` is empty or `NA` for 8,364 of 8,542 users and only
  1 user has a supervisor set), so the Headmaster and Education Officer pages
  report at school and district level while keeping the client's page titles

The pages are also blocked from writing anything to the client database: the
`kefuat` account is read-only (`CREATE`/`INSERT`/`DROP` denied), which is why
the flat table is projected as SQL rather than loaded.

