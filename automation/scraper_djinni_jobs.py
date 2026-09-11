import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from sqlalchemy import text

import reconcile_djinni_jobs as lifecycle
from djinni_http import create_djinni_scraper, raise_if_blocked
from telegram_alert import send_heartbeat

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PARSER_VERSION = "jobs-v2-lifecycle-v2"
JOB_ONE_CLICK_BUDGET_SECONDS = 165 * 60
JOB_CONFIRMATION_BUFFER_SECONDS = 60
JOB_RETRY_MAX_FIRST_SECONDS = 75 * 60
IGAMING_KEYWORDS = (
    "igaming",
    "gambling",
    "casino",
    "betting",
    "sportsbook",
    "slots",
    "poker",
)
COUNTRY_CODE_ALIASES = {"UA": "UKR"}
MARKETING_KEYWORDS = (
    "performance marketing",
    "media buying",
    "affiliate marketing",
    "user acquisition",
    "digital marketing",
    "lead generation",
)


def normalize(value):
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized or None


def nested(value, *keys):
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def collect_path_values(value, *keys):
    nodes = [value]
    for key in keys:
        next_nodes = []
        for node in nodes:
            items = node if isinstance(node, list) else [node]
            for item in items:
                if isinstance(item, dict) and key in item:
                    next_nodes.append(item[key])
        nodes = next_nodes

    values = []
    pending = list(nodes)
    while pending:
        item = pending.pop(0)
        if isinstance(item, list):
            pending[0:0] = item
            continue
        normalized = normalize(item)
        if normalized and normalized not in values:
            values.append(normalized)
    return values or None


def normalize_country_codes(values):
    if not values:
        return None
    normalized = []
    for value in values:
        code = COUNTRY_CODE_ALIASES.get(value.upper(), value.upper())
        if code and code not in normalized:
            normalized.append(code)
    return normalized or None


def find_job_posting(value):
    if isinstance(value, dict):
        if value.get("@type") == "JobPosting":
            return value
        graph = value.get("@graph")
        if graph is not None:
            found = find_job_posting(graph)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_job_posting(item)
            if found:
                return found
    return None


def extract_json_ld(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        posting = find_job_posting(value)
        if posting:
            return posting
    return None


def parse_salary(data):
    estimated = data.get("estimatedSalary")
    estimated_currency = estimated.get("currency") if isinstance(estimated, dict) else None

    base_salary = data.get("baseSalary")
    if isinstance(base_salary, dict):
        value = base_salary.get("value")
        if isinstance(value, dict):
            salary_min = value.get("minValue")
            salary_max = value.get("maxValue")
            if salary_min is not None or salary_max is not None:
                period = normalize(value.get("unitText"))
                return {
                    "salary_min": int(salary_min) if salary_min is not None else None,
                    "salary_max": int(salary_max) if salary_max is not None else None,
                    "salary_currency": base_salary.get("currency") or estimated_currency,
                    "salary_period": period.lower() if period else None,
                    "salary_source": "baseSalary",
                }

    if isinstance(estimated, dict):
        value = estimated.get("value") if isinstance(estimated.get("value"), dict) else estimated
        salary_min = value.get("minValue")
        salary_max = value.get("maxValue")
        if salary_min is not None or salary_max is not None:
            period = normalize(value.get("unitText"))
            return {
                "salary_min": int(salary_min) if salary_min is not None else None,
                "salary_max": int(salary_max) if salary_max is not None else None,
                "salary_currency": estimated_currency,
                "salary_period": period.lower() if period else None,
                "salary_source": "estimatedSalary",
            }

    return {
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "salary_period": None,
        "salary_source": None,
    }


def parse_timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=None)


def normalize_work_format(data):
    if data.get("jobLocationType") == "TELECOMMUTE":
        return "remote"
    if data.get("jobLocation"):
        return "office"
    return None


def parse_english_level(text_value):
    match = re.search(r"\bEnglish\s*(?:level)?\s*[-:]?\s*([ABC][12])\b", text_value, re.I)
    return match.group(1).upper() if match else None


def parse_response_activity(soup, text_value):
    for node in soup.select("[data-bs-original-title], [aria-label]"):
        tooltip = node.get("data-bs-original-title") or node.get("aria-label")
        if tooltip and re.search(r"respond", tooltip, re.I):
            return normalize(tooltip)
    if re.search(r"DOESN['’]?T RESPOND", text_value, re.I):
        return "does_not_respond"
    if re.search(r"RESPONDS QUICKLY", text_value, re.I):
        return "responds_quickly"
    return None


