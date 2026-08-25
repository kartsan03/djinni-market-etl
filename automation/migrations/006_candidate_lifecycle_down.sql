-- Destructive rollback to the exact pre-lifecycle candidate snapshots.
-- This intentionally removes every candidate and observation added after migration 006.

BEGIN;

DO $$
BEGIN
    IF to_regclass('backup.all_candidates_before_lifecycle_20260731') IS NULL
       OR to_regclass('backup.all_candidate_observations_before_lifecycle_20260731') IS NULL
       OR to_regclass('backup.candidate_backup_manifest') IS NULL THEN
        RAISE EXCEPTION 'Verified sealed candidate backups are missing; rollback refused.';
    END IF;
END
$$;

LOCK TABLE public.all_candidates IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.all_candidate_observations IN ACCESS EXCLUSIVE MODE;
LOCK TABLE backup.all_candidates_before_lifecycle_20260731 IN ACCESS SHARE MODE;
LOCK TABLE backup.all_candidate_observations_before_lifecycle_20260731 IN ACCESS SHARE MODE;
LOCK TABLE backup.candidate_backup_manifest IN ACCESS SHARE MODE;

DO $$
DECLARE
    candidate_count BIGINT;
    candidate_checksum TEXT;
    observation_count BIGINT;
    observation_checksum TEXT;
BEGIN
    SELECT COUNT(*),md5(string_agg(md5(to_jsonb(row_data)::text), '' ORDER BY row_data.id))
    INTO candidate_count,candidate_checksum
    FROM backup.all_candidates_before_lifecycle_20260731 AS row_data;
    SELECT COUNT(*),md5(string_agg(md5(to_jsonb(row_data)::text), '' ORDER BY row_data.id))
    INTO observation_count,observation_checksum
    FROM backup.all_candidate_observations_before_lifecycle_20260731 AS row_data;

    IF NOT EXISTS (
        SELECT 1 FROM backup.candidate_backup_manifest
        WHERE backup_name='all_candidates_before_lifecycle_20260731'
          AND row_count=candidate_count AND full_row_checksum=candidate_checksum
    ) OR NOT EXISTS (
        SELECT 1 FROM backup.candidate_backup_manifest
        WHERE backup_name='all_candidate_observations_before_lifecycle_20260731'
          AND row_count=observation_count AND full_row_checksum=observation_checksum
    ) THEN
        RAISE EXCEPTION 'Candidate rollback snapshot integrity check failed.';
    END IF;
END
$$;

DROP VIEW IF EXISTS public.v_candidate_dynamics;
DROP VIEW IF EXISTS public.v_candidates_current;

DROP TRIGGER IF EXISTS candidate_observations_append_only
    ON public.all_candidate_observations;
DROP FUNCTION IF EXISTS public.reject_candidate_observation_mutation();
ALTER TABLE public.all_candidate_observations OWNER TO admin;
ALTER SEQUENCE public.all_candidate_observations_id_seq OWNER TO admin;

TRUNCATE TABLE public.all_candidate_observations, public.all_candidates RESTART IDENTITY CASCADE;

ALTER TABLE public.all_candidate_observations
    DROP COLUMN IF EXISTS run_id,
    DROP COLUMN IF EXISTS observation_type,
    DROP COLUMN IF EXISTS status,
    DROP COLUMN IF EXISTS status_source,
    DROP COLUMN IF EXISTS position_on_page,
    DROP COLUMN IF EXISTS global_rank,
    DROP COLUMN IF EXISTS http_status,
    DROP COLUMN IF EXISTS parse_ok,
    DROP COLUMN IF EXISTS parse_error,
    DROP COLUMN IF EXISTS parser_version;

ALTER TABLE public.all_candidate_observations
    ALTER COLUMN views_count SET DEFAULT 0,
    ALTER COLUMN views_count SET NOT NULL;

ALTER TABLE public.all_candidates
    DROP COLUMN IF EXISTS status,
    DROP COLUMN IF EXISTS status_source,
    DROP COLUMN IF EXISTS status_checked_at,
    DROP COLUMN IF EXISTS inactive_detected_at,
    DROP COLUMN IF EXISTS reopened_count,
    DROP COLUMN IF EXISTS last_listing_seen_at,
    DROP COLUMN IF EXISTS last_detail_checked_at,
    DROP COLUMN IF EXISTS detail_scraped_at,
    DROP COLUMN IF EXISTS engagement_observed_at,
    DROP COLUMN IF EXISTS monitoring_cohort,
    DROP COLUMN IF EXISTS monitoring_enabled,
    DROP COLUMN IF EXISTS next_confirmation_due_at,
    DROP COLUMN IF EXISTS lifecycle_data_quality,
    DROP COLUMN IF EXISTS lifecycle_tracking_started_at,
    DROP COLUMN IF EXISTS entry_left_censored,
    DROP COLUMN IF EXISTS last_parser_version;

INSERT INTO public.all_candidates
SELECT * FROM backup.all_candidates_before_lifecycle_20260731
ORDER BY id;

INSERT INTO public.all_candidate_observations
SELECT * FROM backup.all_candidate_observations_before_lifecycle_20260731
ORDER BY id;

SELECT setval(
    pg_get_serial_sequence('public.all_candidates', 'id'),
    COALESCE((SELECT MAX(id) FROM public.all_candidates), 1),
    EXISTS (SELECT 1 FROM public.all_candidates)
);
SELECT setval(
    pg_get_serial_sequence('public.all_candidate_observations', 'id'),
    COALESCE((SELECT MAX(id) FROM public.all_candidate_observations), 1),
    EXISTS (SELECT 1 FROM public.all_candidate_observations)
);

DROP TABLE IF EXISTS public.candidate_scrape_runs;

DO $$
BEGIN
    IF EXISTS (
        (SELECT * FROM public.all_candidates
         EXCEPT
         SELECT * FROM backup.all_candidates_before_lifecycle_20260731)
        UNION ALL
        (SELECT * FROM backup.all_candidates_before_lifecycle_20260731
         EXCEPT
         SELECT * FROM public.all_candidates)
    ) THEN
        RAISE EXCEPTION 'Candidate rollback mismatch.';
    END IF;
    IF EXISTS (
        (SELECT * FROM public.all_candidate_observations
         EXCEPT
         SELECT * FROM backup.all_candidate_observations_before_lifecycle_20260731)
        UNION ALL
        (SELECT * FROM backup.all_candidate_observations_before_lifecycle_20260731
         EXCEPT
         SELECT * FROM public.all_candidate_observations)
    ) THEN
        RAISE EXCEPTION 'Candidate observation rollback mismatch.';
    END IF;
END
$$;

COMMIT;
