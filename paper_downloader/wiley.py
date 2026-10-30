"""Wiley TDM full-text PDF downloads, with public Crossref identity checks.

Wiley TDM provides the full-text PDF. Supporting information is discovered only
from explicitly associated Crossref records, then fetched without API tokens.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from numbers import Number
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit

import requests

from .catalog import normalize_journal
from .doi_parser import ArticleReference, extract_dois
from .downloader import (
    AccessDenied, BatchSummary, DownloadCancelled, DownloadConfig, DownloadResult,
    _check_cancel, _digest, _looks_html, _read_manifest, _save_manifest,
    is_valid_pdf, paper_directory_name,
)


MIN_WILEY_INTERVAL = 10.0
MAX_WILEY_INTERVAL = 60.0
_API_HOST = "api.wiley.com"
_METADATA_HOST = "api.crossref.org"
# Additional binary hosts are added only after a verified official API redirect.
_PDF_HOSTS = {_API_HOST, "onlinelibrary.wiley.com", "alm.wiley.com"}
_SUPPLEMENT_HOSTS = _PDF_HOSTS | {"advanced.onlinelibrary.wiley.com"}
_MAX_METADATA = 4 * 1024 * 1024
_MAX_FILE = 150 * 1024 * 1024
_MAX_RETRY_WAIT = 60.0
_REDIRECTS = {301, 302, 303, 307, 308}
_TRANSIENT = {500, 502, 503, 504}
_STREAM_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)
_LABELS = {"success": "下载成功", "exists": "文件已存在", "partial": "部分完成", "filtered": "不符合出版社或期刊筛选",
           "access_denied": "访问受限", "failed": "下载失败", "cancelled": "已取消"}


@dataclass
class WileyConfig(DownloadConfig):
    publisher_id: str = "wiley"
    publisher_name: str = "Wiley"
    interval: float = MIN_WILEY_INTERVAL
    include_supplements: bool = True
    api_key: str = field(default="", repr=False)


class _BatchStopped(AccessDenied):
    pass


def parse_wiley_interval(value: object) -> float:
    message = "Wiley 请求间隔须为 10–60 秒的有效数字，不能小于 10 秒。"
    if isinstance(value, bool) or not isinstance(value, (str, Number)):
        raise ValueError(message)
    try:
        interval = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(message) from None
    if not math.isfinite(interval) or not MIN_WILEY_INTERVAL <= interval <= MAX_WILEY_INTERVAL:
        raise ValueError(message)
    return interval


def validate_wiley_config(config: WileyConfig) -> None:
    if config.publisher_id != "wiley":
        raise ValueError("Wiley 下载器需要选择 Wiley 出版社。")
    if not isinstance(config.api_key, str) or not config.api_key.strip():
        raise ValueError("请设置 WileyKey 环境变量或填写 Wiley TDM Token。")
    if any(ord(char) < 33 or ord(char) > 126 for char in config.api_key.strip()):
        raise ValueError("Wiley TDM Token 格式无效，请检查空格或换行。")
    parse_wiley_interval(config.interval)
    if isinstance(config.timeout, bool) or not isinstance(config.timeout, Number) or not math.isfinite(config.timeout) or not 1 <= config.timeout <= 300:
        raise ValueError("网络超时须为 1–300 秒。")
    if isinstance(config.retries, bool) or not isinstance(config.retries, int) or not 0 <= config.retries <= 5:
        raise ValueError("重试次数须为 0–5 次。")


def _wait(event: threading.Event, seconds: float) -> None:
    if event.wait(max(0.0, seconds)):
        raise DownloadCancelled()


def _public_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        # Wiley ALM includes opaque, temporary access tokens in the path itself.
        # Persist the source article endpoint, but only the origin of file hosts.
        path = parsed.path if parsed.hostname in {_API_HOST, _METADATA_HOST} else "/"
        return urlunsplit((parsed.scheme, parsed.hostname or "", path, "", ""))
    except ValueError:
        return ""


def _redact(text: str, config: WileyConfig) -> str:
    token = config.api_key.strip()
    if token:
        text = text.replace(token, "[已隐藏]").replace(quote(token, safe=""), "[已隐藏]")
    return text


def _safe_error(exc: Exception, config: WileyConfig) -> str:
    if isinstance(exc, requests.RequestException):
        return f"网络请求失败（{type(exc).__name__}）。"
    if isinstance(exc, DownloadCancelled):
        return "用户取消下载。"
    return _redact(str(exc) or type(exc).__name__, config)[:1200]


def _doi(reference: ArticleReference) -> str:
    if not reference.doi:
        raise ValueError("Wiley 下载需要 DOI；仅有 ScienceDirect PII 的记录不能使用。")
    values = extract_dois(reference.doi)
    if len(values) != 1 or values[0] != reference.doi.strip().lower():
        raise ValueError("文献 DOI 格式无效，请重新解析后下载。")
    return values[0]


def _endpoint(reference: ArticleReference) -> str:
    return "https://api.wiley.com/onlinelibrary/tdm/v1/articles/" + quote(_doi(reference), safe="")


def _metadata_endpoint(doi: str) -> str:
    return "https://api.crossref.org/works/" + quote(doi, safe="")


def _allowed(url: str, *, pdf: bool, config: WileyConfig, supplement: bool = False) -> bool:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None
                or (parsed.hostname or "").lower() not in (_SUPPLEMENT_HOSTS if supplement else _PDF_HOSTS if pdf else {_METADATA_HOST})):
            return False
        if config.api_key.strip() in unquote(url):
            return False
        forbidden = {"wiley-tdm-client-token", "apikey", "api_key", "authorization"}
        return not any(key.casefold() in forbidden for key, _ in parse_qsl(parsed.query))
    except ValueError:
        return False


class _Client:
    def __init__(self, session: requests.Session, config: WileyConfig, event: threading.Event):
        validate_wiley_config(config)
        self.session, self.config, self.event = session, config, event
        self.interval = parse_wiley_interval(config.interval)
        self.last_request: float | None = None
        self.stop_reason = ""

    def _retry_delay(self, response: requests.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After", "")
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(raw)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = (date - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = max(self.interval, 2 ** attempt)
        return max(0.0, seconds) if math.isfinite(seconds) else _MAX_RETRY_WAIT + 1

    def _invalid_token(self, response: requests.Response) -> bool:
        """Differentiate explicit invalid credentials from per-article denial."""
        body = bytearray()
        try:
            for chunk in response.iter_content(4096):
                _check_cancel(self.event)
                if len(body) + len(chunk) > 16 * 1024:
                    break
                body.extend(chunk)
        except requests.RequestException:
            return False
        value = bytes(body).decode("utf-8", errors="replace").casefold()
        return any(phrase in value for phrase in (
            "no tdm client token", "invalid tdm client token", "invalid client token",
            "token that you have provided is invalid", "token is invalid", "token is not registered",
            "token has expired", "invalid_token", "invalid token",
        ))

    def request(self, url: str, *, pdf: bool = False, supplement: bool = False, accept: str | None = None) -> requests.Response:
        for redirect in range(7):
            if not _allowed(url, pdf=pdf, config=self.config, supplement=supplement):
                try:
                    host = urlsplit(url).hostname or "无效主机"
                except ValueError:
                    host = "无效主机"
                raise AccessDenied(f"Wiley 请求跳转到未允许的地址（主机：{_redact(host, self.config)}），已停止此文件请求。")
            for attempt in range(self.config.retries + 1):
                _check_cancel(self.event)
                if self.last_request is not None:
                    _wait(self.event, self.interval - (time.monotonic() - self.last_request))
                prepared = self.session.prepare_request(requests.Request("GET", url))
                # Strip session/netrc credentials before adding this API's header.
                for name in list(prepared.headers):
                    if (name.lower().startswith(("wiley-", "x-els-"))
                            or name.lower() in {"authorization", "proxy-authorization", "cookie", "x-api-key"}):
                        del prepared.headers[name]
                host = (urlsplit(url).hostname or "").lower()
                if host == _API_HOST and not supplement:
                    prepared.headers["Wiley-TDM-Client-Token"] = self.config.api_key.strip()
                prepared.headers["Accept"] = accept or ("application/pdf" if pdf else "application/json")
                settings = self.session.merge_environment_settings(prepared.url, {}, True, None, None)
                self.last_request = time.monotonic()
                try:
                    response = self.session.send(prepared, allow_redirects=False,
                                                 timeout=(min(15.0, self.config.timeout), self.config.timeout), **settings)
                except (requests.ConnectionError, requests.Timeout):
                    if attempt == self.config.retries:
                        raise
                    continue
                try:
                    _check_cancel(self.event)
                except DownloadCancelled:
                    response.close()
                    raise
                status = response.status_code
                service = "Wiley" if pdf or supplement else "Crossref"
                if status in (401, 403) or (host == _API_HOST and status == 400):
                    try:
                        invalid_token = host == _API_HOST and not supplement and (status in (400, 401) or self._invalid_token(response))
                    finally:
                        response.close()
                    if invalid_token:
                        self.stop_reason = f"Wiley TDM 认证或访问条件不满足（HTTP {status}），批次已停止。请检查 WileyKey、TDM 授权及机构网络。"
                        raise _BatchStopped(self.stop_reason)
                    if not pdf and not supplement:
                        self.stop_reason = f"Crossref 元数据访问受限（HTTP {status}），无法核验论文，批次已停止。"
                        raise _BatchStopped(self.stop_reason)
                    raise AccessDenied(f"Wiley 拒绝访问此论文（HTTP {status}），请检查机构网络、内容订阅及 TDM 权限；不单凭此状态判定 Token 无效。")
                if status == 429:
                    delay = self._retry_delay(response, attempt)
                    response.close()
                    if delay > _MAX_RETRY_WAIT:
                        self.stop_reason = f"{service} 要求等待至少 {math.ceil(delay)} 秒，批次已停止，请稍后重试。"
                    elif attempt == self.config.retries:
                        self.stop_reason = f"{service} 限流仍未解除，批次已停止，请稍后重试。"
                    else:
                        _wait(self.event, delay)
                        continue
                    raise _BatchStopped(self.stop_reason)
                if status in _TRANSIENT and attempt < self.config.retries:
                    response.close()
                    continue
                if status in _REDIRECTS:
                    location = response.headers.get("Location")
                    response.close()
                    if not location or redirect == 6:
                        raise AccessDenied("Wiley 请求跳转无效或超过 6 次。")
                    url = urljoin(url, location)
                    break
                if status == 404:
                    response.close()
                    if pdf or supplement:
                        raise AccessDenied("Wiley 返回 HTTP 404：论文不可用或当前机构无内容访问权限；该状态不能单独判断论文不存在。")
                    raise ValueError("Crossref 未找到该 DOI 的元数据（HTTP 404），无法核验论文。")
                if status >= 400:
                    response.close()
                    raise ValueError(f"{service} 返回 HTTP {status}，请检查访问条件或稍后重试。")
                return response
        raise AccessDenied("Wiley 请求跳转超过 6 次。")


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _is_wiley(core: dict) -> bool:
    # Crossref member 311 identifies Wiley; publisher aliases cover older records.
    member = str(core.get("member", "")).rstrip("/").rsplit("/", 1)[-1]
    names = {"wiley", "wileyblackwell", "johnwileysons", "johnwileysonsinc", "johnwileysonsltd",
             "wileyvch", "wileyvchgmbh", "wileyvchverlaggmbhcokgaa", "blackwellpublishingltd"}
    return member == "311" or normalize_journal(str(core.get("publisher", ""))) in names


def _metadata(client: _Client, doi: str) -> dict:
    for attempt in range(client.config.retries + 1):
        response = client.request(_metadata_endpoint(doi))
        try:
            with response:
                body = bytearray()
                for chunk in response.iter_content(65536):
                    _check_cancel(client.event)
                    if len(body) + len(chunk) > _MAX_METADATA:
                        raise ValueError("Crossref 元数据超过 4 MB，已停止解析。")
                    body.extend(chunk)
            break
        except _STREAM_ERRORS:
            if attempt == client.config.retries:
                raise
    try:
        document = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Crossref 未返回有效 JSON 元数据，无法核验论文。") from None
    core = document.get("message") if isinstance(document, dict) and document.get("status") == "ok" else None
    if not isinstance(core, dict):
        raise ValueError("Crossref 元数据缺少有效 message。")
    actual = core.get("DOI")
    if not isinstance(actual, str) or actual.casefold() != doi.casefold():
        raise ValueError("Crossref 返回的 DOI 与请求不一致或缺失。")
    journals = [_redact(item, client.config) for item in _string_list(core.get("container-title"))]
    titles = _string_list(core.get("title"))
    return {"doi": doi, "title": _redact(titles[0] if titles else "", client.config),
            "journal": journals[0] if journals else "", "journals": journals,
            "publisher_name": _redact(str(core.get("publisher", "")), client.config),
            "is_wiley": _is_wiley(core), "metadata_source": _metadata_endpoint(doi), "_crossref": core}


def _download_pdf(client: _Client, endpoint: str, target: Path) -> dict:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        for attempt in range(client.config.retries + 1):
            response = client.request(endpoint, pdf=True)
            try:
                with response, temporary.open("wb") as handle:
                    kind = response.headers.get("Content-Type", "").lower()
                    if any(value in kind for value in ("html", "json", "xml")):
                        raise AccessDenied("Wiley 返回登录页或错误信息，未保存为 PDF。请检查 TDM 及机构内容权限。")
                    raw_length = response.headers.get("Content-Length", "")
                    expected = int(raw_length) if raw_length.isdigit() else None
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
                            raise AccessDenied("Wiley 返回 HTML 登录或错误页面，未保存为 PDF。")
                        digest.update(chunk)
                        handle.write(chunk)
                    if expected is not None and not response.headers.get("Content-Encoding") and size != expected:
                        raise ValueError("PDF 传输不完整，与 Content-Length 不一致。")
                    final_url = _public_url(response.url or endpoint)
                _check_cancel(client.event)
                if not size or not is_valid_pdf(temporary):
                    raise ValueError("返回内容不是完整 PDF（文件为空或缺少 PDF 标记）。")
                temporary.replace(target)
                return {"url": _public_url(endpoint), "final_url": final_url, "file": target.name,
                        "status": "success", "bytes": size, "sha256": digest.hexdigest()}
            except _STREAM_ERRORS:
                if attempt == client.config.retries:
                    raise
    finally:
        temporary.unlink(missing_ok=True)
    raise ValueError("Wiley PDF 下载失败。")


def _reuse_pdf(client: _Client, doi: str, endpoint: str, target: Path, previous: dict) -> dict | None:
    old = previous.get("pdf") or {}
    if (previous.get("publisher") != "Wiley" or previous.get("doi") != doi
            or old.get("url") != endpoint or old.get("file") != target.name
            or old.get("status") not in {"success", "exists"} or not old.get("sha256") or not is_valid_pdf(target)):
        return None
    size, digest = _digest(target, client.event)
    if size != old.get("bytes") or digest != old["sha256"]:
        return None
    return {"url": endpoint, "final_url": _public_url(old.get("final_url", "")), "file": target.name,
            "status": "exists", "bytes": size, "sha256": digest}


def _process(client: _Client, reference: ArticleReference) -> DownloadResult:
    from . import wiley_supplements as si
    result = DownloadResult(doi=reference.doi, pii=reference.pii, reference_key=reference.key,
                            status="failed", supplements_status="" if client.config.include_supplements else "disabled")
    directory = Path(client.config.output_dir) / ("wiley_" + paper_directory_name(reference.key))
    path = directory / "manifest.json"
    record = {"schema_version": 2, "publisher": "Wiley", "reference_key": reference.key,
              "doi": reference.doi, "title": "", "journal": "", "status": "in_progress", "pdf": None,
              "include_supplements": client.config.include_supplements, "supplements_status": result.supplements_status,
              "supplements": [], "assets": {},
              "supplement_discovery": {"status": "not_attempted" if client.config.include_supplements else "disabled"},
              "errors": []}
    main_status = "failed"
    discovery_started = False
    try:
        _check_cancel(client.event)
        doi, endpoint = _doi(reference), _endpoint(reference)
        directory.mkdir(parents=True, exist_ok=True)
        result.record_path = str(path.resolve())
        previous = _read_manifest(path)
        record["pdf"] = previous.get("pdf")
        record["assets"] = dict(previous.get("assets") or {})
        for asset in previous.get("supplements", []):
            if asset.get("status") in {"success", "exists"}:
                record["assets"][asset["url"]] = asset
        data = _metadata(client, doi)
        record.update({key: value for key, value in data.items() if not key.startswith("_")})
        result.doi, result.title, result.journal = doi, data["title"], data["journal"]
        result.article_url = "https://onlinelibrary.wiley.com/doi/" + quote(doi, safe="/")
        record["article_url"] = result.article_url
        if not data["is_wiley"]:
            result.status, result.error = "filtered", "Crossref 元数据未确认该论文属于 Wiley，未提交 Wiley TDM 下载。"
            return result
        if client.config.journal and not any(normalize_journal(journal) == normalize_journal(client.config.journal)
                                             for journal in data["journals"]):
            result.status = "filtered"
            result.error = "Crossref 期刊与所选期刊不一致。" if data["journals"] else "Crossref 缺少期刊名称，无法确认所选期刊。"
            return result
        target = directory / "article.pdf"
        try:
            pdf = _reuse_pdf(client, doi, endpoint, target, previous)
            if pdf is None:
                pdf = _download_pdf(client, endpoint, target)
            record["pdf"] = pdf
            main_status = pdf["status"]
            result.pdf_url, result.file_path = endpoint, str(target.resolve())
            _save_manifest(path, record)
        except (DownloadCancelled, _BatchStopped):
            raise
        except (AccessDenied, ValueError, OSError, requests.RequestException) as exc:
            main_status = "access_denied" if isinstance(exc, AccessDenied) else "failed"
            message = "正文 PDF: " + _safe_error(exc, client.config)
            record["pdf"] = {"url": endpoint, "file": "article.pdf", "status": main_status,
                             "bytes": 0, "sha256": "", "error": message}
            record["errors"].append(message)
            _save_manifest(path, record)
        if client.config.include_supplements:
            discovery_started = True
            try:
                descriptors, discovery = si.discover(client, doi, data["_crossref"])
                record["supplement_discovery"] = discovery
                result.supplements_total = len(descriptors)
                record["supplements"] = [{key: value for key, value in item.items() if key != "request_url"} for item in descriptors]
                if discovery["status"] == "not_indexed":
                    result.supplements_status = "not_indexed"
                    record["errors"].append("Crossref 未检索到 SI 记录，不能据此确认论文没有附件；可打开文章页面人工查看。")
                elif discovery["status"] == "failed":
                    result.supplements_status = "discovery_failed"
                else:
                    result.supplements_status = "partial"
                record["errors"].extend(discovery["errors"])
                _save_manifest(path, record)
            except (DownloadCancelled, _BatchStopped):
                raise
            except (AccessDenied, ValueError, OSError, requests.RequestException) as exc:
                descriptors = []
                result.supplements_status = "discovery_failed"
                message = "补充材料发现失败: " + _safe_error(exc, client.config)
                record["supplement_discovery"] = {"source": "Crossref REST + UNIXSD", "status": "failed", "error": message}
                record["errors"].append(message)
            old_assets = {asset.get("asset_key"): asset for asset in record["assets"].values() if asset.get("asset_key")}
            manual_total = 0
            for index, descriptor in enumerate(descriptors):
                _check_cancel(client.event)
                if descriptor["status"] == "manual_required":
                    manual_total += 1
                    continue
                attachment_target = directory / descriptor["file"]
                try:
                    asset = si.reuse(client, descriptor, attachment_target, old_assets.get(descriptor["asset_key"], {}))
                    if asset is None:
                        asset = si.download(client, descriptor, attachment_target)
                    if asset["status"] == "exists":
                        result.supplements_existing += 1
                    else:
                        result.supplements_downloaded += 1
                    record["supplements"][index] = asset
                    record["assets"][asset["url"]] = asset
                    _save_manifest(path, record)
                except DownloadCancelled:
                    record["supplements"][index].update({"status": "cancelled", "error": "用户取消下载。"})
                    raise
                except (AccessDenied, ValueError, OSError, requests.RequestException) as exc:
                    result.supplements_failed += 1
                    message = "补充材料: " + _safe_error(exc, client.config)
                    record["supplements"][index].update({"status": "access_denied" if isinstance(exc, AccessDenied) else "failed", "error": message})
                    record["errors"].append(message)
                    _save_manifest(path, record)
                    if isinstance(exc, _BatchStopped):
                        raise
            if descriptors:
                if manual_total:
                    record["errors"].append(f"有 {manual_total} 条关联附件记录需要人工确认或下载，来源链接见逐篇 JSON 清单。")
                if result.supplements_failed or record["supplement_discovery"]["status"] == "partial":
                    result.supplements_status = "partial"
                elif manual_total:
                    result.supplements_status = "manual_required"
                else:
                    result.supplements_status = "complete"
        main_ok = main_status in {"success", "exists"}
        if main_ok and result.supplements_status in {"disabled", "complete"}:
            result.status = "success" if main_status == "success" or result.supplements_downloaded else "exists"
        elif main_ok or result.supplements_downloaded + result.supplements_existing:
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
    except (ValueError, OSError, requests.RequestException) as exc:
        if result.file_path or result.supplements_downloaded + result.supplements_existing:
            result.status = "partial"
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
                result.error = (result.error + "；无法保存论文清单，请检查写入权限。").strip("；")
    return result


def run_wiley_batch(records: list[ArticleReference], config: WileyConfig,
                    on_result: Callable[[int, int, DownloadResult], None] | None = None,
                    on_log: Callable[[str], None] | None = None,
                    cancel_event: threading.Event | None = None) -> BatchSummary:
    validate_wiley_config(config)
    output = Path(config.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    event = cancel_event if cancel_event is not None else threading.Event()
    manifest = output / f"manifest_wiley_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid.uuid4().hex[:6]}.csv"
    summary = BatchSummary([], manifest)
    with requests.Session() as session, manifest.open("x", encoding="utf-8-sig", newline="") as handle:
        session.headers.update({"User-Agent": "PaperCollections/1.7 (Wiley TDM PDF and Crossref supplements)"})
        client = _Client(session, config, event)
        writer = csv.DictWriter(handle, fieldnames=list(DownloadResult.__dataclass_fields__))
        writer.writeheader()
        handle.flush()
        for index, reference in enumerate(records, 1):
            if event.is_set():
                summary.cancelled = True
                break
            if on_log:
                on_log(f"[{index}/{len(records)}] Wiley 正在处理 {_redact(reference.key, config)}")
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
        on_log(f"Wiley 结果清单已保存：{manifest}")
    return summary
