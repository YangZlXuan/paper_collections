"""Wiley TDM transport and integration checks, without live credentials/network."""
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

from paper_downloader import wiley as wl
from paper_downloader.doi_parser import ArticleReference


DOI = "10.1111/jtsb.12390"
TOKEN = "synthetic-wiley-token-for-offline-tests"
REFERENCE = ArticleReference(doi=DOI)
ENDPOINT = wl._endpoint(REFERENCE)
META = wl._metadata_endpoint(DOI)
PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer <<>>\n%%EOF\n"
CDN = "https://alm.wiley.com/alm/api/v2/download/temporary-path-token?signature=temporary-query"


def response(body=b"", status=200, content_type="application/pdf", headers=None):
    result = requests.Response()
    result.status_code = status
    result.headers.update({"Content-Type": content_type})
    result.headers.update(headers or {})
    result.raw = io.BytesIO(body)
    result.encoding = "utf-8"
    return result


def metadata(doi=DOI, publisher="Wiley", member="311", journal="Journal for the Theory of Social Behaviour", title="Research", headers=None):
    message = {"DOI": doi, "publisher": publisher, "member": member, "container-title": [journal] if journal else [], "title": [title]}
    return response(json.dumps({"status": "ok", "message": message}).encode(), content_type="application/json", headers=headers)


class FakeSession(requests.Session):
    def __init__(self, routes):
        super().__init__()
        self.routes = {key: list(values) for key, values in routes.items()}
        self.calls, self.send_times = [], []

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        self.send_times.append(wl.time.monotonic())
        if request.url not in self.routes or not self.routes[request.url]:
            raise AssertionError("Unexpected request to " + request.url)
        item = self.routes[request.url].pop(0)
        if isinstance(item, Exception):
            raise item
        item = item() if callable(item) else item
        item.url, item.request = request.url, request
        return item


class WileyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.config = wl.WileyConfig("", self.output, api_key=TOKEN, retries=0, include_supplements=False)
        self.now, self.waits = 0.0, []
        self.enterContext(patch.object(wl.time, "monotonic", side_effect=lambda: self.now))
        self.enterContext(patch.object(wl, "_wait", side_effect=self.virtual_wait))

    def virtual_wait(self, event, seconds):
        wl._check_cancel(event)
        self.now += max(0.0, seconds)
        self.waits.append(max(0.0, seconds))

    def run_fake(self, routes, records=None, session=None, **kwargs):
        session = session or FakeSession(routes)
        with patch.object(wl.requests, "Session", return_value=session):
            summary = wl.run_wiley_batch(records or [REFERENCE], self.config, **kwargs)
        return summary, session

    def record(self, summary):
        return json.loads(Path(summary.results[0].record_path).read_text(encoding="utf-8"))

    def test_default_and_interval_validation_before_network(self):
        self.assertEqual(self.config.interval, 10)
        self.assertFalse(self.config.include_supplements)
        self.assertTrue(wl.WileyConfig("", self.output, api_key=TOKEN).include_supplements)
        for value in (None, True, "NaN", float("inf"), 9.99, 61, "bad"):
            with self.subTest(interval=value), self.assertRaises(ValueError):
                wl.parse_wiley_interval(value)
        self.assertEqual(wl.parse_wiley_interval("12.5"), 12.5)
        for name, value in (("api_key", ""), ("api_key", "token\nsecret"), ("interval", 2),
                            ("publisher_id", "elsevier"), ("retries", True), ("timeout", float("nan"))):
            old = getattr(self.config, name)
            with self.subTest(field=name), patch.object(wl.requests, "Session") as factory:
                setattr(self.config, name, value)
                with self.assertRaises(ValueError):
                    wl.run_wiley_batch([REFERENCE], self.config)
                factory.assert_not_called()
            setattr(self.config, name, old)

    def test_crossref_identity_then_pdf_and_results(self):
        events = []
        summary, session = self.run_fake({META: [metadata()], ENDPOINT: [response(PDF)]}, on_result=lambda *args: events.append(args))
        result = summary.results[0]
        self.assertEqual((result.status, result.doi, result.reference_key), ("success", DOI, DOI))
        self.assertEqual(result.supplements_status, "disabled")
        self.assertEqual(result.supplements_total, 0)
        self.assertTrue(wl.is_valid_pdf(Path(result.file_path)))
        self.assertTrue(Path(result.file_path).parent.name.startswith("wiley_"))
        self.assertEqual(self.record(summary)["pdf"]["bytes"], len(PDF))
        self.assertEqual(session.send_times, [0, 10])
        self.assertEqual(events[0][:2], (1, 1))
        self.assertNotIn("Wiley-TDM-Client-Token", session.calls[0][0].headers)
        self.assertEqual(session.calls[1][0].headers["Wiley-TDM-Client-Token"], TOKEN)
        self.assertTrue(all(not kwargs["allow_redirects"] for _, kwargs in session.calls))
        with summary.manifest_path.open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(next(csv.DictReader(handle))["supplements_status"], "disabled")

    def test_cdn_redirect_strips_credentials_and_path_query_tokens(self):
        session = FakeSession({META: [metadata()], ENDPOINT: [response(status=302, headers={"Location": CDN})], CDN: [response(PDF)]})
        session.headers.update({"Wiley-TDM-Client-Token": "stale", "Authorization": "private", "Cookie": "private", "X-ELS-APIKey": "private"})
        summary, session = self.run_fake({}, session=session)
        self.assertEqual(summary.results[0].status, "success")
        for index in (0, 2):
            headers = session.calls[index][0].headers
            self.assertFalse(any(key.lower().startswith(("wiley-", "x-els-")) or key.lower() in {"authorization", "cookie"} for key in headers))
        self.assertEqual(session.send_times, [0, 10, 20])
        record = self.record(summary)
        self.assertEqual(record["pdf"]["final_url"], "https://alm.wiley.com/")
        text = Path(summary.results[0].record_path).read_text(encoding="utf-8")
        self.assertNotIn("temporary-path-token", text)
        self.assertNotIn("temporary-query", text)
        self.assertNotIn(TOKEN, text)

    def test_redirect_unapproved_host_scheme_port_or_credential_rejected(self):
        urls = ["https://evil.example/download", "https://arbitrary.s3.amazonaws.com/paper.pdf", "http://alm.wiley.com/paper.pdf",
                "https://alm.wiley.com:444/paper.pdf", "https://user:password@alm.wiley.com/paper.pdf", "https://alm.wiley.com/" + TOKEN]
        for url in urls:
            with self.subTest(url=url):
                summary, session = self.run_fake({META: [metadata()], ENDPOINT: [response(status=302, headers={"Location": url})]})
                self.assertEqual(summary.results[0].status, "access_denied")
                self.assertEqual(len(session.calls), 2)
                self.assertNotIn(TOKEN, summary.results[0].error)

    def test_non_wiley_and_exact_journal_filter(self):
        summary, session = self.run_fake({META: [metadata(publisher="Elsevier", member="78")]})
        self.assertEqual(summary.results[0].status, "filtered")
        self.assertEqual(len(session.calls), 1)
        self.config.journal = "Nature"
        summary, _ = self.run_fake({META: [metadata(journal="Nature Communications")]})
        self.assertEqual(summary.results[0].status, "filtered")
        summary, _ = self.run_fake({META: [metadata(journal="")]})
        self.assertEqual(summary.results[0].status, "filtered")

    def test_normalized_journal_and_wiley_member(self):
        self.config.journal = "Journal for the Theory of Social Behaviour"
        summary, _ = self.run_fake({META: [metadata(publisher="John Wiley & Sons", member="http://id.crossref.org/member/311",
                                                    journal="Journal for the Theory of Social Behaviour")], ENDPOINT: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")

    def test_input_journal_cannot_override_crossref(self):
        self.config.journal = "Advanced Materials"
        reference = ArticleReference(doi=DOI, journal="Advanced Materials")
        summary, session = self.run_fake({META: [metadata()]}, records=[reference])
        self.assertEqual(summary.results[0].status, "filtered")
        self.assertEqual(len(session.calls), 1)

    def test_doi_mismatch_missing_or_invalid_metadata_not_downloaded(self):
        cases = [metadata(doi="10.1002/other"), metadata(doi=""), response(b"<html>error</html>"),
                 response(b'{"status":"ok","message":null}', content_type="application/json")]
        for item in cases:
            with self.subTest(item=item):
                summary, session = self.run_fake({META: [item]})
                self.assertEqual(summary.results[0].status, "failed")
                self.assertEqual(len(session.calls), 1)

    def test_pii_only_or_invalid_doi_never_calls_network(self):
        for reference in (ArticleReference(pii="S0000000026000010"), ArticleReference(doi="not-a-doi")):
            summary, session = self.run_fake({}, records=[reference])
            self.assertEqual(summary.results[0].status, "failed")
            self.assertEqual(session.calls, [])

    def test_explicit_invalid_token_or_missing_auth_stops_batch(self):
        for status, body in ((400, b"No TDM client token"), (401, b"unknown"), (403, b"The TDM Client Token that you have provided is invalid and not registered " + TOKEN.encode())):
            with self.subTest(status=status):
                summary, session = self.run_fake({META: [metadata()], ENDPOINT: [response(body, status=status, content_type="text/plain")]},
                                                 records=[REFERENCE, ArticleReference(doi="10.1002/next")])
                self.assertTrue(summary.stop_reason)
                self.assertEqual(len(summary.results), 1)
                self.assertEqual(summary.results[0].status, "access_denied")
                self.assertNotIn(TOKEN, summary.stop_reason + summary.results[0].error)

    def test_unknown_403_and_404_are_per_article_access_failures(self):
        other = ArticleReference(doi="10.1002/next")
        for status in (403, 404):
            with self.subTest(status=status):
                summary, _ = self.run_fake({META: [metadata()], ENDPOINT: [response(b"Access denied", status=status)],
                                           wl._metadata_endpoint(other.doi): [metadata(doi=other.doi)], wl._endpoint(other): [response(PDF)]},
                                          records=[REFERENCE, other])
                self.assertEqual([item.status for item in summary.results], ["access_denied", "success"] if status == 403 else ["access_denied", "exists"])
                self.assertFalse(summary.stop_reason)
                if status == 404:
                    self.assertIn("不能单独判断", summary.results[0].error)

    def test_crossref_404_is_metadata_failure_and_no_tdm_request(self):
        summary, session = self.run_fake({META: [response(status=404)]})
        self.assertEqual(summary.results[0].status, "failed")
        self.assertIn("Crossref", summary.results[0].error)
        self.assertEqual(len(session.calls), 1)

    def test_429_honors_retry_after_and_interval(self):
        self.config.retries = 1
        summary, session = self.run_fake({META: [metadata()], ENDPOINT: [response(status=429, headers={"Retry-After": "15"}), response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(session.send_times, [0, 10, 25])

    def test_429_http_date_parsed(self):
        import datetime
        from email.utils import format_datetime
        future = format_datetime(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30), usegmt=True)
        client = wl._Client(FakeSession({}), self.config, threading.Event())
        delay = client._retry_delay(response(status=429, headers={"Retry-After": future}), 0)
        self.assertGreater(delay, 28)
        self.assertLessEqual(delay, 30)

    def test_long_or_repeated_limit_stops_batch(self):
        for retry_after in ("120", "NaN", "10"):
            with self.subTest(retry_after=retry_after):
                summary, session = self.run_fake({META: [response(status=429, headers={"Retry-After": retry_after})]},
                                                 records=[REFERENCE, ArticleReference(doi="10.1002/next")])
                self.assertTrue(summary.stop_reason)
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(len(summary.results), 1)

    def test_network_and_server_retries_keep_ten_second_interval(self):
        self.config.retries = 2
        summary, session = self.run_fake({META: [requests.Timeout(TOKEN), response(status=503), metadata()], ENDPOINT: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(session.send_times, [0, 10, 20, 30])

    def test_html_json_empty_truncated_pdf_not_saved(self):
        for body, mime, status in ((b"<html>login</html>", "application/octet-stream", "access_denied"),
                                   (b'{"error": "denied"}', "application/json", "access_denied"),
                                   (b"", "application/pdf", "failed"), (b"%PDF-1.7\npartial", "application/pdf", "failed")):
            with self.subTest(body=body):
                summary, _ = self.run_fake({META: [metadata()], ENDPOINT: [response(body, content_type=mime)]})
                self.assertEqual(summary.results[0].status, status)
                self.assertEqual(list(self.output.rglob("article.pdf")), [])
                self.assertEqual(list(self.output.rglob("*.part")), [])

    def test_file_size_and_content_length_verified(self):
        for length in (len(PDF) + 1, 151 * 1024 * 1024):
            summary, _ = self.run_fake({META: [metadata()], ENDPOINT: [response(PDF, headers={"Content-Length": str(length)})]})
            self.assertEqual(summary.results[0].status, "failed")
            self.assertEqual(list(self.output.rglob("article.pdf")), [])

    def test_stream_retry_restores_file_atomically_and_is_rate_limited(self):
        self.config.retries = 1
        broken = response()
        def chunks(size):
            yield b"%PDF-1.7\npartial"
            raise requests.exceptions.ChunkedEncodingError(TOKEN)
        broken.iter_content = chunks
        summary, session = self.run_fake({META: [metadata()], ENDPOINT: [broken, response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(session.send_times, [0, 10, 20])
        self.assertEqual(Path(summary.results[0].file_path).read_bytes(), PDF)

    def test_resume_verifies_hash_and_provenance(self):
        first, _ = self.run_fake({META: [metadata()], ENDPOINT: [response(PDF)]})
        resumed, session = self.run_fake({META: [metadata()]})
        self.assertEqual(resumed.results[0].status, "exists")
        self.assertEqual(len(session.calls), 1)
        target = Path(first.results[0].file_path)
        target.write_bytes(PDF.replace(b"Catalog", b"Corrupt"))
        restored, _ = self.run_fake({META: [metadata()], ENDPOINT: [response(PDF)]})
        self.assertEqual(restored.results[0].status, "success")
        self.assertEqual(target.read_bytes(), PDF)
        manifest = Path(restored.results[0].record_path)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["doi"] = "10.1111/unrelated"
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        reverified, _ = self.run_fake({META: [metadata()], ENDPOINT: [response(PDF)]})
        self.assertEqual(reverified.results[0].status, "success")

    def test_cancel_stream_cleans_partial_file(self):
        event = threading.Event()
        streamed = response()
        def chunks(size):
            yield b"%PDF-1.7\npartial"
            event.set()
            yield b"%%EOF"
        streamed.iter_content = chunks
        summary, _ = self.run_fake({META: [metadata()], ENDPOINT: [streamed]}, cancel_event=event)
        self.assertTrue(summary.cancelled)
        self.assertEqual(summary.results[0].status, "cancelled")
        self.assertEqual(list(self.output.rglob("*.part")), [])

    def test_cancel_rate_wait_stops_before_pdf(self):
        event = threading.Event()
        def cancel(wait_event, seconds):
            wait_event.set()
            self.virtual_wait(wait_event, seconds)
        with patch.object(wl, "_wait", side_effect=cancel):
            summary, session = self.run_fake({META: [metadata()]}, cancel_event=event)
        self.assertTrue(summary.cancelled)
        self.assertEqual(len(session.calls), 1)

    def test_cancel_before_start_never_requests(self):
        event = threading.Event()
        event.set()
        summary, session = self.run_fake({}, cancel_event=event)
        self.assertTrue(summary.cancelled)
        self.assertFalse(summary.results)
        self.assertFalse(session.calls)

    def test_token_redacted_in_repr_logs_and_saved_metadata(self):
        logs = []
        summary, _ = self.run_fake({META: [metadata(title="title " + TOKEN)], ENDPOINT: [response(PDF)]}, on_log=logs.append)
        text = repr(self.config) + "\n".join(logs) + Path(summary.results[0].record_path).read_text(encoding="utf-8") + summary.manifest_path.read_text(encoding="utf-8-sig")
        self.assertNotIn(TOKEN, text)
        self.assertIn("[已隐藏]", text)


if __name__ == "__main__":
    unittest.main()
