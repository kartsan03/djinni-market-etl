-- Emergency rollback for 001_job_lifecycle.sql.
-- Do not run after observations become valuable unless you intentionally accept data loss.

BEGIN;

DROP TABLE IF EXISTS public.all_job_observations;
DROP TABLE IF EXISTS public.job_scrape_runs;

ALTER TABLE public.all_jobs
    DROP COLUMN IF EXISTS consecutive_complete_misses,
    DROP COLUMN IF EXISTS reopened_count,
    DROP COLUMN IF EXISTS inactive_detected_at,
    DROP COLUMN IF EXISTS last_seen_at,
    DROP COLUMN IF EXISTS first_seen_at,
    DROP COLUMN IF EXISTS status_source,
    DROP COLUMN IF EXISTS status_checked_at,
    DROP COLUMN IF EXISTS status;

COMMIT;
