"""Exclude only completed library entries, with existing integrity checks."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from paper_downloader import elsevier as el
from paper_downloader.doi_parser import ArticleReference
import test_elsevier as fixtures


class CompletedReferenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("requests.Session.send", side_effect=AssertionError("No publisher request is needed")))

    def completed(self, reference, *, format="xml", status="success", verified=True):
        directory = self.root / ("elsevier_" + el.paper_directory_name(reference.key))
        directory.mkdir(exist_ok=True)
        body = fixtures.PDF if format == "pdf" else fixtures.full_xml(doi=reference.doi)
        target = directory / ("article." + format)
        target.write_bytes(body)
        asset = {"url": el._endpoint(reference), "file": target.name, "status": "success", "bytes": len(body),
                 "sha256": hashlib.sha256(body).hexdigest()}
        if format == "pdf": asset["full_text_verified"] = verified
        else: asset["view"] = "FULL"
        record = {"publisher": "Elsevier", "doi": reference.doi, "pii": reference.pii, "status": status,
                  "supplements_status": "disabled", "supplements": [], format: asset}
        path = directory / "manifest.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return target, path, record

    def test_completed_pdf_and_xml_are_excluded_without_network(self):
        references = [ArticleReference(doi=f"10.1016/paper-{index}") for index in range(3)]
        self.completed(references[0])
        self.completed(references[1], format="pdf")
        remaining, count = el.exclude_downloaded_references(references, self.root)
        self.assertEqual(remaining, references[2:])
        self.assertEqual(count, 2)

    def test_doi_and_pii_match_the_same_article(self):
        saved = ArticleReference(doi=fixtures.DOI, pii=fixtures.PII)
        self.completed(saved)
        remaining, count = el.exclude_downloaded_references([ArticleReference(pii=fixtures.PII)], self.root)
        self.assertEqual((remaining, count), ([], 1))
        conflicting = ArticleReference(doi="10.1016/other", pii=fixtures.PII)
        self.assertEqual(el.exclude_downloaded_references([conflicting], self.root), ([conflicting], 0))

    def test_failed_partial_and_unverified_pdf_remain_eligible(self):
        for index, status in enumerate(("failed", "access_denied", "partial", "cancelled")):
            reference = ArticleReference(doi=f"10.1016/failed-{index}")
            self.completed(reference, status=status)
            self.assertEqual(el.exclude_downloaded_references([reference], self.root), ([reference], 0))
        reference = ArticleReference(doi="10.1016/legacy-pdf")
        self.completed(reference, format="pdf", verified=False)
        self.assertEqual(el.exclude_downloaded_references([reference], self.root), ([reference], 0))

    def test_preview_missing_and_same_size_corruption_are_not_completed(self):
        reference = ArticleReference(doi=fixtures.DOI)
        target, path, record = self.completed(reference)
        target.write_bytes(b"x" * target.stat().st_size)
        self.assertEqual(el.exclude_downloaded_references([reference], self.root), ([reference], 0))
        target.unlink()
        self.assertEqual(el.exclude_downloaded_references([reference], self.root), ([reference], 0))
        target, path, record = self.completed(reference, format="pdf")
        record["pdf"]["status"] = "preview"
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(el.exclude_downloaded_references([reference], self.root), ([reference], 0))

    def test_requested_supplements_must_be_complete_and_present(self):
        reference = ArticleReference(doi=fixtures.DOI)
        _, path, record = self.completed(reference)
        self.assertEqual(el.exclude_downloaded_references([reference], self.root, include_supplements=True), ([reference], 0))
        record["supplements_status"] = "complete"
        record["supplements"] = [{"url": "https://ars.els-cdn.com/si.txt", "file": "SI.txt", "status": "success",
                                   "bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}]
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(el.exclude_downloaded_references([reference], self.root, include_supplements=True), ([reference], 0))
        (path.parent / "SI.txt").write_bytes(b"abc")
        self.assertEqual(el.exclude_downloaded_references([reference], self.root, include_supplements=True), ([], 1))

    def test_missing_directory_is_not_created_and_cancel_is_honored(self):
        reference = ArticleReference(doi=fixtures.DOI)
        missing = self.root / "missing"
        self.assertEqual(el.exclude_downloaded_references([reference], missing), ([reference], 0))
        self.assertFalse(missing.exists())
        self.completed(reference)
        event = threading.Event(); event.set()
        with self.assertRaises(el.DownloadCancelled):
            el.exclude_downloaded_references([reference], self.root, cancel_event=event)


if __name__ == "__main__":
    unittest.main()
