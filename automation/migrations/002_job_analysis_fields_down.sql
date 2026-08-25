-- Emergency rollback for 002_job_analysis_fields.sql.
-- This removes normalized analysis fields and views, not legacy columns.

BEGIN;

DROP VIEW IF EXISTS public.v_job_dynamics;
DROP VIEW IF EXISTS public.v_jobs_current;

ALTER TABLE public.all_jobs
    DROP COLUMN IF EXISTS last_parser_version,
    DROP COLUMN IF EXISTS detail_scraped_at,
    DROP COLUMN IF EXISTS engagement_observed_at,
    DROP COLUMN IF EXISTS latest_apps_count,
    DROP COLUMN IF EXISTS latest_views_count,
    DROP COLUMN IF EXISTS direct_apply,
    DROP COLUMN IF EXISTS salary_source,
    DROP COLUMN IF EXISTS salary_period,
    DROP COLUMN IF EXISTS company_url,
    DROP COLUMN IF EXISTS office_cities,
    DROP COLUMN IF EXISTS office_countries,
    DROP COLUMN IF EXISTS applicant_countries,
    DROP COLUMN IF EXISTS applicant_regions,
    DROP COLUMN IF EXISTS work_format,
    DROP COLUMN IF EXISTS employment_type,
    DROP COLUMN IF EXISTS valid_through;

COMMIT;
