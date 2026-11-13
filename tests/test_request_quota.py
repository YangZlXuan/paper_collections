"""Request boundaries, persistent budgets and transport enforcement without network."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests

from paper_downloader import elsevier, request_quota as quota, search
from paper_downloader.doi_parser import ArticleReference
from test_elsevier import FakeSession as ElsevierSession, ENDPOINT, META_URL, PDF, REFERENCE, metadata, response
from test_search import FakeSession as SearchSession, springer, srow


META = "https://api.springernature.com/meta/v2/json"
OA = "https://api.springernature.com/openaccess/jats"
ARTICLE = "https://api.elsevier.com/content/article/doi/10.1016/example"


class RequestQuotaTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.folder = Path(directory)
        self.database = self.folder / "request-quota.sqlite3"
        self.enterContext(patch.object(quota, "settings_path", return_value=self.folder / "settings.json"))
        # 23:59:59 in Beijing, independent of the test machine's timezone.
        self.now = datetime(2026, 11, 11, 15, 59, 59, tzinfo=timezone.utc).timestamp()
        self.enterContext(patch.object(quota.time, "time", side_effect=lambda: self.now))
        self.enterContext(patch.object(search, "_wait"))
        self.enterContext(patch.object(elsevier, "_wait"))

    def seed(self, publisher, count, expires_at):
        quota.reserve_api_request(META if publisher == "springer" else ARTICLE)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM api_requests WHERE publisher = ?", (publisher,))
            connection.executemany("INSERT INTO api_requests (publisher, expires_at) VALUES (?, ?)",
                                   [(publisher, expires_at)] * count)

    def used(self, publisher):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT COUNT(*) FROM api_requests WHERE publisher = ?", (publisher,)).fetchone()[0]

    def test_springer_meta_and_open_access_share_exact_400_request_limit(self):
        self.seed("springer", 399, self.now + 1)
        quota.reserve_api_request(OA)
        for url in (META, OA):
            with self.subTest(url=url), self.assertRaisesRegex(quota.RequestQuotaError, "400 次"):
                quota.reserve_api_request(url)
        self.assertEqual(self.used("springer"), 400)

    def test_springer_resets_at_beijing_midnight_and_reports_resume_time(self):
        self.seed("springer", 400, self.now + 1)
        with self.assertRaisesRegex(quota.RequestQuotaError, "2026-11-12 00:00:00（北京时间）"):
            quota.reserve_api_request(META)
        self.now += 1
        quota.reserve_api_request(META)
        self.assertEqual(self.used("springer"), 1)

    def test_elsevier_rolling_window_releases_only_expired_requests(self):
        self.seed("elsevier", 39_999, self.now + 5)
        quota.reserve_api_request(ARTICLE)
        with self.assertRaisesRegex(quota.RequestQuotaError, "40,000 次"):
            quota.reserve_api_request(ARTICLE)
        self.assertEqual(self.used("elsevier"), 40_000)
        self.now += 5
        quota.reserve_api_request(ARTICLE)
        self.assertEqual(self.used("elsevier"), 2)
        self.now += 7 * 24 * 60 * 60
        quota.reserve_api_request(ARTICLE)
        self.assertEqual(self.used("elsevier"), 1)

    def test_requests_persist_across_clients_without_storing_keys_or_urls(self):
        for url in (META + "?api_key=synthetic-private-key", OA):
            quota.reserve_api_request(url)
        self.assertEqual(self.used("springer"), 2)
        content = self.database.read_bytes()
        self.assertNotIn(b"synthetic-private-key", content)
        self.assertNotIn(b"api.springernature.com", content)

    def test_unrelated_pages_cdn_and_wiley_do_not_create_budget(self):
        for url in ("https://link.springer.com/content/pdf/example.pdf",
                    "https://api.springernature.com/xmldata/jats",
                    "https://ars.els-cdn.com/content/image/example.pdf",
                    "https://api.wiley.com/onlinelibrary/tdm/v1/articles/example"):
            quota.reserve_api_request(url)
        self.assertFalse(self.database.exists())

    def test_status_without_history_reports_full_budget_without_creating_database(self):
        status = quota.get_request_quotas()
        self.assertEqual((status["springer"].used, status["springer"].remaining), (0, 400))
        self.assertEqual(status["springer"].next_recovery.isoformat(), "2026-11-12T00:00:00+08:00")
        self.assertEqual((status["elsevier"].used, status["elsevier"].remaining), (0, 40_000))
        self.assertIsNone(status["elsevier"].next_recovery)
        self.assertIsNone(status["elsevier"].full_recovery)
        self.assertFalse(self.database.exists())

    def test_status_shares_enforced_counts_and_does_not_modify_records(self):
        self.seed("springer", 399, self.now + 1)
        self.seed("elsevier", 39_999, self.now + 5)
        quota.reserve_api_request(OA)
        quota.reserve_api_request(ARTICLE)
        before = self.database.read_bytes()
        status = quota.get_request_quotas()
        self.assertEqual((status["springer"].used, status["springer"].remaining), (400, 0))
        self.assertEqual((status["elsevier"].used, status["elsevier"].remaining), (40_000, 0))
        self.assertEqual(self.database.read_bytes(), before)
        with self.assertRaises(quota.RequestQuotaError):
            quota.reserve_api_request(ARTICLE)

    def test_status_excludes_expired_requests_and_reports_next_and_full_recovery(self):
        quota.reserve_api_request(ARTICLE)
        first = self.now + 7 * 24 * 60 * 60
        self.now += 60
        quota.reserve_api_request(ARTICLE)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO api_requests (publisher, expires_at) VALUES (?, ?)", ("elsevier", self.now - 1))
        status = quota.get_request_quotas()["elsevier"]
        self.assertEqual((status.used, status.remaining), (2, 39_998))
        self.assertEqual(status.next_recovery.timestamp(), first)
        self.assertEqual(status.full_recovery.timestamp(), first + 60)
        self.assertEqual(self.used("elsevier"), 3)
        self.now = first
        status = quota.get_request_quotas()["elsevier"]
        self.assertEqual((status.used, status.remaining), (1, 39_999))
        self.assertEqual(status.next_recovery.timestamp(), first + 60)
        self.now += 60
        status = quota.get_request_quotas()["elsevier"]
        self.assertEqual((status.used, status.remaining), (0, 40_000))
        self.assertIsNone(status.next_recovery)

    def test_status_releases_springer_budget_at_midnight_without_new_requests(self):
        self.seed("springer", 400, self.now + 1)
        self.now += 1
        status = quota.get_request_quotas()["springer"]
        self.assertEqual((status.used, status.remaining), (0, 400))
        self.assertEqual(status.next_recovery.isoformat(), "2026-11-13T00:00:00+08:00")
        self.assertEqual(self.used("springer"), 400)

    def test_status_reports_corrupt_and_locked_store_as_errors(self):
        quota.reserve_api_request(META)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("BEGIN EXCLUSIVE")
            with self.assertRaisesRegex(quota.RequestQuotaError, "无法读取本地 API 请求额度"):
                quota.get_request_quotas()
            connection.rollback()
        self.database.write_bytes(b"corrupt quota database")
        with self.assertRaisesRegex(quota.RequestQuotaError, "无法读取本地 API 请求额度"):
            quota.get_request_quotas()

    def test_concurrent_connections_cannot_both_take_last_slot(self):
        self.seed("springer", 399, self.now + 1)
        barrier = threading.Barrier(8)

        def reserve():
            barrier.wait()
            try:
                quota.reserve_api_request(META)
                return True
            except quota.RequestQuotaError:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            allowed = list(pool.map(lambda _: reserve(), range(8)))
        self.assertEqual(sum(allowed), 1)
        self.assertEqual(self.used("springer"), 400)

    def test_corrupt_or_unwritable_store_blocks_requests_instead_of_resetting(self):
        self.database.write_bytes(b"corrupt quota database")
        with self.assertRaisesRegex(quota.RequestQuotaError, "无法记录本地 API 请求额度"):
            quota.reserve_api_request(META)
        self.assertEqual(self.database.read_bytes(), b"corrupt quota database")
        with patch.object(quota.sqlite3, "connect", side_effect=sqlite3.OperationalError("read only")):
            with self.assertRaisesRegex(quota.RequestQuotaError, "read only"):
                quota.reserve_api_request(ARTICLE)

    def test_search_stops_before_network_and_preserves_partial_results(self):
        self.seed("springer", 399, self.now + 1)
        config = search.SearchConfig("springer", "example", api_key="synthetic-key", retries=0)
        session = SearchSession([springer([srow()], total=2)])
        with patch.object(search.requests, "Session", return_value=session):
            result = search.search_articles(config)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(result.records), 1)
        self.assertTrue(result.truncated)
        self.assertIn("400 次", " ".join(result.warnings))
        blocked = SearchSession([])
        with patch.object(search.requests, "Session", return_value=blocked):
            with self.assertRaisesRegex(search.SearchError, "400 次"):
                search.search_articles_page(config)
        self.assertFalse(blocked.calls)

    def test_search_retries_and_network_failures_each_consume_a_request(self):
        config = search.SearchConfig("springer", "example", api_key="synthetic-key", retries=1)
        for first in (requests.Timeout("unconfirmed delivery"), springer([], status=503)):
            session = SearchSession([first, springer([])])
            with patch.object(search.requests, "Session", return_value=session):
                search.search_articles(config)
            self.assertEqual(len(session.calls), 2)
        self.assertEqual(self.used("springer"), 4)

    def test_elsevier_search_and_download_share_budget_but_cdn_can_finish(self):
        self.seed("elsevier", 39_999, self.now + 100)
        config = search.SearchConfig("elsevier", "example", api_key="synthetic-key", retries=0)
        session = SearchSession([response(b'{"resultsFound":0,"results":[]}', content_type="application/json")])
        with patch.object(search.requests, "Session", return_value=session):
            search.search_articles_page(config)
        config = elsevier.ElsevierConfig("", self.folder, api_key="synthetic-key", retries=0)
        session = ElsevierSession({"https://ars.els-cdn.com/content/image/example.pdf": [response(PDF)]})
        client = elsevier._Client(session, config, threading.Event())
        with self.assertRaisesRegex(elsevier._BatchStopped, "40,000 次"):
            client.request(ARTICLE)
        self.assertFalse(session.calls)
        client.request("https://ars.els-cdn.com/content/image/example.pdf", pdf=True).close()
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(self.used("elsevier"), 40_000)

    def test_elsevier_attachment_budget_stop_preserves_pdf_and_stops_batch(self):
        self.seed("elsevier", 39_998, self.now + 100)
        config = elsevier.ElsevierConfig("", self.folder, api_key="synthetic-key", retries=0)
        session = ElsevierSession({META_URL: [metadata()], ENDPOINT: [response(PDF)]})
        with patch.object(elsevier.requests, "Session", return_value=session):
            result = elsevier.run_elsevier_batch([REFERENCE, ArticleReference(doi="10.1016/next")], config)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(len(result.results), 1)
        self.assertIn("40,000 次", result.stop_reason)
        self.assertEqual(result.results[0].status, "partial")
        self.assertTrue(elsevier.is_valid_pdf(Path(result.results[0].file_path)))

    def test_elsevier_retries_and_api_redirects_count_but_cdn_does_not(self):
        object_url = "https://api.elsevier.com/content/object/pii/S1234567890123456"
        cdn_url = "https://ars.els-cdn.com/content/image/example.pdf"
        config = elsevier.ElsevierConfig("", self.folder, api_key="synthetic-key", retries=1)
        session = ElsevierSession({
            ARTICLE: [response(status=503), response(status=302, headers={"Location": object_url})],
            object_url: [response(status=302, headers={"Location": cdn_url})],
            cdn_url: [response(PDF)],
        })
        client = elsevier._Client(session, config, threading.Event())
        client.request(ARTICLE, pdf=True).close()
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(self.used("elsevier"), 3)


if __name__ == "__main__":
    unittest.main()
