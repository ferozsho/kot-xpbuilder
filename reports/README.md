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

## Teacher performance report (two pages, two sources)

`provision_teacher_performance_report.py` builds the client's reference report
twice, because its numbers live in two different places:

| Page | Slug | Source | Teacher filter lists |
| --- | --- | --- | --- |
| **Teacher Performance Report** | `teacher-performance-report` | the client's flat **Program Data** export | the client's 150 teachers (incl. *Amit Jadhav*) |
| **Teacher Performance Report (Live Moodle)** | `teacher-performance-report-live` | `kefuat` virtual datasets | the KEF trainers found in Moodle |

```bash
python3 reports/provision_teacher_performance_report.py \
    --base-url https://kot5.example.com \
    --username admin --password '<admin password>' \
    --connection kefuat \
    --program-data 'Moodle Dashboard Data (1).xlsx - Program Data.csv'
```

Superset's select filter reads its option list from **one** dataset and applies
the chosen column to every chart left in scope, so the two populations cannot
share a control; giving each its own page is what puts the client's own
teachers in the `Teacher Name` box the reference screenshot shows.

### Page 1 - the reference report (Program Data export)

The export is one row per *teacher x student* with the teacher-level figures
repeated (`courses_assigned`, `courses_completed`, `artefacts_submitted`,
`microassessment_score`, …) and one `Microassessment Concept` per student. The
script normalises the headers (`BL Score (/20)` -> `bl_score_20`, the
trailing-space `Microassessment Concept ` header, blank spacer rows) and uploads
700 rows into the deployment's writable upload database, then points all eight
tiles at it:

| Tile | Metric |
| --- | --- |
| Courses Enrolled Vs Completed | `AVG(courses_completed) / AVG(courses_assigned)` |
| Total Students | `COUNT(DISTINCT student_name)` |
| Student Gain % | `(SUM(ml_score_20) - SUM(bl_score_20)) / SUM(bl_score_20)` |
| Artefacts Submitted | `AVG(artefacts_submitted)` |
| Avg. BL / Avg. ML out of 20 | `AVG(bl_score_20)`, `AVG(ml_score_20)` |
| Microassessment Score (Out of 10) Concept Wise | `AVG(microassessment_score)` by `microassessment_concept` |
| Student Gain % (per student) | `AVG(ml_score_20 - bl_score_20) / AVG(bl_score_20)` |
| Courses Enrolled vs Completed | `AVG(courses_assigned)`, `AVG(courses_completed)` by teacher |

Filters: Teacher Name, Grade, School, Academic Year, District, Microassessment
Concept - all reading the export.

Verified in the browser with *Teacher Name = Amit Jadhav*; it reproduces the
reference screenshot value for value: **100.0 %** completion, **5** students,
**39.58 %** gain, **6** artefacts, BL **9.6** / ML **13.4** out of 20, the three
concepts at **5.0 / 5.0 / 5.0**, and the per-student chart at
**0 % / 18.2 % / 18.2 % / 75 % / 150 %**.

### Page 2 - the live Moodle view

The same report approximated from `kefuat`, so it keeps updating as the LMS
changes and no live capability was lost. It reads the `kefuat` tables below and
carries the *Micro Assessment* concept chart, which stays empty until assessors
record results.

That concept chart unions the two places a trainer can record a concept-wise
result in this LMS, both scored on a 0-100 scale:

| Store | Grain | Notes |
| --- | --- | --- |
| `mdl_grade_grades` + `mdl_grade_items` | teacher x class x student x concept | grading a concept item of a course whose name contains *micro*; concept = the item name, falling back to `mdl_assign.name` when the item is unnamed |
| `mdl_local_classroom_test_score` | teacher x class x concept | the classroom module's Test Score feature (`classroomid`, `courseid`, `testid`, `totalmarks`, `score`); concept = the course name, no student id |

The course filter is `%micro%` (not `%micro assessment%`) because the pilot
course is named `TRTI26-27MicroAssessmentPilot`, without a space - the narrower
pattern silently excluded it.

