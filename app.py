"""Source and PyInstaller entry point."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import tempfile


def self_test(destination: Path) -> None:
    """Offline verification of dependencies and bundled catalog in a built EXE."""
    import tkinter as tk
    import certifi
    import bs4
    import bibtexparser
    import openpyxl
    import requests
    from paper_downloader import __version__
    from paper_downloader.catalog import load_catalog
    from paper_downloader.doi_parser import extract_dois, parse_doi_file, parse_reference_text
    from paper_downloader.downloader import DownloadConfig, MIN_REQUEST_INTERVAL, article_url
    from paper_downloader.elsevier import ElsevierConfig, validate_elsevier_config
    from paper_downloader.wiley import WileyConfig, validate_wiley_config, MIN_WILEY_INTERVAL
    from paper_downloader.wiley_supplements import parse_components
    from paper_downloader.credentials import _crypt
    from paper_downloader.updates import LATEST_RELEASE_API, parse_version
    from paper_downloader.gui import DownloaderApp

    parse_version(__version__)
    assert parse_version("v1.10.0") > parse_version("v1.9.0")
    catalog = load_catalog()
    assert catalog and any(p.id == "springer" and p.enabled for p in catalog)
    assert any(p.id == "elsevier" and p.enabled for p in catalog)
    assert any(p.id == "wiley" and p.enabled for p in catalog)
    wiley_config = WileyConfig(email="", output_dir=destination.parent, api_key="00000000-0000-4000-8000-000000000001")
    validate_wiley_config(wiley_config)
    assert wiley_config.interval == MIN_WILEY_INTERVAL == 10.0 and wiley_config.include_supplements
    assert wiley_config.api_key not in repr(wiley_config)
    assert parse_components(
        b"<crossref><journal><journal_article><doi_data><doi>10.1002/test-import</doi>"
        b"</doi_data></journal_article></journal></crossref>", "10.1002/test-import"
    ) == []
    elsevier_config = ElsevierConfig(email="", output_dir=destination.parent, api_key="0" * 32)
    validate_elsevier_config(elsevier_config)
    assert "0" * 32 not in repr(elsevier_config)
    assert elsevier_config.interval == 2.0 and elsevier_config.include_supplements
    assert _crypt(_crypt(b"offline-credential-self-test", decrypt=False), decrypt=True) == b"offline-credential-self-test"
    assert extract_dois("https://doi.org/10.1007/s12274-024-6813-0") == ["10.1007/s12274-024-6813-0"]
    sample_pii = "S0000000026000010"
    sample_url = f"https://www.sciencedirect.com/science/article/pii/{sample_pii}"
    parsed_link = parse_reference_text(sample_url + "?via=ihub")
    assert not parsed_link.dois and parsed_link.records[0].pii == sample_pii
    assert parsed_link.records[0].url == sample_url
    with tempfile.TemporaryDirectory(prefix="paper-import-check-") as temporary:
        for suffix, content in (
            (".ris", f"TY  - JOUR\nTI  - Import self-test\nDO  - 10.1007/test-import\nUR  - {sample_url}\nER  -\n"),
            (".bib", f"@article{{check, title={{Import self-test}}, doi={{10.1007/test-import}}, url={{{sample_url}}}}}"),
        ):
            sample = Path(temporary) / ("import" + suffix)
            sample.write_text(content, encoding="utf-8")
            parsed = parse_doi_file(sample)
            assert parsed.dois == ["10.1007/test-import"]
            assert len(parsed.records) == 1 and parsed.records[0].pii == sample_pii
    assert Path(certifi.where()).is_file()
    config = DownloadConfig(email="", output_dir=destination.parent)
    assert config.include_supplements and config.interval == MIN_REQUEST_INTERVAL == 2.0
    assert article_url("10.1038/s41586-021-03819-2") == "https://www.nature.com/articles/s41586-021-03819-2"
    app = DownloaderApp(check_updates=False)
    app.withdraw()
    app.update_idletasks()
    app.update()
    tcl_version = app.tk.call("info", "patchlevel")
    app.destroy()
    payload = {
        "ok": True,
        "version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "tk": str(tcl_version),
        "publishers": len(catalog),
        "journals": sum(len(p.journals) for p in catalog),
        "springer_journals": len(next(p.journals for p in catalog if p.id == "springer")),
        "elsevier_journals": len(next(p.journals for p in catalog if p.id == "elsevier")),
        "wiley_journals": len(next(p.journals for p in catalog if p.id == "wiley")),
        "requests": requests.__version__,
        "openpyxl": openpyxl.__version__,
        "beautifulsoup4": bs4.__version__,
        "bibtexparser": bibtexparser.__version__,
        "import_formats": ["txt", "csv", "tsv", "xlsx", "ris", "bib", "bibtex"],
        "sciencedirect_link_parsing": True,
        "download_mode": "publisher_pages_elsevier_api_wiley_tdm",
        "wiley_pdf_supported": True,
        "wiley_tdm_supplements_supported": False,
        "wiley_supplements_source": "Crossref metadata",
        "wiley_crossref_parser": True,
        "wiley_supplements_default": wiley_config.include_supplements,
        "wiley_minimum_request_interval_seconds": MIN_WILEY_INTERVAL,
        "wiley_credential_environment": "WileyKey",
        "elsevier_supplements_supported": True,
        "elsevier_supplements_default": elsevier_config.include_supplements,
        "update_check_api": LATEST_RELEASE_API,
        "automatic_update_check_in_self_test": False,
        "credential_protection": "windows-dpapi-current-user",
        "supplements_default": config.include_supplements,
        "default_request_interval_seconds": config.interval,
        "minimum_request_interval_seconds": MIN_REQUEST_INTERVAL,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="论文批量下载器")
    parser.add_argument("--self-test", type=Path, metavar="JSON", help="离线检查打包资源，并写入 JSON 结果")
    args = parser.parse_args()
    if args.self_test:
        self_test(args.self_test)
        return
    from paper_downloader.gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_dir = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "PaperDownloader"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "crash.log"
        logging.basicConfig(filename=log_path, encoding="utf-8", level=logging.ERROR)
        logging.exception("Application failed")
        if "--self-test" not in sys.argv:
            from tkinter import messagebox
            messagebox.showerror("启动失败", f"程序发生错误，详细信息已保存：\n{log_path}")
        raise SystemExit(1)
