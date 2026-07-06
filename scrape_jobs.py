"""Scrape job postings from djinni.co job listings into SQLite.

Walks the public listing pages, opens each job it has not seen before,
and stores the structured data from the page's JSON-LD block plus the
visible view/application counters.
"""

import argparse
import json
import random
import re
import sys
import time

import cloudscraper
from bs4 import BeautifulSoup

import db

BASE_URL = "https://djinni.co"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    djinni_id INTEGER NOT NULL UNIQUE,
    title TEXT,
    company TEXT,
    category TEXT,
    salary_min INTEGER,
    salary_max INTEGER,
    salary_currency TEXT,
    experience_months INTEGER,
    description TEXT,
    url TEXT,
    published_at TEXT,
    views_count INTEGER DEFAULT 0,
    applications_count INTEGER DEFAULT 0,
    tags TEXT,
    raw_json_ld TEXT,
    scraped_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_published ON jobs(published_at);
"""

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def get_page(scraper, url, timeout):
    """Fetch a page; None on 404, raises on HTTP errors or an access-denied page."""
    response = scraper.get(url, headers=HEADERS, timeout=timeout)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    if "has been blocked" in response.text.lower():
        raise RuntimeError("Djinni denied access. Stop this run and retry later with longer sleeps.")
    return response


def parse_job_page(html):
    """Extract one job from its detail page. Returns None if there is no usable JSON-LD."""
    soup = BeautifulSoup(html, "html.parser")

    ld_script = soup.find("script", type="application/ld+json")
    if not ld_script:
        return None
    data = json.loads(ld_script.string)
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None

    salary = data.get("estimatedSalary")
    if not isinstance(salary, dict):
        salary = {}
    experience = data.get("experienceRequirements")
    if not isinstance(experience, dict):
        experience = {}
    organization = data.get("hiringOrganization")
    if not isinstance(organization, dict):
        organization = {}

    # View/application counts are not in the JSON-LD, only in the page text.
    page_text = soup.get_text()
    views_match = re.search(r"(\d+)\s+views", page_text)
    applications_match = re.search(r"(\d+)\s+applications", page_text)

    description_node = soup.select_one(".job-post__description")
    tags = [tag.get_text(strip=True) for tag in soup.select(".job-post__tags .job-post__tag")]

    return {
        "title": data.get("title"),
        "company": organization.get("name"),
        "category": data.get("category"),
        "salary_min": salary.get("minValue"),
        "salary_max": salary.get("maxValue"),
        "salary_currency": salary.get("currency"),
        "experience_months": experience.get("monthsOfExperience"),
        "description": description_node.get_text(strip=True) if description_node else "",
        "published_at": data.get("datePosted"),
        "views_count": int(views_match.group(1)) if views_match else 0,
        "applications_count": int(applications_match.group(1)) if applications_match else 0,
        "tags": json.dumps(tags, ensure_ascii=False),
        "raw_json_ld": json.dumps(data, ensure_ascii=False),
    }


def scrape_jobs(args):
    conn = db.connect()
    conn.executescript(SCHEMA_SQL)

    scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    page = args.start_page
    last_page = args.start_page + args.max_pages - 1 if args.max_pages else None
    inserted = 0
    seen = 0

    while last_page is None or page <= last_page:
        list_url = f"{BASE_URL}/jobs/?page={page}"
        print(f"Page {page}: {list_url}")
        response = get_page(scraper, list_url, timeout=20)
        if response is None:
            print("404: end of list")
            break

        soup = BeautifulSoup(response.text, "html.parser")
        items = soup.select(".job-item")
        if not items:
            print("No job items found, stopping")
            break

        for item in items:
            link = item.select_one("a.job_item__header-link")
            if not link:
                continue
            id_match = re.search(r"/jobs/(\d+)-", link["href"])
            if not id_match:
                continue
            djinni_id = int(id_match.group(1))
            job_url = BASE_URL + link["href"]

            if conn.execute("SELECT 1 FROM jobs WHERE djinni_id = ?", (djinni_id,)).fetchone():
                seen += 1
                continue

            time.sleep(random.uniform(args.job_sleep_min, args.job_sleep_max))
            try:
                job_response = get_page(scraper, job_url, timeout=15)
            except RuntimeError:
                raise  # blocked: do not keep hammering the site
            except Exception as exc:
                print(f"[{djinni_id}] fetch failed: {exc}")
                continue
            if job_response is None:
                continue

            job = parse_job_page(job_response.text)
            if job is None:
                print(f"[{djinni_id}] no usable JSON-LD, skipped")
                continue
            job["djinni_id"] = djinni_id
            job["url"] = job_url

            if args.dry_run:
                print(f"DRY [{djinni_id}] {job['title']} | views {job['views_count']} | applications {job['applications_count']}")
                continue

            with conn:
                conn.execute(
                    """INSERT INTO jobs (
                        djinni_id, title, company, category, salary_min, salary_max, salary_currency,
                        experience_months, description, url, published_at,
                        views_count, applications_count, tags, raw_json_ld
                    ) VALUES (
                        :djinni_id, :title, :company, :category, :salary_min, :salary_max, :salary_currency,
                        :experience_months, :description, :url, :published_at,
                        :views_count, :applications_count, :tags, :raw_json_ld
                    )""",
                    job,
                )
            inserted += 1
            print(f"[{djinni_id}] {job['title']} | views {job['views_count']} | applications {job['applications_count']}")

        page += 1
        time.sleep(random.uniform(args.page_sleep_min, args.page_sleep_max))

    print(f"\nDone. inserted={inserted}, already_in_db={seen}")


def parse_args():
    parser = argparse.ArgumentParser(description="Scrape djinni.co job postings into SQLite.")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=None, help="Limit pages for test runs. Omit for a full scan.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print, do not write to the database.")
    parser.add_argument("--job-sleep-min", type=float, default=2.0)
    parser.add_argument("--job-sleep-max", type=float, default=4.0)
    parser.add_argument("--page-sleep-min", type=float, default=5.0)
    parser.add_argument("--page-sleep-max", type=float, default=8.0)
    return parser.parse_args()


if __name__ == "__main__":
    scrape_jobs(parse_args())
