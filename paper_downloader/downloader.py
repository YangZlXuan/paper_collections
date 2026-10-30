"""Download Springer/Nature PDFs and supplementary files from official article pages."""
from __future__ import annotations

import csv
import hashlib
import http.cookiejar
import json
import math
import os
import re
import threading
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from numbers import Number
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urldefrag, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .catalog import normalize_journal
from .doi_parser import extract_dois


MIN_REQUEST_INTERVAL = 2.0
MAX_REQUEST_INTERVAL = 60.0


def parse_request_interval(value: object) -> float:
    """Parse the user-selected interval without permitting a faster request rate."""
    message = "请求间隔须为 2–60 秒的有效数字，不能小于 2 秒。"
    if isinstance(value, bool) or not isinstance(value, (str, Number)):
        raise ValueError(message)
    try:
        interval = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(message) from None
    if not math.isfinite(interval) or not MIN_REQUEST_INTERVAL <= interval <= MAX_REQUEST_INTERVAL:
        raise ValueError(message)
    return interval


@dataclass
class DownloadConfig:
    email: str
    output_dir: Path
    publisher_id: str = "springer"
    publisher_name: str = "Springer Nature"
    journal: str = ""
    known_journals: tuple[str, ...] = ()
    timeout: float = 90
    interval: float = MIN_REQUEST_INTERVAL
    retries: int = 2
    cookie_file: Path | None = None
    include_supplements: bool = True


@dataclass
class DownloadResult:
    doi: str
    status: str
    journal: str = ""
    title: str = ""
    pdf_url: str = ""
    file_path: str = ""
    error: str = ""
    article_url: str = ""
    supplements_total: int = 0
    supplements_downloaded: int = 0
    supplements_existing: int = 0
    supplements_failed: int = 0
    record_path: str = ""
    reference_key: str = ""
    pii: str = ""
    supplements_status: str = ""


@dataclass
class BatchSummary:
    results: list[DownloadResult]
    manifest_path: Path
    cancelled: bool = False
    stop_reason: str = ""


class DownloadCancelled(Exception):
    pass


class AccessDenied(Exception):
    """An authentication, access, rate-limit or destination restriction."""


_DOMAINS = ("nature.com", "springer.com", "springernature.com")
_ASSET_HOSTS = {"static-content.springer-cdn.com"}
_MAX_HTML = 4 * 1024 * 1024
_MAX_FILE = 150 * 1024 * 1024
_TRANSIENT = {500, 502, 503, 504}
_REDIRECTS = {301, 302, 303, 307, 308}
_LABELS = {"success": "下载成功", "exists": "文件已存在", "partial": "部分完成",
           "no_pdf": "未发现正文 PDF", "filtered": "不符合期刊筛选", "access_denied": "访问受限",
           "failed": "下载失败", "cancelled": "已取消"}


def allowed(url: str, *, asset: bool = False) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        domain_ok = any(host == domain or host.endswith("." + domain) for domain in _DOMAINS)
        return (parsed.scheme.lower() == "https" and parsed.port in (None, 443)
                and parsed.username is None and parsed.password is None
                and (domain_ok or (asset and host in _ASSET_HOSTS)))
    except (ValueError, AttributeError):
        return False


def _check_cancel(event: threading.Event) -> None:
    if event.is_set():
        raise DownloadCancelled()


def _wait(event: threading.Event, seconds: float) -> None:
    if event.wait(max(0.0, seconds)):
        raise DownloadCancelled()


def _cookie_path(config: DownloadConfig) -> Path | None:
    value = config.cookie_file or os.environ.get("SN_COOKIE_FILE", "").strip()
    return Path(value).expanduser() if value else None


def _load_cookies(config: DownloadConfig) -> http.cookiejar.MozillaCookieJar:
    policy = http.cookiejar.DefaultCookiePolicy(
        strict_ns_domain=http.cookiejar.DefaultCookiePolicy.DomainStrictNonDomain)
    jar = http.cookiejar.MozillaCookieJar(policy=policy)
    path = _cookie_path(config)
    if path:
        try:
            jar.load(str(path), ignore_discard=True, ignore_expires=False)
        except (OSError, http.cookiejar.LoadError, ValueError):
            raise ValueError("Cookie 文件不可读取或不是有效的 Netscape cookies.txt 格式。") from None
        for cookie in list(jar):
            host = (cookie.domain or "").lstrip(".")
            if not host or not allowed("https://" + host + "/", asset=True):
                jar.clear(cookie.domain, cookie.path, cookie.name)
    return jar


