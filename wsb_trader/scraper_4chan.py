"""Fetch threads from 4chan's /biz/ board via the public JSON API.

4chan provides a free, unauthenticated catalog endpoint — no API key required.
Rate-limit guidance from 4chan: max 1 request/second per board; our poll
interval is minutes, so we're well within limits.

Reference: https://github.com/4chan/4chan-API
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

import httpx


API_BASE = "https://a.4cdn.org"
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class BizThread:
    no: int
    subject: str
    comment: str

    @property
    def combined_text(self) -> str:
        return f"{self.subject}\n{self.comment}"


def _strip_html(raw: str) -> str:
    """Strip HTML tags and unescape entities from a 4chan comment."""
    # 4chan uses <br> for newlines and <wbr> for word-break hints; normalize to spaces.
    no_tags = _TAG_RE.sub(" ", raw)
    return html.unescape(no_tags)


class FourchanScraper:
    def __init__(
        self,
        *,
        board: str = "biz",
        min_replies: int = 5,
        client: httpx.AsyncClient | None = None,
    ):
        self.board = board
        self.min_replies = min_replies
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": "wsb-trader/0.1"},
            timeout=15.0,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self) -> list[BizThread]:
        """Return threads from the board catalog, filtered by activity."""
        url = f"{API_BASE}/{self.board}/catalog.json"
        resp = await self._client.get(url)
        resp.raise_for_status()
        return _parse_catalog(resp.json(), min_replies=self.min_replies)


def _parse_catalog(pages: list[dict], *, min_replies: int) -> list[BizThread]:
    threads: list[BizThread] = []
    for page in pages:
        for t in page.get("threads", []):
            if t.get("replies", 0) < min_replies:
                continue
            subject = _strip_html(t.get("sub", "") or "")
            comment = _strip_html(t.get("com", "") or "")
            if not subject and not comment:
                continue
            threads.append(BizThread(
                no=t["no"],
                subject=subject,
                comment=comment,
            ))
    return threads
