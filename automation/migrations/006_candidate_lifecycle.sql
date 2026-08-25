-- Candidate monitored-cohort lifecycle, run audit, immutable observations, and MCP views.
-- Global Djinni candidate coverage is intentionally NOT claimed: the public listing is ~90k profiles.
-- Run after 005_backup_candidates.sql.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.all_candidates') IS NULL
       OR to_regclass('public.all_candidate_observations') IS NULL THEN
        RAISE EXCEPTION 'Candidate base tables are missing.';
    END IF;
    IF to_regclass('backup.all_candidates_before_lifecycle_20260731') IS NULL
       OR to_regclass('backup.all_candidate_observations_before_lifecycle_20260731') IS NULL
       OR to_regclass('backup.candidate_backup_manifest') IS NULL THEN
        RAISE EXCEPTION 'Verified and sealed candidate backups are required; run migrations 005 and 005a first.';
    END IF;
    IF to_regclass('public.candidate_scrape_runs') IS NOT NULL THEN
        RAISE EXCEPTION 'Candidate lifecycle schema already exists; migration 006 is not rerunnable.';
    END IF;
END
$$;

LOCK TABLE public.all_candidates IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.all_candidate_observations IN ACCESS EXCLUSIVE MODE;
LOCK TABLE backup.all_candidates_before_lifecycle_20260731 IN ACCESS EXCLUSIVE MODE;
LOCK TABLE backup.all_candidate_observations_before_lifecycle_20260731 IN ACCESS EXCLUSIVE MODE;
LOCK TABLE backup.candidate_backup_manifest IN ACCESS SHARE MODE;

DO $$
DECLARE
    candidate_count BIGINT;
    candidate_checksum TEXT;
    observation_count BIGINT;
    observation_checksum TEXT;
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
        RAISE EXCEPTION 'Live candidates drifted after backup; migration 006 refused.';
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
        RAISE EXCEPTION 'Live candidate observations drifted after backup; migration 006 refused.';
    END IF;

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
        RAISE EXCEPTION 'Sealed candidate backup integrity check failed.';
    END IF;
END
$$;

CREATE TABLE public.candidate_scrape_runs (
    id BIGSERIAL PRIMARY KEY,
    run_kind TEXT NOT NULL DEFAULT 'incremental_listing_and_confirmation',
    started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    heartbeat_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'complete', 'partial', 'failed')),
    scope_complete BOOLEAN NOT NULL DEFAULT FALSE,
    global_coverage BOOLEAN NOT NULL DEFAULT FALSE,
    sortby TEXT NOT NULL DEFAULT 'date',
    min_views INTEGER NOT NULL DEFAULT 0,
    max_pages INTEGER,
    detail_limit INTEGER,
    listing_budget_minutes NUMERIC NOT NULL,
    internal_deadline_at TIMESTAMP WITHOUT TIME ZONE,
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
    new_frontier_key TEXT,
    prior_frontier_keys TEXT[],
    new_frontier_keys TEXT[],
    prior_frontier_published_date DATE,
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
    CHECK (global_coverage = FALSE)
);

CREATE UNIQUE INDEX uq_candidate_scrape_runs_one_running
    ON public.candidate_scrape_runs ((status))
    WHERE status = 'running';
CREATE INDEX idx_candidate_scrape_runs_started
    ON public.candidate_scrape_runs(started_at DESC);

ALTER TABLE public.all_candidates
    ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN status_source TEXT NOT NULL DEFAULT 'migration_unknown',
    ADD COLUMN status_checked_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN inactive_detected_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN reopened_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN last_listing_seen_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN last_detail_checked_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN detail_scraped_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN engagement_observed_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN monitoring_cohort TEXT NOT NULL DEFAULT 'prospective_incremental',
    ADD COLUMN monitoring_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN next_confirmation_due_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN lifecycle_data_quality TEXT NOT NULL DEFAULT 'prospective_incremental',
    ADD COLUMN lifecycle_tracking_started_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN entry_left_censored BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN last_parser_version TEXT;

ALTER TABLE public.all_candidates
    ADD CONSTRAINT all_candidates_status_check
        CHECK (status IN ('unknown', 'active', 'offline_confirmed', 'deleted_confirmed')),
    ADD CONSTRAINT all_candidates_lifecycle_quality_check
        CHECK (lifecycle_data_quality IN ('legacy_min_views_ge_5', 'prospective_incremental'));

ALTER TABLE public.all_candidate_observations
    ALTER COLUMN candidate_id SET NOT NULL,
    ALTER COLUMN views_count DROP NOT NULL,
    ALTER COLUMN views_count DROP DEFAULT,
    ADD COLUMN run_id BIGINT REFERENCES public.candidate_scrape_runs(id) ON DELETE SET NULL,
    ADD COLUMN observation_type TEXT NOT NULL DEFAULT 'legacy_listing',
    ADD COLUMN status TEXT,
    ADD COLUMN status_source TEXT,
    ADD COLUMN position_on_page INTEGER,
    ADD COLUMN global_rank INTEGER,
    ADD COLUMN http_status INTEGER,
    ADD COLUMN parse_ok BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN parse_error TEXT,
    ADD COLUMN parser_version TEXT;

