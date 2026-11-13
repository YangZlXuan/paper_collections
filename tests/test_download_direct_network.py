"""Publisher downloads ignore environment proxies while preserving TLS checks."""
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from paper_downloader import downloader, elsevier, wiley


class DirectDownloadNetworkTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch("paper_downloader.request_quota.settings_path", return_value=Path(directory) / "settings.json"))

    def test_metadata_main_and_supplements_bypass_environment_proxies(self):
        cases = [
            (downloader, downloader.DownloadConfig("", Path(".")), [
                ("https://link.springer.com/article/10.1007/example", {}),
                ("https://link.springer.com/content/pdf/10.1007/example.pdf", {"asset": True}),
                ("https://static-content.springer-cdn.com/esm/supplement.csv", {"asset": True}),
            ]),
            (elsevier, elsevier.ElsevierConfig("", Path("."), api_key="synthetic-test-key"), [
                ("https://api.elsevier.com/content/article/doi/10.1016/example?view=META", {}),
                ("https://api.elsevier.com/content/article/doi/10.1016/example", {"pdf": True}),
                ("https://pdf.sciencedirectassets.com/paper.pdf", {"pdf": True}),
                ("https://ars.els-cdn.com/content/image/supplement.docx", {"pdf": True, "document_scoped": True}),
            ]),
            (wiley, wiley.WileyConfig("", Path("."), api_key="synthetic-test-key"), [
                ("https://api.crossref.org/works/10.1002/example", {}),
                ("https://api.wiley.com/onlinelibrary/tdm/v1/articles/10.1002/example", {"pdf": True}),
                ("https://alm.wiley.com/alm/api/v2/download/example", {"pdf": True}),
                ("https://onlinelibrary.wiley.com/doi/suppl/10.1002/example/supinfo/si.docx", {"supplement": True}),
            ]),
        ]
        proxy_env = {name: "http://unreachable-proxy.invalid:8080" for name in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")}
        proxy_env.update(NO_PROXY="", no_proxy="", SN_COOKIE_FILE="")
        for module, config, routes in cases:
            with self.subTest(publisher=config.publisher_id), patch.dict("os.environ", proxy_env), \
                    patch.object(module, "_wait"), requests.Session() as session:
                sent = []

                def reply(request, **kwargs):
                    sent.append(kwargs)
                    response = requests.Response()
                    response.status_code, response.url, response.request = 200, request.url, request
                    response.raw = io.BytesIO(b"")
                    return response

                # Keep real Session.send and environment merging; replace only I/O.
                with patch.object(session.get_adapter("https://"), "send", side_effect=reply), \
                        patch("requests.sessions.get_environ_proxies", side_effect=AssertionError("Proxy lookup must be disabled")):
                    client = module._Client(session, config, threading.Event())
                    for url, kwargs in routes:
                        client.request(url, **kwargs).close()
                self.assertEqual(len(sent), len(routes))
                self.assertTrue(all(call["proxies"] == {} for call in sent))
                self.assertTrue(all(call["verify"] is True for call in sent))


if __name__ == "__main__":
    unittest.main()
