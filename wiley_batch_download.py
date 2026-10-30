"""Download Wiley PDFs via TDM and discover SI through Crossref metadata."""
from __future__ import annotations

import argparse
from pathlib import Path
import signal
import threading

from paper_downloader.credentials import load_wiley_credentials
from paper_downloader.doi_parser import parse_doi_file
from paper_downloader.wiley import WileyConfig, parse_wiley_interval, run_wiley_batch, validate_wiley_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wiley API 正文下载，Crossref 辅助发现和下载 SI")
    parser.add_argument("--input", type=Path, required=True, help="TXT / CSV / TSV / XLSX / RIS / BibTeX 文献文件")
    parser.add_argument("--output", type=Path, required=True, help="下载保存目录")
    parser.add_argument("--column", help="表格中的自定义 DOI 列名")
    parser.add_argument("--journal", default="", help="完整期刊名；留空处理全部 Wiley 期刊")
    parser.add_argument("--interval", default="10", help="请求间隔，10–60 秒，可输入小数")
    parser.add_argument("--no-supplements", action="store_true", help="仅下载正文，关闭 Crossref SI 查询")
    args = parser.parse_args(argv)
    try:
        config = WileyConfig(email="", output_dir=args.output, journal=args.journal.strip(),
                             interval=parse_wiley_interval(args.interval),
                             api_key=load_wiley_credentials()["api_key"],
                             include_supplements=not args.no_supplements)
        validate_wiley_config(config)
        parsed = parse_doi_file(args.input, args.column)
        records = [record for record in parsed.records if record.doi and
                   not record.doi.lower().startswith(("10.1007/", "10.1038/", "10.1016/"))]
        if not records:
            raise ValueError("文件中没有可交给 Wiley 处理的 DOI；仅有 PII 的 ScienceDirect 链接不适用。")
    except (ValueError, OSError) as exc:
        print(f"无法开始：{exc}")
        return 2
    for warning in parsed.warnings:
        print(warning)
    print(f"已导入 {len(parsed.records)} 条文献，将处理 {len(records)} 条；"
          f"请求间隔 {config.interval:g} 秒。正文通过 Wiley TDM API，"
          + ("SI 通过 Crossref 辅助查找；未检索到记录不代表没有附件。" if config.include_supplements else "SI 查询已关闭。"))
    cancel = threading.Event()
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda _signum, _frame: cancel.set())
    try:
        summary = run_wiley_batch(records, config, on_log=print, cancel_event=cancel)
    except Exception as exc:
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
