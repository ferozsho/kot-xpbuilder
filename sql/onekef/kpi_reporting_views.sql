-- OneKEF KPI reporting views (database: auditonekef, MariaDB 11.4)
--
-- These views back the Superset datasets used by the "OneKEF KPI Report"
-- dashboard (KPI #1, KPI #3, KPI #7). They are read-only wrappers over
-- Moodle core/plugin tables; nothing is written to Moodle.
--
-- Apply with:
--   docker exec -i kot5_mariadb mariadb -uroot -p<PASS> auditonekef < kpi_reporting_views.sql
--
-- Grants already held by the reporting user:
--   GRANT SELECT, EXECUTE, SHOW VIEW ON auditonekef.* TO 'superset_ro'@'%';

-- ---------------------------------------------------------------------------
-- KPI #1 - Course Progress %
-- One row per visible course: enrolled beneficiaries, beneficiaries with a
-- finished course-completion record, and average learner module progress.
--
-- mdl_course_completions.timecompleted IS NULL means "not completed yet"
-- (Moodle keeps the row while aggregation is pending), so completion is
-- counted with timecompleted > 0.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW auditonekef.kpi1_course_progress AS
WITH cm_totals AS (
  SELECT cm.course AS course_id, COUNT(*) AS total_modules
  FROM mdl_course_modules cm
  WHERE cm.deletioninprogress = 0
  GROUP BY cm.course
),
cm_done AS (
  SELECT cm.course AS course_id, cmc.userid, COUNT(*) AS modules_completed
  FROM mdl_course_modules_completion cmc
  JOIN mdl_course_modules cm ON cm.id = cmc.coursemoduleid AND cm.deletioninprogress = 0
  WHERE cmc.completionstate > 0
  GROUP BY cm.course, cmc.userid
),
learner AS (
  SELECT e.courseid AS course_id, ue.userid,
         MAX(CASE WHEN cc.timecompleted > 0 THEN 1 ELSE 0 END) AS course_completed,
         COALESCE(MAX(d.modules_completed), 0) AS modules_completed
  FROM mdl_enrol e
  JOIN mdl_user_enrolments ue ON ue.enrolid = e.id AND ue.status = 0
  JOIN mdl_user u ON u.id = ue.userid AND u.deleted = 0 AND u.suspended = 0
  LEFT JOIN mdl_course_completions cc ON cc.course = e.courseid AND cc.userid = ue.userid
  LEFT JOIN cm_done d ON d.course_id = e.courseid AND d.userid = ue.userid
  GROUP BY e.courseid, ue.userid
)
SELECT
  c.id                                                      AS course_id,
  c.fullname                                                AS course_name,
  c.shortname                                               AS course_code,
  COALESCE(cat.name, 'Uncategorised')                       AS course_category,
  COUNT(l.userid)                                           AS enrolled_beneficiaries,
  SUM(l.course_completed)                                   AS completed_beneficiaries,
  ROUND(100 * SUM(l.course_completed) / NULLIF(COUNT(l.userid), 0), 1) AS progress_pct,
  COALESCE(MAX(t.total_modules), 0)                         AS total_modules,
  ROUND(AVG(l.modules_completed), 1)                        AS avg_modules_completed,
  ROUND(100 * AVG(LEAST(1, l.modules_completed / NULLIF(t.total_modules, 0))), 1)
                                                            AS module_progress_pct
FROM mdl_course c
JOIN learner l                      ON l.course_id = c.id
LEFT JOIN cm_totals t               ON t.course_id = c.id
LEFT JOIN mdl_course_categories cat ON cat.id = c.category
WHERE c.visible = 1
GROUP BY c.id;

-- ---------------------------------------------------------------------------
-- KPI #3 - Score and feedback for every classroom observation
-- One row per submitted classroom observation response
-- (formerly the "BBB grading sheet"): observed teacher, evaluator, rubric
-- score (average of the multichoicerated rubric items, 0-5) and the
-- qualitative remarks captured in the textarea items.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW auditonekef.kpi3_classroom_observations AS
WITH scored AS (
  SELECT v.completed,
         COUNT(*) AS rated_items,
         ROUND(AVG(CAST(v.value AS DECIMAL(6,2))), 2) AS avg_score
  FROM mdl_local_observations_value v
  JOIN mdl_local_observations_item i ON i.id = v.item AND i.typ = 'multichoicerated'
  WHERE v.value IS NOT NULL AND TRIM(v.value) <> ''
  GROUP BY v.completed
),
feedback AS (
  SELECT v.completed,
         GROUP_CONCAT(TRIM(v.value) ORDER BY i.position SEPARATOR ' | ') AS feedback
  FROM mdl_local_observations_value v
  JOIN mdl_local_observations_item i ON i.id = v.item AND i.typ = 'textarea'
  WHERE v.value IS NOT NULL AND TRIM(v.value) <> ''
  GROUP BY v.completed
)
SELECT
  oc.id                                        AS observation_response_id,
  oc.observations                              AS observation_form_id,
  o.name                                       AS observation_form,
  oc.userid                                    AS teacher_id,
  TRIM(CONCAT(COALESCE(u.firstname, ''), ' ', COALESCE(u.lastname, ''))) AS teacher_name,
  u.username                                   AS teacher_username,
  COALESCE(s.school_name, '(not set)')         AS teacher_school,
  oc.evaluatedby                               AS evaluator_id,
  TRIM(CONCAT(COALESCE(ev.firstname, ''), ' ', COALESCE(ev.lastname, ''))) AS evaluator_name,
  DATE(FROM_UNIXTIME(oc.timemodified))         AS observation_date,
  COALESCE(sc.rated_items, 0)                  AS rated_items,
  ROUND(COALESCE(sc.avg_score, 0), 2)          AS avg_score,
  ROUND(100 * COALESCE(sc.avg_score, 0) / 5, 1) AS score_pct,
  REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(fb.feedback, ''),
    '&#039;', ''''), '&amp;', '&'), '&quot;', '"'), '&nbsp;', ' '), '&lt;', '<'), '&gt;', '>')
                                               AS feedback
FROM mdl_local_observations_completed oc
JOIN mdl_local_observations o  ON o.id = oc.observations
JOIN mdl_user u                ON u.id = oc.userid
LEFT JOIN mdl_user ev          ON ev.id = oc.evaluatedby
LEFT JOIN mdl_local_school s   ON s.id = u.open_school
LEFT JOIN scored sc            ON sc.completed = oc.id
LEFT JOIN feedback fb          ON fb.completed = oc.id;

-- ---------------------------------------------------------------------------
-- KPI #7 - Student-wise baseline & endline average, scoped to the teacher
--
-- Survey pairs are detected from the course name: the phase keyword
-- (baseline / endline / pre-session / post-session) is normalised away and
-- only names that occur with BOTH phases are kept, so a lone "Baseline"
-- course never produces a half-empty report.
--
-- Teacher assignment: the student's most recently active classroom trainer
-- (mdl_local_classroom_users -> mdl_local_classroom_trainers). One teacher per
-- student keeps the grain at teacher x student, so a teacher's
-- teacher_avg_* / teacher_student_count describe exactly their own cohort.
-- The window averages are computed before any row-level (RLS) filter, so a
-- teacher-scoped view still shows that teacher's true averages.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW auditonekef.kpi7_baseline_endline_teacher AS
WITH crs AS (
  SELECT c.id AS course_id,
         c.fullname,
         TRIM(REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(LOWER(c.fullname),
           'baseline|endline', '<ph>'),
           '_?pre[-_ ]?survey|pre[-_ ]session|presurvey|pre_?assessment', '<ph>'),
           '_?post[-_ ]?survey|post[-_ ]session|postsurvey|post_?assessment', '<ph>'),
           '[[:space:]]+', ' ')) AS pair_key,
         CASE WHEN LOWER(c.fullname) REGEXP 'baseline|pre[-_ ]session|presurvey|pre_?assessment'
              THEN 'baseline' ELSE 'endline' END AS phase
  FROM mdl_course c
  WHERE LOWER(c.fullname) REGEXP 'baseline|endline|pre[-_ ]session|post[-_ ]session|presurvey|post[-_ ]?survey'
),
pr AS (
  SELECT pair_key,
         MAX(CASE WHEN phase = 'baseline' THEN course_id END) AS baseline_course_id,
         MAX(CASE WHEN phase = 'endline'  THEN course_id END) AS endline_course_id,
         MIN(CASE WHEN phase = 'baseline' THEN fullname END)  AS baseline_course,
         MIN(CASE WHEN phase = 'endline'  THEN fullname END)  AS endline_course
  FROM crs
  GROUP BY pair_key
  HAVING COUNT(DISTINCT phase) = 2
),
pair_courses AS (
  SELECT baseline_course_id AS course_id FROM pr
  UNION
  SELECT endline_course_id  AS course_id FROM pr
),
grades AS (
  SELECT gi.courseid, gg.userid,
         ROUND(gg.finalgrade / NULLIF(gi.grademax, 0) * 100, 1) AS pct
  FROM mdl_grade_items gi
  JOIN mdl_grade_grades gg ON gg.itemid = gi.id
  WHERE gi.itemtype = 'course'
    AND gi.grademax > 0
    AND gg.finalgrade IS NOT NULL
    AND gi.courseid IN (SELECT course_id FROM pair_courses)
),
stu AS (
  SELECT pr.pair_key, pr.baseline_course, pr.endline_course, g.userid,
         MAX(CASE WHEN g.courseid = pr.baseline_course_id THEN g.pct END) AS baseline_pct,
         MAX(CASE WHEN g.courseid = pr.endline_course_id  THEN g.pct END) AS endline_pct
  FROM grades g
  JOIN pr ON g.courseid IN (pr.baseline_course_id, pr.endline_course_id)
  GROUP BY pr.pair_key, pr.baseline_course, pr.endline_course, g.userid
),
teach AS (
  SELECT userid, trainerid FROM (
    SELECT cu.userid, t.trainerid,
           ROW_NUMBER() OVER (PARTITION BY cu.userid
             ORDER BY GREATEST(COALESCE(cu.timemodified, 0), COALESCE(cl.timemodified, 0)) DESC,
                      cu.classroomid DESC) AS rn
    FROM mdl_local_classroom_users cu
    JOIN mdl_local_classroom_trainers t ON t.classroomid = cu.classroomid
    JOIN mdl_local_classroom cl ON cl.id = cu.classroomid
    WHERE cu.userid IN (SELECT userid FROM stu)
  ) x
  WHERE rn = 1
)
SELECT
  s.pair_key,
  TRIM(TRAILING '_' FROM TRIM(SUBSTRING_INDEX(s.pair_key, '<ph>', 1))) AS survey_label,
  s.baseline_course,
  s.endline_course,
  COALESCE(t.trainerid, 0)                                            AS teacher_id,
  COALESCE(NULLIF(TRIM(CONCAT(COALESCE(tu.firstname, ''), ' ', COALESCE(tu.lastname, ''))), ''),
           'Unassigned')                                              AS teacher_name,
  COALESCE(tu.username, '(unassigned)')                               AS teacher_username,
  COALESCE(ts.school_name, '(not set)')                               AS teacher_school,
  s.userid                                                            AS student_id,
  TRIM(CONCAT(COALESCE(u.firstname, ''), ' ', COALESCE(u.lastname, ''))) AS student_name,
  u.username                                                          AS student_username,
  COALESCE(ss.school_name, '(not set)')                               AS student_school,
  s.baseline_pct,
  s.endline_pct,
  ROUND(s.endline_pct - s.baseline_pct, 1)                            AS gain_pct,
  ROUND(AVG(s.baseline_pct) OVER (PARTITION BY s.pair_key, COALESCE(t.trainerid, 0)), 1)
                                                                      AS teacher_avg_baseline_pct,
  ROUND(AVG(s.endline_pct)  OVER (PARTITION BY s.pair_key, COALESCE(t.trainerid, 0)), 1)
                                                                      AS teacher_avg_endline_pct,
  COUNT(*)                  OVER (PARTITION BY s.pair_key, COALESCE(t.trainerid, 0))
                                                                      AS teacher_student_count
FROM stu s
JOIN mdl_user u               ON u.id = s.userid
LEFT JOIN mdl_local_school ss ON ss.id = u.open_school
LEFT JOIN teach t             ON t.userid = s.userid
LEFT JOIN mdl_user tu         ON tu.id = t.trainerid
LEFT JOIN mdl_local_school ts ON ts.id = tu.open_school;