Nothing has been recorded in `kefuat` yet, which is why the tile renders its
empty state there: the ten micro-assessment courses hold **173 concept items and
not one grade** (95 `mdl_grade_grades` rows exist for them, all with a NULL
`finalgrade`; the only `mdl_assign_grades` rows carry `grade = -1`, i.e. "no
grade"), `mdl_local_classroom_test_score` holds 0 rows, and the grade *history*
tables confirm no score was ever entered and deleted. Register the client's
production Moodle - a second connection exactly like `kefuat` - or let a trainer
grade one concept item, and the tile fills by itself with no further change.

The client's reference export, by contrast, is a purpose-made mock-up rather
than an extract of this database: its 700 rows carry 700 *distinct* student names
and every teacher has exactly 4 or 5 of them, and its 60 school names and 150
teacher names match none of the 106 `mdl_local_classroom_trainers` trainers.


Not one of the export's 150 teacher names matches the 106
`mdl_local_classroom_trainers` trainers (the single `Amit Jadhav` in `mdl_user`
has no role and no classroom), so that name can never appear in this page's
`Teacher Name` list.

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
| BL score | graded *baseline* / *pre* items, as a percentage of each item's maximum |
| ML score | graded *midline* / *endline* / *post* items, as a percentage of the maximum |
| Student gain % | (sum ML - sum BL) / sum BL over the students holding both scores |
| Micro-assessment concept | graded items of the *Micro Assessment* courses |

Every figure uses `COUNT(DISTINCT ...)`, `MAX(...)` or an average over the
student grain, so joined tables cannot inflate a KPI; the course counts use
`COUNT(DISTINCT CASE WHEN completed_flag = 1 THEN course_id END)` so completion
can never exceed 100 %.

Verified: 107 teachers, 2,898 students, 8 grades, BL 66.1 % vs ML 80.5 %,
courses 96.2 % complete, 22 of 2,898 students holding both scores (gain 5.7 %).
Filters verified in the browser: teacher-only (17 students / 16.0 % gain /
3 artefacts for *Amoolya Shenvi*), teacher + grade (40 students for *Harshada
Zode* + Grade 5), grade cascade (8 grades -> 2 after picking a teacher) and
clear-all.

### The Program Data export

The micro-assessment scores exist **only** in it. In `kefuat` the ten
micro-assessment courses (*TRTI Micro Assessment*, *FLN Micro Assessment
Marathi/Gujarati Grade 1-4*, *TRTI 26-27 Micro Assessment Pilot*) hold 173
concept items with **zero graded records** and three ungraded submissions, and
the tables that would normally hold such scores are all empty
(`mdl_local_classroom_test_score`, `mdl_local_program_test_score`,
`mdl_local_classroom_trainerfb`, `mdl_local_program_trainerfb`,
`mdl_local_performance_overall`, `mdl_local_skillmatrix`). No column in the
schema mentions a concept except `mdl_glossary_entries.concept`, and Report
Builder holds only generic Moodle reports.

Upload notes: `POST /api/v1/database/<id>/upload/` writes into the
`File uploads` connection (`xpbuilder_uploads`, schema **`public`** -
`parameters.database` is the database name, not the schema). Superset's
`UploadCommand` already registers the dataset, so the script looks it up by
table name instead of creating a duplicate (which fails 422). The upload only
works when `extra.schemas_allowed_for_file_upload` contains the schema; the REST
API does not return `extra`, so the script re-writes that allowlist every run.
The table is a static snapshot, so a re-run **without** `--program-data` reuses
what was uploaded last time and only fails if there is no table to fall back on.

### Remaining gaps

- **live micro-assessment scores** - the LMS activities carry no grades, so page
  2's concept chart renders an empty state; the export-backed page covers it.
- **no 20-point scale in the LMS** - live BL/ML items have no common maximum
  (41, 20, 15, 10 …), so page 2 shows a percentage of each item's maximum;
  page 1 reports the export's own /20 scores.
- **only 22 students hold both a live baseline and a post score**, so page 2's
  gain card is populated for a few teachers only (*No data* otherwise).
- **no academic year in Moodle** - live filtering uses the last assessment date;
  *Academic Year* exists only in the export.
- **single-value KPI card** - Superset's big-number tile holds one metric, so
  the "3 / 3" ratio is carried by the courses chart while the card shows the
  completion percentage.
