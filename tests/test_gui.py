"""Offline UI workflow checks; no network or real user settings are touched."""

from pathlib import Path
import csv
import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from paper_downloader import gui
from paper_downloader.downloader import BatchSummary, DownloadResult


class GuiWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "dois.txt"
        self.source.write_text("10.1007/test-a\n10.1007/test-a\n10.1007/test-b\n", encoding="utf-8")
        self.load_patch = patch.object(gui, "load_settings", return_value={})
        self.save_patch = patch.object(gui, "save_settings")
        self.error_patch = patch.object(gui.messagebox, "showerror")
        self.credential_load_patch = patch.object(gui, "load_elsevier_credentials", return_value={"api_key": "", "insttoken": "", "remembered": False})
        self.credential_save_patch = patch.object(gui, "save_elsevier_credentials")
        self.credential_clear_patch = patch.object(gui, "clear_elsevier_credentials")
        self.wiley_credential_load_patch = patch.object(gui, "load_wiley_credentials", return_value={"api_key": ""})
        self.load_patch.start()
        self.saved = self.save_patch.start()
        self.errors = self.error_patch.start()
        self.credential_load = self.credential_load_patch.start()
        self.credential_save = self.credential_save_patch.start()
        self.credential_clear = self.credential_clear_patch.start()
        self.wiley_credential_load = self.wiley_credential_load_patch.start()
        self.addCleanup(self.load_patch.stop)
        self.addCleanup(self.save_patch.stop)
        self.addCleanup(self.error_patch.stop)
        self.addCleanup(self.credential_load_patch.stop)
        self.addCleanup(self.credential_save_patch.stop)
        self.addCleanup(self.credential_clear_patch.stop)
        self.addCleanup(self.wiley_credential_load_patch.stop)
        try:
            self.app = gui.DownloaderApp()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.app.withdraw()
        self.app.email_var.set("")
        self.app.cookie_var.set("")
        self.app.output_var.set(str(self.folder / "output"))
        self.addCleanup(self._destroy)

    def _destroy(self):
        self.app._cancel.set()
        worker = self.app._worker
        if worker is not None:
            worker.join(timeout=3)
        try:
            self.app.destroy()
        except tk.TclError:
            pass

    def wait_for(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if condition():
                return
            time.sleep(0.01)
        self.fail("GUI worker did not finish within the test timeout")

    def parse(self):
        self.app.file_var.set(str(self.source))
        self.app._parse_file()
        self.assertTrue(self.app._busy)
        self.wait_for(lambda: not self.app._busy)
        self.assertIsNotNone(self.app._parsed)
        self.assertEqual(len(self.app._parsed.dois), 2)
        self.assertFalse(self.errors.called)

    def test_parse_to_download_passes_filters_and_shows_results(self):
        self.parse()
        self.assertEqual(str(self.app.start_button.cget("state")), "normal")
        selected_journal = self.app._selected_publisher().journals[0]
        self.app.journal_var.set(selected_journal)
        captured = []

        def fake_batch(dois, config, on_result, on_log, cancel_event):
            captured.append((dois, config, threading.get_ident()))
            config.output_dir.mkdir()
            manifest = config.output_dir / "manifest.csv"
            manifest.write_text("doi,status\n", encoding="utf-8")
            results = [
                DownloadResult(dois[0], "success", journal=selected_journal, title="A paper"),
                DownloadResult(dois[1], "no_pdf", journal=selected_journal),
            ]
            for index, result in enumerate(results, 1):
                on_result(index, len(dois), result)
            on_log("Offline test batch")
            return BatchSummary(results, manifest)

        with patch.object(gui, "run_batch", side_effect=fake_batch) as batch:
            self.app._start_download()
            self.assertTrue(self.app._busy)
            self.assertEqual(str(self.app.file_entry.cget("state")), "disabled")
            self.assertEqual(str(self.app.interval_combo.cget("state")), "disabled")
            self.wait_for(lambda: not self.app._busy)
        batch.assert_called_once()
        self.assertEqual(captured[0][1].journal, selected_journal)
        self.assertEqual(captured[0][1].publisher_id, "springer")
        self.assertEqual(captured[0][1].email, "")
        self.assertIsNone(captured[0][1].cookie_file)
        self.assertTrue(captured[0][1].include_supplements)
        self.assertEqual(captured[0][1].interval, 2)
        self.assertNotEqual(captured[0][2], threading.get_ident())
        self.assertEqual(self.app._counts["success"], 1)
        self.assertEqual(self.app._counts["no_pdf"], 1)
        self.assertEqual(self.app.progress_var.get(), 100)
        self.assertEqual(str(self.app.manifest_button.cget("state")), "normal")
        self.saved.assert_called_once()
        self.assertFalse(self.errors.called)

    def test_custom_interval_keeps_parsed_dois_and_is_passed_and_saved(self):
        self.assertEqual(self.app.interval_var.get(), "2")
        self.parse()
        parsed = self.app._parsed
        self.app.interval_var.set("2.75")
        self.assertIs(self.app._parsed, parsed)
        self.assertEqual(str(self.app.start_button.cget("state")), "normal")
        captured = []

        def fake_batch(dois, config, on_result, on_log, cancel_event):
            captured.append(config)
            manifest = self.folder / "interval.csv"
            manifest.write_text("doi,status\n", encoding="utf-8")
            return BatchSummary([], manifest)

        with patch.object(gui, "run_batch", side_effect=fake_batch):
            self.app._start_download()
            self.assertEqual(str(self.app.interval_combo.cget("state")), "disabled")
            self.wait_for(lambda: not self.app._busy)
        self.assertEqual(captured[0].interval, 2.75)
        self.assertEqual(self.saved.call_args.args[0]["request_interval"], "2.75")
        self.assertEqual(str(self.app.interval_combo.cget("state")), "normal")
        self.assertIn("间隔 2.75 秒", self.app.log_text.get("1.0", "end"))
        self.assertFalse(self.errors.called)

    def test_invalid_intervals_are_rejected_before_worker_starts(self):
        self.parse()
        parsed = self.app._parsed
        for value in ("1.9", "nan", "fast", "61", "inf", ""):
            with self.subTest(interval=value):
                self.errors.reset_mock()
                self.app.interval_var.set(value)
                with patch.object(gui, "run_batch") as batch:
                    self.app._start_download()
                batch.assert_not_called()
                self.errors.assert_called_once()
                self.assertFalse(self.app._busy)
                self.assertIs(self.app._parsed, parsed)

    def test_saved_interval_is_restored_or_defaults_when_invalid(self):
        for stored, expected in (("3.5", "3.5"), ("60", "60"), ("1.5", "2"), ("nan", "2"), ("bad", "2")):
            with self.subTest(stored=stored), patch.object(gui, "load_settings", return_value={"request_interval": stored}):
                second_app = gui.DownloaderApp()
                try:
                    second_app.withdraw()
                    self.assertEqual(second_app.interval_var.get(), expected)
                finally:
                    second_app.destroy()
        self.errors.assert_not_called()

    def test_unsupported_publisher_keeps_journals_visible_and_start_disabled(self):
        self.parse()
        unsupported = next(item for item in self.app._publishers if not item.enabled)
        self.app.publisher_var.set(unsupported.name)
        self.app._publisher_changed()
        self.assertEqual(str(self.app.start_button.cget("state")), "disabled")
        self.assertIn("尚未实现", self.app.publisher_hint_var.get())
        self.assertIn(unsupported.journals[0], self.app.journal_combo.cget("values"))

    def test_input_or_column_change_invalidates_parsed_data(self):
        self.parse()
        self.app.column_var.set("Custom DOI")
        self.assertIsNone(self.app._parsed)
        self.assertFalse(self.app.table.get_children())
        self.assertEqual(str(self.app.start_button.cget("state")), "disabled")

    def test_modified_source_requires_reparse_before_download(self):
        self.parse()
        self.source.write_text("10.1007/replacement-longer\n", encoding="utf-8")
        with patch.object(gui, "run_batch") as batch:
            self.app._start_download()
        batch.assert_not_called()
        self.assertIsNone(self.app._parsed)
        self.errors.assert_called_once()

    def test_cancel_is_sent_to_worker_and_leaves_remaining_dois_unprocessed(self):
        self.parse()
        started = threading.Event()

        def fake_batch(dois, config, on_result, on_log, cancel_event):
            started.set()
            if not cancel_event.wait(3):
                raise RuntimeError("Cancellation was not received")
            manifest = self.folder / "cancelled.csv"
            manifest.write_text("doi,status\n", encoding="utf-8")
            return BatchSummary([], manifest, cancelled=True)

        with patch.object(gui, "run_batch", side_effect=fake_batch):
            self.app._start_download()
            self.wait_for(started.is_set)
            self.app._cancel_download()
            self.wait_for(lambda: not self.app._busy)
        self.assertTrue(self.app._cancel.is_set())
        self.assertIn("任务已取消", self.app.status_var.get())
        for row in self.app.table.get_children():
            self.assertEqual(self.app.table.item(row, "values")[1], "未处理")

    def test_arbitrary_journal_search_text_is_not_silently_used_as_filter(self):
        self.parse()
        self.app.journal_var.set("Nature C")
        with patch.object(gui, "run_batch") as batch:
            self.app._start_download()
        batch.assert_not_called()
        self.errors.assert_called_once()
        self.assertFalse(self.app._busy)

    def test_cookie_path_and_supplement_preference_are_passed_and_saved(self):
        self.parse()
        cookie_path = self.folder / "cookies.txt"
        cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        self.app.cookie_var.set(str(cookie_path))
        self.app.supplements_var.set(False)
        captured = []

        def fake_batch(dois, config, on_result, on_log, cancel_event):
            captured.append(config)
            manifest = self.folder / "manifest.csv"
            manifest.write_text("doi,status\n", encoding="utf-8")
            return BatchSummary([], manifest)

        with patch.object(gui, "run_batch", side_effect=fake_batch):
            self.app._start_download()
            self.wait_for(lambda: not self.app._busy)
        self.assertEqual(captured[0].cookie_file, cookie_path)
        self.assertFalse(captured[0].include_supplements)
        saved_settings = self.saved.call_args.args[0]
        self.assertEqual(saved_settings["cookie_file"], str(cookie_path))
        self.assertEqual(saved_settings["include_supplements"], "false")
        self.assertNotIn("Netscape HTTP Cookie File", str(saved_settings))
        self.assertFalse(self.errors.called)

    def test_partial_and_access_denied_results_expose_details_and_records(self):
        self.parse()
        record = self.folder / "result.json"
        record.write_text("{}", encoding="utf-8")
        partial = DownloadResult(
            self.app._parsed.dois[0], "partial", file_path=str(self.folder / "paper.pdf"),
            supplements_total=3, supplements_downloaded=1, supplements_existing=1,
            supplements_failed=1, record_path=str(record), error="Supplement download failed",
        )
        denied = DownloadResult(self.app._parsed.dois[1], "access_denied", error="HTTP 403")
        self.app._render_result(partial)
        self.app._render_result(denied)
        self.assertEqual(self.app._counts["partial"], 1)
        self.assertEqual(self.app._counts["access_denied"], 1)
        row_id = self.app._row_ids[partial.doi]
        self.assertEqual(self.app.table.item(row_id, "values")[2], "2/3（失败 1）")
        self.app.table.selection_set(row_id)
        self.app._show_result_details()
        self.assertIn("Supplement download failed", self.app.detail_var.get())
        self.assertIn("paper.pdf", self.app.detail_var.get())
        self.assertEqual(str(self.app.record_button.cget("state")), "normal")
        with patch.object(self.app, "_open_path") as open_path:
            self.app._open_selected_record()
            open_path.assert_called_once_with(record)

    def test_small_window_preserves_table_and_scrolls_to_progress(self):
        self.app.deiconify()
        self.app.geometry("900x650")
        self.app.update()
        self.assertGreaterEqual(self.app.table.winfo_height(), 95)
        self.assertGreater(self.app._content.winfo_height(), self.app._scroll_canvas.winfo_height())
        self.app._show_progress_panel()
        self.app.update()
        self.assertGreater(self.app._scroll_canvas.yview()[0], 0)
        self.app._show_access_settings()
        self.app.update()
        self.assertIsNotNone(self.app._access_dialog)
        self.assertTrue(self.app._access_dialog.winfo_exists())

    def test_empty_saved_cookie_path_displays_environment_default(self):
        cookie_path = str(self.folder / "environment.cookies")
        with patch.dict(os.environ, {"SN_COOKIE_FILE": cookie_path}), patch.object(gui, "load_settings", return_value={"cookie_file": "", "include_supplements": "false"}):
            second_app = gui.DownloaderApp()
            try:
                second_app.withdraw()
                self.assertEqual(second_app.cookie_var.get(), cookie_path)
                self.assertFalse(second_app.supplements_var.get())
            finally:
                second_app.destroy()

    def import_text(self, text):
        self.app._import_reference_text(text)
        self.assertTrue(self.app._busy)
        self.wait_for(lambda: not self.app._busy)
        self.assertIsNotNone(self.app._parsed)
        self.assertFalse(self.errors.called)

    def test_pii_only_link_is_previewed_and_exported_without_download(self):
        url = "https://www.sciencedirect.com/science/article/pii/S0009250924001234"
        self.import_text(url)
        self.assertEqual(self.app._parsed.dois, [])
        reference = self.app._parsed.records[0]
        self.assertEqual(reference.pii, "S0009250924001234")
        row_id = self.app._row_ids[reference.key]
        self.assertEqual(self.app.table.item(row_id, "values")[1], "仅导入")
        self.assertEqual(str(self.app.start_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.export_button.cget("state")), "normal")
        with patch.object(gui, "run_batch") as batch:
            self.app._start_download()
        batch.assert_not_called()
        self.app.table.selection_set(row_id)
        self.app._show_result_details()
        self.assertIn(reference.pii, self.app.detail_var.get())
        self.assertIn(url, self.app.detail_var.get())
        exported = self.folder / "pii.csv"
        self.assertEqual(self.app._export_references(exported), exported)
        with exported.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["pii"], reference.pii)
        self.assertEqual(rows[0]["url"], reference.url)
        self.assertEqual(rows[0]["doi"], "")
        reimported = gui.parse_doi_file(exported)
        self.assertEqual(reimported.records[0].pii, reference.pii)

    def test_mixed_imports_send_only_eligible_dois_and_keep_elsevier_rows(self):
        self.import_text("10.1007/test-a\n10.1016/j.cej.2024.123456\nhttps://www.sciencedirect.com/science/article/pii/S0009250924001234")
        records = list(self.app._parsed.records)
        self.assertEqual(len(records), 3)
        captured = []

        def fake_batch(dois, config, on_result, on_log, cancel_event):
            captured.append(dois)
            result = DownloadResult(dois[0], "success")
            on_result(1, 1, result)
            manifest = self.folder / "mixed.csv"
            manifest.write_text("doi,status\n", encoding="utf-8")
            return BatchSummary([result], manifest)

        with patch.object(gui, "run_batch", side_effect=fake_batch):
            self.app._start_download()
            self.wait_for(lambda: not self.app._busy)
        self.assertEqual(captured, [["10.1007/test-a"]])
        self.assertEqual(len(self.app.table.get_children()), 3)
        for reference in records[1:]:
            self.assertEqual(self.app.table.item(self.app._row_ids[reference.key], "values")[1], "仅导入")
        self.assertEqual(self.app.progress_text_var.get(), "1 / 1")
        self.assertFalse(self.errors.called)

    def test_cancel_leaves_import_only_records_unchanged(self):
        self.import_text("10.1007/test-a\nhttps://www.sciencedirect.com/science/article/pii/S0009250924001234")
        pii_key = self.app._parsed.records[1].key

        def fake_batch(dois, config, on_result, on_log, cancel_event):
            if not cancel_event.wait(3):
                raise RuntimeError("No cancellation received")
            manifest = self.folder / "mixed-cancel.csv"
            manifest.write_text("doi,status\n", encoding="utf-8")
            return BatchSummary([], manifest, cancelled=True)

        with patch.object(gui, "run_batch", side_effect=fake_batch):
            self.app._start_download()
            self.app._cancel_download()
            self.wait_for(lambda: not self.app._busy)
        self.assertEqual(self.app.table.item(self.app._row_ids[pii_key], "values")[1], "仅导入")
        self.assertEqual(self.app.table.item(self.app._row_ids["10.1007/test-a"], "values")[1], "未处理")

    def test_ris_import_preserves_metadata_through_export_and_reimport(self):
        source = self.folder / "library.ris"
        source.write_text("TY  - JOUR\nDO  - 10.1007/test-ris\nTI  - A useful paper\nJO  - Nano Research\nUR  - https://link.springer.com/article/10.1007/test-ris\nER  -\n", encoding="utf-8")
        self.app.file_var.set(str(source))
        self.app._parse_file()
        self.wait_for(lambda: not self.app._busy)
        reference = self.app._parsed.records[0]
        self.assertEqual(reference.title, "A useful paper")
        self.assertEqual(reference.journal, "Nano Research")
        values = self.app.table.item(self.app._row_ids[reference.key], "values")
        self.assertEqual(values[3:5], ("Nano Research", "A useful paper"))
        exported = self.folder / "metadata.csv"
        self.app._export_references(exported)
        restored = gui.parse_doi_file(exported).records[0]
        self.assertEqual((restored.doi, restored.title, restored.journal, restored.url), (reference.doi, reference.title, reference.journal, reference.url))

    def test_elsevier_publisher_allows_preview_and_export(self):
        elsevier = next(item for item in self.app._publishers if "elsevier" in item.name.lower())
        self.app.publisher_var.set(elsevier.name)
        self.app._publisher_changed()
        self.import_text("10.1016/j.cej.2024.123456")
        self.assertEqual(str(self.app.start_button.cget("state")), "normal")
        with patch.object(gui, "run_elsevier_batch") as batch:
            self.app._start_download()
        batch.assert_not_called()
        self.errors.assert_called_once()
        self.assertEqual(str(self.app.export_button.cget("state")), "normal")
        target = self.folder / "elsevier.csv"
        self.assertEqual(self.app._export_references(target), target)

    def test_new_paste_replaces_file_and_later_file_edit_invalidates_it(self):
        self.parse()
        self.import_text("10.1007/from-paste")
        self.assertEqual(self.app._parsed.dois, ["10.1007/from-paste"])
        self.assertEqual(self.app._source_mode, "paste")
        self.assertTrue(self.app._source_is_current())
        self.assertEqual(len(self.app.table.get_children()), 1)
        self.app.file_var.set(str(self.source))
        self.assertIsNone(self.app._parsed)
        self.assertEqual(str(self.app.export_button.cget("state")), "disabled")
        self.assertFalse(self.app.table.get_children())

    def test_export_rejects_source_overwrite_and_stale_file(self):
        self.parse()
        original = self.source.read_text(encoding="utf-8")
        self.assertIsNone(self.app._export_references(self.source))
        self.assertEqual(self.source.read_text(encoding="utf-8"), original)
        self.errors.assert_called_once()
        self.errors.reset_mock()
        self.source.write_text("10.1007/new-longer-reference\n", encoding="utf-8")
        target = self.folder / "stale.csv"
        self.assertIsNone(self.app._export_references(target))
        self.assertFalse(target.exists())
        self.assertIsNone(self.app._parsed)
        self.errors.assert_called_once()

    def test_paste_dialog_uses_explicit_text_and_does_not_read_clipboard(self):
        with patch.object(self.app, "clipboard_get") as clipboard:
            self.app._show_paste_dialog()
            dialog = self.app._paste_dialog
            frame = dialog.winfo_children()[0]
            editor = next(child for child in frame.winfo_children() if isinstance(child, tk.Text))
            self.assertEqual(editor.get("1.0", "end-1c"), "")
            editor.insert("1.0", "10.1007/dialog-reference")
            button_frame = frame.winfo_children()[-1]
            button = next(child for child in button_frame.winfo_children() if child.cget("text") == "解析并导入")
            button.invoke()
            self.wait_for(lambda: not self.app._busy)
            clipboard.assert_not_called()
        self.assertIsNone(self.app._paste_dialog)
        self.assertEqual(self.app._parsed.dois, ["10.1007/dialog-reference"])

    def select_elsevier(self):
        publisher = next(item for item in self.app._publishers if item.id == "elsevier")
        self.app.publisher_var.set(publisher.name)
        self.app._publisher_changed()
        return publisher

    def test_elsevier_pii_dispatch_maps_resolved_doi_to_original_row(self):
        self.select_elsevier()
        self.app.api_key_var.set("a" * 32)
        self.app.insttoken_var.set("ui-test-insttoken")
        self.import_text("https://www.sciencedirect.com/science/article/pii/S0009250924001234")
        reference = self.app._parsed.records[0]
        captured = []

        def fake_batch(records, config, on_result, on_log, cancel_event):
            captured.append((records, config))
            result = DownloadResult("10.1016/j.test.2024.123", "success", reference_key=reference.key, pii=reference.pii, title="Resolved article", file_path=str(self.folder / "article.pdf"))
            on_result(1, 1, result)
            manifest = self.folder / "elsevier-results.csv"
            manifest.write_text("doi,status\n", encoding="utf-8")
            return BatchSummary([result], manifest)

        with patch.object(gui, "run_elsevier_batch", side_effect=fake_batch) as elsevier_batch, patch.object(gui, "run_batch") as springer_batch:
            self.app._start_download()
            original_row = self.app._row_ids[reference.key]
            self.wait_for(lambda: not self.app._busy)
        elsevier_batch.assert_called_once()
        springer_batch.assert_not_called()
        self.assertEqual(captured[0][0][0].pii, reference.pii)
        self.assertEqual(captured[0][1].api_key, "a" * 32)
        self.assertEqual(captured[0][1].insttoken, "ui-test-insttoken")
        self.assertTrue(captured[0][1].include_supplements)
        self.assertEqual(self.app._row_ids[reference.key], original_row)
        self.assertEqual(len(self.app.table.get_children()), 1)
        self.assertEqual(self.app.table.item(original_row, "values")[0], "10.1016/j.test.2024.123")
        self.app.table.selection_set(original_row)
        self.assertEqual(self.app._selected_result().pii, reference.pii)
        self.assertNotIn("api_key", self.saved.call_args.args[0])
        self.assertNotIn("insttoken", self.saved.call_args.args[0])
        self.assertNotIn("a" * 32, self.app.log_text.get("1.0", "end"))
        self.credential_save.assert_not_called()

    def test_publisher_switch_updates_eligibility_and_preserves_completed_results(self):
        self.import_text("10.1007/test-a\n10.1016/j.test.2024.123\nhttps://www.sciencedirect.com/science/article/pii/S0009250924001234")
        self.app._render_result(DownloadResult("10.1007/test-a", "success"))
        self.app.supplements_var.set(True)
        self.select_elsevier()
        self.assertTrue(self.app.supplements_var.get())
        self.assertEqual(str(self.app.supplements_check.cget("state")), "normal")
        self.app.supplements_var.set(False)
        self.assertEqual(len(self.app._eligible_references()), 2)
        self.assertEqual(self.app.table.item(self.app._row_ids["10.1007/test-a"], "values")[1], "已下载")
        self.assertEqual(self.app.table.item(self.app._row_ids["10.1016/j.test.2024.123"], "values")[1], "待处理")
        springer = next(item for item in self.app._publishers if item.id == "springer")
        self.app.publisher_var.set(springer.name)
        self.app._publisher_changed()
        self.assertTrue(self.app.supplements_var.get())
        self.assertEqual(str(self.app.supplements_check.cget("state")), "normal")
        self.select_elsevier()
        self.assertFalse(self.app.supplements_var.get())

    def test_elsevier_quota_pause_keeps_remaining_queue_unprocessed(self):
        self.select_elsevier()
        self.app.api_key_var.set("a" * 32)
        self.import_text("10.1016/j.test.2024.123\nhttps://www.sciencedirect.com/science/article/pii/S0009250924001234\n10.1007/springer-only")
        captured = []

        def fake_batch(records, config, on_result, on_log, cancel_event):
            captured.extend(records)
            result = DownloadResult(records[0].doi, "access_denied", reference_key=records[0].key, error="Quota exhausted")
            on_result(1, 2, result)
            manifest = self.folder / "quota.csv"
            manifest.write_text("doi,status\n", encoding="utf-8")
            return BatchSummary([result], manifest, stop_reason="API 配额已用完，请稍后继续")

        with patch.object(gui, "run_elsevier_batch", side_effect=fake_batch):
            self.app._start_download()
            self.wait_for(lambda: not self.app._busy)
        self.assertEqual(len(captured), 2)
        self.assertNotIn("10.1007/springer-only", [reference.doi for reference in captured])
        self.assertEqual(self.app.progress_var.get(), 50)
        self.assertEqual(self.app.progress_text_var.get(), "1 / 2")
        self.assertIn("任务已暂停", self.app.status_var.get())
        self.assertEqual(self.app.table.item(self.app._row_ids[captured[1].key], "values")[1], "未处理（暂停）")
        self.assertEqual(self.app.table.item(self.app._row_ids["10.1007/springer-only"], "values")[1], "仅导入")

    def test_api_credentials_are_masked_and_saved_only_by_explicit_dialog_action(self):
        self.app.api_key_var.set("a" * 32)
        self.app.insttoken_var.set("ui-test-insttoken")
        self.app._show_access_settings()
        self.assertEqual(self.app.api_key_entry.cget("show"), "*")
        self.assertEqual(self.app.insttoken_entry.cget("show"), "*")
        self.app._save_preferences()
        self.assertNotIn("a" * 32, str(self.saved.call_args.args[0]))
        self.credential_save.assert_not_called()
        self.credential_clear.assert_not_called()
        self.app.remember_credentials_var.set(True)
        self.assertTrue(self.app._save_api_credentials())
        self.credential_save.assert_called_once_with("a" * 32, "ui-test-insttoken")
        self.app.remember_credentials_var.set(False)
        self.assertTrue(self.app._save_api_credentials())
        self.credential_clear.assert_called_once()
        self.app._log("No credential should appear: " + "a" * 32)
        self.assertNotIn("a" * 32, self.app.log_text.get("1.0", "end"))

    def test_encrypted_credential_load_populates_session_and_remember_flag(self):
        with patch.object(gui, "load_elsevier_credentials", return_value={"api_key": "a" * 32, "insttoken": "loaded-insttoken", "remembered": True}):
            second_app = gui.DownloaderApp()
            try:
                second_app.withdraw()
                self.assertEqual(second_app.api_key_var.get(), "a" * 32)
                self.assertEqual(second_app.insttoken_var.get(), "loaded-insttoken")
                self.assertTrue(second_app.remember_credentials_var.get())
                self.assertNotIn("a" * 32, second_app.log_text.get("1.0", "end"))
            finally:
                second_app.destroy()

    def test_elsevier_si_defaults_true_for_old_settings_and_is_independently_restored(self):
        cases = [
            ({"publisher_id": "elsevier", "include_supplements": "false"}, True, False),
            ({"publisher_id": "elsevier", "include_supplements": "true", "elsevier_include_supplements": "false"}, False, True),
        ]
        for preferences, expected_elsevier, expected_springer in cases:
            with self.subTest(preferences=preferences), patch.object(gui, "load_settings", return_value=preferences):
                second_app = gui.DownloaderApp()
                try:
                    second_app.withdraw()
                    self.assertEqual(second_app.supplements_var.get(), expected_elsevier)
                    self.assertEqual(str(second_app.supplements_check.cget("state")), "normal")
                    springer = next(item for item in second_app._publishers if item.id == "springer")
                    second_app.publisher_var.set(springer.name)
                    second_app._publisher_changed()
                    self.assertEqual(second_app.supplements_var.get(), expected_springer)
                finally:
                    second_app.destroy()

    def test_elsevier_si_preference_saves_independently_and_false_reaches_worker(self):
        self.app.supplements_var.set(True)
        self.select_elsevier()
        self.app.supplements_var.set(False)
        self.app.api_key_var.set("a" * 32)
        self.import_text("10.1016/j.test.2024.123")
        captured = []

        def fake_batch(records, config, on_result, on_log, cancel_event):
            captured.append(config)
            result = DownloadResult(records[0].doi, "success", reference_key=records[0].key, supplements_status="disabled")
            on_result(1, 1, result)
            manifest = self.folder / "no-si.csv"
            manifest.write_text("doi,status\n", encoding="utf-8")
            return BatchSummary([result], manifest)

        with patch.object(gui, "run_elsevier_batch", side_effect=fake_batch):
            self.app._start_download()
            self.assertEqual(str(self.app.supplements_check.cget("state")), "disabled")
            self.wait_for(lambda: not self.app._busy)
        self.assertFalse(captured[0].include_supplements)
        preferences = self.saved.call_args.args[0]
        self.assertEqual(preferences["include_supplements"], "true")
        self.assertEqual(preferences["elsevier_include_supplements"], "false")
        self.assertEqual(str(self.app.supplements_check.cget("state")), "normal")
        self.assertEqual(self.app.table.item(self.app._row_ids["10.1016/j.test.2024.123"], "values")[2], "未启用")

    def test_elsevier_si_counts_and_discovery_failure_are_distinct_from_none(self):
        self.select_elsevier()
        self.import_text("10.1016/j.test.2024.123")
        doi = self.app._parsed.dois[0]
        row_id = self.app._row_ids[doi]
        self.app.table.selection_set(row_id)
        failed_discovery = DownloadResult(doi, "partial", reference_key=doi,
            supplements_status="discovery_failed", file_path=str(self.folder / "article.pdf"),
            error="补充材料发现失败：HTTP 403", record_path=str(self.folder / "manifest.json"))
        self.app._render_result(failed_discovery)
        self.assertEqual(self.app.table.item(row_id, "values")[2], "发现失败")
        self.assertIn("HTTP 403", self.app.detail_var.get())
        self.assertIn("article.pdf", self.app.detail_var.get())
        self.assertEqual(self.app._counts["partial"], 1)
        self.assertEqual(str(self.app.record_button.cget("state")), "normal")
        partly_saved = DownloadResult(doi, "partial", reference_key=doi, supplements_status="partial",
            supplements_total=3, supplements_downloaded=1, supplements_existing=1, supplements_failed=1,
            error="Supplement 3 failed")
        self.app._render_result(partly_saved)
        self.assertEqual(self.app.table.item(row_id, "values")[2], "2/3（失败 1）")
        self.assertIn("Supplement 3 failed", self.app.detail_var.get())
        self.app._render_result(DownloadResult(doi, "success", reference_key=doi, supplements_status="none"))
        self.assertEqual(self.app.table.item(row_id, "values")[2], "无附件")
        self.assertEqual(self.app._counts["partial"], 0)
        self.assertNotEqual(self.app._supplements_text(DownloadResult(doi, "success")), "无附件")

    def select_wiley(self):
        publisher = next(item for item in self.app._publishers if item.id == "wiley")
        self.app.publisher_var.set(publisher.name)
        self.app._publisher_changed()
        return publisher

    def test_wiley_interval_is_independent_and_restores_other_publishers(self):
        self.app.interval_var.set("3.5")
        self.select_wiley()
        self.assertEqual(self.app.interval_var.get(), "10")
        self.assertIn("10", self.app.interval_hint_var.get())
        self.assertTrue(self.app.supplements_var.get())
        self.assertEqual(str(self.app.supplements_check.cget("state")), "normal")
        self.app.interval_var.set("12.5")
        springer = next(item for item in self.app._publishers if item.id == "springer")
        self.app.publisher_var.set(springer.name)
        self.app._publisher_changed()
        self.assertEqual(self.app.interval_var.get(), "3.5")
        self.assertTrue(self.app.supplements_var.get())
        self.select_elsevier()
        self.assertEqual(self.app.interval_var.get(), "3.5")
        self.app.interval_var.set("4")
        self.select_wiley()
        self.assertEqual(self.app.interval_var.get(), "12.5")
        self.app._save_preferences()
        preferences = self.saved.call_args.args[0]
        self.assertEqual(preferences["request_interval"], "4")
        self.assertEqual(preferences["wiley_request_interval"], "12.5")

    def test_wiley_saved_interval_loads_separately_and_rejects_old_short_value(self):
        for value, expected in (("15.5", "15.5"), ("2", "10"), ("nan", "10")):
            with self.subTest(value=value), patch.object(gui, "load_settings", return_value={"publisher_id": "wiley", "request_interval": "3", "wiley_request_interval": value}):
                second_app = gui.DownloaderApp()
                try:
                    second_app.withdraw()
                    self.assertEqual(second_app.interval_var.get(), expected)
                    springer = next(item for item in second_app._publishers if item.id == "springer")
                    second_app.publisher_var.set(springer.name)
                    second_app._publisher_changed()
                    self.assertEqual(second_app.interval_var.get(), "3")
                finally:
                    second_app.destroy()

    def test_wiley_dispatch_excludes_pii_and_known_other_publishers(self):
        self.select_wiley()
        self.app.wiley_api_key_var.set("wiley-ui-test-token")
        self.import_text("10.1002/test-a\n10.1111/test-b\n10.1007/springer\n10.1038/nature\n10.1016/elsevier\nhttps://www.sciencedirect.com/science/article/pii/S0009250924001234")
        captured = []

        def fake_batch(records, config, on_result, on_log, cancel_event):
            captured.append((records, config))
            results = []
            for index, reference in enumerate(records, 1):
                result = DownloadResult(reference.doi, "success", reference_key=reference.key, supplements_status="not_indexed")
                results.append(result)
                on_result(index, len(records), result)
            manifest = self.folder / "wiley.csv"
            manifest.write_text("doi,status\n", encoding="utf-8")
            return BatchSummary(results, manifest)

        with patch.object(gui, "run_wiley_batch", side_effect=fake_batch), patch.object(gui, "run_batch") as springer_batch, patch.object(gui, "run_elsevier_batch") as elsevier_batch:
            self.app._start_download()
            self.assertEqual(str(self.app.interval_combo.cget("state")), "disabled")
            self.wait_for(lambda: not self.app._busy)
        springer_batch.assert_not_called()
        elsevier_batch.assert_not_called()
        self.assertEqual([reference.doi for reference in captured[0][0]], ["10.1002/test-a", "10.1111/test-b"])
        self.assertEqual(captured[0][1].interval, 10)
        self.assertEqual(captured[0][1].api_key, "wiley-ui-test-token")
        self.assertTrue(captured[0][1].include_supplements)
        self.assertEqual(self.app.table.item(self.app._row_ids["10.1002/test-a"], "values")[2], "未检索到SI记录")
        self.assertEqual(self.app.table.item(self.app._row_ids["pii:S0009250924001234"], "values")[1], "仅导入")
        self.assertEqual(str(self.app.supplements_check.cget("state")), "normal")
        self.assertNotIn("wiley-ui-test-token", str(self.saved.call_args.args[0]))

    def test_wiley_requires_key_and_rejects_intervals_below_ten(self):
        self.select_wiley()
        self.import_text("10.1002/test-a")
        with patch.object(gui, "run_wiley_batch") as batch:
            self.app._start_download()
        batch.assert_not_called()
        self.errors.assert_called_once()
        self.app.wiley_api_key_var.set("wiley-ui-test-token")
        for value in ("2", "9.99", "nan", "61"):
            with self.subTest(interval=value):
                self.errors.reset_mock()
                self.app.interval_var.set(value)
                with patch.object(gui, "run_wiley_batch") as batch:
                    self.app._start_download()
                batch.assert_not_called()
                self.errors.assert_called_once()

    def test_known_wiley_dois_do_not_enter_springer_or_elsevier_queues(self):
        self.import_text("10.1002/test-a\n10.1111/test-b\n10.1007/springer\n10.1016/elsevier")
        self.assertEqual([reference.doi for reference in self.app._eligible_references()], ["10.1007/springer"])
        self.select_elsevier()
        self.assertEqual([reference.doi for reference in self.app._eligible_references()], ["10.1016/elsevier"])
        self.select_wiley()
        self.assertEqual([reference.doi for reference in self.app._eligible_references()], ["10.1002/test-a", "10.1111/test-b"])

    def test_wiley_token_is_loaded_masked_and_never_saved(self):
        with patch.object(gui, "load_wiley_credentials", return_value={"api_key": "mock-wiley-token"}):
            second_app = gui.DownloaderApp()
            try:
                second_app.withdraw()
                self.assertEqual(second_app.wiley_api_key_var.get(), "mock-wiley-token")
                second_app._show_access_settings()
                self.assertEqual(second_app.wiley_api_key_entry.cget("show"), "*")
                second_app._save_preferences()
                self.assertNotIn("mock-wiley-token", str(self.saved.call_args.args[0]))
                second_app._log("token mock-wiley-token must be hidden")
                self.assertNotIn("mock-wiley-token", second_app.log_text.get("1.0", "end"))
                self.credential_save.assert_not_called()
            finally:
                second_app.destroy()

    def test_wiley_si_preference_defaults_true_and_is_independent(self):
        self.app.supplements_var.set(False)
        self.select_elsevier()
        self.assertTrue(self.app.supplements_var.get())
        self.select_wiley()
        self.assertTrue(self.app.supplements_var.get())
        self.assertEqual(str(self.app.supplements_check.cget("state")), "normal")
        self.assertIn("Crossref", self.app.publisher_hint_var.get())
        self.app.supplements_var.set(False)
        self.app._save_preferences()
        preferences = self.saved.call_args.args[0]
        self.assertEqual(preferences["include_supplements"], "false")
        self.assertEqual(preferences["elsevier_include_supplements"], "true")
        self.assertEqual(preferences["wiley_include_supplements"], "false")
        with patch.object(gui, "load_settings", return_value=preferences):
            second_app = gui.DownloaderApp()
            try:
                second_app.withdraw()
                self.assertFalse(second_app.supplements_var.get())
                for publisher_id, expected in (("elsevier", True), ("springer", False), ("wiley", False)):
                    publisher = next(item for item in second_app._publishers if item.id == publisher_id)
                    second_app.publisher_var.set(publisher.name)
                    second_app._publisher_changed()
                    self.assertEqual(second_app.supplements_var.get(), expected)
            finally:
                second_app.destroy()

    def test_wiley_si_can_be_disabled_for_pdf_only_download(self):
        self.select_wiley()
        self.app.wiley_api_key_var.set("wiley-ui-test-token")
        self.app.supplements_var.set(False)
        self.import_text("10.1002/test-a")
        captured = []

        def fake_batch(records, config, on_result, on_log, cancel_event):
            captured.append(config)
            result = DownloadResult(records[0].doi, "success", reference_key=records[0].key, supplements_status="disabled")
            on_result(1, 1, result)
            manifest = self.folder / "wiley-pdf-only.csv"
            manifest.write_text("doi,status\n", encoding="utf-8")
            return BatchSummary([result], manifest)

        with patch.object(gui, "run_wiley_batch", side_effect=fake_batch):
            self.app._start_download()
            self.wait_for(lambda: not self.app._busy)
        self.assertFalse(captured[0].include_supplements)
        self.assertEqual(captured[0].interval, 10)
        self.assertEqual(self.saved.call_args.args[0]["wiley_include_supplements"], "false")
        self.assertEqual(self.app.table.item(self.app._row_ids["10.1002/test-a"], "values")[2], "未启用")

    def test_wiley_crossref_states_keep_manual_clues_and_article_action(self):
        self.select_wiley()
        self.import_text("10.1002/test-a")
        doi = self.app._parsed.dois[0]
        row_id = self.app._row_ids[doi]
        self.app.table.selection_set(row_id)
        record_path = self.folder / "wiley-manifest.json"
        record_path.write_text("{}", encoding="utf-8")
        article_url = "https://onlinelibrary.wiley.com/doi/10.1002/test-a"
        for status, expected in (("not_indexed", "未检索到SI记录"), ("manual_required", "需手动获取"), ("discovery_failed", "发现失败")):
            with self.subTest(status=status):
                result = DownloadResult(doi, "partial", reference_key=doi, supplements_status=status,
                    article_url=article_url, record_path=str(record_path), error="请查看文章页面或 JSON 中的附件线索")
                self.app._render_result(result)
                self.assertEqual(self.app.table.item(row_id, "values")[2], expected)
                self.assertNotEqual(expected, "无附件")
                self.assertIn("附件线索", self.app.detail_var.get())
                self.assertEqual(str(self.app.record_button.cget("state")), "normal")
                self.assertEqual(str(self.app.article_button.cget("state")), "normal")
        with patch.object(gui.webbrowser, "open", return_value=True) as open_browser:
            self.app._open_selected_article()
            open_browser.assert_called_once_with(article_url, new=2)
        with patch.object(self.app, "_open_path") as open_path:
            self.app._open_selected_record()
            open_path.assert_called_once_with(record_path)


if __name__ == "__main__":
    unittest.main()
