"""Persistent request budgets shared by this user's GUI and CLI instances."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3
import time
from urllib.parse import urlsplit

from .settings import settings_path


SPRINGER_DAILY_LIMIT = 400
ELSEVIER_SEVEN_DAY_LIMIT = 40_000
_BEIJING = timezone(timedelta(hours=8))
_SEVEN_DAYS = 7 * 24 * 60 * 60


class RequestQuotaError(ValueError):
    """The local request budget is exhausted or cannot be safely recorded."""


@dataclass(frozen=True)
class RequestQuota:
    limit: int
    used: int
    next_recovery: datetime | None
    full_recovery: datetime | None

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def _springer_reset(now: float) -> datetime:
    today = datetime.fromtimestamp(now, _BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    return today + timedelta(days=1)


def get_request_quotas() -> dict[str, RequestQuota]:
    """Read active usage without creating a database or changing request records."""
    now = time.time()
    reset = _springer_reset(now)
    quotas = {
        "springer": RequestQuota(SPRINGER_DAILY_LIMIT, 0, reset, reset),
        "elsevier": RequestQuota(ELSEVIER_SEVEN_DAY_LIMIT, 0, None, None),
    }
    destination = settings_path().with_name("request-quota.sqlite3")
    try:
        if not destination.exists():
            return quotas
        # Read-only access avoids a GUI refresh modifying usage. A short lock
        # timeout keeps the Tk event loop responsive while another instance writes.
        with closing(sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as connection:
            rows = connection.execute("""SELECT publisher, COUNT(*), MIN(expires_at), MAX(expires_at)
                FROM api_requests WHERE expires_at > ? AND publisher IN ('springer', 'elsevier')
                GROUP BY publisher""", (now,)).fetchall()
        for publisher, used, first, last in rows:
            quotas[publisher] = RequestQuota(
                quotas[publisher].limit, used,
                datetime.fromtimestamp(first, _BEIJING), datetime.fromtimestamp(last, _BEIJING),
            )
        return quotas
    except (OSError, sqlite3.Error) as exc:
        raise RequestQuotaError(f"无法读取本地 API 请求额度：{exc}") from exc


def reserve_api_request(url: str) -> None:
    """Commit one request before sending it; failed attempts are not refunded.

    Only publisher API calls count. Article pages, CDN downloads and Wiley
    requests do not consume either budget. No API keys or URLs are stored.
    """
    parsed = urlsplit(url)
    if parsed.hostname == "api.springernature.com" and parsed.path.startswith(
        ("/meta/", "/metadata/", "/openaccess/")
    ):
        publisher, limit = "springer", SPRINGER_DAILY_LIMIT
        label = "Springer Meta / Open Access API 每日"
    elif parsed.hostname == "api.elsevier.com":
        publisher, limit = "elsevier", ELSEVIER_SEVEN_DAY_LIMIT
        label = "Elsevier API 滚动 7 天"
    else:
        return

    destination = settings_path().with_name("request-quota.sqlite3")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(destination)) as connection, connection:
            # Reserve under a write transaction so separate processes cannot
            # both take the final available request slot.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS api_requests (
                id INTEGER PRIMARY KEY,
                publisher TEXT NOT NULL,
                expires_at REAL NOT NULL
            )""")
            now = time.time()
            connection.execute("DELETE FROM api_requests WHERE expires_at <= ?", (now,))
            used, next_available = connection.execute(
                "SELECT COUNT(*), MIN(expires_at) FROM api_requests WHERE publisher = ?",
                (publisher,),
            ).fetchone()
            if used >= limit:
                resume = datetime.fromtimestamp(next_available, _BEIJING).strftime("%Y-%m-%d %H:%M:%S")
                raise RequestQuotaError(
                    f"已达到本地 {label} {limit:,} 次请求上限，已停止 API 请求。"
                    f"最早可于 {resume}（北京时间）继续；实际仍受出版社配额限制。"
                )
            if publisher == "springer":
                expires_at = _springer_reset(now).timestamp()
            else:
                expires_at = now + _SEVEN_DAYS
            connection.execute(
                "INSERT INTO api_requests (publisher, expires_at) VALUES (?, ?)",
                (publisher, expires_at),
            )
    except (OSError, sqlite3.Error) as exc:
        raise RequestQuotaError(f"无法记录本地 API 请求额度，已停止请求：{exc}") from exc
