"""Check public GitHub releases without sharing publisher credentials."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time

import requests


REPOSITORY = "YangZlXuan/paper_collections"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_PAGE_PREFIX = f"https://github.com/{REPOSITORY}/releases/tag/"
MAX_RESPONSE_BYTES = 256 * 1024


class UpdateCheckError(Exception):
    """A safe, user-facing failure; never includes raw network error details."""


@dataclass(frozen=True)
class UpdateResult:
    version: str
    url: str
    available: bool


def parse_version(value: str) -> tuple[int, int, int]:
    """Stable releases use X.Y.Z or vX.Y.Z, compared as numbers, not text."""
    if not isinstance(value, str) or len(value) > 64:
        raise UpdateCheckError("版本号格式无效；正式版本标签应为 vX.Y.Z。")
    match = re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)
    if match is None:
        raise UpdateCheckError("版本号格式无效；正式版本标签应为 vX.Y.Z。")
    return tuple(int(part) for part in match.groups())


def check_for_update(current_version: str) -> UpdateResult:
    current = parse_version(current_version)
    deadline = time.monotonic() + 15
    try:
        with requests.Session() as session:
            # Prepare independently: Session.prepare_request may load .netrc auth.
            # Keep system proxy/CA support without inheriting account credentials.
            prepared = requests.Request("GET", LATEST_RELEASE_API, headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": f"PaperDownloader/{current_version}",
            }).prepare()
            settings = session.merge_environment_settings(prepared.url, {}, True, None, None)
            with session.send(prepared, timeout=(3.05, 5), allow_redirects=False, **settings) as response:
                if response.status_code == 404:
                    raise UpdateCheckError("尚无可访问的正式版本，请确认仓库已公开并发布正式 Release。")
                if response.status_code in (403, 429):
                    raise UpdateCheckError("GitHub 暂时限制访问，请稍后重试。")
                if response.status_code != 200:
                    raise UpdateCheckError(f"更新服务暂不可用（HTTP {response.status_code}），请稍后重试。")
                body = bytearray()
                for chunk in response.iter_content(8192):
                    if time.monotonic() > deadline:
                        raise UpdateCheckError("检查更新超时，请稍后重试。")
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise UpdateCheckError("更新信息过大，已停止读取。")
                    body.extend(chunk)
    except requests.RequestException:
        raise UpdateCheckError("无法连接更新服务，请检查网络后重试。") from None

    try:
        document = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise UpdateCheckError("更新服务返回了无效信息，请稍后重试。") from None
    if not isinstance(document, dict):
        raise UpdateCheckError("更新服务返回了无效信息，请稍后重试。")
    if document.get("draft") is not False or document.get("prerelease") is not False:
        raise UpdateCheckError("更新服务未返回正式发布版本。")
    tag = document.get("tag_name")
    latest = parse_version(tag)
    # Exact repository and safe tag: reject external, malformed or injected URLs.
    expected_url = RELEASE_PAGE_PREFIX + tag
    if document.get("html_url") != expected_url:
        raise UpdateCheckError("更新页面地址与发布仓库不匹配，已停止打开。")
    return UpdateResult(".".join(str(part) for part in latest), expected_url, latest > current)
