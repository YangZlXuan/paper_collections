"""Quota display, refresh and shutdown behavior using an isolated usage database."""
from datetime import datetime, timezone
import threading
import unittest
from unittest.mock import patch

from paper_downloader import gui, request_quota as quota
import test_gui as gui_cases


META = "https://api.springernature.com/meta/v2/json"
ARTICLE = "https://api.elsevier.com/content/article/doi/10.1016/example"


class RequestQuotaGuiTests(unittest.TestCase):
    _destroy = gui_cases.GuiWorkflowTests._destroy
    wait_for = gui_cases.GuiWorkflowTests.wait_for

    def setUp(self):
        self.now = datetime(2026, 11, 11, 8, tzinfo=timezone.utc).timestamp()
        self.enterContext(patch.object(quota.time, "time", side_effect=lambda: self.now))
        gui_cases.GuiWorkflowTests.setUp(self)

    def test_startup_and_manual_refresh_show_counts_and_beijing_recovery_times(self):
        self.assertEqual(self.app.quota_used_vars["springer"].get(), "0 / 400")
        self.assertEqual(self.app.quota_remaining_vars["elsevier"].get(), "40,000")
        self.assertEqual(self.app.quota_recovery_vars["elsevier"].get(), "暂无待恢复额度")
        quota.reserve_api_request(META)
        quota.reserve_api_request(ARTICLE)
        self.now += 60
        quota.reserve_api_request(ARTICLE)
        self.app.quota_refresh_button.invoke()
        self.assertEqual(self.app.quota_used_vars["springer"].get(), "1 / 400")
        self.assertEqual(self.app.quota_remaining_vars["springer"].get(), "399")
        self.assertEqual(self.app.quota_recovery_vars["springer"].get(), "每日重置：2026-11-12 00:00:00")
        self.assertEqual(self.app.quota_used_vars["elsevier"].get(), "2 / 40,000")
        self.assertEqual(self.app.quota_remaining_vars["elsevier"].get(), "39,998")
        self.assertEqual(self.app.quota_recovery_vars["elsevier"].get(),
                         "下次恢复：2026-11-18 16:00:00\n全部恢复：2026-11-18 16:01:00")
        second = gui.DownloaderApp(check_updates=False)
        try:
            second.withdraw()
            self.assertEqual(second.quota_used_vars["elsevier"].get(), "2 / 40,000")
        finally:
            second.destroy()

    def test_auto_refresh_sees_worker_requests_while_busy(self):
        self.app._set_busy(True)
        worker = threading.Thread(target=quota.reserve_api_request, args=(ARTICLE,))
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.wait_for(lambda: self.app.quota_used_vars["elsevier"].get() == "1 / 40,000")
        self.assertTrue(self.app._busy)
        self.assertEqual(str(self.app.quota_refresh_button.cget("state")), "normal")
        self.app._set_busy(False)

    def test_task_completion_refreshes_without_waiting_for_timer(self):
        self.app._set_busy(True)
        quota.reserve_api_request(META)
        self.app._set_busy(False)
        self.assertEqual(self.app.quota_used_vars["springer"].get(), "1 / 400")

    def test_refresh_after_expiry_updates_remaining_and_recovery(self):
        quota.reserve_api_request(META)
        quota.reserve_api_request(ARTICLE)
        self.now += 7 * 24 * 60 * 60
        self.app.quota_refresh_button.invoke()
        self.assertEqual(self.app.quota_remaining_vars["springer"].get(), "400")
        self.assertEqual(self.app.quota_remaining_vars["elsevier"].get(), "40,000")
        self.assertEqual(self.app.quota_recovery_vars["elsevier"].get(), "暂无待恢复额度")

    def test_read_failure_shows_unknown_usage_instead_of_zero(self):
        (self.folder / "request-quota.sqlite3").write_bytes(b"corrupt quota database")
        self.app.quota_refresh_button.invoke()
        self.assertIn("无法读取本地 API 请求额度", self.app.quota_status_var.get())
        for publisher in ("springer", "elsevier"):
            self.assertEqual(self.app.quota_used_vars[publisher].get(), "读取失败")
            self.assertEqual(self.app.quota_remaining_vars[publisher].get(), "—")
            self.assertEqual(self.app.quota_recovery_vars[publisher].get(), "恢复时间未知")

    def test_quota_fields_fit_small_window_with_two_recovery_times(self):
        reset = datetime(2026, 11, 18, 16, tzinfo=timezone.utc)
        status = {
            "springer": quota.RequestQuota(400, 400, reset, reset),
            "elsevier": quota.RequestQuota(40_000, 40_000, reset, reset),
        }
        with patch.object(gui, "get_request_quotas", return_value=status):
            self.app._refresh_request_quotas()
        self.app.deiconify()
        self.app.geometry("900x650")
        self.app.notebook.select(self.app._quota_tab)
        self.app.update()
        panel = self.app._quota_panel
        right = panel.winfo_rootx() + panel.winfo_width()
        self.assertLessEqual(right, self.app.winfo_rootx() + self.app.winfo_width())
        for widget in panel.winfo_children():
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right)
        self.assertLess(panel.winfo_rooty() + panel.winfo_height(), self.app.winfo_rooty() + self.app.winfo_height())
        self.assertEqual(self.app.quota_remaining_vars["elsevier"].get(), "0")

    def test_close_cancels_quota_refresh_and_prevents_further_reads(self):
        self.assertIsNotNone(self.app._quota_poll_id)
        self.app.destroy()
        self.assertIsNone(self.app._quota_poll_id)
        with patch.object(gui, "get_request_quotas") as read:
            self.app._refresh_request_quotas()
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
