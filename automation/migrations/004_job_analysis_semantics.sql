-- Make lifecycle censoring, geography, and rate-window semantics explicit for MCP.
-- Idempotent post-deployment patch; run after migrations 001-003.

BEGIN;

ALTER TABLE public.all_jobs
    ADD COLUMN IF NOT EXISTS lifecycle_data_quality TEXT NOT NULL DEFAULT 'prospective',
    ADD COLUMN IF NOT EXISTS lifecycle_tracking_started_at TIMESTAMP WITHOUT TIME ZONE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.all_jobs'::regclass
          AND conname = 'all_jobs_lifecycle_data_quality_check'
    ) THEN
        ALTER TABLE public.all_jobs
            ADD CONSTRAINT all_jobs_lifecycle_data_quality_check
            CHECK (lifecycle_data_quality IN ('legacy_snapshot', 'prospective'));
    END IF;
END
$$;

UPDATE public.all_jobs AS job
SET
    lifecycle_data_quality = CASE
        WHEN EXISTS (
            SELECT 1
            FROM backup.all_jobs_before_lifecycle_20260729 AS legacy
            WHERE legacy.id = job.id
        ) THEN 'legacy_snapshot'
        ELSE 'prospective'
    END,
    lifecycle_tracking_started_at = (
        SELECT MIN(observation.observed_at)
        FROM public.all_job_observations AS observation
        WHERE observation.job_id = job.id
    );

ALTER TABLE public.all_jobs
    ALTER COLUMN lifecycle_tracking_started_at SET DEFAULT CURRENT_TIMESTAMP;

UPDATE public.all_jobs AS job
SET
    applicant_regions = (
        SELECT array_agg(DISTINCT value ORDER BY value)
        FROM unnest(job.applicant_regions) AS item(value)
        WHERE value IS NOT NULL AND btrim(value) <> ''
    ),
    applicant_countries = (
        SELECT array_agg(DISTINCT normalized ORDER BY normalized)
        FROM (
            SELECT CASE
                WHEN upper(value) = 'UA' THEN 'UKR'
                ELSE upper(value)
            END AS normalized
            FROM unnest(job.applicant_countries) AS item(value)
            WHERE value IS NOT NULL AND btrim(value) <> ''
        ) AS values
    ),
    office_countries = (
        SELECT array_agg(DISTINCT normalized ORDER BY normalized)
        FROM (
            SELECT CASE
                WHEN upper(value) = 'UA' THEN 'UKR'
                ELSE upper(value)
            END AS normalized
            FROM unnest(job.office_countries) AS item(value)
            WHERE value IS NOT NULL AND btrim(value) <> ''
        ) AS values
    ),
    office_cities = (
        SELECT array_agg(DISTINCT value ORDER BY value)
        FROM unnest(job.office_cities) AS item(value)
        WHERE value IS NOT NULL AND btrim(value) <> ''
    );

UPDATE public.job_scrape_runs
SET error_summary = NULL
WHERE status = 'complete';

COMMENT ON COLUMN public.all_jobs.first_seen_at IS
    'First database sighting. For lifecycle_data_quality=legacy_snapshot this predates continuous lifecycle tracking and must not be used alone for lifespan.';
COMMENT ON COLUMN public.all_jobs.last_seen_at IS
    'Latest positive sighting. For legacy_snapshot inactive rows this can equal first_seen_at because historical repeat observations do not exist.';
COMMENT ON COLUMN public.all_jobs.inactive_detected_at IS
    'Detection time, not exact closure time.';
COMMENT ON COLUMN public.all_jobs.lifecycle_data_quality IS
    'legacy_snapshot = left-censored pre-Step-0 row; prospective = observed under continuous lifecycle tracking.';
COMMENT ON COLUMN public.all_jobs.lifecycle_tracking_started_at IS
    'Beginning of reliable lifecycle observation coverage for this row.';
COMMENT ON COLUMN public.all_jobs.applicant_countries IS
    'Applicant-location codes from Djinni, normalized to uppercase and UA->UKR; normally ISO-like alpha-3 codes.';
COMMENT ON COLUMN public.all_jobs.office_countries IS
    'Uppercase raw jobLocation country tokens; may be country names rather than applicant-country codes, so map before cross-field comparison.';
COMMENT ON COLUMN public.job_scrape_runs.finished_at IS
    'Listing reconciliation completion time. Post-run detail status confirmations may update observations after this timestamp.';
COMMENT ON COLUMN public.all_job_observations.observed_at IS
    'Evidence timestamp; detail confirmation may occur shortly after the parent run finished its listing reconciliation.';

