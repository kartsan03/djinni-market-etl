-- Baseline schema for a fresh PostgreSQL deployment.
-- Mirrors the production database this pipeline runs on (TIMESTAMPTZ throughout).
--
-- Creates:
--   * djinni_jobs / djinni_candidates current-state tables
--   * scrape-run audit tables with a DB-enforced single-writer
--   * append-only observation tables (UPDATE/DELETE/TRUNCATE rejected)
--   * first_seen_at immutability triggers
--   * analysis views: v_jobs_current, v_job_dynamics,
--                     v_candidates_current, v_candidate_dynamics
--
-- Two ways to use this repository:
--   * Fresh install:   create the database, run THIS file once, done.
--   * Existing database that predates the final table names:
--       automation/migrations/ is the ordered historical journal (backup-first,
--       verified snapshots, *_down.sql rollbacks). It documents how the schema
--       evolved; it assumes tables created before migration 001 existed already
--       under the legacy all_* names renamed by 015_rename_djinni_tables.sql.
--
-- Apply with: psql -v ON_ERROR_STOP=1 -f schema.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- Jobs: run audit
-- ---------------------------------------------------------------------------

CREATE TABLE public.djinni_job_scrape_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'complete', 'partial', 'failed')),
    show_stale BOOLEAN NOT NULL DEFAULT TRUE,
    site_reported_total INTEGER,
    site_reported_total_end INTEGER,
    expected_pages INTEGER,
    pages_scraped INTEGER NOT NULL DEFAULT 0,
    cards_seen INTEGER NOT NULL DEFAULT 0,
    unique_jobs_seen INTEGER NOT NULL DEFAULT 0,
    existing_jobs_seen INTEGER NOT NULL DEFAULT 0,
    missing_in_db INTEGER NOT NULL DEFAULT 0,
    duplicate_cards INTEGER NOT NULL DEFAULT 0,
    errors_count INTEGER NOT NULL DEFAULT 0,
    parser_version TEXT,
    error_summary JSONB
);

-- Database-level mutex: only one running reconciliation can ever exist.
CREATE UNIQUE INDEX uq_djinni_job_scrape_runs_one_running
    ON public.djinni_job_scrape_runs ((status))
    WHERE status = 'running';

CREATE INDEX idx_djinni_job_scrape_runs_started
    ON public.djinni_job_scrape_runs (started_at DESC);

-- ---------------------------------------------------------------------------
-- Jobs: current state
-- ---------------------------------------------------------------------------