def collect_tags(soup, text_value):
    values = []
    for selector in (".job-post__tags .job-post__tag", ".job-item__tags > *"):
        for node in soup.select(selector):
            value = normalize(node.get_text(" ", strip=True))
            if value and value not in values:
                values.append(value)

    known_labels = (
        "Ukrainian Product",
        "Product",
        "DefTech",
        "Mobilisation reservation",
    )
    for label in known_labels:
        if label.lower() in text_value.lower() and label not in values:
            values.append(label)
    return values


def parse_detail_response(response, url):
    response.raise_for_status()
    lowered = response.text.lower()
    raise_if_blocked(lowered)

    soup = BeautifulSoup(response.content, "html.parser")
    text_value = soup.get_text(" ", strip=True)
    data = extract_json_ld(soup)
    if not data:
        if "no longer active" in text_value.lower() or re.search(r"\bOffline\b", text_value):
            raise RuntimeError("Vacancy became offline before detail ingest")
        raise RuntimeError("JobPosting JSON-LD not found")

    salary = parse_salary(data)
    description = normalize(data.get("description"))
    if not description:
        description_node = soup.select_one(".job-post__description")
        description = normalize(description_node.get_text(" ", strip=True)) if description_node else None

    domain = normalize(data.get("industry"))
    searchable = " ".join(
        value for value in (data.get("title"), domain, description) if value
    ).lower()
    category = normalize(data.get("category"))

    direct_apply = data.get("directApply")
    if not isinstance(direct_apply, bool):
        direct_apply = None

    return {
        "title": normalize(data.get("title")),
        "company": normalize(nested(data, "hiringOrganization", "name")) or "Unknown",
        "category": category,
        "description": description or "",
        "published_at": parse_timestamp(data.get("datePosted")) or datetime.now(timezone.utc),
        "valid_through": parse_timestamp(data.get("validThrough")),
        "experience_months": int(nested(data, "experienceRequirements", "monthsOfExperience") or 0),
        "employment_type": normalize(data.get("employmentType")),
        "work_format": normalize_work_format(data),
        "domain": domain,
        "applicant_regions": collect_path_values(
            data.get("applicantLocationRequirements"),
            "address",
            "addressRegion",
        ),
        "applicant_countries": normalize_country_codes(collect_path_values(
            data.get("applicantLocationRequirements"),
            "address",
            "addressCountry",
        )),
        "office_countries": normalize_country_codes(collect_path_values(
            data.get("jobLocation"),
            "address",
            "addressCountry",
        )),
        "office_cities": collect_path_values(
            data.get("jobLocation"),
            "address",
            "addressLocality",
        ),
        "company_url": (
            collect_path_values(data.get("hiringOrganization"), "sameAs") or [None]
        )[0],
        "direct_apply": direct_apply,
        "english_level": parse_english_level(text_value),
        "response_activity": parse_response_activity(soup, text_value),
        "tags": collect_tags(soup, text_value),
        "is_igaming": (
            domain is not None and domain.lower() == "gambling"
        ) or any(keyword in searchable for keyword in IGAMING_KEYWORDS),
        "is_marketing": any(keyword in searchable for keyword in MARKETING_KEYWORDS),
        "raw_json_ld": data,
        "url": url,
        **salary,
    }


