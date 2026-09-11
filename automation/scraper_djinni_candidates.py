import argparse
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from djinni_http import (
    HEADERS,
    BlockedResponseError,
    create_djinni_scraper,
    raise_if_blocked,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "https://djinni.co"

PARSER_VERSION = "candidate-lifecycle-v3"
CANDIDATE_LOCK_KEY = 774_202_607
ACTIVE_CONFIRMATION_DAYS = 14
INACTIVE_CONFIRMATION_DAYS = 30
DETAIL_BUDGET_SECONDS = 14
DETAIL_DEADLINE_BUFFER_SECONDS = 60
OFFLINE_MARKERS = (
    "candidate is offline",
    "profile is offline",
    "candidate no longer active",
    "candidate is no longer active",
    "profile is no longer active",
)

IGAMING_KEYWORDS = ("igaming", "gambling", "casino", "betting", "sportsbook", "slots", "poker")
AI_KEYWORDS = (
    "claude", "cursor", "copilot", "chatgpt", "openai", "gemini", "llm",
    "langchain", "ai agent", "ai agents", "agentic", "v0", "bolt",
)


def load_engine():
    # Always load the repo-root .env, even when launched from another directory.
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


def normalize(value):
    if value is None:
        return None
    text_value = " ".join(str(value).split())
    return text_value or None


def clean_count(value):
    if value is None or normalize(value) is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def parse_duration_to_months(value):
    value = (value or "").lower().replace(",", ".")
    months = 0.0

    year_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:\+\s*)?(?:year|years|yr|yrs)", value)
    month_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:month|months|mo)", value)

    if year_match:
        months += float(year_match.group(1)) * 12
    if month_match:
        months += float(month_match.group(1))

    return int(round(months)) if months else None


def parse_salary(value):
    value = normalize(value) or ""
    match = re.search(r"([$€₴])\s*([\d\s,]+)", value)
    if not match:
        return None, None, None

    currency = {"$": "USD", "€": "EUR", "₴": "UAH"}.get(match.group(1), match.group(1))
    salary = int(re.sub(r"\D", "", match.group(2)))

    lower = value.lower()
    period = "year" if "year" in lower or "/ yr" in lower else "mo"
    return salary, currency, period


