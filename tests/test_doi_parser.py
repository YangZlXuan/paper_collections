import tempfile
import csv
from dataclasses import asdict
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from paper_downloader.doi_parser import (ArticleReference, ParseResult, canonical_sciencedirect_url,
                                         extract_dois, normalize_pii, parse_doi_file, parse_reference_text)


def write_xlsx_fixture(path, sheets):
    """Minimal XML test fixture, unrelated to the original classification file."""
    namespace = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    content_types = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    workbook = f'<workbook xmlns="{namespace}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
    relationships = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    with zipfile.ZipFile(path, 'w') as archive:
        for index, rows in enumerate(sheets, 1):
            content_types += f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            workbook += f'<sheet name="Sheet{index}" sheetId="{index}" r:id="rId{index}"/>'
            relationships += f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
            xml = f'<worksheet xmlns="{namespace}"><sheetData>'
            for row_index, row in enumerate(rows, 1):
                xml += f'<row r="{row_index}">'
                for column, value in enumerate(row):
                    xml += f'<c r="{chr(65 + column)}{row_index}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
                xml += '</row>'
            archive.writestr(f'xl/worksheets/sheet{index}.xml', xml + '</sheetData></worksheet>')
        archive.writestr('[Content_Types].xml', content_types + '</Types>')
        archive.writestr('xl/workbook.xml', workbook + '</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', relationships + '</Relationships>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')


class ExtractTests(unittest.TestCase):
    def test_urls_encoded_paths_wrappers_and_trailing_punctuation(self):
        self.assertEqual(extract_dois('https://doi.org/10.1007%2FS12274-024-6813-0?utm_source=test [DOI:10.1038/ABC].'), ['10.1007/s12274-024-6813-0', '10.1038/abc'])

    def test_balanced_parentheses_and_legacy_semicolon_are_preserved(self):
        doi = '10.1002/(SICI)1099-0844(199912)17:4<290::AID-CBF849>3.0.CO;2-P'
        self.assertEqual(extract_dois(f'({doi}).'), [doi.lower()])
        self.assertEqual(extract_dois('10.1016/S0022-2836(05)80360-2;'), ['10.1016/s0022-2836(05)80360-2'])

    def test_multiple_dois_with_commas_semicolons_or_spaces(self):
        self.assertEqual(extract_dois('10.1007/one,10.1038/two;10.1007/three 10.1038/four'), ['10.1007/one', '10.1038/two', '10.1007/three', '10.1038/four'])

    def test_empty_and_non_doi_input(self):
        self.assertEqual(extract_dois('1234/abc no DOI 10.1007/'), [])

    def test_reference_dataclasses_preserve_legacy_constructor_and_key(self):
        parsed = ParseResult(['10.1007/one'], 2, 3, ['warning'])
        self.assertEqual(parsed.records[0].key, '10.1007/one')
        self.assertEqual(parsed.duplicates, 2)
        self.assertEqual(ArticleReference(pii='S0000000026000010').key, 'pii:S0000000026000010')
        self.assertEqual(ArticleReference(url='https://example.invalid').key, 'https://example.invalid')

    def test_sciencedirect_abstract_url_normalizes_tracking_and_pii(self):
        pii, url = canonical_sciencedirect_url('http://www.sciencedirect.com/science/article/abs/pii/s0000000026000010?via=ihub&x=1#section')
        self.assertEqual(pii, 'S0000000026000010')
        self.assertEqual(url, 'https://www.sciencedirect.com/science/article/pii/S0000000026000010')
        self.assertEqual(normalize_pii('S0167-577X(23)00079-6'), 'S0167577X23000796')

    def test_sciencedirect_rejects_search_journal_and_deceptive_hosts(self):
        tail = '/science/article/pii/S0000000026000010'
        invalid = [
            'https://sciencedirect.com.evil.invalid' + tail,
            'https://evilsciencedirect.com' + tail,
            'https://sciencedirect.com@evil.invalid' + tail,
            'https://user@sciencedirect.com' + tail,
            'https://www.sciencedirect.com:8443' + tail,
            'https://www.sciencedirect.com/search?pii=S0000000026000010',
            'https://www.sciencedirect.com/journal/pii/S0000000026000010',
            'https://www.sciencedirect.com/science/article/pii/not-a-pii',
        ]
        for url in invalid:
            with self.subTest(url=url):
                self.assertEqual(canonical_sciencedirect_url(url), ('', ''))
                self.assertFalse(parse_reference_text(url).records)

    def test_mixed_bare_and_url_dois_keep_textual_order(self):
        result = parse_reference_text('10.1007/first https://doi.org/10.1038/second 10.1016/third')
        self.assertEqual(result.dois, ['10.1007/first', '10.1038/second', '10.1016/third'])

    def test_pii_only_links_are_records_and_never_fake_dois(self):
        result = parse_reference_text('www.sciencedirect.com/science/article/pii/S0000000026000010?via=ihub\nhttps://www.sciencedirect.com/science/article/abs/pii/S0000000026000010#top')
        self.assertEqual(result.dois, [])
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.invalid_rows, 0)
        self.assertEqual(result.warnings, [])


class FileParserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def make_file(self, name, content, encoding='utf-8-sig'):
        path = self.directory / name
        path.write_text(content, encoding=encoding)
        return path

    def test_txt_dedup_counts_and_multiple_dois_per_line(self):
        path = self.make_file('dois.txt', '\n10.1007/ABC\nhttps://doi.org/10.1007/abc\ninvalid\n10.1038/one 10.1038/two\n')
        result = parse_doi_file(path)
        self.assertEqual(result.dois, ['10.1007/abc', '10.1038/one', '10.1038/two'])
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.invalid_rows, 1)

    def test_csv_uses_doi_column_instead_of_unrelated_cells(self):
        path = self.make_file('input.csv', 'Title,Item DOI,References\nArticle,10.1007/one,10.1038/unwanted\n"Article, two",https://doi.org/10.1007/two,\n')
        self.assertEqual(parse_doi_file(path).dois, ['10.1007/one', '10.1007/two'])

    def test_tab_separated_gbk_file(self):
        path = self.make_file('input.tsv', '标题\tDI\n论文\t10.1007/one\n', 'gb18030')
        result = parse_doi_file(path)
        self.assertEqual(result.dois, ['10.1007/one'])
        self.assertIn('GB18030', result.warnings[0])

    def test_custom_header_and_missing_header(self):
        path = self.make_file('input.csv', 'record,my identifier\n1,10.1007/one\n')
        self.assertEqual(parse_doi_file(path, 'my identifier').dois, ['10.1007/one'])
        with self.assertRaisesRegex(ValueError, '找不到 DOI 列'):
            parse_doi_file(path, 'missing')

    def test_headerless_csv_does_not_drop_first_doi(self):
        path = self.make_file('input.csv', '10.1007/first\n10.1038/second\n')
        result = parse_doi_file(path)
        self.assertEqual(result.dois, ['10.1007/first', '10.1038/second'])
        self.assertTrue(result.warnings)

    def test_headerless_legacy_doi_does_not_become_semicolon_delimited(self):
        path = self.make_file('legacy.csv', '10.1002/old;2-P\n10.1002/second;2-X\n')
        self.assertEqual(parse_doi_file(path).dois, ['10.1002/old;2-p', '10.1002/second;2-x'])

    def test_xlsx_reads_all_sheets_and_detects_offset_headers(self):
        path = self.directory / 'input.xlsx'
        write_xlsx_fixture(path, [[['Exported articles'], ['Title', 'DOI'], ['Article', '10.1007/one']], [['Item DOI'], ['10.1038/two'], ['10.1007/ONE']]])
        result = parse_doi_file(path)
        self.assertEqual(result.dois, ['10.1007/one', '10.1038/two'])
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.invalid_rows, 0)

    def test_xlsx_custom_column_skips_other_sheets(self):
        path = self.directory / 'input.xlsx'
        write_xlsx_fixture(path, [[['My DOI'], ['10.1007/one']], [['DOI'], ['10.1038/unwanted']]])
        result = parse_doi_file(path, 'My DOI')
        self.assertEqual(result.dois, ['10.1007/one'])
        self.assertIn('跳过', result.warnings[0])
        with self.assertRaisesRegex(ValueError, '所有工作表'):
            parse_doi_file(path, 'missing')

    def test_legacy_excel_and_unsupported_extensions_are_actionable(self):
        with self.assertRaisesRegex(ValueError, '另存为'):
            parse_doi_file(self.directory / 'old.xls')
        with self.assertRaisesRegex(ValueError, 'TXT'):
            parse_doi_file(self.directory / 'input.pdf')

    def test_tables_preserve_metadata_and_merge_later_bridge(self):
        path = self.make_file('refs.csv', 'DOI,URL,Title,Journal,References\n10.1016/first,,First title,Carbon,10.1007/unwanted\n,https://www.sciencedirect.com/science/article/pii/S0000000026000010,,,\n10.1016/first,https://www.sciencedirect.com/science/article/abs/pii/S0000000026000010?via=ihub,Later title,Carbon,\n')
        result = parse_doi_file(path)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.duplicates, 2)
        self.assertEqual(result.dois, ['10.1016/first'])
        self.assertEqual(result.records[0].pii, 'S0000000026000010')
        self.assertEqual(result.records[0].title, 'First title')
        self.assertEqual(result.records[0].journal, 'Carbon')

    def test_table_explicit_column_ignores_other_identifiers(self):
        path = self.make_file('refs.csv', 'DOI,URL,Title\n10.1007/one,https://www.sciencedirect.com/science/article/pii/S0000000026000010,Article\n')
        result = parse_doi_file(path, 'DOI')
        self.assertEqual(result.records, [ArticleReference(doi='10.1007/one', title='Article')])

    def test_table_without_identifier_header_ignores_reference_and_abstract_dois(self):
        path = self.make_file('refs.csv', 'Title,Abstract,References\nArticle,Discusses 10.1007/wrong,10.1016/also-wrong\n')
        result = parse_doi_file(path)
        self.assertEqual(result.records, [])
        self.assertEqual(result.invalid_rows, 1)

    def test_same_pii_conflicting_dois_and_same_doi_conflicting_piis_raise(self):
        for text in (
            'DOI,PII\n10.1016/one,S0000000026000010\n10.1016/two,S0000000026000010\n',
            'DOI,PII\n10.1016/one,S0000000026000010\n10.1016/one,S0000000026000022\n',
        ):
            with self.subTest(text=text):
                path = self.make_file('conflict.csv', text)
                with self.assertRaisesRegex(ValueError, '标识冲突'):
                    parse_doi_file(path)

    def test_conflicting_later_bridge_is_not_silently_merged(self):
        path = self.make_file('refs.csv', 'DOI,PII\n10.1016/one,S0000000026000010\n10.1016/two,S0000000026000022\n10.1016/one,S0000000026000022\n')
        with self.assertRaisesRegex(ValueError, '标识冲突'):
            parse_doi_file(path)

    def test_csv_export_round_trip_preserves_all_reference_fields(self):
        records = [ArticleReference('10.1016/test', 'S0000000026000010', 'https://www.sciencedirect.com/science/article/pii/S0000000026000010', 'Title, with comma', 'Carbon'), ArticleReference(pii='S0000000026000022', url='https://www.sciencedirect.com/science/article/pii/S0000000026000022', title='Other', journal='Joule')]
        path = self.directory / 'export.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=['doi', 'pii', 'url', 'title', 'journal'])
            writer.writeheader()
            writer.writerows(asdict(record) for record in records)
        self.assertEqual(parse_doi_file(path).records, records)

    def test_xlsx_mixed_identifiers_across_sheets_and_title_banner(self):
        path = self.directory / 'refs.xlsx'
        write_xlsx_fixture(path, [[['Title'], ['DOI', 'URL', 'Title', 'Journal'], ['10.1016/one', 'https://www.sciencedirect.com/science/article/pii/S0000000026000010', 'Article', 'Carbon']], [['PII'], ['S0000000026000010'], ['S0000000026000022']]])
        result = parse_doi_file(path)
        self.assertEqual([record.key for record in result.records], ['10.1016/one', 'pii:S0000000026000022'])
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.records[0].journal, 'Carbon')

    def test_ris_multiline_metadata_and_identifiers_only(self):
        text = ('TY  - JOUR\nTI  - A title mentioning 10.1007/not-an-identifier\n      continued title\nJO  - Carbon\n'
                'AB  - References 10.1007/ignore-this\nDO  - 10.1016/\n      real\n'
                'UR  - https://www.sciencedirect.com/science/article/abs/pii/\n      S0000000026000010?via=ihub\nER  -\n'
                'TY  - JOUR\nTI  - Missing identifier 10.1007/not-real\nAB  - 10.1007/no\nER  -\n')
        result = parse_doi_file(self.make_file('refs.ris', text))
        self.assertEqual(result.dois, ['10.1016/real'])
        self.assertEqual(result.records[0].pii, 'S0000000026000010')
        self.assertEqual(result.records[0].journal, 'Carbon')
        self.assertTrue(result.records[0].title.endswith('continued title'))
        self.assertEqual(result.invalid_rows, 1)

    def test_ris_pii_only_and_record_without_final_er(self):
        text = 'TY  - JOUR\nPII - S0000000026000010\nTI  - First\nER  -\nTY  - JOUR\nPI  - S0000000026000022\nJF  - Joule\n'
        result = parse_doi_file(self.make_file('refs.ris', text))
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.dois, [])
        self.assertEqual(result.records[1].journal, 'Joule')

    def test_empty_ris_identifier_counts_record_once(self):
        result = parse_doi_file(self.make_file('empty.ris', 'TY  - JOUR\nDO  -\nUR  -\nER  -\n'))
        self.assertEqual(result.invalid_rows, 1)
        self.assertEqual(result.records, [])

    def test_bibtex_wrapped_identifier_fields(self):
        text = '@article{one,doi={10.1016/\n    real},url={https://www.sciencedirect.com/science/article/pii/\n    S0000000026000010},title={A title\n    continued},journal={Carbon}}'
        result = parse_doi_file(self.make_file('wrapped.bib', text))
        self.assertEqual(result.dois, ['10.1016/real'])
        self.assertEqual(result.records[0].pii, 'S0000000026000010')
        self.assertEqual(result.records[0].title, 'A title continued')

    def test_bibtex_nested_fields_macros_and_identifier_whitelist(self):
        text = ('@string{carbon = "Carbon"}\n'
                '@comment{Example @article{not-a-record, doi={10.1007/ignored}}}\n'
                '@article{first, title={An {important} title mentioning 10.1007/not-real}, journal=carbon, '
                'doi={10.1016/real}, url={https://www.sciencedirect.com/science/article/pii/S0000000026000010?via=ihub}, abstract={See 10.1007/no}}\n'
                '@article{second, title="No identifier 10.1007/ignore", journal="Joule"}\n'
                '@article(third, title={PII only}, pii={S0000000026000022})')
        result = parse_doi_file(self.make_file('refs.bibtex', text))
        self.assertEqual(result.dois, ['10.1016/real'])
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.records[0].journal, 'Carbon')
        self.assertIn('{important}', result.records[0].title)
        self.assertEqual(result.records[1].pii, 'S0000000026000022')
        self.assertEqual(result.invalid_rows, 1)

    def test_bibtex_rejects_partial_or_malformed_entries(self):
        for text in (
            '@article{good,doi={10.1007/first}}\n@article{bad,doi={10.1007/second},title={Broken}',
            '@article{bad doi={10.1007/second}}',
            '@article{bad, title="Unclosed}',
        ):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, 'BibTeX'):
                    parse_doi_file(self.make_file('bad.bib', text))


if __name__ == '__main__':
    unittest.main()
