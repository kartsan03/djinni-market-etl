-- Realign active candidate confirmation due dates to the 14-day cadence.
-- Uses only real positive listing/detail/status evidence timestamps.

BEGIN;

WITH evidence AS (
    SELECT
        id,
        GREATEST(detail_scraped_at, last_listing_seen_at, status_checked_at)
            AS latest_positive_evidence_at
    FROM public.all_candidates
    WHERE status = 'active'
      AND raw_profile IS NOT NULL
), updated AS (
    UPDATE public.all_candidates AS candidate
    SET next_confirmation_due_at = evidence.latest_positive_evidence_at + INTERVAL '14 days'
    FROM evidence
    WHERE candidate.id = evidence.id
      AND evidence.latest_positive_evidence_at IS NOT NULL
      AND candidate.next_confirmation_due_at IS DISTINCT FROM
          evidence.latest_positive_evidence_at + INTERVAL '14 days'
    RETURNING candidate.id
)
SELECT COUNT(*) AS active_due_dates_realigned
FROM updated;

COMMIT;