CREATE TABLE public.djinni_jobs (
    id SERIAL PRIMARY KEY,
    djinni_id INTEGER UNIQUE,                      -- public /jobs/<id>-... id
    title TEXT,
    company TEXT,
    category TEXT,
    domain TEXT,
    salary_min INTEGER,
    salary_max INTEGER,
    salary_currency VARCHAR(10),
    salary_period TEXT,
    salary_source TEXT,
    exp_selector_months INTEGER,
    description TEXT,
    url TEXT,
    published_at TIMESTAMPTZ,
    valid_through TIMESTAMPTZ,
    scraped_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,

    -- Engagement as first scraped (immutable) and as last seen (updated).
    views_count INTEGER DEFAULT 0,
    apps_count INTEGER DEFAULT 0,
    latest_views_count INTEGER,
    latest_apps_count INTEGER,
    engagement_observed_at TIMESTAMPTZ,
    response_activity TEXT,

    tags TEXT[],
    english_level TEXT,
    employment_type TEXT,
    work_format TEXT,
    applicant_regions TEXT[],
    applicant_countries TEXT[],
    office_countries TEXT[],
    office_cities TEXT[],
    company_url TEXT,
    direct_apply BOOLEAN,
    is_igaming BOOLEAN DEFAULT FALSE,
    is_marketing BOOLEAN DEFAULT FALSE,
    raw_json_ld JSONB,

    -- Lifecycle state machine; see README.
    status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (status IN (
            'unknown', 'active',
            'inactive_inferred', 'offline_confirmed', 'deleted_confirmed'
        )),
    status_source TEXT,
    status_checked_at TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- immutable; trigger-enforced
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    inactive_detected_at TIMESTAMPTZ,
    reopened_count INTEGER NOT NULL DEFAULT 0,
    consecutive_complete_misses INTEGER NOT NULL DEFAULT 0,
    detail_attempted_at TIMESTAMPTZ,
    detail_scraped_at TIMESTAMPTZ,
    last_parser_version TEXT,
    lifecycle_data_quality TEXT NOT NULL DEFAULT 'prospective'
        CHECK (lifecycle_data_quality IN ('legacy_snapshot', 'prospective')),
    lifecycle_tracking_started_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_djinni_jobs_status     ON public.djinni_jobs (status);
CREATE INDEX idx_djinni_jobs_last_seen  ON public.djinni_jobs (last_seen_at DESC);
CREATE INDEX idx_djinni_jobs_published  ON public.djinni_jobs (published_at);
CREATE INDEX idx_djinni_jobs_salary_max ON public.djinni_jobs (salary_max);
CREATE INDEX idx_djinni_jobs_domain     ON public.djinni_jobs (domain);
CREATE INDEX idx_djinni_jobs_employment_type ON public.djinni_jobs (employment_type);
CREATE INDEX idx_djinni_jobs_work_format     ON public.djinni_jobs (work_format);
CREATE INDEX idx_djinni_jobs_valid_through   ON public.djinni_jobs (valid_through);
CREATE INDEX idx_djinni_jobs_latest_apps     ON public.djinni_jobs (latest_apps_count);

-- Unknown-status confirmation queue: oldest unattempted first.
CREATE INDEX idx_djinni_jobs_unknown_detail_attempt
    ON public.djinni_jobs (detail_attempted_at NULLS FIRST, id)
    WHERE status = 'unknown' AND status_source = 'first_complete_scan_absence';

-- ---------------------------------------------------------------------------
-- Jobs: immutable observation history
-- ---------------------------------------------------------------------------

CREATE TABLE public.djinni_job_observations (
    id BIGSERIAL PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES public.djinni_jobs(id) ON DELETE CASCADE,
    djinni_id INTEGER NOT NULL,
    run_id BIGINT NOT NULL REFERENCES public.djinni_job_scrape_runs(id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL,
    status_source TEXT,
    page INTEGER,
    position_on_page SMALLINT,
    global_rank INTEGER,
    views_count INTEGER,
    apps_count INTEGER,
    url TEXT,
    parse_ok BOOLEAN NOT NULL DEFAULT TRUE,
    parse_error TEXT,
    CONSTRAINT uq_djinni_job_observation_run_job UNIQUE (run_id, job_id)
);

CREATE INDEX idx_djinni_job_obs_job_time ON public.djinni_job_observations (job_id, observed_at DESC);
CREATE INDEX idx_djinni_job_obs_run      ON public.djinni_job_observations (run_id);

CREATE VIEW public.v_jobs_current AS
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
FROM public.djinni_jobs AS job;

CREATE VIEW public.v_job_dynamics AS
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
    FROM public.djinni_job_observations AS observation
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
FROM public.djinni_jobs AS job
JOIN latest ON latest.job_id = job.id
LEFT JOIN previous ON previous.job_id = job.id;

COMMENT ON VIEW public.v_jobs_current IS
    'Current vacancy snapshot. Use lifecycle_duration_observable before any lifespan analysis.';
COMMENT ON VIEW public.v_job_dynamics IS
    'Latest two engagement observations. *_per_day columns are always NULL; use *_per_hour with is_short_window.';

-- ---------------------------------------------------------------------------
-- Candidates: run audit
-- ---------------------------------------------------------------------------

CREATE TABLE public.djinni_candidate_scrape_runs (
    id BIGSERIAL PRIMARY KEY,
    run_kind TEXT NOT NULL DEFAULT 'incremental_listing_and_confirmation',
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'complete', 'partial', 'failed')),
    scope_complete BOOLEAN NOT NULL DEFAULT FALSE,

    -- The tracked cohort is explicitly NOT a claim of full population coverage.
    global_coverage BOOLEAN NOT NULL DEFAULT FALSE CHECK (global_coverage = FALSE),

    sortby TEXT NOT NULL DEFAULT 'date',
    min_views INTEGER NOT NULL DEFAULT 0,
    max_pages INTEGER,
    detail_limit INTEGER,
    listing_budget_minutes NUMERIC NOT NULL,
    internal_deadline_at TIMESTAMPTZ,
    site_reported_total_start INTEGER,
    site_reported_total_end INTEGER,
    site_reported_pages_start INTEGER,
    site_reported_pages_end INTEGER,
    pages_scraped INTEGER NOT NULL DEFAULT 0,
    cards_seen INTEGER NOT NULL DEFAULT 0,
    unique_candidates_seen INTEGER NOT NULL DEFAULT 0,
    existing_candidates_seen INTEGER NOT NULL DEFAULT 0,
    new_candidates_inserted INTEGER NOT NULL DEFAULT 0,
    duplicate_cards INTEGER NOT NULL DEFAULT 0,
    listing_parse_errors INTEGER NOT NULL DEFAULT 0,

    prior_frontier_key TEXT,
    prior_frontier_keys TEXT[],
    prior_frontier_published_date DATE,
    new_frontier_key TEXT,
    new_frontier_keys TEXT[],
    new_frontier_published_date DATE,
    frontier_reached BOOLEAN NOT NULL DEFAULT FALSE,
    frontier_overlap_pages INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,

    detail_selected INTEGER NOT NULL DEFAULT 0,
    detail_attempted INTEGER NOT NULL DEFAULT 0,
    detail_active INTEGER NOT NULL DEFAULT 0,
    detail_offline INTEGER NOT NULL DEFAULT 0,
    detail_deleted INTEGER NOT NULL DEFAULT 0,
    detail_ambiguous INTEGER NOT NULL DEFAULT 0,
    detail_request_errors INTEGER NOT NULL DEFAULT 0,
    db_errors INTEGER NOT NULL DEFAULT 0,
    parser_version TEXT NOT NULL,
    error_summary JSONB,

    new_candidate_limit INTEGER CHECK (new_candidate_limit >= 0),
    frontier_key_pages INTEGER CHECK (frontier_key_pages >= 1),
    due_candidates_before INTEGER CHECK (due_candidates_before >= 0),
    due_candidates_after INTEGER CHECK (due_candidates_after >= 0),
    pending_enrichment_selected INTEGER CHECK (pending_enrichment_selected >= 0),
    pending_enrichment_attempted INTEGER CHECK (pending_enrichment_attempted >= 0),
    profiles_hydrated INTEGER CHECK (profiles_hydrated >= 0),
    scheduled_existing_selected INTEGER CHECK (scheduled_existing_selected >= 0),
    scheduled_existing_attempted INTEGER CHECK (scheduled_existing_attempted >= 0),
    confirmation_budget_exhausted BOOLEAN
);

