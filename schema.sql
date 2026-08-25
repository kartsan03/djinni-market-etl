-- Baseline schema for a fresh PostgreSQL deployment.
-- Mirrors the production database this pipeline runs on (TIMESTAMPTZ throughout).
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
    error_summary JSONB,

    -- Database-level mutex: only one write reconciliation may run at a time
    -- (see the partial unique index below).
    CONSTRAINT djinni_job_scrape_runs_status_check
        CHECK (status IN ('running', 'complete', 'partial', 'failed'))
);

-- Only one running reconciliation can ever exist.
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
    exp_text_extracted TEXT,
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
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- immutable
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

    first_seen_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
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

COMMIT;