def parse_published_date(value, today=None):
    value = (normalize(value) or "").replace("Published", "").strip()
    lowered = value.lower()
    today = today or date.today()
    if lowered == "today":
        return today
    if lowered == "yesterday":
        return today - timedelta(days=1)
    for fmt in ("%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def split_location(value):
    value = normalize(value)
    if not value:
        return None, None
    parts = [part.strip() for part in value.split(",", 1)]
    country = parts[0] if parts else None
    city = parts[1] if len(parts) > 1 else None
    return country, city


def build_list_url(page, sortby):
    params = {}
    if page > 1:
        params["page"] = page
    if sortby and sortby != "date":
        params["sortby"] = sortby
    suffix = f"?{urlencode(params)}" if params else ""
    return f"{BASE_URL}/developers/{suffix}"


def extract_csc_item(node):
    primary = node.select_one(".csc__primary")
    secondary = node.select_one(".csc__secondary")
    name = normalize(primary.get_text(" ", strip=True)) if primary else None
    experience = normalize(secondary.get_text(" ", strip=True)) if secondary else None
    if not name:
        return None
    item = {"name": name}
    if experience:
        item["experience"] = experience
        months = parse_duration_to_months(experience)
        if months is not None:
            item["months"] = months
    return item


def find_heading(soup, heading_text):
    for h2 in soup.find_all("h2"):
        if normalize(h2.get_text(" ", strip=True)) == heading_text:
            return h2
    return None


def csc_items_under_heading(soup, heading_text, required_class=None):
    heading = find_heading(soup, heading_text)
    if not heading:
        return []

    items = []
    for node in heading.next_elements:
        if node is heading:
            continue
        if isinstance(node, Tag) and node.name == "h2":
            break
        if not isinstance(node, Tag):
            continue
        classes = node.get("class") or []
        if "csc" not in classes:
            continue
        if required_class and required_class not in classes:
            continue
        item = extract_csc_item(node)
        if item:
            items.append(item)
    return items


def extract_sections(soup):
    main = soup.select_one(".col-sm-8")
    if not main:
        return {}

    sections = {}
    for heading in main.find_all("h2"):
        label = normalize(heading.get_text(" ", strip=True))
        if not label:
            continue

        parts = []
        for sibling in heading.next_siblings:
            if isinstance(sibling, Tag) and sibling.name == "h2":
                break
            if isinstance(sibling, Tag):
                text_value = normalize(sibling.get_text(" ", strip=True))
                if text_value:
                    parts.append(text_value)
        sections[label] = " ".join(parts) or None

    return sections


def parse_listing_card(card):
    profile_link = card.select_one("a.profile[href]")
    if not profile_link:
        return None

    href = profile_link.get("href")
    key_match = re.search(r"/q/([^/]+)/", href or "")
    if not key_match:
        return None

    salary_text = normalize(card.select_one(".fs-2.text-success").get_text(" ", strip=True)) if card.select_one(".fs-2.text-success") else None
    salary, currency, period = parse_salary(salary_text)

    views_node = card.select_one('.card-footer span[title*="view"]')
    views_count = clean_count(
        views_node.get("title") or views_node.get_text(" ", strip=True)
    ) if views_node else None

    rows = []
    icon_values = {}
    # Djinni changes decorative spacing classes (for example column-gap-2 to
    # column-gap-075). Metadata extraction must depend on structure/icons only.
    for row in card.select(".d-flex.align-items-center.flex-wrap"):
        row_values = []
        for item in row.find_all("span", recursive=False):
            value = normalize(item.get_text(" ", strip=True))
            use = item.select_one("use[href]")
            icon = (use.get("href") or "").split("#")[-1] if use else None
            if value:
                row_values.append(value)
            if icon and value:
                icon_values[icon] = value
        if row_values:
            rows.append(row_values)

    category = icon_values.get("code-square")
    experience_text = icon_values.get("lightning-charge")
    experience_months = parse_duration_to_months(experience_text)
    english_level = icon_values.get("translate")
    published_label = icon_values.get("clock")
    location_text = icon_values.get("geo-alt")
    work_format = icon_values.get("laptop")

    country, city = split_location(location_text)
    skill_tags = []
    for tag in card.select("span.bg-light.rounded"):
        value = normalize(tag.get_text(" ", strip=True))
        if value and not value.startswith("+"):
            skill_tags.append(value)

    title = normalize(profile_link.get_text(" ", strip=True))
    url = urljoin(BASE_URL, href)
    is_confirmed = bool(card.select_one('use[href*="#patch-check-fill"]'))

    raw_list = {
        "title": title,
        "salary_text": salary_text,
        "rows": rows,
        "published_label": published_label,
        "visible_skills": skill_tags,
        "views_count": views_count,
        "is_confirmed": is_confirmed,
    }

    return {
        "djinni_key": key_match.group(1),
        "url": url,
        "title": title,
        "category": category,
        "salary_expectation": salary,
        "salary_currency": currency,
        "salary_period": period,
        "experience_months": experience_months,
        "experience_years": round(experience_months / 12, 1) if experience_months is not None else None,
        "english_level": english_level,
        "location_text": location_text,
        "country": country,
        "city": city,
        "work_format": work_format,
        "published_at": parse_published_date(published_label),
        "views_count": views_count,
        "is_confirmed": is_confirmed,
        "raw_list": raw_list,
    }


def listing_parser_drift_error(records):
    """Return an error when a full listing page loses mandatory metadata at once."""
    if len(records) < 5:
        return None
    published = sum(record.get("published_at") is not None for record in records)
    categories = sum(bool(record.get("category")) for record in records)
    if published == 0:
        return "published_at missing from every parsed card on the first listing page"
    if categories == 0:
        return "category missing from every parsed card on the first listing page"
    return None


def parse_profile_detail(html, url):
    soup = BeautifulSoup(html, "html.parser")
    title = normalize(soup.select_one("h1").get_text(" ", strip=True)) if soup.select_one("h1") else None

    breadcrumbs = []
    country = None
    category = None
    specialization = None
    title_breadcrumbs = []
    for crumb in soup.select(".breadcrumb-item a[href]"):
        label = normalize(crumb.get_text(" ", strip=True))
        href = crumb.get("href") or ""
        if not label or label == "Candidates":
            continue
        breadcrumbs.append({"label": label, "href": href})

        query = parse_qs(urlparse(href).query)
        # Category links often also contain region=..., so title must win over region.
        if "title" in query:
            title_breadcrumbs.append(label)
        elif "region" in query:
            country = label

    if title_breadcrumbs:
        category = title_breadcrumbs[0]
        if len(title_breadcrumbs) > 1:
            specialization = title_breadcrumbs[-1]

    salary = currency = period = None
    location_text = None
    city = None
    experience_months = None
    english_level = None
    ukrainian_level = None
    employment_type = None
    work_format = None
    employment_types = []
    work_formats = []
    published_at = None
    aside_values = []

    aside = soup.select_one("aside")
    if aside:
        salary_text = normalize(aside.select_one(".card-header h3").get_text(" ", strip=True)) if aside.select_one(".card-header h3") else None
        salary, currency, period = parse_salary(salary_text)

        for li in aside.select("li"):
            value = normalize(li.get_text(" ", strip=True))
            if not value:
                continue
            use = li.select_one("use[href]")
            icon = (use.get("href") or "").split("#")[-1] if use else None
            aside_values.append({"icon": icon, "value": value})

            if icon in ("globe", "geo-alt"):
                location_text = value
                parsed_country, city = split_location(value)
                country = parsed_country or country
            elif icon == "lightning-charge":
                experience_months = parse_duration_to_months(value)
            elif icon in ("translate", "chat-text"):
                if value.lower().startswith("english:"):
                    english_level = normalize(value.split(":", 1)[1])
                elif value.lower().startswith("ukrainian:"):
                    ukrainian_level = normalize(value.split(":", 1)[1])
            elif icon == "check-circle":
                employment_types.append(value)
            elif icon == "laptop":
                work_formats.append(value)
            elif icon == "clock" and value.lower().startswith("published"):
                published_at = parse_published_date(value)

    employment_type = ", ".join(dict.fromkeys(employment_types)) or None
    work_format = ", ".join(dict.fromkeys(work_formats)) or None

    sections = extract_sections(soup)
    skills_exp = csc_items_under_heading(soup, "Skills experience", "csc--skills")
    agentic_exp = csc_items_under_heading(soup, "Agentic coding experience", "csc--skills")
    domain_exp = csc_items_under_heading(soup, "Domain experience", "csc--domain")
    languages = csc_items_under_heading(soup, "Languages", "csc--language")

    skills = [item["name"] for item in skills_exp]
    agentic_tools = [item["name"] for item in agentic_exp]
    domains = [item["name"] for item in domain_exp]

    text_blob = " ".join(
        value for value in [title, sections.get("Work experience"), sections.get("Looking for"), " ".join(domains)] if value
    ).lower()
    skill_blob = " ".join(skills + agentic_tools).lower()

    raw_profile = {
        "url": url,
        "breadcrumbs": breadcrumbs,
        "aside": aside_values,
        "sections": sections,
        "skills_experience": skills_exp,
        "agentic_coding_experience": agentic_exp,
        "domain_experience": domain_exp,
        "languages": languages,
    }

    return {
        "title": title,
        "category": category,
        "specialization": specialization,
        "salary_expectation": salary,
        "salary_currency": currency,
        "salary_period": period,
        "experience_months": experience_months,
        "experience_years": round(experience_months / 12, 1) if experience_months is not None else None,
        "english_level": english_level,
        "ukrainian_level": ukrainian_level,
        "location_text": location_text,
        "country": country,
        "city": city,
        "employment_type": employment_type,
        "work_format": work_format,
        "published_at": published_at,
        "work_experience": sections.get("Work experience"),
        "looking_for": sections.get("Looking for"),
        "skills": skills,
        "skills_experience": skills_exp,
        "domains": domains,
        "domain_experience": domain_exp,
        "languages": languages,
        "agentic_tools": agentic_tools,
        "sections": sections,
        "is_igaming": any(keyword in text_blob for keyword in IGAMING_KEYWORDS),
        "has_agentic_ai": bool(agentic_tools) or any(keyword in skill_blob for keyword in AI_KEYWORDS),
        "raw_profile": raw_profile,
    }


def merge_record(base, extra):
    merged = dict(base)
    for key, value in extra.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        merged[key] = value
    return merged


def to_jsonb(value):
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


class AmbiguousDetailError(RuntimeError):
    pass


def parse_listing_meta(soup):
    heading = soup.find("h1")
    heading_text = normalize(heading.get_text(" ", strip=True)) if heading else None
    total_match = re.search(r"Candidates\s+([\d\s,]+)", heading_text or "", re.I)
    site_total = clean_count(total_match.group(1)) if total_match else None

    page_numbers = []
    for link in soup.select('.pagination a[href*="page="]'):
        query = parse_qs(urlparse(link.get("href") or "").query)
        for value in query.get("page", []):
            if str(value).isdigit():
                page_numbers.append(int(value))
    return site_total, max(page_numbers) if page_numbers else None


def classify_candidate_detail_response(response, url):
    if response.status_code in (404, 410):
        return "deleted_confirmed", "detail_404", None

    response.raise_for_status()
    lowered_html = response.text.lower()
    raise_if_blocked(lowered_html)

    soup = BeautifulSoup(response.content, "html.parser")
    offline_evidence = []
    badge = soup.select_one("h1 .badge")
    badge_text = normalize(badge.get_text(" ", strip=True)) if badge else None
    if badge_text:
        offline_evidence.append(badge_text)
    for notice in soup.select("strong.text-danger, .alert-danger"):
        offline_evidence.append(normalize(notice.get_text(" ", strip=True)) or "")
    lowered_evidence = " ".join(offline_evidence).lower()
    if (badge_text or "").lower() == "offline" or any(marker in lowered_evidence for marker in OFFLINE_MARKERS):
        return "offline_confirmed", "detail_offline", None

    candidate_breadcrumb = any(
        normalize(link.get_text(" ", strip=True)) == "Candidates"
        and (link.get("href") or "").startswith("/developers/")
        for link in soup.select(".breadcrumb-item a[href]")
    )
    if (
        soup.select_one('form[action*="login"]')
        or not soup.select_one("h1")
        or not soup.select_one("aside")
        or not candidate_breadcrumb
    ):
        raise AmbiguousDetailError(f"Ambiguous candidate detail response: {url}")

    detail = parse_profile_detail(response.content, url)
    if not detail.get("title"):
        raise AmbiguousDetailError(f"Candidate detail has no profile title: {url}")
    return "active", "detail_confirmation", detail


def require_lifecycle_schema(engine):
    required_candidate_columns = {
        "status", "status_source", "status_checked_at", "inactive_detected_at",
        "reopened_count", "last_listing_seen_at", "last_detail_checked_at",
        "detail_scraped_at", "engagement_observed_at", "monitoring_cohort",
        "monitoring_enabled", "next_confirmation_due_at", "lifecycle_data_quality",
        "lifecycle_tracking_started_at", "entry_left_censored", "last_parser_version",
    }
    required_observation_columns = {
        "run_id", "observation_type", "status", "status_source", "position_on_page",
        "global_rank", "http_status", "parse_ok", "parse_error", "parser_version",
    }
    required_frontier_run_columns = {"new_candidate_limit", "frontier_key_pages"}
    required_confirmation_run_columns = {
        "due_candidates_before", "due_candidates_after",
        "pending_enrichment_selected", "pending_enrichment_attempted",
        "profiles_hydrated", "scheduled_existing_selected",
        "scheduled_existing_attempted", "confirmation_budget_exhausted",
    }
    with engine.connect() as conn:
        candidate_columns = {
            row[0] for row in conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name='djinni_candidates'
            """))
        }
        observation_columns = {
            row[0] for row in conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name='djinni_candidate_observations'
            """))
        }
        run_table = conn.execute(text("SELECT to_regclass('public.djinni_candidate_scrape_runs')")).scalar()
        run_columns = {
            row[0] for row in conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name='djinni_candidate_scrape_runs'
            """))
        }

    base_missing = sorted(
        (required_candidate_columns - candidate_columns)
        | (required_observation_columns - observation_columns)
    )
    frontier_missing = sorted(required_frontier_run_columns - run_columns)
    confirmation_missing = sorted(required_confirmation_run_columns - run_columns)
    if base_missing or run_table is None:
        migration_name = "006_candidate_lifecycle.sql"
        missing = base_missing + (["djinni_candidate_scrape_runs"] if run_table is None else [])
    elif frontier_missing:
        migration_name = "009_candidate_frontier_audit.sql"
        missing = frontier_missing
    elif confirmation_missing:
        migration_name = "010_candidate_confirmation_budget.sql"
        missing = confirmation_missing
    else:
        return

    migration = os.path.join(os.path.dirname(__file__), "migrations", migration_name)
    raise RuntimeError(
        f"Candidate lifecycle schema is incomplete ({', '.join(missing)}). "
        f"For a fresh database run schema.sql in the repository root. "
        f"Historical journal step (existing DBs only): {migration}"
    )


def acquire_candidate_lock(engine):
    lock_conn = engine.connect()
    acquired = lock_conn.execute(
        text("SELECT pg_try_advisory_lock(:lock_key)"),
        {"lock_key": CANDIDATE_LOCK_KEY},
    ).scalar_one()
    lock_conn.commit()
    if not acquired:
        lock_conn.close()
        raise RuntimeError("Another candidate scraper is already running")
    return lock_conn


def release_candidate_lock(lock_conn):
    if lock_conn is None:
        return
    try:
        lock_conn.execute(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": CANDIDATE_LOCK_KEY},
        )
        lock_conn.commit()
    finally:
        lock_conn.close()


def recover_abandoned_runs(engine):
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE public.djinni_candidate_scrape_runs
            SET status='failed', finished_at=CURRENT_TIMESTAMP,
                stop_reason='abandoned_run_recovered',
                error_summary=jsonb_build_object('message','Previous process ended without finalizing the run.')
            WHERE status='running'
        """))


