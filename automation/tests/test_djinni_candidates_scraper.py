import sys
import unittest
from unittest.mock import patch
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bs4 import BeautifulSoup

from scraper_djinni_candidates import (
    DETAIL_BUDGET_SECONDS,
    AmbiguousDetailError,
    BlockedResponseError,
    canonical_listing_scope,
    classify_candidate_detail_response,
    clean_count,
    confirmation_budget_was_exhausted,
    confirmation_capacity,
    listing_parser_drift_error,
    parse_listing_card,
    parse_args,
    parse_profile_detail,
    parse_published_date,
)


class FakeResponse:
    def __init__(self, status_code, html):
        self.status_code = status_code
        self.text = html
        self.content = html.encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class CandidateParserTests(unittest.TestCase):
    def test_confirmation_only_capacity_defaults(self):
        with patch("sys.argv", ["scraper_djinni_candidates.py"]):
            args = parse_args()
        self.assertEqual(args.confirmation_limit, 1000)
        self.assertEqual(args.deadline_minutes, 330.0)
        self.assertEqual((args.detail_sleep_min, args.detail_sleep_max), (7.0, 14.0))

    def test_missing_view_count_stays_unknown(self):
        self.assertIsNone(clean_count(None))
        self.assertIsNone(clean_count("not available"))
        self.assertEqual(clean_count("0 views"), 0)
        self.assertEqual(clean_count("1,234 views"), 1234)

    def test_relative_published_dates(self):
        today = date(2026, 7, 31)
        self.assertEqual(parse_published_date("Published today", today), today)
        self.assertEqual(parse_published_date("Published yesterday", today), date(2026, 7, 30))
        self.assertEqual(parse_published_date("Published 29 July 2026", today), date(2026, 7, 29))

    def test_current_listing_icon_shape(self):
        soup = BeautifulSoup("""
        <div class="card mb-4">
          <a class="profile" href="/q/example123/">Data Engineer</a>
          <div class="fs-2 text-success">$2500 / mo</div>
          <div class="d-flex align-items-center flex-wrap column-gap-075">
            <span><svg><use href="#code-square"></use></svg>Data Engineer</span>
            <span><svg><use href="#lightning-charge"></use></svg>4 years of experience</span>
            <span><svg><use href="#translate"></use></svg>B2 - Upper Intermediate</span>
            <span><svg><use href="#clock"></use></svg>Published today</span>
          </div>
          <div class="d-flex align-items-center flex-wrap column-gap-075">
            <span><svg><use href="#geo-alt"></use></svg>Ukraine, Kyiv</span>
            <span><svg><use href="#laptop"></use></svg>Only remote</span>
          </div>
          <div class="card-footer"><span title="5 views">5</span></div>
        </div>
        """, "html.parser")
        record = parse_listing_card(soup.select_one(".card"))
        self.assertEqual(record["djinni_key"], "example123")
        self.assertEqual(record["category"], "Data Engineer")
        self.assertEqual(record["experience_months"], 48)
        self.assertEqual(record["country"], "Ukraine")
        self.assertEqual(record["city"], "Kyiv")
        self.assertEqual(record["work_format"], "Only remote")
        self.assertEqual(record["views_count"], 5)
        self.assertEqual(record["published_at"], date.today())

    def test_listing_parser_drift_guard(self):
        healthy = [{"published_at": date.today(), "category": "Data"} for _ in range(10)]
        no_dates = [{"published_at": None, "category": "Data"} for _ in range(10)]
        no_categories = [{"published_at": date.today(), "category": None} for _ in range(10)]
        self.assertIsNone(listing_parser_drift_error(healthy))
        self.assertIn("published_at", listing_parser_drift_error(no_dates))
        self.assertIn("category", listing_parser_drift_error(no_categories))
        self.assertIsNone(listing_parser_drift_error(no_dates[:4]))

    def test_detail_parser_extracts_absolute_published_date(self):
        detail = parse_profile_detail("""
        <html><h1>Data Engineer</h1><aside>
          <div class="card-header"><h3>$3000 / mo</h3></div>
          <ul><li><svg><use href="#clock"></use></svg>Published 1 August 2026</li></ul>
        </aside></html>
        """, "https://djinni.co/q/test/")
        self.assertEqual(detail["published_at"], date(2026, 8, 1))

    def test_confirmation_capacity_reserves_deadline_buffer(self):
        now = datetime(2026, 8, 9, 12, 0, 0)
        self.assertEqual(DETAIL_BUDGET_SECONDS, 14)
        self.assertEqual(
            confirmation_capacity(now + timedelta(minutes=61), 1000, now),
            257,
        )
        self.assertEqual(
            confirmation_capacity(now + timedelta(minutes=330), 1000, now),
            1000,
        )
        self.assertEqual(
            confirmation_capacity(now + timedelta(seconds=30), 1000, now),
            0,
        )

    def test_confirmation_budget_does_not_hide_blocks(self):
        self.assertTrue(confirmation_budget_was_exhausted(
            due_after=10, capacity=100, requested_limit=500,
            selected=100, reason="detail_internal_deadline",
        ))
        self.assertTrue(confirmation_budget_was_exhausted(
            due_after=10, capacity=500, requested_limit=500,
            selected=500, reason="detail_batch_complete",
        ))
        self.assertFalse(confirmation_budget_was_exhausted(
            due_after=10, capacity=500, requested_limit=500,
            selected=500, reason="detail_blocked",
        ))

    def test_detail_status_classifier(self):
        active_html = """
        <html>
          <nav><span class="breadcrumb-item"><a href="/developers/">Candidates</a></span></nav>
          <h1>Data Engineer</h1><aside><ul></ul></aside>
          <section>I previously worked offline-first systems and offline applications.</section>
        </html>
        """
        status, source, detail = classify_candidate_detail_response(
            FakeResponse(200, active_html), "https://djinni.co/q/test/"
        )
        self.assertEqual((status, source), ("active", "detail_confirmation"))
        self.assertEqual(detail["title"], "Data Engineer")

        status, source, detail = classify_candidate_detail_response(
            FakeResponse(
                200,
                '<html><h1>Candidate <span class="badge">Offline</span></h1>'
                '<strong class="text-danger">Candidate is no longer active</strong></html>',
            ),
            "https://djinni.co/q/test/",
        )
        self.assertEqual((status, source, detail), ("offline_confirmed", "detail_offline", None))

        status, source, detail = classify_candidate_detail_response(
            FakeResponse(404, "not found"), "https://djinni.co/q/test/"
        )
        self.assertEqual((status, source, detail), ("deleted_confirmed", "detail_404", None))

        with self.assertRaises(AmbiguousDetailError):
            classify_candidate_detail_response(
                FakeResponse(200, "<html><h1>Generic page</h1><aside></aside></html>"),
                "https://djinni.co/q/test/",
            )
        with self.assertRaises(BlockedResponseError):
            classify_candidate_detail_response(
                FakeResponse(200, "<html>cf-chl-token</html>"),
                "https://djinni.co/q/test/",
            )

    def test_only_unfiltered_date_scope_advances_frontier(self):
        base = dict(start_page=1, sortby="date", min_views=0, min_salary=0, min_exp_months=0)
        self.assertTrue(canonical_listing_scope(SimpleNamespace(**base)))
        self.assertFalse(canonical_listing_scope(SimpleNamespace(**{**base, "min_views": 5})))
        self.assertFalse(canonical_listing_scope(SimpleNamespace(**{**base, "sortby": "experience"})))


if __name__ == "__main__":
    unittest.main()
