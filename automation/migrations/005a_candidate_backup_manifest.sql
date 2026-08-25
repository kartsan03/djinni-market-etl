-- Seal and fingerprint the already-created candidate rollback snapshots.
-- Run after 005 and before 006. Idempotent only when the sealed contents match.

BEGIN;

LOCK TABLE public.all_candidates IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.all_candidate_observations IN ACCESS EXCLUSIVE MODE;
LOCK TABLE backup.all_candidates_before_lifecycle_20260731 IN ACCESS EXCLUSIVE MODE;
LOCK TABLE backup.all_candidate_observations_before_lifecycle_20260731 IN ACCESS EXCLUSIVE MODE;

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
        RAISE EXCEPTION 'Live candidates drifted from the rollback snapshot; sealing refused.';
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
        RAISE EXCEPTION 'Live candidate observations drifted from the rollback snapshot; sealing refused.';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS backup.candidate_backup_manifest (
    backup_name TEXT PRIMARY KEY,
    sealed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    row_count BIGINT NOT NULL,
    full_row_checksum TEXT NOT NULL
);

INSERT INTO backup.candidate_backup_manifest (backup_name,row_count,full_row_checksum)
SELECT
    'all_candidates_before_lifecycle_20260731',
    COUNT(*),
    md5(string_agg(md5(to_jsonb(row_data)::text), '' ORDER BY row_data.id))
FROM backup.all_candidates_before_lifecycle_20260731 AS row_data
ON CONFLICT (backup_name) DO UPDATE SET
    row_count=EXCLUDED.row_count,
    full_row_checksum=EXCLUDED.full_row_checksum
WHERE backup.candidate_backup_manifest.row_count=EXCLUDED.row_count
  AND backup.candidate_backup_manifest.full_row_checksum=EXCLUDED.full_row_checksum;

INSERT INTO backup.candidate_backup_manifest (backup_name,row_count,full_row_checksum)
SELECT
    'all_candidate_observations_before_lifecycle_20260731',
    COUNT(*),
    md5(string_agg(md5(to_jsonb(row_data)::text), '' ORDER BY row_data.id))
FROM backup.all_candidate_observations_before_lifecycle_20260731 AS row_data
ON CONFLICT (backup_name) DO UPDATE SET
    row_count=EXCLUDED.row_count,
    full_row_checksum=EXCLUDED.full_row_checksum
WHERE backup.candidate_backup_manifest.row_count=EXCLUDED.row_count
  AND backup.candidate_backup_manifest.full_row_checksum=EXCLUDED.full_row_checksum;

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
        RAISE EXCEPTION 'Candidate backup manifest mismatch.';
    END IF;
END
$$;

REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON backup.all_candidates_before_lifecycle_20260731 FROM PUBLIC;
REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON backup.all_candidate_observations_before_lifecycle_20260731 FROM PUBLIC;
REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON backup.candidate_backup_manifest FROM PUBLIC;

COMMIT;

SELECT * FROM backup.candidate_backup_manifest ORDER BY backup_name;
