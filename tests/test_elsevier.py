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
XML_URL = ENDPOINT + "?view=FULL"
PREVIEW_STATUS = "WARNING - Response limited to first page because requestor not entitled to resource"
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


def full_xml(*, doi=DOI, body="<body><section><para>Full body text.</para></section></body>"):
    return (f'<full-text-retrieval-response xmlns="http://www.elsevier.com/xml/svapi/article/dtd" '
            f'xmlns:p="http://prismstandard.org/namespaces/basic/2.0/">'
            f'<coredata><p:doi>{doi}</p:doi><pii>{PII}</pii></coredata>'
            f'<originalText><article>{body}</article></originalText></full-text-retrieval-response>').encode()


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
        self.enterContext(patch("paper_downloader.request_quota.settings_path", return_value=self.output / "settings.json"))
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

    def test_authentication_403_skips_article_without_echoing_body(self):
        other = ArticleReference(doi="10.1016/next")
        endpoint = el._endpoint(other)
        body = json.dumps({"service-error": {"status": {"statusCode": "AUTHENTICATION_ERROR", "statusText": API_KEY + INSTTOKEN}}}).encode()
        summary, session = self.run_fake({META_URL: [response(body, status=403, content_type="application/json")],
            endpoint + "?view=META": [metadata(doi=other.doi)], endpoint: [response(PDF)]}, records=[REFERENCE, other])
        self.assertEqual([item.status for item in summary.results], ["access_denied", "success"])
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(summary.stop_reason, "")
        text = summary.results[0].error + summary.manifest_path.read_text(encoding="utf-8-sig")
        self.assertNotIn(API_KEY, text)
        self.assertNotIn(INSTTOKEN, text)

    def test_requestor_configuration_header_skips_article(self):
        other = ArticleReference(doi="10.1016/next")
        endpoint = el._endpoint(other)
        summary, _ = self.run_fake({META_URL: [response(status=403, headers={"X-ELS-Status": "Requestor configuration settings insufficient"})],
            endpoint + "?view=META": [metadata(doi=other.doi)], endpoint: [response(PDF)]}, records=[REFERENCE, other])
        self.assertEqual([item.status for item in summary.results], ["access_denied", "success"])
        self.assertEqual(summary.stop_reason, "")

    def test_401_skips_and_404_records_missing(self):
        summary, _ = self.run_fake({META_URL: [response(status=401)]})
        self.assertEqual(summary.results[0].status, "access_denied")
        self.assertEqual(summary.stop_reason, "")
        summary, _ = self.run_fake({META_URL: [response(status=404)]})
        self.assertEqual(summary.results[0].status, "no_pdf")

    def test_xml_fallback_403_does_not_stop_next_article(self):
        other = ArticleReference(doi="10.1016/next")
        endpoint = el._endpoint(other)
        summary, _ = self.run_fake({META_URL: [metadata()],
            ENDPOINT: [response(PDF, headers={"X-ELS-Status": PREVIEW_STATUS})],
            XML_URL: [response(status=403, headers={"X-ELS-Status": "AUTHENTICATION_ERROR"})],
            endpoint + "?view=META": [metadata(doi=other.doi)], endpoint: [response(PDF)]}, records=[REFERENCE, other])
        self.assertEqual([item.status for item in summary.results], ["access_denied", "success"])
        self.assertEqual(summary.stop_reason, "")

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

    def test_xml_auth_error_is_per_article_without_echoing_contents(self):
        body = b'<service-error><status><statusCode>AUTHORIZATION_ERROR</statusCode><statusText>No APIKey provided for request</statusText></status></service-error>'
        summary, _ = self.run_fake({META_URL: [response(body, status=403, content_type="application/xml")]})
        self.assertEqual(summary.stop_reason, "")
        self.assertEqual(summary.results[0].status, "access_denied")
        self.assertNotIn("No APIKey provided", summary.results[0].error)

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

    def test_preview_pdf_downloads_full_xml_without_saving_preview(self):
        xml = full_xml()
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF, headers={"X-ELS-Status": PREVIEW_STATUS})],
                                         XML_URL: [response(xml, content_type="text/xml")]})
        result = summary.results[0]
        self.assertEqual(result.status, "success")
        self.assertEqual(Path(result.file_path).name, "article.xml")
        self.assertEqual(Path(result.file_path).read_bytes(), xml)
        self.assertEqual(result.pdf_url, "")
        self.assertEqual(result.error, "")
        self.assertEqual(list(self.output.rglob("article.pdf")), [])
        self.assertEqual(self.record(summary)["pdf"]["status"], "preview")
        self.assertEqual(self.record(summary)["xml"]["view"], "FULL")
        self.assertEqual(session.calls[-1][0].headers["Accept"], "text/xml")
        self.assertEqual(session.calls[-1][0].headers["X-ELS-APIKey"], API_KEY)
        self.assertEqual(session.send_times, [0, 2, 4])

    def test_preview_warning_on_redirect_is_not_lost(self):
        cdn = "https://pdf.sciencedirectassets.com/preview.pdf"
        summary, session = self.run_fake({META_URL: [metadata()],
            ENDPOINT: [response(status=303, headers={"Location": cdn, "X-ELS-Status": PREVIEW_STATUS})],
            XML_URL: [response(full_xml(), content_type="application/xml")]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertFalse(any(request.url == cdn for request, _ in session.calls))

    def test_resume_reuses_verified_xml_without_pdf_or_xml_requests(self):
        first, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF, headers={"X-ELS-Status": PREVIEW_STATUS})],
                                  XML_URL: [response(full_xml(), content_type="text/xml")]})
        target = Path(first.results[0].file_path)
        modified = target.stat().st_mtime_ns
        resumed, session = self.run_fake({META_URL: [metadata()]})
        self.assertEqual(resumed.results[0].status, "exists")
        self.assertEqual(resumed.results[0].file_path, str(target))
        self.assertEqual(target.stat().st_mtime_ns, modified)
        self.assertEqual(len(session.calls), 1)

    def test_legacy_pdf_is_rechecked_and_xml_added_when_it_was_preview(self):
        first, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)]})
        record = self.record(first)
        record["pdf"].pop("full_text_verified")
        Path(first.results[0].record_path).write_text(json.dumps(record), encoding="utf-8")
        target = Path(first.results[0].file_path)
        modified = target.stat().st_mtime_ns
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF, headers={"X-ELS-Status": PREVIEW_STATUS})],
                                         XML_URL: [response(full_xml(), content_type="text/xml")]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(Path(summary.results[0].file_path).suffix, ".xml")
        self.assertEqual(target.stat().st_mtime_ns, modified)
        self.assertEqual(self.record(summary)["pdf"]["status"], "preview")
        self.assertEqual(len(session.calls), 3)

    def test_legacy_complete_pdf_is_verified_without_downloading_its_body(self):
        first, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)]})
        record = self.record(first)
        record["pdf"].pop("full_text_verified")
        Path(first.results[0].record_path).write_text(json.dumps(record), encoding="utf-8")
        target = Path(first.results[0].file_path)
        modified = target.stat().st_mtime_ns
        headers_only = response(PDF)
        headers_only.iter_content = lambda *_: self.fail("An existing complete PDF should not be downloaded again")
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [headers_only]})
        self.assertEqual(summary.results[0].status, "exists")
        self.assertTrue(self.record(summary)["pdf"]["full_text_verified"])
        self.assertEqual(target.stat().st_mtime_ns, modified)
        self.assertEqual(len(session.calls), 2)

    def test_corrupt_existing_xml_is_downloaded_again(self):
        first, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF, headers={"X-ELS-Status": PREVIEW_STATUS})],
                                  XML_URL: [response(full_xml(), content_type="text/xml")]})
        target = Path(first.results[0].file_path)
        target.write_bytes(b"x" * len(full_xml()))
        summary, session = self.run_fake({META_URL: [metadata()], XML_URL: [response(full_xml(), content_type="text/xml")]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(target.read_bytes(), full_xml())
        self.assertEqual(len(session.calls), 2)

    def test_xml_fallback_rejects_metadata_wrong_article_and_unsafe_documents(self):
        declaration = '<!DOCTYPE x [<!ENTITY x "expanded">]><x>&x;</x>'
        cases = [full_xml(body="<abstract>Only an abstract.</abstract>"),
                 full_xml(body="<body/>"), full_xml(doi="10.1016/wrong"),
                 b"<full-text-retrieval-response>", b"<service-error/>",
                 declaration.encode(), declaration.encode("utf-16")]
        for body in cases:
            with self.subTest(body=body[:50]):
                summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF, headers={"X-ELS-Status": PREVIEW_STATUS})],
                                            XML_URL: [response(body, content_type="text/xml")]})
                self.assertNotEqual(summary.results[0].status, "success")
                self.assertEqual(summary.results[0].file_path, "")
                self.assertIn("XML", summary.results[0].error)
                self.assertEqual(list(self.output.rglob("article.xml")), [])
                self.assertEqual(list(self.output.rglob("*.part")), [])

    def test_cancelled_xml_cleans_temporary_file_and_can_resume(self):
        event = threading.Event()
        streamed = response(full_xml(), content_type="text/xml")
        def chunks(_size):
            yield full_xml()[:50]
            event.set()
            yield full_xml()[50:]
        streamed.iter_content = chunks
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF, headers={"X-ELS-Status": PREVIEW_STATUS})],
                                    XML_URL: [streamed]}, cancel_event=event)
        self.assertTrue(summary.cancelled)
        self.assertEqual(list(self.output.rglob("*.part")), [])
        self.assertEqual(self.record(summary)["pdf"]["status"], "preview")
        resumed, session = self.run_fake({META_URL: [metadata()], XML_URL: [response(full_xml(), content_type="text/xml")]})
        self.assertEqual(resumed.results[0].status, "success")
        self.assertEqual(len(session.calls), 2)

    def test_preview_at_quota_limit_does_not_attempt_xml(self):
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF, headers={
            "X-ELS-Status": PREVIEW_STATUS, "X-RateLimit-Remaining": "0"})]})
        self.assertIn("配额", summary.stop_reason)
        self.assertEqual(self.record(summary)["pdf"]["status"], "preview")
        self.assertEqual(len(session.calls), 2)

    def test_xml_stream_retry_keeps_request_interval(self):
        self.config.retries = 1
        broken = response(content_type="text/xml")
        def chunks(_size):
            yield full_xml()[:30]
            raise requests.exceptions.ChunkedEncodingError("interrupted")
        broken.iter_content = chunks
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF, headers={"X-ELS-Status": PREVIEW_STATUS})],
            XML_URL: [broken, response(full_xml(), content_type="text/xml")]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(session.send_times, [0, 2, 4, 6])

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