def ensure_schema(engine):
    require_lifecycle_schema(engine)


def existing_candidate_id(conn, djinni_key):
    return conn.execute(
        text("SELECT id FROM public.djinni_candidates WHERE djinni_key = :djinni_key"),
        {"djinni_key": djinni_key},
    ).scalar()


def candidate_params(record):
    return {
        "djinni_key": record.get("djinni_key"),
        "title": record.get("title"),
        "category": record.get("category"),
        "specialization": record.get("specialization"),
        "salary_expectation": record.get("salary_expectation"),
        "salary_currency": record.get("salary_currency"),
        "salary_period": record.get("salary_period"),
        "experience_years": record.get("experience_years"),
        "experience_months": record.get("experience_months"),
        "english_level": record.get("english_level"),
        "ukrainian_level": record.get("ukrainian_level"),
        "location_text": record.get("location_text"),
        "country": record.get("country"),
        "city": record.get("city"),
        "employment_type": record.get("employment_type"),
        "work_format": record.get("work_format"),
        "work_experience": record.get("work_experience"),
        "looking_for": record.get("looking_for"),
        "skills": record.get("skills"),
        "skills_experience": to_jsonb(record.get("skills_experience")),
        "domains": record.get("domains"),
        "domain_experience": to_jsonb(record.get("domain_experience")),
        "languages": to_jsonb(record.get("languages")),
        "agentic_tools": record.get("agentic_tools"),
        "sections": to_jsonb(record.get("sections")),
        "views_count": record.get("views_count"),
        "published_at": record.get("published_at"),
        "url": record.get("url"),
        "is_confirmed": bool(record.get("is_confirmed")),
        "is_igaming": bool(record.get("is_igaming")),
        "has_agentic_ai": bool(record.get("has_agentic_ai")),
        "raw_list": to_jsonb(record.get("raw_list")),
        "raw_profile": to_jsonb(record.get("raw_profile")),
    }


