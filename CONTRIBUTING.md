# Contributing

## Setup

```
git clone https://github.com/kartsan03/djinni-market-etl
cd djinni-market-etl
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m unittest discover -s automation/tests
```

A live run additionally needs PostgreSQL credentials in `.env` (see `.env.example`) and `psql -v ON_ERROR_STOP=1 -f schema.sql` on a fresh database. Always start with `--dry-run`.

## Ground rules

- Public pages only. No login, no contact extraction, no proxies, no working around access controls.
- Absence evidence only on a structurally complete scan. Partial or failed runs keep positive observations and never mass-inactivate jobs.
- Candidate coverage is a tracked cohort: `global_coverage` is always false. Do not infer candidate inactivity from listing absence.
- Schema changes go in `schema.sql` (the fresh-install contract). `automation/migrations/` is a historical journal of the author's database, not the install path for new clones.
- No scraped records, `.env` files, or database dumps in commits.
- New parse/classifier logic needs an offline unit test. HTML-shape tests are how selector drift shows up as a red CI run instead of silent data loss.

## Pull requests

Keep them small. CI runs `python -m unittest discover -s automation/tests` on Python 3.9 and 3.12.
