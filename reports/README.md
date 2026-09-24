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

## Teacher performance report (teacher x grade x student)

`provision_teacher_performance_report.py` builds a fourth live page, the
**Teacher Performance Report**, from the client's reference screenshot: a
teacher selector with a cascading grade selector, a four-card KPI row, a
grouped BL/ML score chart, a concept-wise micro-assessment chart and a
per-student gain chart.

```bash
python3 reports/provision_teacher_performance_report.py \
    --base-url https://kot5.example.com \
    --username admin --password '<admin password>' \
    --connection kefuat \
    --program-data 'Moodle Dashboard Data (1).xlsx - Program Data.csv'
```

Three live virtual datasets plus the client's flat export feed it, so a single
dashboard can mix teacher-level, student-level and concept-level numbers
without double counting:

| Dataset | Source | Grain | Feeds |
| --- | --- | --- | --- |
| `Teacher Student Performance` | `kefuat` (SQL) | teacher x class x student | students, gain, artefacts, BL/ML |
| `Teacher Course Report` | `kefuat` (SQL) | teacher x course | courses assigned/completed |
| `Teacher Concept Assessment` | `kefuat` (SQL) | teacher x class x student x concept | live micro-assessment concepts (`--concept-source moodle`) |
| `program_data_teacher_report` | uploaded Program Data CSV | teacher x student | micro-assessment concept chart |

### Live mapping

The reference reads a flat *Program Data* export that the Moodle database does
not contain, so each element is derived from the closest live equivalent:

| Report element | Live source |
| --- | --- |
| Teacher | `mdl_local_classroom_trainers.trainerid` -> `mdl_user` |
| Grade | `mdl_local_classroom.open_standard` -> `mdl_local_standard` |
| Class / section | `mdl_local_classroom.name` |
| School | `mdl_local_classroom.open_school` -> `mdl_local_school` |
| Student | `mdl_local_classroom_users.userid` -> `mdl_user` |
| Courses assigned | courses the teacher is enrolled in (union of `mdl_enrol`/`mdl_user_enrolments` and `mdl_course_completions`) |
| Courses completed | `mdl_course_completions` for those courses |
| Artefacts submitted | the teacher's submitted `mdl_assign_submission` rows |
| BL score | the class students' graded *baseline* / *pre* items, as a percentage of each item's maximum |
| ML score | the class students' graded *midline* / *endline* / *post* items, as a percentage of the maximum |
| Student gain % | (sum ML − sum BL) / sum BL over the students holding both scores |
| Micro-assessment concept | the client's Program Data export (see below) |

Every figure uses `COUNT(DISTINCT ...)`, `MAX(...)` or an average over the
student grain, so the joined tables cannot inflate a KPI; the course counts use
`COUNT(DISTINCT CASE WHEN completed_flag = 1 THEN course_id END)` so the
completion rate can never exceed 100 %. The gain card deliberately uses the
reference's aggregate formula (matching set of students only), while the
per-student chart uses each student's own ratio.

### The Program Data export (why the concept chart reads a file)

The micro-assessment scores exist **only** in the client's flat *Program Data*
export, not in Moodle. In `kefuat` the ten micro-assessment courses
(*TRTI Micro Assessment*, *FLN Micro Assessment Marathi/Gujarati Grade 1-4*,
*TRTI 26-27 Micro Assessment Pilot*) hold 173 concept items with **zero graded
records** and three ungraded submissions, and the tables that would normally
hold such scores are all empty (`mdl_local_classroom_test_score`,
`mdl_local_program_test_score`, `mdl_local_classroom_trainerfb`,
`mdl_local_program_trainerfb`, `mdl_local_performance_overall`,
`mdl_local_skillmatrix`). No column in the whole schema mentions a concept
except `mdl_glossary_entries.concept`, and Report Builder holds only generic
Moodle reports.

The export describes a **different population** from the LMS - none of its 60
schools exists in `kefuat` and only two of its 150 teacher names match - so it
is uploaded as its own dataset (`700` rows) and given its own filter group:

- live controls (Teacher Name, Grade, Class / Section, School, Course,
  Assessment Date) exclude the export chart
- *Program Data Teacher* and *Academic Year* exclude every live chart

Without that separation a native filter would empty the other source entirely,
because a filter's payload carries a column name but no dataset, so every chart
left in scope applies it to its own data. (In the filter config `scope.excluded`
takes chart **ids**, not `CHART-<id>` layout keys.)

The upload needs the target schema allow-listed in the connection's
`schemas_allowed_for_file_upload`; the API does not return `extra`, so the
script re-writes that allowlist on every run.

### Verified against the live database (2026-09-24)

The datasets are cross-checked by running the same SQL in SQL Lab:

- 107 teachers, 2,898 students, 8 grades, 7,014 dataset rows
- BL 66.1 % vs ML 80.5 %, student gain 5.7 % (22 students hold both scores)
- courses 96.2 % complete (106 assigned courses, 102 completed)
- micro-assessment concepts: 12, averaging 5.2-6.4 out of 10 from the export;
  filtered to *Amit Jadhav* it returns exactly the reference screenshot's three
  bars (*Addition & Subtraction*, *Place Value*, *Reading Comprehension*) at 5.0
- filters verified in the browser: teacher-only (17 students / 16.0 % gain /
  3 artefacts for *Amoolya Shenvi*), teacher + grade (40 students for *Harshada
  Zode* + Grade 5), grade cascade (8 grades -> 2 after picking a teacher),
  clear-all (both action buttons disable and every tile resets), and the two
  filter groups leave each other's charts untouched

### Known gaps (as of 2026-09-24, connection `kefuat`)

- **micro-assessment scores in the LMS** - the *FLN Micro Assessment ...
  Grade 1-4* and *TRTI Micro Assessment* courses carry 173 concept items, but
  not one has a grade, so the live variant of the concept chart
  (`--concept-source moodle`) renders an empty state. The dashboard therefore
  reads the client's export for that one tile; switch the flag once the
  assessors start grading in Moodle.
- **no 20-point scale** - the reference reports BL/ML "out of 20"; the live
  items have no common maximum (41, 20, 15, 10 …), so both scores are shown as a
  percentage of each assessment's own maximum and the chart is titled
  "Avg. BL Score and Avg. ML Score" instead.
- **sparse BL/ML coverage** - only 22 of the 2,898 students in a trainer-led
  class hold both a baseline and a post score, so the gain card is populated for
  a few teachers only (it shows *No data* rather than a misleading zero).
- **no academic year in Moodle** - the live date filter uses the last assessment
  date; *Academic Year* is available only through the export.
- **single-value KPI card** - Superset's big-number tile holds one metric, so
  card 1 shows the completion percentage and the "3 / 3" ratio is carried by the
  "Courses Enrolled vs Courses Completed" chart.
- **artefacts** - the site's artefact-named activities have no submissions from
  classroom trainers, so "Artefacts Submitted" counts every assignment the
  teacher has submitted.

