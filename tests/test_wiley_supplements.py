"""Crossref component discovery, per-file checks and Wiley SI result handling."""
from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from paper_downloader import wiley as wl
from paper_downloader import wiley_supplements as si
from paper_downloader.doi_parser import ArticleReference
from test_wiley import DOI, TOKEN, REFERENCE, ENDPOINT, META, PDF, CDN, FakeSession, metadata, response


XML_URL = si.metadata_url(DOI)
REVERSE_URL = si.relations_url(DOI)
SI_URL = f"https://onlinelibrary.wiley.com/doi/suppl/{DOI}/supinfo/research-sup-0001.docx"


def docx():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
    return output.getvalue()


def component(doi=DOI + ".s1", title="Supporting information", url=SI_URL, description="", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
    return {"doi": doi, "title": title, "url": url, "description": description, "mime": mime}


def xml_response(items=(), parent=DOI, extra_reference=None):
    root = ET.Element("crossref_result", {"xmlns": "http://www.crossref.org/qrschema/3.0"})
    content = ET.SubElement(root, "crossref", {"xmlns": "http://www.crossref.org/xschema/1.1"})
    article = ET.SubElement(ET.SubElement(content, "journal"), "journal_article")
    data = ET.SubElement(article, "doi_data")
    ET.SubElement(data, "doi").text = parent
    ET.SubElement(data, "resource").text = "https://onlinelibrary.wiley.com/doi/" + parent
    group = ET.SubElement(article, "component_list")
    for item in items:
        entry = ET.SubElement(group, "component", {"parent_relation": "isPartOf"})
        if item.get("title"):
            ET.SubElement(ET.SubElement(entry, "titles"), "title").text = item["title"]
        if item.get("description"):
            ET.SubElement(entry, "description").text = item["description"]
        if item.get("mime"):
            ET.SubElement(entry, "format", {"mime_type": item["mime"]})
        details = ET.SubElement(entry, "doi_data")
        ET.SubElement(details, "doi").text = item["doi"]
        ET.SubElement(details, "resource").text = item["url"]
    if extra_reference:
        citation = ET.SubElement(ET.SubElement(article, "citation_list"), "citation")
        ET.SubElement(citation, "doi").text = extra_reference
    return response(ET.tostring(root, encoding="utf-8"), content_type="application/xml")


def reverse_response(items=(), total=None):
    return response(json.dumps({"status": "ok", "message": {"items": list(items), "total-results": len(items) if total is None else total}}).encode(), content_type="application/json")


def core_response(core):
    return response(json.dumps({"status": "ok", "message": core}).encode(), content_type="application/json")


def child_core(doi=DOI + ".s1", title="Supporting Information", url=SI_URL, parent=DOI, kind="is-supplement-to", work_type="component"):
    return {"DOI": doi, "title": [title], "publisher": "Wiley", "member": "311", "type": work_type,
            "link": [{"URL": url, "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}],
            "relation": {kind: [{"id-type": "doi", "id": parent}]}}


class WileySupplementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.config = wl.WileyConfig("", self.output, api_key=TOKEN, retries=0)
        self.now = 0.0
        self.enterContext(patch.object(wl.time, "monotonic", side_effect=lambda: self.now))
        self.enterContext(patch.object(wl, "_wait", side_effect=self.virtual_wait))

    def virtual_wait(self, event, seconds):
        wl._check_cancel(event)
        self.now += max(0.0, seconds)

    def run_fake(self, routes, records=None, **kwargs):
        session = FakeSession(routes)
        with patch.object(wl.requests, "Session", return_value=session):
            summary = wl.run_wiley_batch(records or [REFERENCE], self.config, **kwargs)
        return summary, session

    def base(self, items=()):
        return {META: [metadata()], ENDPOINT: [response(PDF)], XML_URL: [xml_response(items)], REVERSE_URL: [reverse_response()]}

    def record(self, summary):
        return json.loads(Path(summary.results[0].record_path).read_text(encoding="utf-8"))

    def test_xml_si_docx_download_preserves_pdf_and_uses_no_token(self):
        routes = self.base([component()])
        routes[SI_URL] = [response(docx(), content_type="application/octet-stream")]
        summary, session = self.run_fake(routes)
        result = summary.results[0]
        self.assertEqual((result.status, result.supplements_status, result.supplements_downloaded), ("success", "complete", 1))
        self.assertTrue(wl.is_valid_pdf(Path(result.file_path)))
        self.assertEqual(session.send_times, [0, 10, 20, 30, 40])
        self.assertTrue(all("Wiley-TDM-Client-Token" not in request.headers for request, _ in session.calls if request.url != ENDPOINT))
        asset = self.record(summary)["supplements"][0]
        self.assertEqual(asset["bytes"], len(docx()))
        self.assertTrue(asset["sha256"])
        with zipfile.ZipFile(Path(result.record_path).parent / asset["file"]) as archive:
            self.assertIsNone(archive.testzip())

    def test_no_crossref_record_is_not_indexed_not_no_attachments(self):
        summary, session = self.run_fake(self.base())
        self.assertEqual((summary.results[0].status, summary.results[0].supplements_status), ("partial", "not_indexed"))
        self.assertIn("不能据此确认", summary.results[0].error)
        self.assertEqual(len(session.calls), 4)

    def test_article_links_figures_and_references_are_not_si(self):
        item = component(doi=DOI + ".f1", title="Figure 1", url="https://onlinelibrary.wiley.com/images/figure1.jpg", mime="image/jpeg")
        routes = self.base()
        routes[XML_URL] = [xml_response([item], extra_reference="10.1002/unrelated.s1")]
        summary, session = self.run_fake(routes)
        self.assertEqual(summary.results[0].supplements_status, "not_indexed")
        self.assertEqual(len(session.calls), 4)

    def test_supplement_url_context_without_title_is_manual_for_unknown_host(self):
        item = component(title="", url=f"http://www.bioone.org/doi/suppl/{DOI}/suppl_file/si.docx")
        summary, session = self.run_fake(self.base([item]))
        self.assertEqual(summary.results[0].supplements_status, "manual_required")
        self.assertEqual(summary.results[0].supplements_total, 1)
        asset = self.record(summary)["supplements"][0]
        self.assertIn("bioone.org", asset["url"])
        self.assertEqual(len(session.calls), 4)

    def test_landing_page_is_manual_and_never_fetched(self):
        item = component(url="https://onlinelibrary.wiley.com/doi/" + DOI + ".s1")
        summary, session = self.run_fake(self.base([item]))
        self.assertEqual(summary.results[0].supplements_status, "manual_required")
        self.assertEqual(len(session.calls), 4)

    def test_xml_wrong_parent_or_invalid_component_doi_is_discovery_failure(self):
        for body in (xml_response([component()], parent="10.1002/unrelated"), xml_response([component(doi=DOI)]), xml_response([component(doi="")])):
            routes = self.base()
            routes[XML_URL] = [body]
            summary, session = self.run_fake(routes)
            self.assertEqual(summary.results[0].status, "partial")
            self.assertEqual(summary.results[0].supplements_status, "discovery_failed")
            self.assertFalse(any(request.url == SI_URL for request, _ in session.calls))

    def test_xml_access_error_does_not_become_not_indexed(self):
        routes = self.base()
        routes[XML_URL] = [response(status=404)]
        summary, _ = self.run_fake(routes)
        self.assertEqual(summary.results[0].supplements_status, "discovery_failed")
        self.assertTrue(Path(summary.results[0].file_path).exists())

    def test_action_link_public_doi_file_identifies_two_distinct_files(self):
        first = f"https://onlinelibrary.wiley.com/action/downloadSupplement?doi={DOI}&file=si1.docx&signature=secret-one"
        second = f"https://onlinelibrary.wiley.com/action/downloadSupplement?doi={DOI}&file=si2.docx&signature=secret-two"
        routes = self.base([component(url=first), component(doi=DOI + ".s2", url=second)])
        routes[first] = [response(docx())]
        routes[second] = [response(docx())]
        summary, _ = self.run_fake(routes)
        self.assertEqual(summary.results[0].supplements_downloaded, 2)
        assets = self.record(summary)["supplements"]
        self.assertNotEqual(assets[0]["asset_key"], assets[1]["asset_key"])
        self.assertNotEqual(assets[0]["file"], assets[1]["file"])
        for asset in assets:
            fields = parse_qs(urlsplit(asset["url"]).query)
            self.assertEqual(set(fields), {"doi", "file"})
            self.assertEqual(fields["doi"], [DOI])
            self.assertTrue(asset["file"].endswith(".docx"))
        serialized = Path(summary.results[0].record_path).read_text(encoding="utf-8")
        self.assertNotIn("secret-one", serialized)
        self.assertNotIn("secret-two", serialized)

    def test_action_wrong_parent_and_suppl_wrong_parent_not_downloaded(self):
        for url in ("https://onlinelibrary.wiley.com/action/downloadSupplement?doi=10.1002/other&file=si.docx",
                    "https://onlinelibrary.wiley.com/doi/suppl/10.1002/other/supinfo/si.docx"):
            summary, session = self.run_fake(self.base([component(url=url)]))
            self.assertEqual(summary.results[0].supplements_status, "manual_required")
            self.assertFalse(any(request.url == url for request, _ in session.calls))

    def test_reverse_relation_parent_and_si_classification(self):
        unrelated = child_core(doi=DOI + ".bad", parent="10.1002/other")
        figure = child_core(doi=DOI + ".f1", title="Figure 1", kind="is-part-of", url="https://onlinelibrary.wiley.com/fig1.png")
        reference = child_core(doi=DOI + ".ref", kind="references")
        routes = self.base()
        routes[REVERSE_URL] = [reverse_response([unrelated, figure, reference, child_core()])]
        routes[SI_URL] = [response(docx())]
        summary, session = self.run_fake(routes)
        self.assertEqual(summary.results[0].supplements_total, 1)
        self.assertEqual(summary.results[0].supplements_downloaded, 1)
        self.assertEqual(len(session.calls), 5)

    def test_reverse_truncated_response_keeps_partial(self):
        routes = self.base()
        routes[REVERSE_URL] = [reverse_response([child_core()], total=101)]
        routes[SI_URL] = [response(docx())]
        summary, _ = self.run_fake(routes)
        self.assertEqual(summary.results[0].supplements_downloaded, 1)
        self.assertEqual((summary.results[0].status, summary.results[0].supplements_status), ("partial", "partial"))
        self.assertIn("不完整", summary.results[0].error)

    def test_related_protocol_or_preprint_is_manual_even_with_pdf_link(self):
        related = DOI + ".protocol"
        core = {"DOI": DOI, "publisher": "Wiley", "member": "311", "title": ["Research"],
                "relation": {"is-supplemented-by": [{"id-type": "doi", "id": related}]}}
        child = child_core(doi=related, title="Study protocol", work_type="journal-article", url="https://onlinelibrary.wiley.com/protocol.pdf")
        routes = self.base()
        routes[META] = [core_response(core)]
        routes[wl._metadata_endpoint(related)] = [core_response(child)]
        summary, session = self.run_fake(routes)
        self.assertEqual(summary.results[0].supplements_status, "manual_required")
        self.assertFalse(any(request.url.endswith("protocol.pdf") for request, _ in session.calls))

    def test_related_doi_not_in_crossref_is_manual_not_discovery_failure(self):
        related = "10.17504/protocols.io.example"
        core = {"DOI": DOI, "publisher": "Wiley", "member": "311", "relation": {"is-supplemented-by": [{"id-type": "doi", "id": related}]}}
        routes = self.base()
        routes[META] = [core_response(core)]
        routes[wl._metadata_endpoint(related)] = [response(status=404)]
        summary, _ = self.run_fake(routes)
        self.assertEqual(summary.results[0].supplements_status, "manual_required")
        self.assertEqual(self.record(summary)["supplements"][0]["component_doi"], related)

    def test_related_uri_is_visible_manual_without_external_request(self):
        core = {"DOI": DOI, "publisher": "Wiley", "member": "311", "relation": {"is-supplemented-by": [{"id-type": "uri", "id": "https://repository.example/record/123"}]}}
        routes = self.base()
        routes[META] = [core_response(core)]
        summary, session = self.run_fake(routes)
        self.assertEqual(summary.results[0].supplements_status, "manual_required")
        self.assertEqual(len(session.calls), 4)
        self.assertIn("repository.example", self.record(summary)["supplements"][0]["url"])

    def test_related_component_landing_and_binary_do_not_create_manual_duplicate(self):
        related = DOI + ".s1"
        core = {"DOI": DOI, "publisher": "Wiley", "member": "311", "relation": {"is-supplemented-by": [{"id-type": "doi", "id": related}]}}
        child = child_core()
        child["resource"] = {"primary": {"URL": "https://onlinelibrary.wiley.com/doi/" + related}}
        routes = self.base()
        routes[META] = [core_response(core)]
        routes[wl._metadata_endpoint(related)] = [core_response(child)]
        routes[SI_URL] = [response(docx())]
        summary, _ = self.run_fake(routes)
        self.assertEqual(summary.results[0].supplements_total, 1)
        self.assertEqual(summary.results[0].supplements_status, "complete")

    def test_pdf_denied_still_downloads_si_and_is_partial(self):
        routes = self.base([component()])
        routes[ENDPOINT] = [response(status=403)]
        routes[SI_URL] = [response(docx())]
        summary, _ = self.run_fake(routes)
        result = summary.results[0]
        self.assertEqual((result.status, result.supplements_status, result.supplements_downloaded), ("partial", "complete", 1))
        self.assertEqual(result.file_path, "")

    def test_bad_si_does_not_drop_next_si_or_pdf(self):
        bad = component()
        good_url = SI_URL.replace("0001.docx", "0002.txt")
        good = component(doi=DOI + ".s2", url=good_url, mime="text/plain")
        routes = self.base([bad, good])
        routes[SI_URL] = [response(b"broken docx")]
        routes[good_url] = [response(b"supplement data", content_type="text/plain")]
        summary, _ = self.run_fake(routes)
        result = summary.results[0]
        self.assertEqual((result.status, result.supplements_failed, result.supplements_downloaded), ("partial", 1, 1))
        self.assertTrue(Path(result.file_path).exists())

    def test_json_error_masquerading_as_text_rejected_but_measurement_error_allowed(self):
        for content, expected in ((b'{"error":"Access denied"}', "partial"), (b'{"error":[0.1,0.2],"value":[1,2]}', "success")):
            url = SI_URL.replace("docx", "txt")
            routes = self.base([component(url=url, mime="text/plain")])
            routes[url] = [response(content, content_type="application/json")]
            summary, _ = self.run_fake(routes)
            self.assertEqual(summary.results[0].status, expected)

    def test_html_empty_length_and_oversize_are_rejected(self):
        cases = [response(b"<html>Login</html>"), response(b""), response(docx(), headers={"Content-Length": "1"}),
                 response(docx(), headers={"Content-Length": str(151 * 1024 * 1024)})]
        for content in cases:
            routes = self.base([component()])
            routes[SI_URL] = [content]
            summary, _ = self.run_fake(routes)
            self.assertEqual(summary.results[0].status, "partial")
            self.assertEqual(summary.results[0].supplements_failed, 1)
            self.assertFalse(list(self.output.rglob("*.part")))

    def test_scientific_xml_nested_error_allowed_but_root_service_error_rejected(self):
        cases = [(b'<measurements><value>1</value><error>0.1</error></measurements>', "success"),
                 (b'<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>', "partial")]
        for index, (content, expected) in enumerate(cases):
            url = SI_URL.replace("0001.docx", f"xml{index}.xml")
            routes = self.base([component(doi=DOI + f".xml{index}", url=url, mime="application/xml")])
            routes[url] = [response(content, content_type="application/xml")]
            summary, _ = self.run_fake(routes)
            self.assertEqual(summary.results[0].status, expected)

    def test_si_redirect_to_api_never_sends_token_and_alm_path_not_stored(self):
        redirected_api = "https://api.wiley.com/supplement-resource"
        routes = self.base([component()])
        routes[SI_URL] = [response(status=302, headers={"Location": redirected_api})]
        routes[redirected_api] = [response(status=302, headers={"Location": CDN})]
        routes[CDN] = [response(docx())]
        summary, session = self.run_fake(routes)
        self.assertEqual(summary.results[0].status, "success")
        for request, _kwargs in session.calls:
            if request.url in {SI_URL, redirected_api, CDN}:
                self.assertNotIn("Wiley-TDM-Client-Token", request.headers)
        text = Path(summary.results[0].record_path).read_text(encoding="utf-8")
        self.assertNotIn("temporary-path-token", text)

    def test_resume_preserves_pdf_and_si_and_repairs_tampered_si(self):
        routes = self.base([component()])
        routes[SI_URL] = [response(docx())]
        first, _ = self.run_fake(routes)
        target_pdf = Path(first.results[0].file_path)
        pdf_mtime = target_pdf.stat().st_mtime_ns
        routes = self.base([component()])
        del routes[ENDPOINT]
        resumed, session = self.run_fake(routes)
        self.assertEqual((resumed.results[0].status, resumed.results[0].supplements_existing), ("exists", 1))
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(target_pdf.stat().st_mtime_ns, pdf_mtime)
        asset = self.record(resumed)["supplements"][0]
        target_si = target_pdf.parent / asset["file"]
        target_si.write_bytes(b"corrupt")
        routes = self.base([component()])
        del routes[ENDPOINT]
        routes[SI_URL] = [response(docx())]
        repaired, _ = self.run_fake(routes)
        self.assertEqual(repaired.results[0].supplements_downloaded, 1)
        self.assertEqual(target_si.read_bytes(), docx())

    def test_cancel_after_first_si_preserves_completed_asset_for_resume(self):
        event = threading.Event()
        next_url = SI_URL.replace("0001", "0002")
        streamed = response()
        def chunks(_size):
            yield b"PK\x03\x04partial"
            event.set()
            yield b"more"
        streamed.iter_content = chunks
        routes = self.base([component(), component(doi=DOI + ".s2", url=next_url)])
        routes[SI_URL] = [response(docx())]
        routes[next_url] = [streamed]
        summary, _ = self.run_fake(routes, cancel_event=event)
        self.assertTrue(summary.cancelled)
        self.assertEqual(summary.results[0].supplements_status, "cancelled")
        self.assertEqual(summary.results[0].supplements_downloaded, 1)
        record = self.record(summary)
        self.assertEqual([item["status"] for item in record["supplements"]], ["success", "cancelled"])
        self.assertEqual(len(record["assets"]), 1)
        self.assertFalse(list(self.output.rglob("*.part")))

    def test_limit_during_si_discovery_keeps_completed_pdf(self):
        routes = self.base()
        routes[XML_URL] = [response(status=429, headers={"Retry-After": "120"})]
        summary, _ = self.run_fake(routes)
        self.assertTrue(summary.stop_reason)
        self.assertEqual((summary.results[0].status, summary.results[0].supplements_status), ("partial", "discovery_failed"))
        self.assertTrue(Path(summary.results[0].file_path).exists())

    def test_disabled_si_preserves_completed_attachment_provenance(self):
        routes = self.base([component()])
        routes[SI_URL] = [response(docx())]
        self.run_fake(routes)
        self.config.include_supplements = False
        summary, session = self.run_fake({META: [metadata()]})
        self.assertEqual(summary.results[0].supplements_status, "disabled")
        self.assertEqual(len(self.record(summary)["assets"]), 1)
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
