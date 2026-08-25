-- Audit the bounded candidate discovery parameters introduced after parser recovery.

BEGIN;

ALTER TABLE public.candidate_scrape_runs
    ADD COLUMN IF NOT EXISTS new_candidate_limit INTEGER,
    ADD COLUMN IF NOT EXISTS frontier_key_pages INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.candidate_scrape_runs'::regclass
          AND conname = 'candidate_scrape_runs_new_candidate_limit_check'
    ) THEN
        ALTER TABLE public.candidate_scrape_runs
            ADD CONSTRAINT candidate_scrape_runs_new_candidate_limit_check
            CHECK (new_candidate_limit IS NULL OR new_candidate_limit >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.candidate_scrape_runs'::regclass
          AND conname = 'candidate_scrape_runs_frontier_key_pages_check'
    ) THEN
        ALTER TABLE public.candidate_scrape_runs
            ADD CONSTRAINT candidate_scrape_runs_frontier_key_pages_check
            CHECK (frontier_key_pages IS NULL OR frontier_key_pages >= 1);
    END IF;
END
$$;

COMMIT;
