# Changelog

## Unreleased

## 1.1.1 - 2026-09-05

### Fixed

- Fresh-install `schema.sql` now actually ships the analysis views, append-only observation triggers, and `first_seen_at` immutability that the README already described.
- Job lifecycle diagram: a first complete-scan miss stays `unknown` (`first_complete_scan_absence`), not a fictional `offline_assumed` status.
- Same-run duplicate job observations are ignored (`ON CONFLICT DO NOTHING`) so the append-only trigger is not fighting an upsert.
- Schema-check errors point at `schema.sql` for a fresh database, and at the historical journal only for existing DBs.
- UTF-8 BOM stripped from the two scraper entry points.

## 1.1.0 - 2026-08-25

### Changed

- Replaced the SQLite MVP with the production PostgreSQL lifecycle pipeline: scrape-run audit, append-only observations, absence-evidence gates, candidate monitored-cohort confirmations.

## 1.0.0 - 2026-07-06

### Added

- Initial public release (SQLite snapshot scraper).