def validate_config(config: DownloadConfig) -> None:
    if config.publisher_id != "springer":
        raise ValueError("当前版本仅支持 Springer Nature 出版社。")
    if config.email.strip() and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", config.email.strip()):
        raise ValueError("联系邮箱可留空；填写时须使用有效邮箱格式。")
    if not math.isfinite(config.timeout) or not 1 <= config.timeout <= 300:
        raise ValueError("网络超时须为 1–300 秒。")
    parse_request_interval(config.interval)
    if not isinstance(config.retries, int) or not 0 <= config.retries <= 5:
        raise ValueError("重试次数须为 0–5 次。")
    _load_cookies(config)


def article_url(doi: str) -> str:
    if doi.lower().startswith("10.1038/"):
        return "https://www.nature.com/articles/" + quote(doi.split("/", 1)[1], safe="")
    return "https://link.springer.com/article/" + quote(doi, safe="/")


def paper_directory_name(doi: str) -> str:
    readable = re.sub(r"[^a-zA-Z0-9._-]+", "_", doi).strip(". ")[:90] or "paper"
    return f"{readable}_{hashlib.sha256(doi.encode('utf-8')).hexdigest()[:12]}"


def _asset_filename(url: str) -> str:
    suffix = Path(urlsplit(url).path).suffix.lower()
    valid = {".pdf", ".zip", ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".csv",
             ".txt", ".xml", ".mp4", ".mov", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff"}
    return f"SI_{hashlib.sha256(url.encode('utf-8')).hexdigest()[:20]}{suffix if suffix in valid else '.bin'}"


def is_valid_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                return False
            handle.seek(0, 2)
            size = handle.tell()
            if size < 15 or size > _MAX_FILE:
                return False
            handle.seek(max(0, size - 4096))
            return b"%%EOF" in handle.read()
    except (OSError, ValueError):
        return False


def _error(exc: Exception) -> str:
    # requests exceptions can contain sensitive authorization redirect URLs.
    if isinstance(exc, DownloadCancelled):
        return "用户取消下载。"
    if isinstance(exc, requests.RequestException):
        return f"网络请求失败（{type(exc).__name__}）。"
    return (str(exc) or type(exc).__name__)[:1000]


class _Client:
    def __init__(self, session: requests.Session, config: DownloadConfig, event: threading.Event):
        self.session, self.config, self.event = session, config, event
        # Snapshot once: edits to the UI/config during a batch cannot change its
        # request rate or bypass validation through direct client construction.
        self.interval = parse_request_interval(config.interval)
        self.last_request: float | None = None
        self.cookies = _load_cookies(config)
        self.session.cookies = self.cookies

    def request(self, url: str, *, asset: bool = False) -> requests.Response:
        for redirect in range(7):
            if not allowed(url, asset=asset):
                raise AccessDenied("请求或跳转地址不在允许的出版社域名内。")
            for attempt in range(self.config.retries + 1):
                _check_cancel(self.event)
                if self.last_request is not None:
                    _wait(self.event, self.interval - (time.monotonic() - self.last_request))
                prepared = self.session.prepare_request(requests.Request("GET", url))
                # Rebuild with original strict policy after requests merges jars.
                prepared.headers.pop("Cookie", None)
                prepared.prepare_cookies(self.cookies)
                settings = self.session.merge_environment_settings(prepared.url, {}, True, None, None)
                self.last_request = time.monotonic()
                try:
                    response = self.session.send(prepared, timeout=(min(15.0, self.config.timeout), self.config.timeout),
                                                 allow_redirects=False, **settings)
                except (requests.ConnectionError, requests.Timeout):
                    if attempt == self.config.retries:
                        raise
                    _wait(self.event, min(8.0, 0.5 * 2 ** attempt))
                    continue
                try:
                    _check_cancel(self.event)
                except DownloadCancelled:
                    response.close()
                    raise
                code = response.status_code
                if code in (401, 403, 429):
                    response.close()
                    raise AccessDenied(f"访问受限或请求过于频繁（HTTP {code}）。")
                if code in _TRANSIENT and attempt < self.config.retries:
                    response.close()
                    _wait(self.event, min(8.0, 0.5 * 2 ** attempt))
                    continue
                if code in _REDIRECTS:
                    location = response.headers.get("Location")
                    response.close()
                    if not location:
                        raise AccessDenied("出版社跳转缺少目标地址。")
                    if redirect == 6:
                        raise AccessDenied("出版社页面跳转超过 6 次。")
                    url = urljoin(url, location)
                    break
                if code >= 400:
                    response.close()
                    raise ValueError(f"服务器返回 HTTP {code}。")
                return response
        raise AccessDenied("出版社页面跳转超过 6 次。")


def _read_page(response: requests.Response, event: threading.Event) -> bytes:
    body = bytearray()
    for chunk in response.iter_content(65536):
        _check_cancel(event)
        if len(body) + len(chunk) > _MAX_HTML:
            raise ValueError("论文网页超过 4 MB，已停止解析。")
        body.extend(chunk)
    return bytes(body)


def _discover(client: _Client, doi: str) -> dict:
    for attempt in range(client.config.retries + 1):
        response = client.request(article_url(doi))
        try:
            with response:
                if "html" not in response.headers.get("Content-Type", "").lower():
                    raise AccessDenied("出版社论文页未返回 HTML，可能需要登录。")
                html = _read_page(response, client.event)
                final_url = response.url or article_url(doi)
            break
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
            if attempt == client.config.retries:
                raise
            _wait(client.event, min(8.0, 0.5 * 2 ** attempt))
    soup = BeautifulSoup(html, "html.parser")
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        name = str(tag.get("name") or tag.get("property") or "").casefold()
        value = str(tag.get("content") or "").strip()
        if value:
            meta.setdefault(name, value)
    declared = []
    for name in ("citation_doi", "prism.doi", "dc.identifier"):
        declared.extend(extract_dois(meta.get(name, "")))
    if declared and any(value.casefold() != doi.casefold() for value in declared):
        raise ValueError("论文网页 DOI 元数据与请求不一致。")
    journal = next((meta[name] for name in ("citation_journal_title", "citation_journal_name", "prism.publicationname") if meta.get(name)), "")
    title = meta.get("citation_title") or meta.get("dc.title") or meta.get("og:title") or (soup.title.get_text(" ", strip=True) if soup.title else "")
    article_metadata = bool(declared or (journal and meta.get("citation_title")))
    if not article_metadata:
        login_form = any(re.search(r"login|signin|sign-in|authenticate", str(form.get("action") or ""), re.I)
                         or form.select_one('input[type="password"]') for form in soup.find_all("form"))
        blocked_title = re.search(r"access denied|just a moment|sign in|log in|captcha|robot check|verify.*human", title, re.I)
        if login_form or blocked_title:
            raise AccessDenied("出版社返回登录或访问验证页面。")
    pdfs: list[str] = []
    supplements: list[str] = []
    for name in ("citation_pdf_url", "citation_fulltext_pdf_url"):
        if meta.get(name):
            pdfs.append(urljoin(final_url, meta[name]))
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith("#"):
            continue
        full = urljoin(final_url, href)
        if urldefrag(full)[0] in (urldefrag(final_url)[0], article_url(doi)):
            continue
        if not allowed(full, asset=True):
            continue
        label, low = anchor.get_text(" ", strip=True).casefold(), full.casefold()
        excluded = any(word in label or word.replace(" ", "-") in low or word.replace(" ", "_") in low
                       for word in ("peer review", "reporting summary"))
        path = urlsplit(full).path.casefold()
        is_si = ("/esm/" in low or "moesm" in path or "_esm" in path or
                 (("supplementary" in label or "supplemental" in label) and bool(Path(path).suffix)))
        if is_si and not excluded:
            supplements.append(full)
        if (("/content/pdf/" in path or path.endswith(".pdf")) and not is_si
                and "supplement" not in low and not excluded
                and ("download pdf" in label or "/content/pdf/" in path)):
            pdfs.append(full)
    def unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(urldefrag(value)[0] for value in values if allowed(value, asset=True)))
    return {"article_url": final_url, "journal": journal, "title": title,
            "pdf_urls": unique(pdfs), "supplement_urls": unique(supplements)}


