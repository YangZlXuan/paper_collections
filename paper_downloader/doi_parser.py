"""Import DOI and ScienceDirect references without executing document content."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from html import unescape
from itertools import chain, islice
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import unquote, urlsplit


DOI_RE = re.compile(r"(?<![\w])10\.\d{4,9}/[^\s\"'，；]+", re.IGNORECASE)
DOI_URL_RE = re.compile(r"https?://(?:dx\.)?doi\.org/[^\s\"'，；]+", re.IGNORECASE)
JOINED_DOIS_RE = re.compile(r"[,;](?=(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?10\.\d{4,9}/)", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s\"'<>，；]+|(?<![\w.@/])(?:www\.)?sciencedirect\.com/[^\s\"'<>，；]+", re.IGNORECASE)
PII_RE = re.compile(r"(?:S[0-9X]{16}|B(?:[0-9X]{16}|[0-9X]{19}))", re.IGNORECASE)
HEADER_NAMES = {"doi", "itemdoi", "di", "digitalobjectidentifier", "文献doi", "论文doi"}
URL_HEADERS = {"url", "link", "articleurl", "articlelink", "sciencedirecturl", "sciencedirectlink", "fulltexturl", "doilink", "链接", "论文链接", "网址"}
PII_HEADERS = {"pii", "articlepii", "publisheritemidentifier", "publisheritemid"}
TITLE_HEADERS = {"title", "articletitle", "documenttitle", "ti", "标题", "论文标题", "文献标题"}
JOURNAL_HEADERS = {"journal", "journalname", "journaltitle", "publicationname", "sourcetitle", "source", "so", "期刊", "期刊名称"}
OTHER_HEADERS = {"references", "reference", "citedreferences", "abstract", "author", "authors", "year", "publicationyear", "摘要", "参考文献", "作者"}


@dataclass
class ArticleReference:
    doi: str = ""
    pii: str = ""
    url: str = ""
    title: str = ""
    journal: str = ""

    @property
    def key(self) -> str:
        return self.doi or ("pii:" + self.pii if self.pii else self.url)


@dataclass
class ParseResult:
    dois: list[str]
    duplicates: int = 0
    invalid_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    records: list[ArticleReference] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.records:
            self.records = [ArticleReference(doi=doi) for doi in self.dois]
        elif not self.dois:
            self.dois = list(dict.fromkeys(record.doi for record in self.records if record.doi))


def _strip_wrapping(value: str) -> str:
    value = value.rstrip(".,;:，。；：!?！？”’")
    while value:
        previous = value
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}"), ("<", ">")):
            if value.endswith(closing) and value.count(closing) > value.count(opening):
                value = value[:-1].rstrip(".,;:，。；：!?")
        if previous == value:
            break
    return value


def extract_dois(text: str) -> list[str]:
    """Keep the legacy DOI extractor, including balanced legacy punctuation."""
    text = unescape(str(text or ""))
    text = DOI_URL_RE.sub(lambda match: unquote(urlsplit(match.group(0)).path.lstrip("/")), text)
    text = JOINED_DOIS_RE.sub(" ", unquote(text))
    return [doi for match in DOI_RE.finditer(text)
            if (doi := _strip_wrapping(match.group(0)).lower()).split("/", 1)[1]]


def normalize_pii(value: str) -> str:
    value = re.sub(r"^pii\s*:\s*", "", unquote(str(value or "")).strip(), flags=re.I)
    compact = re.sub(r"[\s()\-]", "", _strip_wrapping(value)).upper()
    return compact if PII_RE.fullmatch(compact) else ""


def canonical_sciencedirect_url(value: str) -> tuple[str, str]:
    """Return (PII, canonical URL) only for genuine ScienceDirect article URLs."""
    value = _strip_wrapping(unescape(str(value or "").strip()))
    if re.match(r"^(?:www\.)?sciencedirect\.com/", value, re.I):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme.lower() not in {"http", "https"} or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 80, 443}
                or not (host == "sciencedirect.com" or host.endswith(".sciencedirect.com"))):
            return "", ""
        match = re.fullmatch(r"/(?:science/)?article/(?:abs/)?pii/([^/]+)(?:/(?:pdf|pdfft))?/?", unquote(parsed.path), re.I)
        pii = normalize_pii(match.group(1)) if match else ""
        return (pii, f"https://www.sciencedirect.com/science/article/pii/{pii}") if pii else ("", "")
    except ValueError:
        return "", ""


def _doi_url(value: str) -> list[str]:
    """Accept DOI resolver URLs and Springer article URLs, not URL queries."""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.username is not None or parsed.password is not None:
            return []
        if host in {"doi.org", "dx.doi.org"}:
            return extract_dois(unquote(parsed.path))
        if any(host == domain or host.endswith("." + domain) for domain in ("springer.com", "springernature.com")):
            return extract_dois(unquote(parsed.path))
    except ValueError:
        pass
    return []


def _identifiers(value: object, kind: str = "text") -> tuple[list[str], list[tuple[str, str]]]:
    text = unescape(str(value or ""))
    dois: list[str] = []
    links: list[tuple[str, str]] = []
    cursor = 0
    for match in URL_RE.finditer(text):
        if kind != "url":
            dois.extend(extract_dois(text[cursor:match.start()]))
        url = _strip_wrapping(match.group(0))
        pii, canonical = canonical_sciencedirect_url(url)
        if pii:
            links.append((pii, canonical))
        else:
            dois.extend(_doi_url(url))
        cursor = match.end()
    remaining = URL_RE.sub(" ", text)
    if kind != "url":
        dois.extend(extract_dois(text[cursor:]))
    if kind == "pii":
        for part in re.split(r"[,;\n]+", remaining):
            if pii := normalize_pii(part):
                links.append((pii, f"https://www.sciencedirect.com/science/article/pii/{pii}"))
    elif pii := normalize_pii(remaining.strip()):
        links.append((pii, f"https://www.sciencedirect.com/science/article/pii/{pii}"))
    return dois, links


class _Collector:
    """Merge connected DOI/PII aliases, keeping the earliest record position."""
    def __init__(self, result: ParseResult):
        self.result = result
        self.records: list[ArticleReference | None] = []
        self.aliases: dict[str, int] = {}

    @staticmethod
    def keys(record: ArticleReference) -> set[str]:
        return {key for key in ("doi:" + record.doi if record.doi else "", "pii:" + record.pii if record.pii else "", "url:" + record.url if record.url else "") if key}

    def add(self, record: ArticleReference) -> None:
        keys = self.keys(record)
        matches = sorted({self.aliases[key] for key in keys if key in self.aliases})
        connected = [record, *(self.records[index] for index in matches)]
        for identifier in ("doi", "pii"):
            values = {getattr(item, identifier) for item in connected if getattr(item, identifier)}
            if len(values) > 1:
                raise ValueError("文献标识冲突：同一 DOI 或 PII 对应多个不同标识（"
                                 + ", ".join(sorted(values))
                                 + "）。请检查并修正 DOI、PII 和链接的对应关系后重新导入。")
        if not matches:
            target = len(self.records)
            self.records.append(record)
        else:
            target = matches[0]
            base = self.records[target]
            self.result.duplicates += len(matches)
            for incoming in [*(self.records[index] for index in matches[1:]), record]:
                for name in ("doi", "pii", "url", "title", "journal"):
                    if not getattr(base, name) and getattr(incoming, name):
                        setattr(base, name, getattr(incoming, name))
            merged = set(matches)
            for alias, index in list(self.aliases.items()):
                if index in merged:
                    self.aliases[alias] = target
            for index in matches[1:]:
                self.records[index] = None
        for key in keys:
            self.aliases[key] = target

    def row(self, values: Iterable[tuple[object, str]], *, title: str = "", journal: str = "", structured: bool = True) -> None:
        values = [(value, kind) for value, kind in values if value is not None and str(value).strip()]
        if not values:
            if title or journal:
                self.result.invalid_rows += 1
            return
        dois, links = [], []
        for value, kind in values:
            found_dois, found_links = _identifiers(value, kind)
            dois.extend(found_dois)
            links.extend(found_links)
        unique_dois, unique_links = list(dict.fromkeys(dois)), list(dict.fromkeys(links))
        if not unique_dois and not unique_links:
            self.result.invalid_rows += 1
            return
        if len(unique_dois) <= 1 and len(unique_links) <= 1 and (structured or (unique_dois and unique_links)):
            pii, url = unique_links[0] if unique_links else ("", "")
            self.add(ArticleReference(unique_dois[0] if unique_dois else "", pii, url, title, journal))
        else:
            if structured and unique_dois and unique_links:
                self.result.warnings.append("有一条记录包含多个 DOI 或 PII，已分别导入，未猜测它们之间的对应关系。")
            for doi in unique_dois if structured else dois:
                self.add(ArticleReference(doi=doi, title=title, journal=journal))
            for pii, url in unique_links if structured else links:
                self.add(ArticleReference(pii=pii, url=url, title=title, journal=journal))

    def finish(self) -> ParseResult:
        self.result.records = [record for record in self.records if record is not None]
        self.result.dois = list(dict.fromkeys(record.doi for record in self.result.records if record.doi))
        if not self.result.records:
            self.result.warnings.append("未找到有效 DOI、PII 或 ScienceDirect 文章链接。")
        self.result.warnings = list(dict.fromkeys(self.result.warnings))
        return self.result


def parse_reference_text(text: str) -> ParseResult:
    collector = _Collector(ParseResult([]))
    for line in str(text or "").splitlines():
        if line.strip():
            collector.row([(line, "text")], structured=False)
    return collector.finish()


def _header(value: object) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip()).casefold()


def _read_text(path: Path) -> tuple[str, str | None]:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), None
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        try:
            return raw.decode("gb18030"), "文件按 GB18030（兼容 GBK）编码读取。"
        except UnicodeDecodeError as exc:
            raise ValueError("无法识别文件编码，请另存为 UTF-8 文本或 CSV 后重试。") from exc


def _table(rows: Iterable[Sequence[object]], column: str | None, collector: _Collector, label: str, *, strict: bool) -> bool:
    iterator = iter(rows)
    prefix = list(islice(iterator, 25))
    selected = None
    metadata_header = None
    for position, row in enumerate(prefix):
        headers = [_header(value) for value in row]
        if column:
            indices = [(index, "text") for index, name in enumerate(headers) if name == _header(column)]
            detected = bool(indices)
        else:
            indices = [(index, "doi" if name in HEADER_NAMES else "pii" if name in PII_HEADERS else "url")
                       for index, name in enumerate(headers) if name in HEADER_NAMES | URL_HEADERS | PII_HEADERS]
            detected = bool(indices)
            if not detected and metadata_header is None and any(name in TITLE_HEADERS | JOURNAL_HEADERS | OTHER_HEADERS for name in headers):
                metadata_header = (position, indices, headers)
        if detected:
            selected = (position, indices, headers)
            break
    if selected is None and not column:
        selected = metadata_header
    if selected is not None:
        position, indices, headers = selected
        title_index = next((i for i, name in enumerate(headers) if name in TITLE_HEADERS), None)
        journal_index = next((i for i, name in enumerate(headers) if name in JOURNAL_HEADERS), None)
        if not indices:
            collector.result.warnings.append(f"{label}未找到 DOI、URL 或 PII 列，未扫描标题、摘要或参考文献。")
        for row in chain(prefix[position + 1:], iterator):
            if not any(value is not None and str(value).strip() for value in row):
                continue
            cell = lambda index: str(row[index]).strip() if index is not None and index < len(row) and row[index] is not None else ""
            values = [(cell(index), kind) for index, kind in indices]
            if not any(value for value, _ in values):
                collector.result.invalid_rows += 1
            else:
                collector.row(values, title=cell(title_index), journal=cell(journal_index))
        return True
    if column:
        if strict:
            raise ValueError(f"{label}找不到 DOI 列：{column}。请检查列名。")
        if any(any(value is not None for value in row) for row in prefix):
            collector.result.warnings.append(f"{label}未找到指定列“{column}”，已跳过。")
        return False
    if any(any(value is not None for value in row) for row in prefix):
        collector.result.warnings.append(f"{label}未识别到标识列表头，已按无表头列表扫描单元格。")
    for row in chain(prefix, iterator):
        collector.row([(value, "text") for value in row], structured=False)
    return True


def _citation(fields: dict[str, list[str]], collector: _Collector, *, ris: bool) -> None:
    identifiers = (("DO", "doi"), ("UR", "url"), ("L1", "url"), ("PI", "pii"), ("PII", "pii")) if ris else (("doi", "doi"), ("url", "url"), ("pii", "pii"))
    values = [(re.sub(r"\s*[\r\n]+\s*", "", value), kind) for key, kind in identifiers for value in fields.get(key, [])]
    first = lambda keys: next((re.sub(r"\s+", " ", " ".join(fields[key])).strip() for key in keys if fields.get(key)), "")
    title = first(("TI", "T1", "CT") if ris else ("title",))
    journal = first(("JF", "JO", "T2", "JA") if ris else ("journal", "journaltitle"))
    if any(str(value).strip() for value, _ in values):
        collector.row(values, title=title, journal=journal)
    else:
        collector.result.invalid_rows += 1


def _ris(text: str, collector: _Collector) -> None:
    fields: dict[str, list[str]] = {}
    last = ""
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9]{2,3})\s{0,2}-\s?(.*)$", line)
        if match:
            key, value = match.group(1).upper(), match.group(2).strip()
            if key == "TY" and fields:
                _citation(fields, collector, ris=True)
                fields = {}
            if key == "ER":
                if fields:
                    _citation(fields, collector, ris=True)
                fields, last = {}, ""
            else:
                fields.setdefault(key, []).append(value)
                last = key
        elif line.strip() and last:
            separator = "" if last in {"DO", "UR", "L1", "PI", "PII"} else " "
            fields[last][-1] += separator + line.strip()
    if fields:
        _citation(fields, collector, ris=True)


def _bibtex(text: str, collector: _Collector) -> None:
    import bibtexparser
    from bibtexparser.bparser import BibTexParser

    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    parser.homogenize_fields = False
    expected = _bibtex_entry_count(text)
    try:
        database = bibtexparser.loads(text, parser=parser)
    except Exception as exc:
        raise ValueError("无法解析 BibTeX，请检查花括号、引号和字段格式。") from exc
    if len(database.entries) != expected:
        raise ValueError("BibTeX 中有未能解析的文献条目，请检查字段分隔符、花括号和引号。未导入不完整文件。")
    for entry in database.entries:
        _citation({key.lower(): [str(value)] for key, value in entry.items()}, collector, ris=False)


def _bibtex_entry_count(text: str) -> int:
    """Catch malformed entries the library would otherwise treat as comments."""
    count, position = 0, 0
    while position < len(text):
        if text[position] == "%":
            end = text.find("\n", position)
            position = len(text) if end < 0 else end + 1
            continue
        declaration = re.match(r"@\s*([A-Za-z]+)\s*([{(])", text[position:]) if text[position] == "@" else None
        if not declaration:
            position += 1
            continue
        kind, opening = declaration.group(1).casefold(), declaration.group(2)
        position += declaration.end()
        depth = 1 if opening == "{" else 0
        base = depth
        quoted = False
        while position < len(text):
            char = text[position]
            if char == "\\":
                position += 2
                continue
            if char == '"' and depth == base:
                quoted = not quoted
            elif not quoted:
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if opening == "{" and depth == 0:
                        position += 1
                        break
                    if depth < 0:
                        raise ValueError("BibTeX 花括号不匹配，请修正后重新导入。")
                elif char == ")" and opening == "(" and depth == 0:
                    position += 1
                    break
            position += 1
        else:
            raise ValueError("BibTeX 条目缺少闭合花括号或引号，请修正后重新导入。")
        if kind not in {"comment", "string", "preamble"}:
            count += 1
    return count


def parse_doi_file(path: Path, column: str | None = None) -> ParseResult:
    """Import references; legacy ``dois`` contains real DOI values only.

    ``invalid_rows`` counts a missing identifier once per nonempty table row,
    text line or citation record. A custom column applies to tabular inputs only.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".xls":
        raise ValueError("暂不支持旧版 .xls，请用 Excel 另存为 .xlsx 或 CSV。")
    if suffix not in {".txt", ".csv", ".tsv", ".xlsx", ".ris", ".bib", ".bibtex"}:
        raise ValueError("请选择 TXT、CSV、TSV、XLSX、RIS 或 BibTeX 格式的文献文件。")
    column = column.strip() if column and column.strip() else None
    collector = _Collector(ParseResult([]))
    if suffix == ".xlsx":
        import openpyxl

        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_links=False)
        try:
            matching_sheets = 0
            for sheet in workbook:
                matching_sheets += _table(sheet.iter_rows(values_only=True), column, collector, f"工作表“{sheet.title}”", strict=False)
            if column and not matching_sheets:
                raise ValueError(f"所有工作表均找不到 DOI 列：{column}。请检查列名。")
        finally:
            workbook.close()
    else:
        text, warning = _read_text(path)
        if warning:
            collector.result.warnings.append(warning)
        if suffix == ".txt":
            for line in text.splitlines():
                if line.strip():
                    collector.row([(line, "text")], structured=False)
        elif suffix == ".ris":
            _ris(text, collector)
        elif suffix in {".bib", ".bibtex"}:
            _bibtex(text, collector)
        else:
            delimiter = "\t" if suffix == ".tsv" else ","
            try:
                delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t").delimiter
            except csv.Error:
                pass
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
            if delimiter == ";" and DOI_RE.match(first_line.lstrip('"')):
                delimiter = ","
            _table(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter), column, collector, "文件", strict=True)
    return collector.finish()
