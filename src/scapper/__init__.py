"""Scapper - Telegram and Telegra.ph media scraper."""

from scapper.cli import main
from scapper.config import ScraperConfig
from scapper.engine import MediaScraper
from scapper.storage import Storage

__all__ = [
    "MediaScraper",
    "ScraperConfig",
    "Storage",
    "main",
]
