"""Research metadata travels with references without leaking service credentials."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paper_downloader import search
from test_search import FakeSession, springer, elsevier, srow, erow, wiley, wrow


def test_springer_metadata_reaches_reference_and_redacts_credentials():
    token = "synthetic-metadata-secret"
    config = search.SearchConfig(publisher_id="springer", keywords="acidic OER", api_key=token)
    row = {**srow(), "abstract": f"<p>Fe dissolution {token}</p>", "contentType": "Article"}
    with TemporaryDirectory() as directory, \
            patch("paper_downloader.request_quota.settings_path", return_value=Path(directory) / "settings.json"), \
            patch.object(search.requests, "Session", return_value=FakeSession([springer([row])])):
        result = search.search_articles_page(config)
    assert result.records[0].abstract == "Fe dissolution [已隐藏]"
    assert result.records[0].publication_type == "Article"


def test_elsevier_optional_abstract_and_article_type_are_preserved():
    config = search.SearchConfig(publisher_id="elsevier", keywords="acidic OER", api_key="synthetic")
    row = {**erow(), "abstract": "Measured Ir coordination", "articleType": "Research article"}
    with TemporaryDirectory() as directory, \
            patch("paper_downloader.request_quota.settings_path", return_value=Path(directory) / "settings.json"), \
            patch.object(search.requests, "Session", return_value=FakeSession([elsevier([row])])):
        result = search.search_articles_page(config)
    assert result.records[0].abstract == "Measured Ir coordination"
    assert result.records[0].publication_type == "Research article"


def test_wiley_description_and_type_are_metadata_only():
    config = search.SearchConfig(publisher_id="wiley", keywords="acidic OER")
    row = wrow(extra="<dc:description>Acid stability</dc:description><dc:type>Article</dc:type>")
    with patch.object(search.requests, "Session", return_value=FakeSession([wiley(row, total=1)])):
        result = search.search_articles_page(config)
    assert result.records[0].abstract == "Acid stability"
    assert result.records[0].publication_type == "Article"


def test_missing_or_non_string_abstract_is_not_fabricated():
    config = search.SearchConfig(publisher_id="springer", keywords="acidic OER", api_key="synthetic")
    row = {**srow(), "abstract": {"untrusted": "not a supported metadata string"}}
    with TemporaryDirectory() as directory, \
            patch("paper_downloader.request_quota.settings_path", return_value=Path(directory) / "settings.json"), \
            patch.object(search.requests, "Session", return_value=FakeSession([springer([row])])):
        result = search.search_articles_page(config)
    assert result.records[0].abstract == "" and result.records[0].publication_type == ""
