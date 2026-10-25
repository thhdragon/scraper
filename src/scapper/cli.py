from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Iterable
from pathlib import Path

from scapper.config import ScraperConfig
from scapper.constants import TELEGRAM_URL_RE
from scapper.engine import MediaScraper
from scapper.urls import (
    canonicalize_seed,
    normalize_telegram_url,
    normalize_telegraph_url,
)
from scapper.utils import setup_logging

logger = logging.getLogger("scraper")


def load_seed_files(paths: Iterable[Path]) -> list[str]:
    """Load, strip, and filter non-comment seed lines from multiple file paths."""
    seeds: list[str] = []

    for path in paths:
        path = Path(path)

        if not path.exists():
            continue

        try:
            lines = path.read_text(encoding="utf-8").splitlines()

            loaded = [
                line.strip()
                for line in lines
                if line.strip() and not line.strip().startswith("#")
            ]

            seeds.extend(loaded)

            logger.info(f"Loaded {len(loaded)} seeds from {path}")
        except Exception as exc:
            logger.error(f"Error reading seed file {path}: {exc}")

    return seeds


def build_argument_parser() -> argparse.ArgumentParser:
    """Build and return command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Telegram & Telegra.ph media scraper with unlimited depth and resume support"
    )

    parser.add_argument(
        "extra_seeds",
        nargs="*",
        help="Direct URLs, usernames, or phrases to crawl.",
    )

    parser.add_argument(
        "--seeds",
        default="seeds.txt",
        type=Path,
        help="Path to seeds file.",
    )

    parser.add_argument(
        "--config",
        default="config.json",
        type=Path,
        help="Path to config file.",
    )

    parser.add_argument(
        "--db",
        type=Path,
        help="Override SQLite database path.",
    )

    parser.add_argument(
        "--download-dir",
        type=Path,
        help="Override download directory.",
    )

    parser.add_argument(
        "--no-api",
        action="store_true",
        help="Skip Telegram API mode.",
    )

    parser.add_argument(
        "--no-discovered",
        action="store_true",
        help="Do not load discovered_seeds.txt on startup.",
    )

    parser.add_argument(
        "--include-discovered",
        action="store_true",
        help="Force loading discovered_seeds.txt on startup even if disabled in config.",
    )

    parser.add_argument(
        "--min-size",
        type=int,
        help="Minimum file size in KB.",
    )

    parser.add_argument(
        "--max-depth",
        type=int,
        help="Override max crawl depth. Use -1 for infinite.",
    )

    parser.add_argument(
        "--discover",
        action="store_true",
        help="Run search-engine discovery.",
    )

    parser.add_argument(
        "--keywords",
        help="Comma-separated keywords for discovery.",
    )

    parser.add_argument(
        "--reset-queue",
        action="store_true",
        help="Clear crawl queue before starting.",
    )

    parser.add_argument(
        "--probe-dates",
        action="store_true",
        help="Enable aggressive Telegraph date probing.",
    )

    parser.add_argument(
        "--force-crawl",
        action="store_true",
        help="Re-crawl provided seed URLs even if already marked crawled in database.",
    )

    parser.add_argument(
        "--reset-crawled",
        action="store_true",
        help="Clear already crawled URLs history in database (allows re-crawling without losing downloaded media cache).",
    )

    parser.add_argument(
        "--timeout",
        type=float,
        help="Override HTTP request timeout in seconds.",
    )

    parser.add_argument(
        "--max-connections",
        type=int,
        help="Override maximum concurrent HTTP connections.",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )

    return parser


async def async_main() -> None:
    """Async CLI entry point."""
    parser = build_argument_parser()
    args = parser.parse_args()

    setup_logging(args.verbose)

    cfg = ScraperConfig.load(args.config)

    if args.db:
        cfg.db_path = args.db

    if args.download_dir:
        cfg.download_dir = args.download_dir

    if args.min_size is not None:
        cfg.min_size_kb = max(0, args.min_size)
        logger.info(f"Overridden min size filter: {cfg.min_size_kb} KB")

    if args.max_depth is not None:
        cfg.max_web_depth = args.max_depth

        if cfg.max_web_depth < 0:
            logger.info("Overridden max depth: infinite")
        else:
            logger.info(f"Overridden max depth: {cfg.max_web_depth}")

    if args.timeout is not None:
        cfg.request_timeout = max(1.0, args.timeout)
        logger.info(f"Overridden request timeout: {cfg.request_timeout}s")

    if args.max_connections is not None:
        cfg.max_connections = max(1, args.max_connections)
        logger.info(f"Overridden max connections: {cfg.max_connections}")

    if args.probe_dates:
        cfg.probe_telegraph_dates = True
        logger.info("Enabled aggressive Telegraph date probing.")

    if args.include_discovered:
        cfg.include_discovered = True

    async with MediaScraper(cfg) as scraper:
        if args.reset_crawled:
            scraper.storage.clear_crawled_urls()
            logger.info("Cleared crawled URLs history in database.")

        if args.reset_queue:
            scraper.storage.clear_queue()
            logger.info("Cleared crawl queue.")

        seed_paths = [args.seeds]

        if cfg.include_discovered and not args.no_discovered:
            seed_paths.append(cfg.discovered_seeds_file)

        seeds = load_seed_files(seed_paths)

        if args.extra_seeds:
            seeds.extend(args.extra_seeds)

        if args.discover or cfg.discovery_enabled:
            keywords = list(cfg.keywords)

            if args.keywords:
                keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

            logger.info(f"Running discovery for keywords: {keywords}")

            discovered_telegraph, discovered_telegram = await scraper.run_discovery(
                keywords
            )

            discovered = sorted(discovered_telegraph | discovered_telegram)
            seeds.extend(discovered)

            logger.info(f"Discovery added {len(discovered)} seeds.")

        # Deduplicate while preserving order using canonical representation
        unique_seeds: list[str] = []
        seen: set[str] = set()

        for seed in seeds:
            s_clean = seed.strip()
            if not s_clean or s_clean.startswith("#"):
                continue

            # Preserve distinct dated Telegra.ph articles
            norm_ph = normalize_telegraph_url(s_clean)
            if norm_ph:
                key = f"telegraph:{norm_ph.lower()}"
                if key not in seen:
                    seen.add(key)
                    unique_seeds.append(norm_ph)
                continue

            # Normalize Telegram URLs and handles
            norm_tg = normalize_telegram_url(s_clean)
            if not norm_tg and s_clean.startswith("@"):
                norm_tg = normalize_telegram_url(f"https://t.me/{s_clean.lstrip('@')}")

            if norm_tg:
                m_tg = TELEGRAM_URL_RE.match(norm_tg)
                ch_key = m_tg.group(1).lower() if m_tg else norm_tg.lower()
                key = f"telegram:{ch_key}"
            else:
                canonical = canonicalize_seed(s_clean) or s_clean.lower()
                key = f"seed:{canonical.lower()}"

            if key not in seen:
                seen.add(key)
                unique_seeds.append(s_clean)

        pending_depth = scraper.storage.get_min_pending_depth(cfg.max_depth)

        if not unique_seeds and pending_depth is None:
            if not args.seeds.exists() and Path("seeds.txt.example").exists():
                logger.warning(
                    f"Seed file '{args.seeds}' does not exist, but 'seeds.txt.example' was found. "
                    f"Copy 'seeds.txt.example' to '{args.seeds}' or provide seeds via CLI arguments."
                )
            else:
                logger.warning(
                    "No seeds found and no pending crawl queue. "
                    "Provide seeds via file, CLI args, discovery, or previous discovered seeds."
                )
            return

        if not unique_seeds and pending_depth is not None:
            logger.info(
                "No new seeds provided, but pending crawl queue found. Resuming..."
            )

        if args.force_crawl and unique_seeds:
            logger.info(
                "Force crawl enabled: resetting crawl status for provided seeds."
            )
            for seed_val in unique_seeds:
                scraper.storage.uncrawl_seed(seed_val)

        await scraper.run(unique_seeds, use_api=not args.no_api)

        total = scraper.storage.count_downloaded_hashes()

        logger.info("=" * 60)
        logger.info("Scraping run completed.")
        logger.info(f"Total unique media records in database: {total}")
        logger.info(f"Downloads directory: {scraper.cfg.download_dir.resolve()}")
        logger.info(
            f"Discovered seeds file: {scraper.cfg.discovered_seeds_file.resolve()}"
        )
        logger.info("=" * 60)


def main() -> None:
    """CLI entry point with graceful interrupt handling."""
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Scraper interrupted by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
