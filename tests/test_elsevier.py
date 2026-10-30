from __future__ import annotations

import csv
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from paper_downloader import elsevier as el
from paper_downloader.doi_parser import ArticleReference


DOI = "10.1016/j.cell.2011.02.013"
PII = "S0092867411001279"
API_KEY = "test-api-key-0123456789"
INSTTOKEN = "test-institution-token-0123456789"
REFERENCE = ArticleReference(doi=DOI)
ENDPOINT = el._endpoint(REFERENCE)
META_URL = ENDPOINT + "?view=META"
PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer <<>>\n%%EOF\n"


def response(body=b"", status=200, content_type="application/pdf", headers=None):
    item = requests.Response()
    item.status_code = status
    item.headers.update({"Content-Type": content_type})
    item.headers.update(headers or {})
    item.raw = io.BytesIO(body)
    item.encoding = "utf-8"
    return item


def metadata(*, doi=DOI, pii=PII, journal="Cell", title="Research title", headers=None, extra=None):
    core = {"prism:doi": doi, "pii": pii, "prism:publicationName": journal, "dc:title": title}
    core.update(extra or {})
    return response(json.dumps({"full-text-retrieval-response": {"coredata": core}}).encode(),
                    content_type="application/json", headers=headers)


class FakeSession(requests.Session):
    def __init__(self, routes):
        super().__init__()
        self.routes = {key: list(values) for key, values in routes.items()}
        self.calls, self.send_times = [], []

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        self.send_times.append(el.time.monotonic())
        if request.url not in self.routes or not self.routes[request.url]:
            raise AssertionError(f"Unexpected request to {request.url}")
        value = self.routes[request.url].pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value()
        value.url, value.request = request.url, request
        return value


class ElsevierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.config = el.ElsevierConfig("", self.output, api_key=API_KEY, insttoken=INSTTOKEN, retries=0, include_supplements=False)
        self.now, self.waits = 0.0, []
        self.clock = patch.object(el.time, "monotonic", side_effect=lambda: self.now)
        self.wait = patch.object(el, "_wait", side_effect=self.virtual_wait)
        self.clock.start()
        self.wait.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.wait.stop)

    def virtual_wait(self, event, seconds):
        el._check_cancel(event)
        self.now += max(0.0, seconds)
        self.waits.append(max(0.0, seconds))

    def run_fake(self, routes, records=None, **kwargs):
        session = FakeSession(routes)
        with patch.object(el.requests, "Session", return_value=session):
            summary = el.run_elsevier_batch(records or [REFERENCE], self.config, **kwargs)
        return summary, session

    def record(self, summary):
        return json.loads(Path(summary.results[0].record_path).read_text(encoding="utf-8"))

    def test_doi_metadata_then_pdf_and_manifests(self):
        events = []
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)]}, on_result=lambda *args: events.append(args))
        result = summary.results[0]
        self.assertEqual(result.status, "success")
        self.assertEqual((result.reference_key, result.doi, result.pii), (DOI, DOI, PII))
        self.assertTrue(el.is_valid_pdf(Path(result.file_path)))
        self.assertEqual(self.record(summary)["pdf"]["bytes"], len(PDF))
        self.assertEqual(events[0][:2], (1, 1))
        self.assertEqual(session.send_times, [0.0, 2.0])
        self.assertEqual([r.headers["Accept"] for r, _ in session.calls], ["application/json", "application/pdf"])
        self.assertTrue(all(not kw["allow_redirects"] for _, kw in session.calls))
        self.assertTrue(all(r.headers["X-ELS-APIKey"] == API_KEY for r, _ in session.calls))
        self.assertTrue(all(API_KEY not in r.url and INSTTOKEN not in r.url for r, _ in session.calls))
        with summary.manifest_path.open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(next(csv.DictReader(handle))["reference_key"], DOI)

    def test_pii_only_retains_original_key_and_returns_true_doi(self):
        reference = ArticleReference(pii=PII)
        endpoint = el._endpoint(reference)
        summary, _ = self.run_fake({endpoint + "?view=META": [metadata()], endpoint: [response(PDF)]}, records=[reference])
        result = summary.results[0]
        self.assertEqual(result.reference_key, "pii:" + PII)
        self.assertEqual(result.doi, DOI)
        self.assertEqual(result.pii, PII)
        self.assertNotEqual(result.doi, result.reference_key)

    def test_pii_metadata_without_doi_keeps_doi_empty(self):
        reference = ArticleReference(pii=PII)
        endpoint = el._endpoint(reference)
        summary, _ = self.run_fake({endpoint + "?view=META": [metadata(doi="")], endpoint: [response(PDF)]}, records=[reference])
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(summary.results[0].doi, "")

    def test_exact_journal_filter_prevents_pdf_request(self):
        self.config.journal = "Nature"
        summary, session = self.run_fake({META_URL: [metadata()]})
        self.assertEqual(summary.results[0].status, "filtered")
        self.assertEqual(len(session.calls), 1)

    def test_selected_journal_missing_metadata_is_filtered(self):
        self.config.journal = "Cell"
        summary, _ = self.run_fake({META_URL: [metadata(journal="")]})
        self.assertEqual(summary.results[0].status, "filtered")

    def test_doi_mismatch_or_missing_doi_is_rejected(self):
        for doi in ("10.1016/unrelated", ""):
            with self.subTest(doi=doi):
                summary, session = self.run_fake({META_URL: [metadata(doi=doi)]})
                self.assertEqual(summary.results[0].status, "failed")
                self.assertEqual(len(session.calls), 1)

    def test_pii_mismatch_or_missing_pii_is_rejected(self):
        reference = ArticleReference(pii=PII)
        endpoint = el._endpoint(reference)
        for pii in ("S1385894724012345", ""):
            with self.subTest(pii=pii):
                summary, session = self.run_fake({endpoint + "?view=META": [metadata(pii=pii)]}, records=[reference])
                self.assertEqual(summary.results[0].status, "failed")
                self.assertEqual(len(session.calls), 1)

    def test_doi_fallback_dc_identifier_is_verified(self):
        summary, _ = self.run_fake({META_URL: [metadata(doi="", extra={"dc:identifier": "doi:" + DOI})], ENDPOINT: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")

    def test_metadata_success_does_not_imply_pdf_permission(self):
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(status=403)]})
        self.assertEqual(summary.results[0].status, "access_denied")
        self.assertIn("元数据", summary.results[0].error)
        self.assertEqual(summary.stop_reason, "")

    def test_global_authentication_403_stops_queue_without_echoing_body(self):
        body = json.dumps({"service-error": {"status": {"statusCode": "AUTHENTICATION_ERROR", "statusText": API_KEY + INSTTOKEN}}}).encode()
        summary, session = self.run_fake({META_URL: [response(body, status=403, content_type="application/json")]},
                                        records=[REFERENCE, ArticleReference(doi="10.1016/next")])
        self.assertEqual(len(summary.results), 1)
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(summary.stop_reason)
        self.assertNotIn(API_KEY, summary.stop_reason + summary.results[0].error)

    def test_requestor_configuration_header_stops_queue(self):
        summary, _ = self.run_fake({META_URL: [response(status=403, headers={"X-ELS-Status": "Requestor configuration settings insufficient"})]})
        self.assertTrue(summary.stop_reason)

    def test_401_stops_and_404_records_missing(self):
        summary, _ = self.run_fake({META_URL: [response(status=401)]})
        self.assertEqual(summary.results[0].status, "access_denied")
        self.assertTrue(summary.stop_reason)
        summary, _ = self.run_fake({META_URL: [response(status=404)]})
        self.assertEqual(summary.results[0].status, "no_pdf")

    def test_article_entitlement_403_does_not_stop_next_record(self):
        other = ArticleReference(doi="10.1016/next")
        endpoint = el._endpoint(other)
        body = b'{"service-error":{"status":{"statusCode":"ENTITLEMENT_ERROR"}}}'
        summary, _ = self.run_fake({META_URL: [response(body, status=403)], endpoint + "?view=META": [metadata(doi=other.doi)],
                                    endpoint: [response(PDF)]}, records=[REFERENCE, other])
        self.assertEqual([item.status for item in summary.results], ["access_denied", "success"])

    def test_pdf_401_does_not_prevent_next_downloadable_article(self):
        other = ArticleReference(doi="10.1016/next")
        endpoint = el._endpoint(other)
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(status=401)],
                                    endpoint + "?view=META": [metadata(doi=other.doi)], endpoint: [response(PDF)]},
                                   records=[REFERENCE, other])
        self.assertEqual([item.status for item in summary.results], ["access_denied", "success"])
        self.assertEqual(summary.stop_reason, "")

    def test_explicit_entitlement_401_is_per_article_even_for_metadata(self):
        other = ArticleReference(doi="10.1016/next")
        endpoint = el._endpoint(other)
        body = b'{"service-error":{"status":{"statusCode":"ENTITLEMENT_ERROR"}}}'
        summary, _ = self.run_fake({META_URL: [response(body, status=401)], endpoint + "?view=META": [metadata(doi=other.doi)],
                                    endpoint: [response(PDF)]}, records=[REFERENCE, other])
        self.assertEqual([item.status for item in summary.results], ["access_denied", "success"])

    def test_xml_missing_key_error_is_global_without_echoing_contents(self):
        body = b'<service-error><status><statusCode>AUTHORIZATION_ERROR</statusCode><statusText>No APIKey provided for request</statusText></status></service-error>'
        summary, _ = self.run_fake({META_URL: [response(body, status=403, content_type="application/xml")]})
        self.assertTrue(summary.stop_reason)

    def test_cdn_redirect_has_no_credentials_and_saved_url_has_no_query(self):
        cdn = "https://pdf.sciencedirectassets.com/paper.pdf?signature=transient-signature&expires=123"
        session = FakeSession({META_URL: [metadata()], ENDPOINT: [response(status=303, headers={"Location": cdn})], cdn: [response(PDF)]})
        session.headers.update({"Authorization": "unrelated secret", "Cookie": "session=private", "X-ELS-Other": "private"})
        with patch.object(el.requests, "Session", return_value=session):
            summary = el.run_elsevier_batch([REFERENCE], self.config)
        cdn_headers = session.calls[-1][0].headers
        self.assertFalse(any(k.lower().startswith("x-els-") or k.lower() in {"authorization", "cookie"} for k in cdn_headers))
        record = self.record(summary)
        self.assertEqual(record["pdf"]["final_url"], "https://pdf.sciencedirectassets.com/paper.pdf")
        self.assertEqual(session.send_times, [0.0, 2.0, 4.0])
        self.assertNotIn("transient-signature", Path(summary.results[0].record_path).read_text(encoding="utf-8"))

    def test_external_or_secret_bearing_redirect_is_rejected(self):
        for target in ("https://evil.example/paper.pdf", "http://www.sciencedirect.com/paper.pdf",
                       "https://pdf.sciencedirectassets.com/paper.pdf?apiKey=" + API_KEY):
            with self.subTest(target=target):
                summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(status=307, headers={"Location": target})]})
                self.assertEqual(summary.results[0].status, "access_denied")
                self.assertEqual(len(session.calls), 2)
                self.assertNotIn(API_KEY, summary.results[0].error)

    def test_quota_429_without_remaining_header_stops_with_reset(self):
        self.config.retries = 2
        headers = {"X-ELS-Status": "QUOTA_EXCEEDED", "X-RateLimit-Reset": "2000000000"}
        summary, session = self.run_fake({META_URL: [response(status=429, headers=headers)]},
                                        records=[REFERENCE, ArticleReference(doi="10.1016/next")])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(summary.results), 1)
        self.assertIn("UTC", summary.stop_reason)
        self.assertEqual(self.waits, [])

    def test_zero_remaining_metadata_prevents_pdf_request(self):
        summary, session = self.run_fake({META_URL: [metadata(headers={"X-RateLimit-Remaining": "0"})]})
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(summary.results[0].status, "access_denied")
        self.assertIn("配额", summary.stop_reason)

    def test_last_quota_pdf_redirect_can_finish_cdn_then_stops_next_paper(self):
        cdn = "https://pdf.sciencedirectassets.com/paper.pdf"
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(status=303, headers={"Location": cdn, "X-RateLimit-Remaining": "0"})],
                                         cdn: [response(PDF)]}, records=[REFERENCE, ArticleReference(doi="10.1016/next")])
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(len(summary.results), 1)
        self.assertIn("配额", summary.stop_reason)

    def test_transient_429_honors_retry_after_and_interval(self):
        self.config.retries = 1
        summary, session = self.run_fake({META_URL: [response(status=429, headers={"Retry-After": "3"}), metadata()], ENDPOINT: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(session.send_times, [0.0, 3.0, 5.0])

    def test_long_retry_after_stops_without_sleeping(self):
        self.config.retries = 2
        summary, session = self.run_fake({META_URL: [response(status=429, headers={"Retry-After": "120"})]})
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(self.waits, [])
        self.assertIn("120", summary.stop_reason)

    def test_transient_network_and_server_retry_are_rate_limited(self):
        self.config.retries = 2
        summary, session = self.run_fake({META_URL: [requests.Timeout("contains " + API_KEY), response(status=503), metadata()], ENDPOINT: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(session.send_times, [0.0, 2.0, 4.0, 6.0])

    def test_html_fake_empty_and_truncated_pdf_are_not_saved(self):
        cases = [(b'<html>Sign in</html>', "application/octet-stream", "access_denied"),
                 (b"", "application/pdf", "failed"), (b"%PDF-1.7\npartial", "application/pdf", "failed"),
                 (b'{"error":"denied"}', "application/json", "access_denied")]
        for data, content_type, status in cases:
            with self.subTest(status=status, data=data):
                summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(data, content_type=content_type)]})
                self.assertEqual(summary.results[0].status, status)
                self.assertEqual(list(self.output.rglob("*.part")), [])
                self.assertEqual(list(self.output.rglob("article.pdf")), [])

    def test_size_limit_and_content_length_checked(self):
        for length in (len(PDF) + 1, 151 * 1024 * 1024):
            with self.subTest(length=length):
                summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF, headers={"Content-Length": str(length)})]})
                self.assertEqual(summary.results[0].status, "failed")

    def test_resume_checks_hash_and_replaces_corruption(self):
        first, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)]})
        summary, session = self.run_fake({META_URL: [metadata()]})
        self.assertEqual(summary.results[0].status, "exists")
        self.assertEqual(len(session.calls), 1)
        target = Path(first.results[0].file_path)
        target.write_bytes(PDF.replace(b"Catalog", b"Corrupt"))
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(target.read_bytes(), PDF)

    def test_cancel_during_stream_cleans_temp(self):
        event = threading.Event()
        streamed = response(PDF)
        def chunks(_size):
            yield b"%PDF-1.7\npartial"
            event.set()
            yield b"%%EOF"
        streamed.iter_content = chunks
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [streamed]}, cancel_event=event)
        self.assertTrue(summary.cancelled)
        self.assertEqual(summary.results[0].status, "cancelled")
        self.assertEqual(list(self.output.rglob("*.part")), [])

    def test_cancel_during_rate_wait_prevents_next_request(self):
        event = threading.Event()
        def cancel(wait_event, seconds):
            wait_event.set()
            self.virtual_wait(wait_event, seconds)
        with patch.object(el, "_wait", side_effect=cancel):
            summary, session = self.run_fake({META_URL: [metadata()]}, cancel_event=event)
        self.assertTrue(summary.cancelled)
        self.assertEqual(len(session.calls), 1)

    def test_credentials_absent_from_repr_logs_and_saved_metadata(self):
        logs = []
        summary, _ = self.run_fake({META_URL: [metadata(title=API_KEY + " " + INSTTOKEN)], ENDPOINT: [response(PDF)]}, on_log=logs.append)
        text = repr(self.config) + "\n".join(logs) + summary.manifest_path.read_text(encoding="utf-8-sig") + Path(summary.results[0].record_path).read_text(encoding="utf-8")
        self.assertNotIn(API_KEY, text)
        self.assertNotIn(INSTTOKEN, text)

    def test_validation_precedes_network(self):
        for field, value in (("api_key", ""), ("api_key", "bad\nkey"), ("interval", 1)):
            with self.subTest(field=field), patch.object(el.requests, "Session") as factory:
                old = getattr(self.config, field)
                setattr(self.config, field, value)
                with self.assertRaises(ValueError):
                    el.run_elsevier_batch([REFERENCE], self.config)
                factory.assert_not_called()
                setattr(self.config, field, old)


if __name__ == "__main__":
    unittest.main()
