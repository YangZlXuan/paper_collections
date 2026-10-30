"""Bounded keyword discovery through publisher APIs, without downloading papers.

Search results feed the existing DOI/PII download adapters. A search match is not
an assertion that the current institution may retrieve the full text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import json
import math
from numbers import Real
import re
import threading
import time
from typing import Callable
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import requests

from .catalog import normalize_journal
from .doi_parser import ArticleReference, DOI_RE, extract_dois, normalize_pii
from .downloader import DownloadCancelled, _check_cancel


SPRINGER_ENDPOINT = "https://api.springernature.com/meta/v2/json"
ELSEVIER_ENDPOINT = "https://api.elsevier.com/content/search/sciencedirect"
WILEY_ENDPOINT = "https://onlinelibrary.wiley.com/action/sru"
_ENDPOINTS = {"springer": SPRINGER_ENDPOINT, "elsevier": ELSEVIER_ENDPOINT, "wiley": WILEY_ENDPOINT}
_SOURCES = {"springer": "Springer Meta API", "elsevier": "ScienceDirect Search API v2", "wiley": "Wiley SRU"}
_MAX_BODY = 8 * 1024 * 1024
_MAX_PAGES = 100
_MAX_SCANNED = 6000
_MAX_WAIT = 60.0
_REDIRECTS = {301, 302, 303, 307, 308}
_TRANSIENT = {500, 502, 503, 504}
_STREAM_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)
_SECRET_FIELDS = {"api_key", "apikey", "authorization", "access_token", "token", "insttoken"}


class SearchError(ValueError):
    """An actionable, credential-free discovery failure."""


@dataclass
class SearchConfig:
    publisher_id: str
    keywords: str
    journal: str = ""
    year_from: int | None = None
    year_to: int | None = None
    max_results: int = 100
    interval: float = 2.0
    timeout: float = 45
    retries: int = 2
    api_key: str = field(default="", repr=False)
    insttoken: str = field(default="", repr=False)


@dataclass
class SearchResult:
    records: list[ArticleReference]
    total: int | None = None
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    source: str = ""
    cancelled: bool = False


def validate_search_config(config: SearchConfig) -> None:
    if config.publisher_id not in _ENDPOINTS:
        raise SearchError("关键词检索目前支持 Springer Nature、Elsevier 和 Wiley。")
    if not isinstance(config.keywords, str) or not config.keywords.strip() or len(config.keywords.strip()) > 250:
        raise SearchError("请输入 1–250 个字符的关键词。")
    if not isinstance(config.journal, str) or len(config.journal) > 250:
        raise SearchError("期刊名称过长或无效。")
    if any(ord(char) < 32 for char in config.keywords + config.journal):
        raise SearchError("关键词和期刊名不能包含换行或控制字符。")
    for value in (config.year_from, config.year_to):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1000 <= value <= 9999):
            raise SearchError("年份须为四位数字，留空表示不限。")
    if config.year_from is not None and config.year_to is not None and config.year_from > config.year_to:
        raise SearchError("开始年份不能晚于结束年份。")
    if isinstance(config.max_results, bool) or not isinstance(config.max_results, int) or not 1 <= config.max_results <= 1000:
        raise SearchError("检索篇数上限须为 1–1000 的整数。")
    for name, value, minimum, maximum in (("请求间隔", config.interval, 2, 60), ("请求超时", config.timeout, 1, 300)):
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise SearchError(f"{name}须为 {minimum}–{maximum} 秒的有效数字。")
    if isinstance(config.retries, bool) or not isinstance(config.retries, int) or not 0 <= config.retries <= 5:
        raise SearchError("重试次数须为 0–5 的整数。")
    for token in (config.api_key, config.insttoken):
        if not isinstance(token, str) or any(ord(char) < 33 or ord(char) > 126 for char in token.strip()):
            raise SearchError("检索 API 凭据格式无效，请检查空格或换行。")
    if config.publisher_id != "wiley" and not config.api_key.strip():
        name = "SpringerMetaKey" if config.publisher_id == "springer" else "ElsevierKey"
        raise SearchError(f"请设置 {name} 环境变量或在 API 设置中填写对应 Key。")


def _redact(value: object, config: SearchConfig) -> str:
    text = str(value)
    for token in (config.api_key.strip(), config.insttoken.strip()):
        if token:
            text = text.replace(token, "[已隐藏]").replace(quote(token, safe=""), "[已隐藏]")
    return text


def _clean(value: object, config: SearchConfig) -> str:
    if not isinstance(value, str):
        return ""
    return _redact(" ".join(unescape(re.sub(r"<[^>]*>", "", value)).split()), config)[:2000]


def _warn(result: SearchResult, message: str) -> None:
    if message not in result.warnings:
        result.warnings.append(message)


def _wait(event: threading.Event, seconds: float) -> None:
    if event.wait(max(0.0, seconds)):
        raise DownloadCancelled()


def _number(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r"\d{1,12}", str(value)):
        raise SearchError(f"检索结果缺少有效的 {name}，无法确认分页完整性。")
    return int(value)


class _Client:
    def __init__(self, session: requests.Session, config: SearchConfig, event: threading.Event):
        self.session, self.config, self.event = session, config, event
        self.last_request: float | None = None
        self.quota_exhausted = False
        # Only cookies received during this SRU search may be sent back to Wiley.
        # Do not reuse browser cookies, raw Cookie headers or another client's jar.
        self._wiley_cookies = requests.cookies.RequestsCookieJar()

    def _allowed(self, url: str) -> bool:
        try:
            actual, expected = urlsplit(url), urlsplit(_ENDPOINTS[self.config.publisher_id])
            return (actual.scheme == "https" and actual.hostname == expected.hostname
                    and actual.port in (None, 443) and actual.username is None and actual.password is None
                    and actual.path == expected.path and not actual.fragment)
        except ValueError:
            return False

    def _retry_after(self, response: requests.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After", "")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(raw)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                value = (date - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                value = max(float(self.config.interval), 2 ** attempt)
        return max(0.0, value) if math.isfinite(value) else _MAX_WAIT + 1

    def _redirect_error(self, url: str) -> str:
        if self.config.publisher_id == "wiley":
            try:
                destination = urlsplit(url)
                if destination.hostname == "onlinelibrary.wiley.com":
                    if destination.path == "/action/cookieAbsent":
                        return "Wiley 检索会话的 Cookie 检查未通过。请稍后重试，或使用官网导出的 DOI / RIS 文件；此问题与 WileyKey 无关。"
                    if destination.path == "/action/oidcStart":
                        return "Wiley 检索被转到身份验证页面，已停止请求。请检查网络访问条件，或使用官网导出的 DOI / RIS 文件。"
            except ValueError:
                pass
        return "检索接口跳转到非预期地址，已停止请求。"

    def read(self, url: str, *, payload: dict | None = None, xml: bool = False) -> bytes:
        if self.quota_exhausted:
            raise SearchError("检索接口配额已耗尽，请等待重置后再试。")
        method = "PUT" if payload is not None else "GET"
        for redirect in range(5):
            if not self._allowed(url):
                raise SearchError(self._redirect_error(url))
            for attempt in range(self.config.retries + 1):
                _check_cancel(self.event)
                if self.last_request is not None:
                    _wait(self.event, self.config.interval - (time.monotonic() - self.last_request))
                prepared = self.session.prepare_request(requests.Request(method, url, json=payload))
                for name in list(prepared.headers):
                    if name.casefold().startswith(("wiley-", "x-els-")) or name.casefold() in {
                        "authorization", "proxy-authorization", "cookie", "x-api-key", "api-key", "apikey"}:
                        del prepared.headers[name]
                # A reused Session must not leak another publisher's credentials.
                parsed = urlsplit(prepared.url)
                query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                         if key.casefold() not in _SECRET_FIELDS]
                if self.config.publisher_id == "springer":
                    query.append(("api_key", self.config.api_key.strip()))
                prepared.url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
                if not self._allowed(prepared.url):
                    raise SearchError("检索请求地址无效，已停止请求。")
                if self.config.publisher_id == "wiley":
                    # CookieJar applies domain, path, Secure and expiry rules;
                    # the endpoint allowlist above still applies to every hop.
                    prepared.prepare_cookies(self._wiley_cookies)
                if self.config.publisher_id == "elsevier":
                    prepared.headers["X-ELS-APIKey"] = self.config.api_key.strip()
                    if self.config.insttoken.strip():
                        prepared.headers["X-ELS-Insttoken"] = self.config.insttoken.strip()
                prepared.headers["Accept"] = "application/xml" if xml else "application/json"
                settings = self.session.merge_environment_settings(prepared.url, {}, True, None, None)
                self.last_request = time.monotonic()
                try:
                    response = self.session.send(prepared, allow_redirects=False,
                        timeout=(min(15.0, self.config.timeout), self.config.timeout), **settings)
                    with response:
                        _check_cancel(self.event)
                        if self.config.publisher_id == "wiley":
                            requests.cookies.extract_cookies_to_jar(self._wiley_cookies, prepared, response.raw)
                        status = response.status_code
                        if status in _REDIRECTS:
                            location = response.headers.get("Location", "")
                            if not location or redirect == 4 or (method == "PUT" and status not in {307, 308}):
                                raise SearchError("检索接口返回无法安全跟随的跳转。")
                            url = urljoin(prepared.url, location)
                            break
                        if status in {401, 403}:
                            if self.config.publisher_id == "elsevier":
                                raise SearchError(f"ScienceDirect 搜索接口访问受限（HTTP {status}）。请确认 ElsevierKey 的 Search API 权限及机构网络；正文下载权限不等于搜索权限。")
                            if self.config.publisher_id == "springer":
                                raise SearchError(f"Springer Meta 检索访问受限（HTTP {status}），请检查 SpringerMetaKey 及 Meta API 授权。")
                            raise SearchError(f"Wiley SRU 检索访问受限（HTTP {status}），请稍后重试或使用官网导出的 DOI/RIS 文件；WileyKey 不用于 SRU 认证。")
                        if status == 429:
                            delay = self._retry_after(response, attempt)
                            if "QUOTA_EXCEEDED" in response.headers.get("X-ELS-Status", ""):
                                raise SearchError("ScienceDirect 搜索配额已耗尽，请等待重置或联系接口支持。")
                            if delay > _MAX_WAIT or attempt == self.config.retries:
                                raise SearchError("检索接口限流，已停止请求，请按服务端等待要求稍后重试。")
                            _wait(self.event, delay)
                            continue
                        if status in _TRANSIENT and attempt < self.config.retries:
                            continue
                        if status >= 400:
                            raise SearchError(f"{_SOURCES[self.config.publisher_id]} 返回 HTTP {status}，请检查检索条件、接口权限或稍后重试。")
                        kind = response.headers.get("Content-Type", "").casefold()
                        if "html" in kind:
                            raise SearchError("检索接口返回网页或验证页，未将其当作检索结果。")
                        raw_length = response.headers.get("Content-Length", "")
                        if raw_length.isdigit() and int(raw_length) > _MAX_BODY:
                            raise SearchError("检索响应超过 8 MB，已停止读取。")
                        body = bytearray()
                        for chunk in response.iter_content(65536):
                            _check_cancel(self.event)
                            if len(body) + len(chunk) > _MAX_BODY:
                                raise SearchError("检索响应超过 8 MB，已停止读取。")
                            body.extend(chunk)
                        if raw_length.isdigit() and not response.headers.get("Content-Encoding") and len(body) != int(raw_length):
                            raise SearchError("检索响应传输不完整。")
                        self.quota_exhausted = response.headers.get("X-RateLimit-Remaining") == "0"
                        return bytes(body)
                except _STREAM_ERRORS:
                    if attempt == self.config.retries:
                        raise SearchError("检索网络连接中断或超时，请检查网络后重试。") from None
                except requests.RequestException:
                    raise SearchError("检索网络请求失败，请检查网络或代理设置。") from None
            else:
                raise SearchError("检索请求重试后仍失败。")
        raise SearchError("检索接口跳转过多。")


def _json(body: bytes) -> dict:
    try:
        document = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise SearchError("检索接口未返回有效 JSON，可能是登录或错误页面。") from None
    if not isinstance(document, dict):
        raise SearchError("检索接口返回了不支持的数据结构。")
    if any(key in document for key in ("error", "error-response", "service-error")):
        raise SearchError("检索接口返回错误信息，请检查检索条件及 API 权限。")
    return document


def _springer_query(config: SearchConfig) -> str:
    # Basic Meta keys support free-text and date constraints. The journal:
    # constraint is Premium-only. Verify the returned journal locally; adding
    # its name as unscoped text would omit records whose searchable text lacks it.
    parts = [config.keywords.strip(), "type:Journal"]
    if config.year_from is not None:
        parts.append(f"datefrom:{config.year_from}-01-01")
    if config.year_to is not None:
        parts.append(f"dateto:{config.year_to}-12-31")
    return " ".join(parts)


def _springer_page(client: _Client, start: int, count: int) -> tuple[list[dict], int, int | None]:
    query = urlencode({"q": _springer_query(client.config), "s": start, "p": count})
    data = _json(client.read(SPRINGER_ENDPOINT + "?" + query))
    info, rows = data.get("result"), data.get("records")
    if not isinstance(info, list) or not info or not isinstance(info[0], dict) or not isinstance(rows, list):
        raise SearchError("Springer Meta 响应缺少有效的 result / records。")
    total = _number(info[0].get("total"), "total")
    if "start" in info[0] and _number(info[0]["start"], "start") != start:
        raise SearchError("Springer Meta 返回的页位置与请求不一致。")
    entries = []
    for row in rows:
        if isinstance(row, dict):
            entries.append({"doi": row.get("doi"), "title": row.get("title"),
                            "journals": [row.get("publicationName") or row.get("journalTitle") or ""],
                            "date": row.get("publicationDate"), "pii": ""})
        else:
            entries.append({})
    return entries, total, start + len(rows) if rows and start + len(rows) <= total else None


def _elsevier_page(client: _Client, start: int, count: int) -> tuple[list[dict], int, int | None]:
    config = client.config
    payload = {"qs": config.keywords.strip(), "display": {"offset": start, "show": count, "sortBy": "relevance"}}
    if config.journal.strip():
        payload["pub"] = config.journal.strip()
    if config.year_from is not None or config.year_to is not None:
        payload["date"] = f"{config.year_from or 1000}-{config.year_to or 9999}"
    data = _json(client.read(ELSEVIER_ENDPOINT, payload=payload))
    rows = data.get("results")
    if not isinstance(rows, list):
        raise SearchError("ScienceDirect 搜索响应缺少有效的 results。")
    total = _number(data.get("resultsFound"), "resultsFound")
    entries = []
    for row in rows:
        if isinstance(row, dict):
            entries.append({"doi": row.get("doi"), "pii": row.get("pii"), "title": row.get("title"),
                            "journals": [row.get("sourceTitle") or ""], "date": row.get("publicationDate")})
        else:
            entries.append({})
    return entries, total, start + len(rows) if rows and start + len(rows) < total else None


def _cql(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _wiley_query(config: SearchConfig) -> str:
    parts = ["cql.anywhere=" + _cql(config.keywords.strip())]
    if config.journal.strip():
        parts.append("dcterms.isPartOf=" + _cql(config.journal.strip()))
    if config.year_from is not None:
        parts.append(f'dc.date >= "{config.year_from}-01-01"')
    if config.year_to is not None:
        parts.append(f'dc.date <= "{config.year_to}-12-31"')
    return " and ".join(parts)


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _texts(element: ET.Element, name: str) -> list[str]:
    return [" ".join("".join(item.itertext()).split()) for item in element.iter() if _tag(item) == name]


def _doi_identifier(value: object) -> str:
    if not isinstance(value, str):
        return ""
    raw = unquote(unescape(value.strip()))
    if raw.lower().startswith(("http://", "https://")):
        try:
            parsed = urlsplit(raw)
            if (parsed.hostname not in {"doi.org", "dx.doi.org"} or parsed.username is not None
                    or parsed.password is not None or parsed.port not in {None, 80, 443}
                    or parsed.query or parsed.fragment):
                return ""
            raw = parsed.path.lstrip("/")
        except ValueError:
            return ""
    raw = re.sub(r"^doi\s*:\s*", "", raw, flags=re.I)
    if not DOI_RE.fullmatch(raw) or any(char in raw for char in "<>"):
        return ""
    candidates = extract_dois(raw)
    return candidates[0] if len(candidates) == 1 else ""


def _wiley_page(client: _Client, start: int, count: int) -> tuple[list[dict], int, int | None]:
    query = urlencode({"version": "1.2", "operation": "searchRetrieve", "query": _wiley_query(client.config),
                       "recordSchema": "pam", "recordPacking": "xml", "startRecord": start, "maximumRecords": count})
    body = client.read(WILEY_ENDPOINT + "?" + query, xml=True)
    try:
        xml_text = body.decode("utf-8-sig")
    except UnicodeError:
        raise SearchError("Wiley SRU 返回了不支持的 XML 编码。") from None
    if "\x00" in xml_text or "<!DOCTYPE" in xml_text.upper() or "<!ENTITY" in xml_text.upper():
        raise SearchError("Wiley SRU XML 包含不支持的文档类型或实体声明。")
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError):
        raise SearchError("Wiley SRU 未返回有效 XML，可能是网页验证或服务异常。") from None
    if _tag(root) != "searchRetrieveResponse":
        raise SearchError("Wiley SRU 返回了非检索响应。")
    if any(_tag(item) == "diagnostic" for item in root.iter()):
        raise SearchError("Wiley SRU 拒绝了检索条件或请求参数，请调整关键词后重试。")
    totals = _texts(root, "numberOfRecords")
    total = _number(totals[0] if totals else None, "numberOfRecords")
    entries = []
    for record in (item for item in root.iter() if _tag(item) == "record"):
        data = next((item for item in record if _tag(item) == "recordData"), None)
        if data is None:
            entries.append({})
            continue
        titles, dates = _texts(data, "title"), _texts(data, "date")
        identifiers = _texts(data, "identifier")
        # DC identifiers can include unrelated non-DOI IDs. Only a single DOI is
        # accepted; the article URL is rebuilt from that identifier later.
        dois = list(dict.fromkeys(doi for value in identifiers if (doi := _doi_identifier(value))))
        entries.append({"doi": dois[0] if len(dois) == 1 else "", "pii": "",
                        "title": titles[0] if titles else "", "journals": _texts(data, "isPartOf"),
                        "date": dates[0] if dates else ""})
    positions = _texts(root, "nextRecordPosition")
    next_page = _number(positions[0], "nextRecordPosition") if positions else None
    if next_page == 0:
        next_page = None
    if next_page is None and entries and start + len(entries) <= total:
        next_page = start + len(entries)
    return entries, total, next_page


def _reference(row: dict, config: SearchConfig, result: SearchResult) -> ArticleReference | None:
    journals = [_clean(value, config) for value in row.get("journals", []) if isinstance(value, str)]
    if config.journal.strip() and not any(normalize_journal(value) == normalize_journal(config.journal) for value in journals):
        if not any(journals):
            _warn(result, "部分记录缺少期刊名称，无法核验所选期刊，已跳过。")
        return None
    if config.year_from is not None or config.year_to is not None:
        match = re.match(r"^(\d{4})(?:$|[-/T ])", str(row.get("date") or ""))
        if not match:
            _warn(result, "部分记录缺少可核验的发表年份，已跳过。")
            return None
        year = int(match[1])
        if (config.year_from is not None and year < config.year_from) or (config.year_to is not None and year > config.year_to):
            return None
    doi = _doi_identifier(row.get("doi"))
    pii = normalize_pii(row.get("pii")) if config.publisher_id == "elsevier" else ""
    if not doi and not pii:
        _warn(result, "部分记录没有有效 DOI/PII，已跳过。")
        return None
    if any(token.strip() and token.strip().lower() in doi for token in (config.api_key, config.insttoken)):
        _warn(result, "检索返回异常标识，已跳过。")
        return None
    url = "https://doi.org/" + quote(doi, safe="/") if doi else "https://www.sciencedirect.com/science/article/pii/" + pii
    journal = next((value for value in journals if config.journal and normalize_journal(value) == normalize_journal(config.journal)), journals[0] if journals else "")
    return ArticleReference(doi=doi, pii=pii, url=url, title=_clean(row.get("title"), config), journal=journal)


def search_articles(config: SearchConfig, on_log: Callable[[str], None] | None = None,
                    cancel_event: threading.Event | None = None) -> SearchResult:
    validate_search_config(config)
    event = cancel_event if cancel_event is not None else threading.Event()
    result = SearchResult([], source=_SOURCES[config.publisher_id])
    if config.publisher_id == "springer" and config.journal.strip():
        _warn(result, "Springer 基础 Meta Key 不支持按期刊检索，已在关键词结果中本地精确筛选期刊；接口总命中数属于关键词检索，非所选期刊总量。")
    start = 0 if config.publisher_id == "elsevier" else 1
    fetch = {"springer": _springer_page, "elsevier": _elsevier_page, "wiley": _wiley_page}[config.publisher_id]
    page_size = 25 if config.publisher_id == "elsevier" else 20
    visited: set[tuple] = set()
    by_doi: dict[str, ArticleReference] = {}
    by_pii: dict[str, ArticleReference] = {}
    scanned = 0
    try:
        with requests.Session() as session:
            session.headers.update({"User-Agent": "PaperCollections/1.9 (publisher keyword discovery)"})
            client = _Client(session, config, event)
            for page in range(_MAX_PAGES):
                _check_cancel(event)
                rows, total, next_page = fetch(client, start, page_size)
                result.total = total
                signature = tuple((str(row.get("doi", "")), str(row.get("pii", "")), str(row.get("title", ""))) for row in rows)
                if rows and signature in visited:
                    result.truncated = True
                    _warn(result, "检索接口重复返回同一页，已停止继续分页；结果可能不完整。")
                    break
                visited.add(signature)
                scanned += len(rows)
                for index, row in enumerate(rows):
                    _check_cancel(event)
                    item = _reference(row, config, result)
                    if item is None:
                        continue
                    doi_match, pii_match = by_doi.get(item.doi), by_pii.get(item.pii)
                    if doi_match is not None and pii_match is not None and doi_match is not pii_match:
                        _warn(result, "检索记录中的 DOI 与 PII 关联存在冲突，已跳过冲突记录。")
                        continue
                    old = doi_match or pii_match
                    if old is not None:
                        if (old.doi and item.doi and old.doi != item.doi) or (old.pii and item.pii and old.pii != item.pii):
                            _warn(result, "检索记录中的 DOI 与 PII 关联存在冲突，已跳过冲突记录。")
                            continue
                        old.doi, old.pii = old.doi or item.doi, old.pii or item.pii
                        old.title, old.journal = old.title or item.title, old.journal or item.journal
                        if old.doi:
                            old.url = "https://doi.org/" + quote(old.doi, safe="/")
                            by_doi[old.doi] = old
                        if old.pii:
                            by_pii[old.pii] = old
                        continue
                    result.records.append(item)
                    if item.doi:
                        by_doi[item.doi] = item
                    if item.pii:
                        by_pii[item.pii] = item
                    if len(result.records) >= config.max_results:
                        result.truncated = next_page is not None or index < len(rows) - 1
                        if result.truncated:
                            _warn(result, "已达到本次检索篇数上限；可缩小关键词、期刊或年份范围继续检索。")
                        break
                if on_log:
                    on_log(f"{result.source}：已查询 {page + 1} 页，取得 {len(result.records)} 条可下载标识。")
                if len(result.records) >= config.max_results:
                    break
                if not rows:
                    if (start if config.publisher_id == "elsevier" else start - 1) < total:
                        result.truncated = True
                        _warn(result, "检索接口提前返回空页，结果可能不完整。")
                    break
                if next_page is None:
                    break
                if next_page <= start:
                    result.truncated = True
                    _warn(result, "检索接口分页位置未前进，已停止；结果可能不完整。")
                    break
                if scanned >= _MAX_SCANNED or next_page >= _MAX_SCANNED or page == _MAX_PAGES - 1:
                    result.truncated = True
                    _warn(result, "已达到本次安全分页上限，结果可能不完整；请缩小年份或关键词范围。")
                    break
                start = next_page
    except DownloadCancelled:
        result.cancelled = True
        result.truncated = True
        _warn(result, "检索已取消；保留已取得的结果。")
    except (SearchError, requests.RequestException) as exc:
        if not result.records:
            if isinstance(exc, SearchError):
                raise SearchError(_redact(exc, config)) from None
            raise SearchError("检索网络请求失败，请检查网络后重试。") from None
        result.truncated = True
        message = _redact(exc, config) if isinstance(exc, SearchError) else "检索网络请求失败。"
        _warn(result, "检索未完成，已保留部分结果：" + message)
    return result
