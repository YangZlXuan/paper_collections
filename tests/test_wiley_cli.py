"""CLI integration without live requests or real credentials."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import wiley_batch_download as cli
from paper_downloader.downloader import BatchSummary, DownloadResult


class WileyCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "input.txt"
        self.source.write_text("10.1002/test-wiley\n10.1007/test-springer\n10.1016/test-elsevier\n"
                               "https://www.sciencedirect.com/science/article/pii/S0000000026000010\n", encoding="utf-8")
        self.args = ["--input", str(self.source), "--output", str(self.folder / "output")]
        self.key = "00000000-0000-4000-8000-000000000001"
        self.enterContext(patch.object(cli, "load_wiley_credentials", return_value={"api_key": self.key}))
        self.output = self.enterContext(patch("sys.stdout", new_callable=io.StringIO))

    def test_default_dispatch_and_credentials_are_not_logged(self):
        with patch.object(cli, "run_wiley_batch", return_value=BatchSummary([], self.folder / "result.csv")) as run:
            self.assertEqual(cli.main(self.args), 0)
        records, config = run.call_args.args
        self.assertEqual([record.key for record in records], ["10.1002/test-wiley"])
        self.assertEqual(config.api_key, self.key)
        self.assertEqual(config.interval, 10)
        self.assertTrue(config.include_supplements)
        self.assertNotIn(self.key, self.output.getvalue())

    def test_no_supplements_keeps_pdf_only_mode(self):
        with patch.object(cli, "run_wiley_batch", return_value=BatchSummary([], self.folder / "result.csv")) as run:
            self.assertEqual(cli.main(self.args + ["--no-supplements"]), 0)
        self.assertFalse(run.call_args.args[1].include_supplements)

    def test_invalid_interval_or_missing_key_never_starts_download(self):
        with patch.object(cli, "run_wiley_batch") as run:
            self.assertEqual(cli.main(self.args + ["--interval", "2"]), 2)
            with patch.object(cli, "load_wiley_credentials", return_value={"api_key": ""}):
                self.assertEqual(cli.main(self.args), 2)
            run.assert_not_called()

    def test_filters_and_exit_codes(self):
        with patch.object(cli, "run_wiley_batch", return_value=BatchSummary([], self.folder / "result.csv")) as run:
            self.assertEqual(cli.main(self.args + ["--interval", "20", "--journal", "Advanced Materials"]), 0)
            self.assertEqual(run.call_args.args[1].interval, 20)
            self.assertEqual(run.call_args.args[1].journal, "Advanced Materials")
        cases = [(BatchSummary([], self.folder / "result.csv", stop_reason="限流暂停"), 3),
                 (BatchSummary([], self.folder / "result.csv", cancelled=True), 130),
                 (BatchSummary([DownloadResult("10.1002/test-wiley", "access_denied")], self.folder / "result.csv"), 1)]
        for summary, code in cases:
            with patch.object(cli, "run_wiley_batch", return_value=summary):
                self.assertEqual(cli.main(self.args), code)


if __name__ == "__main__":
    unittest.main()
