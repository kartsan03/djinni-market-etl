"""Scrape public candidate profiles from djinni.co into SQLite.

Walks the public candidate listing, filters cards by views/salary/experience,
opens each profile for full details, and upserts into the candidates table.
Every sighting is also appended to candidate_observations so view counts can
be tracked over time.
"""

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import cloudscraper
from bs4 import BeautifulSoup, Tag

import db

BASE_URL = "https://djinni.co"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    djinni_key TEXT NOT NULL UNIQUE,
    title TEXT,
    category TEXT,
    specialization TEXT,
    salary_expectation INTEGER,
    salary_currency TEXT DEFAULT 'USD',
    salary_period TEXT DEFAULT 'mo',
    experience_years REAL,
    experience_months INTEGER,
    english_level TEXT,
    ukrainian_level TEXT,
    location_text TEXT,
    country TEXT,
    city TEXT,
    employment_type TEXT,
    work_format TEXT,
    work_experience TEXT,
    looking_for TEXT,
    skills TEXT,
    skills_experience TEXT,
    domains TEXT,
    domain_experience TEXT,
    languages TEXT,
    agentic_tools TEXT,
    sections TEXT,
    views_count INTEGER DEFAULT 0,
    max_views_count INTEGER DEFAULT 0,
    first_seen_at TEXT DEFAULT (datetime('now')),
    last_seen_at TEXT DEFAULT (datetime('now')),
    published_at TEXT,
    scraped_at TEXT DEFAULT (datetime('now')),
    url TEXT,
    is_confirmed INTEGER DEFAULT 0,
    raw_list TEXT,
    raw_profile TEXT
);

CREATE TABLE IF NOT EXISTS candidate_observations (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    djinni_key TEXT NOT NULL,
    observed_at TEXT DEFAULT (datetime('now')),
    views_count INTEGER NOT NULL DEFAULT 0,
    page INTEGER,
    sortby TEXT,
    url TEXT
);

