"""CLI integration without network or real stored credentials."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import elsevier_batch_download as cli
from paper_downloader.downloader import BatchSummary, DownloadResult


class ElsevierCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.source = self.folder / "input.txt"
        self.source.write_text("10.1007/springer-example\n10.1002/wiley-example\n10.1016/test-example\nhttps://www.sciencedirect.com/science/article/pii/S0000000026000010\n", encoding="utf-8")
        self.args = ["--input", str(self.source), "--output", str(self.folder / "output")]
        self.key = "synthetic-credential-for-tests"
        self.enterContext(patch.object(cli, "load_elsevier_credentials", return_value={"api_key": self.key}))
        self.output = self.enterContext(patch("sys.stdout", new_callable=io.StringIO))

    def test_cli_dispatches_doi_and_pii_with_credentials_without_logging_them(self):
        with patch.object(cli, "run_elsevier_batch", return_value=BatchSummary([], self.folder / "result.csv")) as run:
            self.assertEqual(cli.main(self.args + ["--interval", "3.5"]), 0)
        records, config = run.call_args.args
        self.assertEqual([record.key for record in records], ["10.1016/test-example", "pii:S0000000026000010"])
        self.assertEqual(config.api_key, self.key)
        self.assertEqual(config.interval, 3.5)
        self.assertTrue(config.include_supplements)
        self.assertNotIn(self.key, self.output.getvalue())

    def test_no_supplements_option_reaches_downloader(self):
        with patch.object(cli, "run_elsevier_batch", return_value=BatchSummary([], self.folder / "result.csv")) as run:
            self.assertEqual(cli.main(self.args + ["--no-supplements"]), 0)
        self.assertFalse(run.call_args.args[1].include_supplements)

    def test_invalid_interval_or_missing_credentials_never_starts_network(self):
        with patch.object(cli, "run_elsevier_batch") as run:
            self.assertEqual(cli.main(self.args + ["--interval", "1"]), 2)
            with patch.object(cli, "load_elsevier_credentials", return_value={}):
                self.assertEqual(cli.main(self.args), 2)
            run.assert_not_called()

    def test_quota_pause_and_article_failure_have_distinct_exit_status(self):
        summary = BatchSummary([], self.folder / "result.csv", stop_reason="配额已用尽")
        with patch.object(cli, "run_elsevier_batch", return_value=summary):
            self.assertEqual(cli.main(self.args), 3)
        self.assertIn("配额已用尽", self.output.getvalue())
        summary = BatchSummary([DownloadResult("10.1016/test-example", "access_denied")], self.folder / "result.csv")
        with patch.object(cli, "run_elsevier_batch", return_value=summary):
            self.assertEqual(cli.main(self.args), 1)


if __name__ == "__main__":
    unittest.main()
