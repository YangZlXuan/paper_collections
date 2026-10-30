from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from paper_downloader import downloader as dl


DOI = "10.1007/s12274-024-6813-0"
PAGE = dl.article_url(DOI)
PDF_URL = "https://link.springer.com/content/pdf/10.1007/test.pdf"
SI_URL = "https://media.springernature.com/esm/MediaObjects/supplement.csv"
PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer <<>>\n%%EOF\n"
CSV = b"measure,value\none,1\n"


def response(body=b"", status=200, url="", content_type="application/pdf", headers=None):
    item = requests.Response()
    item.status_code, item.url = status, url
    item.headers.update({"Content-Type": content_type})
    item.headers.update(headers or {})
    item.raw = io.BytesIO(body)
    item.encoding = "utf-8"
    return item


def page(*, doi=DOI, journal="Nano Research", pdf=PDF_URL, si=(), extra="", journal_key="citation_journal_title"):
    text = f'<html><head><meta name="citation_doi" content="{doi}"><meta name="citation_title" content="Research title">'
    if journal is not None:
        text += f'<meta name="{journal_key}" content="{journal}">'
    if pdf:
        text += f'<meta name="citation_pdf_url" content="{pdf}">'
    text += '</head><body>' + ''.join(f'<a href="{url}">Supplementary Information</a>' for url in si)
    return response((text + extra + '</body></html>').encode(), content_type="text/html")


