"""Discover Wiley supplements from explicit Crossref associations, never page crawling.

Crossref's UNIXSD transform exposes deposited component_list entries that are
often absent from its REST JSON. A missing component is not evidence of no SI.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import requests

from . import wiley as wl
from .doi_parser import extract_dois
from .downloader import AccessDenied, DownloadCancelled, _check_cancel, _digest, _looks_html, is_valid_pdf
from .elsevier import _supplement_suffix, _validate_archive


_XML_MIME = "application/vnd.crossref.unixsd+xml"
_SI_HOSTS = {"onlinelibrary.wiley.com", "advanced.onlinelibrary.wiley.com", "alm.wiley.com"}
_MAX_COMPONENTS = 100
_MAX_RELATED = 30
_FILE_SUFFIXES = {".pdf", ".zip", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".csv", ".tsv",
                  ".txt", ".xml", ".json", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp", ".svg",
                  ".mp4", ".mov", ".avi", ".mpg", ".mpeg", ".mp3", ".wav", ".ogg", ".webm",
                  ".gz", ".tgz", ".tar", ".bz2", ".rar", ".7z", ".mat", ".cif", ".pdb", ".sdf", ".mol"}


def metadata_url(doi: str) -> str:
    return wl._metadata_endpoint(doi) + "/transform/" + _XML_MIME


def relations_url(doi: str) -> str:
    return "https://api.crossref.org/works?" + urlencode({"filter": "relation.object:" + doi, "rows": 100})


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [item for item in element if _tag(item) == name]


def _text(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def _doi_data(element: ET.Element) -> tuple[str, list[str]]:
    items = _children(element, "doi_data")
    if not items:
        return "", []
    identifiers = _children(items[0], "doi")
    dois = extract_dois(_text(identifiers[0])) if identifiers else []
    urls = [_text(item) for item in items[0].iter() if _tag(item) == "resource" and _text(item)]
    return dois[0] if len(dois) == 1 else "", list(dict.fromkeys(urls))


def parse_components(body: bytes, doi: str) -> list[dict]:
    """Read only component lists attached to the requested, verified parent DOI."""
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("Crossref XML 包含不支持的文档类型或实体声明。")
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError):
        raise ValueError("Crossref 未返回有效的 UNIXSD XML 元数据。") from None
    if _tag(root) not in {"crossref_result", "doi_records", "crossref", "doi_record"}:
        raise ValueError("Crossref 响应不是支持的论文元数据 XML。")
    containers = []
    for element in root.iter():
        if _tag(element) == "journal_article" and _doi_data(element)[0].casefold() == doi.casefold():
            containers.append(element)
        elif _tag(element) == "sa_component" and element.attrib.get("parent_doi", "").casefold() == doi.casefold():
            containers.append(element)
    if not containers:
        raise ValueError("Crossref XML 缺少与当前论文 DOI 匹配的主体，无法核验附件归属。")
    components = []
    for container in containers:
        for group in _children(container, "component_list"):
            for element in _children(group, "component"):
                if len(components) >= _MAX_COMPONENTS:
                    raise ValueError("Crossref 组件超过 100 个，已停止自动解析，请人工查看来源记录。")
                if element.attrib.get("parent_relation") != "isPartOf":
                    continue
                component_doi, urls = _doi_data(element)
                if not component_doi or component_doi.casefold() == doi.casefold():
                    raise ValueError("Crossref 组件 DOI 缺失或与父论文相同，无法核验附件身份。")
                title = " ".join(_text(item) for item in element.iter() if _tag(item) == "title")
                description = " ".join(_text(item) for item in _children(element, "description"))
                formats = _children(element, "format")
                components.append({"doi": component_doi, "title": title, "description": description,
                                   "mimetype": formats[0].attrib.get("mime_type", "") if formats else "",
                                   "urls": urls, "parent_doi": doi, "source": "Crossref UNIXSD component_list"})
    return components


def _supplement_marked(item: dict) -> bool:
    label = item.get("title", "") + " " + item.get("description", "")
    if re.search(r"\b(?:supporting\s+information|supplementary|supplemental|supplement(?:s|ary)?\s+(?:material|data|file)|appendix\s+s\d+|(?:data|table|figure)\s+s\d+)\b", label, re.I):
        return True
    # These are metadata-advertised supplementary-resource paths, not guessed
    # filenames or guessed '.s1' DOI relationships.
    for url in item.get("urls", []):
        try:
            path = unquote(urlsplit(url).path).lower()
        except ValueError:
            continue
        if "/supinfo/" in path or "/suppl_file/" in path or "/doi/suppl/" in path:
            return True
        if path == "/action/downloadsupplement":
            return True
    return False


def _safe_source_url(url: str, config: wl.WileyConfig) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        if parsed.hostname == "alm.wiley.com":
            return "https://alm.wiley.com/"
        query = ""
        if parsed.path.casefold() == "/action/downloadsupplement":
            fields = {key.casefold(): value for key, value in parse_qsl(parsed.query)}
            query = urlencode({key: fields[key] for key in ("doi", "file") if key in fields})
        return wl._redact(urlunsplit((parsed.scheme, parsed.hostname, parsed.path, query, "")), config)
    except ValueError:
        return ""


def _download_url(url: str, mime: str, parent_doi: str, config: wl.WileyConfig) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme == "http" and parsed.hostname in _SI_HOSTS:
            url = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
            parsed = urlsplit(url)
        if parsed.hostname not in _SI_HOSTS or not wl._allowed(url, pdf=False, supplement=True, config=config):
            return ""
        path = unquote(parsed.path).lower()
        if path == "/action/downloadsupplement":
            fields = {key.casefold(): value for key, value in parse_qsl(parsed.query)}
            if fields.get("doi", "").casefold() != parent_doi.casefold() or Path(fields.get("file", "")).suffix.lower() not in _FILE_SUFFIXES:
                return ""
            return url
        if "/doi/suppl/" in path:
            article = path.split("/doi/suppl/", 1)[1].split("/supinfo/", 1)[0].split("/suppl_file/", 1)[0]
            if article.casefold() != parent_doi.casefold():
                return ""
        # Bare /doi/ and /doi/suppl/ pages are navigation targets, not files.
        if "/doi/" in path and "/supinfo/" not in path and "/suppl_file/" not in path:
            return ""
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in _FILE_SUFFIXES and not ("/download/" in path and mime not in {"", "text/html", "application/xhtml+xml"}):
            return ""
        return url
    except ValueError:
        return ""


def _descriptors(item: dict, source: str, config: wl.WileyConfig) -> list[dict]:
    result = []
    for url in item.get("urls", []):
        mime = item.get("resource_mimes", {}).get(url, item.get("mimetype", ""))
        request_url = "" if item.get("force_manual") else _download_url(url, mime, item["parent_doi"], config)
        if not request_url:
            continue
        public = _safe_source_url(request_url, config)
        identity = item.get("doi", "") + "|" + public
        key = hashlib.sha256(identity.encode()).hexdigest()
        filename = {key.casefold(): value for key, value in parse_qsl(urlsplit(request_url).query)}.get("file", "") if urlsplit(request_url).path.casefold() == "/action/downloadsupplement" else ""
        suffix = _supplement_suffix(filename, mime, request_url)
        result.append({"asset_key": key, "component_doi": item.get("doi", ""), "parent_doi": item["parent_doi"],
                       "title": wl._redact(item.get("title", ""), config), "url": public, "source": item["source"],
                       "metadata_url": source, "mimetype": mime, "request_url": request_url,
                       "file": f"SI_{key[:20]}{suffix}", "status": "pending", "bytes": 0, "sha256": ""})
    if result:
        return result
    links = list(dict.fromkeys(link for url in item.get("urls", []) if (link := _safe_source_url(url, config))))
    if item.get("doi"):
        links.append("https://doi.org/" + quote(item["doi"], safe="/"))
    key = hashlib.sha256((item.get("doi", "") + "|" + "|".join(links)).encode()).hexdigest()
    return [{"asset_key": key, "component_doi": item.get("doi", ""), "parent_doi": item["parent_doi"],
             "title": wl._redact(item.get("title", ""), config), "url": links[0] if links else "",
             "source": item["source"], "metadata_url": source, "links": links,
             "file": "", "status": "manual_required", "bytes": 0, "sha256": "",
             "error": item.get("manual_reason", "已发现关联记录，但没有可自动下载并核验的附件直链，请人工查看来源链接。") }]


def _xml_body(client: wl._Client, doi: str) -> bytes:
    for attempt in range(client.config.retries + 1):
        response = client.request(metadata_url(doi), accept=_XML_MIME)
        try:
            with response:
                body = bytearray()
                for chunk in response.iter_content(65536):
                    _check_cancel(client.event)
                    if len(body) + len(chunk) > wl._MAX_METADATA:
                        raise ValueError("Crossref 组件元数据超过 4 MB。")
                    body.extend(chunk)
            return bytes(body)
        except wl._STREAM_ERRORS:
            if attempt == client.config.retries:
                raise
    raise ValueError("Crossref 组件元数据读取失败。")


def _reverse_body(client: wl._Client, doi: str) -> dict:
    for attempt in range(client.config.retries + 1):
        response = client.request(relations_url(doi))
        try:
            with response:
                body = bytearray()
                for chunk in response.iter_content(65536):
                    _check_cancel(client.event)
                    if len(body) + len(chunk) > wl._MAX_METADATA:
                        raise ValueError("Crossref 反向关系元数据超过 4 MB。")
                    body.extend(chunk)
            break
        except wl._STREAM_ERRORS:
            if attempt == client.config.retries:
                raise
    try:
        document = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Crossref 反向关系未返回有效 JSON。") from None
    message = document.get("message") if isinstance(document, dict) and document.get("status") == "ok" else None
    if not isinstance(message, dict) or not isinstance(message.get("items"), list) or not isinstance(message.get("total-results"), int):
        raise ValueError("Crossref 反向关系缺少有效的 items / total-results。")
    return message


def _related_item(core: dict, doi: str, child_doi: str, source: str) -> dict:
    titles = wl._string_list(core.get("title"))
    item = {"doi": child_doi, "parent_doi": doi, "title": titles[0] if titles else "", "urls": [], "mimetype": "", "source": source}
    links = core.get("link", [])
    resources = []
    if isinstance(links, list):
        resources.extend((link.get("URL", ""), link.get("content-type", "")) for link in links
                         if isinstance(link, dict) and isinstance(link.get("URL"), str))
    resource = core.get("resource", {})
    primary = resource.get("primary", {}) if isinstance(resource, dict) else {}
    if isinstance(primary, dict) and isinstance(primary.get("URL"), str):
        resources.append((primary["URL"], ""))
    item["urls"] = list(dict.fromkeys(url for url, _mime in resources))
    item["resource_mimes"] = {url: mime for url, mime in resources if mime}
    if not (wl._is_wiley(core) and _supplement_marked(item) and core.get("type") in {"component", "dataset"}):
        item["force_manual"] = True
        item["manual_reason"] = "Crossref 关联记录可能是数据、协议或预印本；缺少明确附件类型及可核验直链，需人工确认。"
    return item


def _points_to_parent(core: dict, parent: str, kind: str) -> bool:
    relations = core.get("relation")
    targets = relations.get(kind, []) if isinstance(relations, dict) else []
    return isinstance(targets, list) and any(isinstance(item, dict) and item.get("id-type") == "doi"
        and extract_dois(str(item.get("id", ""))) == [parent] for item in targets)


def discover(client: wl._Client, doi: str, core: dict) -> tuple[list[dict], dict]:
    if not wl._is_wiley(core) or str(core.get("DOI", "")).casefold() != doi.casefold():
        raise ValueError("附件查询缺少已核验的 Wiley 父论文元数据。")
    discovery = {"source": "Crossref REST + UNIXSD", "url": metadata_url(doi), "parent_doi": doi,
                 "status": "complete", "components_total": 0, "selected_total": 0, "errors": [], "sources": []}
    descriptors = []
    try:
        components = parse_components(_xml_body(client, doi), doi)
        discovery["components_total"] = len(components)
        for item in components:
            if _supplement_marked(item):
                descriptors.extend(_descriptors(item, metadata_url(doi), client.config))
        discovery["sources"].append({"url": metadata_url(doi), "status": "complete"})
    except (DownloadCancelled, wl._BatchStopped):
        raise
    except (ValueError, OSError, requests.RequestException, AccessDenied) as exc:
        discovery["errors"].append("Crossref XML: " + wl._safe_error(exc, client.config))
        discovery["sources"].append({"url": metadata_url(doi), "status": "failed"})
    relation = core.get("relation")
    related = relation.get("is-supplemented-by", []) if isinstance(relation, dict) else []
    if not isinstance(related, list):
        related = []
    seen = {item.get("component_doi") for item in descriptors}
    if len(related) > _MAX_RELATED:
        discovery["errors"].append("关联记录超过 30 条，已停止其余关联查询，请人工查看 Crossref 记录。")
    for relation_item in related[:_MAX_RELATED]:
        _check_cancel(client.event)
        if not isinstance(relation_item, dict):
            continue
        if relation_item.get("id-type") == "uri" and isinstance(relation_item.get("id"), str):
            item = {"doi": "", "parent_doi": doi, "title": "Crossref 关联补充资源", "urls": [relation_item["id"]],
                    "source": "Crossref REST is-supplemented-by URI", "force_manual": True}
            descriptors.extend(_descriptors(item, wl._metadata_endpoint(doi), client.config))
            continue
        if relation_item.get("id-type") != "doi":
            continue
        identifiers = extract_dois(str(relation_item.get("id", "")))
        if len(identifiers) != 1 or identifiers[0] == doi or identifiers[0] in seen:
            continue
        related_doi = identifiers[0]
        seen.add(related_doi)
        item = {"doi": related_doi, "parent_doi": doi, "title": "", "urls": [], "mimetype": "",
                "source": "Crossref REST is-supplemented-by"}
        source = wl._metadata_endpoint(related_doi)
        try:
            data = wl._metadata(client, related_doi)
            item = _related_item(data["_crossref"], doi, related_doi, item["source"])
            descriptors.extend(_descriptors(item, source, client.config))
        except (DownloadCancelled, wl._BatchStopped):
            raise
        except (ValueError, OSError, requests.RequestException, AccessDenied) as exc:
            item["manual_reason"] = "已发现关联 DOI，但 Crossref 未提供可用于自动下载的记录：" + wl._safe_error(exc, client.config)
            descriptors.extend(_descriptors(item, source, client.config))
        discovery["sources"].append({"url": source, "status": "queried"})
    try:
        reverse = _reverse_body(client, doi)
        discovery["reverse_total"] = reverse["total-results"]
        if reverse["total-results"] > len(reverse["items"]) or len(reverse["items"]) > 100:
            discovery["errors"].append("Crossref 反向关系超过本次返回上限，发现结果可能不完整，请人工查看来源记录。")
        for core_item in reverse["items"][:100]:
            if not isinstance(core_item, dict):
                continue
            identifiers = extract_dois(str(core_item.get("DOI", "")))
            if len(identifiers) != 1 or identifiers[0] == doi or identifiers[0] in seen:
                continue
            specific = _points_to_parent(core_item, doi, "is-supplement-to")
            part = any(_points_to_parent(core_item, doi, kind) for kind in ("is-part-of", "is-child-of"))
            if not specific and not part:
                continue
            child_doi = identifiers[0]
            item = _related_item(core_item, doi, child_doi, "Crossref REST reverse relation.object")
            if not specific and not _supplement_marked(item):
                continue
            seen.add(child_doi)
            descriptors.extend(_descriptors(item, relations_url(doi), client.config))
        discovery["sources"].append({"url": relations_url(doi), "status": "complete"})
    except (DownloadCancelled, wl._BatchStopped):
        raise
    except (ValueError, OSError, requests.RequestException, AccessDenied) as exc:
        discovery["errors"].append("Crossref 反向关系: " + wl._safe_error(exc, client.config))
        discovery["sources"].append({"url": relations_url(doi), "status": "failed"})
    unique = {}
    for item in descriptors:
        unique.setdefault(item["asset_key"], item)
    descriptors = list(unique.values())
    discovery["selected_total"] = len(descriptors)
    discovery["manual_total"] = sum(item["status"] == "manual_required" for item in descriptors)
    if discovery["errors"]:
        discovery["status"] = "partial" if descriptors else "failed"
    elif not descriptors:
        discovery["status"] = "not_indexed"
    discovery["candidates"] = [{key: value for key, value in item.items() if key != "request_url"} for item in descriptors]
    return descriptors, discovery


def _validate(path: Path, suffix: str, prefix: bytes, client: wl._Client) -> None:
    if _looks_html(prefix):
        raise AccessDenied("附件地址返回 HTML 登录或错误页面。")
    if suffix == ".pdf" or prefix.startswith(b"%PDF-"):
        if not is_valid_pdf(path):
            raise ValueError("补充材料 PDF 不完整。")
    elif suffix in {".zip", ".docx", ".xlsx", ".pptx"} or prefix.startswith(b"PK\x03\x04"):
        _validate_archive(path, suffix if suffix in {".docx", ".xlsx", ".pptx"} else ".zip", client.event)
    elif suffix in {".doc", ".xls", ".ppt"} and not prefix.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise ValueError("旧版 Office 附件文件头无效。")
    if suffix == ".json" or (prefix.lstrip().startswith((b"{", b"[")) and path.stat().st_size <= wl._MAX_METADATA):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError, RecursionError):
            if suffix == ".json":
                raise ValueError("JSON 附件格式无效。") from None
        else:
            if isinstance(payload, dict):
                error = payload.get("error")
                service_error = any(key in payload for key in ("service-error", "error-response"))
                simple_error = isinstance(error, str) and (len(payload) == 1 or any(key in payload for key in ("status", "code", "message")))
                structured_error = isinstance(error, dict) and any(key in error for key in ("code", "status", "message"))
                if service_error or simple_error or structured_error:
                    raise AccessDenied("附件地址返回错误响应，未保存为附件。")
    # Scientific XML can legitimately contain nested <error> measurements.
    # Reject only an error/metadata envelope at the document root.
    if re.match(br"(?is)^\s*(?:\xef\xbb\xbf)?\s*(?:<\?xml[^?]*\?>\s*)?(?:<!--.*?-->\s*)*<(?:[A-Za-z0-9_-]+:)?(?:Error|service-error|error-response|crossref_result)\b", prefix):
        raise AccessDenied("附件地址返回错误或元数据 XML，未保存为附件。")


def download(client: wl._Client, descriptor: dict, target: Path) -> dict:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        for attempt in range(client.config.retries + 1):
            response = client.request(descriptor["request_url"], supplement=True, accept="*/*")
            try:
                with response, temporary.open("wb") as handle:
                    if "html" in response.headers.get("Content-Type", "").lower():
                        raise AccessDenied("附件地址返回 HTML 登录或错误页面。")
                    raw_length = response.headers.get("Content-Length", "")
                    expected = int(raw_length) if raw_length.isdigit() else None
                    if expected is not None and expected > wl._MAX_FILE:
                        raise ValueError("补充材料超过 150 MB。")
                    size, digest, prefix = 0, hashlib.sha256(), b""
                    for chunk in response.iter_content(128 * 1024):
                        _check_cancel(client.event)
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > wl._MAX_FILE:
                            raise ValueError("补充材料超过 150 MB。")
                        prefix = (prefix + chunk)[:4096]
                        if _looks_html(prefix):
                            raise AccessDenied("附件内容是 HTML 登录或错误页面。")
                        digest.update(chunk)
                        handle.write(chunk)
                    if expected is not None and not response.headers.get("Content-Encoding") and size != expected:
                        raise ValueError("附件传输不完整，与 Content-Length 不一致。")
                    final_url = wl._public_url(response.url or descriptor["request_url"])
                if not size:
                    raise ValueError("附件为空。")
                _validate(temporary, target.suffix.lower(), prefix, client)
                _check_cancel(client.event)
                temporary.replace(target)
                return {key: value for key, value in descriptor.items() if key != "request_url"} | {
                    "status": "success", "bytes": size, "sha256": digest.hexdigest(), "final_url": final_url}
            except wl._STREAM_ERRORS:
                if attempt == client.config.retries:
                    raise
    finally:
        temporary.unlink(missing_ok=True)
    raise ValueError("补充材料下载失败。")


def reuse(client: wl._Client, descriptor: dict, target: Path, previous: dict) -> dict | None:
    if (previous.get("asset_key") != descriptor["asset_key"] or previous.get("parent_doi") != descriptor["parent_doi"]
            or previous.get("url") != descriptor["url"] or previous.get("file") != target.name
            or previous.get("status") not in {"success", "exists"} or not previous.get("sha256") or not target.is_file()):
        return None
    size, digest = _digest(target, client.event)
    if not size or size != previous.get("bytes") or digest != previous["sha256"]:
        return None
    return {key: value for key, value in descriptor.items() if key != "request_url"} | {
        "status": "exists", "bytes": size, "sha256": digest, "final_url": wl._public_url(previous.get("final_url", ""))}
