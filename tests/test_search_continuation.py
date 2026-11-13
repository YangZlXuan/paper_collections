"""Batch boundaries, source offsets and resuming without publisher requests."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import requests
from paper_downloader import search
import test_search as fixtures


class SearchContinuationTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("paper_downloader.request_quota.settings_path", return_value=folder / "settings.json"))
        self.enterContext(patch.object(search, "_wait", side_effect=lambda event, seconds: search._check_cancel(event)))
        self.config = search.SearchConfig("elsevier", "zinc batteries", api_key="synthetic-key", retries=0, max_results=1000)

    def run_fake(self, replies, **kwargs):
        session = fixtures.FakeSession(replies)
        with patch.object(search.requests, "Session", return_value=session):
            result = search.search_articles(self.config, **kwargs)
        return result, session

    @staticmethod
    def rows(start, count):
        return [fixtures.erow(doi=f"10.1016/article-{index}", pii="") for index in range(start, start + count)]

    def test_first_thousand_then_remaining_507_are_disjoint_and_complete(self):
        total = 1507
        first, _ = self.run_fake([fixtures.elsevier(self.rows(start, 25), total) for start in range(0, 1000, 25)])
        self.assertEqual(len(first.records), 1000)
        self.assertEqual(first.next_cursor, 1000)
        self.config.start_offset = first.next_cursor
        second, session = self.run_fake([fixtures.elsevier(self.rows(start, min(25, total - start)), total)
                                        for start in range(1000, total, 25)])
        self.assertEqual(len(second.records), 507)
        self.assertIsNone(second.next_cursor)
        self.assertFalse(second.truncated)
        self.assertFalse({record.doi for record in first.records} & {record.doi for record in second.records})
        self.assertEqual(json.loads(session.calls[0][0].body)["display"]["offset"], 1000)

    def test_batch_ending_mid_page_resumes_at_next_unprocessed_row(self):
        self.config.max_results = 3
        first, _ = self.run_fake([fixtures.elsevier(self.rows(0, 25), 25)])
        self.assertEqual(first.next_cursor, 3)
        self.config.start_offset = first.next_cursor
        second, _ = self.run_fake([fixtures.elsevier(self.rows(3, 22), 25)])
        self.assertEqual([record.doi for record in second.records], [f"10.1016/article-{index}" for index in range(3, 6)])
        self.assertEqual(second.next_cursor, 6)

    def test_filtered_and_duplicate_rows_count_toward_source_position(self):
        self.config.max_results = 2
        rows = [{"title": "missing identifier"}, *self.rows(0, 1), *self.rows(0, 1), *self.rows(1, 2)]
        result, _ = self.run_fake([fixtures.elsevier(rows, 8)])
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.next_cursor, 4)

    def test_cancel_mid_page_preserves_first_unprocessed_position(self):
        event = threading.Event()
        original = search._reference
        processed = []
        def reference(row, config, result):
            item = original(row, config, result)
            processed.append(item)
            if len(processed) == 2:
                event.set()
            return item
        with patch.object(search, "_reference", side_effect=reference):
            result, _ = self.run_fake([fixtures.elsevier(self.rows(0, 25), 100)], cancel_event=event)
        self.assertTrue(result.cancelled)
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.next_cursor, 2)

    def test_partial_network_failure_can_retry_the_failed_page(self):
        result, _ = self.run_fake([fixtures.elsevier(self.rows(0, 25), 60), requests.Timeout()])
        self.assertEqual(len(result.records), 25)
        self.assertEqual(result.next_cursor, 25)
        self.assertTrue(result.truncated)

    def test_page_budget_returns_a_continuation_position(self):
        with patch.object(search, "_MAX_PAGES", 1):
            result, _ = self.run_fake([fixtures.elsevier(self.rows(0, 25), 60)])
        self.assertEqual(result.next_cursor, 25)
        self.assertTrue(result.truncated)

    def test_exhausted_results_and_absolute_boundary_disable_continuation(self):
        self.config.start_offset = 2000
        result, _ = self.run_fake([fixtures.elsevier([], 1507)])
        self.assertIsNone(result.next_cursor)
        self.config.start_offset = 5999
        self.config.max_results = 1
        result, _ = self.run_fake([fixtures.elsevier(self.rows(5999, 1), 7000)])
        self.assertIsNone(result.next_cursor)
        self.assertTrue(result.truncated)

    def test_invalid_offsets_are_rejected_before_network(self):
        for value in (-1, 6000, True, 1.5, "1000"):
            with self.subTest(value=value), patch.object(search.requests, "Session") as factory:
                self.config.start_offset = value
                with self.assertRaises(search.SearchError): search.search_articles(self.config)
                factory.assert_not_called()

    def test_one_based_publisher_receives_offset_plus_one(self):
        self.config.publisher_id = "springer"
        self.config.start_offset = 1000
        result, session = self.run_fake([fixtures.springer([fixtures.srow()], total=1001, start=1001)])
        from urllib.parse import parse_qs, urlsplit
        self.assertEqual(parse_qs(urlsplit(session.calls[0][0].url).query)["s"], ["1001"])
        self.assertIsNone(result.next_cursor)


if __name__ == "__main__":
    unittest.main()
