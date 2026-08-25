# Jobs and candidates lifecycle migrations

> **Two paths.**
> * **Fresh deployment:** run `schema.sql` in the repository root once — it creates
>   the final `djinni_*` schema. You do not need the files below.
> * **Historical journal:** the numbered files are the ordered record of how the
>   production database actually evolved — backup-first steps with checksum
>   verification, additive lifecycle columns, sealed snapshots, `*_down.sql`
>   rollbacks. They assume base tables that predate migration 001 and use the
>   legacy `all_*` table names renamed by `015_rename_djinni_tables.sql`.
>
> Apply a journal step with: `psql -v ON_ERROR_STOP=1 -f automation/migrations/NNN_....sql`.
> `*_down.sql` files are emergency rollback only.

This copy of the journal tracks the Djinni tables (000–016). Later migrations for
the gold/analytics layers live outside this repository's scope.

## 1. Backup

Open and execute the whole file:

`000_backup_all_jobs.sql`

Expected final result: `source_rows` equals `backup_rows` and both checksums match (currently 25,609 rows). The script intentionally fails if the named backup already exists; do not rerun it blindly.

## 2. Add lifecycle schema

Open and execute the whole file:

`001_job_lifecycle.sql`

Expected final result immediately after migration:

```text
status   count
unknown  25609
```

The exact count can be higher if the legacy scraper inserts jobs before migration.

## 3. Add normalized analysis fields

Open and execute the whole file:

`002_job_analysis_fields.sql`

This backfills legacy rows from their own stored JSON-LD and real observations. It also creates `v_jobs_current` and `v_job_dynamics` for analysis.

Then run the post-deployment consistency/semantics patches:

`003_job_analysis_consistency.sql`

`004_job_analysis_semantics.sql`

Migration 004 marks pre-Step-0 rows as left-censored legacy snapshots, normalizes country arrays, and prevents short observation windows from being presented as stable daily rates.

## 4. Validation

Do not run either `*_down.sql` file. They are emergency rollback only.

After the jobs migrations are installed, run parser/unit checks and the unified scraper.

The reconciler:

- never changes legacy `views_count`, `apps_count`, or `url`;
- allows only one write run at a time;
- records absence only when every expected page and the final advertised card count were scanned, duplicate pagination shifts stay within one page, and there are zero parse errors;
- requires two independent complete absences before assigning `inactive_inferred`;
- leaves a partial/failed run without mass-inactivating jobs;
- automatically retries one canonical full scan when the first attempt is structurally partial, but only when the first attempt used at most 75 minutes;
- keeps jobs work inside a 165-minute one-click budget and leaves unresolved first-miss detail confirmations safely queued before the three-hour desktop process-tree timeout.

# Candidate monitored-cohort lifecycle

The public Djinni candidate index is roughly 90,000 profiles / 9,000 pages. The canonical candidate dataset is therefore explicitly a **tracked cohort**, not a claim of complete global population coverage.

## 5. Candidate backup

Run once before candidate lifecycle changes:

`005_backup_candidates.sql`

It snapshots both candidate tables and verifies full-row equality inside the transaction. The named backups are immutable and the migration intentionally refuses to overwrite them.

Seal and fingerprint the immutable snapshots:

`005a_candidate_backup_manifest.sql`

This second step refuses to seal if live rows already drifted from the backup and stores full-row checksums used again by migration and rollback.

## 6. Candidate lifecycle, runs, and views

Run:

`006_candidate_lifecycle.sql`

This creates:

- `djinni_candidate_scrape_runs` for bounded-scope audit and interruption recovery;
- explicit candidate status/lifecycle fields without absence inference;
- immutable listing/detail-status observations with run provenance.

Historical rows are labeled `legacy_min_views_ge_5`, left-censored, and initially `unknown`. No historical status or lifecycle date is invented.

The default candidate scraper:

- incrementally discovers newly published profiles using newest-first frontier overlap;
- stores prospective profiles even when views are zero;
- confirms a due monitored batch through detail pages;
- changes lifecycle status only from positive listing/detail evidence, explicit offline content, or 404/410;
- treats blocks, timeouts, ambiguous HTML, filtered runs, and unreached frontiers as partial/failed evidence;
- never infers candidate inactivity from bounded listing absence;
- always records `global_coverage = false`.

## 7. Archive hardening

Run:

`007_candidate_archive_hardening.sql`

This transfers DB snapshots to separate no-login owner roles, gives the application role explicit read-only archive ACLs, installs mutation-blocking archive triggers, and makes live candidate observations insert-only. PostgreSQL superusers can bypass database ACLs by definition, so independent binary COPY archives and SHA-256 hashes are retained outside the database as well.

`006_candidate_lifecycle_down.sql` is destructive emergency rollback. It validates the sealed snapshot, restores the exact pre-lifecycle candidate rows, and removes every post-migration candidate/observation.

## 8. Candidate publication-date parser recovery

Run the idempotent data repair:

`008_candidate_published_at_recovery.sql`

It fills only NULL `published_at` values when an absolute Djinni publication date is already present in the candidate's stored `raw_profile.aside` evidence. It never invents dates or overwrites existing values. The candidate scraper also:

- supports `--confirmation-only` to hydrate due listing-only profiles without starting another discovery crawl;
- aborts immediately when the first listing page loses all publication-date or category metadata;
- stores frontier anchors from multiple leading pages instead of relying on only ten volatile keys;
- gives listing discovery a 75-minute budget while retaining the 165-minute internal deadline and three-hour desktop hard timeout;
- caps newly tracked profiles per run with `--max-new-candidates` (default 500), matching the default detail-confirmation capacity so listing discovery cannot create an unbounded enrichment backlog.

## 9. Candidate frontier audit parameters

Run:

`009_candidate_frontier_audit.sql`

It adds nullable, validated `new_candidate_limit` and `frontier_key_pages` columns to `djinni_candidate_scrape_runs`. Existing historical runs remain NULL rather than receiving invented settings; future runs record the actual discovery bounds used.

## 10. Candidate confirmation-budget audit

Run:

`010_candidate_confirmation_budget.sql`

It adds nullable run metrics for due backlog before/after, mandatory pending-profile enrichment, scheduled confirmations of already enriched candidates, and deadline-budget exhaustion. Historical runs remain NULL. Active candidates become due every 14 days; inactive candidates remain on the 30-day cadence.

As of 2026-08-13, Djinni redirects `/developers/` to `/login` for anonymous visitors, while already-known public `/q/{key}/` profile URLs remain readable. Candidate runs are therefore intentionally confirmation-only: discovery/frontier progress and listing-view observations are paused, but the stored cohort continues to receive real detail/status checks. The one-click confirmation batch is capped at 1,000 candidates, uses a 330-minute internal deadline, a six-hour desktop process-tree timeout, and 7–14 second detail pacing (1.4x the earlier 5–10 second interval). The rollback tag `candidates-before-login-wall-confirmation-only` preserves the last discovery-capable implementation.

## 11. Candidate active-confirmation cadence

Run:

`011_candidate_active_confirmation_cadence.sql`

It realigns existing active/enriched candidates to the 14-day schedule using their latest real listing/detail/status evidence timestamp. It does not change lifecycle status or invent observations. Listing-only profiles remain immediately due for mandatory enrichment; inactive candidates retain their 30-day schedule.

## 12. Jobs detail-attempt audit

Run:

`012_job_detail_attempt_audit.sql`

It records the real time of each bounded first-miss detail attempt and indexes the unknown confirmation queue. Failed low-ID jobs rotate behind older/unattempted work instead of starving the rest of the queue. This is attempt metadata, not positive lifecycle evidence.
