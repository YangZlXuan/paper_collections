"""Elsevier Article/Object Retrieval API metadata, PDF and supplementary downloads.

API credentials are sent only as headers to api.elsevier.com. This module never
uses browser cookies and never stores credentials or signed URL queries.
"""
from __future__ import annotations

import csv
import hashlib
import json
import lzma
import math
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit

import requests

from .catalog import normalize_journal
from .doi_parser import ArticleReference, extract_dois, normalize_pii
from .downloader import (
    AccessDenied, BatchSummary, DownloadCancelled, DownloadConfig, DownloadResult,
    _check_cancel, _digest, _looks_html, _read_manifest, _save_manifest,
    is_valid_pdf, paper_directory_name, parse_request_interval,
)


@dataclass
class ElsevierConfig(DownloadConfig):
    publisher_id: str = "elsevier"
    publisher_name: str = "Elsevier"
    include_supplements: bool = True
    api_key: str = field(default="", repr=False)
    insttoken: str = field(default="", repr=False)


_API_HOST = "api.elsevier.com"
# Exact publisher hosts, never wildcard public-cloud or arbitrary CDN domains.
_PDF_HOSTS = {_API_HOST, "www.sciencedirect.com", "sciencedirect.com",
              "pdf.sciencedirectassets.com", "ars.els-cdn.com",
              "reader.elsevier.com", "linkinghub.elsevier.com"}
_MAX_METADATA = 4 * 1024 * 1024
_MAX_FILE = 150 * 1024 * 1024
_MAX_ARCHIVE_EXPANDED = 500 * 1024 * 1024
_MAX_RETRY_WAIT = 60.0
_TRANSIENT = {500, 502, 503, 504}
_REDIRECTS = {301, 302, 303, 307, 308}
_LABELS = {"success": "下载成功", "exists": "文件已存在", "partial": "部分完成", "filtered": "不符合期刊筛选",
           "access_denied": "访问受限", "no_pdf": "未找到论文", "failed": "下载失败", "cancelled": "已取消"}


class _BatchStopped(AccessDenied):
    pass


class _NotFound(ValueError):
    pass


def validate_elsevier_config(config: ElsevierConfig) -> None:
    if config.publisher_id != "elsevier":
        raise ValueError("Elsevier 下载器需要选择 Elsevier 出版社。")
    if not isinstance(config.api_key, str) or not config.api_key.strip():
        raise ValueError("请填写 Elsevier API Key。")
    for value in (config.api_key, config.insttoken):
        if not isinstance(value, str) or any(ord(char) < 33 or ord(char) > 126 for char in value.strip()):
            raise ValueError("API Key 或机构令牌格式无效，请检查是否包含空格或换行。")
    parse_request_interval(config.interval)
    if not math.isfinite(config.timeout) or not 1 <= config.timeout <= 300:
        raise ValueError("网络超时须为 1–300 秒。")
    if not isinstance(config.retries, int) or not 0 <= config.retries <= 5:
        raise ValueError("重试次数须为 0–5 次。")


def _wait(event: threading.Event, seconds: float) -> None:
    if event.wait(max(0.0, seconds)):
        raise DownloadCancelled()


def _public_url(url: str) -> str:
    """Do not persist signed URL queries, fragments, or embedded credentials."""
    try:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
    except ValueError:
        return ""


def _redact(value: str, config: ElsevierConfig) -> str:
    for secret in (config.api_key.strip(), config.insttoken.strip()):
        if secret:
            value = value.replace(secret, "[已隐藏]").replace(quote(secret, safe=""), "[已隐藏]")
    return value


def _safe_error(exc: Exception, config: ElsevierConfig) -> str:
    if isinstance(exc, requests.RequestException):
        return f"网络请求失败（{type(exc).__name__}）。"
    if isinstance(exc, DownloadCancelled):
        return "用户取消下载。"
    return _redact(str(exc) or type(exc).__name__, config)[:1200]


def _endpoint(reference: ArticleReference) -> str:
    if reference.doi:
        dois = extract_dois(reference.doi)
        if len(dois) != 1 or dois[0] != reference.doi.strip().lower():
            raise ValueError("文献 DOI 格式无效，请重新解析后下载。")
        return "https://api.elsevier.com/content/article/doi/" + quote(dois[0], safe="")
    pii = normalize_pii(reference.pii)
    if not pii:
        raise ValueError("该文献缺少有效 DOI 或 PII，无法调用 Elsevier API。")
    return "https://api.elsevier.com/content/article/pii/" + quote(pii, safe="")


def _allowed(url: str, *, pdf: bool, config: ElsevierConfig) -> bool:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None
                or (parsed.hostname or "").lower() not in (_PDF_HOSTS if pdf else {_API_HOST})):
            return False
        # An API redirect must not smuggle configured credentials into a query.
        decoded = unquote(url)
        if any(secret and secret in decoded for secret in (config.api_key.strip(), config.insttoken.strip())):
            return False
        forbidden = {"apikey", "api_key", "x-els-apikey", "insttoken", "x-els-insttoken", "authorization"}
        return not any(key.casefold() in forbidden for key, _ in parse_qsl(parsed.query))
    except ValueError:
        return False


