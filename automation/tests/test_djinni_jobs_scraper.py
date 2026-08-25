import os
import sys
import unittest
from types import SimpleNamespace

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper_djinni_jobs import (  # noqa: E402
    classify_status_response,
    collect_path_values,
    extract_json_ld,
    find_job_posting,
    job_confirmation_budget_available,
    normalize_country_codes,
    parse_english_level,
    parse_salary,
    should_retry_structural_partial,
)


class FakeResponse:
    def __init__(self, html, status_code=200):
        self.status_code = status_code
        self.text = html
        self.content = html.encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class JobDetailParserTests(unittest.TestCase):
    def test_finds_job_posting_inside_graph(self):
        posting = {"@type": "JobPosting", "identifier": 123, "title": "Engineer"}
        self.assertEqual(
            find_job_posting({"@graph": [{"@type": "Organization"}, posting]}),
            posting,
        )

    def test_extracts_json_ld_and_current_base_salary_shape(self):
        soup = BeautifulSoup(
            """
            <script type="application/ld+json">
            {
              "@context": "https://schema.org/",
              "@type": "JobPosting",
              "identifier": 840095,
              "baseSalary": {
                "currency": "USD",
                "value": {
                  "minValue": 1500,
                  "maxValue": 2000,
                  "unitText": "MONTH"
                }
              }
            }
            </script>
            """,
            "html.parser",
        )
        posting = extract_json_ld(soup)
        salary = parse_salary(posting)
        self.assertEqual(salary["salary_min"], 1500)
        self.assertEqual(salary["salary_max"], 2000)
        self.assertEqual(salary["salary_currency"], "USD")
        self.assertEqual(salary["salary_period"], "month")
        self.assertEqual(salary["salary_source"], "baseSalary")

    def test_base_salary_uses_estimated_currency_fallback(self):
        salary = parse_salary({
            "baseSalary": {
                "value": {"minValue": 1000, "maxValue": 1500, "unitText": "MONTH"}
            },
            "estimatedSalary": {"currency": "USD"},
        })
        self.assertEqual(salary["salary_currency"], "USD")
        self.assertEqual(salary["salary_source"], "baseSalary")

    def test_supports_legacy_estimated_salary_shape(self):
        salary = parse_salary({
            "estimatedSalary": {
                "minValue": 2500,
                "maxValue": 4000,
                "currency": "USD",
            }
        })
        self.assertEqual(salary["salary_min"], 2500)
        self.assertEqual(salary["salary_max"], 4000)
        self.assertEqual(salary["salary_source"], "estimatedSalary")

    def test_extracts_english_level(self):
        self.assertEqual(parse_english_level("Required languages English B2 - Upper"), "B2")
        self.assertIsNone(parse_english_level("No language requirement"))

    def test_normalizes_object_and_array_location_shapes(self):
        value = [
            {"address": {"addressLocality": ["Kyiv", "Poltava"]}},
            {"address": {"addressLocality": "Kyiv"}},
        ]
        self.assertEqual(
            collect_path_values(value, "address", "addressLocality"),
            ["Kyiv", "Poltava"],
        )

    def test_normalizes_country_code_aliases_and_nulls(self):
        self.assertEqual(normalize_country_codes(["UA", "UKR", "pol"]), ["UKR", "POL"])
        self.assertIsNone(normalize_country_codes(None))

    def test_structural_partial_retry_is_bounded_and_canonical_only(self):
        args = SimpleNamespace(
            dry_run=False, no_structural_retry=False, start_page=1, max_pages=None
        )
        self.assertTrue(should_retry_structural_partial(2, args, 60 * 60))
        self.assertFalse(should_retry_structural_partial(0, args, 60 * 60))
        self.assertFalse(should_retry_structural_partial(2, args, 76 * 60))
        args.max_pages = 1
        self.assertFalse(should_retry_structural_partial(2, args, 60 * 60))
        args.max_pages = None
        args.no_structural_retry = True
        self.assertFalse(should_retry_structural_partial(2, args, 60 * 60))

    def test_job_confirmation_budget_keeps_a_shutdown_buffer(self):
        self.assertTrue(job_confirmation_budget_available(1000, 900))
        self.assertFalse(job_confirmation_budget_available(1000, 940))
        self.assertFalse(job_confirmation_budget_available(1000, 950))
        self.assertTrue(job_confirmation_budget_available(None, 10_000))

    def test_classifies_active_offline_and_deleted_details(self):
        active = FakeResponse(
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","identifier":1}'
            '</script><button>Apply for the job</button>'
        )
        offline = FakeResponse('<h1>Example Offline</h1><p>The job ad is no longer active</p>')
        deleted = FakeResponse("", status_code=404)

        self.assertEqual(classify_status_response(active), ("active", "detail_confirmation"))
        self.assertEqual(classify_status_response(offline), ("offline_confirmed", "detail_offline"))
        self.assertEqual(classify_status_response(deleted), ("deleted_confirmed", "detail_404"))


if __name__ == "__main__":
    unittest.main()
