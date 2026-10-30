"""Offline keyword discovery, selection, and download handoff UI checks."""

import csv
import threading
import unittest
from unittest.mock import patch

from paper_downloader import gui
from paper_downloader.doi_parser import ArticleReference
from paper_downloader.downloader import BatchSummary
from paper_downloader.search import SearchResult
from tests import test_gui as gui_cases


class KeywordSearchGuiTests(unittest.TestCase):
    _destroy = gui_cases.GuiWorkflowTests._destroy
    wait_for = gui_cases.GuiWorkflowTests.wait_for
    parse = gui_cases.GuiWorkflowTests.parse

    def setUp(self):
        gui_cases.GuiWorkflowTests.setUp(self)
        self.app.springer_meta_key_var.set("synthetic-springer-key")

    def open_search(self, publisher_id="springer"):
        publisher = next(item for item in self.app._publishers if item.id == publisher_id)
        if self.app._selected_publisher().id != publisher_id:
            self.app.publisher_var.set(publisher.name)
            self.app._publisher_changed()
        self.app._show_search_dialog()
        dialog = self.app._search_dialog
        self.assertIsNotNone(dialog)
        dialog.keywords_var.set("electrocatalysis")
        return dialog

    @staticmethod
    def result(*, cancelled=False):
        return SearchResult(
            records=[ArticleReference(doi="10.1007/search-one", title="First paper", journal="Journal A"),
                     ArticleReference(doi="10.1007/search-two", title="Second paper", journal="Journal A")],
            total=25, truncated=True, source="Springer Meta API", cancelled=cancelled,
        )

    def complete_search(self, dialog, result=None):
        with patch.object(gui, "search_articles", return_value=result or self.result()):
            dialog.start()
            self.wait_for(lambda: not self.app._busy)

    def test_search_runs_off_main_thread_and_captures_filters_without_downloading(self):
        self.parse()
        previous = self.app._parsed
        journal = self.app._selected_publisher().journals[0]
        self.app.journal_var.set(journal)
        dialog = self.open_search()
        dialog.year_from_var.set("2020")
        dialog.year_to_var.set("2025")
        dialog.max_results_var.set("40")
        self.app.interval_var.set("3")
        captured = []
        release = threading.Event()

        def fake_search(config, on_log, cancel_event):
            captured.append((config, threading.get_ident(), cancel_event))
            release.wait(2)
            on_log("search complete")
            return self.result()

        with patch.object(gui, "search_articles", side_effect=fake_search), patch.object(gui, "run_batch") as download:
            dialog.start()
            self.assertTrue(self.app._busy)
            self.assertEqual(str(self.app.start_button.cget("state")), "disabled")
            self.assertEqual(str(dialog.import_button.cget("state")), "disabled")
            self.app._start_download()
            release.set()
            self.wait_for(lambda: not self.app._busy)
            download.assert_not_called()
        config, worker_id, event = captured[0]
        self.assertNotEqual(worker_id, threading.get_ident())
        self.assertIs(event, self.app._cancel)
        self.assertEqual((config.publisher_id, config.keywords, config.journal), ("springer", "electrocatalysis", journal))
        self.assertEqual((config.year_from, config.year_to, config.max_results, config.interval), (2020, 2025, 40, 3))
        self.assertEqual(config.api_key, "synthetic-springer-key")
        self.assertIs(self.app._parsed, previous)
        self.assertEqual(len(dialog.table.get_children()), 2)
        self.assertIn("25", dialog.status_var.get())
        self.assertIn("尚有结果", dialog.status_var.get())
        self.errors.assert_not_called()

    def test_selected_search_records_export_and_download_without_file_stat(self):
        dialog = self.open_search()
        self.complete_search(dialog)
        dialog.table.selection_set(dialog.table.get_children()[1])
        dialog.import_selection()
        self.assertIsNone(self.app._search_dialog)
        self.assertEqual(self.app._source_mode, "search")
        self.assertEqual(self.app._parsed.dois, ["10.1007/search-two"])
        self.assertEqual(str(self.app.column_entry.cget("state")), "disabled")
        self.assertEqual(self.app.parse_button.cget("text"), "重新检索")
        with patch.object(self.app, "_file_signature", side_effect=AssertionError("search must not stat a file")):
            self.assertTrue(self.app._source_is_current())
            output = self.folder / "search.csv"
            self.app._export_references(output)
            with output.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(rows[0]["doi"], "10.1007/search-two")
            with patch.object(gui, "run_batch", return_value=BatchSummary([], self.folder / "manifest.csv")) as batch:
                self.app._start_download()
                self.wait_for(lambda: not self.app._busy)
            self.assertEqual(batch.call_args.args[0], ["10.1007/search-two"])
        self.errors.assert_not_called()

    def test_journal_filter_groups_titles_and_downloads_only_visible_selection(self):
        dialog = self.open_search()
        references = [
            ArticleReference(doi="10.1007/filter-one", journal="Journal A"),
            ArticleReference(doi="10.1007/filter-two", journal="JOURNAL-A"),
            ArticleReference(doi="10.1007/filter-three", journal="Journal Alpha"),
            ArticleReference(doi="10.1007/filter-four", journal=""),
        ]
        self.complete_search(dialog, SearchResult(references, total=400, truncated=True))
        options = list(dialog.journal_filter_combo.cget("values"))
        self.assertEqual(len(options), 4)  # All, two distinct journal names, missing title.
        target = next(value for value in options if "Journal A" in value and "Alpha" not in value)
        self.assertIn("2", target)
        with patch.object(gui, "search_articles") as search:
            dialog.journal_filter_var.set(target)
            self.app.update()
            self.assertEqual([record.doi for record in dialog.records.values()], [item.doi for item in references[:2]])
            self.assertEqual(len(dialog.table.selection()), 2)
            self.assertIn("2", dialog.filter_count_var.get())
            self.assertIn("4", dialog.filter_count_var.get())
            dialog.deselect_button.invoke()
            self.assertEqual(dialog.table.selection(), ())
            dialog.select_all_button.invoke()
            dialog.import_selection()
            search.assert_not_called()
        self.assertEqual(self.app._parsed.dois, [item.doi for item in references[:2]])
        with patch.object(gui, "run_batch", return_value=BatchSummary([], self.folder / "filtered.csv")) as download:
            self.app._start_download()
            self.wait_for(lambda: not self.app._busy)
        self.assertEqual(download.call_args.args[0], [item.doi for item in references[:2]])
        self.errors.assert_not_called()

    def test_journal_filter_restores_results_and_resets_on_new_query(self):
        dialog = self.open_search()
        references = [ArticleReference(doi="10.1007/filter-one", journal="Journal A"),
                      ArticleReference(doi="10.1007/filter-two", journal="Journal B")]
        self.complete_search(dialog, SearchResult(references))
        options = list(dialog.journal_filter_combo.cget("values"))
        dialog.journal_filter_var.set(next(value for value in options if "Journal B" in value))
        self.assertEqual(len(dialog.records), 1)
        dialog.journal_filter_var.set(options[0])
        self.assertEqual(list(dialog.records.values()), references)
        self.assertEqual(len(dialog.table.selection()), 2)
        dialog.keywords_var.set("a new keyword")
        self.assertFalse(dialog.records)
        self.assertFalse(any("Journal" in value for value in dialog.journal_filter_combo.cget("values")))
        self.assertEqual(str(dialog.import_button.cget("state")), "disabled")
        self.complete_search(dialog, SearchResult([ArticleReference(doi="10.1007/filter-new", journal="New Journal")]))
        self.assertEqual(len(dialog.records), 1)
        self.assertFalse(any("Journal A" in value or "Journal B" in value for value in dialog.journal_filter_combo.cget("values")))
        dialog.set_running(True)
        self.assertEqual(str(dialog.journal_filter_combo.cget("state")), "disabled")
        dialog.set_running(False)
        self.assertEqual(str(dialog.journal_filter_combo.cget("state")), "readonly")

    def test_journal_filter_handles_missing_names_and_preserves_search_scope(self):
        journal = self.app._selected_publisher().journals[0]
        self.app.journal_var.set(journal)
        dialog = self.open_search()
        missing = ArticleReference(doi="10.1007/missing-journal", journal="")
        self.complete_search(dialog, SearchResult([missing, ArticleReference(doi="10.1007/named-journal", journal=journal)]))
        options = list(dialog.journal_filter_combo.cget("values"))
        missing_option = next(value for value in options[1:] if journal not in value)
        dialog.journal_filter_var.set(missing_option)
        self.assertEqual(list(dialog.records.values()), [missing])
        self.assertEqual(dialog.journal, journal)
        self.assertEqual(self.app.journal_var.get(), journal)
        # An unrecognized value cannot accidentally import all other records.
        dialog.journal_filter_var.set("not an available journal")
        self.assertFalse(dialog.table.selection())
        self.assertEqual(str(dialog.import_button.cget("state")), "disabled")

    def test_empty_search_and_close_preserve_prior_import(self):
        self.parse()
        previous = self.app._parsed
        dialog = self.open_search()
        self.complete_search(dialog, SearchResult(records=[], total=0))
        self.assertIs(self.app._parsed, previous)
        self.assertEqual(str(dialog.import_button.cget("state")), "disabled")
        dialog.import_selection()
        dialog.close()
        self.assertIs(self.app._parsed, previous)
        self.assertTrue(self.app._source_is_current())

    def test_search_failure_preserves_import_and_redacts_credentials(self):
        self.parse()
        previous = self.app._parsed
        dialog = self.open_search()
        with patch.object(gui, "search_articles", side_effect=RuntimeError("failed synthetic-springer-key")):
            dialog.start()
            self.wait_for(lambda: not self.app._busy)
        self.assertIs(self.app._parsed, previous)
        self.errors.assert_called_once()
        self.assertNotIn("synthetic-springer-key", str(self.errors.call_args))
        self.assertNotIn("synthetic-springer-key", self.app.log_text.get("1.0", "end"))
        self.assertNotIn("synthetic-springer-key", dialog.status_var.get())

    def test_invalid_search_inputs_do_not_start_worker(self):
        dialog = self.open_search()
        cases = [
            ("", "", "", "100"), ("x", "abc", "", "100"),
            ("x", "2025", "2020", "100"), ("x", "23", "", "100"),
            ("x", "", "", "0"), ("x", "", "", "1001"),
            ("x", "", "", "1.5"),
        ]
        for keywords, year_from, year_to, maximum in cases:
            with self.subTest(values=(keywords, year_from, year_to, maximum)):
                for variable, value in zip((dialog.keywords_var, dialog.year_from_var, dialog.year_to_var, dialog.max_results_var), (keywords, year_from, year_to, maximum)):
                    variable.set(value)
                self.errors.reset_mock()
                with patch.object(gui, "search_articles") as search:
                    dialog.start()
                search.assert_not_called()
                self.errors.assert_called_once()
                self.assertFalse(self.app._busy)

    def test_cancel_keeps_partial_results_selectable(self):
        dialog = self.open_search()

        def fake_search(config, on_log, cancel_event):
            cancel_event.wait(2)
            return self.result(cancelled=True)

        with patch.object(gui, "search_articles", side_effect=fake_search):
            dialog.start()
            dialog.cancel()
            self.wait_for(lambda: not self.app._busy)
        self.assertTrue(self.app._cancel.is_set())
        self.assertIn("检索已取消", dialog.status_var.get())
        self.assertEqual(str(dialog.import_button.cget("state")), "normal")
        dialog.import_selection()
        self.assertEqual(len(self.app._parsed.records), 2)
        observed_cancel = []

        def fake_download(items, config, on_result, on_log, cancel_event):
            observed_cancel.append(cancel_event.is_set())
            return BatchSummary([], self.folder / "after-cancel.csv")

        with patch.object(gui, "run_batch", side_effect=fake_download):
            self.app._start_download()
            self.wait_for(lambda: not self.app._busy)
        self.assertEqual(observed_cancel, [False])
        self.errors.assert_not_called()

    def test_closing_running_search_keeps_busy_until_worker_finishes(self):
        self.parse()
        previous = self.app._parsed
        dialog = self.open_search()
        release = threading.Event()

        def fake_search(config, on_log, cancel_event):
            release.wait(2)
            return self.result()

        with patch.object(gui, "search_articles", side_effect=fake_search):
            dialog.start()
            old_token = self.app._search_token
            dialog.close()
            self.assertTrue(self.app._cancel.is_set())
            self.assertTrue(self.app._busy)
            self.app._show_search_dialog()
            self.assertIsNone(self.app._search_dialog)
            release.set()
            self.wait_for(lambda: not self.app._busy)
        self.assertIs(self.app._parsed, previous)
        new_dialog = self.open_search()
        self.app._events.put(("searched", old_token, dialog, self.result(), ""))
        self.wait_for(lambda: self.app._events.empty())
        self.assertIs(self.app._search_dialog, new_dialog)
        self.assertFalse(new_dialog.records)
        self.assertIs(self.app._parsed, previous)
        self.errors.assert_not_called()

    def test_query_change_clears_preview_and_disables_import(self):
        dialog = self.open_search()
        self.complete_search(dialog)
        self.assertTrue(dialog.records)
        dialog.keywords_var.set("a different query")
        self.assertFalse(dialog.records)
        self.assertEqual(str(dialog.import_button.cget("state")), "disabled")
        dialog.import_selection()
        self.assertIsNone(self.app._parsed)

    def test_search_import_ignores_column_changes_but_file_change_invalidates(self):
        dialog = self.open_search()
        self.complete_search(dialog)
        dialog.import_selection()
        previous = self.app._parsed
        self.app.column_var.set("another_column")
        self.assertIs(self.app._parsed, previous)
        self.assertTrue(self.app._source_is_current())
        self.app._parse_file()
        self.assertIsNotNone(self.app._search_dialog)
        self.assertEqual(self.app._search_dialog.keywords_var.get(), "electrocatalysis")
        self.app._search_dialog.close()
        self.app.file_var.set(str(self.source))
        self.assertIsNone(self.app._parsed)
        self.assertEqual(self.app._source_mode, "file")
        self.assertEqual(str(self.app.column_entry.cget("state")), "normal")

    def test_search_import_can_be_replaced_by_pasted_dois(self):
        dialog = self.open_search()
        self.complete_search(dialog)
        dialog.import_selection()
        self.app._import_reference_text("10.1007/pasted")
        self.wait_for(lambda: not self.app._busy)
        self.assertEqual(self.app._source_mode, "paste")
        self.assertEqual(self.app._parsed.dois, ["10.1007/pasted"])
        self.assertTrue(self.app._source_is_current())

    def test_other_publishers_receive_only_their_own_search_credentials(self):
        self.app.api_key_var.set("synthetic-elsevier-key")
        self.app.insttoken_var.set("synthetic-insttoken")
        self.app.wiley_api_key_var.set("synthetic-wiley-token")
        for publisher_id in ("elsevier", "wiley"):
            with self.subTest(publisher_id=publisher_id):
                dialog = self.open_search(publisher_id)
                with patch.object(gui, "search_articles", return_value=SearchResult([])) as search:
                    dialog.start()
                    self.wait_for(lambda: not self.app._busy)
                config = search.call_args.args[0]
                self.assertEqual(config.publisher_id, publisher_id)
                self.assertEqual(config.api_key, "synthetic-elsevier-key" if publisher_id == "elsevier" else "")
                self.assertEqual(config.insttoken, "synthetic-insttoken" if publisher_id == "elsevier" else "")
                self.assertGreaterEqual(config.interval, 2)
                dialog.close()

    def test_elsevier_pii_and_wiley_doi_search_imports_use_corresponding_downloaders(self):
        self.app.api_key_var.set("synthetic-elsevier-key")
        self.app.insttoken_var.set("synthetic-insttoken")
        self.app.wiley_api_key_var.set("synthetic-wiley-token")
        cases = (
            ("elsevier", ArticleReference(pii="S0010938X26000012X", title="PII paper"), "run_elsevier_batch", "synthetic-elsevier-key"),
            ("wiley", ArticleReference(doi="10.1002/search-wiley", title="Wiley paper"), "run_wiley_batch", "synthetic-wiley-token"),
        )
        for publisher_id, reference, runner_name, expected_key in cases:
            with self.subTest(publisher=publisher_id):
                dialog = self.open_search(publisher_id)
                self.complete_search(dialog, SearchResult(records=[reference]))
                dialog.import_selection()
                self.assertTrue(self.app._source_is_current())
                with patch.object(gui, runner_name, return_value=BatchSummary([], self.folder / f"{publisher_id}.csv")) as runner, patch.object(gui, "run_batch") as springer:
                    self.app._start_download()
                    self.wait_for(lambda: not self.app._busy)
                runner.assert_called_once()
                springer.assert_not_called()
                self.assertEqual(runner.call_args.args[0], [reference])
                self.assertEqual(runner.call_args.args[1].api_key, expected_key)
                self.assertEqual(runner.call_args.args[1].publisher_id, publisher_id)
                self.assertEqual(self.app._source_mode, "search")
        self.errors.assert_not_called()

    def test_springer_search_key_is_masked_redacted_and_never_saved(self):
        self.app._show_access_settings()
        self.assertEqual(self.app.springer_meta_key_entry.cget("show"), "*")
        self.app._log("synthetic-springer-key")
        self.app._save_preferences()
        self.app._save_api_credentials()
        self.assertNotIn("synthetic-springer-key", str(self.saved.call_args))
        self.assertNotIn("synthetic-springer-key", str(self.credential_save.call_args))
        self.assertNotIn("synthetic-springer-key", self.app.log_text.get("1.0", "end"))

    def test_unsupported_publisher_and_incomplete_journal_block_search(self):
        unsupported = next(item for item in self.app._publishers if not item.enabled)
        self.app.publisher_var.set(unsupported.name)
        self.app._publisher_changed()
        self.app._set_busy(False)
        self.assertEqual(str(self.app.search_button.cget("state")), "disabled")
        self.app._show_search_dialog()
        self.assertIsNone(self.app._search_dialog)
        springer = next(item for item in self.app._publishers if item.id == "springer")
        self.app.publisher_var.set(springer.name)
        self.app._publisher_changed()
        self.app.journal_var.set("incomplete journal")
        self.app._show_search_dialog()
        self.assertIsNone(self.app._search_dialog)
        self.errors.assert_called_once()

    def test_close_app_during_search_requests_cancellation_and_waits(self):
        dialog = self.open_search()

        def fake_search(config, on_log, cancel_event):
            cancel_event.wait(2)
            return self.result(cancelled=True)

        with patch.object(gui, "search_articles", side_effect=fake_search):
            dialog.start()
            self.app._on_close()
            self.assertTrue(self.app._closing)
            self.assertTrue(self.app._cancel.is_set())
            self.wait_for(lambda: self.app._destroyed)
        self.assertTrue(self.app._destroyed)

    def test_search_dialog_actions_remain_visible_at_minimum_size(self):
        self.app.deiconify()
        dialog = self.open_search()
        dialog.geometry("720x480")
        dialog.deiconify()
        self.app.update()
        for widget in (dialog.search_button, dialog.import_button, dialog.cancel_button, dialog.journal_filter_combo):
            self.assertGreaterEqual(widget.winfo_rootx(), dialog.winfo_rootx())
            self.assertGreaterEqual(widget.winfo_rooty(), dialog.winfo_rooty())
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), dialog.winfo_rootx() + dialog.winfo_width())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), dialog.winfo_rooty() + dialog.winfo_height())


if __name__ == "__main__":
    unittest.main()
