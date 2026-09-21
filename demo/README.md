# Education reporting demo

This directory contains a deterministic, explicitly synthetic education data
pack for demonstrating XPBuilder reporting. It does not contain production
learner information. Every generated row includes
`data_origin=synthetic_demo`, and every dashboard displays the same disclosure.

## Contents

| CSV | Records |
| --- | ---: |
| `students.csv` | 60 |
| `teachers.csv` | 8 |
| `courses.csv` | 10 |
| `enrollments.csv` | 120 |
| `course_progress.csv` | 120 |
| `assessment_results.csv` | 240 |

Generate and validate the CSVs:

```bash
python3 demo/generate_education_data.py
```

Upload the CSVs through Superset's HTTP upload API, create the six curated
datasets, and build the four dashboards:

```bash
python3 demo/provision_education_demo.py
```

The provisioner reads credentials only from `.env`. It is idempotent: the six
demo tables use replacement uploads, and only artifacts carrying the education
demo marker or slugs are recreated. Unrelated datasets and dashboards are not
modified.

Created dashboard slugs:

- `education-demo-executive-overview`
- `education-demo-student-analytics`
- `education-demo-teacher-analytics`
- `education-demo-course-analytics`