def upsert_listing_candidate(conn, record):
    row = conn.execute(text("""
        INSERT INTO public.djinni_candidates (
            djinni_key, title, category, salary_expectation, salary_currency, salary_period,
            experience_years, experience_months, english_level, location_text, country, city,
            work_format, views_count, max_views_count, published_at, url, is_confirmed, raw_list,
            status, status_source, status_checked_at, first_seen_at, last_seen_at,
            last_listing_seen_at, engagement_observed_at, monitoring_cohort,
            lifecycle_data_quality, lifecycle_tracking_started_at, entry_left_censored,
            next_confirmation_due_at, last_parser_version
        ) VALUES (
            :djinni_key, :title, :category, :salary_expectation, :salary_currency, :salary_period,
            :experience_years, :experience_months, :english_level, :location_text, :country, :city,
            :work_format, :views_count, :views_count, :published_at, :url, :is_confirmed,
            CAST(:raw_list AS jsonb), 'active', 'listing', CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
            CASE WHEN :views_count IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END,
            'prospective_incremental', 'prospective_incremental', CURRENT_TIMESTAMP, FALSE,
            CURRENT_TIMESTAMP, :parser_version
        )
        ON CONFLICT (djinni_key) DO UPDATE SET
            title=COALESCE(EXCLUDED.title, public.djinni_candidates.title),
            category=COALESCE(EXCLUDED.category, public.djinni_candidates.category),
            salary_expectation=COALESCE(EXCLUDED.salary_expectation, public.djinni_candidates.salary_expectation),
            salary_currency=COALESCE(EXCLUDED.salary_currency, public.djinni_candidates.salary_currency),
            salary_period=COALESCE(EXCLUDED.salary_period, public.djinni_candidates.salary_period),
            experience_years=COALESCE(EXCLUDED.experience_years, public.djinni_candidates.experience_years),
            experience_months=COALESCE(EXCLUDED.experience_months, public.djinni_candidates.experience_months),
            english_level=COALESCE(EXCLUDED.english_level, public.djinni_candidates.english_level),
            location_text=COALESCE(EXCLUDED.location_text, public.djinni_candidates.location_text),
            country=COALESCE(EXCLUDED.country, public.djinni_candidates.country),
            city=COALESCE(EXCLUDED.city, public.djinni_candidates.city),
            work_format=COALESCE(EXCLUDED.work_format, public.djinni_candidates.work_format),
            views_count=CASE WHEN EXCLUDED.views_count IS NULL THEN public.djinni_candidates.views_count ELSE EXCLUDED.views_count END,
            max_views_count=CASE
                WHEN EXCLUDED.views_count IS NULL THEN public.djinni_candidates.max_views_count
                ELSE GREATEST(COALESCE(public.djinni_candidates.max_views_count, EXCLUDED.views_count), EXCLUDED.views_count)
            END,
            published_at=COALESCE(EXCLUDED.published_at, public.djinni_candidates.published_at),
            url=COALESCE(EXCLUDED.url, public.djinni_candidates.url),
            is_confirmed=public.djinni_candidates.is_confirmed OR EXCLUDED.is_confirmed,
            raw_list=COALESCE(EXCLUDED.raw_list, public.djinni_candidates.raw_list),
            reopened_count=public.djinni_candidates.reopened_count + CASE
                WHEN public.djinni_candidates.status IN ('offline_confirmed','deleted_confirmed') THEN 1 ELSE 0 END,
            status='active', status_source='listing', status_checked_at=CURRENT_TIMESTAMP,
            last_seen_at=CURRENT_TIMESTAMP, last_listing_seen_at=CURRENT_TIMESTAMP,
            engagement_observed_at=CASE WHEN EXCLUDED.views_count IS NULL
                THEN public.djinni_candidates.engagement_observed_at ELSE CURRENT_TIMESTAMP END,
            lifecycle_tracking_started_at=COALESCE(public.djinni_candidates.lifecycle_tracking_started_at, CURRENT_TIMESTAMP),
            next_confirmation_due_at=CASE WHEN public.djinni_candidates.raw_profile IS NULL
                THEN CURRENT_TIMESTAMP
                ELSE CURRENT_TIMESTAMP + (:active_confirmation_days * INTERVAL '1 day')
            END,
            scraped_at=CURRENT_TIMESTAMP, last_parser_version=:parser_version
        RETURNING id, (xmax = 0) AS inserted
    """), {
        **candidate_params(record),
        "parser_version": PARSER_VERSION,
        "active_confirmation_days": ACTIVE_CONFIRMATION_DAYS,
    }).mappings().one()
    return int(row["id"]), bool(row["inserted"])


