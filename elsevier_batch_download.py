"""Batch-download Elsevier PDFs and supplements with the desktop application's engine.

The API key is read from os.environ via ElsevierKey, with ELSEVIER_API_KEY
and the desktop's encrypted per-user store retained as fallbacks. The optional
institution token uses ELSEVIER_INSTTOKEN. Never place a real key in this script
or a CLI flag.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import signal
import threading

from paper_downloader.credentials import load_elsevier_credentials
from paper_downloader.doi_parser import parse_doi_file
from paper_downloader.elsevier import ElsevierConfig, run_elsevier_batch, validate_elsevier_config
from paper_downloader.downloader import parse_request_interval


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Elsevier / ScienceDirect 正文 PDF 与补充材料批量下载")
    parser.add_argument("--input", type=Path, required=True, help="TXT / CSV / TSV / XLSX / RIS / BibTeX 文献文件")
    parser.add_argument("--output", type=Path, required=True, help="下载保存目录")
    parser.add_argument("--column", help="表格中的自定义 DOI / 链接列名")
    parser.add_argument("--journal", default="", help="完整期刊名；留空下载全部匹配文章")
    parser.add_argument("--interval", default="2", help="每次请求的间隔秒数，2–60，可输入小数")
    parser.add_argument("--no-supplements", action="store_true", help="仅下载正文 PDF，不查询或下载补充材料")
    args = parser.parse_args(argv)
    try:
        credentials = load_elsevier_credentials()
        config = ElsevierConfig(email="", output_dir=args.output, journal=args.journal.strip(),
                                interval=parse_request_interval(args.interval),
                                api_key=str(credentials.get("api_key", "")),
                                insttoken=str(credentials.get("insttoken", "")),
                                include_supplements=not args.no_supplements)
        validate_elsevier_config(config)
        parsed = parse_doi_file(args.input, args.column)
        records = [record for record in parsed.records
                   if record.pii or not record.doi.lower().startswith(("10.1007/", "10.1038/", "10.1002/", "10.1111/"))]
        if not records:
            raise ValueError("文件中没有可交给 Elsevier 处理的 DOI 或 PII，请检查输入文件及出版社。")
    except (ValueError, OSError) as exc:
        print(f"无法开始：{exc}")
        return 2
    for warning in parsed.warnings:
        print(warning)
    print(f"已导入 {len(parsed.records)} 条文献，移除 {parsed.duplicates} 条重复记录；"
          f"将处理 {len(records)} 条，跳过 {len(parsed.records) - len(records)} 条已知其他出版社记录。")
    cancel = threading.Event()
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda _signum, _frame: cancel.set())
    try:
        summary = run_elsevier_batch(records, config, on_log=print, cancel_event=cancel)
    except Exception as exc:
        # Do not print request/response objects or raw exceptions containing URLs.
        print(f"任务中断（{type(exc).__name__}），请检查保存目录中的结果清单。")
        return 1
    finally:
        signal.signal(signal.SIGINT, previous)
    print(f"结果清单：{summary.manifest_path}")
    if summary.stop_reason:
        print(f"任务暂停：{summary.stop_reason}")
        return 3
    if summary.cancelled:
        print("任务已取消；重新运行相同输入和目录可继续。")
        return 130
    failed = sum(result.status not in {"success", "exists", "filtered"} for result in summary.results)
    print(f"已处理 {len(summary.results)} 条，未完成 {failed} 条。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
