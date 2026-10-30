"""Credential tests use isolated files and synthetic values, never user secrets."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from paper_downloader import credentials


class CredentialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "credentials.json"
        self.enterContext(patch.object(credentials, "credentials_path", return_value=self.path))
        self.enterContext(patch.dict(os.environ, {"ElsevierKey": "", "ELSEVIER_API_KEY": "", "ELSEVIER_INSTTOKEN": ""}))

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
    def test_round_trip_encrypted_storage_and_clear(self):
        key, token = "synthetic-key-for-tests", "synthetic-token-for-tests"
        credentials.save_elsevier_credentials(key, token)
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(key, text)
        self.assertNotIn(token, text)
        self.assertEqual(json.loads(text)["protection"], "windows-dpapi-current-user")
        self.assertEqual(credentials.load_elsevier_credentials(),
                         {"api_key": key, "insttoken": token, "remembered": True})
        credentials.clear_elsevier_credentials()
        self.assertFalse(self.path.exists())
        self.assertEqual(credentials.load_elsevier_credentials()["api_key"], "")

    def test_environment_override_and_corrupt_store_do_not_expose_errors(self):
        self.path.write_text('{"version":1,"protection":"windows-dpapi-current-user","ciphertext":"!invalid!"}', encoding="utf-8")
        with patch.dict(os.environ, {"ELSEVIER_API_KEY": "env-key", "ELSEVIER_INSTTOKEN": "env-token"}):
            self.assertEqual(credentials.load_elsevier_credentials(),
                             {"api_key": "env-key", "insttoken": "env-token", "remembered": False})

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
    def test_elsevierkey_takes_precedence_over_legacy_name_and_saved_key(self):
        credentials.save_elsevier_credentials("saved-key", "saved-token")
        with patch.dict(os.environ, {"ElsevierKey": " requested-key ", "ELSEVIER_API_KEY": "legacy-key"}):
            self.assertEqual(credentials.load_elsevier_credentials(),
                             {"api_key": "requested-key", "insttoken": "saved-token", "remembered": True})

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
    def test_failed_encryption_leaves_previous_saved_credentials_intact(self):
        credentials.save_elsevier_credentials("original")
        with patch.object(credentials, "_crypt", side_effect=ValueError("encryption failed")):
            with self.assertRaises(ValueError):
                credentials.save_elsevier_credentials("replacement")
        self.assertEqual(credentials.load_elsevier_credentials()["api_key"], "original")

    def test_empty_save_removes_local_store(self):
        self.path.write_text("{}", encoding="utf-8")
        credentials.save_elsevier_credentials("", "")
        self.assertFalse(self.path.exists())

    def test_wiley_reads_own_environment_without_using_elsevier_store(self):
        with patch.dict(os.environ, {"WileyKey": " synthetic-wiley-token "}), \
                patch.object(credentials, "credentials_path", side_effect=AssertionError("No local store")):
            self.assertEqual(credentials.load_wiley_credentials(), {"api_key": "synthetic-wiley-token"})
        with patch.dict(os.environ, {"WileyKey": "", "ElsevierKey": "different-publisher"}):
            self.assertEqual(credentials.load_wiley_credentials(), {"api_key": ""})

    def test_springer_meta_reads_own_environment_without_creating_a_file(self):
        with patch.dict(os.environ, {"SpringerMetaKey": " synthetic-metadata-key ", "WileyKey": "other"}), \
                patch.object(credentials, "credentials_path", side_effect=AssertionError("No local store")):
            self.assertEqual(credentials.load_springer_meta_credentials(), {"api_key": "synthetic-metadata-key"})
        with patch.dict(os.environ, {"SpringerMetaKey": "", "ElsevierKey": "other"}):
            self.assertEqual(credentials.load_springer_meta_credentials(), {"api_key": ""})
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