CREATE INDEX IF NOT EXISTS idx_candidates_views ON candidates(max_views_count);
CREATE INDEX IF NOT EXISTS idx_candidates_salary ON candidates(salary_expectation);
CREATE INDEX IF NOT EXISTS idx_candidates_category ON candidates(category);
CREATE INDEX IF NOT EXISTS idx_observations_key_time ON candidate_observations(djinni_key, observed_at DESC);
"""

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def normalize(value):
    if value is None:
        return None
    text_value = " ".join(str(value).split())
    return text_value or None


def clean_count(value):
    if not value:
        return 0
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else 0


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


def parse_published_date(value):
    value = (normalize(value) or "").replace("Published", "").strip()
    for fmt in ("%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
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

    salary_node = card.select_one(".fs-2.text-success")
    salary_text = normalize(salary_node.get_text(" ", strip=True)) if salary_node else None
    salary, currency, period = parse_salary(salary_text)

    views_node = card.select_one('.card-footer span[title*="view"]')
    views_count = clean_count(views_node.get("title") or views_node.get_text(" ", strip=True)) if views_node else 0

    rows = [normalize(row.get_text(" ", strip=True)) for row in card.select(".font-size-small .d-flex")]
    rows = [row for row in rows if row]

    category = None
    experience_months = None
    english_level = None
    published_label = None
    location_text = None
    work_format = None

    if rows:
        for part in [part.strip() for part in rows[0].split("·")]:
            if not part:
                continue
            if "experience" in part.lower():
                experience_months = parse_duration_to_months(part)
            elif part.lower().startswith("published"):
                published_label = part
            elif re.match(r"^[ABC][12]\b|^(Fluent|Native|No English|Basic|Pre-Intermediate|Intermediate|Upper)", part, re.I):
                english_level = part
            elif category is None:
                category = part

    if len(rows) > 1:
        location_parts = [part.strip() for part in rows[1].split("·") if part.strip()]
        if location_parts:
            location_text = location_parts[0]
        if len(location_parts) > 1:
            work_format = location_parts[1]

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
        "views_count": views_count,
        "is_confirmed": is_confirmed,
        "raw_list": raw_list,
    }


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
    published_at = None
    aside_values = []

    aside = soup.select_one("aside")
    if aside:
        salary_node = aside.select_one(".card-header h3")
        salary_text = normalize(salary_node.get_text(" ", strip=True)) if salary_node else None
        salary, currency, period = parse_salary(salary_text)

        for li in aside.select("li"):
            value = normalize(li.get_text(" ", strip=True))
            if not value:
                continue
            use = li.select_one("use[href]")
            icon = (use.get("href") or "").split("#")[-1] if use else None
            aside_values.append({"icon": icon, "value": value})

            if icon == "globe":
                location_text = value
                parsed_country, city = split_location(value)
                country = parsed_country or country
            elif icon == "lightning-charge":
                experience_months = parse_duration_to_months(value)
            elif icon == "chat-text":
                if value.lower().startswith("english:"):
                    english_level = normalize(value.split(":", 1)[1])
                elif value.lower().startswith("ukrainian:"):
                    ukrainian_level = normalize(value.split(":", 1)[1])
            elif icon == "check-circle":
                if any(word in value.lower() for word in ("remote", "office", "hybrid")):
                    work_format = value
                else:
                    employment_type = value
            elif icon == "clock" and value.lower().startswith("published"):
                published_at = parse_published_date(value)

    sections = extract_sections(soup)
    skills_exp = csc_items_under_heading(soup, "Skills experience", "csc--skills")
    agentic_exp = csc_items_under_heading(soup, "Agentic coding experience", "csc--skills")
    domain_exp = csc_items_under_heading(soup, "Domain experience", "csc--domain")
    languages = csc_items_under_heading(soup, "Languages", "csc--language")

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
        "skills": [item["name"] for item in skills_exp],
        "skills_experience": skills_exp,
        "domains": [item["name"] for item in domain_exp],
        "domain_experience": domain_exp,
        "languages": languages,
        "agentic_tools": [item["name"] for item in agentic_exp],
        "sections": sections,
        "raw_profile": raw_profile,
    }


def merge_record(base, extra):
    merged = dict(base)
    for key, value in extra.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        merged[key] = value
    return merged


def to_json(value):
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


JSON_FIELDS = ("skills", "skills_experience", "domains", "domain_experience",
               "languages", "agentic_tools", "sections", "raw_list", "raw_profile")


def upsert_candidate(conn, record):
    params = {key: record.get(key) for key in (
        "djinni_key", "title", "category", "specialization",
        "salary_expectation", "salary_currency", "salary_period",
        "experience_years", "experience_months", "english_level", "ukrainian_level",
        "location_text", "country", "city", "employment_type", "work_format",
        "work_experience", "looking_for", "published_at", "url",
    )}
    for key in JSON_FIELDS:
        params[key] = to_json(record.get(key))
    params["views_count"] = record.get("views_count") or 0
    params["is_confirmed"] = 1 if record.get("is_confirmed") else 0

    conn.execute("""
        INSERT INTO candidates (
            djinni_key, title, category, specialization,
            salary_expectation, salary_currency, salary_period,
            experience_years, experience_months, english_level, ukrainian_level,
            location_text, country, city, employment_type, work_format,
            work_experience, looking_for,
            skills, skills_experience, domains, domain_experience, languages, agentic_tools, sections,
            views_count, max_views_count, published_at, url, is_confirmed,
            raw_list, raw_profile
        ) VALUES (
            :djinni_key, :title, :category, :specialization,
            :salary_expectation, :salary_currency, :salary_period,
            :experience_years, :experience_months, :english_level, :ukrainian_level,
            :location_text, :country, :city, :employment_type, :work_format,
            :work_experience, :looking_for,
            :skills, :skills_experience, :domains, :domain_experience, :languages, :agentic_tools, :sections,
            :views_count, :views_count, :published_at, :url, :is_confirmed,
            :raw_list, :raw_profile
        )
        ON CONFLICT(djinni_key) DO UPDATE SET
            title = COALESCE(excluded.title, title),
            category = COALESCE(excluded.category, category),
            specialization = COALESCE(excluded.specialization, specialization),
            salary_expectation = COALESCE(excluded.salary_expectation, salary_expectation),
            salary_currency = COALESCE(excluded.salary_currency, salary_currency),
            salary_period = COALESCE(excluded.salary_period, salary_period),
            experience_years = COALESCE(excluded.experience_years, experience_years),
            experience_months = COALESCE(excluded.experience_months, experience_months),
            english_level = COALESCE(excluded.english_level, english_level),
            ukrainian_level = COALESCE(excluded.ukrainian_level, ukrainian_level),
            location_text = COALESCE(excluded.location_text, location_text),
            country = COALESCE(excluded.country, country),
            city = COALESCE(excluded.city, city),
            employment_type = COALESCE(excluded.employment_type, employment_type),
            work_format = COALESCE(excluded.work_format, work_format),
            work_experience = COALESCE(excluded.work_experience, work_experience),
            looking_for = COALESCE(excluded.looking_for, looking_for),
            skills = COALESCE(excluded.skills, skills),
            skills_experience = COALESCE(excluded.skills_experience, skills_experience),
            domains = COALESCE(excluded.domains, domains),
            domain_experience = COALESCE(excluded.domain_experience, domain_experience),
            languages = COALESCE(excluded.languages, languages),
            agentic_tools = COALESCE(excluded.agentic_tools, agentic_tools),
            sections = COALESCE(excluded.sections, sections),
            views_count = excluded.views_count,
            max_views_count = max(coalesce(max_views_count, 0), coalesce(excluded.views_count, 0)),
            published_at = COALESCE(excluded.published_at, published_at),
            url = COALESCE(excluded.url, url),
            is_confirmed = max(is_confirmed, excluded.is_confirmed),
            raw_list = COALESCE(excluded.raw_list, raw_list),
            raw_profile = COALESCE(excluded.raw_profile, raw_profile),
            last_seen_at = datetime('now'),
            scraped_at = datetime('now')
    """, params)
    return conn.execute(
        "SELECT id FROM candidates WHERE djinni_key = ?", (record["djinni_key"],)
    ).fetchone()[0]


def insert_observation(conn, candidate_id, record, page, sortby):
    conn.execute("""
        INSERT INTO candidate_observations (candidate_id, djinni_key, views_count, page, sortby, url)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (candidate_id, record.get("djinni_key"), record.get("views_count") or 0,
          page, sortby, record.get("url")))


