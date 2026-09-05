# Analysis recipes

Anonymized PostgreSQL queries against the shipped schema/views. Aggregate only — no profile or company identifiers in the result set. Apply `schema.sql` first.

Use `lifecycle_duration_observable` from `v_jobs_current` before treating lifespans as complete (only `prospective` rows have a trustworthy open clock).

## 1. Time-to-close (days) for jobs that left active

```sql
SELECT
    percentile_cont(0.5) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (inactive_detected_at - first_seen_at)) / 86400.0
    ) AS median_days_to_inactive,
    count(*) AS n
FROM v_jobs_current
WHERE lifecycle_duration_observable
  AND inactive_detected_at IS NOT NULL
  AND status IN ('inactive_inferred', 'offline_confirmed', 'deleted_confirmed');
```

## 2. Reopen rate among jobs that went inactive at least once

```sql
SELECT
    count(*) FILTER (WHERE reopened_count > 0) AS reopened,
    count(*) AS ever_inactive,
    round(
        100.0 * count(*) FILTER (WHERE reopened_count > 0) / nullif(count(*), 0),
        1
    ) AS reopen_pct
FROM v_jobs_current
WHERE inactive_detected_at IS NOT NULL
   OR reopened_count > 0;
```

## 3. Engagement velocity (views/hour) on short observation windows

```sql
SELECT
    percentile_cont(0.5) WITHIN GROUP (ORDER BY views_per_hour) AS median_views_per_hour,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY apps_per_hour) AS median_apps_per_hour,
    count(*) AS n
FROM v_job_dynamics
WHERE is_short_window IS TRUE
  AND views_per_hour IS NOT NULL;
```

## 4. Active vs inactive mix (current snapshot)

```sql
SELECT status, count(*) AS n
FROM v_jobs_current
GROUP BY status
ORDER BY n DESC;
```

## 5. Candidate cohort confirmation ages (no PII)

```sql
SELECT
    status,
    count(*) AS n,
    percentile_cont(0.5) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (now() - first_seen_at)) / 86400.0
    ) AS median_age_days
FROM v_candidates_current
GROUP BY status
ORDER BY n DESC;
```

Candidate listing discovery has been confirmation-only since August 2026; these aggregates still mature on the stored cohort.