def insert_new_job(engine, detail, listing_record):
    params = {
        **detail,
        "djinni_id": listing_record["djinni_id"],
        "views_count": listing_record.get("views_count"),
        "apps_count": listing_record.get("apps_count"),
        "raw_json_ld": json.dumps(detail["raw_json_ld"], ensure_ascii=False),
        "parser_version": PARSER_VERSION,
    }

    with engine.begin() as conn:
        return conn.execute(text("""
            INSERT INTO public.djinni_jobs (
                djinni_id, title, company, category,
                salary_min, salary_max, salary_currency, salary_period, salary_source,
                exp_selector_months, description, url, published_at, valid_through,
                views_count, apps_count, latest_views_count, latest_apps_count,
                engagement_observed_at, response_activity, tags,
                domain, english_level, employment_type, work_format,
                applicant_regions, applicant_countries, office_countries, office_cities,
                company_url, direct_apply,
                is_igaming, is_marketing, raw_json_ld,
                status, status_source, status_checked_at,
                first_seen_at, last_seen_at, detail_scraped_at,
                consecutive_complete_misses, last_parser_version,
                lifecycle_data_quality, lifecycle_tracking_started_at
            ) VALUES (
                :djinni_id, :title, :company, :category,
                :salary_min, :salary_max, :salary_currency, :salary_period, :salary_source,
                :experience_months, :description, :url, :published_at, :valid_through,
                :views_count, :apps_count, :views_count, :apps_count,
                CASE
                    WHEN :views_count IS NOT NULL OR :apps_count IS NOT NULL
                        THEN CURRENT_TIMESTAMP
                    ELSE NULL
                END,
                :response_activity, :tags,
                :domain, :english_level, :employment_type, :work_format,
                :applicant_regions, :applicant_countries, :office_countries, :office_cities,
                :company_url, :direct_apply,
                :is_igaming, :is_marketing, CAST(:raw_json_ld AS jsonb),
                'active', 'detail_new', CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                0, :parser_version,
                'prospective', CURRENT_TIMESTAMP
            )
            ON CONFLICT (djinni_id) DO UPDATE SET
                status = 'active',
                status_source = 'detail_conflict',
                status_checked_at = CURRENT_TIMESTAMP,
                last_seen_at = CURRENT_TIMESTAMP,
                consecutive_complete_misses = 0,
                latest_views_count = CASE
                    WHEN EXCLUDED.latest_views_count IS NOT NULL
                      OR EXCLUDED.latest_apps_count IS NOT NULL
                        THEN EXCLUDED.latest_views_count
                    ELSE public.djinni_jobs.latest_views_count
                END,
                latest_apps_count = CASE
                    WHEN EXCLUDED.latest_views_count IS NOT NULL
                      OR EXCLUDED.latest_apps_count IS NOT NULL
                        THEN EXCLUDED.latest_apps_count
                    ELSE public.djinni_jobs.latest_apps_count
                END,
                engagement_observed_at = CASE
                    WHEN EXCLUDED.latest_views_count IS NOT NULL
                      OR EXCLUDED.latest_apps_count IS NOT NULL
                        THEN CURRENT_TIMESTAMP
                    ELSE public.djinni_jobs.engagement_observed_at
                END
            RETURNING id
        """), params).scalar_one()



def select_active_jobs_for_detail_refresh(engine, limit):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, djinni_id, url
            FROM public.djinni_jobs
            WHERE status = 'active' AND url IS NOT NULL
            ORDER BY detail_scraped_at NULLS FIRST, id
            LIMIT :limit
        """), {"limit": limit}).mappings().all()
        return [dict(row) for row in rows]


def update_job_detail_fields(engine, djinni_id, detail):
    params = {
        **detail,
        "djinni_id": djinni_id,
        "raw_json_ld": json.dumps(detail["raw_json_ld"], ensure_ascii=False),
        "parser_version": PARSER_VERSION,
    }
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE public.djinni_jobs
            SET
                salary_min = :salary_min,
                salary_max = :salary_max,
                salary_currency = :salary_currency,
                salary_period = :salary_period,
                salary_source = :salary_source,
                valid_through = :valid_through,
                tags = :tags,
                exp_selector_months = :experience_months,
                raw_json_ld = CAST(:raw_json_ld AS jsonb),
                detail_scraped_at = CURRENT_TIMESTAMP,
                last_parser_version = :parser_version
            WHERE djinni_id = :djinni_id
              AND status = 'active'
        """), params)


def refresh_active_job_details(args, deadline_monotonic=None):
    """Budgeted re-fetch of JSON-LD fields that otherwise freeze after first ingest."""
    if args.detail_refresh_limit <= 0 or args.dry_run:
        return 0
    engine = lifecycle.load_engine()
    scraper = create_djinni_scraper()
    targets = select_active_jobs_for_detail_refresh(engine, args.detail_refresh_limit)
    if not targets:
        print("Detail refresh: no active jobs queued")
        return 0
    refreshed = 0
    errors = 0
    print(f"Detail refresh: up to {len(targets)} active job(s)")
    for row in targets:
        if (
            deadline_monotonic is not None
            and time.monotonic() >= deadline_monotonic - lifecycle.SCAN_DEADLINE_BUFFER_SECONDS
        ):
            print("Detail refresh stopped: deadline buffer reached")
            break
        time.sleep(random.uniform(args.detail_sleep_min, args.detail_sleep_max))
        try:
            response = scraper.get(row["url"], headers=lifecycle.HEADERS, timeout=15)
            detail = parse_detail_response(response, row["url"])
            update_job_detail_fields(engine, row["djinni_id"], detail)
            refreshed += 1
            print(f"♻️ refreshed [{row['djinni_id']}] salary/tags/validThrough")
        except Exception as exc:
            errors += 1
            print(f"⚠️ detail refresh failed [{row['djinni_id']}]: {exc}")
    print(f"Detail refresh done: refreshed={refreshed} errors={errors}")
    return 1 if errors and refreshed == 0 else 0


