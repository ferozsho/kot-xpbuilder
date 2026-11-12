-- OneKEF KPI reporting views, academic year 2026-27 only
-- (database: auditonekef, MariaDB 11.4)
--
-- Companion to kpi_reporting_views.sql. The originals are shared by the
-- existing dashboards (Superset 84/85 -> Moodle XP dashboards #28/#29) and are
-- deliberately NOT modified: these views are the year-scoped twins used by the
-- new combined "OneKEF KPI Report (Combined 2026-27)" dashboard only.
--
-- Apply with:
--   docker exec -i kot5_mariadb mariadb -uroot -p<PASS> auditonekef < kpi_reporting_views_2627.sql
--
-- Grants already held by the reporting user:
--   GRANT SELECT, EXECUTE, SHOW VIEW ON auditonekef.* TO 'superset_ro'@'%';
--
-- ---------------------------------------------------------------------------
-- THE YEAR PREDICATE (single source of truth)
--
-- Course names for the 2026-27 academic year do not use one single shape in
-- mdl_course.fullname. Measured on 2026-11-12 (491 courses total):
--
--   2026-27_...   154 courses
--   2026_27_...   158 courses
--   2026 - 2027_...  (spaced variant, e.g. "2026 - 2027_KGS_KFS_Speak with Structure")
--
-- so the predicate below accepts "26", "2026" followed by any mix of spaces,
-- underscores and hyphens and then "27" or "2027":
--
--   20?26[ _-]*(20)?27
--
-- It matches 163 of 491 courses today. NEXT YEAR this predicate has to be
-- changed by hand (accepted with the client) in every view of this file.
-- ---------------------------------------------------------------------------


-- ---------------------------------------------------------------------------
-- KPI #1 - Course Progress % (2026-27)
-- Same shape as kpi1_course_progress; filtered on the course name.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW auditonekef.kpi1_course_progress_2627 AS
SELECT *
FROM auditonekef.kpi1_course_progress
WHERE course_name REGEXP '^20?26[ _-]*(20)?27';


-- ---------------------------------------------------------------------------
-- KPI #3 - Classroom observations (2026-27 academic year)
--
-- mdl_local_observations.course is 0 for every observation form and the
-- completion rows carry courseid 0 too, so there is no course to key a year
-- filter on. The only year signal is the observation date, so this view keeps
-- observations dated inside the 2026-27 academic year (1 Apr 2026 - 31 Mar
-- 2027). Measured 2026-11-12: 4,154 of 5,516 observations qualify.
--
-- NEXT YEAR: move the window (or replace it with a derived academic-year
-- column) in this single place.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW auditonekef.kpi3_classroom_observations_2627 AS
SELECT *
FROM auditonekef.kpi3_classroom_observations
WHERE observation_date >= '2026-04-01'
  AND observation_date <  '2027-04-01';


-- ---------------------------------------------------------------------------
-- KPI #5 - Grade and feedback against every artefact (2026-27)
-- Same shape as kpi5_artefact_grades; filtered on the course name.
-- Measured 2026-11-12: 576 of 11,265 graded submissions.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW auditonekef.kpi5_artefact_grades_2627 AS
SELECT *
FROM auditonekef.kpi5_artefact_grades
WHERE course_name REGEXP '^20?26[ _-]*(20)?27';


-- ---------------------------------------------------------------------------
-- KPI #7 - Student-wise baseline & endline, 2026-27, scoped to the teacher
--
-- Two deliberate differences from kpi7_baseline_endline_teacher:
--
-- 1. EITHER LEG IS ENOUGH. The original keeps only survey names that occur
--    with BOTH phases (HAVING COUNT(DISTINCT phase) = 2). In 2026-27 no family
--    is complete yet - the year has only just started - so that rule yields
--    ZERO rows and every panel would render empty. Measured 2026-11-12:
--      395 2026-27_NESTS EMRS_CE-FR_English_Bud Student Baseline      82 rows,   0 graded
--      422 2026-27_TRTI_Maharashtra_Marathi_Secondary_Student Baseline 3,134 graded, 54.94 %
--      429 2026_27_TRTI_STEM_Marathi_PrimaryGrade3_Base Line Survey      876 graded, 56.10 %
--      399 2026-27_PM Shri_MH_FLN_Marathi_Post Survey                   175 graded, 44.23 %
--    A family therefore needs at least ONE graded leg, in either direction.
--    Requiring the baseline (the first cut of this view) silently dropped
--    course 399 - the only graded 2026-27 endline in the database - which is
--    exactly why the "My Endline %" and "My Gain" tiles rendered "No data".
--    baseline_pct / endline_pct stay NULL for the leg that does not exist yet,
--    so gain_pct is NULL whenever either leg is missing (never a fake 0).
--
-- 2. WIDER NAME MATCHING. "Base Line" (with a space) is accepted, which the
--    original regex misses; course 429 is a 2026-27 baseline survey named
--    "..._Base Line Survey" and is invisible to kpi7_baseline_endline_teacher.
--
-- Everything else is intentionally identical to the original - same 18 output
-- columns and the same teacher attribution (most recent classroom trainers,
-- then the school's most active trainer as fallback) - because the charts are
-- copies of the existing ones and reference these column names.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW auditonekef.kpi7_baseline_endline_teacher_2627 AS
WITH crs AS (
  SELECT c.id AS course_id,
         c.fullname,
         TRIM(REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(LOWER(c.fullname),
           'base[ _-]?line|baseline|endline', '<ph>'),
           '_?pre[-_ ]?survey|pre[-_ ]session|presurvey|pre_?assessment', '<ph>'),
           '_?post[-_ ]?survey|post[-_ ]session|postsurvey|post_?assessment', '<ph>'),
           '[[:space:]]+', ' ')) AS pair_key,
         CASE WHEN LOWER(c.fullname) REGEXP 'base[ _-]?line|baseline|pre[-_ ]session|presurvey|pre_?assessment'
              THEN 'baseline' ELSE 'endline' END AS phase
  FROM mdl_course c
  WHERE c.fullname REGEXP '^20?26[ _-]*(20)?27'
    AND LOWER(c.fullname) REGEXP 'base[ _-]?line|baseline|endline|pre[-_ ]session|post[-_ ]session|presurvey|post[-_ ]?survey'
),
pr AS (
  -- One row per survey family. Both legs are optional: 422/429 only have a
  -- baseline so far, 399 only has a post (endline) leg. Keep the family as
  -- long as at least one of the two exists - `crs` has already restricted the
  -- candidates to survey courses of this academic year.
  SELECT pair_key,
         MAX(CASE WHEN phase = 'baseline' THEN course_id END) AS baseline_course_id,
         MAX(CASE WHEN phase = 'endline'  THEN course_id END) AS endline_course_id,
         MIN(CASE WHEN phase = 'baseline' THEN fullname END)  AS baseline_course,
         MIN(CASE WHEN phase = 'endline'  THEN fullname END)  AS endline_course
  FROM crs
  GROUP BY pair_key
),
pair_courses AS (
  SELECT baseline_course_id AS course_id FROM pr
  UNION
  SELECT endline_course_id  AS course_id FROM pr
),
grades AS (
  -- `pair_courses` contains a NULL row for every family that is missing one
  -- leg; NULL never matches in an IN list, so it needs no extra guard beyond
  -- the explicit IS NOT NULL filter below.
  SELECT gi.courseid, gg.userid,
         ROUND(gg.finalgrade / NULLIF(gi.grademax, 0) * 100, 1) AS pct
  FROM mdl_grade_items gi
  JOIN mdl_grade_grades gg ON gg.itemid = gi.id
  WHERE gi.itemtype = 'course'
    AND gi.grademax > 0
    AND gg.finalgrade IS NOT NULL
    AND gi.courseid IN (SELECT course_id FROM pair_courses WHERE course_id IS NOT NULL)
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
  SELECT DISTINCT x.userid, t.trainerid
  FROM (
    SELECT cu.userid, cu.classroomid,
           ROW_NUMBER() OVER (PARTITION BY cu.userid
             ORDER BY GREATEST(COALESCE(cu.timemodified, 0), COALESCE(cl.timemodified, 0)) DESC,
                      cu.classroomid DESC) AS rn
    FROM mdl_local_classroom_users cu
    JOIN mdl_local_classroom cl ON cl.id = cu.classroomid
    WHERE cu.userid IN (SELECT userid FROM stu)
  ) x
  JOIN mdl_local_classroom_trainers t ON t.classroomid = x.classroomid
  WHERE x.rn = 1
),
school_primary AS (
  -- Same fallback as the original: the school's most active trainer, for
  -- beneficiaries who never joined a classroom.
  SELECT school_id, trainerid FROM (
    SELECT tr.open_school AS school_id, x.trainerid,
           ROW_NUMBER() OVER (PARTITION BY tr.open_school
                              ORDER BY COUNT(DISTINCT cu.userid) DESC, x.trainerid ASC) AS rn
    FROM mdl_user tr
    JOIN mdl_local_classroom_trainers x ON x.trainerid = tr.id
    JOIN mdl_local_classroom_users cu  ON cu.classroomid = x.classroomid
    WHERE tr.open_school > 0
      AND tr.open_school IN (SELECT u.open_school FROM mdl_user u
                             WHERE u.id IN (SELECT userid FROM stu))
    GROUP BY tr.open_school, x.trainerid
  ) z
  WHERE rn = 1
),
teacher_link AS (
  SELECT t.userid, t.trainerid FROM teach t
  UNION ALL
  SELECT s.userid, sp.trainerid
  FROM stu s
  JOIN mdl_user su ON su.id = s.userid
  JOIN school_primary sp ON sp.school_id = su.open_school
  WHERE NOT EXISTS (SELECT 1 FROM teach t2 WHERE t2.userid = s.userid)
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
LEFT JOIN teacher_link t      ON t.userid = s.userid
LEFT JOIN mdl_user tu         ON tu.id = t.trainerid
LEFT JOIN mdl_local_school ts ON ts.id = tu.open_school;


-- ---------------------------------------------------------------------------
-- Learner variant (single mapped column: student_username), 2026-27.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW auditonekef.kpi7_learner_baseline_endline_2627 AS
SELECT pair_key, survey_label, baseline_course, endline_course,
       teacher_id, teacher_name, teacher_username, teacher_school,
       student_id, student_name, student_username, student_school,
       baseline_pct, endline_pct, gain_pct,
       teacher_avg_baseline_pct, teacher_avg_endline_pct, teacher_student_count
FROM auditonekef.kpi7_baseline_endline_teacher_2627;
