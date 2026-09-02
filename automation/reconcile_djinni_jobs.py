import argparse
import json
import math
import os
import random
import re
import sys
import time
from urllib.parse import urlencode, urljoin

import cloudscraper
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "https://djinni.co"
PARSER_VERSION = "job-status-v3"
PAGE_SIZE = 15
RECONCILE_LOCK_KEY = 84009520260729
SCAN_DEADLINE_BUFFER_SECONDS = 60
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}

class ScanDeadlineExceeded(RuntimeError):
    pass


TERMINAL_JOB_STATUSES = (
    "inactive_inferred",
    "offline_confirmed",
    "deleted_confirmed",
)
CONFIRMED_JOB_STATUSES = ("offline_confirmed", "deleted_confirmed")


def classify_complete_scan_miss(status, consecutive_complete_misses):
    """Next status after one additional complete-scan absence.

    A first miss stays `unknown` (queued for detail confirmation).
    A second consecutive miss becomes `inactive_inferred`.
    Terminal statuses are left alone.
    """
    next_misses = consecutive_complete_misses + 1
    if status in CONFIRMED_JOB_STATUSES:
        return status, None  # caller keeps existing status_source
    if status == "inactive_inferred":
        return status, None
    if next_misses >= 2:
        return "inactive_inferred", "two_complete_scan_absences"
    return "unknown", "first_complete_scan_absence"


def ensure_scan_time(deadline_monotonic):
    if (
        deadline_monotonic is not None
        and time.monotonic() >= deadline_monotonic - SCAN_DEADLINE_BUFFER_SECONDS
    ):
        raise ScanDeadlineExceeded(
            "Jobs one-click deadline reached; positive evidence was kept and the run was finalized safely."
        )


ACTIVE_OBSERVATION_SQL = text("""
    INSERT INTO public.djinni_job_observations (
        job_id, djinni_id, run_id, status, status_source,
        page, position_on_page, global_rank,
        views_count, apps_count, url, parse_ok
    ) VALUES (
        :job_id, :djinni_id, :run_id, 'active', 'listing',
        :page, :position_on_page, :global_rank,
        :views_count, :apps_count, :url, TRUE
    )
    ON CONFLICT (run_id, job_id) DO NOTHING
""")

ACTIVATE_JOB_SQL = text("""
    UPDATE public.djinni_jobs
    SET
        reopened_count = COALESCE(reopened_count, 0)
            + CASE
                WHEN status IN (
                    'inactive_inferred',
                    'offline_confirmed',
                    'deleted_confirmed'
                ) THEN 1
                ELSE 0
              END,
        status = 'active',
        status_source = 'listing',
        status_checked_at = CURRENT_TIMESTAMP,
        last_seen_at = CURRENT_TIMESTAMP,
        consecutive_complete_misses = 0,
        latest_views_count = CASE
            WHEN :views_count IS NOT NULL OR :apps_count IS NOT NULL
                THEN :views_count
            ELSE latest_views_count
        END,
        latest_apps_count = CASE
            WHEN :views_count IS NOT NULL OR :apps_count IS NOT NULL
                THEN :apps_count
            ELSE latest_apps_count
        END,
        engagement_observed_at = CASE
            WHEN :views_count IS NOT NULL OR :apps_count IS NOT NULL
                THEN CURRENT_TIMESTAMP
            ELSE engagement_observed_at
        END
    WHERE id = :job_id
""")