def update_candidate_from_detail(conn, candidate_id, status, source, detail):
    params = candidate_params(detail or {})
    params.update({
        "candidate_id": candidate_id,
        "status": status,
        "source": source,
        "parser_version": PARSER_VERSION,
        "next_days": ACTIVE_CONFIRMATION_DAYS if status == "active" else INACTIVE_CONFIRMATION_DAYS,
    })
    conn.execute(text("""
        UPDATE public.djinni_candidates SET
            title=COALESCE(:title,title), category=COALESCE(:category,category),
            specialization=COALESCE(:specialization,specialization),
            salary_expectation=COALESCE(:salary_expectation,salary_expectation),
            salary_currency=COALESCE(:salary_currency,salary_currency),
            salary_period=COALESCE(:salary_period,salary_period),
            experience_years=COALESCE(:experience_years,experience_years),
            experience_months=COALESCE(:experience_months,experience_months),
            english_level=COALESCE(:english_level,english_level),
            ukrainian_level=COALESCE(:ukrainian_level,ukrainian_level),
            location_text=COALESCE(:location_text,location_text),
            country=COALESCE(:country,country), city=COALESCE(:city,city),
            employment_type=COALESCE(:employment_type,employment_type),
            work_format=COALESCE(:work_format,work_format),
            published_at=COALESCE(:published_at,published_at),
            work_experience=COALESCE(:work_experience,work_experience),
            looking_for=COALESCE(:looking_for,looking_for),
            skills=COALESCE(:skills,skills),
            skills_experience=COALESCE(CAST(:skills_experience AS jsonb),skills_experience),
            domains=COALESCE(:domains,domains),
            domain_experience=COALESCE(CAST(:domain_experience AS jsonb),domain_experience),
            languages=COALESCE(CAST(:languages AS jsonb),languages),
            agentic_tools=COALESCE(:agentic_tools,agentic_tools),
            sections=COALESCE(CAST(:sections AS jsonb),sections),
            is_igaming=is_igaming OR :is_igaming,
            has_agentic_ai=has_agentic_ai OR :has_agentic_ai,
            raw_profile=COALESCE(CAST(:raw_profile AS jsonb),raw_profile),
            reopened_count=reopened_count + CASE
                WHEN :status='active' AND status IN ('offline_confirmed','deleted_confirmed') THEN 1 ELSE 0 END,
            status=:status, status_source=:source, status_checked_at=CURRENT_TIMESTAMP,
            last_seen_at=CASE WHEN :status='active' THEN CURRENT_TIMESTAMP ELSE last_seen_at END,
            inactive_detected_at=CASE WHEN :status IN ('offline_confirmed','deleted_confirmed')
                THEN COALESCE(inactive_detected_at,CURRENT_TIMESTAMP) ELSE inactive_detected_at END,
            last_detail_checked_at=CURRENT_TIMESTAMP,
            detail_scraped_at=CASE WHEN :status='active' THEN CURRENT_TIMESTAMP ELSE detail_scraped_at END,
            lifecycle_tracking_started_at=COALESCE(lifecycle_tracking_started_at,CURRENT_TIMESTAMP),
            next_confirmation_due_at=CURRENT_TIMESTAMP + (:next_days * INTERVAL '1 day'),
            scraped_at=CURRENT_TIMESTAMP, last_parser_version=:parser_version
        WHERE id=:candidate_id
    """), params)


def insert_observation(
    conn, candidate_id, record, run_id, observation_type,
    status=None, status_source=None, page=None, sortby=None,
    position_on_page=None, global_rank=None, http_status=None,
    parse_ok=True, parse_error=None,
):
    conn.execute(text("""
        INSERT INTO public.djinni_candidate_observations (
            candidate_id,djinni_key,observed_at,views_count,page,sortby,url,
            run_id,observation_type,status,status_source,position_on_page,
            global_rank,http_status,parse_ok,parse_error,parser_version
        ) VALUES (
            :candidate_id,:djinni_key,CURRENT_TIMESTAMP,:views_count,:page,:sortby,:url,
            :run_id,:observation_type,:status,:status_source,:position_on_page,
            :global_rank,:http_status,:parse_ok,:parse_error,:parser_version
        )
        ON CONFLICT (run_id,candidate_id,observation_type) WHERE run_id IS NOT NULL DO NOTHING
    """), {
        "candidate_id": candidate_id,
        "djinni_key": record.get("djinni_key"),
        "views_count": record.get("views_count") if observation_type != "detail_status" else None,
        "page": page, "sortby": sortby, "url": record.get("url"),
        "run_id": run_id, "observation_type": observation_type,
        "status": status, "status_source": status_source,
        "position_on_page": position_on_page, "global_rank": global_rank,
        "http_status": http_status, "parse_ok": parse_ok,
        "parse_error": normalize(parse_error), "parser_version": PARSER_VERSION,
    })


def create_run(engine, args, prior_frontier):
    prior_keys = (prior_frontier or {}).get("keys") or []
    with engine.begin() as conn:
        return conn.execute(text("""
            INSERT INTO public.djinni_candidate_scrape_runs (
                run_kind,sortby,min_views,max_pages,detail_limit,listing_budget_minutes,
                new_candidate_limit,frontier_key_pages,
                internal_deadline_at,prior_frontier_key,prior_frontier_keys,
                prior_frontier_published_date,frontier_overlap_pages,parser_version
            ) VALUES (
                :run_kind,:sortby,:min_views,:max_pages,:detail_limit,:listing_budget,
                :new_candidate_limit,:frontier_key_pages,
                CURRENT_TIMESTAMP + (:deadline_minutes * INTERVAL '1 minute'),
                :prior_frontier,:prior_keys,:prior_date,:overlap,:parser_version
            ) RETURNING id
        """), {
            "run_kind": "confirmation_only" if args.confirmation_only
                else "incremental_listing_and_confirmation",
            "sortby": args.sortby, "min_views": args.min_views,
            "max_pages": args.max_pages, "detail_limit": args.confirmation_limit,
            "listing_budget": args.listing_budget_minutes,
            "new_candidate_limit": args.max_new_candidates,
            "frontier_key_pages": args.frontier_key_pages,
            "deadline_minutes": args.deadline_minutes,
            "prior_frontier": prior_keys[0] if prior_keys else None,
            "prior_keys": prior_keys or None,
            "prior_date": (prior_frontier or {}).get("published_date"),
            "overlap": args.frontier_overlap_pages, "parser_version": PARSER_VERSION,
        }).scalar_one()