def build_missing_job_handler(args):
    def ingest(engine, scraper, listing_record):
        last_error = None
        for attempt in range(1, 3):
            time.sleep(random.uniform(args.detail_sleep_min, args.detail_sleep_max))
            try:
                response = scraper.get(
                    listing_record["url"],
                    headers=lifecycle.HEADERS,
                    timeout=15,
                )
                detail = parse_detail_response(response, listing_record["url"])
                job_id = insert_new_job(engine, detail, listing_record)
                print(
                    f"✅ NEW [{listing_record['djinni_id']}] {detail['title']} | "
                    f"👁 {listing_record.get('views_count')} | "
                    f"📩 {listing_record.get('apps_count')}"
                )
                return job_id
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    print(
                        f"⚠️ Retrying detail [{listing_record['djinni_id']}] "
                        f"after: {exc}"
                    )
        raise last_error

    return ingest


def classify_status_response(response):
    if response.status_code in (404, 410):
        return "deleted_confirmed", "detail_404"

    response.raise_for_status()
    lowered = response.text.lower()
    raise_if_blocked(lowered, "Djinni/Cloudflare block page detected during status confirmation")

    soup = BeautifulSoup(response.content, "html.parser")
    text_value = soup.get_text(" ", strip=True)
    if extract_json_ld(soup):
        return "active", "detail_confirmation"
    if "no longer active" in text_value.lower() or re.search(r"\bOffline\b", text_value):
        return "offline_confirmed", "detail_offline"
    raise RuntimeError("Ambiguous detail status")


def job_confirmation_budget_available(deadline_monotonic, now_monotonic=None):
    if deadline_monotonic is None:
        return True
    now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    return now_monotonic < deadline_monotonic - JOB_CONFIRMATION_BUFFER_SECONDS