def load_engine():
    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    load_dotenv(env_path)
    load_dotenv()

    required = ["DB_USER", "DB_PASS", "DB_HOST", "DB_PORT", "DB_NAME"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing DB env vars: {', '.join(missing)}")

    return create_engine(
        f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASS')}@"
        f"{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )


def build_list_url(page):
    return f"{BASE_URL}/jobs/?{urlencode({'show_stale': 'on', 'page': page})}"


def clean_count(value):
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def parse_site_total(soup):
    heading = soup.find("h1", string=lambda value: value and "Jobs at Djinni" in value)
    if not heading or not heading.parent:
        return None

    container_text = " ".join(heading.parent.stripped_strings)
    match = re.search(r"Jobs at Djinni\s+([\d\s,]+)", container_text, re.IGNORECASE)
    return clean_count(match.group(1)) if match else None


def parse_last_page(soup):
    pages = []
    for link in soup.select('a[href*="page="]'):
        match = re.search(r"(?:\?|&)page=(\d+)", link.get("href") or "")
        if match:
            pages.append(int(match.group(1)))
    return max(pages) if pages else None


def parse_card(card, page, position_on_page):
    link = card.select_one("a.job_item__header-link[href]")
    if not link:
        return None

    href = link.get("href") or ""
    id_match = re.search(r"/jobs/(\d+)(?:-|/)", href)
    if not id_match:
        return None

    card_text = card.get_text(" ", strip=True)
    views_match = re.search(r"([\d\s,]+)\s+views\b", card_text, re.IGNORECASE)
    apps_match = re.search(r"([\d\s,]+)\s+applications\b", card_text, re.IGNORECASE)

    return {
        "djinni_id": int(id_match.group(1)),
        "url": urljoin(BASE_URL, href),
        "page": page,
        "position_on_page": position_on_page,
        "global_rank": (page - 1) * PAGE_SIZE + position_on_page,
        "views_count": clean_count(views_match.group(1)) if views_match else None,
        "apps_count": clean_count(apps_match.group(1)) if apps_match else None,
    }


def fetch_existing_jobs(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, djinni_id
            FROM public.djinni_jobs
            WHERE djinni_id IS NOT NULL
        """)).mappings()
        return {int(row["djinni_id"]): int(row["id"]) for row in rows}


def require_lifecycle_schema(engine):
    lifecycle_columns = {
        "status",
        "status_checked_at",
        "status_source",
        "first_seen_at",
        "last_seen_at",
        "inactive_detected_at",
        "reopened_count",
        "consecutive_complete_misses",
    }
    analysis_columns = {
        "valid_through",
        "employment_type",
        "work_format",
        "applicant_regions",
        "applicant_countries",
        "office_countries",
        "office_cities",
        "company_url",
        "salary_period",
        "salary_source",
        "direct_apply",
        "latest_views_count",
        "latest_apps_count",
        "engagement_observed_at",
        "detail_scraped_at",
        "last_parser_version",
    }
    operational_columns = {"detail_attempted_at"}
    semantic_columns = {
        "lifecycle_data_quality",
        "lifecycle_tracking_started_at",
    }

    with engine.connect() as conn:
        columns = {
            row[0]
            for row in conn.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'djinni_jobs'
            """))
        }
        tables = {
            row[0]
            for row in conn.execute(text("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('djinni_job_scrape_runs', 'djinni_job_observations')
            """))
        }

    missing_lifecycle = sorted(lifecycle_columns - columns)
    missing_analysis = sorted(analysis_columns - columns)
    missing_semantic = sorted(semantic_columns - columns)
    missing_operational = sorted(operational_columns - columns)
    missing_tables = sorted({"djinni_job_scrape_runs", "djinni_job_observations"} - tables)
    if missing_lifecycle or missing_tables:
        migration = os.path.join(
            os.path.dirname(__file__),
            "migrations",
            "001_job_lifecycle.sql",
        )
        missing = missing_lifecycle + missing_tables
        raise RuntimeError(
            f"Lifecycle schema is incomplete ({', '.join(missing)}). "
            f"For a fresh database run schema.sql in the repository root. "
            f"Historical journal step (existing DBs only): {migration}"
        )
    if missing_analysis:
        migration = os.path.join(
            os.path.dirname(__file__),
            "migrations",
            "002_job_analysis_fields.sql",
        )
        raise RuntimeError(
            f"Job analysis schema is incomplete ({', '.join(missing_analysis)}). "
            f"For a fresh database run schema.sql in the repository root. "
            f"Historical journal step (existing DBs only): {migration}"
        )
    if missing_operational:
        migration = os.path.join(
            os.path.dirname(__file__),
            "migrations",
            "012_job_detail_attempt_audit.sql",
        )
        raise RuntimeError(
            f"Job operational schema is incomplete ({', '.join(missing_operational)}). "
            f"For a fresh database run schema.sql in the repository root. "
            f"Historical journal step (existing DBs only): {migration}"
        )
    if missing_semantic:
        migration = os.path.join(
            os.path.dirname(__file__),
            "migrations",
            "004_job_analysis_semantics.sql",
        )
        raise RuntimeError(
            f"Job analysis semantics are incomplete ({', '.join(missing_semantic)}). "
            f"For a fresh database run schema.sql in the repository root. "
            f"Historical journal step (existing DBs only): {migration}"
        )


def acquire_reconcile_lock(engine):
    lock_conn = engine.connect()
    acquired = lock_conn.execute(
        text("SELECT pg_try_advisory_lock(:lock_key)"),
        {"lock_key": RECONCILE_LOCK_KEY},
    ).scalar_one()
    lock_conn.commit()

    if not acquired:
        lock_conn.close()
        raise RuntimeError(
            "Another write reconciliation is already running. "
            "Wait for it to finish; do not start concurrent status scans."
        )
    return lock_conn


def release_reconcile_lock(lock_conn):
    if lock_conn is None:
        return
    try:
        lock_conn.execute(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": RECONCILE_LOCK_KEY},
        )
        lock_conn.commit()
    finally:
        lock_conn.close()


def recover_abandoned_runs(engine):
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE public.djinni_job_scrape_runs
            SET
                finished_at = CURRENT_TIMESTAMP,
                status = 'failed',
                errors_count = errors_count + 1,
                error_summary = jsonb_build_object(
                    'message',
                    'Recovered abandoned run after acquiring the advisory lock'
                )
            WHERE status = 'running'
        """))


def create_run(engine):
    try:
        with engine.begin() as conn:
            row = conn.execute(text("""
                INSERT INTO public.djinni_job_scrape_runs (status, show_stale, parser_version)
                VALUES ('running', TRUE, :parser_version)
                RETURNING id, started_at
            """), {"parser_version": PARSER_VERSION}).mappings().one()
            return int(row["id"]), row["started_at"]
    except IntegrityError as exc:
        raise RuntimeError(
            "The database rejected a second running reconciliation."
        ) from exc


def update_run_progress(engine, run_id, stats):
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE public.djinni_job_scrape_runs
            SET
                site_reported_total = :site_reported_total,
                site_reported_total_end = :site_reported_total_end,
                expected_pages = :expected_pages,
                pages_scraped = :pages_scraped,
                cards_seen = :cards_seen,
                unique_jobs_seen = :unique_jobs_seen,
                existing_jobs_seen = :existing_jobs_seen,
                missing_in_db = :missing_in_db,
                duplicate_cards = :duplicate_cards,
                errors_count = :errors_count
            WHERE id = :run_id
        """), {**stats, "run_id": run_id})


def persist_active_page(engine, run_id, records, existing_jobs):
    known_records = []
    for record in records:
        job_id = existing_jobs.get(record["djinni_id"])
        if job_id is None:
            continue
        known_records.append({**record, "job_id": job_id, "run_id": run_id})

    if not known_records:
        return

    with engine.begin() as conn:
        conn.execute(ACTIVE_OBSERVATION_SQL, known_records)
        conn.execute(ACTIVATE_JOB_SQL, known_records)


def finalize_complete_run(engine, run_id, run_started_at, stats):
    with engine.begin() as conn:
        newly_inactive = conn.execute(text("""
            SELECT COUNT(*)
            FROM public.djinni_jobs AS job
            WHERE job.first_seen_at <= :run_started_at
              AND job.last_seen_at < :run_started_at
              AND job.status NOT IN (
                  'inactive_inferred',
                  'offline_confirmed',
                  'deleted_confirmed'
              )
              AND job.consecutive_complete_misses + 1 >= 2
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.djinni_job_observations AS observation
                  WHERE observation.run_id = :run_id
                    AND observation.job_id = job.id
              )
        """), {"run_id": run_id, "run_started_at": run_started_at}).scalar_one()

        missing_result = conn.execute(text("""
            UPDATE public.djinni_jobs AS job
            SET
                consecutive_complete_misses = job.consecutive_complete_misses + 1,
                -- Keep in sync with classify_complete_scan_miss().
                status = CASE
                    WHEN job.status IN (
                        'inactive_inferred',
                        'offline_confirmed',
                        'deleted_confirmed'
                    ) THEN job.status
                    WHEN job.consecutive_complete_misses + 1 >= 2
                        THEN 'inactive_inferred'
                    ELSE 'unknown'
                END,
                status_source = CASE
                    WHEN job.status IN ('offline_confirmed', 'deleted_confirmed')
                        THEN job.status_source
                    WHEN job.consecutive_complete_misses + 1 >= 2
                        THEN 'two_complete_scan_absences'
                    ELSE 'first_complete_scan_absence'
                END,
                status_checked_at = CURRENT_TIMESTAMP,
                inactive_detected_at = CASE
                    WHEN job.consecutive_complete_misses + 1 >= 2
                        THEN COALESCE(job.inactive_detected_at, CURRENT_TIMESTAMP)
                    ELSE job.inactive_detected_at
                END
            WHERE job.first_seen_at <= :run_started_at
              AND job.last_seen_at < :run_started_at
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.djinni_job_observations AS observation
                  WHERE observation.run_id = :run_id
                    AND observation.job_id = job.id
              )
        """), {"run_id": run_id, "run_started_at": run_started_at})

        conn.execute(text("""
            INSERT INTO public.djinni_job_observations (
                job_id, djinni_id, run_id,
                status, status_source,
                views_count, apps_count, url, parse_ok
            )
            SELECT
                job.id,
                job.djinni_id,
                :run_id,
                job.status,
                job.status_source,
                NULL,
                NULL,
                job.url,
                TRUE
            FROM public.djinni_jobs AS job
            WHERE job.first_seen_at <= :run_started_at
              AND job.last_seen_at < :run_started_at
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.djinni_job_observations AS observation
                  WHERE observation.run_id = :run_id
                    AND observation.job_id = job.id
              )
        """), {"run_id": run_id, "run_started_at": run_started_at})

        conn.execute(text("""
            UPDATE public.djinni_job_scrape_runs
            SET
                finished_at = CURRENT_TIMESTAMP,
                status = 'complete',
                site_reported_total = :site_reported_total,
                site_reported_total_end = :site_reported_total_end,
                expected_pages = :expected_pages,
                pages_scraped = :pages_scraped,
                cards_seen = :cards_seen,
                unique_jobs_seen = :unique_jobs_seen,
                existing_jobs_seen = :existing_jobs_seen,
                missing_in_db = :missing_in_db,
                duplicate_cards = :duplicate_cards,
                errors_count = :errors_count,
                error_summary = NULL
            WHERE id = :run_id
        """), {**stats, "run_id": run_id})

    return missing_result.rowcount, int(newly_inactive)


def finalize_incomplete_run(engine, run_id, stats, error_message=None):
    run_status = "partial" if stats["pages_scraped"] else "failed"
    error_summary = json.dumps({"message": error_message}, ensure_ascii=False) if error_message else None

    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE public.djinni_job_scrape_runs
            SET
                finished_at = CURRENT_TIMESTAMP,
                status = :status,
                site_reported_total = :site_reported_total,
                site_reported_total_end = :site_reported_total_end,
                expected_pages = :expected_pages,
                pages_scraped = :pages_scraped,
                cards_seen = :cards_seen,
                unique_jobs_seen = :unique_jobs_seen,
                existing_jobs_seen = :existing_jobs_seen,
                missing_in_db = :missing_in_db,
                duplicate_cards = :duplicate_cards,
                errors_count = :errors_count,
                error_summary = CAST(:error_summary AS jsonb)
            WHERE id = :run_id
              AND status = 'running'
        """), {
            **stats,
            "run_id": run_id,
            "status": run_status,
            "error_summary": error_summary,
        })