ALTER TABLE public.all_candidate_observations
    ADD CONSTRAINT all_candidate_observations_type_check
        CHECK (observation_type IN ('legacy_listing', 'listing', 'detail_status')),
    ADD CONSTRAINT all_candidate_observations_status_check
        CHECK (status IS NULL OR status IN ('unknown', 'active', 'offline_confirmed', 'deleted_confirmed'));

CREATE UNIQUE INDEX uq_candidate_observation_run_evidence
    ON public.all_candidate_observations(run_id, candidate_id, observation_type)
    WHERE run_id IS NOT NULL;
CREATE INDEX idx_candidate_observation_candidate_time
    ON public.all_candidate_observations(candidate_id, observed_at DESC, id DESC);
CREATE INDEX idx_candidate_observation_run
    ON public.all_candidate_observations(run_id);
CREATE INDEX idx_all_candidates_status
    ON public.all_candidates(status);
CREATE INDEX idx_all_candidates_confirmation_due
    ON public.all_candidates(next_confirmation_due_at, id)
    WHERE monitoring_enabled;

UPDATE public.all_candidate_observations
SET parser_version = 'legacy_candidate_parser_v1'
WHERE parser_version IS NULL;

UPDATE public.all_candidates AS candidate
SET
    status = 'unknown',
    status_source = 'migration_unknown',
    status_checked_at = NULL,
    inactive_detected_at = NULL,
    reopened_count = 0,
    last_listing_seen_at = (
        SELECT MAX(observation.observed_at)
        FROM public.all_candidate_observations AS observation
        WHERE observation.candidate_id = candidate.id
    ),
    engagement_observed_at = (
        SELECT observation.observed_at
        FROM public.all_candidate_observations AS observation
        WHERE observation.candidate_id = candidate.id
          AND observation.views_count IS NOT NULL
        ORDER BY observation.observed_at DESC, observation.id DESC
        LIMIT 1
    ),
    monitoring_cohort = 'legacy_min_views_ge_5',
    monitoring_enabled = TRUE,
    next_confirmation_due_at = CURRENT_TIMESTAMP,
    lifecycle_data_quality = 'legacy_min_views_ge_5',
    lifecycle_tracking_started_at = NULL,
    entry_left_censored = TRUE,
    last_parser_version = 'legacy_candidate_parser_v1';

COMMENT ON TABLE public.candidate_scrape_runs IS
    'Audit ledger for bounded incremental candidate discovery and monitored-cohort confirmations; complete is scope-local, never global Djinni coverage.';
COMMENT ON COLUMN public.candidate_scrape_runs.scope_complete IS
    'True only when the declared incremental/frontier and selected confirmation batch completed.';
COMMENT ON COLUMN public.candidate_scrape_runs.global_coverage IS
    'Always false: the canonical candidate dataset is a tracked cohort, not all public Djinni candidates.';
COMMENT ON COLUMN public.all_candidates.status IS
    'Current monitored-profile status from positive listing/detail evidence or explicit offline/404 confirmation.';
COMMENT ON COLUMN public.all_candidates.status_checked_at IS
    'Evidence timestamp; not necessarily the exact time availability changed.';
COMMENT ON COLUMN public.all_candidates.is_confirmed IS
    'Djinni verification badge ever observed; unrelated to lifecycle availability.';
COMMENT ON COLUMN public.all_candidates.lifecycle_data_quality IS
    'legacy_min_views_ge_5 is a left-censored historical filtered cohort; prospective_incremental is tracked after migration 006.';
COMMENT ON COLUMN public.all_candidates.lifecycle_tracking_started_at IS
    'First reliable post-migration lifecycle evidence. NULL means not yet checked under lifecycle tracking.';
COMMENT ON COLUMN public.all_candidate_observations.views_count IS
    'Listing-card view count; NULL when unavailable or when evidence came only from a detail status check.';
COMMENT ON COLUMN public.all_candidate_observations.observation_type IS
    'legacy_listing/listing carry listing evidence; detail_status carries immutable profile-status evidence.';

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
FROM public.all_candidates AS candidate;

CREATE VIEW public.v_candidate_dynamics AS
WITH ranked AS (
    SELECT
        observation.*,
        row_number() OVER (
            PARTITION BY observation.candidate_id
            ORDER BY observation.observed_at DESC, observation.id DESC
        ) AS observation_number,
        count(*) OVER (PARTITION BY observation.candidate_id) AS observation_count
    FROM public.all_candidate_observations AS observation
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
FROM public.all_candidates AS candidate
JOIN latest ON latest.candidate_id = candidate.id
LEFT JOIN previous ON previous.candidate_id = candidate.id;

COMMENT ON VIEW public.v_candidates_current IS
    'Current MCP-facing tracked candidate cohort. It is not complete global Djinni population coverage; honor availability and censoring fields.';
COMMENT ON VIEW public.v_candidate_dynamics IS
    'Latest two valid listing view-count observations. Preserve negative deltas as possible resets; inspect elapsed_hours and is_short_window.';

COMMIT;

SELECT
    COUNT(*) AS candidates,
    COUNT(*) FILTER (WHERE lifecycle_data_quality='legacy_min_views_ge_5') AS legacy_left_censored,
    COUNT(*) FILTER (WHERE lifecycle_tracking_started_at IS NOT NULL) AS lifecycle_started,
    (SELECT COUNT(*) FROM public.all_candidate_observations) AS observations,
    (SELECT COUNT(*) FROM public.v_candidate_dynamics) AS dynamics_rows
FROM public.all_candidates;
