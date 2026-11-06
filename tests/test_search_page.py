"""Stateless publisher paging for project-specific DOI exclusion by a remote caller."""
import threading
import unittest
from unittest.mock import patch

from paper_downloader import search


class PageTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("requests.Session.send", side_effect=AssertionError("Unexpected network")))
        self.enterContext(patch.object(search, "_wait"))

    def config(self, publisher="elsevier"):
        return search.SearchConfig(publisher_id=publisher, keywords="acidic OER", api_key="SYNTHETIC")

    def test_cursor_is_zero_based_for_all_publishers(self):
        row = {"doi": "10.1016/example", "title": "acidic OER", "journals": ["Journal"]}
        for publisher, start in (("elsevier", 25), ("springer", 26), ("wiley", 26)):
            with self.subTest(publisher=publisher), patch.object(search, f"_{publisher}_page", return_value=([row], 100, start + 1)) as page:
                result = search.search_articles_page(self.config(publisher), 25, 25)
                self.assertEqual(page.call_args.args[1], start)
                self.assertEqual(result.next_cursor, 26)
                self.assertEqual(result.scanned_count, 1)
                self.assertEqual(result.records[0].doi, row["doi"])

    def test_empty_and_nonadvancing_pages_stop(self):
        for rows, next_page in (([], 30), ([{"doi": "10.1016/example"}], 25)):
            with patch.object(search, "_elsevier_page", return_value=(rows, 100, next_page)):
                result = search.search_articles_page(self.config(), 25)
            self.assertTrue(result.truncated)
            self.assertIsNone(result.next_cursor)
            self.assertTrue(result.warnings)

    def test_duplicate_and_missing_identifiers_do_not_fill_candidates(self):
        rows = [{"doi": "10.1016/example"}, {"doi": "https://doi.org/10.1016/example"}, {"title": "missing DOI"}]
        with patch.object(search, "_elsevier_page", return_value=(rows, 3, None)):
            result = search.search_articles_page(self.config())
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.scanned_count, 3)
        self.assertIsNone(result.next_cursor)

    def test_invalid_input_and_pre_cancelled_request_do_not_fetch(self):
        with patch.object(search, "_elsevier_page") as fetch:
            for cursor, size in ((-1, 25), (6000, 25), (True, 25), (0, 0), (0, 26)):
                with self.assertRaises(search.SearchError): search.search_articles_page(self.config(), cursor, size)
            event = threading.Event(); event.set()
            result = search.search_articles_page(self.config(), cancel_event=event)
            self.assertTrue(result.cancelled)
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