def current_stats(site_total, site_total_end, expected_pages, pages_scraped, cards_seen, seen_ids, existing_seen, missing_ids, duplicate_cards, errors_count):
    return {
        "site_reported_total": site_total,
        "site_reported_total_end": site_total_end,
        "expected_pages": expected_pages,
        "pages_scraped": pages_scraped,
        "cards_seen": cards_seen,
        "unique_jobs_seen": len(seen_ids),
        "existing_jobs_seen": len(existing_seen),
        "missing_in_db": len(missing_ids),
        "duplicate_cards": duplicate_cards,
        "errors_count": errors_count,
    }


def can_record_absence_evidence(
    start_page,
    max_pages,
    reached_end,
    expected_pages,
    pages_scraped,
    errors_count,
    site_total,
    site_total_end,
    cards_seen,
    unique_jobs_seen,
    duplicate_cards,
):
    return (
        start_page == 1
        and max_pages is None
        and reached_end
        and expected_pages is not None
        and pages_scraped >= expected_pages
        and errors_count == 0
        and site_total is not None
        and site_total_end is not None
        and cards_seen == site_total_end
        and unique_jobs_seen + duplicate_cards == cards_seen
        and duplicate_cards <= PAGE_SIZE
    )


def fetch_final_site_total(scraper):
    response = scraper.get(build_list_url(1), headers=HEADERS, timeout=20)
    response.raise_for_status()
    lowered = response.text.lower()
    if "has been blocked" in lowered or "cf-chl-" in lowered:
        raise RuntimeError("Block page detected during final total verification")

    total = parse_site_total(BeautifulSoup(response.content, "html.parser"))
    if total is None:
        raise RuntimeError("Could not parse final site total")
    return total