def scrape_candidates(args):
    conn = db.connect()
    conn.executescript(SCHEMA_SQL)

    scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    page = args.start_page
    last_page = args.start_page + args.max_pages - 1 if args.max_pages else None

    total_cards = 0
    eligible_cards = 0
    inserted = 0
    updated = 0
    skipped = 0

    while True:
        if last_page and page > last_page:
            break

        list_url = build_list_url(page, args.sortby)
        print(f"\nCandidates page {page}: {list_url}")
        response = scraper.get(list_url, headers=HEADERS, timeout=25)
        if response.status_code == 404:
            print("404: end of list")
            break
        response.raise_for_status()
        if "has been blocked" in response.text.lower():
            raise RuntimeError("Djinni denied access. Stop this run and retry later with longer sleeps.")

        soup = BeautifulSoup(response.content, "html.parser")
        cards = soup.select(".card.mb-4")
        if not cards:
            print("No candidate cards found")
            break

        page_eligible = 0
        for card in cards:
            total_cards += 1
            list_record = parse_listing_card(card)
            if not list_record:
                skipped += 1
                continue

            if list_record["views_count"] < args.min_views:
                skipped += 1
                continue
            if args.min_salary and (list_record.get("salary_expectation") or 0) < args.min_salary:
                skipped += 1
                continue
            if args.min_exp_months and (list_record.get("experience_months") or 0) < args.min_exp_months:
                skipped += 1
                continue

            eligible_cards += 1
            page_eligible += 1
            existing = conn.execute(
                "SELECT id FROM candidates WHERE djinni_key = ?", (list_record["djinni_key"],)
            ).fetchone()

            detail_record = {}
            if args.refresh_existing or existing is None or args.dry_run:
                time.sleep(random.uniform(args.detail_sleep_min, args.detail_sleep_max))
                try:
                    detail_response = scraper.get(list_record["url"], headers=HEADERS, timeout=25)
                    detail_response.raise_for_status()
                    if "has been blocked" in detail_response.text.lower():
                        raise RuntimeError("Djinni denied access")
                    detail_record = parse_profile_detail(detail_response.content, list_record["url"])
                except Exception as exc:
                    print(f"Detail failed {list_record['url']}: {exc}")

            record = merge_record(list_record, detail_record)

            if args.dry_run:
                print(
                    f"DRY {record['djinni_key']} | views {record['views_count']} | "
                    f"{record.get('title')} | {record.get('salary_expectation')} | "
                    f"exp={record.get('experience_months')}m | skills={len(record.get('skills') or [])}"
                )
                continue

            try:
                with conn:
                    candidate_id = upsert_candidate(conn, record)
                    insert_observation(conn, candidate_id, record, page, args.sortby)
                if existing:
                    updated += 1
                    action = "updated"
                else:
                    inserted += 1
                    action = "inserted"
                print(
                    f"{action} {record['djinni_key']} | views {record['views_count']} | "
                    f"{record.get('title')} | {record.get('salary_expectation')}"
                )
            except Exception as exc:
                skipped += 1
                print(f"DB error {record.get('djinni_key')}: {exc}")

        print(f"Page {page}: eligible {page_eligible}/{len(cards)}")
        page += 1
        time.sleep(random.uniform(args.page_sleep_min, args.page_sleep_max))

    print(
        f"\nDone. cards={total_cards}, eligible={eligible_cards}, "
        f"inserted={inserted}, updated={updated}, skipped={skipped}"
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Scrape public djinni.co candidate profiles into SQLite.")
    parser.add_argument("--min-views", type=int, default=5, help="Only save profiles with at least this many views. Default: 5")
    parser.add_argument("--min-salary", type=int, default=0, help="Optional salary floor.")
    parser.add_argument("--min-exp-months", type=int, default=0, help="Optional experience floor in months.")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=None, help="Limit pages for test runs. Omit for a full scan.")
    parser.add_argument("--sortby", choices=["date", "experience", "salary_max", "salary_min"], default="date")
    parser.add_argument("--refresh-existing", action="store_true", help="Fetch the detail page even if the candidate already exists.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print, do not write to the database.")
    parser.add_argument("--detail-sleep-min", type=float, default=5.0)
    parser.add_argument("--detail-sleep-max", type=float, default=10.0)
    parser.add_argument("--page-sleep-min", type=float, default=12.0)
    parser.add_argument("--page-sleep-max", type=float, default=20.0)
    return parser.parse_args()


if __name__ == "__main__":
    scrape_candidates(parse_args())