def confirm_unknown_jobs(args, deadline_monotonic=None):
    engine = lifecycle.load_engine()
    lock_conn = None
    try:
        lock_conn = lifecycle.acquire_reconcile_lock(engine)
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    job.id,
                    job.djinni_id,
                    job.url,
                    (
                        SELECT observation.id
                        FROM public.djinni_job_observations AS observation
                        WHERE observation.job_id = job.id
                        ORDER BY observation.observed_at DESC, observation.id DESC
                        LIMIT 1
                    ) AS observation_id
                FROM public.djinni_jobs AS job
                WHERE job.status = 'unknown'
                  AND job.status_source = 'first_complete_scan_absence'
                ORDER BY job.detail_attempted_at NULLS FIRST, job.id
            """)).mappings().all()

        if not rows:
            return 0

        scraper = create_djinni_scraper()
        errors = 0
        for position, row in enumerate(rows):
            if not job_confirmation_budget_available(deadline_monotonic):
                remaining = len(rows) - position
                print(
                    f"⏸️ Jobs detail-confirmation budget exhausted; "
                    f"leaving {remaining} safe first-miss confirmations for a later run."
                )
                return 1 if errors else 0

            confirmed = None
            last_error = None
            for attempt in range(1, 3):
                time.sleep(random.uniform(args.detail_sleep_min, args.detail_sleep_max))
                try:
                    with engine.begin() as conn:
                        conn.execute(text("""
                            UPDATE public.djinni_jobs
                            SET detail_attempted_at = CURRENT_TIMESTAMP
                            WHERE id = :job_id
                        """), {"job_id": row["id"]})
                    response = scraper.get(row["url"], headers=lifecycle.HEADERS, timeout=15)
                    confirmed = classify_status_response(response)
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < 2:
                        print(
                            f"⚠️ Retrying status confirmation [{row['djinni_id']}] "
                            f"after: {exc}"
                        )

            if confirmed is None:
                errors += 1
                print(f"❌ Status confirmation failed [{row['djinni_id']}]: {last_error}")
                continue

            status, source = confirmed
            with engine.begin() as conn:
                conn.execute(text("""
                    UPDATE public.djinni_jobs
                    SET
                        status = :status,
                        status_source = :source,
                        status_checked_at = CURRENT_TIMESTAMP,
                        last_seen_at = CASE
                            WHEN :status = 'active' THEN CURRENT_TIMESTAMP
                            ELSE last_seen_at
                        END,
                        inactive_detected_at = CASE
                            WHEN :status IN ('offline_confirmed', 'deleted_confirmed')
                                THEN COALESCE(inactive_detected_at, CURRENT_TIMESTAMP)
                            ELSE inactive_detected_at
                        END,
                        consecutive_complete_misses = CASE
                            WHEN :status = 'active' THEN 0
                            ELSE consecutive_complete_misses
                        END,
                        detail_scraped_at = CURRENT_TIMESTAMP,
                        last_parser_version = :parser_version
                    WHERE id = :job_id
                """), {
                    "job_id": row["id"],
                    "status": status,
                    "source": source,
                    "parser_version": PARSER_VERSION,
                })
                if row["observation_id"] is not None:
                    conn.execute(text("""
                        UPDATE public.djinni_job_observations
                        SET
                            status = :status,
                            status_source = :source,
                            observed_at = CURRENT_TIMESTAMP
                        WHERE id = :observation_id
                    """), {
                        "observation_id": row["observation_id"],
                        "status": status,
                        "source": source,
                    })
            print(f"🔎 [{row['djinni_id']}] confirmed status={status}")

        return 1 if errors else 0
    finally:
        lifecycle.release_reconcile_lock(lock_conn)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape all public Djinni jobs with lifecycle observations."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--detail-sleep-min", type=float, default=2.0)
    parser.add_argument("--detail-sleep-max", type=float, default=4.0)
    parser.add_argument("--page-sleep-min", type=float, default=5.0)
    parser.add_argument("--page-sleep-max", type=float, default=8.0)
    parser.add_argument(
        "--no-structural-retry", action="store_true",
        help="Do not retry one full scan when the first scan is structurally partial.",
    )
    parser.add_argument(
        "--detail-refresh-limit", type=int, default=0,
        help="Budgeted re-fetch of salary/tags/validThrough for active jobs (0=off).",
    )
    args = parser.parse_args()

    if args.start_page < 1:
        parser.error("--start-page must be >= 1")
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be >= 1")
    if args.detail_sleep_min < 0 or args.detail_sleep_max < args.detail_sleep_min:
        parser.error("invalid detail sleep range")
    if args.page_sleep_min < 0 or args.page_sleep_max < args.page_sleep_min:
        parser.error("invalid page sleep range")
    if args.detail_refresh_limit < 0:
        parser.error("--detail-refresh-limit must be non-negative")
    return args


def should_retry_structural_partial(result, args, elapsed_seconds):
    return (
        result == 2
        and not args.dry_run
        and not args.no_structural_retry
        and args.start_page == 1
        and args.max_pages is None
        and elapsed_seconds <= JOB_RETRY_MAX_FIRST_SECONDS
    )


def main():
    args = parse_args()
    started = time.monotonic()
    deadline = started + JOB_ONE_CLICK_BUDGET_SECONDS
    result = lifecycle.reconcile(
        args,
        missing_job_handler=build_missing_job_handler(args),
        deadline_monotonic=deadline,
    )
    elapsed = time.monotonic() - started
    if should_retry_structural_partial(result, args, elapsed):
        print(
            "\n🔁 First full jobs scan was structurally partial; "
            "starting one automatic stabilization retry."
        )
        result = lifecycle.reconcile(
            args,
            missing_job_handler=build_missing_job_handler(args),
            deadline_monotonic=deadline,
        )
    confirmation_status = None
    if not args.dry_run and result in (0, 2):
        confirmation_result = confirm_unknown_jobs(args, deadline)
        if result == 0:
            result = confirmation_result
    if not args.dry_run and result in (0, 2) and args.detail_refresh_limit > 0:
        refresh_result = refresh_active_job_details(args, deadline)
        if result == 0:
            result = refresh_result

    # Telegram heartbeat: Djinni runs used to report nothing at all.
    # result 0 = clean run, anything else means a partial/failed cycle.
    hb_status = "success" if result == 0 else "partial"
    send_heartbeat("DJINNI", hb_status, f"exit={result}, duration {elapsed/60:.0f} min")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
