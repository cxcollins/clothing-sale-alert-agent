from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import httpx
from bs4 import BeautifulSoup, Comment

from .config import Site
from .schemas import SiteFetchResult

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
MAX_HTML_CHARS = 250_000
TIMEOUT_SECONDS = 20.0
CONCURRENCY = 5
STRIP_TAGS = ("script", "style", "svg", "iframe", "noscript", "link", "meta")
KEEP_ATTRS = {"href", "src", "alt", "title", "srcset", "data-src", "data-image"}


async def fetch_all(sites: Iterable[Site]) -> list[SiteFetchResult]:
    sem = asyncio.Semaphore(CONCURRENCY)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=headers,
    ) as client:
        tasks = [_fetch_one(client, sem, site) for site in sites]
        return await asyncio.gather(*tasks)


async def _fetch_one(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, site: Site
) -> SiteFetchResult:
    async with sem:
        url = str(site.url)
        for attempt in (1, 2):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                cleaned = clean_html(resp.text, base_url=url)
                log.info("fetched %s (%d chars cleaned)", site.name, len(cleaned))
                return SiteFetchResult(site_name=site.name, url=url, cleaned_html=cleaned)
            except httpx.HTTPError as e:
                if attempt == 2:
                    log.warning("fetch failed for %s: %s", site.name, e)
                    return SiteFetchResult(site_name=site.name, url=url, error=str(e))
                await asyncio.sleep(1.5)
        return SiteFetchResult(site_name=site.name, url=url, error="unknown")


def clean_html(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    for tag in soup.find_all(STRIP_TAGS):
        tag.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()

    for tag in soup.find_all(True):
        attrs = tag.attrs
        for name in list(attrs.keys()):
            if name not in KEEP_ATTRS:
                del attrs[name]

    base_tag = soup.find("base", href=True)
    if not base_tag:
        head = soup.find("head") or soup.new_tag("head")
        new_base = soup.new_tag("base", href=base_url)
        head.insert(0, new_base)
        if not soup.find("head"):
            soup.insert(0, head)

    text = str(soup)
    if len(text) > MAX_HTML_CHARS:
        text = text[:MAX_HTML_CHARS]
    return text
