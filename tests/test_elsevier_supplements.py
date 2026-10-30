from __future__ import annotations

import hashlib
import io
import json
import lzma
import tempfile
import threading
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

from paper_downloader import elsevier as el
from paper_downloader.doi_parser import ArticleReference
from test_elsevier import API_KEY, INSTTOKEN, DOI, PII, ENDPOINT, META_URL, PDF, REFERENCE, FakeSession, metadata, response


OBJECTS = el._object_endpoint(ArticleReference(pii=PII)) + "?view=META"


def docx_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>')
    return output.getvalue()


def attachment(ref="mmc1", filename="mmc1.docx", kind="APPLICATION", mime="application/word", body=None, url=None):
    body = docx_bytes() if body is None else body
    return {"prism:url": url or f"https://api.elsevier.com/content/object/eid/1-s2.0-{PII}-{filename}?httpAccept=%2A%2F%2A",
            "eid": f"1-s2.0-{PII}-{filename}", "ref": ref, "filename": filename, "type": kind,
            "mimetype": mime, "size": str(len(body))}


def inventory(items=(), *, doi=DOI, pii=PII, headers=None):
    body = json.dumps({"attachment-metadata-response": {
        "coredata": {"prism:doi": doi, "dc:identifier": "PII:" + pii}, "attachment": list(items)}}).encode()
    return response(body, content_type="application/json", headers=headers)


class ElsevierSupplementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.config = el.ElsevierConfig("", self.output, api_key=API_KEY, insttoken=INSTTOKEN, retries=0)
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

    def run_fake(self, routes, **kwargs):
        session = FakeSession(routes)
        with patch.object(el.requests, "Session", return_value=session):
            summary = el.run_elsevier_batch([REFERENCE], self.config, **kwargs)
        return summary, session

    def record(self, summary):
        return json.loads(Path(summary.results[0].record_path).read_text(encoding="utf-8"))

    def test_real_schema_mmc_docx_download_and_figure_exclusion(self):
        item = attachment()
        objects = [attachment("gr1", "gr1.jpg", "IMAGE-HIGH-RES", "image/jpeg", b"figure"),
                   attachment("si0001", "si0001.svg", "ALTIMG", "image/svg+xml", b"equation"),
                   attachment("am", "am.pdf", "AAM-PDF", "application/pdf", PDF),
                   attachment("mmc1", "mmc1.sml", "IMAGE-MMC-THUMBNAIL", "image/gif", b"preview"), item]
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory(objects)],
                                         item["prism:url"]: [response(docx_bytes(), content_type="application/word")]})
        result = summary.results[0]
        self.assertTrue(self.config.include_supplements)
        self.assertEqual((result.status, result.supplements_status), ("success", "complete"))
        self.assertEqual((result.supplements_total, result.supplements_downloaded), (1, 1))
        self.assertEqual(session.send_times, [0.0, 2.0, 4.0, 6.0])
        self.assertEqual(session.calls[-1][0].headers["Accept"], "*/*")
        record = self.record(summary)
        si = record["supplements"][0]
        self.assertEqual(si["object_ref"], "mmc1")
        self.assertEqual(si["source_filename"], "mmc1.docx")
        self.assertEqual(si["bytes"], len(docx_bytes()))
        self.assertEqual(si["sha256"], hashlib.sha256(docx_bytes()).hexdigest())
        self.assertNotIn("?", si["url"])
        self.assertNotIn("request_url", si)
        self.assertEqual(record["supplement_discovery"]["objects_total"], 5)
        with zipfile.ZipFile(Path(result.record_path).parent / si["file"]) as archive:
            self.assertIsNone(archive.testzip())

    def test_valid_figure_only_inventory_is_confirmed_none(self):
        item = attachment("si0001", "si0001.svg", "ALTIMG", "image/svg+xml", b"x")
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory([item])]})
        self.assertEqual((summary.results[0].status, summary.results[0].supplements_status), ("success", "none"))
        self.assertEqual(len(session.calls), 3)

    def test_valid_empty_inventory_is_none(self):
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory()]})
        self.assertEqual(summary.results[0].supplements_status, "none")

    def test_disabled_never_queries_object_api(self):
        self.config.include_supplements = False
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)]})
        self.assertEqual(summary.results[0].supplements_status, "disabled")
        self.assertEqual(len(session.calls), 2)

    def test_discovery_failure_keeps_main_pdf_and_reports_partial(self):
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [response(status=404)]})
        result = summary.results[0]
        self.assertEqual((result.status, result.supplements_status), ("partial", "discovery_failed"))
        self.assertTrue(el.is_valid_pdf(Path(result.file_path)))
        self.assertEqual(self.record(summary)["supplement_discovery"]["status"], "failed")

    def test_inventory_wrong_identity_never_downloads_attachment(self):
        item = attachment()
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)],
                                         OBJECTS: [inventory([item], pii="S1385894724012345")]})
        self.assertEqual(summary.results[0].supplements_status, "discovery_failed")
        self.assertEqual(len(session.calls), 3)

    def test_inventory_other_article_object_or_wrong_ref_rejected(self):
        for url in ("https://api.elsevier.com/content/object/eid/1-s2.0-S1385894724012345-mmc1.docx",
                    f"https://api.elsevier.com/content/object/pii/{PII}/ref/gr1"):
            with self.subTest(url=url):
                item = attachment(url=url)
                routes = {META_URL: [metadata()], OBJECTS: [inventory([item])]}
                if not list(self.output.rglob("article.pdf")):
                    routes[ENDPOINT] = [response(PDF)]
                summary, session = self.run_fake(routes)
                self.assertEqual(summary.results[0].supplements_status, "discovery_failed")
                self.assertFalse(any(request.url == url for request, _ in session.calls))

    def test_original_mmc_preferred_over_earlier_downsampled_variant(self):
        raw = b"\x89PNG\r\n\x1a\noriginal image bytes"
        preview = b"\x89PNG\r\n\x1a\npreview"
        downsampled = attachment("mmc1", "mmc1_small.png", "IMAGE-MMC-DOWNSAMPLED", "image/png", preview)
        original = attachment("mmc1", "mmc1.png", "IMAGE-MMC", "image/png", raw)
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory([downsampled, original])],
                                         original["prism:url"]: [response(raw, content_type="image/png")]})
        self.assertEqual(summary.results[0].supplements_total, 1)
        self.assertEqual(self.record(summary)["supplements"][0]["object_type"], "IMAGE-MMC")
        self.assertEqual(len(session.calls), 4)

    def test_xml_inventory_and_old_http_api_url_supported(self):
        item = attachment(url=f"http://api.elsevier.com/content/object/pii/{PII}/ref/mmc1")
        xml = (f'<attachment-metadata-response xmlns:p="http://prismstandard.org/namespaces/basic/2.0/" xmlns:d="http://purl.org/dc/elements/1.1/">'
               f'<coredata><p:doi>{DOI}</p:doi><d:identifier>PII:{PII}</d:identifier></coredata>'
               f'<attachment><ref>mmc1</ref><filename>mmc1.docx</filename><mimetype>application/word</mimetype>'
               f'<type>APPLICATION</type><size>{len(docx_bytes())}</size><p:url>{item["prism:url"]}</p:url></attachment></attachment-metadata-response>').encode()
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [response(xml, content_type="text/xml")],
                                   item["prism:url"].replace("http:", "https:"): [response(docx_bytes())]})
        self.assertEqual(summary.results[0].supplements_downloaded, 1)

    def test_missing_advertised_url_uses_only_observed_ref_endpoint(self):
        item = attachment()
        item.pop("prism:url")
        url = el._object_endpoint(ArticleReference(pii=PII)) + "/ref/mmc1"
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory([item])], url: [response(docx_bytes())]})
        self.assertEqual(summary.results[0].supplements_downloaded, 1)
        self.assertEqual(len(session.calls), 4)

    def test_pdf_denied_but_supplement_success_is_partial(self):
        item = attachment()
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(status=403)], OBJECTS: [inventory([item])],
                                   item["prism:url"]: [response(docx_bytes())]})
        self.assertEqual(summary.results[0].status, "partial")
        self.assertEqual(summary.results[0].file_path, "")
        self.assertEqual(summary.results[0].supplements_downloaded, 1)

    def test_one_failed_attachment_does_not_drop_later_valid_file(self):
        bad = attachment("mmc1", "mmc1.pdf", mime="application/pdf", body=PDF)
        good = attachment("mmc2", "mmc2.txt", mime="text/plain", body=b"real data")
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory([bad, good])],
                                   bad["prism:url"]: [response(status=403)], good["prism:url"]: [response(b"real data", content_type="text/plain")]})
        result = summary.results[0]
        self.assertEqual((result.status, result.supplements_status), ("partial", "partial"))
        self.assertEqual((result.supplements_downloaded, result.supplements_failed), (1, 1))
        self.assertEqual([item["status"] for item in self.record(summary)["supplements"]], ["access_denied", "success"])

    def test_json_data_allowed_but_api_error_json_rejected(self):
        data = b'{"measurements":[1,2,3]}'
        good = attachment("mmc1", "mmc1.json", mime="application/json", body=data)
        error = b'{"service-error":{"status":{"statusCode":"AUTHORIZATION_ERROR"}}}'
        bad = attachment("mmc2", "mmc2.json", mime="application/json", body=error)
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory([good, bad])],
                                   good["prism:url"]: [response(data, content_type="application/json")],
                                   bad["prism:url"]: [response(error, content_type="application/json")]})
        self.assertEqual(summary.results[0].supplements_downloaded, 1)
        self.assertEqual(summary.results[0].supplements_failed, 1)

    def test_office_crc_corruption_rejected(self):
        good = docx_bytes()
        corrupt = good.replace(b"<w:body/>", b"<x:body/>")
        item = attachment(body=corrupt)
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory([item])],
                                   item["prism:url"]: [response(corrupt)]})
        self.assertEqual(summary.results[0].supplements_failed, 1)
        self.assertIn("CRC", self.record(summary)["supplements"][0]["error"])
        self.assertEqual(list(self.output.rglob("*.part")), [])

    def test_corrupt_deflate_stream_and_lzma_error_are_normal_file_failures(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("measurements.txt", b"values " * 100)
        broken = bytearray(buffer.getvalue())
        name_length = int.from_bytes(broken[26:28], "little")
        extra_length = int.from_bytes(broken[28:30], "little")
        start = 30 + name_length + extra_length
        broken[start:start + 4] = b"\xff\xff\xff\xff"
        item = attachment("mmc1", "mmc1.zip", mime="application/zip", body=broken)
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory([item])],
                                   item["prism:url"]: [response(bytes(broken), content_type="application/zip")]})
        self.assertEqual(summary.results[0].supplements_failed, 1)
        self.assertEqual(summary.results[0].status, "partial")
        path = self.output / "archive.zip"
        path.write_bytes(docx_bytes())
        with patch.object(zipfile.ZipFile, "open", side_effect=lzma.LZMAError("corrupt stream")):
            with self.assertRaisesRegex(ValueError, "CRC"):
                el._validate_archive(path, ".zip", threading.Event())

    def test_inventory_size_mismatch_rejected(self):
        item = attachment()
        item["size"] = str(len(docx_bytes()) + 10)
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory([item])],
                                   item["prism:url"]: [response(docx_bytes())]})
        self.assertEqual(summary.results[0].supplements_failed, 1)
        self.assertIn("目录不一致", self.record(summary)["supplements"][0]["error"])

    def test_resume_requires_matching_provenance_size_and_sha(self):
        item = attachment()
        first, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory([item])],
                                 item["prism:url"]: [response(docx_bytes())]})
        summary, session = self.run_fake({META_URL: [metadata()], OBJECTS: [inventory([item])]})
        self.assertEqual((summary.results[0].status, summary.results[0].supplements_existing), ("exists", 1))
        self.assertEqual(len(session.calls), 2)
        si = self.record(first)["supplements"][0]
        target = Path(first.results[0].record_path).parent / si["file"]
        target.write_bytes(b"x" * len(docx_bytes()))
        summary, _ = self.run_fake({META_URL: [metadata()], OBJECTS: [inventory([item])], item["prism:url"]: [response(docx_bytes())]})
        self.assertEqual(summary.results[0].supplements_downloaded, 1)

    def test_resume_survives_disabled_run_and_reordered_inventory(self):
        one = attachment("mmc1", "mmc1.txt", mime="text/plain", body=b"one")
        two = attachment("mmc2", "mmc2.txt", mime="text/plain", body=b"two")
        self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory([one, two])],
                       one["prism:url"]: [response(b"one", content_type="text/plain")], two["prism:url"]: [response(b"two", content_type="text/plain")]})
        self.config.include_supplements = False
        self.run_fake({META_URL: [metadata()]})
        self.config.include_supplements = True
        summary, _ = self.run_fake({META_URL: [metadata()], OBJECTS: [inventory([two, one])]})
        self.assertEqual(summary.results[0].supplements_existing, 2)

    def test_quota_mid_supplements_preserves_main_and_completed_asset(self):
        one = attachment("mmc1", "mmc1.txt", mime="text/plain", body=b"one")
        two = attachment("mmc2", "mmc2.txt", mime="text/plain", body=b"two")
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory([one, two])],
                                         one["prism:url"]: [response(b"one", content_type="text/plain", headers={"X-RateLimit-Remaining": "0"})]})
        result = summary.results[0]
        self.assertEqual((result.status, result.supplements_status), ("partial", "partial"))
        self.assertEqual(result.supplements_downloaded, 1)
        self.assertTrue(result.file_path)
        self.assertTrue(summary.stop_reason)
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(self.record(summary)["supplements"][0]["status"], "success")

    def test_cancel_mid_supplement_keeps_main_and_manifest_progress(self):
        event = threading.Event()
        item = attachment()
        streamed = response(docx_bytes())
        def chunks(_size):
            yield docx_bytes()[:50]
            event.set()
            yield docx_bytes()[50:]
        streamed.iter_content = chunks
        summary, _ = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory([item])],
                                   item["prism:url"]: [streamed]}, cancel_event=event)
        result = summary.results[0]
        self.assertEqual((result.status, result.supplements_status), ("cancelled", "cancelled"))
        self.assertTrue(el.is_valid_pdf(Path(result.file_path)))
        self.assertEqual(self.record(summary)["supplements"][0]["status"], "cancelled")
        self.assertEqual(list(self.output.rglob("*.part")), [])

    def test_cdn_supplement_gets_no_api_credentials_or_saved_query(self):
        item = attachment()
        cdn = "https://ars.els-cdn.com/content/image/attachment.docx?signature=temporary"
        summary, session = self.run_fake({META_URL: [metadata()], ENDPOINT: [response(PDF)], OBJECTS: [inventory([item])],
                                         item["prism:url"]: [response(status=303, headers={"Location": cdn})], cdn: [response(docx_bytes())]})
        self.assertFalse(any(key.lower().startswith("x-els-") for key in session.calls[-1][0].headers))
        saved = Path(summary.results[0].record_path).read_text(encoding="utf-8")
        self.assertNotIn("signature=", saved)
        self.assertNotIn(API_KEY, saved)
        self.assertNotIn(INSTTOKEN, saved)


if __name__ == "__main__":
    unittest.main()