def _looks_html(prefix: bytes) -> bool:
    return re.match(br"(?is)\s*(?:\xef\xbb\xbf)?\s*(?:<!doctype\s+html|<html\b|<head\b|<body\b|<form\b)", prefix) is not None


def _digest(path: Path, event: threading.Event) -> tuple[int, str]:
    size, digest = 0, hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            _check_cancel(event)
            size += len(chunk)
            if size > _MAX_FILE:
                raise ValueError("文件超过 150 MB。")
            digest.update(chunk)
    return size, digest.hexdigest()


def _existing_asset(path: Path, url: str, previous: dict, event: threading.Event) -> dict | None:
    try:
        if (previous.get("url") != url or previous.get("file") != path.name
                or previous.get("status") not in ("success", "exists") or not path.is_file()):
            return None
        if path.stat().st_size != previous.get("bytes") or not 0 < path.stat().st_size <= _MAX_FILE:
            return None
        size, sha = _digest(path, event)
        if sha != previous.get("sha256") or (path.suffix == ".pdf" and not is_valid_pdf(path)):
            return None
        with path.open("rb") as handle:
            if _looks_html(handle.read(4096)):
                return None
        return {**previous, "status": "exists", "bytes": size, "sha256": sha}
    except OSError:
        return None


def _download_asset(client: _Client, url: str, target: Path, *, expect_pdf: bool = False) -> dict:
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        for attempt in range(client.config.retries + 1):
            response = client.request(url, asset=True)
            try:
                with response, temp.open("wb") as handle:
                    if "html" in response.headers.get("Content-Type", "").lower():
                        raise AccessDenied("文件地址返回 HTML 登录或错误页面。")
                    length = response.headers.get("Content-Length", "")
                    expected = int(length) if length.isdigit() else None
                    if expected is not None and expected > _MAX_FILE:
                        raise ValueError("文件超过 150 MB。")
                    size, digest, prefix = 0, hashlib.sha256(), b""
                    for chunk in response.iter_content(128 * 1024):
                        _check_cancel(client.event)
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > _MAX_FILE:
                            raise ValueError("文件超过 150 MB。")
                        prefix = (prefix + chunk)[:4096]
                        if _looks_html(prefix):
                            raise AccessDenied("文件内容是 HTML 登录或错误页面。")
                        digest.update(chunk)
                        handle.write(chunk)
                    final_url = response.url or url
                    if expected is not None and not response.headers.get("Content-Encoding") and size != expected:
                        raise ValueError("文件传输不完整：实际大小与 Content-Length 不一致。")
                _check_cancel(client.event)
                if size == 0:
                    raise ValueError("下载文件为空。")
                if (expect_pdf or target.suffix == ".pdf" or prefix.startswith(b"%PDF-")) and not is_valid_pdf(temp):
                    raise ValueError("返回内容不是完整 PDF（缺少文件头或结束标记）。")
                if target.suffix in {".zip", ".docx", ".xlsx", ".pptx"} and not zipfile.is_zipfile(temp):
                    raise ValueError("压缩文件或 Office 文件不完整。")
                temp.replace(target)
                return {"url": url, "final_url": final_url, "file": target.name, "status": "success",
                        "bytes": size, "sha256": digest.hexdigest()}
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
                if attempt == client.config.retries:
                    raise
                _wait(client.event, min(8.0, 0.5 * 2 ** attempt))
    finally:
        temp.unlink(missing_ok=True)
    raise ValueError("文件下载失败。")


