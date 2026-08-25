-- Recover candidate publication dates from real detail evidence already stored in raw_profile.
-- Idempotent: only fills NULL published_at values and never overwrites existing dates.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.all_candidates') IS NULL THEN
        RAISE EXCEPTION 'public.all_candidates is missing';
    END IF;
END
$$;

CREATE TEMP TABLE candidate_published_at_recovery ON COMMIT DROP AS
SELECT DISTINCT ON (candidate.id)
    candidate.id,
    to_date(
        replace(evidence.item->>'value', 'Published ', ''),
        'FMDD FMMonth YYYY'
    ) AS published_at
FROM public.all_candidates AS candidate
CROSS JOIN LATERAL jsonb_array_elements(
    COALESCE(candidate.raw_profile->'aside', '[]'::jsonb)
) AS evidence(item)
WHERE candidate.published_at IS NULL
  AND evidence.item->>'icon' = 'clock'
  AND evidence.item->>'value' ~ '^Published [0-9]{1,2} [A-Za-z]+ [0-9]{4}$'
ORDER BY candidate.id;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM candidate_published_at_recovery
        WHERE published_at IS NULL
           OR published_at > CURRENT_DATE + 1
    ) THEN
        RAISE EXCEPTION 'Candidate publication-date recovery produced an invalid date';
    END IF;
END
$$;

UPDATE public.all_candidates AS candidate
SET published_at = recovery.published_at
FROM candidate_published_at_recovery AS recovery
WHERE candidate.id = recovery.id
  AND candidate.published_at IS NULL;

SELECT
    (SELECT COUNT(*) FROM candidate_published_at_recovery) AS recovered_from_raw_profile,
    (SELECT COUNT(*) FROM public.all_candidates WHERE published_at IS NULL) AS remaining_without_published_at;

COMMIT;
