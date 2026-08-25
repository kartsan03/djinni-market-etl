-- Harden candidate rollback snapshots and make lifecycle observations insert-only.
-- Superusers can bypass PostgreSQL ACLs by definition, so an external Git-tracked
-- binary COPY archive and SHA-256 manifest remain the independent integrity anchor.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='candidate_archive_owner') THEN
        CREATE ROLE candidate_archive_owner NOLOGIN NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='candidate_observation_owner') THEN
        CREATE ROLE candidate_observation_owner NOLOGIN NOINHERIT;
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION backup.reject_candidate_archive_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Candidate archive is sealed; % is forbidden on %.%', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME;
END
$$;

DROP TRIGGER IF EXISTS protect_candidate_backup
    ON backup.all_candidates_before_lifecycle_20260731;
CREATE TRIGGER protect_candidate_backup
BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE
ON backup.all_candidates_before_lifecycle_20260731
FOR EACH STATEMENT EXECUTE FUNCTION backup.reject_candidate_archive_mutation();

DROP TRIGGER IF EXISTS protect_candidate_observation_backup
    ON backup.all_candidate_observations_before_lifecycle_20260731;
CREATE TRIGGER protect_candidate_observation_backup
BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE
ON backup.all_candidate_observations_before_lifecycle_20260731
FOR EACH STATEMENT EXECUTE FUNCTION backup.reject_candidate_archive_mutation();

DROP TRIGGER IF EXISTS protect_candidate_backup_manifest
    ON backup.candidate_backup_manifest;
CREATE TRIGGER protect_candidate_backup_manifest
BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE
ON backup.candidate_backup_manifest
FOR EACH STATEMENT EXECUTE FUNCTION backup.reject_candidate_archive_mutation();

CREATE OR REPLACE FUNCTION public.reject_candidate_observation_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Candidate observations are append-only; % is forbidden.', TG_OP;
END
$$;

DROP TRIGGER IF EXISTS candidate_observations_append_only
    ON public.all_candidate_observations;
CREATE TRIGGER candidate_observations_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE
ON public.all_candidate_observations
FOR EACH STATEMENT EXECUTE FUNCTION public.reject_candidate_observation_mutation();

ALTER TABLE backup.all_candidates_before_lifecycle_20260731 OWNER TO candidate_archive_owner;
ALTER TABLE backup.all_candidate_observations_before_lifecycle_20260731 OWNER TO candidate_archive_owner;
ALTER TABLE backup.candidate_backup_manifest OWNER TO candidate_archive_owner;
ALTER FUNCTION backup.reject_candidate_archive_mutation() OWNER TO candidate_archive_owner;

ALTER TABLE public.all_candidate_observations OWNER TO candidate_observation_owner;
ALTER SEQUENCE public.all_candidate_observations_id_seq OWNER TO candidate_observation_owner;
ALTER FUNCTION public.reject_candidate_observation_mutation() OWNER TO candidate_observation_owner;

REVOKE ALL ON backup.all_candidates_before_lifecycle_20260731 FROM PUBLIC;
REVOKE ALL ON backup.all_candidate_observations_before_lifecycle_20260731 FROM PUBLIC;
REVOKE ALL ON backup.candidate_backup_manifest FROM PUBLIC;
REVOKE ALL ON public.all_candidate_observations FROM PUBLIC;
REVOKE ALL ON SEQUENCE public.all_candidate_observations_id_seq FROM PUBLIC;

GRANT SELECT ON backup.all_candidates_before_lifecycle_20260731 TO admin;
GRANT SELECT ON backup.all_candidate_observations_before_lifecycle_20260731 TO admin;
GRANT SELECT ON backup.candidate_backup_manifest TO admin;
GRANT SELECT,INSERT ON public.all_candidate_observations TO admin;
GRANT USAGE,SELECT ON SEQUENCE public.all_candidate_observations_id_seq TO admin;

COMMENT ON FUNCTION backup.reject_candidate_archive_mutation() IS
    'Normal-session DML guard for sealed candidate DB snapshots; independent SHA-256 COPY archives are retained in Git.';
COMMENT ON FUNCTION public.reject_candidate_observation_mutation() IS
    'Enforces append-only candidate evidence for normal scraper/database operations.';

COMMIT;

SELECT
    n.nspname AS schema_name,
    c.relname,
    pg_get_userbyid(c.relowner) AS owner,
    c.relacl AS explicit_acl
FROM pg_class c
JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE (n.nspname,c.relname) IN (
    ('backup','all_candidates_before_lifecycle_20260731'),
    ('backup','all_candidate_observations_before_lifecycle_20260731'),
    ('backup','candidate_backup_manifest'),
    ('public','all_candidate_observations')
)
ORDER BY n.nspname,c.relname;