def reconcile(args, missing_job_handler=None, deadline_monotonic=None):
    engine = load_engine()
    lock_conn = None
    run_id = None
    run_started_at = None

    try:
        if args.dry_run:
            existing_jobs = fetch_existing_jobs(engine)
        else:
            require_lifecycle_schema(engine)
            lock_conn = acquire_reconcile_lock(engine)
            recover_abandoned_runs(engine)
            run_id, run_started_at = create_run(engine)
            existing_jobs = fetch_existing_jobs(engine)
            print(f"🧾 Started DB run {run_id}")

        return run_scan(
            args,
            engine,
            existing_jobs,
            run_id=run_id,
            run_started_at=run_started_at,
            missing_job_handler=missing_job_handler,
            deadline_monotonic=deadline_monotonic,
        )
    except (Exception, KeyboardInterrupt) as exc:
        error_message = str(exc) or type(exc).__name__
        if run_id is not None:
            empty_stats = current_stats(
                None,
                None,
                None,
                0,
                0,
                set(),
                set(),
                set(),
                0,
                1,
            )
            finalize_incomplete_run(engine, run_id, empty_stats, error_message)
        print(f"\n❌ Reconciliation setup failed: {error_message}")
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        release_reconcile_lock(lock_conn)


def run_scan(
    args,
    engine,
    existing_jobs,
    run_id=None,
    run_started_at=None,
    missing_job_handler=None,
    deadline_monotonic=None,
):
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )

    page = args.start_page
    final_requested_page = page + args.max_pages - 1 if args.max_pages else None
    site_total = None
    site_total_end = None
    expected_pages = None
    pages_scraped = 0
    cards_seen = 0
    duplicate_cards = 0
    errors_count = 0
    reached_end = False
    seen_ids = set()
    existing_seen = set()
    missing_ids = set()
    inserted_jobs = 0
    detail_errors = 0

    try:
        while True:
            ensure_scan_time(deadline_monotonic)
            if final_requested_page and page > final_requested_page:
                break

            list_url = build_list_url(page)
            print(f"\n🚀 Jobs status page {page}: {list_url}")
            response = scraper.get(list_url, headers=HEADERS, timeout=20)

            if response.status_code == 404:
                reached_end = True
                break

            response.raise_for_status()
            lowered = response.text.lower()
            if "has been blocked" in lowered or "cf-chl-" in lowered:
                raise RuntimeError("Djinni/Cloudflare block page detected; refusing to reconcile statuses")

            soup = BeautifulSoup(response.content, "html.parser")
            cards = soup.select(".job-item")
            if not cards:
                if expected_pages and page <= expected_pages:
                    raise RuntimeError(
                        f"No job cards on page {page}, but expected at least {expected_pages} pages"
                    )
                reached_end = True
                break

            page_total = parse_site_total(soup)
            if site_total is None and page_total is not None:
                site_total = page_total

            pagination_last_page = parse_last_page(soup)
            if pagination_last_page:
                expected_pages = max(expected_pages or 0, pagination_last_page)
            elif site_total:
                expected_pages = math.ceil(site_total / PAGE_SIZE)

            page_records = []
            for position, card in enumerate(cards, start=1):
                record = parse_card(card, page, position)
                if not record:
                    errors_count += 1
                    continue

                cards_seen += 1
                djinni_id = record["djinni_id"]
                if djinni_id in seen_ids:
                    duplicate_cards += 1
                seen_ids.add(djinni_id)

                job_id = existing_jobs.get(djinni_id)
                if job_id is None and missing_job_handler and not args.dry_run:
                    try:
                        ensure_scan_time(deadline_monotonic)
                        job_id = missing_job_handler(engine, scraper, record)
                    except Exception as exc:
                        detail_errors += 1
                        errors_count += 1
                        print(f"⚠️ Detail ingest failed for {record['url']}: {exc}")
                    if job_id is not None:
                        existing_jobs[djinni_id] = int(job_id)
                        inserted_jobs += 1

                if job_id is not None:
                    existing_seen.add(djinni_id)
                    missing_ids.discard(djinni_id)
                else:
                    missing_ids.add(djinni_id)
                page_records.append(record)

            if not args.dry_run:
                persist_active_page(engine, run_id, page_records, existing_jobs)

            pages_scraped += 1
            stats = current_stats(
                site_total,
                site_total_end,
                expected_pages,
                pages_scraped,
                cards_seen,
                seen_ids,
                existing_seen,
                missing_ids,
                duplicate_cards,
                errors_count,
            )
            if not args.dry_run:
                update_run_progress(engine, run_id, stats)

            print(
                f"📄 page={page} cards={len(page_records)}/{len(cards)} "
                f"unique={len(seen_ids)} existing={len(existing_seen)} "
                f"missing_in_db={len(missing_ids)} duplicates={duplicate_cards}"
            )

            if expected_pages and page >= expected_pages:
                reached_end = True
                break

            page += 1
            ensure_scan_time(deadline_monotonic)
            time.sleep(random.uniform(args.page_sleep_min, args.page_sleep_max))

        if args.start_page == 1 and args.max_pages is None and reached_end:
            ensure_scan_time(deadline_monotonic)
            site_total_end = fetch_final_site_total(scraper)

        stats = current_stats(
            site_total,
            site_total_end,
            expected_pages,
            pages_scraped,
            cards_seen,
            seen_ids,
            existing_seen,
            missing_ids,
            duplicate_cards,
            errors_count,
        )

        can_record_absences = can_record_absence_evidence(
            args.start_page,
            args.max_pages,
            reached_end,
            expected_pages,
            pages_scraped,
            errors_count,
            site_total,
            site_total_end,
            cards_seen,
            len(seen_ids),
            duplicate_cards,
        )

        would_record_missing = len(existing_jobs) - len(existing_seen)
        print("\nSummary")
        print(f"  site_reported_total_start={site_total}")
        print(f"  site_reported_total_end={site_total_end}")
        print(f"  expected_pages={expected_pages}")
        print(f"  pages_scraped={pages_scraped}")
        print(f"  cards_seen={cards_seen}")
        print(f"  unique_jobs_seen={len(seen_ids)}")
        print(f"  existing_jobs_seen={len(existing_seen)}")
        print(f"  missing_in_db={len(missing_ids)}")
        print(f"  duplicate_cards={duplicate_cards}")
        print(f"  parse_errors={errors_count}")
        print(f"  inserted_jobs={inserted_jobs}")
        print(f"  detail_errors={detail_errors}")
        print(f"  would_record_missing={would_record_missing}")
        print(f"  safe_to_record_absence_evidence={can_record_absences}")

        if missing_ids:
            preview = ", ".join(str(value) for value in sorted(missing_ids)[:20])
            print(f"  missing_id_preview={preview}")

        if args.dry_run:
            print("\nDRY RUN: PostgreSQL was not modified.")
            return 0

        if can_record_absences:
            missing_count, newly_inactive = finalize_complete_run(
                engine,
                run_id,
                run_started_at,
                stats,
            )
            print(
                f"\n✅ Run {run_id} complete. "
                f"Missing evidence recorded={missing_count}; "
                f"newly inactive after second complete miss={newly_inactive}"
            )
            return 0

        reason = (
            "Full-scan safety checks did not pass; active observations were kept, "
            "but no missing jobs were marked inactive."
        )
        finalize_incomplete_run(engine, run_id, stats, reason)
        print(f"\n⚠️ Run {run_id} partial. {reason}")
        return 2

    except (Exception, KeyboardInterrupt) as exc:
        errors_count += 1
        error_message = str(exc) or type(exc).__name__
        stats = current_stats(
            site_total,
            site_total_end,
            expected_pages,
            pages_scraped,
            cards_seen,
            seen_ids,
            existing_seen,
            missing_ids,
            duplicate_cards,
            errors_count,
        )
        if run_id is not None:
            finalize_incomplete_run(engine, run_id, stats, error_message)
        print(f"\n❌ Reconciliation failed: {error_message}")
        if isinstance(exc, KeyboardInterrupt):
            return 130
        if isinstance(exc, ScanDeadlineExceeded):
            return 2
        return 1


def parse_args():
    parser = argparse.ArgumentParser(
        description="Reconcile current Djinni job statuses without fetching every detail page."
    )
    parser.add_argument("--dry-run", action="store_true", help="Scan and report without DB writes.")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--page-sleep-min", type=float, default=5.0)
    parser.add_argument("--page-sleep-max", type=float, default=8.0)
    args = parser.parse_args()

    if args.start_page < 1:
        parser.error("--start-page must be >= 1")
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be >= 1")
    if args.page_sleep_min < 0 or args.page_sleep_max < args.page_sleep_min:
        parser.error("invalid page sleep range")
    return args


if __name__ == "__main__":
    raise SystemExit(reconcile(parse_args()))
