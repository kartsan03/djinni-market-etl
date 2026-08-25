-- Audit bounded jobs detail-confirmation attempts and prevent retry starvation.

BEGIN;

ALTER TABLE public.all_jobs
    ADD COLUMN IF NOT EXISTS detail_attempted_at TIMESTAMP WITHOUT TIME ZONE;

CREATE INDEX IF NOT EXISTS idx_all_jobs_unknown_detail_attempt
    ON public.all_jobs(detail_attempted_at NULLS FIRST, id)
    WHERE status = 'unknown'
      AND status_source = 'first_complete_scan_absence';

COMMIT;
