"""Search publisher APIs, export references, and optionally download the results."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from pathlib import Path
import signal
import threading

from paper_downloader.credentials import (
    load_elsevier_credentials, load_springer_meta_credentials, load_wiley_credentials,
)
from paper_downloader.downloader import DownloadConfig, parse_request_interval, run_batch, validate_config
from paper_downloader.elsevier import ElsevierConfig, run_elsevier_batch, validate_elsevier_config
from paper_downloader.search import SearchConfig, SearchError, search_articles, validate_search_config
from paper_downloader.wiley import WileyConfig, parse_wiley_interval, run_wiley_batch, validate_wiley_config


def _csv_cell(value: str) -> str:
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="官方关键词检索并导出文献；添加 --download 才开始下载")
    parser.add_argument("--publisher", choices=("springer", "elsevier", "wiley"), required=True)
    parser.add_argument("--keywords", required=True, help="检索词；布尔及短语语法依出版社接口而定")
    parser.add_argument("--journal", default="", help="完整期刊名；留空检索该出版社全部期刊")
    parser.add_argument("--year-from", type=int)
    parser.add_argument("--year-to", type=int)
    parser.add_argument("--max-results", type=int, default=100, help="最多保留 1–1000 条文献，默认 100")
    parser.add_argument("--interval", help="请求间隔；默认 Springer/Elsevier 2 秒、Wiley 10 秒")
    parser.add_argument("--results", type=Path, required=True, help="导出 DOI/PII/标题/期刊 CSV 路径，不能覆盖已有文件")
    parser.add_argument("--download", action="store_true", help="检索后下载全部返回结果；默认仅导出清单")
    parser.add_argument("--output", type=Path, help="使用 --download 时指定下载目录")
    parser.add_argument("--no-supplements", action="store_true")
    parser.add_argument("--cookie-file", type=Path, help="Springer 下载使用的可选 Netscape Cookie 文件")
    args = parser.parse_args(argv)
    if args.download and args.output is None:
        parser.error("--download 必须同时指定 --output")
    try:
        interval = (parse_wiley_interval(args.interval or "10") if args.publisher == "wiley"
                    else parse_request_interval(args.interval or "2"))
        credentials = (load_springer_meta_credentials() if args.publisher == "springer" else
                       load_elsevier_credentials() if args.publisher == "elsevier" else {})
        config = SearchConfig(publisher_id=args.publisher, keywords=args.keywords.strip(),
                              journal=args.journal.strip(), year_from=args.year_from, year_to=args.year_to,
                              max_results=args.max_results, interval=interval,
                              api_key=credentials.get("api_key", ""), insttoken=credentials.get("insttoken", ""))
        validate_search_config(config)
        if args.results.exists():
            raise ValueError("检索清单已存在，请选择新的 --results 文件名。")
        download_config = None
        if args.download:
            common = {"email": "", "output_dir": args.output, "journal": config.journal,
                      "interval": interval, "include_supplements": not args.no_supplements}
            if args.publisher == "springer":
                download_config = DownloadConfig(**common, cookie_file=args.cookie_file)
                validate_config(download_config)
            elif args.publisher == "elsevier":
                download_config = ElsevierConfig(**common, api_key=config.api_key, insttoken=config.insttoken)
                validate_elsevier_config(download_config)
            else:
                download_config = WileyConfig(**common, api_key=load_wiley_credentials()["api_key"])
                validate_wiley_config(download_config)
    except (ValueError, OSError) as exc:
        print(f"无法开始：{exc}")
        return 2
    cancel = threading.Event()
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda _signum, _frame: cancel.set())
    try:
        found = search_articles(config, on_log=print, cancel_event=cancel)
        args.results.parent.mkdir(parents=True, exist_ok=True)
        with args.results.open("x", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["doi", "pii", "url", "title", "journal"])
            writer.writeheader()
            for record in found.records:
                writer.writerow({name: _csv_cell(value) for name, value in asdict(record).items()})
        print(f"来源：{found.source}；保留 {len(found.records)} 条文献。清单：{args.results}")
        for warning in found.warnings:
            print(warning)
        if found.truncated:
            print("本次结果有数量或分页限制，不代表全部命中文献。")
        if found.cancelled or cancel.is_set():
            print("检索已取消；已获得的记录已导出，未开始下载。")
            return 130
        if not args.download or not found.records:
            return 0
        runner = {"springer": run_batch, "elsevier": run_elsevier_batch, "wiley": run_wiley_batch}[args.publisher]
        records = [record.doi for record in found.records] if args.publisher == "springer" else found.records
        summary = runner(records, download_config, on_log=print, cancel_event=cancel)
        print(f"下载结果清单：{summary.manifest_path}")
        if summary.stop_reason:
            print(f"任务暂停：{summary.stop_reason}")
            return 3
        if summary.cancelled:
            return 130
        return 1 if any(result.status not in {"success", "exists", "filtered"} for result in summary.results) else 0
    except SearchError as exc:
        print(f"检索失败：{exc}")
        return 2
    except Exception as exc:
        # Publisher exceptions may contain credential-bearing URLs; do not print them.
        print(f"任务中断（{type(exc).__name__}），请检查网络、接口权限或保存目录。")
        return 1
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
