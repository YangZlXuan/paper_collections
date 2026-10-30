"""Offline update checks: versions, untrusted release data and credentials."""

import json
import os
import unittest
from unittest.mock import Mock, patch

import requests

from paper_downloader import updates


def release(tag="v1.9.0", **overrides):
    return {"tag_name": tag, "html_url": updates.RELEASE_PAGE_PREFIX + tag,
            "draft": False, "prerelease": False, **overrides}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.session = requests.Session()
        self.addCleanup(self.session.close)
        self.response = Mock()
        self.response.__enter__ = Mock(return_value=self.response)
        self.response.__exit__ = Mock(return_value=False)
        self.response.status_code = 200
        self.response.iter_content.side_effect = lambda size: iter([json.dumps(release()).encode()])
        self.send = self.enterContext(patch.object(self.session, "send", return_value=self.response))
        self.enterContext(patch.object(updates.requests, "Session", return_value=self.session))

    def body(self, document):
        self.response.iter_content.side_effect = lambda size: iter([json.dumps(document).encode()])

    def test_version_numeric_order_and_equal_or_older_releases(self):
        for current, tag, expected in (("1.9.0", "v1.10.0", True), ("1.8.0", "v1.8.0", False),
                                       ("1.8.0", "v1.7.0", False), ("1.8.0", "2.0.0", True)):
            with self.subTest(current=current, tag=tag):
                self.body(release(tag))
                result = updates.check_for_update(current)
                self.assertEqual(result.available, expected)
                self.assertEqual(result.version, tag.removeprefix("v"))

    def test_invalid_versions(self):
        for value in (None, 18, "1.8", "v1.8.0-rc1", "v1.08.0", "v1.8.0/evil", "1.8.0\n", "9"*100):
            with self.subTest(value=value), self.assertRaises(updates.UpdateCheckError):
                updates.parse_version(value)

    def test_no_publisher_session_netrc_or_cookie_credentials(self):
        self.session.headers.update({"Authorization": "session-secret", "Wiley-TDM-Client-Token": "wiley-secret",
                                     "X-ELS-APIKey": "elsevier-secret"})
        self.session.cookies.set("session", "cookie-secret", domain="api.github.com")
        self.session.auth = ("user", "password")
        with patch.dict(os.environ, {"WileyKey": "wiley-env", "ElsevierKey": "elsevier-env"}), \
                patch("requests.sessions.get_netrc_auth", side_effect=AssertionError("netrc must not be read")):
            result = updates.check_for_update("1.8.0")
        self.assertTrue(result.available)
        prepared = self.send.call_args.args[0]
        self.assertEqual(prepared.url, updates.LATEST_RELEASE_API)
        self.assertEqual(prepared.method, "GET")
        self.assertIsNone(prepared.body)
        self.assertEqual(set(prepared.headers), {"Accept", "X-GitHub-Api-Version", "User-Agent"})
        self.assertFalse(self.send.call_args.kwargs["allow_redirects"])
        self.assertEqual(self.send.call_args.kwargs["timeout"], (3.05, 5))

    def test_http_failure_is_not_reported_as_up_to_date(self):
        for status in (301, 302, 403, 404, 429, 500):
            with self.subTest(status=status):
                self.response.status_code = status
                with self.assertRaises(updates.UpdateCheckError):
                    updates.check_for_update("1.8.0")
        self.response.iter_content.assert_not_called()

    def test_network_exception_details_are_not_exposed(self):
        for exception in (requests.Timeout, requests.ConnectionError, requests.exceptions.SSLError):
            self.send.side_effect = exception("proxy-password-sensitive")
            with self.assertRaises(updates.UpdateCheckError) as caught:
                updates.check_for_update("1.8.0")
            self.assertNotIn("proxy-password-sensitive", str(caught.exception))

    def test_invalid_data_and_non_stable_releases_are_rejected(self):
        for value in ([], None, {}, release(draft=True), release(prerelease=True),
                      release(draft="false"), release(tag_name=None), release("v1.9.0-rc1")):
            with self.subTest(value=value), self.assertRaises(updates.UpdateCheckError):
                self.body(value)
                updates.check_for_update("1.8.0")

    def test_download_page_must_match_repository_and_tag(self):
        for url in ("https://evil.example/download", "http://github.com/YangZlXuan/paper_collections/releases/tag/v1.9.0",
                    updates.RELEASE_PAGE_PREFIX + "v2.0.0", updates.RELEASE_PAGE_PREFIX + "v1.9.0?next=evil",
                    "https://github.com/other/repo/releases/tag/v1.9.0", None):
            with self.subTest(url=url), self.assertRaises(updates.UpdateCheckError):
                self.body(release(html_url=url))
                updates.check_for_update("1.8.0")

    def test_response_size_and_deadline_are_bounded(self):
        self.response.iter_content.side_effect = lambda size: iter([b"x"*updates.MAX_RESPONSE_BYTES, b"x"])
        with self.assertRaisesRegex(updates.UpdateCheckError, "过大"):
            updates.check_for_update("1.8.0")
        with patch.object(updates.time, "monotonic", side_effect=[0, 16]), \
                self.assertRaisesRegex(updates.UpdateCheckError, "超时"):
            updates.check_for_update("1.8.0")

    def test_invalid_json(self):
        self.response.iter_content.side_effect = lambda size: iter([b"<html>error</html>"])
        with self.assertRaises(updates.UpdateCheckError):
            updates.check_for_update("1.8.0")


if __name__ == "__main__":
    unittest.main()