CREATE OR REPLACE VIEW public.v_jobs_current AS
SELECT
    job.id,
    job.djinni_id,
    job.title,
    job.company,
    job.status,
    job.status_source,
    job.status_checked_at,
    job.first_seen_at,
    job.last_seen_at,
    job.inactive_detected_at,
    job.reopened_count,
    job.published_at,
    job.valid_through,
    job.category,
    job.domain,
    job.employment_type,
    job.work_format,
    job.applicant_regions,
    job.applicant_countries,
    job.office_countries,
    job.office_cities,
    job.exp_selector_months AS experience_months,
    job.english_level,
    job.salary_min,
    job.salary_max,
    job.salary_currency,
    job.salary_period,
    job.salary_source,
    job.latest_views_count,
    job.latest_apps_count,
    job.engagement_observed_at,
    job.is_igaming,
    job.is_marketing,
    job.direct_apply,
    job.company_url,
    job.description,
    job.url,
    job.detail_scraped_at,
    job.last_parser_version,
    job.lifecycle_data_quality,
    job.lifecycle_tracking_started_at,
    (job.lifecycle_data_quality = 'prospective') AS lifecycle_duration_observable
FROM public.all_jobs AS job;

CREATE OR REPLACE VIEW public.v_job_dynamics AS
WITH ranked AS (
    SELECT
        observation.job_id,
        observation.observed_at,
        observation.views_count,
        observation.apps_count,
        observation.page,
        observation.global_rank,
        row_number() OVER (
            PARTITION BY observation.job_id
            ORDER BY observation.observed_at DESC
        ) AS observation_number,
        count(*) OVER (PARTITION BY observation.job_id) AS observation_count
    FROM public.all_job_observations AS observation
    WHERE observation.views_count IS NOT NULL
       OR observation.apps_count IS NOT NULL
),
latest AS (
    SELECT * FROM ranked WHERE observation_number = 1
),
previous AS (
    SELECT * FROM ranked WHERE observation_number = 2
)
SELECT
    job.id AS job_id,
    job.djinni_id,
    job.title,
    job.company,
    job.status,
    latest.observed_at AS latest_observed_at,
    previous.observed_at AS previous_observed_at,
    latest.views_count,
    latest.apps_count,
    latest.views_count - previous.views_count AS views_delta,
    latest.apps_count - previous.apps_count AS apps_delta,
    EXTRACT(EPOCH FROM (latest.observed_at - previous.observed_at)) / 3600.0
        AS elapsed_hours,
    NULL::numeric AS views_per_day,
    NULL::numeric AS apps_per_day,
    latest.page,
    latest.global_rank,
    CASE
        WHEN previous.observed_at IS NOT NULL
         AND latest.observed_at > previous.observed_at
            THEN (latest.views_count - previous.views_count)
                 / NULLIF(
                     EXTRACT(EPOCH FROM (latest.observed_at - previous.observed_at)) / 3600.0,
                     0
                 )
        ELSE NULL
    END AS views_per_hour,
    CASE
        WHEN previous.observed_at IS NOT NULL
         AND latest.observed_at > previous.observed_at
            THEN (latest.apps_count - previous.apps_count)
                 / NULLIF(
                     EXTRACT(EPOCH FROM (latest.observed_at - previous.observed_at)) / 3600.0,
                     0
                 )
        ELSE NULL
    END AS apps_per_hour,
    CASE
        WHEN previous.observed_at IS NULL THEN NULL
        ELSE latest.observed_at - previous.observed_at < INTERVAL '24 hours'
    END AS is_short_window,
    latest.observation_count
FROM public.all_jobs AS job
JOIN latest ON latest.job_id = job.id
LEFT JOIN previous ON previous.job_id = job.id;

COMMENT ON VIEW public.v_jobs_current IS
    'Current MCP-facing vacancy snapshot. Use lifecycle_duration_observable before any lifespan analysis.';
COMMENT ON VIEW public.v_job_dynamics IS
    'Observed interval deltas and per-hour rates. Legacy *_per_day columns are retained as NULL for dependency compatibility; use is_short_window before interpreting rates.';
COMMENT ON COLUMN public.v_job_dynamics.views_per_day IS
    'Deprecated compatibility column; always NULL. Use views_per_hour with elapsed_hours.';
COMMENT ON COLUMN public.v_job_dynamics.apps_per_day IS
    'Deprecated compatibility column; always NULL. Use apps_per_hour with elapsed_hours.';

COMMIT;

SELECT
    COUNT(*) FILTER (WHERE lifecycle_data_quality='legacy_snapshot') AS legacy_snapshot_jobs,
    COUNT(*) FILTER (WHERE lifecycle_data_quality='prospective') AS prospective_jobs,
    COUNT(*) FILTER (WHERE lifecycle_tracking_started_at IS NULL) AS missing_tracking_start,
    COUNT(*) FILTER (WHERE applicant_countries @> ARRAY['UA']) AS unnormalized_ua,
    COUNT(*) FILTER (WHERE array_position(applicant_countries, NULL) IS NOT NULL) AS null_country_elements
FROM public.all_jobs;
