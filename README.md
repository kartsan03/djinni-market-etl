# djinni-market-etl

A local ETL tool for the Ukrainian tech job market. It parses public listing pages from [djinni.co](https://djinni.co) into a SQLite file, so salary ranges, categories, tags and posting activity can be analyzed with plain SQL.

- `scrape_jobs.py` is the main collector. It walks the job listing pages and saves each posting: title, company, category, salary range, experience requirement, description, tags, view and application counters, plus the raw JSON-LD from the page.
- `scrape_candidates.py` is an optional second collector for the public candidate listing: title, category, salary expectation, experience, languages, skills. It exists for local aggregate analysis of the supply side of the market; keep what it collects in the local database and don't republish it.

What it does not do: no login or authenticated scraping, no contact extraction, no proxying or working around access controls, no scheduling, no API (Djinni doesn't have a public one, this parses HTML), no export beyond the SQLite file itself. It reads only pages that are visible without logging in.

## Requirements

- Python 3.9+
- No database server. Storage is a single SQLite file created on first run.

## Installation

```
git clone https://github.com/kartsan03/djinni-market-etl
cd djinni-market-etl
python -m venv .venv
.venv/Scripts/activate        # Windows; on Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

The only setting in `.env` is `DB_PATH`, the location of the SQLite file. The default (`djinni.db` in the project directory) is fine; the scripts also run with no `.env` at all.

## Usage

Both scripts create their own tables on first run, so there is no separate database setup step.

Scrape jobs, first two listing pages only:

```
python scrape_jobs.py --max-pages 2
```

Scrape candidate profiles with at least 5 views, one page, without writing anything:

```
python scrape_candidates.py --max-pages 1 --dry-run
```

Full runs (`--max-pages` omitted) walk the entire listing and can take hours; start with a limited run to check that parsing still works. Already-scraped jobs are skipped, and already-scraped candidates are updated without re-fetching their detail page unless you pass `--refresh-existing`.

Useful flags on `scrape_candidates.py`:

- `--min-views N` saves only profiles with at least N views (default 5, use 0 for everything)
- `--min-salary N`, `--min-exp-months N` skip cards below a salary or experience floor
- `--sortby date|experience|salary_max|salary_min` listing sort order
- `--detail-sleep-min/max`, `--page-sleep-min/max` politeness delays in seconds

`scrape_jobs.py` has the same `--start-page`, `--max-pages`, `--dry-run`, and sleep flags.

## Output

The database is append-only: no run ever deletes anything, so repeat local runs accumulate a market dataset over time. Jobs are written once, as a snapshot of the posting at scrape time (including its view/application counters), and skipped on later runs. Candidates are updated in place on re-runs, with every sighting logged to the observations table for aggregate scrape-time counters over repeat local runs.

Everything lands in one SQLite file with three tables:

- `jobs`, one row per posting, unique on `djinni_id`
- `candidates`, one row per profile, unique on `djinni_key`, updated in place on re-runs
- `candidate_observations`, one row per sighting of a profile, for aggregate scrape-time counters over repeat local runs

The full column list is the `SCHEMA_SQL` block at the top of each script. List-valued fields (tags, skills, languages) are stored as JSON text; SQLite's `json_each()` unpacks them. Raw parsed fields are also kept in `raw_json_ld` / `raw_profile`, mainly for debugging and repairing the parser when a field the normalized schema doesn't break out is needed. Don't publish this database.

Check what a run wrote:

```
sqlite3 djinni.db "SELECT COUNT(*) FROM jobs"
sqlite3 djinni.db "SELECT title, salary_min, salary_max, views_count FROM jobs ORDER BY id DESC LIMIT 5"
```

## Analyzing the data (including with AI agents)

The output is a plain SQLite file, so anything that reads SQLite works: the `sqlite3` CLI, pandas (`pd.read_sql`), DBeaver, whatever you already use.

That also covers AI-assisted analysis. A coding agent with shell access (Claude Code, Codex CLI and similar) needs no setup at all: point it at the file and it will run `sqlite3 djinni.db "..."` queries itself. For chat clients without shell access, connect any SQLite MCP server to `djinni.db`. Note that a Postgres MCP server will not work here; earlier versions of these scripts wrote to Postgres, this one does not.

## Project structure

```
db.py                   database connection (reads DB_PATH from .env)
scrape_jobs.py          job postings scraper
scrape_candidates.py    candidate profiles scraper
```

## Limitations

- Selectors and regexes are tied to Djinni's current HTML. A site redesign breaks parsing; the places to fix are `parse_job_page()` in `scrape_jobs.py` and `parse_listing_card()` / `parse_profile_detail()` in `scrape_candidates.py`.
- Djinni may rate-limit or deny automated traffic. Both scripts use conservative delays by default and stop on an access-denied page instead of retrying. Start with small `--max-pages` values and don't run concurrent crawls.
- View/application counters are read from visible page text in English. If Djinni serves you another language, those counters come back as 0.
- Salary parsing assumes $, €, or ₴ amounts as Djinni currently formats them.

## Troubleshooting

- "Djinni denied access": stop the run. Come back later (hours, not minutes) with larger `--*-sleep-*` values and smaller runs, not sooner with more retries.
- Rows have empty titles/salaries but the run "works": the HTML changed. Run with `--dry-run`, look at what prints, then adjust the selectors named above.
- `sqlite3.OperationalError: database is locked`: don't run both scripts against the same file at the same time, or point them at different `DB_PATH`s.

## Scraping responsibly

This tool reads only pages visible without logging in and does not collect contacts. It sleeps several seconds between requests by default. Keep it that way:

- The database is output for your own analysis, not a dataset to publish. Don't commit, export, or redistribute scraped records, candidate data least of all.
- Don't lower the sleep values to hammer the site, and don't run scheduled or concurrent crawls.
- Don't add contact extraction, login automation, or anything that works around access controls.
- You are responsible for complying with Djinni's terms of use and for how you use what you collect.

## License

MIT, see [LICENSE](LICENSE).