CREATE UNIQUE INDEX uq_djinni_candidate_scrape_runs_one_running
    ON public.djinni_candidate_scrape_runs ((status))
    WHERE status = 'running';

CREATE INDEX idx_djinni_candidate_scrape_runs_started
    ON public.djinni_candidate_scrape_runs (started_at DESC);

-- ---------------------------------------------------------------------------
-- Candidates: current state (tracked cohort)
-- ---------------------------------------------------------------------------

CREATE TABLE public.djinni_candidates (
    id SERIAL PRIMARY KEY,
    djinni_key TEXT NOT NULL UNIQUE,               -- public /q/<key>/ profile id
    title TEXT,
    category TEXT,
    specialization TEXT,

    salary_expectation INTEGER,
    salary_currency VARCHAR(8) DEFAULT 'USD',
    salary_period TEXT DEFAULT 'mo',

    experience_years NUMERIC(4,1),
    experience_months INTEGER,
    english_level TEXT,
    ukrainian_level TEXT,

    location_text TEXT,
    country TEXT,
    city TEXT,
    employment_type TEXT,
    work_format TEXT,

    work_experience TEXT,
    looking_for TEXT,

    skills TEXT[],
    skills_experience JSONB,
    domains TEXT[],
    domain_experience JSONB,
    languages JSONB,
    agentic_tools TEXT[],
    sections JSONB,

    views_count INTEGER DEFAULT 0,
    max_views_count INTEGER DEFAULT 0,

    first_seen_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,  -- immutable; trigger-enforced
    last_seen_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    published_at DATE,
    scraped_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,

    url TEXT,
    is_confirmed BOOLEAN DEFAULT FALSE,
    is_igaming BOOLEAN DEFAULT FALSE,
    has_agentic_ai BOOLEAN DEFAULT FALSE,

    raw_list JSONB,
    raw_profile JSONB,

    -- Lifecycle: positive evidence only, no absence inference for candidates.
    status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (status IN ('unknown', 'active', 'offline_confirmed', 'deleted_confirmed')),
    status_source TEXT NOT NULL DEFAULT 'migration_unknown',
    status_checked_at TIMESTAMPTZ,
    inactive_detected_at TIMESTAMPTZ,
    reopened_count INTEGER NOT NULL DEFAULT 0,
    last_listing_seen_at TIMESTAMPTZ,
    last_detail_checked_at TIMESTAMPTZ,
    detail_scraped_at TIMESTAMPTZ,
    engagement_observed_at TIMESTAMPTZ,
    monitoring_cohort TEXT NOT NULL DEFAULT 'prospective_incremental',
    monitoring_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    next_confirmation_due_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    lifecycle_data_quality TEXT NOT NULL DEFAULT 'prospective_incremental'
        CHECK (lifecycle_data_quality IN ('legacy_min_views_ge_5', 'prospective_incremental')),
    lifecycle_tracking_started_at TIMESTAMPTZ,
    entry_left_censored BOOLEAN NOT NULL DEFAULT FALSE,
    last_parser_version TEXT
);

