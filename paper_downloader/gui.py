"""Chinese desktop UI. All Tk operations run exclusively on the main thread."""

from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime
import hashlib
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote, urlsplit

from .catalog import Publisher, load_catalog
from .doi_parser import ArticleReference, ParseResult, parse_doi_file, parse_reference_text
from .credentials import clear_elsevier_credentials, load_elsevier_credentials, load_wiley_credentials, save_elsevier_credentials
from .downloader import (
    MAX_REQUEST_INTERVAL, MIN_REQUEST_INTERVAL, BatchSummary, DownloadConfig,
    DownloadResult, parse_request_interval, run_batch, validate_config,
)
from .elsevier import ElsevierConfig, run_elsevier_batch, validate_elsevier_config
from .settings import load_settings, save_settings
from .wiley import MIN_WILEY_INTERVAL, WileyConfig, parse_wiley_interval, run_wiley_batch, validate_wiley_config


ALL_JOURNALS = "全部期刊（该出版社）"
PASTE_SOURCE_LABEL = "已粘贴 DOI / 文献链接"
STATUS_LABELS = {
    "pending": "待处理", "success": "已下载", "exists": "已存在",
    "partial": "部分完成", "no_pdf": "未找到正文", "access_denied": "访问受限",
    "filtered": "不符合筛选", "failed": "失败", "cancelled": "已取消", "import_only": "仅导入",
}


