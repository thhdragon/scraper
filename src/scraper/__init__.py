"""Scrapper - Telegram and Telegra.ph media scraper."""

from scraper.cli import main
from scraper.config import ScraperConfig
from scraper.engine import MediaScraper
from scraper.storage import Storage

__all__ = [
    "MediaScraper",
    "ScraperConfig",
    "Storage",
    "main",
]
