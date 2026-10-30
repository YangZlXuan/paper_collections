"""Rebuild the bundled catalogue from the user-supplied workbook (read only).

Only numeric journal rows in Sheet1 are read; remarks and download logs are
excluded. This script does not evaluate or execute cell content.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import openpyxl


GROUP_IDS = {1: "springer", 2: "elsevier", 3: "wiley", 4: "acs", 5: "rsc", 6: "aaas"}
OTHER_IDS = {"清华大学出版社（SciOpen）": "tsinghua", "OICC Press": "oicc", "Oxford University Press（牛津大学出版社）": "oxford",
             "Maximum Academic Press": "maximum_academic", "OAE Publishing": "oae", "美国国家科学院（NAS）": "nas",
             "中国化学会（CCS Publishing）": "ccs"}


def extract(source: Path, destination: Path) -> dict:
    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True, keep_links=False)
    publishers: dict[str, dict] = {}
    group_number = None
    group_name = ""
    try:
        for row_number, row in enumerate(workbook["Sheet1"].iter_rows(values_only=True), 1):
            heading = re.match(r"^(\d+)\.\s*(.+)$", str(row[0] or ""))
            if heading:
                group_number = int(heading.group(1))
                group_name = heading.group(2).strip()
                if group_number in GROUP_IDS:
                    identifier = GROUP_IDS[group_number]
                    publishers[identifier] = {"id": identifier, "name": group_name, "enabled": identifier in {"springer", "elsevier", "wiley"}, "journals": []}
            elif isinstance(row[0], (int, float)) and row[1]:
                if group_number in GROUP_IDS:
                    identifier = GROUP_IDS[group_number]
                elif group_number == 7:
                    name = str(row[2]).strip()
                    identifier = OTHER_IDS[name]
                    publishers.setdefault(identifier, {"id": identifier, "name": name, "enabled": False, "journals": []})
                else:
                    raise ValueError(f"Journal row {row_number} has no publisher section")
                publishers[identifier]["journals"].append(str(row[1]).strip())
    finally:
        workbook.close()
    # Bundled data contains only the catalogue, never private workbook provenance.
    document = {"schema_version": 1, "publishers": list(publishers.values())}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return document


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "paper_downloader/data/journals.json")
    args = parser.parse_args()
    data = extract(args.source, args.output)
    print(json.dumps({item["name"]: len(item["journals"]) for item in data["publishers"]}, ensure_ascii=False))
