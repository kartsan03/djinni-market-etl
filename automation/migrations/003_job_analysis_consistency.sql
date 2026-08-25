-- Post-deployment consistency patch for installations that already ran migration 002.
-- Safe to run repeatedly.

BEGIN;

UPDATE public.all_jobs
SET company_url = NULLIF(
    jsonb_path_query_first(
        raw_json_ld,
        'lax $.hiringOrganization.sameAs[*]'
    ) #>> '{}',
    ''
);

COMMIT;

SELECT
    COUNT(*) AS total_jobs,
    COUNT(*) FILTER (WHERE company_url IS NOT NULL) AS with_company_url
FROM public.all_jobs;