class DownloaderApp(tk.Tk):
    """Offline startup with explicit DOI parsing and cancellable download workers."""

    def __init__(self) -> None:
        super().__init__()
        self.title("论文批量下载助手")
        window_width = min(1120, max(760, self.winfo_screenwidth() - 80))
        window_height = min(850, max(620, self.winfo_screenheight() - 100))
        self.geometry(f"{window_width}x{window_height}")
        self.minsize(min(900, window_width), min(650, window_height))
        self.configure(background="#f3f6fb")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._events: queue.Queue[tuple] = queue.Queue()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._poll_id: str | None = None
        self._access_dialog: tk.Toplevel | None = None
        self._paste_dialog: tk.Toplevel | None = None
        self._busy = False
        self._closing = False
        self._generation = 0
        self._parsed: ParseResult | None = None
        self._parsed_key: tuple[str, str] | None = None
        self._source_signature: tuple[int, int] | None = None
        self._source_mode = "file"
        self._pasted_text = ""
        self._row_ids: dict[str, str] = {}
        self._row_keys: dict[str, str] = {}
        self._references: dict[str, ArticleReference] = {}
        self._active_keys: set[str] = set()
        self._batch_publisher_id = "springer"
        self._results: dict[str, DownloadResult] = {}
        self._counts: Counter[str] = Counter()
        self._manifest: Path | None = None
        self._preferences = load_settings()
        credential_warning = ""
        try:
            credentials = load_elsevier_credentials()
        except (OSError, ValueError):
            credentials = {}
            credential_warning = "无法读取本机加密凭据，请在 API / Cookie 设置中重新填写。"
        self.api_key_var = tk.StringVar(value=credentials.get("api_key", ""))
        self.insttoken_var = tk.StringVar(value=credentials.get("insttoken", ""))
        self.remember_credentials_var = tk.BooleanVar(value=bool(credentials.get("remembered", False)))
        self.wiley_api_key_var = tk.StringVar(value=load_wiley_credentials().get("api_key", ""))
        self._publishers = load_catalog()
        if not self._publishers:
            raise ValueError("期刊目录为空，请重新安装软件。")
        self._publishers_by_name = {item.name: item for item in self._publishers}
        preferred = self._preferences.get("publisher_id", "springer")
        publisher = next((item for item in self._publishers if item.id == preferred), self._publishers[0])
        self.publisher_var = tk.StringVar(value=publisher.name)
        self.journal_var = tk.StringVar(value=ALL_JOURNALS)
        self.file_var = tk.StringVar(value="")
        self.column_var = tk.StringVar(value=self._preferences.get("doi_column", ""))
        self.email_var = tk.StringVar(value=self._preferences.get("email", ""))
        self.cookie_var = tk.StringVar(value=self._preferences.get("cookie_file") or os.environ.get("SN_COOKIE_FILE", ""))
        self.supplements_var = tk.BooleanVar(value=self._preferences.get("include_supplements", "true").lower() != "false")
        self._springer_supplements = self.supplements_var.get()
        self._elsevier_supplements = self._preferences.get("elsevier_include_supplements", "true").lower() != "false"
        self._wiley_supplements = self._preferences.get("wiley_include_supplements", "true").lower() != "false"
        self._last_publisher_id: str | None = None
        try:
            saved_interval = parse_request_interval(self._preferences.get("request_interval", MIN_REQUEST_INTERVAL))
        except ValueError:
            saved_interval = MIN_REQUEST_INTERVAL
        self._standard_interval = f"{saved_interval:g}"
        try:
            saved_wiley_interval = parse_wiley_interval(self._preferences.get("wiley_request_interval", MIN_WILEY_INTERVAL))
        except ValueError:
            saved_wiley_interval = MIN_WILEY_INTERVAL
        self._wiley_interval = f"{saved_wiley_interval:g}"
        self.interval_var = tk.StringVar(value=self._standard_interval)
        default_output = Path.home() / "Downloads" / "PaperDownloader"
        self.output_var = tk.StringVar(value=self._preferences.get("output_dir", str(default_output)))
        self.publisher_hint_var = tk.StringVar()
        self.access_hint_var = tk.StringVar()
        self.interval_hint_var = tk.StringVar()
        self.parse_hint_var = tk.StringVar(value="支持 TXT、CSV、TSV、XLSX、RIS、BibTeX；也可粘贴 DOI 或 ScienceDirect 链接。")
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text_var = tk.StringVar(value="尚未开始")
        self.status_var = tk.StringVar(value="就绪")
        self.counts_var = tk.StringVar(value="已下载 0    已存在 0    部分完成 0    无正文 0    访问受限 0    筛除 0    失败 0")
        self.detail_var = tk.StringVar(value="选择记录可查看 DOI、PII、链接和下载结果。")
        self._configure_styles()
        self._build_ui()
        self._publisher_changed()
        if credential_warning:
            self._log(credential_warning)
        stored_journal = self._preferences.get("journal", "")
        if stored_journal in publisher.journals:
            self.journal_var.set(stored_journal)
        self.file_var.trace_add("write", self._invalidate_parse)
        self.column_var.trace_add("write", self._invalidate_parse)
        self._poll_id = self.after(80, self._drain_events)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        style.configure("TFrame", background="#f3f6fb")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f3f6fb", foreground="#25364c")
        style.configure("Card.TLabel", background="#ffffff", foreground="#25364c")
        style.configure("Muted.TLabel", foreground="#61718a", background="#ffffff", font=("Microsoft YaHei UI", 9))
        style.configure("Card.TCheckbutton", background="#ffffff", foreground="#25364c")
        style.configure("TButton", padding=(12, 7), font=("Microsoft YaHei UI", 10))
        style.configure("Primary.TButton", background="#1764c0", foreground="#ffffff", padding=(18, 8))
        style.map("Primary.TButton", background=[("disabled", "#c4d2e5"), ("active", "#13539e")], foreground=[("disabled", "#f4f7fb")])
        style.configure("TEntry", padding=6, fieldbackground="#ffffff")
        style.configure("TCombobox", padding=5, fieldbackground="#ffffff")
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", rowheight=29, font=("Microsoft YaHei UI", 9), borderwidth=0)
        style.configure("Treeview.Heading", background="#eaf0f8", foreground="#25364c", font=("Microsoft YaHei UI", 9, "bold"), padding=(7, 8))
        style.map("Treeview", background=[("selected", "#dceaff")], foreground=[("selected", "#183956")])
        style.configure("TNotebook", background="#f3f6fb", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(15, 8))
        style.configure("Horizontal.TProgressbar", background="#1764c0", troughcolor="#e9eff7", borderwidth=0)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._scroll_canvas = tk.Canvas(self, background="#f3f6fb", highlightthickness=0)
        self._scroll_canvas.grid(row=0, column=0, sticky="nsew")
        page_scroll = ttk.Scrollbar(self, orient="vertical", command=self._scroll_canvas.yview)
        page_scroll.grid(row=0, column=1, sticky="ns")
        self._scroll_canvas.configure(yscrollcommand=page_scroll.set)
        self._content = ttk.Frame(self._scroll_canvas)
        content_window = self._scroll_canvas.create_window((0, 0), window=self._content, anchor="nw")
        self._content.grid_columnconfigure(0, weight=1)
        self._content.grid_rowconfigure(3, weight=1, minsize=275)

        def size_content(_event: object = None) -> None:
            desired_height = max(self._scroll_canvas.winfo_height(), self._content.winfo_reqheight())
            self._scroll_canvas.itemconfigure(content_window, width=self._scroll_canvas.winfo_width(), height=desired_height)
            self._scroll_canvas.configure(scrollregion=(0, 0, self._scroll_canvas.winfo_width(), desired_height))

        self._scroll_canvas.bind("<Configure>", size_content)
        self._content.bind("<Configure>", size_content)
        self.bind("<MouseWheel>", self._scroll_page)
        header = tk.Frame(self._content, background="#173f70", padx=22, pady=10)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(header, text="论文批量下载助手", foreground="white", background="#173f70", font=("Microsoft YaHei UI", 19, "bold")).pack(anchor="w")
        tk.Label(header, text="支持 Springer / Nature、Elsevier 与 Wiley 论文批量下载", foreground="#d6e4f7", background="#173f70", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(4, 0))

        settings = ttk.Frame(self._content, style="Card.TFrame", padding=(18, 10))
        settings.grid(row=1, column=0, sticky="ew", padx=18, pady=(10, 10))
        settings.grid_columnconfigure(1, weight=2)
        settings.grid_columnconfigure(3, weight=3)
        ttk.Label(settings, text="出版社", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.publisher_combo = ttk.Combobox(settings, textvariable=self.publisher_var, values=list(self._publishers_by_name), state="readonly", width=27)
        self.publisher_combo.grid(row=0, column=1, sticky="ew", padx=(0, 20))
        self.publisher_combo.bind("<<ComboboxSelected>>", self._publisher_changed)
        ttk.Label(settings, text="期刊", style="Card.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 12))
        self.journal_combo = ttk.Combobox(settings, textvariable=self.journal_var, width=42)
        self.journal_combo.grid(row=0, column=3, columnspan=2, sticky="ew")
        self.journal_combo.bind("<KeyRelease>", self._filter_journals)
        self.journal_combo.bind("<FocusIn>", self._journal_focus)
        ttk.Label(settings, textvariable=self.publisher_hint_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 10))

        ttk.Label(settings, text="文献文件", style="Card.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 12))
        self.file_entry = ttk.Entry(settings, textvariable=self.file_var)
        self.file_entry.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(0, 8))
        self.file_button = ttk.Button(settings, text="选择文件", command=self._pick_file)
        self.file_button.grid(row=2, column=4, sticky="ew")
        ttk.Label(settings, text="DOI 列名", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.column_entry = ttk.Entry(settings, textvariable=self.column_var)
        self.column_entry.grid(row=3, column=1, sticky="ew", padx=(0, 20), pady=(8, 0))
        ttk.Label(settings, text="留空自动识别", style="Muted.TLabel").grid(row=3, column=2, sticky="w", pady=(8, 0))
        self.paste_button = ttk.Button(settings, text="粘贴 DOI / 链接", command=self._show_paste_dialog)
        self.paste_button.grid(row=3, column=3, sticky="e", padx=(0, 8), pady=(8, 0))
        self.parse_button = ttk.Button(settings, text="解析文件", command=self._parse_file)
        self.parse_button.grid(row=3, column=4, sticky="ew", pady=(8, 0))
        ttk.Label(settings, textvariable=self.parse_hint_var, style="Muted.TLabel").grid(row=4, column=0, columnspan=5, sticky="w", pady=(6, 10))

        ttk.Label(settings, text="保存目录", style="Card.TLabel").grid(row=5, column=0, sticky="w")
        self.output_entry = ttk.Entry(settings, textvariable=self.output_var)
        self.output_entry.grid(row=5, column=1, columnspan=3, sticky="ew", padx=(0, 8))
        self.output_button = ttk.Button(settings, text="选择目录", command=self._pick_output)
        self.output_button.grid(row=5, column=4, sticky="ew")
        self.supplements_check = ttk.Checkbutton(settings, text="下载补充材料", variable=self.supplements_var, style="Card.TCheckbutton")
        self.supplements_check.grid(row=6, column=1, sticky="w", pady=(8, 0))
        ttk.Label(settings, textvariable=self.access_hint_var, style="Muted.TLabel").grid(row=6, column=2, columnspan=2, sticky="w", pady=(8, 0))
        self.access_button = ttk.Button(settings, text="API / Cookie 设置", command=self._show_access_settings)
        self.access_button.grid(row=6, column=4, sticky="ew", pady=(8, 0))
        ttk.Label(settings, text="下载间隔（秒）", style="Card.TLabel").grid(row=7, column=0, sticky="w", padx=(0, 12), pady=(8, 0))
        self.interval_combo = ttk.Combobox(settings, textvariable=self.interval_var, values=("2", "3", "5", "10", "20", "30", "60"), width=12)
        self.interval_combo.grid(row=7, column=1, sticky="ew", padx=(0, 20), pady=(8, 0))
        ttk.Label(settings, textvariable=self.interval_hint_var, style="Muted.TLabel").grid(row=7, column=2, columnspan=3, sticky="w", pady=(8, 0))

        progress = ttk.Frame(self._content, padding=(18, 0))
        self._progress_panel = progress
        progress.grid(row=2, column=0, sticky="ew")
        progress.grid_columnconfigure(0, weight=1)
        actions = ttk.Frame(progress)
        actions.grid(row=0, column=0, sticky="ew")
        self.start_button = ttk.Button(actions, text="开始下载", style="Primary.TButton", command=self._start_download)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="取消任务", command=self._cancel_download, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        self.open_button = ttk.Button(actions, text="打开保存目录", command=self._open_output)
        self.open_button.pack(side="right")
        self.manifest_button = ttk.Button(actions, text="打开结果清单", command=self._open_manifest, state="disabled")
        self.manifest_button.pack(side="right", padx=(0, 8))
        ttk.Label(actions, textvariable=self.progress_text_var).pack(side="left", padx=8)
        self.progress_bar = ttk.Progressbar(progress, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(10, 5))
        ttk.Label(progress, textvariable=self.counts_var, font=("Microsoft YaHei UI", 9)).grid(row=2, column=0, sticky="w", pady=(0, 7))

        notebook = ttk.Notebook(self._content, height=235)
        notebook.grid(row=3, column=0, sticky="nsew", padx=18, pady=(0, 10))
        self.notebook = notebook
        results_tab = ttk.Frame(notebook, padding=(0, 5, 0, 0))
        results_tab.grid_columnconfigure(0, weight=1)
        results_tab.grid_rowconfigure(0, weight=1)
        notebook.add(results_tab, text="论文列表")
        columns = ("doi", "status", "supplements", "journal", "title", "file")
        self.table = ttk.Treeview(results_tab, columns=columns, show="headings", selectmode="browse", height=8)
        widths = {"doi": 230, "status": 100, "supplements": 145, "journal": 200, "title": 330, "file": 250}
        for key, label in zip(columns, ("DOI / PII", "状态", "补充材料", "期刊", "论文标题", "保存文件 / 说明")):
            self.table.heading(key, text=label)
            self.table.column(key, width=widths[key], minwidth=70, stretch=key in {"doi", "journal", "title", "file"})
        self.table.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(results_tab, orient="vertical", command=self.table.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(results_tab, orient="horizontal", command=self.table.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        for key, foreground in (("success", "#19734d"), ("exists", "#386e60"), ("failed", "#b13e36"), ("partial", "#976122"), ("no_pdf", "#976122"), ("access_denied", "#b13e36"), ("filtered", "#6b7484"), ("cancelled", "#6b7484"), ("import_only", "#6b7484")):
            self.table.tag_configure(key, foreground=foreground)
        self.table.bind("<<TreeviewSelect>>", self._show_result_details)
        self.table.bind("<Double-1>", self._open_selected_pdf)
        detail_frame = ttk.Frame(results_tab, padding=(8, 5))
        detail_frame.grid(row=2, column=0, columnspan=2, sticky="ew")
        detail_frame.grid_columnconfigure(0, weight=1)
        self._detail_text = tk.Text(detail_frame, height=3, wrap="word", state="disabled", background="#f3f6fb", foreground="#33465e", relief="flat", font=("Microsoft YaHei UI", 9))
        self._detail_text.grid(row=0, column=0, sticky="ew")
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self._detail_text.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self._detail_text.configure(yscrollcommand=detail_scroll.set)
        self.detail_var.trace_add("write", self._refresh_detail_text)
        self._refresh_detail_text()
        result_actions = ttk.Frame(results_tab)
        result_actions.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.record_button = ttk.Button(result_actions, text="打开论文记录 JSON", command=self._open_selected_record, state="disabled")
        self.record_button.pack(side="left", padx=(8, 5), pady=(0, 5))
        self.paper_folder_button = ttk.Button(result_actions, text="打开论文文件夹", command=self._open_selected_folder, state="disabled")
        self.paper_folder_button.pack(side="left", pady=(0, 5))
        self.article_button = ttk.Button(result_actions, text="打开文章页面", command=self._open_selected_article, state="disabled")
        self.article_button.pack(side="left", padx=5, pady=(0, 5))
        self.export_button = ttk.Button(result_actions, text="导出解析列表 CSV", command=self._export_references, state="disabled")
        self.export_button.pack(side="right", padx=(5, 8), pady=(0, 5))
        log_tab = ttk.Frame(notebook)
        log_tab.grid_rowconfigure(0, weight=1)
        log_tab.grid_columnconfigure(0, weight=1)
        notebook.add(log_tab, text="运行日志")
        self.log_text = tk.Text(log_tab, wrap="word", state="disabled", background="#ffffff", foreground="#33465e", relief="flat", padx=12, pady=10, font=("Microsoft YaHei UI", 9))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        ttk.Label(self._content, textvariable=self.status_var, padding=(20, 0, 20, 9), font=("Microsoft YaHei UI", 9)).grid(row=4, column=0, sticky="ew")
        self._editable_widgets = [self.file_entry, self.column_entry, self.output_entry, self.file_button, self.output_button, self.access_button, self.supplements_check, self.interval_combo, self.parse_button, self.paste_button]

    def _selected_publisher(self) -> Publisher:
        return self._publishers_by_name[self.publisher_var.get()]

    def _scroll_page(self, event: tk.Event) -> str | None:
        if event.widget.winfo_class() in {"Text", "Treeview", "TCombobox"}:
            return None
        if self._content.winfo_height() > self._scroll_canvas.winfo_height() and event.delta:
            self._scroll_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        return None

    def _show_progress_panel(self) -> None:
        self.update_idletasks()
        if self._content.winfo_height() > self._scroll_canvas.winfo_height():
            self._scroll_canvas.yview_moveto(max(0, self._progress_panel.winfo_y() - 12) / self._content.winfo_height())

    def _publisher_changed(self, _event: object = None) -> None:
        publisher = self._selected_publisher()
        if self._last_publisher_id == "wiley":
            self._wiley_interval = self.interval_var.get()
        elif self._last_publisher_id in {"springer", "elsevier"}:
            self._standard_interval = self.interval_var.get()
        self.interval_var.set(self._wiley_interval if publisher.id == "wiley" else self._standard_interval)
        self.interval_combo.configure(values=("10", "15", "20", "30", "60") if publisher.id == "wiley" else ("2", "3", "5", "10", "20", "30", "60"))
        minimum_interval = MIN_WILEY_INTERVAL if publisher.id == "wiley" else MIN_REQUEST_INTERVAL
        self.interval_hint_var.set(f"至少 {minimum_interval:g} 秒，适用于每次请求；可输入小数，最多 {MAX_REQUEST_INTERVAL:g} 秒")
        if self._last_publisher_id == "springer":
            self._springer_supplements = self.supplements_var.get()
        elif self._last_publisher_id == "elsevier":
            self._elsevier_supplements = self.supplements_var.get()
        elif self._last_publisher_id == "wiley":
            self._wiley_supplements = self.supplements_var.get()
        self._last_publisher_id = publisher.id
        supplement_preferences = {"springer": self._springer_supplements, "elsevier": self._elsevier_supplements, "wiley": self._wiley_supplements}
        self.supplements_var.set(supplement_preferences.get(publisher.id, False))
        supports_supplements = publisher.id in supplement_preferences
        self.supplements_check.configure(state="normal" if supports_supplements and not self._busy else "disabled", text="下载补充材料" if supports_supplements else "补充材料（当前不支持）")
        self.journal_var.set(ALL_JOURNALS)
        self.journal_combo.configure(values=[ALL_JOURNALS, *publisher.journals])
        if publisher.id == "wiley" and publisher.enabled:
            self.publisher_hint_var.set(f"已收录 {len(publisher.journals)} 本期刊。正文 API 下载，Crossref 辅助寻找补充材料；未检索到记录不代表论文无 SI。")
            self.access_hint_var.set("WileyKey 自动读取，请求间隔至少 10 秒")
        elif publisher.id == "elsevier" and publisher.enabled:
            self.publisher_hint_var.set(f"已收录 {len(publisher.journals)} 本期刊。支持 DOI / PII 下载，自动读取 ElsevierKey；PDF 可用性取决于访问权限。")
            self.access_hint_var.set("自动读取 ElsevierKey，支持正文与补充材料")
        elif publisher.enabled:
            self.publisher_hint_var.set(f"已收录 {len(publisher.journals)} 本期刊。可输入期刊名搜索；不符合出版社或期刊的 DOI 将跳过。")
            self.access_hint_var.set("Cookie 与联系邮箱均可选")
        else:
            self.publisher_hint_var.set(f"已收录 {len(publisher.journals)} 本期刊。该出版社尚未实现下载，仍可导入、预览和导出列表。")
            self.access_hint_var.set("当前出版社尚未实现下载")
        self._refresh_import_rows()
        self._update_start_state()

    def _journal_focus(self, _event: object = None) -> None:
        if not self._busy:
            self.journal_combo.configure(values=[ALL_JOURNALS, *self._selected_publisher().journals])

    def _filter_journals(self, event: tk.Event) -> None:
        if self._busy or event.keysym in {"Up", "Down", "Left", "Right", "Return", "Escape", "Tab"}:
            return
        query = self.journal_var.get().strip().casefold()
        journals = self._selected_publisher().journals
        matches = journals if not query or query == ALL_JOURNALS.casefold() else [name for name in journals if query in name.casefold()]
        self.journal_combo.configure(values=[ALL_JOURNALS, *matches])

    def _update_start_state(self) -> None:
        enabled = not self._busy and self._selected_publisher().enabled and bool(self._eligible_references())
        self.start_button.configure(state="normal" if enabled else "disabled")
        self.export_button.configure(state="normal" if not self._busy and self._parsed and self._parsed.records else "disabled")

    def _import_only_reason(self, reference: ArticleReference) -> str:
        try:
            hostname = (urlsplit(reference.url).hostname or "").lower()
        except ValueError:
            hostname = ""
        elsevier_hint = bool(reference.pii or hostname == "sciencedirect.com" or hostname.endswith(".sciencedirect.com") or reference.doi.lower().startswith("10.1016/"))
        springer_doi = reference.doi.lower().startswith(("10.1007/", "10.1038/"))
        wiley_hint = reference.doi.lower().startswith(("10.1002/", "10.1111/")) or hostname == "wiley.com" or hostname.endswith(".wiley.com")
        publisher = self._selected_publisher()
        if not publisher.enabled:
            return "仅导入：当前出版社尚未实现下载，可导出解析列表。"
        if publisher.id == "wiley":
            if not reference.doi:
                return "仅导入：Wiley 仅支持 DOI，PII 或链接不能直接提交。"
            if springer_doi:
                return "仅导入：此 DOI 属于 Springer / Nature，请切换出版社后下载。"
            if elsevier_hint:
                return "仅导入：此记录属于 Elsevier / ScienceDirect，请切换出版社后下载。"
            return ""
        if wiley_hint:
            return "仅导入：此记录属于 Wiley，请切换出版社后下载。"
        if publisher.id == "elsevier":
            if springer_doi:
                return "仅导入：此记录属于 Springer / Nature，请切换出版社后下载。"
            return "" if reference.doi or reference.pii else "仅导入：此链接未包含 DOI 或 PII，可导出解析列表。"
        if elsevier_hint:
            return "仅导入：此记录属于 Elsevier / ScienceDirect，请切换出版社后下载。"
        if not reference.doi:
            return "仅导入：此记录没有 DOI，已保留文献链接。"
        return ""

    def _eligible_references(self) -> list[ArticleReference]:
        if self._parsed is None:
            return []
        return [reference for reference in self._parsed.records if not self._import_only_reason(reference)]

    def _refresh_import_rows(self) -> None:
        for key, row_id in self._row_ids.items():
            if key in self._results:
                continue
            reference = self._references.get(key)
            if reference is None:
                continue
            reason = self._import_only_reason(reference)
            status = "import_only" if reason else "pending"
            values = list(self.table.item(row_id, "values"))
            values[1] = STATUS_LABELS[status]
            values[5] = reason
            self.table.item(row_id, values=values, tags=(status,))
        if self._parsed is not None:
            eligible = len(self._eligible_references())
            self.parse_hint_var.set(f"已导入 {len(self._parsed.records)} 条文献，当前出版社可处理 {eligible} 条，另 {len(self._parsed.records) - eligible} 条仅导入。")

    def _invalidate_parse(self, *_args: object) -> None:
        if self._busy:
            return
        if _args and _args[0] == str(self.column_var) and self._source_mode == "paste":
            return
        self._generation += 1
        self._parsed = None
        self._parsed_key = None
        self._source_signature = None
        self._source_mode = "file"
        self._pasted_text = ""
        self.parse_button.configure(text="解析文件")
        self.column_entry.configure(state="normal")
        self.parse_hint_var.set("文件或列名已更改，请点击“解析文件”。")
        self._clear_table()
        self.progress_var.set(0)
        self.progress_text_var.set("尚未开始")
        self._update_start_state()

    def _pick_file(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="选择文献文件", filetypes=[("文献文件", "*.txt *.csv *.tsv *.xlsx *.ris *.bib *.bibtex"), ("RIS 文献", "*.ris"), ("BibTeX 文献", "*.bib *.bibtex"), ("文本文件", "*.txt"), ("CSV / TSV", "*.csv *.tsv"), ("Excel 工作簿", "*.xlsx"), ("所有文件", "*.*")])
        if path:
            self.file_var.set(path)
            self._parse_file()

    def _show_paste_dialog(self) -> None:
        if self._busy:
            return
        if self._paste_dialog is not None and self._paste_dialog.winfo_exists():
            self._paste_dialog.lift()
            return
        dialog = tk.Toplevel(self)
        self._paste_dialog = dialog
        dialog.title("粘贴 DOI / ScienceDirect 链接")
        dialog.geometry("760x430")
        dialog.minsize(600, 320)
        dialog.transient(self)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="粘贴 DOI 或文献链接，可每行一条；本次导入将替换当前列表。", wraplength=700).pack(anchor="w", pady=(0, 8))
        editor = tk.Text(frame, height=12, wrap="word", font=("Microsoft YaHei UI", 10))
        editor.pack(fill="both", expand=True)
        if self._source_mode == "paste":
            editor.insert("1.0", self._pasted_text)
        ttk.Label(frame, text="ScienceDirect 链接会提取 PII；选择 Elsevier 后自动读取环境变量 ElsevierKey。", wraplength=700).pack(anchor="w", pady=8)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")

        def close() -> None:
            dialog.grab_release()
            dialog.destroy()
            self._paste_dialog = None

        def import_text() -> None:
            text = editor.get("1.0", "end-1c")
            if not text.strip():
                messagebox.showerror("没有导入内容", "请粘贴 DOI 或文献链接。", parent=dialog)
                return
            close()
            self._import_reference_text(text)

        ttk.Button(buttons, text="取消", command=close).pack(side="right")
        ttk.Button(buttons, text="解析并导入", style="Primary.TButton", command=import_text).pack(side="right", padx=8)
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.grab_set()
        editor.focus_set()

    @staticmethod
    def _text_key(text: str) -> tuple[str, str]:
        return PASTE_SOURCE_LABEL, hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _import_reference_text(self, text: str) -> None:
        if self._busy:
            return
        if not text.strip():
            messagebox.showerror("没有导入内容", "请粘贴 DOI 或文献链接。", parent=self)
            return
        self.file_var.set(PASTE_SOURCE_LABEL)
        self._generation += 1
        generation = self._generation
        self._parsed = None
        self._parsed_key = None
        self._source_signature = None
        self._source_mode = "paste"
        self._pasted_text = text
        self._clear_table()
        key = self._text_key(text)
        self._set_busy(True)
        self.status_var.set("正在解析粘贴的 DOI / 文献链接…")
        self.parse_hint_var.set("正在读取并去重，请稍候…")

        def work() -> None:
            try:
                result = parse_reference_text(text)
                self._events.put(("parsed", generation, key, None, result))
            except Exception as exc:
                self._events.put(("parse_error", str(exc)))

        self._worker = threading.Thread(target=work, name="reference-parser", daemon=True)
        self._worker.start()

    def _export_references(self, path: Path | None = None) -> Path | None:
        if self._busy or self._parsed is None or not self._parsed.records:
            return None
        try:
            if not self._source_is_current():
                self._invalidate_parse()
                raise ValueError("导入内容已更改，请重新解析后导出。")
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法导出", str(exc), parent=self)
            return None
        if path is None:
            filename = filedialog.asksaveasfilename(parent=self, title="导出解析后的文献列表", defaultextension=".csv", initialfile="parsed_references.csv", filetypes=[("CSV 文献列表", "*.csv")])
            if not filename:
                return None
            path = Path(filename)
        try:
            if self._source_mode == "file" and self._parsed_key and path.resolve() == Path(self._parsed_key[0]).expanduser().resolve():
                raise ValueError("请选择其他文件名，导出的列表不能覆盖当前导入文件。")
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                fields = ("doi", "pii", "url", "title", "journal")
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for reference in self._parsed.records:
                    writer.writerow({field: getattr(reference, field) for field in fields})
        except (OSError, ValueError) as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return None
        self.status_var.set(f"已导出 {len(self._parsed.records)} 条文献记录：{path}")
        self._log(f"导出解析列表：{path}")
        return path

    def _pick_output(self) -> None:
        current = Path(self.output_var.get()).expanduser()
        initial = str(current) if current.is_dir() else str(Path.home())
        path = filedialog.askdirectory(parent=self, title="选择 PDF 保存目录", initialdir=initial, mustexist=False)
        if path:
            self.output_var.set(path)

    def _pick_cookie(self) -> None:
        path = filedialog.askopenfilename(parent=self._access_dialog or self, title="选择 Netscape 格式 Cookie 文件（可选）", filetypes=[("Cookie 文本文件", "*.txt *.cookies"), ("所有文件", "*.*")])
        if path:
            self.cookie_var.set(path)

    def _show_access_settings(self) -> None:
        if self._busy:
            return
        if self._access_dialog is not None and self._access_dialog.winfo_exists():
            self._access_dialog.lift()
            return
        dialog = tk.Toplevel(self)
        self._access_dialog = dialog
        dialog.title("API / Cookie 设置")
        dialog.transient(self)
        dialog.resizable(True, False)
        dialog.geometry(f"{min(760, max(600, self.winfo_screenwidth() - 100))}x570")
        dialog.minsize(600, 570)
        dialog.configure(background="#ffffff")
        frame = ttk.Frame(dialog, style="Card.TFrame", padding=20)
        frame.pack(fill="both", expand=True)
        frame.grid_columnconfigure(1, weight=1)
        ttk.Label(frame, text="Elsevier API Key", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.api_key_entry = ttk.Entry(frame, textvariable=self.api_key_var, show="*")
        self.api_key_entry.grid(row=0, column=1, columnspan=2, sticky="ew")
        ttk.Label(frame, text="自动读取环境变量 ElsevierKey，无需重复填写；兼容 ELSEVIER_API_KEY。", style="Muted.TLabel", wraplength=520).grid(row=1, column=1, columnspan=2, sticky="w", pady=(5, 10))
        ttk.Label(frame, text="Insttoken（可选）", style="Card.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 12))
        self.insttoken_entry = ttk.Entry(frame, textvariable=self.insttoken_var, show="*")
        self.insttoken_entry.grid(row=2, column=1, columnspan=2, sticky="ew")
        ttk.Checkbutton(frame, text="在本机加密保存 Elsevier 凭据", variable=self.remember_credentials_var, style="Card.TCheckbutton").grid(row=3, column=1, columnspan=2, sticky="w", pady=(8, 4))
        ttk.Label(frame, text="取消勾选后点击“完成”将删除本机已保存的凭据；本次会话仍可使用。", style="Muted.TLabel", wraplength=620).grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 16))
        ttk.Label(frame, text="联系邮箱", style="Card.TLabel").grid(row=5, column=0, sticky="w", padx=(0, 12))
        ttk.Entry(frame, textvariable=self.email_var).grid(row=5, column=1, columnspan=2, sticky="ew")
        ttk.Label(frame, text="可留空；填写后作为网络请求的联系信息。", style="Muted.TLabel").grid(row=6, column=1, columnspan=2, sticky="w", pady=(5, 16))
        ttk.Label(frame, text="Springer Cookie", style="Card.TLabel").grid(row=7, column=0, sticky="w", padx=(0, 12))
        ttk.Entry(frame, textvariable=self.cookie_var).grid(row=7, column=1, sticky="ew", padx=(0, 8))
        ttk.Button(frame, text="选择文件", command=self._pick_cookie).grid(row=7, column=2, sticky="ew")
        hint = "Springer 可选：导入已有授权会话的 Netscape 格式 Cookie 文件。\n留空时使用 SN_COOKIE_FILE（如已设置）。软件只保存文件路径。"
        ttk.Label(frame, text=hint, style="Muted.TLabel", wraplength=620).grid(row=8, column=0, columnspan=3, sticky="w", pady=(12, 16))
        ttk.Label(frame, text="Wiley API Token", style="Card.TLabel").grid(row=9, column=0, sticky="w", padx=(0, 12))
        self.wiley_api_key_entry = ttk.Entry(frame, textvariable=self.wiley_api_key_var, show="*")
        self.wiley_api_key_entry.grid(row=9, column=1, columnspan=2, sticky="ew")
        ttk.Label(frame, text="自动读取 WileyKey；在此修改只对本次会话生效，不写入设置或本机凭据文件。", style="Muted.TLabel", wraplength=620).grid(row=10, column=0, columnspan=3, sticky="w", pady=(6, 12))

        def close() -> None:
            dialog.grab_release()
            dialog.destroy()
            self._access_dialog = None

        def save_and_close() -> None:
            if self._save_api_credentials():
                self._save_preferences()
                close()

        ttk.Button(frame, text="完成", style="Primary.TButton", command=save_and_close).grid(row=11, column=2, sticky="e")
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.grab_set()

    def _save_api_credentials(self) -> bool:
        try:
            if self.remember_credentials_var.get():
                save_elsevier_credentials(self.api_key_var.get().strip(), self.insttoken_var.get().strip())
            else:
                clear_elsevier_credentials()
        except (OSError, ValueError):
            messagebox.showerror("凭据保存失败", "未能更新本机加密凭据，请检查用户目录权限后重试。", parent=self._access_dialog or self)
            return False
        return True

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns

    def _source_is_current(self) -> bool:
        raw_path = self.file_var.get().strip().strip('"')
        if self._source_mode == "paste":
            return raw_path == PASTE_SOURCE_LABEL and self._text_key(self._pasted_text) == self._parsed_key
        return (raw_path, self.column_var.get().strip()) == self._parsed_key and self._file_signature(Path(raw_path).expanduser()) == self._source_signature

    def _parse_file(self) -> None:
        if self._busy:
            return
        if self._source_mode == "paste" and self.file_var.get() == PASTE_SOURCE_LABEL:
            self._import_reference_text(self._pasted_text)
            return
        raw_path = self.file_var.get().strip().strip('"')
        path = Path(raw_path).expanduser()
        if not raw_path or not path.is_file():
            messagebox.showerror("无法读取文件", "请选择存在的文献文件。", parent=self)
            return
        try:
            signature = self._file_signature(path)
        except OSError as exc:
            messagebox.showerror("无法读取文件", str(exc), parent=self)
            return
        column = self.column_var.get().strip()
        generation = self._generation
        key = (raw_path, column)
        self._source_mode = "file"
        self._pasted_text = ""
        self._parsed = None
        self._clear_table()
        self._set_busy(True, downloading=False)
        self.status_var.set("正在解析文献文件…")
        self.parse_hint_var.set("正在读取并去重，请稍候…")

        def work() -> None:
            try:
                result = parse_doi_file(path, column or None)
                if self._file_signature(path) != signature:
                    raise ValueError("解析期间文件发生变化，请重新解析。")
                self._events.put(("parsed", generation, key, signature, result))
            except Exception as exc:
                self._events.put(("parse_error", str(exc)))

        self._worker = threading.Thread(target=work, name="doi-parser", daemon=True)
        self._worker.start()

    def _start_download(self) -> None:
        references = self._eligible_references()
        if self._busy or self._parsed is None or not references or not self._selected_publisher().enabled:
            return
        publisher = self._selected_publisher()
        journal = self.journal_var.get().strip()
        if journal == ALL_JOURNALS or not journal:
            journal = ""
        elif journal not in publisher.journals:
            messagebox.showerror("请选择期刊", "请从期刊下拉列表中选择完整期刊名称，或选择“全部期刊（该出版社）”。", parent=self)
            return
        try:
            if not self._source_is_current():
                self._invalidate_parse()
                raise ValueError("导入内容已更改，请重新解析后开始下载。")
            raw_output = self.output_var.get().strip().strip('"')
            if not raw_output:
                raise ValueError("请选择 PDF 保存目录。")
            common_config = dict(
                email=self.email_var.get().strip(), output_dir=Path(raw_output).expanduser(),
                publisher_id=publisher.id, publisher_name=publisher.name,
                journal=journal, known_journals=tuple(publisher.journals),
                interval=parse_wiley_interval(self.interval_var.get()) if publisher.id == "wiley" else parse_request_interval(self.interval_var.get()),
            )
            if publisher.id == "elsevier":
                config = ElsevierConfig(**common_config, include_supplements=self.supplements_var.get(),
                                        api_key=self.api_key_var.get().strip(), insttoken=self.insttoken_var.get().strip())
                validate_elsevier_config(config)
            elif publisher.id == "wiley":
                config = WileyConfig(**common_config, include_supplements=self.supplements_var.get(), api_key=self.wiley_api_key_var.get().strip())
                validate_wiley_config(config)
            else:
                config = DownloadConfig(**common_config,
                    cookie_file=Path(self.cookie_var.get().strip().strip('"')).expanduser() if self.cookie_var.get().strip().strip('"') else None,
                    include_supplements=self.supplements_var.get())
                validate_config(config)
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法开始下载", str(exc), parent=self)
            return
        self._save_preferences()
        self._cancel.clear()
        self._manifest = None
        self.manifest_button.configure(state="disabled")
        self._counts.clear()
        self._results.clear()
        self._update_counts()
        self._populate_table(self._parsed.records)
        self._active_keys = {reference.key for reference in references}
        self._batch_publisher_id = publisher.id
        self.progress_var.set(0)
        self.progress_text_var.set(f"0 / {len(references)}")
        self.status_var.set("下载中，可以随时取消任务。")
        import_only = len(self._parsed.records) - len(references)
        self._log(f"开始下载：{publisher.name}，{journal or ALL_JOURNALS}，共 {len(references)} 条文献；另 {import_only} 条仅导入；每次请求至少间隔 {config.interval:g} 秒。")
        self._set_busy(True, downloading=True)
        self.notebook.select(0)
        self._show_progress_panel()

        def work() -> None:
            try:
                runner = {"elsevier": run_elsevier_batch, "wiley": run_wiley_batch}.get(publisher.id, run_batch)
                items = references if publisher.id in {"elsevier", "wiley"} else [reference.doi for reference in references]
                summary = runner(
                    items, config,
                    on_result=lambda index, total, result: self._events.put(("result", index, total, result)),
                    on_log=lambda text: self._events.put(("log", text)),
                    cancel_event=self._cancel,
                )
                self._events.put(("finished", summary))
            except Exception as exc:
                self._events.put(("download_error", str(exc)))

        self._worker = threading.Thread(target=work, name="paper-downloader", daemon=True)
        self._worker.start()

    def _set_busy(self, value: bool, downloading: bool = False) -> None:
        self._busy = value
        for widget in self._editable_widgets:
            widget.configure(state="disabled" if value else "normal")
        self.publisher_combo.configure(state="disabled" if value else "readonly")
        self.journal_combo.configure(state="disabled" if value else "normal")
        self.column_entry.configure(state="disabled" if value or self._source_mode == "paste" else "normal")
        self.supplements_check.configure(state="normal" if not value and self._selected_publisher().id in {"springer", "elsevier", "wiley"} else "disabled")
        self.parse_button.configure(text="重新解析" if self._source_mode == "paste" else "解析文件")
        self.cancel_button.configure(state="normal" if value and downloading else "disabled")
        self._update_start_state()

    def _cancel_download(self) -> None:
        self._cancel.set()
        self.cancel_button.configure(state="disabled")
        self.status_var.set("正在取消，等待当前网络请求结束并保存结果清单…")
        self._log("已请求取消任务。")

    def _drain_events(self) -> None:
        try:
            for _ in range(150):
                event = self._events.get_nowait()
                kind = event[0]
                if kind == "parsed":
                    _, generation, key, signature, result = event
                    self._set_busy(False)
                    if generation == self._generation:
                        self._parsed = result
                        self._parsed_key = key
                        self._source_signature = signature
                        self._populate_table(result.records)
                        eligible = len(self._eligible_references())
                        import_only = len(result.records) - eligible
                        self.parse_hint_var.set(f"已导入 {len(result.records)} 条文献，其中 DOI {len(result.dois)} 条、仅导入 {import_only} 条；移除重复 {result.duplicates} 条。")
                        if not result.records:
                            self.status_var.set("没有识别到 DOI 或文献链接，请检查输入内容。")
                        elif not self._selected_publisher().enabled or not eligible:
                            self.status_var.set("已完成解析，可预览或导出列表。切换到对应出版社可处理支持的文献。")
                        else:
                            self.status_var.set(f"可提交 {eligible} 条文献下载；{import_only} 条仅导入记录保留在列表中，可导出 CSV。")
                        self._log(f"解析来源：{key[0]}；文献 {len(result.records)} 条、DOI {len(result.dois)} 条、仅导入 {import_only} 条。")
                        for warning in result.warnings:
                            self._log(warning)
                        self._update_start_state()
                elif kind == "parse_error":
                    self._parsed = None
                    self._parsed_key = None
                    self._source_signature = None
                    self._clear_table()
                    self._set_busy(False)
                    self.parse_hint_var.set("解析失败，请检查文件格式和 DOI 列名。")
                    self.status_var.set("解析失败")
                    self._log(f"解析失败：{event[1]}")
                    if not self._closing:
                        messagebox.showerror("解析失败", event[1], parent=self)
                elif kind == "result":
                    _, index, total, result = event
                    self._render_result(result)
                    self.progress_var.set(index / total * 100 if total else 0)
                    self.progress_text_var.set(f"{index} / {total}")
                elif kind == "log":
                    self._log(event[1])
                elif kind == "finished":
                    self._finish_batch(event[1])
                elif kind == "download_error":
                    self._set_busy(False)
                    self.status_var.set("任务中断。已写入的 PDF 和结果清单保留在保存目录。")
                    self._log(f"任务中断：{event[1]}")
                    if not self._closing:
                        messagebox.showerror("下载中断", event[1], parent=self)
        except queue.Empty:
            pass
        if self._closing and not self._busy:
            self.destroy()
            return
        self._poll_id = self.after(80, self._drain_events)

    def _finish_batch(self, summary: BatchSummary) -> None:
        self._set_busy(False)
        self._manifest = Path(summary.manifest_path)
        self.manifest_button.configure(state="normal" if self._manifest.exists() else "disabled")
        for result in summary.results:
            if self._result_key(result) not in self._results:
                self._render_result(result)
        stopped = bool(summary.stop_reason)
        processed = sum(key in self._results for key in self._active_keys)
        total = len(self._active_keys)
        if summary.cancelled or stopped:
            for key, row_id in self._row_ids.items():
                if key in self._active_keys and key not in self._results:
                    values = list(self.table.item(row_id, "values"))
                    values[1] = "未处理（暂停）" if stopped else "未处理"
                    values[5] = "任务已暂停，可稍后重试" if stopped else "任务已取消，下次导入文件可继续"
                    self.table.item(row_id, values=values, tags=("pending" if stopped else "cancelled",))
            self.progress_var.set(processed / total * 100 if total else 0)
            self.progress_text_var.set(f"{processed} / {total}")
            self.status_var.set(f"任务已暂停：{summary.stop_reason}。已处理 {processed}/{total}。" if stopped else f"任务已取消。结果清单：{self._manifest}")
        else:
            self.progress_var.set(100)
            self.status_var.set(f"任务完成。结果清单：{self._manifest}")
        outcome = f"任务已暂停：{summary.stop_reason}" if stopped else ("任务取消" if summary.cancelled else "任务完成")
        self._log(f"{outcome}；结果清单：{self._manifest}")

    @staticmethod
    def _result_key(result: DownloadResult) -> str:
        return result.reference_key or result.doi or (f"pii:{result.pii}" if result.pii else "")

    def _render_result(self, result: DownloadResult) -> None:
        key = self._result_key(result)
        previous = self._results.get(key)
        if previous is not None:
            self._counts[previous.status] -= 1
        self._results[key] = result
        self._counts[result.status] += 1
        row_id = self._row_ids.get(key)
        supplements_text = self._supplements_text(result)
        reference = self._references.get(key)
        identifier = result.doi or (f"PII: {result.pii}" if result.pii else (f"PII: {reference.pii}" if reference and reference.pii else key))
        values = (identifier, STATUS_LABELS.get(result.status, result.status), supplements_text, result.journal or (reference.journal if reference else ""), result.title or (reference.title if reference else ""), result.error or result.file_path)
        if row_id:
            self.table.item(row_id, values=values, tags=(result.status,))
            self.table.see(row_id)
        else:
            row_id = self.table.insert("", "end", values=values, tags=(result.status,))
            self._row_ids[key] = row_id
            self._row_keys[row_id] = key
        self._update_counts()
        if row_id in self.table.selection():
            self._show_result_details()

    @staticmethod
    def _supplements_text(result: DownloadResult) -> str:
        status = result.supplements_status
        if status == "disabled":
            return "未启用"
        if status == "unsupported":
            return "接口不支持"
        if status == "not_indexed":
            return "未检索到SI记录"
        if status == "manual_required":
            return "需手动获取"
        if status == "none":
            return "无附件"
        if status == "discovery_failed":
            return "发现失败"
        ready = result.supplements_downloaded + result.supplements_existing
        text = f"{ready}/{result.supplements_total}"
        if result.supplements_failed:
            text += f"（失败 {result.supplements_failed}）"
        if status == "cancelled":
            text += "（已取消）"
        return text

    def _update_counts(self) -> None:
        self.counts_var.set(f"已下载 {self._counts['success']}    已存在 {self._counts['exists']}    部分完成 {self._counts['partial']}    无正文 {self._counts['no_pdf']}    访问受限 {self._counts['access_denied']}    筛除 {self._counts['filtered']}    失败 {self._counts['failed']}")

    def _clear_table(self) -> None:
        children = self.table.get_children()
        if children:
            self.table.delete(*children)
        self._row_ids.clear()
        self._row_keys.clear()
        self._references.clear()
        self._active_keys.clear()
        self._manifest = None
        self.manifest_button.configure(state="disabled")
        self.progress_var.set(0)
        self.progress_text_var.set("尚未开始")
        self._results.clear()
        self._counts.clear()
        self._update_counts()
        self.detail_var.set("选择记录可查看 DOI、PII、链接和下载结果。")
        self.record_button.configure(state="disabled")
        self.paper_folder_button.configure(state="disabled")
        self.article_button.configure(state="disabled")

    def _populate_table(self, references: list[ArticleReference]) -> None:
        self._clear_table()
        for reference in references:
            key = reference.key
            self._references[key] = reference
            reason = self._import_only_reason(reference)
            status = "import_only" if reason else "pending"
            identifier = reference.doi or (f"PII: {reference.pii}" if reference.pii else reference.url)
            row_id = self.table.insert("", "end", values=(identifier, STATUS_LABELS[status], "", reference.journal, reference.title, reason), tags=(status,))
            self._row_ids[key] = row_id
            self._row_keys[row_id] = key

    def _refresh_detail_text(self, *_args: object) -> None:
        self._detail_text.configure(state="normal")
        self._detail_text.delete("1.0", "end")
        self._detail_text.insert("1.0", self.detail_var.get())
        self._detail_text.configure(state="disabled")

    def _show_result_details(self, _event: object = None) -> None:
        selected = self.table.selection()
        if not selected:
            return
        key = self._row_keys.get(selected[0])
        if key is None:
            return
        reference = self._references.get(key)
        parts = []
        if reference:
            for label, value in (("DOI", reference.doi), ("PII", reference.pii), ("链接", reference.url), ("标题", reference.title), ("期刊", reference.journal)):
                if value:
                    parts.append(f"{label}：{value}")
        result = self._results.get(key)
        if result:
            parts.append(f"{result.doi}  ·  {STATUS_LABELS.get(result.status, result.status)}  ·  补充材料：{self._supplements_text(result)}；新下载 {result.supplements_downloaded}，已有 {result.supplements_existing}，失败 {result.supplements_failed}")
            if result.error:
                parts.append(f"说明：{result.error}")
            if result.file_path:
                parts.append(f"正文：{result.file_path}")
            self.record_button.configure(state="normal" if result.record_path else "disabled")
            self.paper_folder_button.configure(state="normal" if result.record_path or result.file_path else "disabled")
        else:
            if reference and self._import_only_reason(reference):
                parts.append(self._import_only_reason(reference))
            self.record_button.configure(state="disabled")
            self.paper_folder_button.configure(state="disabled")
        self.detail_var.set("\n".join(parts) or key)
        self.article_button.configure(state="normal" if self._selected_article_url() else "disabled")

    def _selected_result(self) -> DownloadResult | None:
        selection = self.table.selection()
        if not selection:
            return None
        key = self._row_keys.get(selection[0])
        return self._results.get(key) if key else None

    def _open_selected_record(self) -> None:
        result = self._selected_result()
        if result and result.record_path:
            self._open_path(Path(result.record_path))

    def _open_selected_folder(self) -> None:
        result = self._selected_result()
        if result and (result.record_path or result.file_path):
            self._open_path(Path(result.record_path or result.file_path).parent)

    def _selected_article_url(self) -> str:
        selected = self.table.selection()
        if not selected:
            return ""
        key = self._row_keys.get(selected[0])
        reference = self._references.get(key) if key else None
        result = self._selected_result()
        doi = (result.doi if result else "") or (reference.doi if reference else "")
        candidates = [result.article_url if result else "", reference.url if reference else "", f"https://doi.org/{quote(doi, safe='/')}" if doi else ""]
        for url in candidates:
            try:
                parsed = urlsplit(url)
                if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
                    return url
            except ValueError:
                continue
        return ""

    def _open_selected_article(self) -> None:
        url = self._selected_article_url()
        if not url:
            return
        try:
            opened = webbrowser.open(url, new=2)
        except (OSError, webbrowser.Error):
            opened = False
        if not opened:
            messagebox.showerror("无法打开文章页面", "无法启动浏览器，可从论文记录 JSON 中复制文章地址。", parent=self)

    def _open_selected_pdf(self, _event: object = None) -> None:
        result = self._selected_result()
        if result and result.file_path:
            self._open_path(Path(result.file_path))

    def _log(self, text: str) -> None:
        for secret in (self.api_key_var.get().strip(), self.insttoken_var.get().strip(), self.wiley_api_key_var.get().strip()):
            if secret:
                text = text.replace(secret, "[已隐藏]")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{datetime.now():%H:%M:%S}] {text}\n")
        # Keep the UI responsive for long batches; the manifest is the durable record.
        if int(self.log_text.index("end-1c").split(".")[0]) > 4000:
            self.log_text.delete("1.0", "501.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _open_output(self) -> None:
        raw = self.output_var.get().strip().strip('"')
        if not raw:
            messagebox.showinfo("保存目录", "请先选择保存目录。", parent=self)
            return
        path = Path(raw).expanduser()
        if not path.is_dir():
            messagebox.showinfo("保存目录", "保存目录尚不存在，开始下载后会自动创建。", parent=self)
            return
        self._open_path(path)

    def _open_manifest(self) -> None:
        if self._manifest:
            self._open_path(self._manifest)

    def _open_path(self, path: Path) -> None:
        if not path.exists():
            messagebox.showerror("无法打开", f"文件或目录不存在：\n{path}", parent=self)
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("无法打开", str(exc), parent=self)

    def _save_preferences(self) -> None:
        active_wiley = self._selected_publisher().id == "wiley"
        try:
            interval = parse_request_interval(self._standard_interval if active_wiley else self.interval_var.get())
        except ValueError:
            interval = MIN_REQUEST_INTERVAL
        try:
            wiley_interval = parse_wiley_interval(self.interval_var.get() if active_wiley else self._wiley_interval)
        except ValueError:
            wiley_interval = MIN_WILEY_INTERVAL
        try:
            save_settings({
                "publisher_id": self._selected_publisher().id,
                "journal": self.journal_var.get(),
                "email": self.email_var.get().strip(),
                "output_dir": self.output_var.get().strip().strip('"'),
                "doi_column": self.column_var.get().strip(),
                "cookie_file": self.cookie_var.get().strip().strip('"'),
                "include_supplements": "true" if (self.supplements_var.get() if self._selected_publisher().id == "springer" else self._springer_supplements) else "false",
                "elsevier_include_supplements": "true" if (self.supplements_var.get() if self._selected_publisher().id == "elsevier" else self._elsevier_supplements) else "false",
                "wiley_include_supplements": "true" if (self.supplements_var.get() if self._selected_publisher().id == "wiley" else self._wiley_supplements) else "false",
                "request_interval": f"{interval:g}",
                "wiley_request_interval": f"{wiley_interval:g}",
            })
        except OSError as exc:
            self._log(f"设置未保存：{exc}")

    def _on_close(self) -> None:
        if self._closing:
            return
        self._save_preferences()
        if self._busy:
            self._closing = True
            self._cancel.set()
            self.cancel_button.configure(state="disabled")
            self.status_var.set("正在结束任务并保存结果，请稍候…")
            self._log("正在关闭软件，已请求停止任务。")
        else:
            self.destroy()

    def destroy(self) -> None:
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        super().destroy()


def main() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    app = DownloaderApp()
    app.mainloop()
