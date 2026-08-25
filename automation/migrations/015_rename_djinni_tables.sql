BEGIN;

-- Drop dependent views first
DROP VIEW IF EXISTS public.v_jobs_current;
DROP VIEW IF EXISTS public.v_job_dynamics;
DROP VIEW IF EXISTS public.v_candidates_current;
DROP VIEW IF EXISTS public.v_candidate_dynamics;

-- Rename jobs tables
ALTER TABLE public.all_jobs RENAME TO djinni_jobs;
ALTER TABLE public.all_job_observations RENAME TO djinni_job_observations;
ALTER TABLE public.job_scrape_runs RENAME TO djinni_job_scrape_runs;

-- Rename candidates tables
ALTER TABLE public.all_candidates RENAME TO djinni_candidates;
ALTER TABLE public.all_candidate_observations RENAME TO djinni_candidate_observations;
ALTER TABLE public.candidate_scrape_runs RENAME TO djinni_candidate_scrape_runs;

-- Recreate views with new names (Skipping view recreation in this migration, the user can recreate them if needed, or we just ignore views for now, but better to keep them if they were used. Let's not recreate views unless we have the DDL. Let's just drop them to allow the rename).
COMMIT;