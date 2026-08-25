-- Run once in DBeaver before 001_job_lifecycle.sql.
-- This is an in-database safety snapshot; it does not replace an external pg_dump.

BEGIN;

CREATE SCHEMA IF NOT EXISTS backup;

DO $$
BEGIN
    IF to_regclass('backup.all_jobs_before_lifecycle_20260729') IS NOT NULL THEN
        RAISE EXCEPTION
            'Backup table already exists. Stop and inspect it instead of silently reusing it.';
    END IF;
END
$$;

CREATE TABLE backup.all_jobs_before_lifecycle_20260729
    (LIKE public.all_jobs INCLUDING ALL);

INSERT INTO backup.all_jobs_before_lifecycle_20260729
SELECT *
FROM public.all_jobs;

DO $$
DECLARE
    source_count BIGINT;
    backup_count BIGINT;
    source_checksum TEXT;
    backup_checksum TEXT;
BEGIN
    SELECT COUNT(*), md5(string_agg(md5(to_jsonb(job)::text), '' ORDER BY job.id))
    INTO source_count, source_checksum
    FROM public.all_jobs AS job;

    SELECT COUNT(*), md5(string_agg(md5(to_jsonb(job)::text), '' ORDER BY job.id))
    INTO backup_count, backup_checksum
    FROM backup.all_jobs_before_lifecycle_20260729 AS job;

    IF source_count <> backup_count OR source_checksum <> backup_checksum THEN
        RAISE EXCEPTION
            'Backup verification failed: source count/checksum %/%, backup %/%',
            source_count,
            source_checksum,
            backup_count,
            backup_checksum;
    END IF;
END
$$;

COMMIT;

SELECT
    (SELECT COUNT(*) FROM public.all_jobs) AS source_rows,
    (SELECT COUNT(*) FROM backup.all_jobs_before_lifecycle_20260729) AS backup_rows,
    (
        SELECT md5(string_agg(md5(to_jsonb(job)::text), '' ORDER BY job.id))
        FROM public.all_jobs AS job
    ) AS source_checksum,
    (
        SELECT md5(string_agg(md5(to_jsonb(job)::text), '' ORDER BY job.id))
        FROM backup.all_jobs_before_lifecycle_20260729 AS job
    ) AS backup_checksum;