CREATE INDEX idx_djinni_candidates_status  ON public.djinni_candidates (status);
CREATE INDEX idx_djinni_candidates_views   ON public.djinni_candidates (max_views_count);
CREATE INDEX idx_djinni_candidates_salary  ON public.djinni_candidates (salary_expectation);
CREATE INDEX idx_djinni_candidates_exp     ON public.djinni_candidates (experience_months);
CREATE INDEX idx_djinni_candidates_category ON public.djinni_candidates (category);
CREATE INDEX idx_djinni_candidates_published ON public.djinni_candidates (published_at);
CREATE INDEX idx_djinni_candidates_skills_gin ON public.djinni_candidates USING GIN (skills);
CREATE INDEX idx_djinni_candidates_domains_gin ON public.djinni_candidates USING GIN (domains);

-- Due-confirmation queue for the monitored cohort.
CREATE INDEX idx_djinni_candidates_confirmation_due
    ON public.djinni_candidates (next_confirmation_due_at, id)
    WHERE monitoring_enabled;

-- ---------------------------------------------------------------------------
-- Candidates: immutable observation history
-- ---------------------------------------------------------------------------

CREATE TABLE public.djinni_candidate_observations (
    id BIGSERIAL PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES public.djinni_candidates(id) ON DELETE CASCADE,
    djinni_key TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    views_count INTEGER,
    page INTEGER,
    sortby TEXT,
    url TEXT,
    run_id BIGINT REFERENCES public.djinni_candidate_scrape_runs(id) ON DELETE SET NULL,
    observation_type TEXT NOT NULL DEFAULT 'listing'
        CHECK (observation_type IN ('legacy_listing', 'listing', 'detail_status')),
    status TEXT
        CHECK (status IS NULL OR status IN ('unknown', 'active', 'offline_confirmed', 'deleted_confirmed')),
    status_source TEXT,
    position_on_page INTEGER,
    global_rank INTEGER,
    http_status INTEGER,
    parse_ok BOOLEAN NOT NULL DEFAULT TRUE,
    parse_error TEXT,
    parser_version TEXT
);

-- One listing/detail_status observation per candidate per run.
CREATE UNIQUE INDEX uq_djinni_candidate_observation_run_evidence
    ON public.djinni_candidate_observations (run_id, candidate_id, observation_type)
    WHERE run_id IS NOT NULL;

CREATE INDEX idx_djinni_candidate_obs_key_time
    ON public.djinni_candidate_observations (djinni_key, observed_at DESC);
CREATE INDEX idx_djinni_candidate_obs_candidate_time
    ON public.djinni_candidate_observations (candidate_id, observed_at DESC, id DESC);
CREATE INDEX idx_djinni_candidate_obs_run
    ON public.djinni_candidate_observations (run_id);

CREATE VIEW public.v_candidates_current AS
SELECT
    candidate.id,
    candidate.djinni_key,
    candidate.title,
    candidate.category,
    candidate.specialization,
    candidate.status,
    candidate.status_source,
    candidate.status_checked_at,
    CASE
        WHEN candidate.status = 'active' THEN TRUE
        WHEN candidate.status IN ('offline_confirmed', 'deleted_confirmed') THEN FALSE
        ELSE NULL
    END AS is_currently_available,
    candidate.first_seen_at,
    candidate.last_seen_at,
    candidate.inactive_detected_at,
    candidate.reopened_count,
    candidate.last_listing_seen_at,
    candidate.last_detail_checked_at,
    candidate.detail_scraped_at,
    candidate.lifecycle_data_quality,
    candidate.lifecycle_tracking_started_at,
    candidate.entry_left_censored,
    (candidate.status NOT IN ('offline_confirmed', 'deleted_confirmed')) AS exit_right_censored,
    (
        NOT candidate.entry_left_censored
        AND candidate.status IN ('offline_confirmed', 'deleted_confirmed')
    ) AS closed_interval_observable,
    candidate.monitoring_cohort,
    candidate.monitoring_enabled,
    candidate.next_confirmation_due_at,
    'tracked_canonical_only'::text AS coverage_scope,
    candidate.salary_expectation,
    candidate.salary_currency,
    candidate.salary_period,
    candidate.experience_years,
    candidate.experience_months,
    candidate.english_level,
    candidate.ukrainian_level,
    candidate.location_text,
    candidate.country,
    candidate.city,
    candidate.employment_type,
    candidate.work_format,
    candidate.work_experience,
    candidate.looking_for,
    candidate.skills,
    candidate.skills_experience,
    candidate.domains,
    candidate.domain_experience,
    candidate.languages,
    candidate.agentic_tools,
    candidate.sections,
    candidate.views_count AS latest_views_count,
    candidate.max_views_count,
    candidate.engagement_observed_at,
    candidate.published_at,
    candidate.scraped_at,
    candidate.url,
    candidate.is_confirmed AS verification_badge_ever_seen,
    candidate.is_igaming,
    candidate.has_agentic_ai,
    candidate.raw_list,
    candidate.raw_profile,
    candidate.last_parser_version
