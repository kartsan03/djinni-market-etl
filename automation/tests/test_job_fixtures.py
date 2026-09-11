import os
import sys
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from reconcile_djinni_jobs import parse_card  # noqa: E402
from scraper_djinni_jobs import collect_tags, parse_detail_response  # noqa: E402


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeResponse:
    def __init__(self, html, status_code=200):
        self.status_code = status_code
        self.text = html
        self.content = html.encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class GoldenFixtureTests(unittest.TestCase):
    def test_listing_card_en_engagement_and_link(self):
        html = (FIXTURES / "job_listing_card.html").read_text(encoding="utf-8")
        card = BeautifulSoup(html, "html.parser").select_one(".job-item")
        record = parse_card(card, page=1, position_on_page=1)
        self.assertEqual(record["djinni_id"], 840095)
        self.assertEqual(record["views_count"], 42)
        self.assertEqual(record["apps_count"], 15)

    def test_listing_card_ua_engagement(self):
        html = (FIXTURES / "job_listing_card_ua.html").read_text(encoding="utf-8")
        card = BeautifulSoup(html, "html.parser").select_one(".job-item")
        record = parse_card(card, page=1, position_on_page=1)
        self.assertEqual(record["views_count"], 128)
        self.assertEqual(record["apps_count"], 7)

    def test_detail_tags_and_json_ld_selectors(self):
        html = (FIXTURES / "job_detail_tags.html").read_text(encoding="utf-8")
        detail = parse_detail_response(FakeResponse(html), "https://djinni.co/jobs/840095-senior-python/")
        self.assertEqual(detail["title"], "Senior Python")
        self.assertEqual(detail["salary_min"], 3000)
        self.assertEqual(detail["salary_max"], 4500)
        self.assertIn("Python", detail["tags"])
        self.assertIn("ETL", detail["tags"])
        self.assertIn("Ukrainian Product", detail["tags"])
        self.assertIsNotNone(detail["valid_through"])

        soup = BeautifulSoup(html, "html.parser")
        tags = collect_tags(soup, soup.get_text(" ", strip=True))
        self.assertEqual(tags[:2], ["Python", "ETL"])


if __name__ == "__main__":
    unittest.main()
