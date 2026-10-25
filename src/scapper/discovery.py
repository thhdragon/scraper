from __future__ import annotations

import asyncio
import logging
import urllib.parse
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx
from bs4 import BeautifulSoup

from scapper.constants import TELEGRAM_URL_RE
from scapper.downloader import fetch_url
from scapper.urls import (
    extract_base_slug,
    normalize_telegram_url,
    normalize_telegraph_url,
)

if TYPE_CHECKING:
    from scapper.config import ScraperConfig
    from scapper.storage import Storage

logger = logging.getLogger("scraper")


def unwrap_search_url(href: str, default_domain: str = "https://duckduckgo.com") -> str:
    """Unwrap redirect URL from search result links (DuckDuckGo, Yandex, etc.)."""
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = default_domain.rstrip("/") + href

    parsed = urllib.parse.urlparse(href)

    if parsed.query:
        qs = urllib.parse.parse_qs(parsed.query)
        # DuckDuckGo
        if "uddg" in qs:
            return qs["uddg"][0]
        # Yandex, Google, and standard redirect wrappers
        for key in ("url", "rurl", "target", "q"):
            val = qs.get(key, [None])[0]
            if val and val.startswith(("http://", "https://")):
                return val

    return href


unwrap_ddg = unwrap_search_url


async def discover_urls_duckduckgo(
    client: httpx.AsyncClient,
    keyword: str,
    delay: float = 1.0,
    fetch_fn: Callable | None = None,
) -> tuple[set[str], set[str]]:
    """Discover Telegra.ph and Telegram URLs using DuckDuckGo HTML search."""
    telegraph_urls: set[str] = set()
    telegram_urls: set[str] = set()
    do_fetch = fetch_fn or fetch_url

    queries = [
        f"site:telegra.ph {keyword}",
        f"site:graph.org {keyword}",
        f"site:t.me {keyword}",
    ]

    for query in queries:
        try:
            logger.info(f"DuckDuckGo search: {query}")

            resp = await do_fetch(
                client,
                "https://html.duckduckgo.com/html/",
                method="POST",
                data={"q": query},
            )

            if resp is None:
                continue

            if resp.status_code == 202 or (resp.text and "anomaly" in resp.text):
                logger.warning(
                    f"DuckDuckGo rate-limiting/anomaly check triggered for '{query}' (status {resp.status_code})."
                )
                continue

            if resp.status_code != 200:
                logger.warning(
                    f"DuckDuckGo search returned status {resp.status_code} for '{query}'."
                )
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            for a in soup.find_all("a", href=True):
                href_val = a.get("href")
                if not isinstance(href_val, str) or not href_val:
                    continue
                target = unwrap_ddg(href_val)

                tg = normalize_telegraph_url(target)

                if tg:
                    telegraph_urls.add(tg)
                    continue

                tel = normalize_telegram_url(target)

                if tel:
                    telegram_urls.add(tel)

        except Exception as exc:
            logger.warning(f"DuckDuckGo discovery failed for '{query}': {exc}")

        await asyncio.sleep(delay)

    return telegraph_urls, telegram_urls


async def discover_urls_yandex(
    client: httpx.AsyncClient,
    keyword: str,
    delay: float = 1.0,
    fetch_fn: Callable | None = None,
) -> tuple[set[str], set[str]]:
    """Discover Telegra.ph and Telegram URLs using Yandex search."""
    telegraph_urls: set[str] = set()
    telegram_urls: set[str] = set()
    do_fetch = fetch_fn or fetch_url

    queries = [
        f"site:telegra.ph {keyword}",
        f"site:graph.org {keyword}",
        f"site:t.me {keyword}",
    ]

    for query in queries:
        url = f"https://yandex.com/search/?text={urllib.parse.quote_plus(query)}"

        try:
            logger.info(f"Yandex search: {query}")

            resp = await do_fetch(client, url)

            if resp is None or resp.status_code != 200:
                continue

            text = resp.text.lower()

            if any(marker in text for marker in ("smartcaptcha", "captcha", "robot")):
                logger.warning("Yandex captcha/bot check triggered. Skipping.")
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            for a in soup.find_all("a", href=True):
                href_val = a.get("href")
                if not isinstance(href_val, str) or not href_val:
                    continue
                target = unwrap_search_url(href_val, default_domain="https://yandex.com")

                tg = normalize_telegraph_url(target)

                if tg:
                    telegraph_urls.add(tg)
                    continue

                tel = normalize_telegram_url(target)

                if tel:
                    telegram_urls.add(tel)

        except Exception as exc:
            logger.warning(f"Yandex discovery failed for '{query}': {exc}")

        await asyncio.sleep(delay)

    return telegraph_urls, telegram_urls


async def run_discovery(
    keywords: list[str],
    config: ScraperConfig,
    storage: Storage,
    client: httpx.AsyncClient,
    fetch_fn: Callable | None = None,
) -> tuple[set[str], set[str]]:
    """Execute search engine discovery and register found seeds into storage."""
    discovered_telegraph: set[str] = set()
    discovered_telegram: set[str] = set()

    engines = set(config.search_engines)

    for keyword in keywords:
        if "duckduckgo" in engines:
            tg, tel = await discover_urls_duckduckgo(
                client, keyword, delay=config.delay(), fetch_fn=fetch_fn
            )
            discovered_telegraph.update(tg)
            discovered_telegram.update(tel)

        if "yandex" in engines:
            tg, tel = await discover_urls_yandex(
                client, keyword, delay=config.delay(), fetch_fn=fetch_fn
            )
            discovered_telegraph.update(tg)
            discovered_telegram.update(tel)

    for url in discovered_telegraph:
        base = extract_base_slug(url)
        if base:
            storage.add_discovered_seed(base)

    for url in discovered_telegram:
        m = TELEGRAM_URL_RE.match(url)
        if m:
            storage.add_discovered_seed(m.group(1))
        else:
            storage.add_discovered_seed(url)

    return discovered_telegraph, discovered_telegram