def _read_manifest(path: Path) -> dict:
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return {}
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            return {}
        def valid_asset(value: object) -> bool:
            return (isinstance(value, dict)
                    and all(isinstance(value.get(key), str) for key in ("url", "file", "status", "sha256"))
                    and isinstance(value.get("bytes"), int) and value["bytes"] >= 0)
        record["pdf"] = record.get("pdf") if valid_asset(record.get("pdf")) else None
        supplements = record.get("supplements")
        record["supplements"] = [item for item in supplements if valid_asset(item)] if isinstance(supplements, list) else []
        assets = record.get("assets")
        record["assets"] = {item["url"]: item for item in assets.values() if valid_asset(item)} if isinstance(assets, dict) else {}
        return record
    except (OSError, ValueError):
        return {}


def _save_manifest(path: Path, record: dict) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _process(client: _Client, doi: str) -> DownloadResult:
    result = DownloadResult(doi=doi, status="failed", article_url=article_url(doi))
    directory = Path(client.config.output_dir) / paper_directory_name(doi)
    record_path = directory / "manifest.json"
    record = {"schema_version": 1, "doi": doi, "article_url": result.article_url, "journal": "", "title": "",
              "status": "in_progress", "include_supplements": client.config.include_supplements,
              "pdf": None, "supplements": [], "assets": {}, "errors": []}
    denied = False
    try:
        _check_cancel(client.event)
        directory.mkdir(parents=True, exist_ok=True)
        result.record_path = str(record_path.resolve())
        previous = _read_manifest(record_path)
        if isinstance(previous.get("assets"), dict):
            record["assets"] = previous["assets"].copy()
        for entry in [previous.get("pdf"), *(previous.get("supplements") or [])]:
            if isinstance(entry, dict) and entry.get("status") in ("success", "exists"):
                record["assets"][entry.get("url", "")] = entry
        discovered = _discover(client, doi)
        result.article_url = discovered["article_url"]
        result.title, result.journal = discovered["title"], discovered["journal"]
        record.update({key: discovered[key] for key in ("article_url", "title", "journal")})
        if client.config.journal and normalize_journal(result.journal) != normalize_journal(client.config.journal):
            result.status = "filtered"
            result.error = (f"论文期刊为 {result.journal}，与所选期刊不一致。" if result.journal
                            else "网页缺少期刊元数据，无法确认所选期刊。")
            return result
        pdfs = discovered["pdf_urls"]
        supplements = discovered["supplement_urls"] if client.config.include_supplements else []
        record["discovered_supplements"] = len(discovered["supplement_urls"])
        result.supplements_total = len(supplements)
        target = directory / "article.pdf"
        old_pdf = previous.get("pdf") if isinstance(previous.get("pdf"), dict) else {}
        if not old_pdf.get("sha256"):
            old_pdf = next((entry for entry in record["assets"].values() if entry.get("file") == "article.pdf"), {})
        reuse_pdf = is_valid_pdf(target)
        if reuse_pdf:
            size, sha = _digest(target, client.event)
            if old_pdf.get("sha256") and (old_pdf["sha256"] != sha or old_pdf.get("bytes") != size):
                reuse_pdf = False
        if reuse_pdf:
            record["pdf"] = {"url": old_pdf.get("url", ""), "final_url": old_pdf.get("final_url", ""),
                             "file": "article.pdf", "status": "exists", "bytes": size, "sha256": sha}
            record["assets"][record["pdf"]["url"]] = record["pdf"]
            result.pdf_url, result.file_path = record["pdf"]["url"], str(target.resolve())
            _save_manifest(record_path, record)
        elif not pdfs:
            record["errors"].append("未发现正文 PDF 链接。")
        else:
            for url in pdfs:
                try:
                    record["pdf"] = _download_asset(client, url, target, expect_pdf=True)
                    record["assets"][url] = record["pdf"]
                    result.pdf_url, result.file_path = url, str(target.resolve())
                    _save_manifest(record_path, record)
                    break
                except DownloadCancelled:
                    raise
                except (AccessDenied, ValueError, OSError, requests.RequestException) as exc:
                    denied |= isinstance(exc, AccessDenied)
                    record["pdf"] = {"url": url, "file": "article.pdf", "status": "access_denied" if isinstance(exc, AccessDenied) else "failed",
                                     "bytes": 0, "sha256": "", "error": _error(exc)}
                    record["errors"].append("PDF: " + _error(exc))
                    _save_manifest(record_path, record)
        old_si = record["assets"]
        for url in supplements:
            _check_cancel(client.event)
            target = directory / _asset_filename(url)
            try:
                asset = _existing_asset(target, url, old_si.get(url, {}), client.event)
                if asset is None:
                    asset = _download_asset(client, url, target, expect_pdf=target.suffix == ".pdf")
                if asset["status"] == "exists":
                    result.supplements_existing += 1
                else:
                    result.supplements_downloaded += 1
                record["assets"][url] = asset
            except DownloadCancelled:
                raise
            except (AccessDenied, ValueError, OSError, requests.RequestException) as exc:
                denied |= isinstance(exc, AccessDenied)
                result.supplements_failed += 1
                asset = {"url": url, "file": target.name, "status": "access_denied" if isinstance(exc, AccessDenied) else "failed",
                         "bytes": 0, "sha256": "", "error": _error(exc)}
                record["errors"].append("补充材料: " + _error(exc))
            record["supplements"].append(asset)
            _save_manifest(record_path, record)
        pdf_ok = bool(record["pdf"] and record["pdf"]["status"] in ("success", "exists"))
        if pdf_ok:
            result.pdf_url = record["pdf"].get("url", "")
            result.file_path = str((directory / "article.pdf").resolve())
        any_ok = pdf_ok or result.supplements_downloaded + result.supplements_existing > 0
        if pdf_ok and result.supplements_failed == 0:
            result.status = "success" if record["pdf"]["status"] == "success" or result.supplements_downloaded else "exists"
        elif any_ok:
            result.status = "partial"
        elif denied:
            result.status = "access_denied"
        elif not pdfs:
            result.status = "no_pdf"
        result.error = "；".join(dict.fromkeys(record["errors"]))[:2000]
    except DownloadCancelled:
        result.status, result.error = "cancelled", "用户取消下载。"
    except AccessDenied as exc:
        result.status, result.error = "access_denied", _error(exc)
    except (ValueError, OSError, requests.RequestException) as exc:
        result.error = _error(exc)
    finally:
        record["status"] = result.status
        if result.error and result.error not in record["errors"]:
            record["errors"].append(result.error)
        if directory.is_dir():
            try:
                _save_manifest(record_path, record)
            except OSError as exc:
                result.error = (result.error + "；无法保存论文清单：" + _error(exc)).strip("；")
                result.status = "failed"
    return result


