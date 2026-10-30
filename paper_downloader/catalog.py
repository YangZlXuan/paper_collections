"""Publisher/journal catalogue transcribed from the supplied classification workbook."""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True)
class Publisher:
    id: str
    name: str
    journals: tuple[str, ...]
    enabled: bool = False


def normalize_journal(value: str) -> str:
    """Compare full journal titles ignoring case, whitespace and punctuation."""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(char for char in text if char.isalnum())


def load_catalog(path: Path | None = None) -> list[Publisher]:
    source = Path(path) if path is not None else files("paper_downloader").joinpath("data/journals.json")
    document = json.loads(source.read_text(encoding="utf-8"))
    publishers: list[Publisher] = []
    seen: set[str] = set()
    for entry in document["publishers"]:
        identifier = entry["id"]
        if identifier in seen or not identifier:
            raise ValueError("期刊目录存在重复或空白的出版社标识")
        seen.add(identifier)
        journals = tuple(dict.fromkeys(str(name).strip() for name in entry["journals"] if str(name).strip()))
        publishers.append(Publisher(identifier, entry["name"], journals, bool(entry.get("enabled", False))))
    if not publishers:
        raise ValueError("期刊目录为空")
    return publishers
