-- Add query-friendly current-state fields and backfill legacy rows from raw_json_ld.
-- No fabricated historical observations: every backfilled value comes from the
-- row's stored JSON-LD, original scrape timestamp, or a real observation.

BEGIN;

ALTER TABLE public.all_jobs
    ADD COLUMN valid_through TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN employment_type TEXT,
    ADD COLUMN work_format TEXT,
    ADD COLUMN applicant_regions TEXT[],
    ADD COLUMN applicant_countries TEXT[],
    ADD COLUMN office_countries TEXT[],
    ADD COLUMN office_cities TEXT[],
    ADD COLUMN company_url TEXT,
    ADD COLUMN salary_period TEXT,
    ADD COLUMN salary_source TEXT,
    ADD COLUMN direct_apply BOOLEAN,
    ADD COLUMN latest_views_count INTEGER,
    ADD COLUMN latest_apps_count INTEGER,
    ADD COLUMN engagement_observed_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN detail_scraped_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN last_parser_version TEXT;

UPDATE public.all_jobs
SET
    valid_through = CASE
        WHEN NULLIF(raw_json_ld ->> 'validThrough', '') IS NOT NULL
            THEN (raw_json_ld ->> 'validThrough')::timestamp
        ELSE NULL
    END,
    employment_type = NULLIF(raw_json_ld ->> 'employmentType', ''),
    work_format = CASE
        WHEN raw_json_ld ->> 'jobLocationType' = 'TELECOMMUTE' THEN 'remote'
        WHEN raw_json_ld ? 'jobLocation' THEN 'office'
        ELSE NULL
    END,
    domain = COALESCE(NULLIF(domain, ''), NULLIF(raw_json_ld ->> 'industry', '')),
    applicant_regions = NULLIF(
        ARRAY(
            SELECT jsonb_array_elements_text(
                jsonb_path_query_array(
                    raw_json_ld,
                    'lax $.applicantLocationRequirements.address.addressRegion[*]'
                )
            )
        ),
        ARRAY[]::text[]
    ),
    applicant_countries = NULLIF(
        ARRAY(
            SELECT jsonb_array_elements_text(
                jsonb_path_query_array(
                    raw_json_ld,
                    'lax $.applicantLocationRequirements.address.addressCountry[*]'
                )
            )
        ),
        ARRAY[]::text[]
    ),
    office_countries = NULLIF(
        ARRAY(
            SELECT jsonb_array_elements_text(
                jsonb_path_query_array(
                    raw_json_ld,
                    'lax $.jobLocation.address.addressCountry[*]'
                )
            )
        ),
        ARRAY[]::text[]
    ),
    office_cities = NULLIF(
        ARRAY(
            SELECT jsonb_array_elements_text(
                jsonb_path_query_array(
                    raw_json_ld,
                    'lax $.jobLocation.address.addressLocality[*]'
                )
            )
        ),
        ARRAY[]::text[]
    ),
    company_url = NULLIF(
        jsonb_path_query_first(
            raw_json_ld,
            'lax $.hiringOrganization.sameAs[*]'
        ) #>> '{}',
        ''
    ),
    salary_min = CASE
        WHEN NULLIF(raw_json_ld #>> '{baseSalary,value,minValue}', '') IS NOT NULL
          OR NULLIF(raw_json_ld #>> '{baseSalary,value,maxValue}', '') IS NOT NULL
            THEN NULLIF(raw_json_ld #>> '{baseSalary,value,minValue}', '')::numeric::integer
        ELSE salary_min
    END,
    salary_max = CASE
        WHEN NULLIF(raw_json_ld #>> '{baseSalary,value,minValue}', '') IS NOT NULL
          OR NULLIF(raw_json_ld #>> '{baseSalary,value,maxValue}', '') IS NOT NULL
            THEN NULLIF(raw_json_ld #>> '{baseSalary,value,maxValue}', '')::numeric::integer
        ELSE salary_max
    END,
    salary_currency = CASE
        WHEN NULLIF(raw_json_ld #>> '{baseSalary,value,minValue}', '') IS NOT NULL
          OR NULLIF(raw_json_ld #>> '{baseSalary,value,maxValue}', '') IS NOT NULL
            THEN COALESCE(
                NULLIF(raw_json_ld #>> '{baseSalary,currency}', ''),
                salary_currency
            )
        ELSE salary_currency
    END,
    salary_period = CASE
        WHEN NULLIF(raw_json_ld #>> '{baseSalary,value,minValue}', '') IS NOT NULL
          OR NULLIF(raw_json_ld #>> '{baseSalary,value,maxValue}', '') IS NOT NULL
            THEN lower(NULLIF(raw_json_ld #>> '{baseSalary,value,unitText}', ''))
        ELSE NULL
    END,
    salary_source = CASE
        WHEN NULLIF(raw_json_ld #>> '{baseSalary,value,minValue}', '') IS NOT NULL
          OR NULLIF(raw_json_ld #>> '{baseSalary,value,maxValue}', '') IS NOT NULL
            THEN 'baseSalary'
        WHEN raw_json_ld ? 'estimatedSalary'
            THEN 'estimatedSalary'
        ELSE NULL
    END,
    direct_apply = CASE
        WHEN raw_json_ld ? 'directApply'
            THEN (raw_json_ld ->> 'directApply')::boolean
        ELSE NULL
    END,
    detail_scraped_at = scraped_at,
    last_parser_version = 'legacy-jsonld-backfill-v1';

WITH latest_engagement AS (
    SELECT DISTINCT ON (observation.job_id)
        observation.job_id,
        observation.observed_at,
        observation.views_count,
        observation.apps_count
    FROM public.all_job_observations AS observation
    WHERE observation.views_count IS NOT NULL
       OR observation.apps_count IS NOT NULL
    ORDER BY observation.job_id, observation.observed_at DESC
)
UPDATE public.all_jobs AS job
SET
    latest_views_count = CASE
        WHEN latest.observed_at IS NOT NULL THEN latest.views_count
        ELSE job.views_count
    END,
    latest_apps_count = CASE
        WHEN latest.observed_at IS NOT NULL THEN latest.apps_count
        ELSE job.apps_count
    END,
    engagement_observed_at = CASE
        WHEN latest.observed_at IS NOT NULL THEN latest.observed_at
        WHEN job.views_count IS NOT NULL OR job.apps_count IS NOT NULL
            THEN job.scraped_at
        ELSE NULL
    END
FROM (
    SELECT
        base.id AS job_id,
        engagement.observed_at,
        engagement.views_count,
        engagement.apps_count
    FROM public.all_jobs AS base
    LEFT JOIN latest_engagement AS engagement ON engagement.job_id = base.id
) AS latest
WHERE latest.job_id = job.id;

CREATE INDEX idx_all_jobs_valid_through
    ON public.all_jobs(valid_through);

CREATE INDEX idx_all_jobs_employment_type
    ON public.all_jobs(employment_type);

CREATE INDEX idx_all_jobs_work_format
    ON public.all_jobs(work_format);

CREATE INDEX idx_all_jobs_domain
    ON public.all_jobs(domain);

CREATE INDEX idx_all_jobs_latest_apps
    ON public.all_jobs(latest_apps_count);

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
    job.last_parser_version
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
        ) AS observation_number
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
    CASE
        WHEN previous.observed_at IS NOT NULL
         AND latest.observed_at > previous.observed_at
            THEN (latest.views_count - previous.views_count)
                 / NULLIF(
                     EXTRACT(EPOCH FROM (latest.observed_at - previous.observed_at)) / 86400.0,
                     0
                 )
        ELSE NULL
    END AS views_per_day,
    CASE
        WHEN previous.observed_at IS NOT NULL
         AND latest.observed_at > previous.observed_at
            THEN (latest.apps_count - previous.apps_count)
                 / NULLIF(
                     EXTRACT(EPOCH FROM (latest.observed_at - previous.observed_at)) / 86400.0,
                     0
                 )
        ELSE NULL
    END AS apps_per_day,
    latest.page,
    latest.global_rank
FROM public.all_jobs AS job
JOIN latest ON latest.job_id = job.id
LEFT JOIN previous ON previous.job_id = job.id;

COMMIT;

SELECT
    COUNT(*) AS total_jobs,
    COUNT(*) FILTER (WHERE domain IS NOT NULL) AS with_domain,
    COUNT(*) FILTER (WHERE employment_type IS NOT NULL) AS with_employment_type,
    COUNT(*) FILTER (WHERE valid_through IS NOT NULL) AS with_valid_through,
    COUNT(*) FILTER (WHERE latest_views_count IS NOT NULL) AS with_latest_views,
    COUNT(*) FILTER (WHERE engagement_observed_at IS NOT NULL) AS with_engagement_time
FROM public.all_jobs;