def latest_frontier(engine):
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT new_frontier_key,new_frontier_keys,new_frontier_published_date
            FROM public.djinni_candidate_scrape_runs
            WHERE status='complete' AND scope_complete AND frontier_reached
              AND new_frontier_key IS NOT NULL
            ORDER BY id DESC LIMIT 1
        """)).mappings().first()
    if not row:
        return None
    return {
        "keys": list(row["new_frontier_keys"] or [row["new_frontier_key"]]),
        "published_date": row["new_frontier_published_date"],
    }


def existing_candidate_map(engine):
    with engine.connect() as conn:
        return {
            row["djinni_key"]: int(row["id"])
            for row in conn.execute(text("SELECT id,djinni_key FROM public.djinni_candidates")).mappings()
        }


def update_run_progress(engine, run_id, metrics, **extra):
    values = {**metrics, **extra, "run_id": run_id}
    assignments = [f"{key}=:{key}" for key in values if key != "run_id"]
    assignments.append("heartbeat_at=CURRENT_TIMESTAMP")
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE public.djinni_candidate_scrape_runs SET " + ",".join(assignments) + " WHERE id=:run_id"
        ), values)


def finalize_run(engine, run_id, metrics, status, scope_complete, stop_reason, error=None):
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE public.djinni_candidate_scrape_runs SET
                finished_at=CURRENT_TIMESTAMP, heartbeat_at=CURRENT_TIMESTAMP,
                status=:status, scope_complete=:scope_complete,
                pages_scraped=:pages_scraped, cards_seen=:cards_seen,
                unique_candidates_seen=:unique_candidates_seen,
                existing_candidates_seen=:existing_candidates_seen,
                new_candidates_inserted=:new_candidates_inserted,
                duplicate_cards=:duplicate_cards, listing_parse_errors=:listing_parse_errors,
                site_reported_total_start=:site_reported_total_start,
                site_reported_total_end=:site_reported_total_end,
                site_reported_pages_start=:site_reported_pages_start,
                site_reported_pages_end=:site_reported_pages_end,
                new_frontier_key=:new_frontier_key,
                new_frontier_keys=:new_frontier_keys,
                new_frontier_published_date=:new_frontier_published_date,
                frontier_reached=:frontier_reached,
                stop_reason=:stop_reason, detail_selected=:detail_selected,
                detail_attempted=:detail_attempted, detail_active=:detail_active,
                detail_offline=:detail_offline, detail_deleted=:detail_deleted,
                detail_ambiguous=:detail_ambiguous,
                detail_request_errors=:detail_request_errors,
                due_candidates_before=:due_candidates_before,
                due_candidates_after=:due_candidates_after,
                pending_enrichment_selected=:pending_enrichment_selected,
                pending_enrichment_attempted=:pending_enrichment_attempted,
                profiles_hydrated=:profiles_hydrated,
                scheduled_existing_selected=:scheduled_existing_selected,
                scheduled_existing_attempted=:scheduled_existing_attempted,
                confirmation_budget_exhausted=:confirmation_budget_exhausted,
                db_errors=:db_errors,
                error_summary=CASE WHEN :error IS NULL THEN NULL
                    ELSE jsonb_build_object('message',CAST(:error AS text)) END
            WHERE id=:run_id
        """), {**metrics, "run_id": run_id, "status": status,
                 "scope_complete": scope_complete, "stop_reason": stop_reason,
                 "error": normalize(error)})


def initial_metrics():
    return {
        "pages_scraped": 0, "cards_seen": 0, "unique_candidates_seen": 0,
        "existing_candidates_seen": 0, "new_candidates_inserted": 0,
        "duplicate_cards": 0, "listing_parse_errors": 0,
        "site_reported_total_start": None, "site_reported_total_end": None,
        "site_reported_pages_start": None, "site_reported_pages_end": None,
        "new_frontier_key": None, "new_frontier_keys": [],
        "new_frontier_published_date": None, "frontier_reached": False,
        "detail_selected": 0, "detail_attempted": 0, "detail_active": 0,
        "detail_offline": 0, "detail_deleted": 0, "detail_ambiguous": 0,
        "detail_request_errors": 0, "db_errors": 0,
        "due_candidates_before": 0, "due_candidates_after": 0,
        "pending_enrichment_selected": 0, "pending_enrichment_attempted": 0,
        "profiles_hydrated": 0, "scheduled_existing_selected": 0,
        "scheduled_existing_attempted": 0,
        "confirmation_budget_exhausted": False,
    }


def canonical_listing_scope(args):
    return (
        args.start_page == 1 and args.sortby == "date" and args.min_views == 0
        and args.min_salary == 0 and args.min_exp_months == 0
    )


def passes_new_candidate_filters(record, args):
    views = record.get("views_count")
    return (
        (args.min_views == 0 or (views is not None and views >= args.min_views))
        and (args.min_salary == 0 or (record.get("salary_expectation") or 0) >= args.min_salary)
        and (args.min_exp_months == 0 or (record.get("experience_months") or 0) >= args.min_exp_months)
    )


def run_incremental_listing(engine, scraper, args, run_id, prior_frontier, known, metrics, deadline):
    # Discovery skipped: anonymous /developers/ is behind a login wall.
    raise RuntimeError(
        "Candidate listing discovery path invoked but is fail-loud: "
        "/developers/ is behind a login wall for anonymous visitors."
    )


def count_due_candidates(engine, missing_profile=None):
    profile_filter = ""
    if missing_profile is True:
        profile_filter = "AND raw_profile IS NULL"
    elif missing_profile is False:
        profile_filter = "AND raw_profile IS NOT NULL"
    with engine.connect() as conn:
        return int(conn.execute(text(f"""
            SELECT COUNT(*)
            FROM public.djinni_candidates
            WHERE monitoring_enabled
              AND COALESCE(next_confirmation_due_at, CURRENT_TIMESTAMP) <= CURRENT_TIMESTAMP
              {profile_filter}
        """)).scalar_one())