class _Client:
    def __init__(self, session: requests.Session, config: ElsevierConfig, event: threading.Event):
        validate_elsevier_config(config)
        self.session, self.config, self.event = session, config, event
        self.interval = parse_request_interval(config.interval)
        self.last_request: float | None = None
        self.quota_exhausted = False
        self.reset_time = ""
        self.stop_reason = ""

    def quota_reason(self) -> str:
        return "Elsevier API 配额已用尽，批次已停止。" + (f"配额重置时间：{self.reset_time}。" if self.reset_time else "请在配额恢复后重试。")

    def _update_quota(self, response: requests.Response) -> None:
        if (urlsplit(response.url).hostname or "").lower() != _API_HOST:
            return
        remaining = response.headers.get("X-RateLimit-Remaining", "")
        try:
            if math.isfinite(float(remaining)) and float(remaining) <= 0:
                self.quota_exhausted = True
        except (TypeError, ValueError, OverflowError):
            pass
        if response.headers.get("X-ELS-Status", "").upper().startswith("QUOTA_EXCEEDED"):
            self.quota_exhausted = True
        try:
            reset = float(response.headers.get("X-RateLimit-Reset", ""))
            if reset > 1e12:
                reset /= 1000
            self.reset_time = datetime.fromtimestamp(reset, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except (TypeError, ValueError, OverflowError, OSError):
            pass

    def _retry_delay(self, response: requests.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After", "")
        try:
            seconds = float(raw)
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(raw)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = (date - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = max(self.interval, 2 ** attempt)
        return max(0.0, seconds) if math.isfinite(seconds) else _MAX_RETRY_WAIT + 1

    def _auth_failure_kind(self, response: requests.Response) -> str:
        status = response.headers.get("X-ELS-Status", "").upper()
        if "AUTHENTICATION_ERROR" in status or "REQUESTOR CONFIGURATION" in status:
            return "global"
        # Some API versions return the error code only in the JSON body. Read
        # a bounded error document, never include its text in messages or files.
        try:
            body = bytearray()
            for chunk in response.iter_content(4096):
                _check_cancel(self.event)
                if len(body) + len(chunk) > 16 * 1024:
                    return "unknown"
                body.extend(chunk)
        except requests.RequestException:
            return "unknown"
        try:
            document = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            # Elsevier documents XML service-error payloads for 401/403 too.
            if b"<!DOCTYPE" in bytes(body).upper():
                return "unknown"
            try:
                root = ET.fromstring(body)
                document = {element.tag.rsplit("}", 1)[-1]: element.text or "" for element in root.iter()}
            except (ET.ParseError, ValueError):
                return "unknown"
        def error_fields(value: object) -> tuple[list[str], list[str]]:
            if not isinstance(value, dict):
                return [], []
            found, messages = [], []
            for key, child in value.items():
                if key.casefold() in {"error-code", "statuscode", "errorcode"} and isinstance(child, str):
                    found.append(child.upper())
                elif key.casefold() in {"statusText".casefold(), "error-message", "errormessage"} and isinstance(child, str):
                    messages.append(child.upper())
                elif isinstance(child, dict):
                    child_codes, child_messages = error_fields(child)
                    found.extend(child_codes)
                    messages.extend(child_messages)
            return found, messages
        codes, messages = error_fields(document)
        if any(code in {"AUTHENTICATION_ERROR", "INVALID_API_KEY", "INVALID_APIKEY", "APIKEY_INVALID", "KEY_MISSING", "APIKEY_MISSING"} for code in codes):
            return "global"
        if any(phrase in message for message in messages for phrase in ("NO APIKEY PROVIDED", "REQUESTOR CONFIGURATION", "INVALID APIKEY", "API KEY IS INVALID")):
            return "global"
        if any("ENTITLEMENT" in code or "SUBSCRIPTION" in code for code in codes):
            return "entitlement"
        return "unknown"

    def request(self, url: str, *, pdf: bool = False, accept: str | None = None,
                document_scoped: bool = False) -> requests.Response:
        for redirect in range(7):
            if not _allowed(url, pdf=pdf, config=self.config):
                raise AccessDenied("Elsevier API 跳转到未允许的地址，已停止此文件请求。")
            for attempt in range(self.config.retries + 1):
                _check_cancel(self.event)
                if self.quota_exhausted and (urlsplit(url).hostname or "").lower() == _API_HOST:
                    self.stop_reason = self.quota_reason()
                    raise _BatchStopped(self.stop_reason)
                if self.last_request is not None:
                    _wait(self.event, self.interval - (time.monotonic() - self.last_request))
                prepared = self.session.prepare_request(requests.Request("GET", url))
                # Remove credentials possibly inserted by session defaults/netrc.
                for header in list(prepared.headers):
                    if header.lower().startswith("x-els-") or header.lower() in {"authorization", "cookie"}:
                        del prepared.headers[header]
                if (urlsplit(url).hostname or "").lower() == _API_HOST:
                    prepared.headers["X-ELS-APIKey"] = self.config.api_key.strip()
                    if self.config.insttoken.strip():
                        prepared.headers["X-ELS-Insttoken"] = self.config.insttoken.strip()
                prepared.headers["Accept"] = accept or ("application/pdf" if pdf else "application/json")
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
                self._update_quota(response)
                code = response.status_code
                if code in (401, 403):
                    try:
                        auth_kind = self._auth_failure_kind(response)
                    finally:
                        response.close()
                    if auth_kind == "global" or (code == 401 and not pdf and not document_scoped and auth_kind == "unknown"):
                        self.stop_reason = f"Elsevier API 配置或机构访问条件不满足（HTTP {code}），批次已停止。请检查 API Key 配置、机构 IP/VPN 和接口授权。"
                        raise _BatchStopped(self.stop_reason)
                    raise AccessDenied(f"Elsevier 拒绝访问此文献（HTTP {code}）。请检查机构订阅或 PDF 格式授权；元数据可读不代表可下载正文。")
                if code == 429:
                    delay = self._retry_delay(response, attempt)
                    response.close()
                    if self.quota_exhausted:
                        self.stop_reason = self.quota_reason()
                    elif delay > _MAX_RETRY_WAIT:
                        self.stop_reason = f"Elsevier 要求等待至少 {math.ceil(delay)} 秒，批次已停止，请稍后重试。"
                    elif attempt == self.config.retries:
                        self.stop_reason = "Elsevier 请求限流仍未解除，批次已停止，请稍后重试。"
                    else:
                        _wait(self.event, delay)
                        continue
                    raise _BatchStopped(self.stop_reason)
                if code in _TRANSIENT and attempt < self.config.retries:
                    response.close()
                    _wait(self.event, min(8.0, 0.5 * 2 ** attempt))
                    continue
                if code in _REDIRECTS:
                    location = response.headers.get("Location")
                    response.close()
                    if not location or redirect == 6:
                        raise AccessDenied("Elsevier API 跳转无效或超过 6 次。")
                    url = urljoin(url, location)
                    break
                if code == 404:
                    response.close()
                    raise _NotFound("Elsevier API 未找到该 DOI 或 PII（HTTP 404）。")
                if code >= 400:
                    response.close()
                    raise ValueError(f"Elsevier API 返回 HTTP {code}，请检查接口权限或稍后重试。")
                return response
        raise AccessDenied("Elsevier API 跳转超过 6 次。")


def _scalar(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("$", "")
    return value.strip() if isinstance(value, str) else ""


def _metadata(client: _Client, reference: ArticleReference, endpoint: str) -> dict:
    for attempt in range(client.config.retries + 1):
        response = client.request(endpoint + "?view=META")
        try:
            with response:
                body = bytearray()
                for chunk in response.iter_content(65536):
                    _check_cancel(client.event)
                    if len(body) + len(chunk) > _MAX_METADATA:
                        raise ValueError("Elsevier 元数据超过 4 MB，已停止解析。")
                    body.extend(chunk)
            break
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
            if attempt == client.config.retries:
                raise
            _wait(client.event, min(8.0, 0.5 * 2 ** attempt))
    try:
        document = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Elsevier 未返回有效 JSON 元数据，可能存在访问限制。") from None
    root = document.get("full-text-retrieval-response") if isinstance(document, dict) else None
    core = root.get("coredata") if isinstance(root, dict) else None
    if not isinstance(core, dict):
        raise ValueError("Elsevier 元数据缺少 coredata，无法核验论文身份和期刊。")
    dois = extract_dois(_scalar(core.get("prism:doi"))) or extract_dois(_scalar(core.get("dc:identifier")))
    doi = dois[0] if len(dois) == 1 else ""
    pii = normalize_pii(_scalar(core.get("pii"))) or normalize_pii(_scalar(core.get("dc:identifier")))
    if not pii:
        eid = _scalar(core.get("eid"))
        if eid.startswith("1-s2.0-"):
            pii = normalize_pii(eid.removeprefix("1-s2.0-"))
    if reference.doi and not doi:
        raise ValueError("Elsevier 元数据未返回请求的 DOI，无法核验论文身份。")
    if reference.pii and not pii:
        raise ValueError("Elsevier 元数据未返回请求的 PII，无法核验论文身份。")
    if reference.doi and doi and reference.doi.casefold() != doi.casefold():
        raise ValueError("Elsevier 返回的 DOI 与输入文献不一致。")
    if reference.pii and pii and normalize_pii(reference.pii) != pii:
        raise ValueError("Elsevier 返回的 PII 与输入文献不一致。")
    if not doi and not pii:
        raise ValueError("Elsevier 元数据没有有效 DOI 或 PII，无法核验论文身份。")
    return {"doi": doi or reference.doi, "pii": pii or normalize_pii(reference.pii),
            "title": _redact(_scalar(core.get("dc:title")), client.config),
            "journal": _redact(_scalar(core.get("prism:publicationName")), client.config)}


def _object_endpoint(reference: ArticleReference) -> str:
    if reference.pii:
        pii = normalize_pii(reference.pii)
        if not pii:
            raise ValueError("补充材料查询缺少有效 PII。")
        return "https://api.elsevier.com/content/object/pii/" + quote(pii, safe="")
    return _endpoint(reference).replace("/content/article/", "/content/object/", 1)


def _inventory_body(client: _Client, url: str) -> bytes:
    for attempt in range(client.config.retries + 1):
        response = client.request(url, document_scoped=True)
        try:
            with response:
                body = bytearray()
                for chunk in response.iter_content(65536):
                    _check_cancel(client.event)
                    if len(body) + len(chunk) > _MAX_METADATA:
                        raise ValueError("Elsevier 附件目录超过 4 MB，已停止解析。")
                    body.extend(chunk)
            return bytes(body)
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
            if attempt == client.config.retries:
                raise
            _wait(client.event, min(8.0, 0.5 * 2 ** attempt))
    raise ValueError("附件目录读取失败。")


def _parse_inventory(body: bytes) -> tuple[dict, list[dict]]:
    try:
        document = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        if b"<!DOCTYPE" in body.upper():
            raise ValueError("附件目录包含不支持的 XML 文档类型声明。") from None
        try:
            root = ET.fromstring(body)
        except (ET.ParseError, ValueError):
            raise ValueError("Elsevier 未返回有效附件目录。") from None
        local = lambda tag: tag.rsplit("}", 1)[-1]
        if local(root.tag) != "attachment-metadata-response":
            raise ValueError("Elsevier 返回的 XML 不是附件目录。")
        core, attachments = {}, []
        for child in root:
            values = {local(item.tag): (item.text or "").strip() for item in child}
            if local(child.tag) == "coredata":
                core = {"prism:doi": values.get("doi", ""), "dc:identifier": values.get("identifier", ""),
                        "pii": values.get("pii", "")}
            elif local(child.tag) == "attachment":
                values["prism:url"] = values.get("url", "")
                attachments.append(values)
        return core, attachments
    root = document.get("attachment-metadata-response") if isinstance(document, dict) else None
    if not isinstance(root, dict) or not isinstance(root.get("coredata"), dict):
        raise ValueError("Elsevier 附件目录缺少可核验的 coredata。")
    attachments = root.get("attachment")
    if attachments is None:
        attachments = []
    elif isinstance(attachments, dict):
        attachments = [attachments]
    if not isinstance(attachments, list) or any(not isinstance(item, dict) for item in attachments):
        raise ValueError("Elsevier 附件目录的 attachment 字段格式无效。")
    return root["coredata"], attachments


def _inventory_identity(core: dict, reference: ArticleReference) -> None:
    dois = extract_dois(_scalar(core.get("prism:doi"))) or extract_dois(_scalar(core.get("dc:identifier")))
    doi = dois[0] if len(dois) == 1 else ""
    pii = normalize_pii(_scalar(core.get("pii"))) or normalize_pii(_scalar(core.get("dc:identifier")))
    if reference.pii:
        if not pii or pii != normalize_pii(reference.pii):
            raise ValueError("Elsevier 附件目录 PII 与论文不一致或缺失。")
    elif reference.doi and (not doi or doi.casefold() != reference.doi.casefold()):
        raise ValueError("Elsevier 附件目录 DOI 与论文不一致或缺失。")
    if reference.doi and doi and doi.casefold() != reference.doi.casefold():
        raise ValueError("Elsevier 附件目录 DOI 与论文不一致。")


def _supplement_suffix(filename: str, mime: str, url: str) -> str:
    valid = {".pdf", ".zip", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".csv", ".tsv",
             ".txt", ".xml", ".json", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp", ".svg",
             ".mp4", ".mov", ".avi", ".mpg", ".mpeg", ".mp3", ".wav", ".ogg", ".webm",
             ".gz", ".tgz", ".tar", ".bz2", ".rar", ".7z", ".mat", ".cif", ".pdb", ".sdf", ".mol"}
    for name in (filename, urlsplit(url).path):
        suffix = Path(name).suffix.lower()
        if suffix in valid:
            return suffix
    return {"application/pdf": ".pdf", "application/zip": ".zip", "text/plain": ".txt", "text/csv": ".csv",
            "application/json": ".json", "application/xml": ".xml", "text/xml": ".xml", "image/jpeg": ".jpg",
            "image/png": ".png", "image/gif": ".gif", "video/mp4": ".mp4"}.get(mime.lower(), ".bin")


def _verified_object_url(url: str, ref: str, reference: ArticleReference, config: ElsevierConfig) -> str:
    fallback = _object_endpoint(reference) + "/ref/" + quote(ref, safe="") if ref else ""
    if not url:
        if fallback:
            return fallback
        raise ValueError("已标记的补充材料缺少下载地址和对象引用。")
    try:
        parsed = urlsplit(url)
        if parsed.scheme == "http" and parsed.hostname == _API_HOST:
            url = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
            parsed = urlsplit(url)
        if not _allowed(url, pdf=True, config=config):
            raise ValueError("附件目录中的下载地址不在允许的出版社域名内。")
        path = unquote(parsed.path)
        if parsed.hostname == _API_HOST:
            if path.startswith("/content/object/pii/") and "/ref/" in path:
                pii, actual_ref = path.removeprefix("/content/object/pii/").split("/ref/", 1)
                valid = bool(reference.pii) and normalize_pii(pii) == normalize_pii(reference.pii) and actual_ref == ref
            elif path.startswith("/content/object/doi/") and "/ref/" in path:
                doi, actual_ref = path.removeprefix("/content/object/doi/").rsplit("/ref/", 1)
                valid = bool(reference.doi) and doi.casefold() == reference.doi.casefold() and actual_ref == ref
            elif path.startswith("/content/object/eid/"):
                prefix = "1-s2.0-" + normalize_pii(reference.pii) + "-"
                eid = path.removeprefix("/content/object/eid/")
                suffix = eid[len(prefix):] if reference.pii and eid.startswith(prefix) else ""
                valid = bool(ref and re.match(re.escape(ref) + r"(?:[_.-]|$)", suffix, re.I))
            else:
                valid = False
            if not valid:
                raise ValueError("附件目录中的对象地址与论文标识或对象引用不一致。")
            return url
        # Direct CDN advertisements must identify this article and observed ref.
        # Opaque CDN paths can instead use the documented same-article ref API.
        marker = re.search(r"1-s2\.0-([SB][0-9X]+)-([^/]+)", path, re.I)
        if marker:
            if (not reference.pii or normalize_pii(marker.group(1)) != normalize_pii(reference.pii)
                    or not ref or not re.match(re.escape(ref) + r"(?:[_.-]|$)", marker.group(2), re.I)):
                raise ValueError("附件目录中的 CDN 地址与论文或对象引用不一致。")
            return url
        if fallback:
            return fallback
        raise ValueError("无法核验附件 CDN 地址所属的论文。")
    except (ValueError, AttributeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(("附件", "无法")):
            raise
        raise ValueError("附件目录中的下载地址无效。") from None


def _discover_supplements(client: _Client, reference: ArticleReference) -> tuple[list[dict], dict]:
    endpoint = _object_endpoint(reference)
    core, objects = _parse_inventory(_inventory_body(client, endpoint + "?view=META"))
    _inventory_identity(core, reference)
    inventory_pii = normalize_pii(_scalar(core.get("pii"))) or normalize_pii(_scalar(core.get("dc:identifier")))
    verified_reference = ArticleReference(doi=reference.doi, pii=reference.pii or inventory_pii)
    selected: dict[str, dict] = {}
    ranks: dict[str, tuple[int, int]] = {}
    for item in objects:
        ref = _scalar(item.get("ref"))
        kind = _scalar(item.get("type")).upper()
        filename = _scalar(item.get("filename"))
        # mmc references are selected only when actually advertised by the
        # publisher inventory. si0001/ALTIMG are equations, not supplements.
        if "THUMBNAIL" in kind or kind in {"ALTIMG", "AAM-PDF", "MAIN-TEXT", "BODY-XML"}:
            continue
        marked = bool(re.fullmatch(r"mmc\d+[a-z]?", ref, re.I)) or kind.startswith("IMAGE-MMC") or "SUPPLEMENT" in kind
        if not marked:
            continue
        url = _verified_object_url(_scalar(item.get("prism:url")), ref, verified_reference, client.config)
        public_url = _public_url(url)
        mime = unquote(_scalar(item.get("mimetype"))).split(";", 1)[0].strip().lower()
        identity = public_url or (endpoint + "/ref/" + quote(ref, safe=""))
        asset_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        safe_ref = re.sub(r"[^a-zA-Z0-9_-]", "_", ref)[:35] or "attachment"
        suffix = _supplement_suffix(filename, mime, url or endpoint)
        expected = str(item["size"]) if isinstance(item.get("size"), int) and not isinstance(item["size"], bool) else _scalar(item.get("size"))
        expected_size = int(expected) if expected.isdigit() else None
        descriptor = {"asset_key": asset_key, "object_ref": _redact(ref, client.config),
                      "source_filename": _redact(filename, client.config), "object_type": kind,
                      "mimetype": mime, "expected_bytes": expected_size, "url": public_url,
                      "file": f"SI_{safe_ref}_{asset_key[:12]}{suffix}", "request_url": url}
        group = ref.casefold() or asset_key
        rank = (1 if "DOWNSAMPLED" in kind else 0, -(expected_size or 0))
        if group not in selected or rank < ranks[group]:
            selected[group], ranks[group] = descriptor, rank
    discovery = {"source": "Elsevier Object Retrieval API", "url": _public_url(endpoint), "view": "META",
                 "status": "complete", "objects_total": len(objects), "selected_total": len(selected),
                 "ignored_total": len(objects) - len(selected)}
    return list(selected.values()), discovery


def _validate_archive(path: Path, suffix: str, event: threading.Event) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 10000 or sum(item.file_size for item in infos) > _MAX_ARCHIVE_EXPANDED:
                raise ValueError("附件 ZIP/Office 解压校验量超过 500 MB 或条目过多。")
            required = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}.get(suffix)
            if required and (required not in archive.namelist() or "[Content_Types].xml" not in archive.namelist()):
                raise ValueError("附件不是有效的 Office 文件包。")
            for info in infos:
                _check_cancel(event)
                if info.is_dir():
                    continue
                if info.flag_bits & 1:
                    raise ValueError("附件 ZIP 已加密，无法完成完整性校验。")
                with archive.open(info) as handle:
                    while handle.read(128 * 1024):
                        _check_cancel(event)
    except (zipfile.BadZipFile, EOFError, RuntimeError, zlib.error, lzma.LZMAError):
        raise ValueError("附件 ZIP/Office 文件损坏或 CRC 校验失败。") from None


def _validate_supplement(path: Path, suffix: str, prefix: bytes, event: threading.Event) -> None:
    if _looks_html(prefix):
        raise AccessDenied("补充材料返回 HTML 登录或错误页面。")
    if re.search(br"<(?:[A-Za-z0-9_-]+:)?(?:service-error|error-response|attachment-metadata-response|full-text-retrieval-response)\b", prefix, re.I):
        raise AccessDenied("补充材料地址返回 Elsevier API 错误或元数据，未保存为附件。")
    if prefix.lstrip().startswith((b"{", b"[")) and path.stat().st_size <= _MAX_METADATA:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError, RecursionError):
            if suffix == ".json":
                raise ValueError("JSON 补充材料内容无效。") from None
        else:
            if isinstance(payload, dict) and any(key in payload for key in ("service-error", "error-response", "attachment-metadata-response", "full-text-retrieval-response")):
                raise AccessDenied("补充材料地址返回 Elsevier API 错误或元数据，未保存为附件。")
    if suffix == ".pdf" or prefix.startswith(b"%PDF-"):
        if not is_valid_pdf(path):
            raise ValueError("补充材料 PDF 不完整。")
    elif suffix in {".zip", ".docx", ".xlsx", ".pptx"}:
        _validate_archive(path, suffix, event)
    elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp"}:
        signatures = {".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
                      ".gif": (b"GIF87a", b"GIF89a"), ".tif": (b"II*\x00", b"MM\x00*"),
                      ".tiff": (b"II*\x00", b"MM\x00*"), ".bmp": (b"BM",)}
        if not prefix.startswith(signatures[suffix]):
            raise ValueError("图片补充材料的文件头与扩展名不一致。")


def _download_supplement(client: _Client, descriptor: dict, target: Path) -> dict:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.part")
    expected_size = descriptor.get("expected_bytes")
    if expected_size is not None and expected_size > _MAX_FILE:
        raise ValueError("补充材料超过 150 MB。")
    try:
        for attempt in range(client.config.retries + 1):
            response = client.request(descriptor["request_url"], pdf=True, accept="*/*", document_scoped=True)
            try:
                with response, temporary.open("wb") as handle:
                    if "html" in response.headers.get("Content-Type", "").lower():
                        raise AccessDenied("补充材料地址返回 HTML 登录或错误页面。")
                    length = response.headers.get("Content-Length", "")
                    expected_http = int(length) if length.isdigit() else None
                    if expected_http is not None and expected_http > _MAX_FILE:
                        raise ValueError("补充材料超过 150 MB。")
                    size, digest, prefix = 0, hashlib.sha256(), b""
                    for chunk in response.iter_content(128 * 1024):
                        _check_cancel(client.event)
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > _MAX_FILE:
                            raise ValueError("补充材料超过 150 MB。")
                        prefix = (prefix + chunk)[:4096]
                        if _looks_html(prefix):
                            raise AccessDenied("补充材料内容是 HTML 登录或错误页面。")
                        digest.update(chunk)
                        handle.write(chunk)
                    if expected_http is not None and not response.headers.get("Content-Encoding") and size != expected_http:
                        raise ValueError("补充材料传输不完整，与 Content-Length 不一致。")
                    final_url = _public_url(response.url or descriptor["request_url"])
                if size == 0:
                    raise ValueError("补充材料文件为空。")
                if expected_size is not None and size != expected_size:
                    raise ValueError("补充材料大小与官方附件目录不一致。")
                _validate_supplement(temporary, target.suffix.lower(), prefix, client.event)
                _check_cancel(client.event)
                temporary.replace(target)
                return {key: value for key, value in descriptor.items() if key != "request_url"} | {
                    "final_url": final_url, "status": "success", "bytes": size, "sha256": digest.hexdigest()}
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
                if attempt == client.config.retries:
                    raise
                _wait(client.event, min(8.0, 0.5 * 2 ** attempt))
    finally:
        temporary.unlink(missing_ok=True)
    raise ValueError("补充材料下载失败。")


def _reuse_supplement(client: _Client, descriptor: dict, target: Path, previous: dict) -> dict | None:
    if (previous.get("asset_key") != descriptor["asset_key"] or previous.get("url") != descriptor["url"]
            or previous.get("file") != target.name or previous.get("status") not in {"success", "exists"}
            or not previous.get("sha256") or not target.is_file()):
        return None
    if not 0 < target.stat().st_size <= _MAX_FILE:
        return None
    size, digest = _digest(target, client.event)
    if not size or size != previous.get("bytes") or digest != previous["sha256"]:
        return None
    if descriptor.get("expected_bytes") is not None and size != descriptor["expected_bytes"]:
        return None
    return {key: value for key, value in descriptor.items() if key != "request_url"} | {
        "final_url": _public_url(previous.get("final_url", "")), "status": "exists", "bytes": size, "sha256": digest}


def _download_pdf(client: _Client, endpoint: str, target: Path) -> dict:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        for attempt in range(client.config.retries + 1):
            response = client.request(endpoint, pdf=True)
            try:
                with response, temporary.open("wb") as handle:
                    content_type = response.headers.get("Content-Type", "").lower()
                    if any(kind in content_type for kind in ("html", "json", "xml")):
                        raise AccessDenied("Elsevier 未返回正文 PDF。元数据可读不代表具有 PDF 格式或机构订阅权限。")
                    length = response.headers.get("Content-Length", "")
                    expected = int(length) if length.isdigit() else None
                    if expected is not None and expected > _MAX_FILE:
                        raise ValueError("PDF 超过 150 MB。")
                    size, digest, prefix = 0, hashlib.sha256(), b""
                    for chunk in response.iter_content(128 * 1024):
                        _check_cancel(client.event)
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > _MAX_FILE:
                            raise ValueError("PDF 超过 150 MB。")
                        prefix = (prefix + chunk)[:4096]
                        if _looks_html(prefix):
                            raise AccessDenied("Elsevier 文件地址返回 HTML 登录或错误页面，未保存为 PDF。")
                        digest.update(chunk)
                        handle.write(chunk)
                    if expected is not None and not response.headers.get("Content-Encoding") and size != expected:
                        raise ValueError("PDF 传输不完整：实际大小与 Content-Length 不一致。")
                    final_url = _public_url(response.url or endpoint)
                _check_cancel(client.event)
                if not size or not is_valid_pdf(temporary):
                    raise ValueError("返回内容不是完整 PDF（文件为空或缺少 PDF 标记）。")
                temporary.replace(target)
                return {"url": _public_url(endpoint), "final_url": final_url, "file": target.name,
                        "status": "success", "bytes": size, "sha256": digest.hexdigest()}
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
                if attempt == client.config.retries:
                    raise
                _wait(client.event, min(8.0, 0.5 * 2 ** attempt))
    finally:
        temporary.unlink(missing_ok=True)
    raise ValueError("Elsevier PDF 下载失败。")


def _process(client: _Client, reference: ArticleReference) -> DownloadResult:
    result = DownloadResult(doi=reference.doi, pii=reference.pii, reference_key=reference.key, status="failed",
                            supplements_status="" if client.config.include_supplements else "disabled")
    directory = Path(client.config.output_dir) / ("elsevier_" + paper_directory_name(reference.key))
    path = directory / "manifest.json"
    record = {"schema_version": 2, "publisher": "Elsevier", "reference_key": reference.key,
              "doi": reference.doi, "pii": reference.pii, "title": "", "journal": "", "pdf": None,
              "status": "in_progress", "include_supplements": client.config.include_supplements,
              "supplements": [], "assets": {}, "supplement_discovery": {
                  "status": "not_attempted" if client.config.include_supplements else "disabled"}, "errors": []}
    main_status = "failed"
    discovery_started = False
    try:
        _check_cancel(client.event)
        endpoint = _endpoint(reference)
        directory.mkdir(parents=True, exist_ok=True)
        result.record_path = str(path.resolve())
        previous = _read_manifest(path)
        record["pdf"] = previous.get("pdf")
        record["assets"] = dict(previous.get("assets") or {})
        for asset in previous.get("supplements", []):
            if asset.get("status") in {"success", "exists"}:
                record["assets"][asset["url"]] = asset
        data = _metadata(client, reference, endpoint)
        record.update(data)
        result.doi, result.pii = data["doi"], data["pii"]
        result.title, result.journal = data["title"], data["journal"]
        result.article_url = ("https://www.sciencedirect.com/science/article/pii/" + result.pii if result.pii
                              else "https://doi.org/" + quote(result.doi, safe="/"))
        record["article_url"] = result.article_url
        if client.config.journal and normalize_journal(result.journal) != normalize_journal(client.config.journal):
            result.status = "filtered"
            result.error = "Elsevier 元数据期刊与所选期刊不一致。" if result.journal else "Elsevier 元数据缺少期刊名称，无法确认所选期刊。"
            return result
        target = directory / "article.pdf"
        previous_pdf = previous.get("pdf") or {}
        reuse = is_valid_pdf(target)
        if reuse:
            size, digest = _digest(target, client.event)
            if previous_pdf.get("sha256") and (previous_pdf["sha256"] != digest or previous_pdf.get("bytes") != size):
                reuse = False
        try:
            if reuse:
                pdf = {"url": _public_url(previous_pdf.get("url", endpoint)),
                       "final_url": _public_url(previous_pdf.get("final_url", "")), "file": "article.pdf",
                       "status": "exists", "bytes": size, "sha256": digest}
            else:
                pdf = _download_pdf(client, endpoint, target)
            record["pdf"] = pdf
            main_status = pdf["status"]
            result.file_path, result.pdf_url = str(target.resolve()), pdf["url"]
            _save_manifest(path, record)
        except (DownloadCancelled, _BatchStopped):
            raise
        except (AccessDenied, _NotFound, ValueError, OSError, requests.RequestException) as exc:
            main_status = "access_denied" if isinstance(exc, AccessDenied) else "no_pdf" if isinstance(exc, _NotFound) else "failed"
            message = "正文 PDF: " + _safe_error(exc, client.config)
            record["errors"].append(message)
            record["pdf"] = {"url": _public_url(endpoint), "file": "article.pdf", "status": main_status,
                             "bytes": 0, "sha256": "", "error": message}
            _save_manifest(path, record)
        if client.config.include_supplements:
            discovery_started = True
            identity = ArticleReference(doi=result.doi, pii=result.pii)
            try:
                descriptors, discovery = _discover_supplements(client, identity)
                record["supplement_discovery"] = discovery
                result.supplements_total = len(descriptors)
                result.supplements_status = "none" if not descriptors else "partial"
                record["supplements"] = [{key: value for key, value in item.items() if key != "request_url"} | {
                    "status": "pending", "bytes": 0, "sha256": ""} for item in descriptors]
                _save_manifest(path, record)
            except (DownloadCancelled, _BatchStopped):
                raise
            except (AccessDenied, ValueError, OSError, requests.RequestException) as exc:
                descriptors = []
                result.supplements_status = "discovery_failed"
                message = "补充材料发现失败: " + _safe_error(exc, client.config)
                record["supplement_discovery"] = {"source": "Elsevier Object Retrieval API",
                                                   "status": "failed", "error": message}
                record["errors"].append(message)
            old_assets = {asset.get("asset_key"): asset for asset in record["assets"].values() if asset.get("asset_key")}
            for index, descriptor in enumerate(descriptors):
                _check_cancel(client.event)
                attachment_target = directory / descriptor["file"]
                try:
                    asset = _reuse_supplement(client, descriptor, attachment_target, old_assets.get(descriptor["asset_key"], {}))
                    if asset is None:
                        asset = _download_supplement(client, descriptor, attachment_target)
                    if asset["status"] == "exists":
                        result.supplements_existing += 1
                    else:
                        result.supplements_downloaded += 1
                    record["supplements"][index] = asset
                    record["assets"][asset["url"]] = asset
                    _save_manifest(path, record)
                except DownloadCancelled:
                    record["supplements"][index]["status"] = "cancelled"
                    record["supplements"][index]["error"] = "用户取消下载。"
                    raise
                except (AccessDenied, ValueError, OSError, requests.RequestException) as exc:
                    result.supplements_failed += 1
                    message = f"补充材料 {descriptor['object_ref']}: " + _safe_error(exc, client.config)
                    record["supplements"][index].update({"status": "access_denied" if isinstance(exc, AccessDenied) else "failed", "error": message})
                    record["errors"].append(message)
                    _save_manifest(path, record)
                    if isinstance(exc, _BatchStopped):
                        raise
            if descriptors:
                result.supplements_status = "partial" if result.supplements_failed else "complete"
        main_ok = main_status in {"success", "exists"}
        si_ok = result.supplements_downloaded + result.supplements_existing
        if main_ok and result.supplements_status in {"disabled", "none", "complete"}:
            result.status = "success" if main_status == "success" or result.supplements_downloaded else "exists"
        elif main_ok or si_ok:
            result.status = "partial"
        else:
            result.status = main_status
        result.error = "；".join(dict.fromkeys(record["errors"]))[:3000]
    except DownloadCancelled:
        result.status, result.error = "cancelled", "用户取消下载。"
        if client.config.include_supplements:
            result.supplements_status = "cancelled"
    except AccessDenied as exc:
        result.status = "partial" if result.file_path or result.supplements_downloaded + result.supplements_existing else "access_denied"
        result.error = _safe_error(exc, client.config)
        if discovery_started and record["supplement_discovery"].get("status") == "not_attempted":
            result.supplements_status = "discovery_failed"
            record["supplement_discovery"].update({"status": "failed", "error": result.error})
    except _NotFound as exc:
        result.status, result.error = "no_pdf", _safe_error(exc, client.config)
    except (ValueError, OSError, requests.RequestException) as exc:
        result.error = _safe_error(exc, client.config)
    finally:
        record["status"] = result.status
        record["supplements_status"] = result.supplements_status
        if result.error:
            record["errors"].append(result.error)
        if directory.is_dir():
            try:
                _save_manifest(path, record)
            except OSError:
                result.status = "failed"
                result.error = (result.error + "；无法保存论文清单，请检查目录写入权限。").strip("；")
    return result


def run_elsevier_batch(records: list[ArticleReference], config: ElsevierConfig,
                       on_result: Callable[[int, int, DownloadResult], None] | None = None,
                       on_log: Callable[[str], None] | None = None,
                       cancel_event: threading.Event | None = None) -> BatchSummary:
    validate_elsevier_config(config)
    output = Path(config.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    event = cancel_event if cancel_event is not None else threading.Event()
    manifest = output / f"manifest_elsevier_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid.uuid4().hex[:6]}.csv"
    summary = BatchSummary([], manifest)
    with requests.Session() as session, manifest.open("x", encoding="utf-8-sig", newline="") as handle:
        session.headers.update({"User-Agent": "PaperCollections/1.0 (Elsevier Article Retrieval API)"})
        client = _Client(session, config, event)
        writer = csv.DictWriter(handle, fieldnames=list(DownloadResult.__dataclass_fields__))
        writer.writeheader()
        handle.flush()
        for index, reference in enumerate(records, 1):
            if event.is_set():
                summary.cancelled = True
                break
            if client.quota_exhausted:
                summary.stop_reason = client.quota_reason()
                break
            if on_log:
                on_log(f"[{index}/{len(records)}] Elsevier 正在处理 {_redact(reference.key, config)}")
            result = _process(client, reference)
            writer.writerow(asdict(result))
            handle.flush()
            summary.results.append(result)
            if on_result:
                on_result(index, len(records), result)
            if on_log:
                on_log(f"[{index}/{len(records)}] {_redact(reference.key, config)}：{_LABELS.get(result.status, result.status)}"
                       + (f"（{result.error}）" if result.error else ""))
            if result.status == "cancelled" or event.is_set():
                summary.cancelled = True
                break
            if client.stop_reason:
                summary.stop_reason = client.stop_reason
                break
    if on_log:
        if summary.stop_reason:
            on_log(summary.stop_reason)
        on_log(f"Elsevier 结果清单已保存：{manifest}")
    return summary