FROM public.djinni_candidates AS candidate;

CREATE VIEW public.v_candidate_dynamics AS
WITH ranked AS (
    SELECT
        observation.*,
        row_number() OVER (
            PARTITION BY observation.candidate_id
            ORDER BY observation.observed_at DESC, observation.id DESC
        ) AS observation_number,
        count(*) OVER (PARTITION BY observation.candidate_id) AS observation_count
    FROM public.djinni_candidate_observations AS observation
    WHERE observation.parse_ok
      AND observation.views_count IS NOT NULL
      AND observation.observation_type IN ('legacy_listing', 'listing')
),
latest AS (
    SELECT * FROM ranked WHERE observation_number = 1
),
previous AS (
    SELECT * FROM ranked WHERE observation_number = 2
)
SELECT
    candidate.id AS candidate_id,
    candidate.djinni_key,
    candidate.title,
    candidate.category,
    candidate.specialization,
    candidate.status,
    latest.run_id AS latest_run_id,
    previous.run_id AS previous_run_id,
    latest.observed_at AS latest_observed_at,
    previous.observed_at AS previous_observed_at,
    latest.views_count AS latest_views_count,
    previous.views_count AS previous_views_count,
    latest.views_count - previous.views_count AS views_delta,
    EXTRACT(EPOCH FROM (latest.observed_at - previous.observed_at)) / 3600.0 AS elapsed_hours,
    CASE
        WHEN previous.observed_at IS NOT NULL
         AND latest.observed_at > previous.observed_at
            THEN (latest.views_count - previous.views_count)
                 / NULLIF(EXTRACT(EPOCH FROM (latest.observed_at - previous.observed_at)) / 3600.0, 0)
        ELSE NULL
    END AS views_per_hour,
    CASE
        WHEN previous.observed_at IS NULL THEN NULL
        ELSE latest.observed_at - previous.observed_at < INTERVAL '24 hours'
    END AS is_short_window,
    CASE
        WHEN previous.views_count IS NULL THEN NULL
        ELSE latest.views_count < previous.views_count
    END AS possible_counter_reset,
    latest.observation_count,
    latest.page,
    latest.position_on_page,
    latest.global_rank
FROM public.djinni_candidates AS candidate
JOIN latest ON latest.candidate_id = candidate.id
LEFT JOIN previous ON previous.candidate_id = candidate.id;

COMMENT ON VIEW public.v_candidates_current IS
    'Current tracked candidate cohort. Not complete Djinni population coverage; honor availability and censoring fields.';
COMMENT ON VIEW public.v_candidate_dynamics IS
    'Latest two valid listing view-count observations. Negative deltas may be counter resets; inspect elapsed_hours and is_short_window.';

-- ---------------------------------------------------------------------------
-- Mutation guards: observations are append-only; first_seen_at is immutable.
-- ---------------------------------------------------------------------------

CREATE FUNCTION public.reject_observation_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% are append-only; % is forbidden.', TG_TABLE_NAME, TG_OP;
END
$$;

CREATE TRIGGER job_observations_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON public.djinni_job_observations
FOR EACH STATEMENT EXECUTE FUNCTION public.reject_observation_mutation();

CREATE TRIGGER candidate_observations_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON public.djinni_candidate_observations
FOR EACH STATEMENT EXECUTE FUNCTION public.reject_observation_mutation();

CREATE FUNCTION public.reject_first_seen_at_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.first_seen_at IS DISTINCT FROM OLD.first_seen_at THEN
        RAISE EXCEPTION '% first_seen_at is immutable.', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER jobs_protect_first_seen_at
BEFORE UPDATE ON public.djinni_jobs
FOR EACH ROW EXECUTE FUNCTION public.reject_first_seen_at_mutation();

CREATE TRIGGER candidates_protect_first_seen_at
BEFORE UPDATE ON public.djinni_candidates
FOR EACH ROW EXECUTE FUNCTION public.reject_first_seen_at_mutation();

COMMIT;
