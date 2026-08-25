-- Immutable rollback snapshot before candidate lifecycle integration.
-- Run once. Deliberately fails if the named backup tables already exist.

BEGIN;

CREATE SCHEMA IF NOT EXISTS backup;

DO $$
BEGIN
    IF to_regclass('backup.all_candidates_before_lifecycle_20260731') IS NOT NULL
       OR to_regclass('backup.all_candidate_observations_before_lifecycle_20260731') IS NOT NULL THEN
        RAISE EXCEPTION 'Candidate lifecycle backup already exists; do not overwrite it.';
    END IF;
END
$$;

CREATE TABLE backup.all_candidates_before_lifecycle_20260731 AS
TABLE public.all_candidates;

CREATE TABLE backup.all_candidate_observations_before_lifecycle_20260731 AS
TABLE public.all_candidate_observations;

COMMENT ON TABLE backup.all_candidates_before_lifecycle_20260731 IS
    'Immutable all_candidates snapshot immediately before candidate lifecycle integration.';
COMMENT ON TABLE backup.all_candidate_observations_before_lifecycle_20260731 IS
    'Immutable all_candidate_observations snapshot immediately before candidate lifecycle integration.';

DO $$
BEGIN
    IF (SELECT COUNT(*) FROM public.all_candidates)
       <> (SELECT COUNT(*) FROM backup.all_candidates_before_lifecycle_20260731) THEN
        RAISE EXCEPTION 'Candidate backup row count mismatch.';
    END IF;
    IF (SELECT COUNT(*) FROM public.all_candidate_observations)
       <> (SELECT COUNT(*) FROM backup.all_candidate_observations_before_lifecycle_20260731) THEN
        RAISE EXCEPTION 'Candidate observation backup row count mismatch.';
    END IF;
    IF EXISTS (
        (SELECT * FROM public.all_candidates
         EXCEPT
         SELECT * FROM backup.all_candidates_before_lifecycle_20260731)
        UNION ALL
        (SELECT * FROM backup.all_candidates_before_lifecycle_20260731
         EXCEPT
         SELECT * FROM public.all_candidates)
    ) THEN
        RAISE EXCEPTION 'Candidate backup full-row mismatch.';
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
        RAISE EXCEPTION 'Candidate observation backup full-row mismatch.';
    END IF;
END
$$;

COMMIT;

SELECT
    (SELECT COUNT(*) FROM public.all_candidates) AS source_candidates,
    (SELECT COUNT(*) FROM backup.all_candidates_before_lifecycle_20260731) AS backup_candidates,
    (SELECT COUNT(*) FROM public.all_candidate_observations) AS source_observations,
    (SELECT COUNT(*) FROM backup.all_candidate_observations_before_lifecycle_20260731) AS backup_observations,
    (
        SELECT md5(string_agg(md5(to_jsonb(row_data)::text), '' ORDER BY row_data.id))
        FROM public.all_candidates AS row_data
    ) AS source_candidate_checksum,
    (
        SELECT md5(string_agg(md5(to_jsonb(row_data)::text), '' ORDER BY row_data.id))
        FROM backup.all_candidates_before_lifecycle_20260731 AS row_data
    ) AS backup_candidate_checksum,
    (
        SELECT md5(string_agg(md5(to_jsonb(row_data)::text), '' ORDER BY row_data.id))
        FROM public.all_candidate_observations AS row_data
    ) AS source_observation_checksum,
    (
        SELECT md5(string_agg(md5(to_jsonb(row_data)::text), '' ORDER BY row_data.id))
        FROM backup.all_candidate_observations_before_lifecycle_20260731 AS row_data
    ) AS backup_observation_checksum;
