-- Additive lifecycle foundation for scraper_v2.py.
-- Safe for the legacy scraper: existing INSERT statements do not need new columns.

BEGIN;

-- Intentionally fail on name collisions: a partial/manual schema must be inspected,
-- not silently accepted as compatible.
ALTER TABLE public.all_jobs
    ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN status_checked_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN status_source TEXT,
    ADD COLUMN first_seen_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN last_seen_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN inactive_detected_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN reopened_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN consecutive_complete_misses INTEGER NOT NULL DEFAULT 0;

-- scraped_at is the only truthful historical sighting available for legacy rows.
UPDATE public.all_jobs
SET
    first_seen_at = COALESCE(first_seen_at, scraped_at, CURRENT_TIMESTAMP),
    last_seen_at = COALESCE(last_seen_at, scraped_at, CURRENT_TIMESTAMP)
WHERE first_seen_at IS NULL
   OR last_seen_at IS NULL;

ALTER TABLE public.all_jobs
    ALTER COLUMN first_seen_at SET DEFAULT CURRENT_TIMESTAMP,
    ALTER COLUMN last_seen_at SET DEFAULT CURRENT_TIMESTAMP,
    ALTER COLUMN first_seen_at SET NOT NULL,
    ALTER COLUMN last_seen_at SET NOT NULL;

ALTER TABLE public.all_jobs
    ADD CONSTRAINT all_jobs_status_check
    CHECK (status IN (
        'unknown',
        'active',
        'inactive_inferred',
        'offline_confirmed',
        'deleted_confirmed'
    ));

CREATE TABLE public.job_scrape_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP WITHOUT TIME ZONE,
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

CREATE TABLE public.all_job_observations (
    id BIGSERIAL PRIMARY KEY,
    job_id INTEGER NOT NULL
        REFERENCES public.all_jobs(id) ON DELETE CASCADE,
    djinni_id INTEGER NOT NULL,
    run_id BIGINT NOT NULL
        REFERENCES public.job_scrape_runs(id) ON DELETE CASCADE,
    observed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
    UNIQUE (run_id, job_id)
);

CREATE INDEX idx_all_jobs_status
    ON public.all_jobs(status);

CREATE INDEX idx_all_jobs_last_seen
    ON public.all_jobs(last_seen_at DESC);

CREATE INDEX idx_job_observations_job_time
    ON public.all_job_observations(job_id, observed_at DESC);

CREATE INDEX idx_job_observations_run
    ON public.all_job_observations(run_id);

-- A durable DB-level mutex: only one write reconciler may be running.
CREATE UNIQUE INDEX job_scrape_runs_one_running
    ON public.job_scrape_runs ((status))
    WHERE status = 'running';

COMMIT;

-- Expected immediately after migration: every legacy row is status='unknown'.
SELECT status, COUNT(*)
FROM public.all_jobs
GROUP BY status
ORDER BY status;