def select_confirmation_batch(engine, limit, missing_profile):
    if limit <= 0:
        return []
    profile_filter = "raw_profile IS NULL" if missing_profile else "raw_profile IS NOT NULL"
    with engine.connect() as conn:
        return conn.execute(text(f"""
            SELECT id,djinni_key,url,status,raw_profile
            FROM public.djinni_candidates
            WHERE monitoring_enabled
              AND COALESCE(next_confirmation_due_at, CURRENT_TIMESTAMP) <= CURRENT_TIMESTAMP
              AND {profile_filter}
            ORDER BY
                (lifecycle_tracking_started_at IS NULL) DESC,
                next_confirmation_due_at NULLS FIRST,
                id
            LIMIT :limit
        """), {"limit": limit}).mappings().all()


def confirmation_capacity(deadline, requested_limit, now=None):
    now = now or datetime.now()
    available_seconds = max(
        0,
        (deadline - now).total_seconds() - DETAIL_DEADLINE_BUFFER_SECONDS,
    )
    return min(requested_limit, int(available_seconds // DETAIL_BUDGET_SECONDS))


def confirmation_budget_was_exhausted(
    due_after, capacity, requested_limit, selected, reason,
):
    if reason not in ("detail_batch_complete", "detail_internal_deadline"):
        return False
    return (
        due_after > 0
        and (
            reason == "detail_internal_deadline"
            or capacity < requested_limit
            or selected >= requested_limit
        )
    )


def record_failed_detail_attempt(engine, run_id, candidate, http_status, error):
    record = {"djinni_key": candidate["djinni_key"], "url": candidate["url"], "views_count": None}
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE public.djinni_candidates SET
                last_detail_checked_at=CURRENT_TIMESTAMP,
                next_confirmation_due_at=CURRENT_TIMESTAMP + INTERVAL '1 day',
                last_parser_version=:parser_version
            WHERE id=:candidate_id
        """), {"candidate_id": candidate["id"], "parser_version": PARSER_VERSION})
        insert_observation(
            conn, candidate["id"], record, run_id, "detail_status",
            http_status=http_status, parse_ok=False, parse_error=str(error),
        )


def run_confirmation_batch(
    engine, scraper, args, run_id, metrics, deadline, candidates, phase,
):
    for candidate in candidates:
        if datetime.now() >= deadline:
            return "detail_internal_deadline"

        metrics["detail_attempted"] += 1
        if phase == "pending_enrichment":
            metrics["pending_enrichment_attempted"] += 1
        else:
            metrics["scheduled_existing_attempted"] += 1

        response = None
        result = None
        last_error = None
        for attempt in range(1, 3):
            time.sleep(random.uniform(args.detail_sleep_min, args.detail_sleep_max))
            try:
                response = scraper.get(candidate["url"], headers=HEADERS, timeout=args.request_timeout)
                result = classify_candidate_detail_response(response, candidate["url"])
                break
            except BlockedResponseError as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    print(f"⚠️ Retry detail [{candidate['djinni_key']}] after: {exc}")

        if result is None:
            if isinstance(last_error, AmbiguousDetailError):
                metrics["detail_ambiguous"] += 1
            else:
                metrics["detail_request_errors"] += 1
            try:
                record_failed_detail_attempt(
                    engine, run_id, candidate,
                    response.status_code if response is not None else None,
                    last_error or "unknown detail error",
                )
            except Exception as db_exc:
                metrics["db_errors"] += 1
                raise RuntimeError(
                    f"Failed to persist detail error for {candidate['djinni_key']}: {db_exc}"
                ) from db_exc
            print(f"❌ Detail unresolved [{candidate['djinni_key']}]: {last_error}")
            update_run_progress(engine, run_id, metrics)
            if isinstance(last_error, BlockedResponseError):
                return "detail_blocked"
            continue

        status, source, detail = result
        record = {"djinni_key": candidate["djinni_key"], "url": candidate["url"], "views_count": None}
        try:
            with engine.begin() as conn:
                update_candidate_from_detail(conn, candidate["id"], status, source, detail)
                insert_observation(
                    conn, candidate["id"], record, run_id, "detail_status",
                    status=status, status_source=source,
                    http_status=response.status_code,
                )
        except Exception as db_exc:
            metrics["db_errors"] += 1
            raise RuntimeError(
                f"Failed to persist detail status for {candidate['djinni_key']}: {db_exc}"
            ) from db_exc
        if status == "active":
            metrics["detail_active"] += 1
            if phase == "pending_enrichment" and detail is not None:
                metrics["profiles_hydrated"] += 1
        elif status == "offline_confirmed":
            metrics["detail_offline"] += 1
        elif status == "deleted_confirmed":
            metrics["detail_deleted"] += 1
        print(f"🔎 [{candidate['djinni_key']}] status={status} phase={phase}")
        update_run_progress(engine, run_id, metrics)

    return "detail_batch_complete"


def run_confirmations(engine, scraper, args, run_id, metrics, deadline):
    pending_count = count_due_candidates(engine, missing_profile=True)
    pending = select_confirmation_batch(engine, pending_count, missing_profile=True)
    metrics["pending_enrichment_selected"] = len(pending)
    metrics["detail_selected"] += len(pending)
    update_run_progress(engine, run_id, metrics)

    pending_reason = run_confirmation_batch(
        engine, scraper, args, run_id, metrics, deadline,
        pending, "pending_enrichment",
    )
    pending_complete = (
        metrics["pending_enrichment_attempted"]
        == metrics["pending_enrichment_selected"]
    )
    if pending_reason != "detail_batch_complete" or not pending_complete:
        metrics["confirmation_budget_exhausted"] = (
            pending_reason == "detail_internal_deadline"
        )
        metrics["due_candidates_after"] = count_due_candidates(engine)
        update_run_progress(engine, run_id, metrics)
        return pending_reason, False

    capacity = confirmation_capacity(deadline, args.confirmation_limit)
    scheduled = select_confirmation_batch(engine, capacity, missing_profile=False)
    metrics["scheduled_existing_selected"] = len(scheduled)
    metrics["detail_selected"] += len(scheduled)
    update_run_progress(engine, run_id, metrics)

    scheduled_reason = run_confirmation_batch(
        engine, scraper, args, run_id, metrics, deadline,
        scheduled, "scheduled_existing",
    )
    scheduled_complete = (
        metrics["scheduled_existing_attempted"]
        == metrics["scheduled_existing_selected"]
    )
    metrics["due_candidates_after"] = count_due_candidates(engine)
    metrics["confirmation_budget_exhausted"] = confirmation_budget_was_exhausted(
        metrics["due_candidates_after"],
        capacity,
        args.confirmation_limit,
        len(scheduled),
        scheduled_reason,
    )
    update_run_progress(engine, run_id, metrics)

    if scheduled_reason != "detail_batch_complete" or not scheduled_complete:
        return scheduled_reason, False
    if metrics["confirmation_budget_exhausted"]:
        return "detail_budget_complete", True
    return "detail_queue_drained", True


def scrape_candidates(args):
    if not args.confirmation_only:
        raise RuntimeError(
            "Candidate listing discovery is refused: anonymous /developers/ "
            "redirects to /login since Aug 2026. Run in confirmation-only mode "
            "(default)."
        )
    scraper = create_djinni_scraper()
    metrics = initial_metrics()
    deadline = datetime.now() + timedelta(minutes=args.deadline_minutes)

    engine = load_engine()
    ensure_schema(engine)
    lock_conn = None
    run_id = None
    try:
        lock_conn = acquire_candidate_lock(engine)
        recover_abandoned_runs(engine)
        prior_frontier = latest_frontier(engine)
        known = existing_candidate_map(engine)
        run_id = create_run(engine, args, prior_frontier)
        metrics["due_candidates_before"] = count_due_candidates(engine)
        pending_backlog_before = count_due_candidates(engine, missing_profile=True)
        update_run_progress(engine, run_id, metrics)
        print(
            f"Candidate run {run_id} started | tracked={len(known)} | "
            f"due={metrics['due_candidates_before']} | "
            f"pending_enrichment={pending_backlog_before} | "
            f"prior_frontier={prior_frontier or 'bootstrap'}"
        )

        listing_reason = "confirmation_only"
        detail_reason, confirmation_complete = run_confirmations(
            engine, scraper, args, run_id, metrics, deadline
        )

        no_errors = (
            metrics["listing_parse_errors"] == 0
            and metrics["detail_ambiguous"] == 0
            and metrics["detail_request_errors"] == 0
            and metrics["db_errors"] == 0
        )
        scope_complete = bool(confirmation_complete and no_errors)
        status = "complete" if scope_complete else "partial"
        stop_reason = f"listing={listing_reason};detail={detail_reason}"
        finalize_run(engine, run_id, metrics, status, scope_complete, stop_reason)

        print("\nCandidate run summary")
        print(f"  run_id={run_id} status={status} scope_complete={scope_complete}")
        for key, value in metrics.items():
            print(f"  {key}={value}")
        print("  global_coverage=False (tracked cohort only)")
        return 0 if scope_complete else 1
    except Exception as exc:
        if run_id is not None:
            finalize_run(engine, run_id, metrics, "failed", False, "unhandled_exception", exc)
        print(f"\n❌ Candidate scraper failed: {exc}")
        return 1
    finally:
        release_candidate_lock(lock_conn)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Confirm a monitored Djinni candidate cohort (confirmation-only by default)."
    )
    parser.add_argument("--min-views", type=int, default=0)
    parser.add_argument("--min-salary", type=int, default=0)
    parser.add_argument("--min-exp-months", type=int, default=0)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--sortby", choices=["date", "experience", "salary_max", "salary_min"], default="date")
    parser.add_argument("--confirmation-limit", type=int, default=1000)
    parser.add_argument(
        "--max-new-candidates", type=int, default=500,
        help="Bound newly tracked listing-only profiles so the detail batch can catch up.",
    )
    parser.add_argument(
        "--confirmation-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Confirm stored cohort only (default). --no-confirmation-only attempts discovery and is refused.",
    )
    parser.add_argument("--frontier-key-pages", type=int, default=3)
    parser.add_argument("--frontier-overlap-pages", type=int, default=2)
    parser.add_argument("--deadline-minutes", type=float, default=330.0)
    parser.add_argument("--listing-budget-minutes", type=float, default=75.0)
    parser.add_argument("--request-timeout", type=float, default=25.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--detail-sleep-min", type=float, default=7.0)
    parser.add_argument("--detail-sleep-max", type=float, default=14.0)
    parser.add_argument("--page-sleep-min", type=float, default=12.0)
    parser.add_argument("--page-sleep-max", type=float, default=20.0)
    args = parser.parse_args()

    if not args.confirmation_only:
        parser.error(
            "Candidate listing discovery is refused: anonymous /developers/ "
            "redirects to /login since Aug 2026. Omit --no-confirmation-only."
        )
    if args.dry_run:
        parser.error(
            "--dry-run only exercised listing discovery, which is refused (login wall)."
        )
    if args.start_page < 1:
        parser.error("--start-page must be >= 1")
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be >= 1")
    if min(
        args.min_views, args.min_salary, args.min_exp_months,
        args.confirmation_limit, args.max_new_candidates,
    ) < 0:
        parser.error("filters and candidate limits must be non-negative")
    if args.frontier_key_pages < 1:
        parser.error("--frontier-key-pages must be >= 1")
    if args.frontier_overlap_pages < 0:
        parser.error("--frontier-overlap-pages must be non-negative")
    if args.deadline_minutes <= 0 or args.listing_budget_minutes <= 0 or args.request_timeout <= 0:
        parser.error("deadlines, listing budget, and request timeout must be positive")
    if args.listing_budget_minutes >= args.deadline_minutes:
        parser.error("--listing-budget-minutes must be smaller than --deadline-minutes")
    if args.detail_sleep_min < 0 or args.detail_sleep_max < args.detail_sleep_min:
        parser.error("invalid detail sleep range")
    if args.page_sleep_min < 0 or args.page_sleep_max < args.page_sleep_min:
        parser.error("invalid page sleep range")
    return args


if __name__ == "__main__":
    raise SystemExit(scrape_candidates(parse_args()))
