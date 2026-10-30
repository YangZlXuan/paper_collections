"""Keyword CLI workflow checks with synthetic metadata and no network."""
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import keyword_batch_download as cli
from paper_downloader.doi_parser import ArticleReference
from paper_downloader.downloader import BatchSummary
from paper_downloader.search import SearchResult


class KeywordCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.result = self.folder / "results.csv"
        self.args = ["--publisher", "springer", "--keywords", "example", "--results", str(self.result)]
        self.springer_key = "1" * 32
        self.elsevier_key = "2" * 32
        self.wiley_key = "00000000-0000-4000-8000-000000000001"
        self.enterContext(patch.object(cli, "load_springer_meta_credentials", return_value={"api_key": self.springer_key}))
        self.enterContext(patch.object(cli, "load_elsevier_credentials", return_value={"api_key": self.elsevier_key, "insttoken": "institution-test"}))
        self.enterContext(patch.object(cli, "load_wiley_credentials", return_value={"api_key": self.wiley_key}))
        self.output = self.enterContext(patch("sys.stdout", new_callable=io.StringIO))
        self.record = ArticleReference(doi="10.1007/example", title="=SUM(1,2)", journal="Example Journal")
        self.search = self.enterContext(patch.object(cli, "search_articles", return_value=SearchResult([self.record], source="test")))
        self.runners = {publisher: self.enterContext(patch.object(cli, name, return_value=BatchSummary([], self.folder / "manifest.csv")))
                        for publisher, name in [("springer", "run_batch"), ("elsevier", "run_elsevier_batch"), ("wiley", "run_wiley_batch")]}

    def test_default_exports_only_and_neutralizes_formulas(self):
        self.assertEqual(cli.main(self.args), 0)
        for run in self.runners.values():
            run.assert_not_called()
        self.assertEqual(self.search.call_args.args[0].api_key, self.springer_key)
        with self.result.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["doi"], self.record.doi)
        self.assertEqual(rows[0]["title"], "'=SUM(1,2)")
        self.assertNotIn(self.springer_key, self.output.getvalue())

    def test_explicit_download_dispatches_all_publishers(self):
        for publisher in self.runners:
            with self.subTest(publisher=publisher):
                args = self.args.copy()
                args[1] = publisher
                args[-1] = str(self.folder / (publisher + ".csv"))
                self.assertEqual(cli.main(args + ["--download", "--output", str(self.folder / "papers"), "--no-supplements"]), 0)
                records, config = self.runners[publisher].call_args.args
                self.assertEqual(records, [self.record.doi] if publisher == "springer" else [self.record])
                self.assertFalse(config.include_supplements)
                if publisher == "elsevier":
                    self.assertEqual(config.api_key, self.elsevier_key)
                    self.assertEqual(config.insttoken, "institution-test")
                if publisher == "wiley":
                    self.assertEqual(config.api_key, self.wiley_key)
                    self.assertEqual(config.interval, 10)
                    self.assertEqual(self.search.call_args.args[0].api_key, "")

    def test_invalid_settings_prevent_search(self):
        for extra in (["--year-from", "2025", "--year-to", "2024"], ["--interval", "1"], ["--max-results", "0"]):
            self.assertEqual(cli.main(self.args + extra), 2)
        with patch.object(cli, "load_springer_meta_credentials", return_value={"api_key": ""}):
            self.assertEqual(cli.main(self.args), 2)
        self.search.assert_not_called()

    def test_existing_export_is_not_overwritten(self):
        self.result.write_text("keep", encoding="utf-8")
        self.assertEqual(cli.main(self.args), 2)
        self.search.assert_not_called()
        self.assertEqual(self.result.read_text(encoding="utf-8"), "keep")

    def test_cancel_exports_partial_results_without_download(self):
        self.search.return_value.cancelled = True
        self.assertEqual(cli.main(self.args + ["--download", "--output", str(self.folder / "papers")]), 130)
        self.assertTrue(self.result.is_file())
        for run in self.runners.values():
            run.assert_not_called()

    def test_wiley_search_does_not_require_download_token(self):
        args = self.args.copy()
        args[1] = "wiley"
        with patch.object(cli, "load_wiley_credentials", return_value={"api_key": ""}) as loader:
            self.assertEqual(cli.main(args), 0)
            loader.assert_not_called()
            args[-1] = str(self.folder / "download.csv")
            self.assertEqual(cli.main(args + ["--download", "--output", str(self.folder / "papers")]), 2)
        self.runners["wiley"].assert_not_called()

    def test_network_exception_message_does_not_expose_key(self):
        self.search.side_effect = RuntimeError("https://api.example/?api_key=" + self.springer_key)
        self.assertEqual(cli.main(self.args), 1)
        self.assertNotIn(self.springer_key, self.output.getvalue())
        self.assertFalse(self.result.exists())


if __name__ == "__main__":
    unittest.main()