def run_batch(dois: list[str], config: DownloadConfig,
              on_result: Callable[[int, int, DownloadResult], None] | None = None,
              on_log: Callable[[str], None] | None = None,
              cancel_event: threading.Event | None = None) -> BatchSummary:
    """Callbacks run in the caller's thread; cancellation respects network timeouts."""
    validate_config(config)
    output = Path(config.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    event = cancel_event if cancel_event is not None else threading.Event()
    manifest = output / f"manifest_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid.uuid4().hex[:6]}.csv"
    summary = BatchSummary([], manifest)
    with requests.Session() as session, manifest.open("x", encoding="utf-8-sig", newline="") as handle:
        contact = f"; contact: {config.email.strip()}" if config.email.strip() else ""
        session.headers.update({"User-Agent": f"InstitutionalResearch-TextDataMining/1.0 (academic research{contact})",
                                "Accept": "text/html,application/pdf,*/*"})
        client = _Client(session, config, event)
        writer = csv.DictWriter(handle, fieldnames=list(DownloadResult.__dataclass_fields__))
        writer.writeheader()
        handle.flush()
        for index, doi in enumerate(dois, 1):
            if event.is_set():
                summary.cancelled = True
                break
            if on_log:
                on_log(f"[{index}/{len(dois)}] 正在处理 {doi}")
            result = _process(client, doi)
            writer.writerow(asdict(result))
            handle.flush()
            summary.results.append(result)
            if on_result:
                on_result(index, len(dois), result)
            if on_log:
                on_log(f"[{index}/{len(dois)}] {doi}：{_LABELS.get(result.status, result.status)}"
                       + (f"（{result.error}）" if result.error else ""))
            if result.status == "cancelled" or event.is_set():
                summary.cancelled = True
                break
    if on_log:
        on_log(f"结果清单已保存：{manifest}")
    return summary
