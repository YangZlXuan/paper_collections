import json
import tempfile
import unittest
from pathlib import Path

from paper_downloader.catalog import load_catalog, normalize_journal


class CatalogTests(unittest.TestCase):
    def test_catalog_has_exact_workbook_counts_and_supported_publishers_enabled(self):
        publishers = load_catalog()
        self.assertEqual(len(publishers), 13)
        self.assertEqual(sum(len(p.journals) for p in publishers), 108)
        self.assertEqual([p.id for p in publishers if p.enabled], ["springer", "elsevier", "wiley"])
        self.assertEqual(len(next(p for p in publishers if p.id == "elsevier").journals), 41)
        self.assertEqual(len(next(p for p in publishers if p.id == "wiley").journals), 17)
        springer = next(p for p in publishers if p.id == "springer")
        self.assertEqual(len(springer.journals), 15)
        self.assertIn("Nature Communications", springer.journals)
        self.assertIn("Nano Research", springer.journals)
        self.assertIn("Science China Materials", springer.journals)

    def test_other_publisher_mapping_is_not_lost(self):
        publishers = {p.id: p for p in load_catalog()}
        self.assertEqual(publishers["tsinghua"].journals, ("Nano Research Energy",))
        self.assertEqual(publishers["oxford"].journals, ("National Science Review",))

    def test_title_comparison_normalizes_punctuation_and_width(self):
        self.assertEqual(normalize_journal("Science China: Materials"), normalize_journal("SCIENCE CHINA Materials"))
        self.assertEqual(normalize_journal("Ｎａｔｕｒｅ"), "nature")
        self.assertNotEqual(normalize_journal("Nano Research"), normalize_journal("Nano Research Energy"))

    def test_duplicate_publisher_identifier_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps({"publishers": [{"id": "same", "name": "One", "journals": []}, {"id": "same", "name": "Two", "journals": []}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_catalog(path)


if __name__ == "__main__":
    unittest.main()
