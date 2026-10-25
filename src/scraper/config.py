from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

from scraper.utils import _safe_float, _safe_int

logger = logging.getLogger("scraper")


@dataclass
class ScraperConfig:
    config_path: Path = Path("config.json")
    db_path: Path = Path("scraper_cache.db")
    download_dir: Path = Path("downloads")

    min_size_kb: int = 20

    # -1 means infinite depth
    max_web_depth: int = -1

    web_delay_min: float = 1.0
    web_delay_max: float = 3.0

    max_connections: int = 5

    # 0 means unlimited downloads per page
    max_downloads_per_page: int = 0

    request_timeout: float = 20.0

    probe_telegraph_dates: bool = False
    probe_date_concurrency: int = 10

    # 0 means unlimited suffix probing
    max_suffix_probes: int = 0

    discovery_enabled: bool = False
    keywords: tuple[str, ...] = ("wallpaper",)
    search_engines: tuple[str, ...] = ("duckduckgo",)

    include_discovered: bool = True
    discovered_seeds_file: Path = Path("discovered_seeds.txt")

    telegram_api: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> ScraperConfig:
        path = Path(path)
        data = {}

        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    logger.error(
                        "config.json must contain a JSON object. Using defaults."
                    )
                    data = {}
            except Exception as exc:
                logger.error(f"Error loading config file: {exc}. Using defaults.")
                data = {}

        delay_range = data.get("web_delay_range", [1.0, 3.0])
        if isinstance(delay_range, (list, tuple)) and len(delay_range) == 2:
            delay_min = _safe_float(delay_range[0], 1.0, min_val=0.0)
            delay_max = max(delay_min, _safe_float(delay_range[1], 3.0, min_val=0.0))
        else:
            delay_min, delay_max = 1.0, 3.0

        discovery = data.get("discovery")
        if not isinstance(discovery, dict):
            discovery = {}

        max_web_depth = _safe_int(data.get("max_web_depth", -1), -1)

        raw_api = data.get("telegram_api")
        telegram_api = raw_api if isinstance(raw_api, dict) else {}

        db_path_raw = data.get("db_path")
        db_path = Path(db_path_raw) if db_path_raw else Path("scraper_cache.db")

        download_dir_raw = data.get("download_dir")
        download_dir = Path(download_dir_raw) if download_dir_raw else Path("./downloads")

        discovered_file_raw = data.get("discovered_seeds_file")
        discovered_seeds_file = Path(discovered_file_raw) if discovered_file_raw else Path("discovered_seeds.txt")

        keywords_raw = discovery.get("keywords")
        if not isinstance(keywords_raw, (list, tuple)):
            keywords_raw = ["wallpaper"]

        search_engines_raw = discovery.get("search_engines")
        if not isinstance(search_engines_raw, (list, tuple)):
            search_engines_raw = ["duckduckgo"]

        return cls(
            config_path=path,
            db_path=db_path,
            download_dir=download_dir,
            min_size_kb=_safe_int(data.get("min_size_kb", 20), 20, min_val=0),
            max_web_depth=max_web_depth,
            web_delay_min=delay_min,
            web_delay_max=delay_max,
            max_connections=_safe_int(data.get("max_connections", 5), 5, min_val=1),
            max_downloads_per_page=_safe_int(
                data.get("max_downloads_per_page", 0), 0, min_val=0
            ),
            request_timeout=_safe_float(
                data.get("request_timeout", 20.0), 20.0, min_val=1.0
            ),
            probe_telegraph_dates=bool(data.get("probe_telegraph_dates", False)),
            probe_date_concurrency=_safe_int(
                data.get("probe_date_concurrency", 10), 10, min_val=1
            ),
            max_suffix_probes=_safe_int(data.get("max_suffix_probes", 0), 0, min_val=0),
            discovery_enabled=bool(discovery.get("enabled", False)),
            keywords=tuple(str(k) for k in keywords_raw),
            search_engines=tuple(str(e).lower() for e in search_engines_raw),
            include_discovered=bool(data.get("include_discovered", True)),
            discovered_seeds_file=discovered_seeds_file,
            telegram_api=telegram_api,
            raw=data,
        )

    @property
    def min_size_bytes(self) -> int:
        return self.min_size_kb * 1024

    @property
    def max_depth(self) -> float:
        return float("inf") if self.max_web_depth < 0 else float(self.max_web_depth)

    def delay(self) -> float:
        return random.uniform(self.web_delay_min, self.web_delay_max)
