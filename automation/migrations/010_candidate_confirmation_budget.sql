-- Separate new-profile hydration from scheduled existing-profile confirmations.
-- Historical runs remain NULL for the new audit metrics rather than receiving invented zeros.

BEGIN;

ALTER TABLE public.candidate_scrape_runs
    ADD COLUMN IF NOT EXISTS due_candidates_before INTEGER,
    ADD COLUMN IF NOT EXISTS due_candidates_after INTEGER,
    ADD COLUMN IF NOT EXISTS pending_enrichment_selected INTEGER,
    ADD COLUMN IF NOT EXISTS pending_enrichment_attempted INTEGER,
    ADD COLUMN IF NOT EXISTS profiles_hydrated INTEGER,
    ADD COLUMN IF NOT EXISTS scheduled_existing_selected INTEGER,
    ADD COLUMN IF NOT EXISTS scheduled_existing_attempted INTEGER,
    ADD COLUMN IF NOT EXISTS confirmation_budget_exhausted BOOLEAN;

DO $$
DECLARE
    column_name TEXT;
    constraint_name TEXT;
BEGIN
    FOREACH column_name IN ARRAY ARRAY[
        'due_candidates_before',
        'due_candidates_after',
        'pending_enrichment_selected',
        'pending_enrichment_attempted',
        'profiles_hydrated',
        'scheduled_existing_selected',
        'scheduled_existing_attempted'
    ]
    LOOP
        constraint_name := 'candidate_scrape_runs_' || column_name || '_check';
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'public.candidate_scrape_runs'::regclass
              AND conname = constraint_name
        ) THEN
            EXECUTE format(
                'ALTER TABLE public.candidate_scrape_runs ADD CONSTRAINT %I CHECK (%I IS NULL OR %I >= 0)',
                constraint_name, column_name, column_name
            );
        END IF;
    END LOOP;
END
$$;

COMMIT;
