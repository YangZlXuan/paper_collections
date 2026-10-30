"""Publisher search transport, filters and paging without network credentials."""
from __future__ import annotations

import io
import json
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import requests

from paper_downloader import search as search


TOKEN = "synthetic-search-token"
INSTTOKEN = "synthetic-institution-token"
PII_A = "S1234567890123456"
PII_B = "S1234567890123457"


def response(body=b"", status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result.headers.update({"Content-Type": "application/json"})
    result.headers.update(headers or {})
    result.raw = io.BytesIO(body)
    result.encoding = "utf-8"
    return result


def springer(rows, total=None, start=1, **kwargs):
    data = {"result": [{"total": str(len(rows) if total is None else total), "start": str(start)}], "records": rows}
    return response(json.dumps(data).encode(), **kwargs)


def elsevier(rows, total=None, **kwargs):
    return response(json.dumps({"resultsFound": len(rows) if total is None else total, "results": rows}).encode(), **kwargs)


def srow(doi="10.1007/article-a", journal="Nano Research", date="2024-03-01", title="Research"):
    return {"doi": doi, "publicationName": journal, "publicationDate": date, "title": title}


def erow(doi="10.1016/article-a", pii=PII_A, journal="Nano Today", date="2024-03-01", title="Research"):
    return {"doi": doi, "pii": pii, "sourceTitle": journal, "publicationDate": date, "title": title}


def wiley(rows="", total=0, next_position=None, **kwargs):
    next_tag = "" if next_position is None else f"<sru:nextRecordPosition>{next_position}</sru:nextRecordPosition>"
    xml = f'''<sru:searchRetrieveResponse xmlns:sru="http://docs.oasis-open.org/ns/search-ws/sruResponse"
        xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"
        xmlns:prism="http://prismstandard.org/namespaces/basic/2.1/">
        <sru:numberOfRecords>{total}</sru:numberOfRecords><sru:records>{rows}</sru:records>{next_tag}
        </sru:searchRetrieveResponse>'''
    return response(xml.encode(), headers={"Content-Type": "application/xml"}, **kwargs)


def wrow(doi="10.1002/article-a", journal="Advanced Materials", date="2024-05-01", extra=""):
    return f'''<sru:record><sru:recordSchema>pam</sru:recordSchema><sru:recordPacking>xml</sru:recordPacking>
        <sru:recordData><dc:dc><dc:identifier>{doi}</dc:identifier>
        <dc:title>Water &amp; Energy</dc:title><dcterms:isPartOf>{journal}</dcterms:isPartOf>
        <dc:date>{date}</dc:date><prism:url>https://untrusted.example/paper</prism:url>{extra}</dc:dc>
        </sru:recordData></sru:record>'''


class FakeSession(requests.Session):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.calls, self.send_times = [], []

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        self.send_times.append(search.time.monotonic())
        if not self.responses:
            raise AssertionError("Unexpected additional search request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        item = item() if callable(item) else item
        item.url, item.request = request.url, request
        return item


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.config = search.SearchConfig("springer", "oxygen evolution", api_key=TOKEN, retries=0)
        self.now, self.waits = 0.0, []
        self.enterContext(patch.object(search.time, "monotonic", side_effect=lambda: self.now))
        self.enterContext(patch.object(search, "_wait", side_effect=self.virtual_wait))

    def virtual_wait(self, event, seconds):
        search._check_cancel(event)
        seconds = max(0.0, seconds)
        self.now += seconds
        self.waits.append(seconds)

    def run_fake(self, responses=(), session=None, **kwargs):
        session = session or FakeSession(responses)
        with patch.object(search.requests, "Session", return_value=session):
            result = search.search_articles(self.config, **kwargs)
        return result, session

    def test_config_validation_precedes_network_and_repr_hides_credentials(self):
        self.config.insttoken = INSTTOKEN
        self.assertNotIn(TOKEN, repr(self.config))
        self.assertNotIn(INSTTOKEN, repr(self.config))
        invalid = [("publisher_id", "acs"), ("keywords", ""), ("keywords", "a\nb"),
                   ("keywords", "x" * 251), ("journal", None), ("journal", "a\tb"),
                   ("year_from", "2024"), ("year_from", True), ("year_from", 999),
                   ("max_results", True), ("max_results", 1001), ("interval", 1.99),
                   ("interval", float("nan")), ("interval", "2"), ("interval", 2j), ("timeout", float("inf")),
                   ("retries", 6), ("api_key", ""), ("api_key", "not\nvalid")]
        for name, value in invalid:
            original = getattr(self.config, name)
            with self.subTest(field=name, value=value), patch.object(search.requests, "Session") as factory:
                setattr(self.config, name, value)
                with self.assertRaises(search.SearchError):
                    search.search_articles(self.config)
                factory.assert_not_called()
            setattr(self.config, name, original)
        self.config.year_from, self.config.year_to = 2025, 2024
        with self.assertRaises(search.SearchError):
            search.validate_search_config(self.config)

    def test_springer_basic_query_identity_and_api_key_scope(self):
        self.config.journal = "Nano Research"
        self.config.year_from, self.config.year_to = 2023, 2025
        result, session = self.run_fake([springer([srow(title="H<sub>2</sub> &amp; energy")])])
        self.assertEqual(result.source, "Springer Meta API")
        self.assertEqual(result.records[0].title, "H2 & energy")
        self.assertEqual(result.records[0].url, "https://doi.org/10.1007/article-a")
        self.assertEqual(result.records[0].journal, "Nano Research")
        request, kwargs = session.calls[0]
        query = parse_qs(urlsplit(request.url).query)
        self.assertEqual(query["q"], ["oxygen evolution type:Journal datefrom:2023-01-01 dateto:2025-12-31"])
        self.assertEqual(query["api_key"], [TOKEN])
        self.assertEqual(query["s"], ["1"])
        self.assertEqual(query["p"], ["20"])
        self.assertEqual(request.method, "GET")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertFalse(any("key" in name.lower() for name in request.headers))
        self.assertTrue(any("本地精确" in warning for warning in result.warnings))

    def test_springer_pagination_dedup_and_rate_limit(self):
        result, session = self.run_fake([
            springer([srow(), srow("10.1007/article-b")], total=4),
            springer([srow(), srow("10.1007/article-c")], total=4, start=3),
        ])
        self.assertEqual(len(result.records), 3)
        self.assertEqual(result.total, 4)
        self.assertFalse(result.truncated)
        self.assertEqual(session.send_times, [0, 2])
        self.assertEqual(parse_qs(urlsplit(session.calls[1][0].url).query)["s"], ["3"])

    def test_exact_journal_and_year_filters_and_missing_fields(self):
        self.config.journal = "Nano Research"
        self.config.year_from, self.config.year_to = 2023, 2025
        rows = [srow("10.1007/good", "NANO-Research", "2023-01-01"),
                srow("10.1007/substring", "Nano Research Energy"),
                srow("10.1007/no-journal", ""), srow("10.1007/no-year", date=""),
                srow("10.1007/early", date="2022-01-01"), srow("10.1007/late", date="2026"),
                srow("10.1007/end", date="2025"), srow("bad")]
        result, _ = self.run_fake([springer(rows)])
        self.assertEqual([row.doi for row in result.records], ["10.1007/good", "10.1007/end"])
        self.assertTrue(any("期刊名称" in text for text in result.warnings))
        self.assertTrue(any("发表年份" in text for text in result.warnings))
        self.assertTrue(any("有效 DOI/PII" in text for text in result.warnings))

    def test_result_limit_marks_truncation_without_requesting_next_page(self):
        self.config.max_results = 1
        result, session = self.run_fake([springer([srow(), srow("10.1007/article-b")], total=50)])
        self.assertEqual(len(result.records), 1)
        self.assertTrue(result.truncated)
        self.assertEqual(len(session.calls), 1)
        result, _ = self.run_fake([springer([srow()])])
        self.assertFalse(result.truncated)

    def test_empty_repeated_and_page_position_mismatch_are_not_complete(self):
        cases = [
            [springer([srow()], total=5), springer([], total=5, start=2)],
            [springer([srow()], total=5), springer([srow()], total=5, start=2)],
            [springer([srow()], total=5), springer([srow("10.1007/other")], total=5, start=1)],
        ]
        for responses in cases:
            with self.subTest(case=len(responses)):
                result, session = self.run_fake(responses)
                self.assertEqual(len(result.records), 1)
                self.assertTrue(result.truncated)
                self.assertTrue(result.warnings)
                self.assertEqual(len(session.calls), 2)

    def test_malformed_rows_are_skipped_without_stopping_pagination(self):
        result, session = self.run_fake([
            springer([None, "invalid"], total=3), springer([srow()], total=3, start=3)])
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(session.calls), 2)
        self.assertFalse(result.truncated)

    def test_page_safety_bound_is_explicit(self):
        with patch.object(search, "_MAX_PAGES", 1):
            result, session = self.run_fake([springer([srow()], total=100)])
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(result.truncated)
        self.assertTrue(any("分页上限" in text for text in result.warnings))

    def test_elsevier_native_put_search_fields_headers_and_paging(self):
        self.config = search.SearchConfig("elsevier", "oxygen evolution", journal="Nano Today",
            year_from=2020, year_to=2025, api_key=TOKEN, insttoken=INSTTOKEN, retries=0)
        result, session = self.run_fake([
            elsevier([erow(title="H<ce:inf loc=\"post\">2</ce:inf>O")], total=2),
            elsevier([erow("10.1016/article-b", PII_B)], total=2)])
        self.assertEqual([row.pii for row in result.records], [PII_A, PII_B])
        self.assertEqual(result.records[0].title, "H2O")
        self.assertEqual(result.source, "ScienceDirect Search API v2")
        self.assertEqual(session.send_times, [0, 2])
        for index, (request, _) in enumerate(session.calls):
            self.assertEqual(request.url, search.ELSEVIER_ENDPOINT)
            self.assertEqual(request.method, "PUT")
            payload = json.loads(request.body)
            self.assertEqual(payload["qs"], "oxygen evolution")
            self.assertEqual(payload["date"], "2020-2025")
            self.assertEqual(payload["pub"], "Nano Today")
            self.assertEqual(payload["display"], {"offset": index, "show": 25, "sortBy": "relevance"})
            self.assertEqual(request.headers["X-ELS-APIKey"], TOKEN)
            self.assertEqual(request.headers["X-ELS-Insttoken"], INSTTOKEN)

    def test_elsevier_pii_only_merge_and_identifier_conflicts(self):
        self.config.publisher_id = "elsevier"
        rows = [erow("", PII_A), erow("10.1016/article-a", PII_A),
                erow("10.1016/article-b", PII_B), erow("10.1016/article-a", PII_B),
                erow("10.1016/article-c", PII_A)]
        result, _ = self.run_fake([elsevier(rows)])
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.records[0].doi, "10.1016/article-a")
        self.assertEqual(result.records[0].url, "https://doi.org/10.1016/article-a")
        self.assertTrue(any("冲突" in text for text in result.warnings))
        result, _ = self.run_fake([elsevier([erow("", PII_A)])])
        self.assertEqual(result.records[0].url, "https://www.sciencedirect.com/science/article/pii/" + PII_A)

    def test_wiley_sru_pam_query_and_dublin_core_record(self):
        self.config = search.SearchConfig("wiley", 'oxygen "evolution"', journal="Advanced Materials",
            year_from=2020, year_to=2025, retries=0)
        result, session = self.run_fake([wiley(wrow(), total=1)])
        self.assertEqual(result.source, "Wiley SRU")
        self.assertEqual(result.records[0].doi, "10.1002/article-a")
        self.assertEqual(result.records[0].title, "Water & Energy")
        self.assertEqual(result.records[0].url, "https://doi.org/10.1002/article-a")
        request, _ = session.calls[0]
        query = parse_qs(urlsplit(request.url).query)
        self.assertEqual(query["recordSchema"], ["pam"])
        self.assertEqual(query["recordPacking"], ["xml"])
        self.assertEqual(query["version"], ["1.2"])
        self.assertEqual(query["maximumRecords"], ["20"])
        self.assertEqual(query["startRecord"], ["1"])
        self.assertEqual(query["query"], ['cql.anywhere="oxygen \\"evolution\\"" and dcterms.isPartOf="Advanced Materials" and dc.date >= "2020-01-01" and dc.date <= "2025-12-31"'])
        self.assertFalse(any("key" in name.lower() or "wiley" in name.lower() for name in request.headers))

    def test_wiley_next_position_and_multiple_ispartof(self):
        self.config = search.SearchConfig("wiley", "water", journal="Advanced Materials", retries=0)
        result, session = self.run_fake([
            wiley(wrow(journal="Print Edition", extra="<dcterms:isPartOf>Advanced Materials</dcterms:isPartOf>"), total=2, next_position=2),
            wiley(wrow("https://doi.org/10.1002/article-b"), total=2)])
        self.assertEqual([row.doi for row in result.records], ["10.1002/article-a", "10.1002/article-b"])
        self.assertEqual(result.records[0].journal, "Advanced Materials")
        self.assertEqual(parse_qs(urlsplit(session.calls[1][0].url).query)["startRecord"], ["2"])

    def test_wiley_stalled_pagination_and_conflicting_identifiers(self):
        self.config = search.SearchConfig("wiley", "water", retries=0)
        result, session = self.run_fake([wiley(wrow(), total=2, next_position=1)])
        self.assertTrue(result.truncated)
        self.assertTrue(any("未前进" in text for text in result.warnings))
        self.assertEqual(len(session.calls), 1)
        result, _ = self.run_fake([wiley(wrow(extra="<dc:identifier>10.1002/other</dc:identifier>"), total=1)])
        self.assertFalse(result.records)
        self.assertTrue(result.warnings)

    def test_wiley_diagnostics_and_unsafe_or_invalid_xml_fail_explicitly(self):
        self.config = search.SearchConfig("wiley", "water", retries=0)
        bodies = [b"<html><body>verify</body></html>", b"not xml",
            b'<searchRetrieveResponse><diagnostics><diagnostic/></diagnostics></searchRetrieveResponse>',
            b'<!DOCTYPE x [<!ENTITY x "expanded">]><searchRetrieveResponse>&x;</searchRetrieveResponse>',
            '<!DOCTYPE x [<!ENTITY x "expanded">]><searchRetrieveResponse>&x;</searchRetrieveResponse>'.encode("utf-16")]
        for body in bodies:
            with self.subTest(body=body[:30]), self.assertRaises(search.SearchError):
                self.run_fake([response(body, headers={"Content-Type": "application/xml"})])

    def test_wiley_never_sends_credentials_from_config_or_session(self):
        self.config = search.SearchConfig("wiley", "water", api_key=TOKEN, insttoken=INSTTOKEN, retries=0)
        session = FakeSession([wiley(total=0)])
        session.headers.update({"Wiley-TDM-Client-Token": "old-token", "X-ELS-APIKey": "old-key",
            "Authorization": "Basic old", "Cookie": "auth=private", "X-API-Key": "old"})
        session.params.update({"api_key": "old-query", "token": "old-token"})
        session.cookies.set("private", "cookie")
        _, session = self.run_fake(session=session)
        request, _ = session.calls[0]
        rendered = request.url + str(dict(request.headers)) + str(request.body)
        for private in (TOKEN, INSTTOKEN, "old-token", "old-key", "Basic old", "auth=private", "old-query"):
            self.assertNotIn(private, rendered)
        self.assertNotIn("Cookie", request.headers)

    def test_redirects_never_forward_credentials_to_different_origin_or_path(self):
        locations = ["https://evil.example/search", "http://api.springernature.com/meta/v2/json",
                     "https://api.springernature.com:444/meta/v2/json", "https://api.springernature.com/other",
                     "https://user:pass@api.springernature.com/meta/v2/json"]
        for location in locations:
            session = FakeSession([response(status=302, headers={"Location": location})])
            with self.subTest(location=location), self.assertRaises(search.SearchError):
                self.run_fake(session=session)
            self.assertEqual(len(session.calls), 1)

    def test_same_endpoint_redirect_is_throttled_and_replaces_key(self):
        result, session = self.run_fake([
            response(status=302, headers={"Location": search.SPRINGER_ENDPOINT + "?q=water&api_key=wrong"}),
            springer([srow()])])
        self.assertEqual(len(result.records), 1)
        self.assertEqual(session.send_times, [0, 2])
        self.assertEqual(parse_qs(urlsplit(session.calls[1][0].url).query)["api_key"], [TOKEN])

    def test_put_redirect_cannot_change_method(self):
        self.config.publisher_id = "elsevier"
        with self.assertRaises(search.SearchError):
            self.run_fake([response(status=302, headers={"Location": search.ELSEVIER_ENDPOINT})])
        _, session = self.run_fake([response(status=307, headers={"Location": search.ELSEVIER_ENDPOINT}), elsevier([])])
        self.assertTrue(all(request.method == "PUT" for request, _ in session.calls))

    def test_429_retry_after_and_transient_retry_respect_interval(self):
        self.config.retries = 2
        result, session = self.run_fake([response(status=429, headers={"Retry-After": "5"}),
            response(status=503), springer([srow()])])
        self.assertEqual(len(result.records), 1)
        self.assertEqual(session.send_times, [0, 5, 7])
        self.assertEqual(self.waits, [5, 0, 2])

    def test_429_long_wait_and_exhausted_quota_do_not_sleep_or_retry(self):
        self.config.retries = 2
        for headers in ({"Retry-After": "3600"}, {"X-ELS-Status": "QUOTA_EXCEEDED"}):
            session = FakeSession([response(status=429, headers=headers)])
            with self.subTest(headers=headers), self.assertRaises(search.SearchError):
                self.run_fake(session=session)
            self.assertEqual(len(session.calls), 1)
        self.assertFalse(self.waits)
        result, session = self.run_fake([springer([srow()], total=50, headers={"X-RateLimit-Remaining": "0"})])
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(result.truncated)
        self.assertTrue(any("配额" in text for text in result.warnings))

    def test_network_error_sanitization_and_partial_result_preservation(self):
        error = requests.Timeout("failed " + search.SPRINGER_ENDPOINT + "?api_key=" + TOKEN)
        with self.assertRaises(search.SearchError) as caught:
            self.run_fake([error])
        self.assertNotIn(TOKEN, str(caught.exception))
        result, _ = self.run_fake([springer([srow()], total=50), error])
        self.assertEqual(len(result.records), 1)
        self.assertTrue(result.truncated)
        self.assertNotIn(TOKEN, repr(result))

    def test_cancel_before_network_and_after_first_page_preserves_records(self):
        event = threading.Event()
        event.set()
        result, session = self.run_fake(cancel_event=event)
        self.assertTrue(result.cancelled)
        self.assertFalse(session.calls)
        event.clear()
        result, session = self.run_fake([springer([srow()], total=50)], cancel_event=event,
            on_log=lambda _message: event.set())
        self.assertEqual(len(result.records), 1)
        self.assertTrue(result.cancelled)
        self.assertTrue(result.truncated)
        self.assertEqual(len(session.calls), 1)

    def test_bad_json_schema_and_body_are_rejected(self):
        bad = [response(b"[]"), response(b"<html>login</html>"),
               response(b'{"error":"private server detail"}'), response(b'{"records":[]}'),
               springer([], total="NaN"), response(b"{}", headers={"Content-Type": "text/html"}),
               response(b"{}", headers={"Content-Length": "99"})]
        for item in bad:
            with self.subTest(headers=item.headers), self.assertRaises(search.SearchError):
                self.run_fake([item])
        with patch.object(search, "_MAX_BODY", 4):
            with self.assertRaises(search.SearchError):
                self.run_fake([response(b"12345")])
            with self.assertRaises(search.SearchError):
                self.run_fake([response(b"", headers={"Content-Length": "5"})])

    def test_identifier_validation_and_metadata_secret_redaction(self):
        rows = [srow("https://evil.example/10.1007/not-an-id"), srow("arbitrary 10.1007/text"),
                srow("https://doi.org/10.1007/legit"), srow("doi: 10.1007/second"),
                srow("10.1007/" + TOKEN), srow("10.1007/third", title="H<sub>2</sub> " + TOKEN)]
        result, _ = self.run_fake([springer(rows)])
        self.assertEqual([row.doi for row in result.records], ["10.1007/legit", "10.1007/second", "10.1007/third"])
        self.assertNotIn(TOKEN, repr(result))
        self.assertEqual(result.records[-1].title, "H2 [已隐藏]")

    def test_search_entitlement_error_distinguishes_fulltext_permission(self):
        self.config.publisher_id = "elsevier"
        with self.assertRaisesRegex(search.SearchError, "正文下载权限不等于搜索权限"):
            self.run_fake([response(status=403)])


if __name__ == "__main__":
    unittest.main()
