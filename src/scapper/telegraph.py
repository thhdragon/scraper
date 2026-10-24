from __future__ import annotations

import asyncio
import logging
import random
import urllib.parse
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx
from bs4 import BeautifulSoup

from scapper.constants import (
    MEDIA_EXTENSIONS,
    TELEGRAM_MENTION_RE,
    TELEGRAM_RESERVED,
    TELEGRAM_URL_RE,
    TELEGRAPH_URL_RE,
    TELEGRAPH_URL_SUFFIX_RE,
)
from scapper.downloader import extension_from_url, fetch_url, probe_url_exists
from scapper.urls import normalize_telegram_url, normalize_telegraph_url

if TYPE_CHECKING:
    from scapper.config import ScraperConfig
    from scapper.storage import Storage

logger = logging.getLogger("scraper")


async def scrape_telegraph_page(
    client: httpx.AsyncClient,
    url: str,
    fetch_fn: Callable | None = None,
) -> tuple[list[str], set[str], set[str]]:
    """
    Parse a Telegra.ph article page and extract media URLs and linked channels/articles.
    Returns (media_urls, telegram_urls, telegraph_urls).
    """
    media_urls: list[str] = []
    telegram_urls: set[str] = set()
    telegraph_urls: set[str] = set()
    do_fetch = fetch_fn or fetch_url

    try:
        resp = await do_fetch(client, url)

        if resp is None or resp.status_code != 200:
            logger.warning(f"Failed to fetch Telegraph page: {url}")
            return media_urls, telegram_urls, telegraph_urls

        soup = BeautifulSoup(resp.text, "html.parser")

        # 0. Clean scripts, styles, noscript to prevent fake handles
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        # 1. Media tags
        for tag in soup.find_all(["img", "video", "source", "audio"]):
            src = tag.get("src") or tag.get("data-src")
            if isinstance(src, str) and src:
                media_urls.append(urllib.parse.urljoin(url, src))
            poster = tag.get("poster")
            if isinstance(poster, str) and poster:
                media_urls.append(urllib.parse.urljoin(url, poster))

        # 2. Links in <a> tags (check for direct media and page links)
        for a in soup.find_all("a", href=True):
            href_val = a.get("href")
            if not isinstance(href_val, str) or not href_val:
                continue
            href = urllib.parse.urljoin(url, href_val)
            ext = extension_from_url(href)
            if ext in MEDIA_EXTENSIONS or "/file/" in href:
                media_urls.append(href)
                continue

            tg = normalize_telegraph_url(href)
            if tg and tg != url:
                telegraph_urls.add(tg)
                continue

            tel = normalize_telegram_url(href)
            if tel:
                telegram_urls.add(tel)

        # 3. Telegram mentions and URLs in article text
        article_text = soup.get_text()
        for mention in TELEGRAM_MENTION_RE.findall(article_text):
            if mention.lower() not in TELEGRAM_RESERVED:
                tel = normalize_telegram_url(f"https://t.me/{mention}")
                if tel:
                    telegram_urls.add(tel)

        for tg_match in TELEGRAM_URL_RE.finditer(article_text):
            tel = normalize_telegram_url(tg_match.group(0))
            if tel:
                telegram_urls.add(tel)

        for ph_match in TELEGRAPH_URL_RE.finditer(article_text):
            tg = normalize_telegraph_url(ph_match.group(0))
            if tg and tg != url:
                telegraph_urls.add(tg)

        # 4. Author handle in <address>
        for addr in soup.find_all("address"):
            for a in addr.find_all("a", href=True):
                a_href = a.get("href")
                if isinstance(a_href, str) and a_href:
                    author_href = urllib.parse.urljoin(url, a_href)
                    tel = normalize_telegram_url(author_href)
                    if tel:
                        telegram_urls.add(tel)
            addr_text = addr.get_text()
            for mention in TELEGRAM_MENTION_RE.findall(addr_text):
                if mention.lower() not in TELEGRAM_RESERVED:
                    tel = normalize_telegram_url(f"https://t.me/{mention}")
                    if tel:
                        telegram_urls.add(tel)

    except Exception as exc:
        logger.error(f"Error scraping Telegraph page {url}: {exc}")

    return media_urls, telegram_urls, telegraph_urls


async def probe_telegraph_suffixes(
    client: httpx.AsyncClient,
    url: str,
    max_suffix_probes: int = 0,
    probe_fn: Callable | None = None,
) -> list[str]:
    """Probe numbered suffixes for dated Telegra.ph articles (e.g. -2, -3)."""
    normalized = normalize_telegraph_url(url)
    if not normalized:
        return []

    m = TELEGRAPH_URL_SUFFIX_RE.match(normalized)
    if not m:
        return []

    # Avoid redundant suffix probing on pages that already have a suffix
    if m.group(2) is not None:
        return []

    base = m.group("base_dated")
    found: list[str] = []
    attempts = 0
    do_probe = probe_fn or probe_url_exists

    while True:
        if max_suffix_probes > 0 and attempts >= max_suffix_probes:
            break

        probe_url = f"{base}-{2 + attempts}"

        if await do_probe(client, probe_url):
            found.append(probe_url)
            attempts += 1
            await asyncio.sleep(random.uniform(0.1, 0.4))
        else:
            break

    return found


async def probe_slug_all_dates(
    client: httpx.AsyncClient,
    base_slug: str,
    config: ScraperConfig,
    storage: Storage,
    probe_fn: Callable | None = None,
) -> list[str]:
    """Probe all 366 possible month-day suffixes for a Telegra.ph base slug."""
    if not config.probe_telegraph_dates:
        return []

    if storage.has_base_slug_probed(base_slug):
        return []

    storage.mark_base_slug_probed(base_slug)

    logger.info(f"Probing all 366 date suffixes for base slug: {base_slug}")

    days_per_month = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    probe_urls = [
        f"https://telegra.ph/{base_slug}-{month:02d}-{day:02d}"
        for month, days in enumerate(days_per_month, start=1)
        for day in range(1, days + 1)
    ]

    concurrency = max(
        1, min(config.probe_date_concurrency, config.max_connections)
    )
    sem = asyncio.Semaphore(concurrency)
    found: list[str] = []
    do_probe = probe_fn or probe_url_exists

    async def probe_one(probe_url: str) -> None:
        async with sem:
            if await do_probe(client, probe_url):
                found.append(probe_url)

    await asyncio.gather(*(probe_one(u) for u in probe_urls))
    found.sort()

    return found
