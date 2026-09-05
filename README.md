# djinni-market-etl

**Status:** jobs OK · candidates confirmation-only since Aug 2026

[![ci](https://github.com/kartsan03/djinni-market-etl/actions/workflows/ci.yml/badge.svg)](https://github.com/kartsan03/djinni-market-etl/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](requirements.txt)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A production ETL pipeline for the Ukrainian tech job market. It reads the public pages of [djinni.co](https://djinni.co) into PostgreSQL and tracks the **lifecycle** of every posting and of a monitored cohort of candidate profiles over time — not just snapshots, but the full observation history from which metrics like time-to-close and reopen rates become computable.

What it is not: no login or authenticated scraping, no contact extraction, no proxying or working around access controls, no API (Djinni doesn't have a public one, this parses HTML). It reads only pages that are visible without logging in.

## Status note: candidate listing access

Since August 2026 Djinni redirects the `/developers/` listing to `/login` for anonymous visitors, so candidate *discovery* (walking the listing) is paused. Already-known public `/q/{key}/` profile URLs remain readable, and the candidate scraper runs in a **confirmation-only** mode against the stored cohort: profiles already collected continue to receive real detail/status checks on their confirmation schedule, so the collected history keeps maturing even without new discovery. The listing-discovery machinery is kept in the codebase and covered by tests; the run-audit tables record explicitly when a run was confirmation-only (`run_kind`) and that coverage was never global (`global_coverage = false`).

Candidate data is treated as supply-side context for aggregate analysis only. Keep what you collect local; don't republish it.

## How it works

Three scripts under `automation/`:

- `scraper_djinni_jobs.py` — walks the job listing, ingests unseen postings from their JSON-LD detail pages, then delegates to the reconciler. One run = one audited cycle inside a time budget.
- `reconcile_djinni_jobs.py` — the core of the system. Re-scans the listing, records an immutable observation per seen job per run, and applies the lifecycle state machine below. Never fetches every detail page: absence evidence plus targeted confirmations are enough.
- `scraper_djinni_candidates.py` — incremental frontier discovery of candidate profiles plus scheduled confirmation batches for the monitored cohort, with per-run budget accounting.

### Job lifecycle state machine

```
              new posting ingested from its detail page
                                  │
                                  ▼
                             ┌────────┐   seen again on a later scan
        ┌───────────────────▶│ active │◀────────────────────────────┐
        │                    └────┬────┘         (reopened_count++) │
        │  seen again             │                                 │
        │                         ▼                                 │
        │              1 complete-scan miss                         │
        │                    ┌─────────┐                            │
        │                    │ unknown │  first_complete_scan_absence
        │                    │ (soft)  │  queued for detail confirm │
        │                    └────┬────┘                            │
        │                         │ 2nd consecutive complete miss   │
        │                         ▼                                 │
        │              ┌────────────────────┐                       │
        │              │ inactive_inferred  │───────────────────────┘
        │              └─────────┬──────────┘
        │    detail confirms     │ detail 404
        │    "no longer active"  │
        ▼                        ▼
 ┌───────────────────┐  ┌────────────────────┐
 │ offline_confirmed │  │ deleted_confirmed  │
 └───────────────────┘  └────────────────────┘
```

A first complete-scan miss does **not** invent a separate status. The row stays `unknown` with `status_source = first_complete_scan_absence` until a second independent complete miss promotes it to `inactive_inferred`, or a detail fetch confirms offline/deleted. Terminal statuses are never overwritten by absence.

The load-bearing rule is **absence evidence discipline**. A missing job proves nothing unless the scan itself was provably complete: started at page 1, reached the end of pagination, zero parse errors, page count matches both the expected pages and the site-advertised total, duplicate drift within one page. Partial or failed runs keep their positive observations but never mass-inactivate anything. Closing requires two independent complete absences, and a broken parser that returns HTTP 200 with empty results cannot wipe the dataset because incomplete scans fail those gates by construction.

### Three entities, three questions

| Table | Question it answers |
|---|---|
| `djinni_jobs` / `djinni_candidates` | What is the *current* state of this posting/profile? |
| `djinni_*_scrape_runs` | When and how *completely* did we scan? (audit trail, one row per run, DB-enforced single-writer) |
| `djinni_*_observations` | What did we *see* and when? (append-only history; raw payloads retained for re-parsing) |

`first_seen_at` is immutable — a trigger rejects any UPDATE that would change it; all age metrics derive from it. Observation tables are append-only: UPDATE, DELETE and TRUNCATE are rejected in SQL. A duplicate sighting of the same job or candidate in the same run is ignored (`ON CONFLICT DO NOTHING`), so one run still produces at most one evidence row per entity. Every run carries its `parser_version`, so a parsing change never silently contaminates history.

### Concurrency and safety

- Session-level Postgres advisory locks per scraper, plus a partial unique index allowing at most one `running` reconciliation — a second writer is rejected at the database level, not by convention.
- Time budgets everywhere: the jobs cycle fits a 165-minute internal deadline with a reserved shutdown buffer; candidate confirmation batches size themselves against the remaining budget (14 s/detail) and record whether they exhausted it.
- Abandoned `running` rows left by a crashed process are recovered and marked failed on next startup instead of blocking forever.
- Optional Telegram heartbeat after each run (`TELEGRAM_BOT_TOKEN` unset ⇒ alerts degrade to stdout logging, never crash a scraper).

## Requirements

- Python 3.9+
- PostgreSQL 14+

## Installation

```
git clone https://github.com/kartsan03/djinni-market-etl
cd djinni-market-etl
python -m venv .venv
.venv/Scripts/activate          # Windows; on Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in your PostgreSQL credentials
createdb djinni_market          # or any database named in .env
psql -v ON_ERROR_STOP=1 -f schema.sql
```

`schema.sql` creates the complete final schema: tables, constraints, indexes, analysis views, append-only observation triggers, and `first_seen_at` immutability. `automation/migrations/` is the ordered journal of how this schema actually evolved on the author's database — backup-first steps, checksum-verified snapshots, rollback files — useful as a reference, not required for a fresh install.

## Usage

Both scrapers accept `--dry-run` (parse and report, write nothing). Always start there.

Jobs — one full audited cycle (listing scan + detail ingest of unseen jobs + status confirmations):

```
python automation/scraper_djinni_jobs.py --max-pages 2 --dry-run
python automation/scraper_djinni_jobs.py
```

Candidates — currently the confirmation-only mode described above:

```
python automation/scraper_djinni_candidates.py --confirmation-only
```

Politeness defaults are deliberate: multi-second randomized sleeps between requests, stop-on-block-page behavior (a detected block raises instead of retrying), and pacing flags (`--detail-sleep-min/max`, `--page-sleep-min/max`) if you need slower. Don't run concurrent crawls.

## Tests

Offline unit tests cover the parsers, the state-machine classification, the absence-evidence gate, and the budget arithmetic:

```
python -m unittest discover -s automation/tests
```

## Project structure

```
schema.sql                              baseline schema for a fresh deployment
automation/
  scraper_djinni_jobs.py                jobs listing + detail ingest, one audited cycle
  reconcile_djinni_jobs.py              lifecycle state machine, observations, safety gates
  scraper_djinni_candidates.py          frontier discovery + monitored-cohort confirmations
  telegram_alert.py                     optional heartbeat (no-op without config)
  migrations/                           historical schema journal (see its README)
  tests/                                offline unit tests
.env.example                            PostgreSQL + optional Telegram settings
```

## Analyzing the data

Anything that speaks SQL works: `psql`, DBeaver, pandas (`pd.read_sql`), or a coding agent pointed at the database. Four views ship in `schema.sql`:

| View | What it is |
|---|---|
| `v_jobs_current` | Current vacancy snapshot, including `lifecycle_duration_observable` |
| `v_job_dynamics` | Latest two engagement observations (per-hour rates; ignore windows shorter than 24h) |
| `v_candidates_current` | Tracked cohort only — not the whole Djinni population |
| `v_candidate_dynamics` | Latest two listing view-counts; negative deltas may be counter resets |

List-valued fields (tags, skills, languages) are `text[]`; raw HTML-derived payloads stay in `raw_json_ld` / `raw_list` / `raw_profile` jsonb columns, so fields that were never normalized can still be extracted retroactively — you can re-parse history, you can never re-scrape it.

## Limitations

- Selectors and regexes are tied to Djinni's current HTML. A redesign breaks parsing; the parse functions are unit-tested precisely so those breaks surface as red tests rather than silent data loss.
- Djinni may rate-limit or deny automated traffic. The scripts stop on a block page instead of pushing through. Respect that.
- Candidate listing access is login-walled (see the status note); job scraping is unaffected at the time of writing.
- View/application counters are read from visible page text in English.

## Scraping responsibly

This tool reads only pages visible without logging in and does not collect contacts. Keep it that way:

- The database is output for your own analysis, not a dataset to publish. Don't commit, export, or redistribute scraped records — candidate data least of all.
- Don't lower sleep values to hammer the site; don't add scheduling beyond your own modest cadence.
- Don't add contact extraction, login automation, or anything that works around access controls.
- You are responsible for complying with Djinni's terms of use and for how you use what you collect.

## License

MIT, see [LICENSE](LICENSE).

See also [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).
