from __future__ import annotations

import asyncio
import logging
import typing

import httpx

from scapper.config import ScraperConfig
from scapper.constants import (
    TELEGRAM_DOMAINS,
    TELEGRAM_URL_RE,
    TELEGRAPH_DOMAINS,
)
from scapper.discovery import run_discovery
from scapper.downloader import download_many, make_client
from scapper.storage import Storage
from scapper.telegram_api import run_api_scraper
from scapper.telegram_web import scrape_telegram_web_preview
from scapper.telegraph import (
    probe_slug_all_dates,
    probe_telegraph_suffixes,
    scrape_telegraph_page,
)
from scapper.urls import (
    extract_base_slug,
    normalize_telegram_url,
    normalize_telegraph_url,
    slugify_telegram_candidates,
    slugify_telegraph_candidates,
)

logger = logging.getLogger("scraper")


class MediaScraper:
    """Crawler engine that coordinates Telegram and Telegra.ph scraping."""

    def __init__(self, config: ScraperConfig):
        self.cfg = config
        self.storage = Storage(
            path=config.db_path,
            discovered_seeds_file=config.discovered_seeds_file,
        )
        self.cfg.download_dir.mkdir(parents=True, exist_ok=True)

    async def __aenter__(self) -> typing.Self:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.storage.close()

    def make_client(self) -> httpx.AsyncClient:
        return make_client(self.cfg)

    def enqueue_url(self, url: str, url_type: str, depth: int) -> bool:
        if not url:
            return False

        if self.cfg.max_depth < float("inf") and depth > self.cfg.max_depth:
            return False

        if self.storage.has_url_crawled(url):
            return False

        added = self.storage.enqueue(url, url_type, depth)
        if added:
            logger.debug(f"Queued {url_type} URL at depth {depth}: {url}")

        return added

    def add_seed(self, seed: str = "", depth: int = 0) -> None:
        seed = seed.strip()
        if not seed or seed.startswith("#"):
            return

        # Direct Telegraph URL
        if any(dom in seed.lower() for dom in TELEGRAPH_DOMAINS):
            url = normalize_telegraph_url(seed)
            if url:
                self.enqueue_url(url, "telegraph", depth)
                base = extract_base_slug(url)
                if base:
                    for tg_cand in slugify_telegram_candidates(base):
                        self.enqueue_url(f"https://t.me/s/{tg_cand}", "telegram", depth)
            return

        # Direct Telegram URL
        if any(dom in seed.lower() for dom in TELEGRAM_DOMAINS):
            url = normalize_telegram_url(seed)
            if url:
                self.enqueue_url(url, "telegram", depth)
                m = TELEGRAM_URL_RE.match(url)
                if m:
                    username = m.group(1)
                    for ph_cand in slugify_telegraph_candidates(username):
                        self.enqueue_url(f"https://telegra.ph/{ph_cand}", "telegraph", depth)
            return

        # Raw username or phrase: Cross-pollinate both Telegram and Telegraph candidates
        raw = seed.lstrip("@").strip()
        if not raw:
            return

        for tg_slug in slugify_telegram_candidates(raw):
            self.enqueue_url(f"https://t.me/s/{tg_slug}", "telegram", depth)

        for ph_slug in slugify_telegraph_candidates(raw):
            self.enqueue_url(f"https://telegra.ph/{ph_slug}", "telegraph", depth)

    async def run_discovery(self, keywords: list[str]) -> tuple[set[str], set[str]]:
        async with self.make_client() as client:
            return await run_discovery(keywords, self.cfg, self.storage, client=client)

    async def process_telegraph_page(
        self,
        client: httpx.AsyncClient,
        url: str,
        depth: int,
    ) -> None:
        logger.info(f"Scraping Telegraph: {url}")

        media_urls, telegram_urls, telegraph_urls = await scrape_telegraph_page(
            client,
            url,
        )

        for tel_url in telegram_urls:
            self.enqueue_url(tel_url, "telegram", depth + 1)
            m = TELEGRAM_URL_RE.match(tel_url)
            if m:
                self.storage.add_discovered_seed(m.group(1))

        for tg_url in telegraph_urls:
            self.enqueue_url(tg_url, "telegraph", depth + 1)
            base = extract_base_slug(tg_url)
            if base:
                self.storage.add_discovered_seed(base)

        if media_urls:
            logger.info(f"Found {len(media_urls)} media candidates on Telegraph page.")
            await download_many(client, media_urls, self.cfg, self.storage)

        suffix_urls = await probe_telegraph_suffixes(
            client, url, max_suffix_probes=self.cfg.max_suffix_probes
        )
        for suffix_url in suffix_urls:
            self.enqueue_url(suffix_url, "telegraph", depth + 1)

        base = extract_base_slug(url)
        if base:
            for tg_cand in slugify_telegram_candidates(base):
                self.enqueue_url(f"https://t.me/s/{tg_cand}", "telegram", depth + 1)

            if self.cfg.probe_telegraph_dates:
                date_urls = await probe_slug_all_dates(
                    client, base, self.cfg, self.storage
                )
                for date_url in date_urls:
                    self.enqueue_url(date_url, "telegraph", depth + 1)

    async def process_telegram_preview(
        self,
        client: httpx.AsyncClient,
        url: str,
        depth: int,
    ) -> None:
        logger.info(f"Scraping Telegram public preview: {url}")

        (
            media_urls,
            telegraph_urls,
            discovered_telegram,
            before_url,
        ) = await scrape_telegram_web_preview(client, url)

        for tg_url in telegraph_urls:
            self.enqueue_url(tg_url, "telegraph", depth + 1)
            base = extract_base_slug(tg_url)
            if base:
                self.storage.add_discovered_seed(base)

        for tel_url in discovered_telegram:
            self.enqueue_url(tel_url, "telegram", depth + 1)
            m = TELEGRAM_URL_RE.match(tel_url)
            if m:
                self.storage.add_discovered_seed(m.group(1))

        m_self = TELEGRAM_URL_RE.match(url)
        if m_self and self.cfg.probe_telegraph_dates and "before=" not in url:
            ch_name = m_self.group(1)
            for ph_cand in slugify_telegraph_candidates(ch_name):
                date_urls = await probe_slug_all_dates(
                    client, ph_cand, self.cfg, self.storage
                )
                for date_url in date_urls:
                    self.enqueue_url(date_url, "telegraph", depth + 1)

        if media_urls:
            logger.info(
                f"Found {len(media_urls)} media candidates in Telegram preview."
            )
            await download_many(client, media_urls, self.cfg, self.storage)

        if before_url:
            normalized_before = normalize_telegram_url(before_url)
            if normalized_before and normalized_before != url:
                self.enqueue_url(normalized_before, "telegram", depth)

    async def run_web_crawler(self, seeds: list[str]) -> None:
        async with self.make_client() as client:
            for seed in seeds:
                self.add_seed(seed, depth=0)

            transient_failures: dict[str, int] = {}

            while True:
                depth = self.storage.get_min_pending_depth(self.cfg.max_depth)
                if depth is None:
                    unbounded_depth = self.storage.get_min_pending_depth()
                    if (
                        unbounded_depth is not None
                        and unbounded_depth > self.cfg.max_depth
                    ):
                        logger.info(
                            f"Reached max crawl depth ({self.cfg.max_web_depth}). "
                            "Remaining queue is preserved."
                        )
                    break

                batch = self.storage.get_pending_batch(depth, limit=200)
                if not batch:
                    self.storage.clear_queue_at_depth(depth)
                    continue

                logger.info(
                    f"--- Depth {depth} | Processing {len(batch)} queued URLs ---"
                )

                for url, url_type in batch:
                    if self.storage.has_url_crawled(url):
                        self.storage.remove_from_queue(url)
                        continue

                    try:
                        if url_type == "telegram":
                            await self.process_telegram_preview(client, url, depth)
                        else:
                            await self.process_telegraph_page(client, url, depth)

                    except asyncio.CancelledError:
                        logger.warning(
                            f"Interrupted while processing {url}. "
                            "Leaving it pending for next run."
                        )
                        raise

                    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                        attempts = transient_failures.get(url, 0) + 1
                        transient_failures[url] = attempts
                        if attempts < 3:
                            logger.warning(
                                f"Transient failure processing {url_type} URL {url} "
                                f"(attempt {attempts}/3): {exc}. Will retry."
                            )
                        else:
                            logger.error(
                                f"Max retries exceeded for {url_type} URL {url}. Dropping from queue."
                            )
                            self.storage.remove_from_queue(url)
                        await asyncio.sleep(self.cfg.delay())

                    except Exception:
                        logger.exception(f"Failed processing {url_type} URL {url}")
                        self.storage.remove_from_queue(url)
                        await asyncio.sleep(self.cfg.delay())

                    else:
                        transient_failures.pop(url, None)
                        self.storage.mark_url_crawled(url)
                        self.storage.remove_from_queue(url)
                        await asyncio.sleep(self.cfg.delay())

        logger.info("Web crawler run finished.")

    async def run(self, seeds: list[str], use_api: bool = True) -> None:
        seeds = [s.strip() for s in seeds if s and s.strip()]

        api_configured = bool(
            self.cfg.telegram_api.get("api_id")
            and self.cfg.telegram_api.get("api_hash")
        )

        if use_api and api_configured:
            api_targets = [
                s
                for s in seeds
                if not any(dom in s.lower() for dom in TELEGRAPH_DOMAINS)
            ]
            if api_targets:
                logger.info(
                    "Telegram API credentials detected. Attempting API scraper..."
                )
                api_success = await run_api_scraper(
                    api_targets,
                    self.cfg,
                    self.storage,
                    self.enqueue_url,
                )
                if not api_success:
                    logger.info(
                        "Proceeding with public web scraper fallback for all seeds."
                    )
        elif not use_api:
            logger.info(
                "Telegram API mode disabled by user (--no-api). Using public web scraping."
            )
        else:
            logger.info(
                "Telegram API credentials not configured. Falling back to public web scraping (no login required)."
            )

        logger.info("Starting web crawler engine...")
        await self.run_web_crawler(seeds)
