import os
import sys
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from reconcile_djinni_jobs import (  # noqa: E402
    ScanDeadlineExceeded,
    can_record_absence_evidence,
    classify_complete_scan_miss,
    ensure_scan_time,
    parse_card,
    parse_site_total,
)


class ListingParserTests(unittest.TestCase):
    def test_parses_site_total(self):
        soup = BeautifulSoup(
            '<header><h1>Jobs at Djinni</h1><div>7 461</div></header>',
            "html.parser",
        )
        self.assertEqual(parse_site_total(soup), 7461)

    def test_parses_card_without_conflating_missing_stats_with_zero(self):
        soup = BeautifulSoup(
            """
            <div class="job-item">
              <a class="job_item__header-link" href="/jobs/840095-example/">Example</a>
              <span>42 views</span><span>15 applications</span>
            </div>
            """,
            "html.parser",
        )
        record = parse_card(soup.select_one(".job-item"), page=2, position_on_page=3)
        self.assertEqual(record["djinni_id"], 840095)
        self.assertEqual(record["views_count"], 42)
        self.assertEqual(record["apps_count"], 15)
        self.assertEqual(record["global_rank"], 18)

        no_stats = BeautifulSoup(
            '<div class="job-item"><a class="job_item__header-link" '
            'href="/jobs/1-example/">Example</a></div>',
            "html.parser",
        )
        record = parse_card(no_stats.select_one(".job-item"), 1, 1)
        self.assertIsNone(record["views_count"])
        self.assertIsNone(record["apps_count"])


    def test_parses_ua_engagement_counters(self):
        soup = BeautifulSoup(
            """
            <div class="job-item">
              <a class="job_item__header-link" href="/jobs/99-example/">Example</a>
              <span>10 переглядів</span><span>3 відгуків</span>
            </div>
            """,
            "html.parser",
        )
        record = parse_card(soup.select_one(".job-item"), page=1, position_on_page=1)
        self.assertEqual(record["views_count"], 10)
        self.assertEqual(record["apps_count"], 3)


class AbsenceSafetyTests(unittest.TestCase):
    def safe(self, **overrides):
        values = {
            "start_page": 1,
            "max_pages": None,
            "reached_end": True,
            "expected_pages": 498,
            "pages_scraped": 498,
            "errors_count": 0,
            "site_total": 7461,
            "site_total_end": 7461,
            "cards_seen": 7461,
            "unique_jobs_seen": 7461,
            "duplicate_cards": 0,
        }
        values.update(overrides)
        return can_record_absence_evidence(**values)

    def test_deadline_guard_reserves_safe_finalization_time(self):
        ensure_scan_time(None)
        with patch("reconcile_djinni_jobs.time.monotonic", return_value=939):
            ensure_scan_time(1000)
        with patch("reconcile_djinni_jobs.time.monotonic", return_value=940):
            with self.assertRaises(ScanDeadlineExceeded):
                ensure_scan_time(1000)

    def test_requires_structurally_complete_scan(self):
        self.assertTrue(self.safe())
        self.assertTrue(self.safe(unique_jobs_seen=7460, duplicate_cards=1))
        self.assertTrue(self.safe(
            site_total_end=7462,
            cards_seen=7462,
            unique_jobs_seen=7461,
            duplicate_cards=1,
        ))
        self.assertFalse(self.safe(unique_jobs_seen=7460))
        self.assertFalse(self.safe(cards_seen=7460))
        self.assertFalse(self.safe(site_total_end=7462))
        self.assertFalse(self.safe(unique_jobs_seen=7445, duplicate_cards=16))
        self.assertFalse(self.safe(errors_count=1))
        self.assertFalse(self.safe(max_pages=10))


class MissClassificationTests(unittest.TestCase):
    def test_first_complete_miss_stays_unknown(self):
        status, source = classify_complete_scan_miss("active", 0)
        self.assertEqual(status, "unknown")
        self.assertEqual(source, "first_complete_scan_absence")

    def test_second_complete_miss_becomes_inactive_inferred(self):
        status, source = classify_complete_scan_miss("unknown", 1)
        self.assertEqual(status, "inactive_inferred")
        self.assertEqual(source, "two_complete_scan_absences")

    def test_terminal_statuses_are_not_overwritten(self):
        for status in ("inactive_inferred", "offline_confirmed", "deleted_confirmed"):
            next_status, source = classify_complete_scan_miss(status, 5)
            self.assertEqual(next_status, status)
            self.assertIsNone(source)


if __name__ == "__main__":
    unittest.main()