class FakeSession(requests.Session):
    def __init__(self, routes):
        super().__init__()
        self.routes = {key: list(values) for key, values in routes.items()}
        self.calls = []
        self.send_times = []

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        self.send_times.append(dl.time.monotonic())
        if request.url not in self.routes or not self.routes[request.url]:
            raise AssertionError(f"Unexpected request: {request.url}")
        item = self.routes[request.url].pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item()
        item.url = item.url or request.url
        item.request = request
        return item


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.config = dl.DownloadConfig("", self.output, retries=0)
        self.env = patch.dict("os.environ", {"SN_COOKIE_FILE": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = 0.0
        self.waits = []
        self.clock_patch = patch.object(dl.time, "monotonic", side_effect=lambda: self.now)
        self.wait_patch = patch.object(dl, "_wait", side_effect=self.virtual_wait)
        self.clock_patch.start()
        self.wait_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.addCleanup(self.wait_patch.stop)

    def virtual_wait(self, event, seconds):
        dl._check_cancel(event)
        self.waits.append(max(0.0, seconds))
        self.now += max(0.0, seconds)

    def run_fake(self, routes, dois=None, **kwargs):
        session = FakeSession(routes)
        with patch.object(dl.requests, "Session", return_value=session):
            summary = dl.run_batch(dois or [DOI], self.config, **kwargs)
        return summary, session

    def record(self, summary):
        return json.loads(Path(summary.results[0].record_path).read_text(encoding="utf-8"))

    def test_direct_main_and_supplement_with_manifests(self):
        events = []
        summary, session = self.run_fake({PAGE: [page(si=[SI_URL])], PDF_URL: [response(PDF)], SI_URL: [response(CSV)]},
                                        on_result=lambda *args: events.append(args))
        result = summary.results[0]
        self.assertEqual(result.status, "success")
        self.assertEqual((result.supplements_total, result.supplements_downloaded), (1, 1))
        self.assertTrue(dl.is_valid_pdf(Path(result.file_path)))
        record = self.record(summary)
        self.assertEqual(record["pdf"]["file"], "article.pdf")
        self.assertEqual(record["supplements"][0]["sha256"], hashlib.sha256(CSV).hexdigest())
        self.assertEqual(record["supplements"][0]["bytes"], len(CSV))
        self.assertEqual(events[0][:2], (1, 1))
        self.assertTrue(summary.manifest_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        with summary.manifest_path.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["supplements_downloaded"], "1")
        self.assertNotIn("oa_status", row)
        self.assertTrue(all(not kwargs["allow_redirects"] for _, kwargs in session.calls))
        self.assertTrue(all(kwargs["timeout"] == (15.0, 90) for _, kwargs in session.calls))

    def test_url_mapping_and_allowlist(self):
        self.assertEqual(dl.article_url("10.1038/s41586-024-example"), "https://www.nature.com/articles/s41586-024-example")
        for url in ("http://nature.com/paper", "https://nature.com.evil.test/a", "https://user@nature.com/a", "https://nature.com:444/a"):
            self.assertFalse(dl.allowed(url, asset=True))
        self.assertTrue(dl.allowed("https://static-content.springer-cdn.com/esm/a.pdf", asset=True))
        self.assertFalse(dl.allowed("https://static-content.springer-cdn.com/esm/a.pdf"))
        self.assertFalse(dl.allowed("https://other.springer-cdn.com/a.pdf", asset=True))

    def test_journal_filter_uses_metadata_before_download(self):
        self.config.journal = "Nature"
        summary, session = self.run_fake({PAGE: [page()]})
        self.assertEqual(summary.results[0].status, "filtered")
        self.assertEqual(len(session.calls), 1)

    def test_missing_journal_cannot_pass_selected_journal(self):
        self.config.journal = "Nature"
        summary, _ = self.run_fake({PAGE: [page(journal=None)]})
        self.assertEqual(summary.results[0].status, "filtered")
        self.assertIn("缺少", summary.results[0].error)

    def test_prism_journal_and_prefixed_doi_are_supported(self):
        self.config.journal = "NANO-RESEARCH"
        summary, _ = self.run_fake({PAGE: [page(doi="doi:" + DOI, journal_key="prism.publicationName")], PDF_URL: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")

    def test_doi_mismatch_rejected(self):
        summary, _ = self.run_fake({PAGE: [page(doi="10.1007/other")]})
        self.assertEqual(summary.results[0].status, "failed")
        self.assertIn("DOI", summary.results[0].error)

    def test_login_form_on_real_article_does_not_block(self):
        summary, _ = self.run_fake({PAGE: [page(extra='<form action="/login"><input type="password"></form>')], PDF_URL: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")

    def test_actual_login_page_is_access_denied(self):
        summary, _ = self.run_fake({PAGE: [response(b'<html><title>Sign in</title><form action="/login"></form></html>', content_type="text/html")]})
        self.assertEqual(summary.results[0].status, "access_denied")

    def test_official_redirect_is_followed_external_redirect_is_not(self):
        login = "https://idp.springer.com/authorize"
        summary, session = self.run_fake({PAGE: [response(status=303, headers={"Location": login})],
                                         login: [page()], PDF_URL: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(len(session.calls), 3)
        summary, session = self.run_fake({PAGE: [response(status=302, headers={"Location": "https://external.example/login"})]})
        self.assertEqual(summary.results[0].status, "access_denied")
        self.assertEqual(len(session.calls), 1)

    def test_asset_redirect_to_external_host_is_not_followed(self):
        summary, session = self.run_fake({PAGE: [page()], PDF_URL: [response(status=302, headers={"Location": "https://external.example/a.pdf"})]})
        self.assertEqual(summary.results[0].status, "access_denied")
        self.assertEqual(len(session.calls), 2)

    def test_redirect_limit(self):
        summary, session = self.run_fake({PAGE: [response(status=302, headers={"Location": PAGE}) for _ in range(7)]})
        self.assertEqual(summary.results[0].status, "access_denied")
        self.assertEqual(len(session.calls), 7)

    def test_401_403_429_never_retry(self):
        self.config.retries = 2
        for code in (401, 403, 429):
            with self.subTest(code=code):
                summary, session = self.run_fake({PAGE: [response(status=code)]})
                self.assertEqual(summary.results[0].status, "access_denied")
                self.assertEqual(len(session.calls), 1)

    def test_all_requests_including_retry_redirect_and_asset_rate_limited(self):
        self.config.interval, self.config.retries = 2.5, 1
        redirected = "https://link.springer.com/article/final"
        second = "10.1007/second"
        second_pdf = "https://link.springer.com/content/pdf/second.pdf"
        summary, session = self.run_fake({PAGE: [response(status=503), response(status=302, headers={"Location": redirected})],
                                         redirected: [page(si=[SI_URL])], PDF_URL: [response(PDF)], SI_URL: [response(CSV)],
                                         dl.article_url(second): [page(doi=second, pdf=second_pdf)], second_pdf: [response(PDF)]},
                                        dois=[DOI, second])
        self.assertEqual([item.status for item in summary.results], ["success", "success"])
        self.assertEqual(session.send_times, [0.0, 2.5, 5.0, 7.5, 10.0, 12.5, 15.0])

    def test_interval_parser_accepts_boundaries_and_custom_numeric_values(self):
        self.assertEqual(dl.DownloadConfig("", self.output).interval, 2.0)
        for value, expected in ((2, 2.0), ("2", 2.0), (" 2.5 ", 2.5), (5.0, 5.0), ("60", 60.0)):
            with self.subTest(value=value):
                self.assertEqual(dl.parse_request_interval(value), expected)

    def test_invalid_intervals_rejected_before_session_or_network(self):
        values = (0, -1, 1.99, "1.99", float("nan"), "NaN", float("inf"), "-inf", "bad", "", None, True, False, 60.01, b"2")
        for value in values:
            with self.subTest(value=value), patch.object(dl.requests, "Session") as factory:
                self.config.interval = value
                with self.assertRaisesRegex(ValueError, "2.*60"):
                    dl.run_batch([DOI], self.config)
                factory.assert_not_called()

    def test_direct_client_validates_and_snapshots_selected_interval(self):
        session = FakeSession({PAGE: [page()], PDF_URL: [response(PDF)]})
        self.config.interval = 0
        with self.assertRaises(ValueError):
            dl._Client(session, self.config, threading.Event())
        self.assertEqual(session.calls, [])
        self.config.interval = "5"
        client = dl._Client(session, self.config, threading.Event())
        self.config.interval = 0
        with client.request(PAGE):
            pass
        with client.request(PDF_URL, asset=True):
            pass
        self.assertEqual(session.send_times, [0.0, 5.0])

    def test_default_interval_between_paper_and_pdf_is_two_seconds(self):
        summary, session = self.run_fake({PAGE: [page()], PDF_URL: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(session.send_times, [0.0, 2.0])

    def test_cancelling_interval_wait_stops_before_next_request(self):
        event = threading.Event()
        def cancel_wait(wait_event, seconds):
            self.assertGreater(seconds, 0)
            wait_event.set()
            self.virtual_wait(wait_event, seconds)
        with patch.object(dl, "_wait", side_effect=cancel_wait):
            summary, session = self.run_fake({PAGE: [page()]}, cancel_event=event)
        self.assertTrue(summary.cancelled)
        self.assertEqual(summary.results[0].status, "cancelled")
        self.assertEqual(len(session.calls), 1)

    def test_pdf_candidate_fallback(self):
        fallback = "https://link.springer.com/content/pdf/fallback.pdf"
        html = page(extra=f'<a href="{fallback}">Download PDF</a>')
        summary, _ = self.run_fake({PAGE: [html], PDF_URL: [response(b"bad")], fallback: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(summary.results[0].pdf_url, fallback)

    def test_si_scope_ignores_navigation_peer_review_and_reporting_summary(self):
        peer = "https://media.springernature.com/esm/peer.pdf"
        report = "https://media.springernature.com/esm/report.pdf"
        extra = (f'<a href="#ESM">Supplementary Information</a><a href="{PAGE}#ESM">Supplementary Information</a>'
                 f'<a href="{peer}">Supplementary peer review</a><a href="{report}">Reporting summary</a>'
                 '<a href="https://nature.com/supplementary-section">Supplementary Information</a>'
                 '<a href="https://media.springernature.com/image/MediaObjects/12274_Fig1_HTML.png">Full size image</a>')
        summary, _ = self.run_fake({PAGE: [page(si=[SI_URL], extra=extra)], PDF_URL: [response(PDF)], SI_URL: [response(CSV)]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(summary.results[0].supplements_total, 1)

    def test_exact_official_cdn_allowed_for_supplement_only(self):
        cdn = "https://static-content.springer-cdn.com/esm/si.csv"
        summary, _ = self.run_fake({PAGE: [page(si=[cdn])], PDF_URL: [response(PDF)], cdn: [response(CSV)]})
        self.assertEqual(summary.results[0].supplements_downloaded, 1)
        summary, _ = self.run_fake({PAGE: [response(status=302, headers={"Location": cdn})]})
        self.assertEqual(summary.results[0].status, "access_denied")

    def test_no_supplement_is_normal_and_toggle_disables_requests(self):
        summary, _ = self.run_fake({PAGE: [page()], PDF_URL: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")
        self.config.include_supplements = False
        summary, session = self.run_fake({PAGE: [page(si=[SI_URL])]})
        self.assertEqual(summary.results[0].status, "exists")
        self.assertEqual(summary.results[0].supplements_total, 0)
        self.assertEqual(len(session.calls), 1)

    def test_partial_when_supplement_fails(self):
        summary, _ = self.run_fake({PAGE: [page(si=[SI_URL])], PDF_URL: [response(PDF)], SI_URL: [response(status=403)]})
        self.assertEqual(summary.results[0].status, "partial")
        self.assertEqual(summary.results[0].supplements_failed, 1)

    def test_partial_when_supplement_succeeds_but_no_main_pdf(self):
        summary, _ = self.run_fake({PAGE: [page(pdf=None, si=[SI_URL])], SI_URL: [response(CSV)]})
        self.assertEqual(summary.results[0].status, "partial")
        self.assertEqual(summary.results[0].file_path, "")

    def test_no_main_pdf_no_files_status(self):
        summary, _ = self.run_fake({PAGE: [page(pdf=None)]})
        self.assertEqual(summary.results[0].status, "no_pdf")

    def test_resume_main_and_verified_supplement_without_asset_requests(self):
        first, _ = self.run_fake({PAGE: [page(si=[SI_URL])], PDF_URL: [response(PDF)], SI_URL: [response(CSV)]})
        summary, session = self.run_fake({PAGE: [page(si=[SI_URL])]})
        self.assertEqual(summary.results[0].status, "exists")
        self.assertEqual(summary.results[0].supplements_existing, 1)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(self.record(summary)["pdf"]["url"], PDF_URL)
        self.assertNotEqual(first.manifest_path, summary.manifest_path)

    def test_same_size_corrupt_supplement_is_redownloaded(self):
        first, _ = self.run_fake({PAGE: [page(si=[SI_URL])], PDF_URL: [response(PDF)], SI_URL: [response(CSV)]})
        record = self.record(first)
        target = Path(first.results[0].record_path).parent / record["supplements"][0]["file"]
        target.write_bytes(b"x" * len(CSV))
        summary, _ = self.run_fake({PAGE: [page(si=[SI_URL])], SI_URL: [response(CSV)]})
        self.assertEqual(summary.results[0].supplements_downloaded, 1)
        self.assertEqual(target.read_bytes(), CSV)

    def test_resume_provenance_survives_disabled_si_run(self):
        self.run_fake({PAGE: [page(si=[SI_URL])], PDF_URL: [response(PDF)], SI_URL: [response(CSV)]})
        self.config.include_supplements = False
        self.run_fake({PAGE: [page(si=[SI_URL])]})
        self.config.include_supplements = True
        summary, _ = self.run_fake({PAGE: [page(si=[SI_URL])]})
        self.assertEqual(summary.results[0].supplements_existing, 1)

    def test_invalid_existing_pdf_is_replaced(self):
        directory = self.output / dl.paper_directory_name(DOI)
        directory.mkdir()
        (directory / "article.pdf").write_bytes(b"%PDF-1.7\ntruncated")
        summary, _ = self.run_fake({PAGE: [page()], PDF_URL: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")

    def test_html_asset_rejected_even_with_wrong_content_type(self):
        summary, _ = self.run_fake({PAGE: [page()], PDF_URL: [response(b'<!DOCTYPE html><html>Login</html>', content_type="application/octet-stream")]})
        self.assertEqual(summary.results[0].status, "access_denied")
        self.assertEqual(list(self.output.rglob("*.part")), [])
        self.assertEqual(list(self.output.rglob("article.pdf")), [])

    def test_empty_truncated_and_oversized_assets_rejected(self):
        cases = [(b"", {}, "为空"), (b"%PDF-1.7\nmissing eof", {}, "完整 PDF"),
                 (PDF, {"Content-Length": str(len(PDF) + 1)}, "传输不完整"),
                 (PDF, {"Content-Length": str(151 * 1024 * 1024)}, "150 MB")]
        for body, headers, error in cases:
            with self.subTest(error=error):
                summary, _ = self.run_fake({PAGE: [page()], PDF_URL: [response(body, headers=headers)]})
                self.assertEqual(summary.results[0].status, "failed")
                self.assertIn(error, summary.results[0].error)
                self.assertEqual(list(self.output.rglob("*.part")), [])

    def test_html_size_limit(self):
        with patch.object(dl, "_MAX_HTML", 32):
            summary, _ = self.run_fake({PAGE: [page()]})
        self.assertEqual(summary.results[0].status, "failed")

    def test_cancel_during_stream_cleans_temp_and_keeps_manifest(self):
        event = threading.Event()
        streamed = response(PDF)
        def chunks(_size):
            yield b"%PDF-1.7\npartial"
            event.set()
            yield b"%%EOF\n"
        streamed.iter_content = chunks
        summary, _ = self.run_fake({PAGE: [page()], PDF_URL: [streamed]}, cancel_event=event)
        self.assertTrue(summary.cancelled)
        self.assertEqual(summary.results[0].status, "cancelled")
        self.assertEqual(self.record(summary)["status"], "cancelled")
        self.assertEqual(list(self.output.rglob("*.part")), [])

    def test_cancel_before_start_no_request(self):
        event = threading.Event()
        event.set()
        summary, session = self.run_fake({}, cancel_event=event)
        self.assertTrue(summary.cancelled)
        self.assertEqual(summary.results, [])
        self.assertEqual(session.calls, [])

    def test_cancel_during_supplement_preserves_main_file_path(self):
        event = threading.Event()
        streamed = response(CSV)
        def chunks(_size):
            event.set()
            yield CSV
        streamed.iter_content = chunks
        summary, _ = self.run_fake({PAGE: [page(si=[SI_URL])], PDF_URL: [response(PDF)], SI_URL: [streamed]}, cancel_event=event)
        result = summary.results[0]
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.pdf_url, PDF_URL)
        self.assertTrue(dl.is_valid_pdf(Path(result.file_path)))
        self.assertEqual(self.record(summary)["pdf"]["status"], "success")

    def test_malformed_manifest_collections_are_ignored(self):
        directory = self.output / dl.paper_directory_name(DOI)
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps({
            "pdf": {"url": ["not a URL"], "sha256": "bad"},
            "assets": {"one": "invalid", "two": {"url": ["bad"]}},
            "supplements": {"not": "a list"},
        }), encoding="utf-8")
        summary, _ = self.run_fake({PAGE: [page(si=[SI_URL])], PDF_URL: [response(PDF)], SI_URL: [response(CSV)]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(len(self.record(summary)["assets"]), 2)

    def test_corrupted_main_with_intact_pdf_markers_is_redownloaded(self):
        first, _ = self.run_fake({PAGE: [page()], PDF_URL: [response(PDF)]})
        target = Path(first.results[0].file_path)
        target.write_bytes(PDF.replace(b"Catalog", b"Corrupt"))
        summary, _ = self.run_fake({PAGE: [page()], PDF_URL: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")
        self.assertEqual(target.read_bytes(), PDF)

    def test_failed_paper_does_not_abort_next(self):
        other = "10.1007/second"
        summary, _ = self.run_fake({PAGE: [response(status=404)], dl.article_url(other): [page(doi=other)], PDF_URL: [response(PDF)]}, dois=[DOI, other])
        self.assertEqual([item.status for item in summary.results], ["failed", "success"])

    def test_netscape_cookies_preserve_domain_host_only_expiry_and_secure(self):
        path = self.output / "cookies.txt"
        path.write_text('# Netscape HTTP Cookie File\n'
                        'link.springer.com\tFALSE\t/\tTRUE\t4102444800\thostonly\tH\n'
                        '.springer.com\tTRUE\t/\tTRUE\t4102444800\tdomainwide\tD\n'
                        '.springer.com\tTRUE\t/\tTRUE\t1\texpired\tX\n'
                        '.unrelated.example\tTRUE\t/\tTRUE\t4102444800\tunrelated\tU\n', encoding="utf-8")
        self.config.cookie_file = path
        redirected = "https://idp.springer.com/authorize"
        summary, session = self.run_fake({PAGE: [response(status=302, headers={"Location": redirected})], redirected: [page()], PDF_URL: [response(PDF)]})
        self.assertEqual(summary.results[0].status, "success")
        first_cookie = session.calls[0][0].headers.get("Cookie", "")
        second_cookie = session.calls[1][0].headers.get("Cookie", "")
        self.assertIn("hostonly=H", first_cookie)
        self.assertIn("domainwide=D", first_cookie)
        self.assertNotIn("hostonly", second_cookie)
        self.assertIn("domainwide=D", second_cookie)
        self.assertNotIn("expired", first_cookie)
        self.assertNotIn("unrelated", first_cookie)
        self.assertNotIn("hostonly", Path(summary.results[0].record_path).read_text(encoding="utf-8"))

    def test_invalid_cookie_file_fails_before_network_without_contents(self):
        path = self.output / "invalid.txt"
        path.write_text("secret-cookie-value", encoding="utf-8")
        self.config.cookie_file = path
        with self.assertRaises(ValueError) as raised:
            dl.validate_config(self.config)
        self.assertNotIn("secret-cookie-value", str(raised.exception))

    def test_empty_email_allowed_and_cookie_env_fallback(self):
        dl.validate_config(self.config)
        path = self.output / "cookies.txt"
        path.write_text('# Netscape HTTP Cookie File\n', encoding="utf-8")
        with patch.dict("os.environ", {"SN_COOKIE_FILE": str(path)}):
            self.assertEqual(dl._cookie_path(self.config), path)
            dl.validate_config(self.config)

    def test_file_names_collision_safe_and_url_stable(self):
        self.assertNotEqual(dl.paper_directory_name("10.1007/a:b"), dl.paper_directory_name("10.1007/a_b"))
        self.assertLess(len(dl.paper_directory_name("10.1007/" + "a" * 2000)), 110)
        self.assertEqual(dl._asset_filename(SI_URL), dl._asset_filename(SI_URL))
        self.assertNotEqual(dl._asset_filename(SI_URL + "?v=1"), dl._asset_filename(SI_URL + "?v=2"))


if __name__ == "__main__":
    unittest.main()
