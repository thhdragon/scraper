# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "httpx",
#   "beautifulsoup4",
#   "telethon",
# ]
# ///

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import hashlib
import json
import logging
import random
import re
import sqlite3
import sys
import typing
import urllib.parse
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger("scraper")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )



USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/opus": ".opus",
    "audio/aac": ".aac",
}

MEDIA_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".avif",
    ".heic",
    ".heif",
    ".bmp",
    ".mp4",
    ".webm",
    ".mov",
    ".mkv",
    ".avi",
    ".m4v",
    ".mp3",
    ".ogg",
    ".wav",
    ".flac",
    ".m4a",
    ".opus",
    ".aac",
}


def detect_media_type_from_bytes(data: bytes) -> tuple[str, str] | None:
    if not data:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data.startswith(b"RIFF") and len(data) >= 12:
        if data[8:12] == b"WEBP":
            return "image/webp", ".webp"
        if data[8:12] == b"WAVE":
            return "audio/wav", ".wav"
        if data[8:12] == b"AVI ":
            return "video/x-msvideo", ".avi"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        major_brand = data[8:12]
        if major_brand in {b"avif", b"avis"}:
            return "image/avif", ".avif"
        if major_brand in {b"heic", b"heix", b"hevc", b"mif1"}:
            return "image/heic", ".heic"
        if major_brand in {b"M4A ", b"M4B "}:
            return "audio/mp4", ".m4a"
        if major_brand in {b"qt  ", b"moov"}:
            return "video/quicktime", ".mov"
        return "video/mp4", ".mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        if b"matroska" in data[:64]:
            return "video/x-matroska", ".mkv"
        return "video/webm", ".webm"
    if data.startswith(b"OggS"):
        return "audio/ogg", ".ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac", ".flac"
    if data.startswith(b"ID3") or (
        len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0
    ):
        return "audio/mpeg", ".mp3"
    if data.startswith(b"BM"):
        return "image/bmp", ".bmp"
    return None


TELEGRAM_DOMAINS = {"t.me", "telegram.me", "telegram.dog", "telegram.space"}
TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
TELEGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog|telegram\.space)/(?:s/)?([A-Za-z0-9_]{3,32})(?:/(\d+))?",
    re.IGNORECASE,
)
TELEGRAM_MENTION_RE = re.compile(r"(?<!\w)@([A-Za-z0-9_]{3,32})\b")
TELEGRAPH_RESERVED = {"file", "upload", "check", "a", "edit", "api"}
TELEGRAPH_URL_RE = re.compile(
    r"https?://(?:www\.)?telegra\.ph/(?!(?:file|upload|check|a|edit|api)(?:[/?#]|$))([A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)
TELEGRAPH_SLUG_DATE_RE = re.compile(
    r"^(?P<base>.+)-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?:-(?P<page>\d+))?$"
)
TELEGRAPH_URL_SUFFIX_RE = re.compile(
    r"^(?P<base_dated>https?://(?:www\.)?telegra\.ph/.+-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))(?:-(\d+))?$"
)

TELEGRAM_RESERVED = {
    "joinchat",
    "addstickers",
    "addtheme",
    "share",
    "addemoji",
    "setlanguage",
    "proxy",
    "socks",
    "login",
    "s",
    "c",
    "m",
    "v",
    "d",
    "bg",
    "invoice",
    "boost",
    "contact",
    "about",
    "blog",
    "apps",
    "press",
    "legal",
    "privacy",
    "security",
    "faq",
    "terms",
    "jobs",
    "tour",
}


def _safe_int(val: typing.Any, default: int, min_val: int | None = None) -> int:
    try:
        res = int(val)
        if min_val is not None:
            res = max(min_val, res)
        return res
    except (TypeError, ValueError):
        return default


def _safe_float(
    val: typing.Any, default: float, min_val: float | None = None
) -> float:
    try:
        res = float(val)
        if min_val is not None:
            res = max(min_val, res)
        return res
    except (TypeError, ValueError):
        return default


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

        discovery = data.get("discovery", {})
        if not isinstance(discovery, dict):
            discovery = {}

        max_web_depth = _safe_int(data.get("max_web_depth", -1), -1)

        raw_api = data.get("telegram_api", {})
        telegram_api = raw_api if isinstance(raw_api, dict) else {}

        return cls(
            config_path=path,
            db_path=Path(data.get("db_path", "scraper_cache.db")),
            download_dir=Path(data.get("download_dir", "./downloads")),
            min_size_kb=_safe_int(data.get("min_size_kb", 20), 20, min_val=0),
            max_web_depth=max_web_depth,
            web_delay_min=delay_min,
            web_delay_max=delay_max,
            max_connections=_safe_int(data.get("max_connections", 5), 5, min_val=1),
            max_downloads_per_page=_safe_int(data.get("max_downloads_per_page", 0), 0, min_val=0),
            request_timeout=_safe_float(data.get("request_timeout", 20.0), 20.0, min_val=1.0),
            probe_telegraph_dates=bool(data.get("probe_telegraph_dates", False)),
            probe_date_concurrency=_safe_int(data.get("probe_date_concurrency", 10), 10, min_val=1),
            max_suffix_probes=_safe_int(data.get("max_suffix_probes", 0), 0, min_val=0),
            discovery_enabled=bool(discovery.get("enabled", False)),
            keywords=tuple(str(k) for k in discovery.get("keywords", ["wallpaper"])),
            search_engines=tuple(
                str(e).lower() for e in discovery.get("search_engines", ["duckduckgo"])
            ),
            include_discovered=bool(data.get("include_discovered", True)),
            discovered_seeds_file=Path(
                data.get("discovered_seeds_file", "discovered_seeds.txt")
            ),
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


class Storage:
    """SQLite persistence layer for crawl state, downloads, and discovered seeds."""

    def __init__(self, path: Path, discovered_seeds_file: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.discovered_seeds_file = Path(discovered_seeds_file)
        self.discovered_seeds_file.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()
        self.clean_discovered_seeds()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA busy_timeout = 30000;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            self._conn = conn
        return self._conn

    def __enter__(self) -> typing.Self:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None

    def _init_db(self) -> None:
        conn = self._get_conn()
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS crawled_urls (
                    url TEXT PRIMARY KEY COLLATE NOCASE,
                    crawled_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS downloaded_hashes (
                    file_hash TEXT PRIMARY KEY,
                    file_path TEXT,
                    downloaded_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS downloaded_media_urls (
                    url TEXT PRIMARY KEY COLLATE NOCASE,
                    file_hash TEXT,
                    downloaded_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS api_channel_progress (
                    channel_username TEXT PRIMARY KEY COLLATE NOCASE,
                    last_message_id INTEGER,
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS probed_base_slugs (
                    base_slug TEXT PRIMARY KEY COLLATE NOCASE,
                    probed_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS discovered_seeds (
                    seed TEXT PRIMARY KEY COLLATE NOCASE,
                    discovered_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS crawl_queue (
                    url TEXT PRIMARY KEY COLLATE NOCASE,
                    url_type TEXT NOT NULL,
                    depth INTEGER NOT NULL DEFAULT 0,
                    queued_at TEXT DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_crawl_queue_depth
                ON crawl_queue(depth, queued_at);
                """
            )

    # ---------- Crawled URLs ----------

    def has_url_crawled(self, url: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM crawled_urls WHERE url = ?",
            (url,),
        ).fetchone()
        return row is not None

    def mark_url_crawled(self, url: str) -> None:
        if not url:
            return
        conn = self._get_conn()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO crawled_urls (url) VALUES (?)",
                (url,),
            )

    def uncrawl_seed(self, seed: str) -> None:
        """Remove a seed URL or username from crawled_urls to allow re-crawling."""
        if not seed:
            return
        clean = seed.strip().lstrip("@")
        urls = [seed, clean]
        if "://" not in seed:
            urls.extend([
                f"https://t.me/s/{clean}",
                f"https://t.me/s/{clean.lower()}",
                f"https://telegra.ph/{clean}",
            ])
        conn = self._get_conn()
        with conn:
            for u in urls:
                conn.execute("DELETE FROM crawled_urls WHERE url = ?", (u,))

    # ---------- Downloaded hashes & URLs ----------

    def has_hash_downloaded(self, file_hash: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM downloaded_hashes WHERE file_hash = ?",
            (file_hash,),
        ).fetchone()
        return row is not None

    def record_downloaded_hash(self, file_hash: str, file_path: Path) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO downloaded_hashes (file_hash, file_path) VALUES (?, ?)",
                (file_hash, str(file_path)),
            )

    def count_downloaded_hashes(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) AS c FROM downloaded_hashes").fetchone()
        return int(row["c"]) if row else 0

    def has_media_url_downloaded(self, url: str) -> bool:
        if not url:
            return False
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM downloaded_media_urls WHERE url = ?",
            (url,),
        ).fetchone()
        return row is not None

    def record_downloaded_media_url(self, url: str, file_hash: str) -> None:
        if not url:
            return
        conn = self._get_conn()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO downloaded_media_urls (url, file_hash) VALUES (?, ?)",
                (url, file_hash),
            )

    # ---------- Queue ----------

    def enqueue(self, url: str, url_type: str, depth: int) -> bool:
        if not url or not url_type:
            return False
        conn = self._get_conn()
        with conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO crawl_queue (url, url_type, depth)
                VALUES (?, ?, ?)
                """,
                (url, url_type, int(depth)),
            )
            return cur.rowcount > 0

    def remove_from_queue(self, url: str) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                "DELETE FROM crawl_queue WHERE url = ?",
                (url,),
            )

    def get_min_pending_depth(self, max_depth: float = float("inf")) -> int | None:
        conn = self._get_conn()
        if max_depth < float("inf"):
            row = conn.execute(
                "SELECT MIN(depth) AS min_depth FROM crawl_queue WHERE depth <= ?",
                (int(max_depth),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT MIN(depth) AS min_depth FROM crawl_queue"
            ).fetchone()
        if row and row["min_depth"] is not None:
            return int(row["min_depth"])
        return None

    def get_pending_batch(self, depth: int, limit: int = 200) -> list[tuple[str, str]]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT url, url_type
            FROM crawl_queue
            WHERE depth = ?
            ORDER BY queued_at, rowid
            LIMIT ?
            """,
            (int(depth), int(limit)),
        ).fetchall()
        return [(row["url"], row["url_type"]) for row in rows]

    def clear_queue(self) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute("DELETE FROM crawl_queue")

    def clear_queue_at_depth(self, depth: int) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                "DELETE FROM crawl_queue WHERE depth = ?",
                (int(depth),),
            )

    # ---------- Base slug probing ----------

    def has_base_slug_probed(self, base_slug: str) -> bool:
        if not base_slug:
            return False
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM probed_base_slugs WHERE base_slug = ?",
            (base_slug.strip().lower(),),
        ).fetchone()
        return row is not None

    def mark_base_slug_probed(self, base_slug: str) -> None:
        if not base_slug:
            return
        conn = self._get_conn()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO probed_base_slugs (base_slug) VALUES (?)",
                (base_slug.strip().lower(),),
            )

    # ---------- Discovered seeds ----------

    def add_discovered_seed(self, seed: str) -> None:
        if not seed:
            return

        seed = seed.strip().lstrip("@")
        if not seed or seed.startswith("#") or len(seed) < 3:
            return

        canonical: str | None = None

        if any(dom in seed for dom in TELEGRAM_DOMAINS):
            m = TELEGRAM_URL_RE.match(seed if "://" in seed else f"https://{seed}")
            if m:
                canonical = m.group(1).lower()
        elif "telegra.ph" in seed:
            canonical = MediaScraper.extract_base_slug(seed)

        if not canonical:
            canonical = MediaScraper.extract_base_slug(seed) or seed

        canonical = canonical.strip().lstrip("@")
        if TELEGRAM_USERNAME_RE.match(canonical):
            canonical = canonical.lower()

        if (
            not canonical
            or len(canonical) < 3
            or canonical.lower() in TELEGRAM_RESERVED
            or canonical.lower() in TELEGRAPH_RESERVED
        ):
            return

        conn = self._get_conn()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO discovered_seeds (seed) VALUES (?)",
                    (canonical,),
                )
        except sqlite3.IntegrityError:
            return

        try:
            with open(self.discovered_seeds_file, "a", encoding="utf-8") as f:
                f.write(canonical + "\n")
            logger.info(f"Discovered new seed: {canonical}")
        except Exception as exc:
            logger.warning(f"Could not write discovered seed to file: {exc}")

    def get_discovered_seeds(self) -> list[str]:
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT seed FROM discovered_seeds ORDER BY discovered_at ASC").fetchall()
            return [r["seed"] for r in rows]
        except Exception:
            return []

    def clean_discovered_seeds(self) -> int:
        """Deduplicate and canonicalize discovered_seeds table and file."""
        seeds_from_db: list[str] = []
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT seed FROM discovered_seeds").fetchall()
            seeds_from_db = [r["seed"] for r in rows]
        except Exception:
            pass

        seeds_from_file: list[str] = []
        if self.discovered_seeds_file.exists():
            try:
                seeds_from_file = self.discovered_seeds_file.read_text(encoding="utf-8").splitlines()
            except Exception as exc:
                logger.warning(f"Could not read discovered seeds file for cleanup: {exc}")

        all_raw = seeds_from_file + seeds_from_db
        if not all_raw:
            return 0

        canonical_seeds: list[str] = []
        seen_lower: set[str] = set()

        for line in all_raw:
            s = line.strip().lstrip("@")
            if not s or s.startswith("#") or len(s) < 3:
                continue

            c: str | None = None
            if any(dom in s for dom in TELEGRAM_DOMAINS):
                m = TELEGRAM_URL_RE.match(s if "://" in s else f"https://{s}")
                if m:
                    c = m.group(1).lower()
            elif "telegra.ph" in s:
                c = MediaScraper.extract_base_slug(s)

            if not c:
                c = MediaScraper.extract_base_slug(s) or s

            c = c.strip().lstrip("@")
            if TELEGRAM_USERNAME_RE.match(c):
                c = c.lower()

            c_lower = c.lower()
            if (
                c
                and len(c) >= 3
                and c_lower not in TELEGRAM_RESERVED
                and c_lower not in TELEGRAPH_RESERVED
                and c_lower not in seen_lower
            ):
                seen_lower.add(c_lower)
                canonical_seeds.append(c)

        try:
            with open(self.discovered_seeds_file, "w", encoding="utf-8") as f:
                f.writelines(s_item + "\n" for s_item in canonical_seeds)
        except Exception as exc:
            logger.warning(f"Could not write cleaned discovered seeds file: {exc}")

        try:
            with conn:
                conn.execute("DELETE FROM discovered_seeds")
                conn.executemany(
                    "INSERT OR IGNORE INTO discovered_seeds (seed) VALUES (?)",
                    [(s_item,) for s_item in canonical_seeds],
                )
        except Exception as exc:
            logger.warning(f"Could not update discovered_seeds database table: {exc}")

        return len(canonical_seeds)

    # ---------- Telegram API progress ----------

    def get_channel_last_message_id(self, channel: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT last_message_id FROM api_channel_progress WHERE channel_username = ?",
            (channel.lower(),),
        ).fetchone()
        return (
            int(row["last_message_id"])
            if row and row["last_message_id"] is not None
            else 0
        )

    def update_channel_progress(self, channel: str, message_id: int) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO api_channel_progress (channel_username, last_message_id, updated_at)
                VALUES (?, ?, datetime('now'))
                """,
                (channel.lower(), int(message_id)),
            )


class MediaScraper:
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

    # ------------------ HTTP helpers ------------------

    def make_client(self) -> httpx.AsyncClient:
        limits = httpx.Limits(
            max_connections=self.cfg.max_connections,
            max_keepalive_connections=self.cfg.max_connections,
        )
        timeout = httpx.Timeout(self.cfg.request_timeout, connect=10.0)

        return httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Sec-Ch-Ua": '"Chromium";v="120", "Not?A_Brand";v="24"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Linux"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )

    def _headers(self) -> dict:
        return {}

    async def _fetch(
        self,
        client: httpx.AsyncClient,
        url: str,
        method: str = "GET",
        retries: int = 3,
        **kwargs,
    ) -> httpx.Response | None:
        last_exc: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                resp = await client.request(
                    method,
                    url,
                    **kwargs,
                )

                if resp.status_code == 429:
                    retry_after = resp.headers.get("retry-after")
                    wait_time = float(retry_after) if retry_after and retry_after.isdigit() else min(30.0, 2**attempt + random.random())
                    if attempt == retries:
                        return resp

                    await asyncio.sleep(wait_time)
                    continue

                if resp.status_code in {500, 502, 503, 504}:
                    if attempt == retries:
                        return resp

                    await asyncio.sleep(min(30.0, 2**attempt + random.random()))
                    continue

                return resp

            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc

                if attempt == retries:
                    raise

                await asyncio.sleep(min(30.0, 2**attempt + random.random()))

        if last_exc:
            raise last_exc

        return None

    async def _probe_url_exists(self, client: httpx.AsyncClient, url: str) -> bool:
        try:
            resp = await client.head(
                url,
                timeout=self.cfg.request_timeout,
                follow_redirects=True,
            )

            if resp.status_code == 429:
                retry_after = resp.headers.get("retry-after")
                wait_time = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else 1.5 + random.uniform(0.5, 2.0)
                )
                await asyncio.sleep(wait_time)
                resp = await client.head(
                    url,
                    timeout=self.cfg.request_timeout,
                    follow_redirects=True,
                )

            # Some endpoints dislike HEAD; fall back to GET if needed.
            if resp.status_code == 405:
                resp = await client.get(
                    url,
                    timeout=self.cfg.request_timeout,
                    follow_redirects=True,
                )

            return resp.status_code == 200

        except Exception:
            return False

    # ------------------ File downloads ------------------

    def _extension_from_content_type(self, content_type: str) -> str | None:
        if not content_type:
            return None

        ct = content_type.split(";")[0].strip().lower()
        return MIME_EXTENSIONS.get(ct)

    def _extension_from_url(self, url: str) -> str:
        try:
            path = urllib.parse.urlparse(url).path
            ext = Path(path).suffix.lower()

            if ext and len(ext) <= 6:
                return ext
        except Exception:
            pass

        return ""

    def _looks_like_media(self, content_type: str, ext: str) -> bool:
        content_type = (content_type or "").lower()
        ext = (ext or "").lower()

        if content_type.startswith("text/html"):
            return False

        if content_type.startswith(("image/", "video/", "audio/")):
            return True

        return ext in MEDIA_EXTENSIONS

    async def download_file(self, client: httpx.AsyncClient, url: str) -> Path | None:
        if not url or not str(url).lower().startswith(("http://", "https://")):
            return None

        # Check media URL cache before downloading
        if self.storage.has_media_url_downloaded(url):
            return None

        temp_path = self.cfg.download_dir / f"temp_{uuid.uuid4().hex}"

        try:
            async with client.stream(
                "GET",
                url,
                follow_redirects=True,
                timeout=self.cfg.request_timeout,
            ) as resp:
                if resp.status_code != 200:
                    logger.debug(f"Skipping {url}: HTTP {resp.status_code}")
                    return None

                content_type = (
                    resp.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                if content_type.startswith(("text/html", "application/json", "text/plain")):
                    self.storage.record_downloaded_media_url(url, "")
                    return None

                content_length = resp.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) < self.cfg.min_size_bytes:
                            self.storage.record_downloaded_media_url(url, "")
                            return None
                    except ValueError:
                        pass

                ext = self._extension_from_content_type(
                    content_type
                ) or self._extension_from_url(url)

                is_generic_or_file = (
                    not ext
                    or content_type in {"application/octet-stream", "binary/octet-stream", ""}
                    or "/file/" in url
                )
                if not self._looks_like_media(content_type, ext) and not is_generic_or_file:
                    self.storage.record_downloaded_media_url(url, "")
                    return None

                sha256 = hashlib.sha256()
                bytes_written = 0
                first_chunk = b""

                with temp_path.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=32768):
                        if not first_chunk:
                            first_chunk = chunk
                        fh.write(chunk)
                        sha256.update(chunk)
                        bytes_written += len(chunk)

            if bytes_written < self.cfg.min_size_bytes:
                self.storage.record_downloaded_media_url(url, "")
                return None

            if (not ext or ext == ".bin") and first_chunk:
                detected = detect_media_type_from_bytes(first_chunk)
                if detected:
                    ext = detected[1]
                elif not self._looks_like_media(content_type, ext):
                    self.storage.record_downloaded_media_url(url, "")
                    return None

            file_hash = sha256.hexdigest()

            # Record media URL as downloaded
            self.storage.record_downloaded_media_url(url, file_hash)

            if self.storage.has_hash_downloaded(file_hash):
                return None

            final_path = self.cfg.download_dir / f"{file_hash}{ext or '.bin'}"

            if final_path.exists():
                self.storage.record_downloaded_hash(file_hash, final_path)
                return final_path

            temp_path.replace(final_path)
            self.storage.record_downloaded_hash(file_hash, final_path)

            size_display = max(1, bytes_written // 1024)
            logger.info(f"Downloaded: {final_path.name} ({size_display} KB)")
            return final_path

        except Exception as exc:
            logger.debug(f"Download failed for {url}: {exc}")
            return None

        finally:
            temp_path.unlink(missing_ok=True)

    async def download_many(
        self, client: httpx.AsyncClient, urls: Iterable[str]
    ) -> None:
        unique_urls: list[str] = []
        seen: set[str] = set()

        for url in urls:
            if url and url not in seen:
                seen.add(url)
                unique_urls.append(url)

        if not unique_urls:
            return

        if (
            self.cfg.max_downloads_per_page > 0
            and len(unique_urls) > self.cfg.max_downloads_per_page
        ):
            logger.warning(
                f"Limiting downloads from page to {self.cfg.max_downloads_per_page} "
                f"of {len(unique_urls)} candidates."
            )
            unique_urls = unique_urls[: self.cfg.max_downloads_per_page]

        sem = asyncio.Semaphore(max(1, self.cfg.max_connections))

        async def _download(url: str):
            async with sem:
                await self.download_file(client, url)

        results = await asyncio.gather(
            *(_download(u) for u in unique_urls),
            return_exceptions=True,
        )

        for url, result in zip(unique_urls, results, strict=True):
            if isinstance(result, Exception):
                logger.debug(f"Download task failed for {url}: {result}")

    # ------------------ Slug / URL helpers ------------------

    @staticmethod
    def ensure_scheme(url: str) -> str:
        url = url.strip()

        if not url:
            return ""

        if "://" not in url:
            return "https://" + url

        return url

    @staticmethod
    def normalize_telegraph_url(url: str) -> str | None:
        url = MediaScraper.ensure_scheme(url)

        if not url:
            return None

        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower().split(":")[0].removeprefix("www.")

        if netloc != "telegra.ph" and not netloc.endswith(".telegra.ph"):
            return None

        path = parsed.path.rstrip("/")

        if not path or path == "/":
            return None

        slug = path.lstrip("/").lower()
        if slug in TELEGRAPH_RESERVED or slug.startswith("file/"):
            return None

        if Path(path).suffix.lower() in MEDIA_EXTENSIONS:
            return None

        if not re.fullmatch(r"/[A-Za-z0-9_\-]+", path):
            return None

        return f"https://telegra.ph{path}"

    @staticmethod
    def normalize_telegram_url(url: str) -> str | None:
        url = MediaScraper.ensure_scheme(url)

        if not url:
            return None

        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower().split(":")[0].removeprefix("www.")

        if netloc not in TELEGRAM_DOMAINS:
            return None

        path_clean = parsed.path.strip("/")
        if not path_clean:
            return None

        # Check for private channel/group invite patterns
        if path_clean.startswith(("+", "joinchat/", "c/")):
            logger.warning(
                f"Skipping private Telegram URL '{url}'. "
                "Private channels/groups cannot be viewed publicly and require an authorized Telegram account."
            )
            return None

        parts = [p for p in path_clean.split("/") if p]

        if not parts:
            return None

        if parts[0].lower() == "s":
            parts = parts[1:]

        if not parts:
            return None

        username = parts[0].split("?")[0].strip().lstrip("@").lower()

        if username.lower() in TELEGRAM_RESERVED:
            return None

        if not TELEGRAM_USERNAME_RE.match(username):
            return None

        msg_id: str | None = None
        if len(parts) > 1 and parts[1].isdigit():
            msg_id = parts[1]

        if msg_id:
            base = f"https://t.me/s/{username}/{msg_id}"
        else:
            base = f"https://t.me/s/{username}"

        if parsed.query:
            qs = urllib.parse.parse_qs(parsed.query)
            q_pairs = []
            for param in ("before", "after"):
                val = qs.get(param, [None])[0]
                if val and val.isdigit() and int(val) > 0:
                    q_pairs.append(f"{param}={val}")
            if q_pairs:
                return f"{base}?{'&'.join(q_pairs)}"

        return base

    @staticmethod
    def extract_base_slug(url: str) -> str | None:
        if not url:
            return None

        url = url.strip()

        if "telegra.ph" in url:
            normalized = MediaScraper.normalize_telegraph_url(url)
            if not normalized:
                return None
            path = urllib.parse.urlparse(normalized).path.strip("/")
        elif "://" in url or "/" in url:
            try:
                parsed = urllib.parse.urlparse(url)
                path = parsed.path.strip("/")
                if not path:
                    return None
            except Exception:
                path = url.strip("/")
        else:
            path = url.strip("/")

        if not path:
            return None

        m = TELEGRAPH_SLUG_DATE_RE.match(path)
        if m:
            return m.group("base")

        return path

    @staticmethod
    def slugify_telegraph_candidates(text: str) -> list[str]:
        if not text:
            return []
        if "://" in text or "/" in text:
            try:
                parsed = urllib.parse.urlparse(text)
                text = parsed.path.rstrip("/").split("/")[-1]
            except Exception:
                pass
        text = text.split("?")[0].strip().lstrip("@")
        candidates: list[str] = []

        # 1. Hyphenated slug (converting underscores and spaces to hyphens)
        ph_hyphen = re.sub(
            r"-+",
            "-",
            re.sub(r"[^A-Za-z0-9_\-]+", "-", text).replace("_", "-").strip("-"),
        )
        if ph_hyphen and len(ph_hyphen) >= 3 and ph_hyphen not in candidates and ph_hyphen.lower() not in TELEGRAPH_RESERVED:
            candidates.append(ph_hyphen)

        # 2. Preserving underscores if present
        ph_underscore = re.sub(
            r"_+", "_", re.sub(r"[^A-Za-z0-9_\-]+", "_", text).strip("-_")
        )
        if (
            ph_underscore
            and len(ph_underscore) >= 3
            and ph_underscore not in candidates
            and ph_underscore.lower() not in TELEGRAPH_RESERVED
        ):
            candidates.append(ph_underscore)

        # 3. Compact without separators
        ph_compact = re.sub(r"[^A-Za-z0-9]+", "", text)
        if ph_compact and len(ph_compact) >= 3 and ph_compact not in candidates and ph_compact.lower() not in TELEGRAPH_RESERVED:
            candidates.append(ph_compact)

        return candidates

    @staticmethod
    def slugify_telegram_candidates(text: str) -> list[str]:
        if not text:
            return []
        if any(dom in text for dom in TELEGRAM_DOMAINS) or "://" in text:
            norm = MediaScraper.normalize_telegram_url(text)
            if norm:
                m = TELEGRAM_URL_RE.match(norm)
                if m:
                    text = m.group(1)
            else:
                try:
                    parsed = urllib.parse.urlparse(text)
                    parts = [p for p in parsed.path.strip("/").split("/") if p and p != "s"]
                    if parts:
                        text = parts[0]
                except Exception:
                    pass
        elif "/" in text:
            parts = [p for p in text.strip("/").split("/") if p and p != "s"]
            if parts:
                text = parts[0]

        text = text.split("?")[0].strip().lstrip("@")
        candidates: list[str] = []

        # 1. Exact match if valid
        if TELEGRAM_USERNAME_RE.match(text):
            candidates.append(text)

        # 2. Replace hyphens and separators with underscores
        tg_underscore = re.sub(
            r"_+", "_", re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")
        )
        if (
            TELEGRAM_USERNAME_RE.match(tg_underscore)
            and tg_underscore not in candidates
        ):
            candidates.append(tg_underscore)

        # 3. Compact alphanumeric without separators
        tg_compact = re.sub(r"[^A-Za-z0-9]+", "", text)
        if TELEGRAM_USERNAME_RE.match(tg_compact) and tg_compact not in candidates:
            candidates.append(tg_compact)

        return [
            c for c in candidates
            if c.lower() not in TELEGRAM_RESERVED and c.lower() not in TELEGRAPH_RESERVED
        ]

    @staticmethod
    def slugify_telegraph(text: str) -> str | None:
        candidates = MediaScraper.slugify_telegraph_candidates(text)
        return candidates[0] if candidates else None

    @staticmethod
    def slugify_telegram(text: str) -> str | None:
        candidates = MediaScraper.slugify_telegram_candidates(text)
        return candidates[0] if candidates else None

    # ------------------ Queue helpers ------------------

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

    async def add_seed(
        self, client: httpx.AsyncClient | None = None, seed: str = "", depth: int = 0
    ) -> None:
        seed = seed.strip()

        if not seed or seed.startswith("#"):
            return

        # Direct Telegraph URL
        if "telegra.ph" in seed:
            url = self.normalize_telegraph_url(seed)

            if url:
                self.enqueue_url(url, "telegraph", depth)
                base = self.extract_base_slug(url)

                if base:
                    # Cross-pollinate to Telegram
                    for tg_cand in self.slugify_telegram_candidates(base):
                        self.enqueue_url(f"https://t.me/s/{tg_cand}", "telegram", depth)

            return

        # Direct Telegram URL
        if any(dom in seed for dom in TELEGRAM_DOMAINS):
            url = self.normalize_telegram_url(seed)

            if url:
                self.enqueue_url(url, "telegram", depth)
                m = TELEGRAM_URL_RE.match(url)

                if m:
                    username = m.group(1)

                    # Cross-pollinate to Telegraph
                    for ph_cand in self.slugify_telegraph_candidates(username):
                        self.enqueue_url(
                            f"https://telegra.ph/{ph_cand}", "telegraph", depth
                        )

            return

        # Raw username or phrase: Cross-pollinate both Telegram and Telegraph candidates
        raw = seed.lstrip("@").strip()

        if not raw:
            return

        # 1. Telegram candidates
        for tg_slug in self.slugify_telegram_candidates(raw):
            self.enqueue_url(f"https://t.me/s/{tg_slug}", "telegram", depth)

        # 2. Telegraph candidates
        for ph_slug in self.slugify_telegraph_candidates(raw):
            self.enqueue_url(f"https://telegra.ph/{ph_slug}", "telegraph", depth)

    # ------------------ Discovery ------------------

    @staticmethod
    def _unwrap_ddg(href: str) -> str:
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://duckduckgo.com" + href

        parsed = urllib.parse.urlparse(href)

        if parsed.netloc.endswith("duckduckgo.com") and "uddg=" in parsed.query:
            qs = urllib.parse.parse_qs(parsed.query)
            return qs.get("uddg", [href])[0]

        return href

    async def discover_urls_duckduckgo(
        self,
        client: httpx.AsyncClient,
        keyword: str,
    ) -> tuple[set[str], set[str]]:
        telegraph_urls: set[str] = set()
        telegram_urls: set[str] = set()

        queries = [
            f"site:telegra.ph {keyword}",
            f"site:t.me/s/ {keyword}",
        ]

        for query in queries:
            try:
                logger.info(f"DuckDuckGo search: {query}")

                resp = await self._fetch(
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
                    target = self._unwrap_ddg(href_val)

                    tg = self.normalize_telegraph_url(target)

                    if tg:
                        telegraph_urls.add(tg)
                        continue

                    tel = self.normalize_telegram_url(target)

                    if tel:
                        telegram_urls.add(tel)

            except Exception as exc:
                logger.warning(f"DuckDuckGo discovery failed for '{query}': {exc}")

            await asyncio.sleep(self.cfg.delay())

        return telegraph_urls, telegram_urls

    async def discover_urls_yandex(
        self,
        client: httpx.AsyncClient,
        keyword: str,
    ) -> tuple[set[str], set[str]]:
        telegraph_urls: set[str] = set()
        telegram_urls: set[str] = set()

        queries = [
            f"site:telegra.ph {keyword}",
            f"site:t.me/s/ {keyword}",
        ]

        for query in queries:
            url = f"https://yandex.com/search/?text={urllib.parse.quote_plus(query)}"

            try:
                logger.info(f"Yandex search: {query}")

                resp = await self._fetch(client, url)

                if resp is None or resp.status_code != 200:
                    continue

                text = resp.text.lower()

                if any(
                    marker in text for marker in ("smartcaptcha", "captcha", "robot")
                ):
                    logger.warning("Yandex captcha/bot check triggered. Skipping.")
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")

                for a in soup.find_all("a", href=True):
                    href_val = a.get("href")
                    if not isinstance(href_val, str) or not href_val:
                        continue
                    target = href_val

                    tg = self.normalize_telegraph_url(target)

                    if tg:
                        telegraph_urls.add(tg)
                        continue

                    tel = self.normalize_telegram_url(target)

                    if tel:
                        telegram_urls.add(tel)

            except Exception as exc:
                logger.warning(f"Yandex discovery failed for '{query}': {exc}")

            await asyncio.sleep(self.cfg.delay())

        return telegraph_urls, telegram_urls

    async def run_discovery(self, keywords: list[str]) -> tuple[set[str], set[str]]:
        discovered_telegraph: set[str] = set()
        discovered_telegram: set[str] = set()

        engines = set(self.cfg.search_engines)

        async with self.make_client() as client:
            for keyword in keywords:
                if "duckduckgo" in engines:
                    tg, tel = await self.discover_urls_duckduckgo(client, keyword)
                    discovered_telegraph.update(tg)
                    discovered_telegram.update(tel)

                if "yandex" in engines:
                    tg, tel = await self.discover_urls_yandex(client, keyword)
                    discovered_telegraph.update(tg)
                    discovered_telegram.update(tel)

        for url in discovered_telegraph:
            base = self.extract_base_slug(url)

            if base:
                self.storage.add_discovered_seed(base)

        for url in discovered_telegram:
            m = TELEGRAM_URL_RE.match(url)

            if m:
                self.storage.add_discovered_seed(m.group(1))
            else:
                self.storage.add_discovered_seed(url)

        return discovered_telegraph, discovered_telegram

    # ------------------ Telegraph crawling ------------------

    async def scrape_telegraph_page(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> tuple[list[str], set[str], set[str]]:
        media_urls: list[str] = []
        telegram_urls: set[str] = set()
        telegraph_urls: set[str] = set()

        try:
            resp = await self._fetch(client, url)

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
                ext = self._extension_from_url(href)
                if ext in MEDIA_EXTENSIONS or "/file/" in href:
                    media_urls.append(href)
                    continue

                tg = self.normalize_telegraph_url(href)
                if tg and tg != url:
                    telegraph_urls.add(tg)
                    continue

                tel = self.normalize_telegram_url(href)
                if tel:
                    telegram_urls.add(tel)

            # 3. Telegram mentions and URLs in article text
            article_text = soup.get_text()
            for mention in TELEGRAM_MENTION_RE.findall(article_text):
                if mention.lower() not in TELEGRAM_RESERVED:
                    tel = self.normalize_telegram_url(f"https://t.me/{mention}")
                    if tel:
                        telegram_urls.add(tel)

            for tg_match in TELEGRAM_URL_RE.finditer(article_text):
                tel = self.normalize_telegram_url(tg_match.group(0))
                if tel:
                    telegram_urls.add(tel)

            for ph_match in TELEGRAPH_URL_RE.finditer(article_text):
                tg = self.normalize_telegraph_url(ph_match.group(0))
                if tg and tg != url:
                    telegraph_urls.add(tg)

            # 4. Author handle in <address>
            for addr in soup.find_all("address"):
                for a in addr.find_all("a", href=True):
                    a_href = a.get("href")
                    if isinstance(a_href, str) and a_href:
                        author_href = urllib.parse.urljoin(url, a_href)
                        tel = self.normalize_telegram_url(author_href)
                        if tel:
                            telegram_urls.add(tel)
                addr_text = addr.get_text()
                for mention in TELEGRAM_MENTION_RE.findall(addr_text):
                    if mention.lower() not in TELEGRAM_RESERVED:
                        tel = self.normalize_telegram_url(f"https://t.me/{mention}")
                        if tel:
                            telegram_urls.add(tel)

        except Exception as exc:
            logger.error(f"Error scraping Telegraph page {url}: {exc}")

        return media_urls, telegram_urls, telegraph_urls

    async def probe_telegraph_suffixes(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> list[str]:
        normalized = self.normalize_telegraph_url(url)
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

        while True:
            if (
                self.cfg.max_suffix_probes > 0
                and attempts >= self.cfg.max_suffix_probes
            ):
                break

            probe_url = f"{base}-{2 + attempts}"

            if await self._probe_url_exists(client, probe_url):
                found.append(probe_url)
                attempts += 1
                await asyncio.sleep(random.uniform(0.1, 0.4))
            else:
                break

        return found

    async def probe_slug_all_dates(
        self,
        client: httpx.AsyncClient,
        base_slug: str,
    ) -> list[str]:
        if not self.cfg.probe_telegraph_dates:
            return []

        if self.storage.has_base_slug_probed(base_slug):
            return []

        self.storage.mark_base_slug_probed(base_slug)

        logger.info(f"Probing all 366 date suffixes for base slug: {base_slug}")

        days_per_month = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        probe_urls = [
            f"https://telegra.ph/{base_slug}-{month:02d}-{day:02d}"
            for month, days in enumerate(days_per_month, start=1)
            for day in range(1, days + 1)
        ]

        concurrency = max(1, min(self.cfg.probe_date_concurrency, self.cfg.max_connections))
        sem = asyncio.Semaphore(concurrency)
        found: list[str] = []

        async def probe_one(probe_url: str) -> None:
            async with sem:
                if await self._probe_url_exists(client, probe_url):
                    found.append(probe_url)

        await asyncio.gather(*(probe_one(u) for u in probe_urls))
        found.sort()

        return found

    async def process_telegraph_page(
        self,
        client: httpx.AsyncClient,
        url: str,
        depth: int,
    ) -> None:
        logger.info(f"Scraping Telegraph: {url}")

        media_urls, telegram_urls, telegraph_urls = await self.scrape_telegraph_page(
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

            base = self.extract_base_slug(tg_url)

            if base:
                self.storage.add_discovered_seed(base)

        if media_urls:
            logger.info(f"Found {len(media_urls)} media candidates on Telegraph page.")
            await self.download_many(client, media_urls)

        suffix_urls = await self.probe_telegraph_suffixes(client, url)

        for suffix_url in suffix_urls:
            self.enqueue_url(suffix_url, "telegraph", depth + 1)

        base = self.extract_base_slug(url)

        if base:
            # Cross-pollinate Telegram candidates from Telegraph slug
            for tg_cand in self.slugify_telegram_candidates(base):
                self.enqueue_url(f"https://t.me/s/{tg_cand}", "telegram", depth + 1)
                self.storage.add_discovered_seed(tg_cand)

            if self.cfg.probe_telegraph_dates:
                date_urls = await self.probe_slug_all_dates(client, base)

                for date_url in date_urls:
                    self.enqueue_url(date_url, "telegraph", depth + 1)

    # ------------------ Telegram web preview crawling ------------------

    async def scrape_telegram_web_preview(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> tuple[list[str], set[str], set[str], str | None]:
        media_urls: list[str] = []
        telegraph_urls: set[str] = set()
        discovered_telegram: set[str] = set()
        before_url: str | None = None

        m_curr = TELEGRAM_URL_RE.match(url)
        current_channel = m_curr.group(1).lower() if m_curr else None

        try:
            resp = await self._fetch(client, url)

            if resp is None or resp.status_code != 200:
                logger.warning(f"Failed to fetch Telegram preview: {url}")
                return media_urls, telegraph_urls, discovered_telegram, before_url

            resp_url_str = str(getattr(resp, "url", url))
            parsed_resp = urllib.parse.urlparse(resp_url_str)
            resp_netloc = parsed_resp.netloc.lower().split(":")[0].removeprefix("www.")
            if resp_netloc not in TELEGRAM_DOMAINS or not parsed_resp.path.startswith("/s/"):
                logger.warning(
                    f"Telegram preview for '{url}' redirected to '{resp_url_str}' "
                    "(channel does not exist or preview unavailable). Skipping."
                )
                return media_urls, telegraph_urls, discovered_telegram, before_url

            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()

            messages = soup.select(".tgme_widget_message")

            # 1. Photos and video thumbnails in message wraps and albums
            for wrap in soup.find_all(
                class_=re.compile(
                    r"tgme_widget_message_photo_wrap|tgme_widget_message_video_thumb|tgme_widget_message_roundvideo_thumb"
                )
            ):
                style = wrap.get("style", "")
                if isinstance(style, str) and style:
                    m = re.search(r"url\((['\"]?)(?P<url>[^'\")]+)\1\)", style)
                    if m:
                        media_urls.append(urllib.parse.urljoin(url, m.group("url")))

            # 2. Photos in link previews
            for preview in soup.select(
                ".link_preview_image, .link_preview_right_image, .tgme_widget_message_link_preview"
            ):
                style = preview.get("style", "")
                if isinstance(style, str) and style:
                    m = re.search(r"url\((['\"]?)(?P<url>[^'\")]+)\1\)", style)
                    if m:
                        media_urls.append(urllib.parse.urljoin(url, m.group("url")))

            # 3. Videos, round videos, and video sources
            for tag in soup.find_all(["video", "source"]):
                src = tag.get("src")
                if isinstance(src, str) and src:
                    media_urls.append(urllib.parse.urljoin(url, src))
                poster = tag.get("poster")
                if isinstance(poster, str) and poster and not src:
                    media_urls.append(urllib.parse.urljoin(url, poster))

            # 4. Audios and voice clips
            for tag in soup.find_all(["audio"]):
                src = tag.get("src")
                if isinstance(src, str) and src:
                    media_urls.append(urllib.parse.urljoin(url, src))

            # 5. Documents / downloadable attachments
            for doc_wrap in soup.select("a.tgme_widget_message_document_wrap"):
                doc_href = doc_wrap.get("href")
                if isinstance(doc_href, str) and doc_href:
                    media_urls.append(urllib.parse.urljoin(url, doc_href))

            # 6. Direct img tags (skipping avatar/profile icons)
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src")
                if not isinstance(src, str) or not src:
                    continue

                parent_cls = img.parent.get("class") if img.parent else None
                parent_classes = " ".join(parent_cls) if isinstance(parent_cls, list) else (parent_cls or "")
                img_cls = img.get("class")
                classes = " ".join(img_cls) if isinstance(img_cls, list) else (img_cls or "")
                if any(
                    avatar_cls in parent_classes or avatar_cls in classes
                    for avatar_cls in (
                        "tgme_widget_message_user_photo",
                        "tgme_page_photo_image",
                        "tgme_page_icon",
                    )
                ):
                    continue

                if (
                    "tgme_widget_message_photo" in classes
                    or "tgme_widget_message_sticker" in classes
                ):
                    media_urls.append(urllib.parse.urljoin(url, src))

            # 7. Telegraph & Telegram links strictly inside message contents (excluding pagination, headers, self-links)
            for a in soup.select(
                ".tgme_widget_message_text a, .tgme_widget_message_inline_button, "
                ".tgme_widget_message_forward_info a, .tgme_widget_message_reply a"
            ):
                href_val = a.get("href")
                if not isinstance(href_val, str) or not href_val:
                    continue

                href = urllib.parse.urljoin(url, href_val)
                ext = self._extension_from_url(href)
                if ext in MEDIA_EXTENSIONS or "/file/" in href:
                    media_urls.append(href)
                    continue

                tg = self.normalize_telegraph_url(href)
                if tg:
                    telegraph_urls.add(tg)
                    continue

                tel = self.normalize_telegram_url(href)
                if tel:
                    m_tel = TELEGRAM_URL_RE.match(tel)
                    cand_channel = m_tel.group(1).lower() if m_tel else None
                    if (
                        cand_channel
                        and cand_channel != current_channel
                        and cand_channel not in TELEGRAM_RESERVED
                    ):
                        discovered_telegram.add(f"https://t.me/s/{cand_channel}")

            # 8. Text mentions (@username) and Telegraph links in message bodies
            for msg_text_elem in soup.select(".tgme_widget_message_text"):
                msg_text = msg_text_elem.get_text()
                for mention in TELEGRAM_MENTION_RE.findall(msg_text):
                    m_lower = mention.lower()
                    if (
                        m_lower != current_channel
                        and m_lower not in TELEGRAM_RESERVED
                    ):
                        m_norm = self.normalize_telegram_url(f"https://t.me/{mention}")
                        if m_norm:
                            discovered_telegram.add(m_norm)

                for ph_match in TELEGRAPH_URL_RE.finditer(msg_text):
                    ph_norm = self.normalize_telegraph_url(ph_match.group(0))
                    if ph_norm:
                        telegraph_urls.add(ph_norm)

            # 9. Cross-pollinate Telegraph candidate slugs from this Telegram channel username (only once per channel root)
            if m_curr and "?before=" not in url:
                ch_name = m_curr.group(1)
                for ph_cand in self.slugify_telegraph_candidates(ch_name):
                    ph_norm = self.normalize_telegraph_url(
                        f"https://telegra.ph/{ph_cand}"
                    )
                    if ph_norm:
                        telegraph_urls.add(ph_norm)

            # 10. Pagination for older messages
            next_link = soup.find("a", class_=re.compile(r"(?:tgme|tme)_messages_more"))
            if next_link is None:
                next_link = soup.find("a", href=re.compile(r"[?&]before="))

            if isinstance(next_link, Tag):
                href = next_link.get("href")
                if isinstance(href, str) and href:
                    before_url = urllib.parse.urljoin(url, href)
            elif messages and current_channel:
                msg_ids = []
                for msg in messages:
                    post_attr = msg.get("data-post", "")
                    if isinstance(post_attr, str) and "/" in post_attr:
                        ch, mid = post_attr.split("/", 1)
                        if ch.lower() == current_channel and mid.isdigit():
                            msg_ids.append(int(mid))
                if msg_ids and min(msg_ids) > 1:
                    before_url = f"https://t.me/s/{current_channel}?before={min(msg_ids)}"

            # Ensure pagination URL is never treated as a newly discovered external channel
            if before_url:
                norm_before = self.normalize_telegram_url(before_url)
                if norm_before:
                    discovered_telegram.discard(norm_before)
                    m_b = TELEGRAM_URL_RE.match(norm_before)
                    if m_b:
                        discovered_telegram.discard(f"https://t.me/s/{m_b.group(1).lower()}")

        except Exception as exc:
            logger.error(f"Error scraping Telegram preview {url}: {exc}")

        return media_urls, telegraph_urls, discovered_telegram, before_url

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
        ) = await self.scrape_telegram_web_preview(
            client,
            url,
        )

        for tg_url in telegraph_urls:
            self.enqueue_url(tg_url, "telegraph", depth + 1)

            base = self.extract_base_slug(tg_url)

            if base:
                self.storage.add_discovered_seed(base)

        for tel_url in discovered_telegram:
            self.enqueue_url(tel_url, "telegram", depth + 1)

            m = TELEGRAM_URL_RE.match(tel_url)

            if m:
                self.storage.add_discovered_seed(m.group(1))

        # Cross-pollinate dates only on initial channel page, not every paginated page
        m_self = TELEGRAM_URL_RE.match(url)
        if m_self and self.cfg.probe_telegraph_dates and "?before=" not in url:
            ch_name = m_self.group(1)
            for ph_cand in self.slugify_telegraph_candidates(ch_name):
                date_urls = await self.probe_slug_all_dates(client, ph_cand)
                for date_url in date_urls:
                    self.enqueue_url(date_url, "telegraph", depth + 1)

        if media_urls:
            logger.info(
                f"Found {len(media_urls)} media candidates in Telegram preview."
            )
            await self.download_many(client, media_urls)

        if before_url:
            normalized_before = self.normalize_telegram_url(before_url)

            if normalized_before and normalized_before != url:
                # Keep same depth for channel pagination so max-depth limits inter-channel hops
                self.enqueue_url(normalized_before, "telegram", depth)

    # ------------------ Telegram API scraping ------------------

    def _extract_chat_username(self, target: str) -> str | None:
        target = target.strip()

        if not target:
            return None

        if any(dom in target for dom in TELEGRAM_DOMAINS) or "://" in target:
            normalized = self.normalize_telegram_url(target)

            if normalized:
                m = TELEGRAM_URL_RE.match(normalized)

                if m:
                    cand = m.group(1)
                    if cand.lower() not in TELEGRAM_RESERVED:
                        return cand

        raw = target.lstrip("@").split("?")[0].strip()
        if "/" in raw:
            parts = [p for p in raw.split("/") if p and p != "s"]
            if parts:
                raw = parts[0]

        if (
            TELEGRAM_USERNAME_RE.match(raw)
            and raw.lower() not in TELEGRAM_RESERVED
            and raw.lower() not in TELEGRAPH_RESERVED
        ):
            return raw

        return self.slugify_telegram(target)

    def _message_has_media(self, message) -> bool:
        try:
            if getattr(message, "photo", None):
                return True

            if getattr(message, "video", None):
                return True

            doc = getattr(message, "document", None)

            if doc:
                mime = (getattr(doc, "mime_type", "") or "").lower()
                if mime.startswith(("image/", "video/", "audio/")):
                    return True

                # Check uncompressed files sent as documents
                for attr in getattr(doc, "attributes", []):
                    fn = getattr(attr, "file_name", "")
                    if fn and Path(fn).suffix.lower() in MEDIA_EXTENSIONS:
                        return True
        except Exception:
            return False

        return False

    async def download_telethon_media(self, client, message) -> Path | None:
        doc = getattr(message, "document", None)

        if doc and getattr(doc, "size", 0) and int(doc.size) < self.cfg.min_size_bytes:
            return None

        temp_path = self.cfg.download_dir / f"temp_api_{uuid.uuid4().hex}"
        downloaded: str | None = None

        try:
            for attempt in range(2):
                try:
                    downloaded = await client.download_media(message, file=str(temp_path))
                    break
                except Exception as exc:
                    seconds = getattr(exc, "seconds", None)
                    if seconds is not None and attempt == 0:
                        if seconds > 120:
                            logger.warning(
                                f"Telegram FloodWait too long ({seconds}s). Skipping media."
                            )
                            return None
                        logger.warning(
                            f"Telegram FloodWait encountered: waiting {seconds} seconds."
                        )
                        await asyncio.sleep(seconds + 1)
                        continue
                    logger.error(f"Error downloading Telethon media: {exc}")
                    return None

            if not downloaded:
                return None

            downloaded_path = Path(downloaded)

            if not downloaded_path.exists():
                return None

            actual_size = downloaded_path.stat().st_size

            if actual_size < self.cfg.min_size_bytes:
                downloaded_path.unlink(missing_ok=True)
                return None

            def _calc_sha256_and_header(p: Path) -> tuple[str, bytes]:
                s = hashlib.sha256()
                header = b""
                with p.open("rb") as fh:
                    while chunk := fh.read(65536):
                        if not header:
                            header = chunk[:64]
                        s.update(chunk)
                return s.hexdigest(), header

            file_hash, header_bytes = await asyncio.to_thread(
                _calc_sha256_and_header, downloaded_path
            )

            if self.storage.has_hash_downloaded(file_hash):
                downloaded_path.unlink(missing_ok=True)
                return None

            ext = downloaded_path.suffix.lower()

            if not ext:
                if getattr(message, "photo", None):
                    ext = ".jpg"
                else:
                    mime = getattr(doc, "mime_type", "") or ""
                    ext = self._extension_from_content_type(mime) or ""

            if (not ext or ext == ".bin") and header_bytes:
                detected = detect_media_type_from_bytes(header_bytes)
                if detected:
                    ext = detected[1]

            if not ext:
                ext = ".bin"

            final_path = self.cfg.download_dir / f"{file_hash}{ext}"

            if final_path.exists():
                downloaded_path.unlink(missing_ok=True)
                self.storage.record_downloaded_hash(file_hash, final_path)
                return final_path

            downloaded_path.replace(final_path)
            self.storage.record_downloaded_hash(file_hash, final_path)

            size_display = max(1, actual_size // 1024)
            logger.info(
                f"Downloaded via API: {final_path.name} ({size_display} KB)"
            )
            return final_path

        except Exception as exc:
            logger.error(f"Error processing Telethon media: {exc}")
            return None

        finally:
            temp_path.unlink(missing_ok=True)
            if downloaded:
                Path(downloaded).unlink(missing_ok=True)

    async def run_api_scraper(self, target_chats: list[str]) -> bool:
        """
        Run Telegram API scraping via Telethon if configured.
        Returns True if successful, or False if skipped/failed (triggering fallback).
        """
        api_cfg = self.cfg.telegram_api or {}

        api_id = api_cfg.get("api_id")
        api_hash = api_cfg.get("api_hash")
        phone = api_cfg.get("phone")
        session_name = api_cfg.get("session_name", "telegram_media_scraper")

        if not api_id or not api_hash:
            logger.info(
                "Telegram API credentials not configured. Using public web scraping."
            )
            return False

        try:
            api_id = int(api_id)
        except Exception:
            logger.warning(
                "telegram_api.api_id must be an integer. Falling back to public web scraping."
            )
            return False

        try:
            from telethon import TelegramClient
        except ImportError:
            logger.warning(
                "Telethon package is not installed. Falling back to public web scraping."
            )
            return False

        client: TelegramClient | None = None

        try:
            client = TelegramClient(session_name, api_id, str(api_hash))
            api_client: typing.Any = client
            await api_client.start(
                phone=phone
                if phone
                else lambda: input("Please enter your phone number: "),
                code_callback=lambda: input("Please enter the Telegram login code: "),
                password=lambda: getpass.getpass(
                    "Please enter your Telegram 2FA password: "
                ),
            )

            assert client is not None
            if not await client.is_user_authorized():
                logger.warning(
                    "Telegram API client is not authorized. Falling back to public web scraping."
                )
                return False

            logger.info("Telegram API authorized successfully.")

            discovered_telegraphs: set[str] = set()

            for target in target_chats:
                username = self._extract_chat_username(target)

                if not username:
                    logger.warning(f"Could not parse Telegram target: {target}")
                    continue

                logger.info(f"Scraping channel via API: {username}")
                last_scraped_msg_id = self.storage.get_channel_last_message_id(username)
                max_msg_id = last_scraped_msg_id
                msg_count = 0

                try:
                    entity = await api_client.get_entity(username)
                    if isinstance(entity, list):
                        entity = entity[0]

                    logger.info(f"Starting from message ID > {last_scraped_msg_id}")

                    async for message in client.iter_messages(
                        entity,
                        min_id=last_scraped_msg_id,
                        reverse=True,
                    ):
                        max_msg_id = max(max_msg_id, message.id)
                        msg_count += 1

                        if self._message_has_media(message):
                            await self.download_telethon_media(client, message)

                        text = getattr(message, "message", "") or ""

                        # Discover Telegraph links
                        for match in TELEGRAPH_URL_RE.finditer(text):
                            normalized = self.normalize_telegraph_url(match.group(0))
                            if not normalized:
                                continue

                            discovered_telegraphs.add(normalized)
                            base = self.extract_base_slug(normalized)
                            if base:
                                self.storage.add_discovered_seed(base)
                            self.enqueue_url(normalized, "telegraph", 1)

                        # Discover Telegram channel mentions & URLs
                        for mention in TELEGRAM_MENTION_RE.findall(text):
                            m_lower = mention.lower()
                            if m_lower != username.lower() and m_lower not in TELEGRAM_RESERVED:
                                self.storage.add_discovered_seed(mention)
                                self.enqueue_url(f"https://t.me/s/{mention}", "telegram", 1)

                        for tg_match in TELEGRAM_URL_RE.finditer(text):
                            cand_url = self.normalize_telegram_url(tg_match.group(0))
                            if cand_url:
                                m_cand = TELEGRAM_URL_RE.match(cand_url)
                                if (
                                    m_cand
                                    and m_cand.group(1).lower() != username.lower()
                                    and m_cand.group(1).lower() not in TELEGRAM_RESERVED
                                ):
                                    self.storage.add_discovered_seed(m_cand.group(1))
                                    self.enqueue_url(f"https://t.me/s/{m_cand.group(1)}", "telegram", 1)

                        if msg_count % 50 == 0:
                            self.storage.update_channel_progress(username, max_msg_id)

                    logger.info(
                        f"Finished API scrape for {username}. "
                        f"Saved progress to message ID {max_msg_id}."
                    )
                    self.storage.mark_url_crawled(f"https://t.me/s/{username.lower()}")

                except Exception as exc:
                    logger.error(f"Error scraping channel {username} via API: {exc}")

                finally:
                    if max_msg_id > last_scraped_msg_id:
                        self.storage.update_channel_progress(username, max_msg_id)

            if discovered_telegraphs:
                logger.info(
                    f"Discovered {len(discovered_telegraphs)} Telegraph links via API. "
                    "They have been saved and queued for the web crawler."
                )

            return True

        except Exception as exc:
            logger.warning(
                f"Telegram API scraping failed ({exc}). Falling back to public web scraping."
            )
            return False

        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    await typing.cast(typing.Any, client).disconnect()

    # ------------------ Main crawler loop ------------------

    async def run_web_crawler(self, seeds: list[str]) -> None:
        async with self.make_client() as client:
            for seed in seeds:
                await self.add_seed(client, seed, depth=0)

            while True:
                depth = self.storage.get_min_pending_depth()

                if depth is None:
                    break

                if depth > self.cfg.max_depth:
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

                    except Exception:
                        logger.exception(f"Failed processing {url_type} URL {url}")

                        # This prevents a bad URL from poisoning the queue forever.
                        self.storage.mark_url_crawled(url)
                        self.storage.remove_from_queue(url)
                        await asyncio.sleep(self.cfg.delay())

                    else:
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
            api_targets = [s for s in seeds if "telegra.ph" not in s.lower()]

            if api_targets:
                logger.info(
                    "Telegram API credentials detected. Attempting API scraper..."
                )
                api_success = await self.run_api_scraper(api_targets)
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


def load_seed_files(paths: Iterable[Path]) -> list[str]:
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


async def async_main() -> None:
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
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )

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

    if args.probe_dates:
        cfg.probe_telegraph_dates = True
        logger.info("Enabled aggressive Telegraph date probing.")

    if args.include_discovered:
        cfg.include_discovered = True

    async with MediaScraper(cfg) as scraper:
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

        # Deduplicate while preserving order
        unique_seeds: list[str] = []
        seen: set[str] = set()

        for seed in seeds:
            if seed not in seen:
                seen.add(seed)
                unique_seeds.append(seed)

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
            logger.info("No new seeds provided, but pending crawl queue found. Resuming...")

        if args.force_crawl and unique_seeds:
            logger.info("Force crawl enabled: resetting crawl status for provided seeds.")
            for seed_val in unique_seeds:
                scraper.storage.uncrawl_seed(seed_val)

        await scraper.run(unique_seeds, use_api=not args.no_api)

        total = scraper.storage.count_downloaded_hashes()

        logger.info("=" * 60)
        logger.info("Scraping run completed.")
        logger.info(f"Total unique media records in database: {total}")
        logger.info(f"Downloads directory: {scraper.cfg.download_dir.resolve()}")
        logger.info(f"Discovered seeds file: {scraper.cfg.discovered_seeds_file.resolve()}")
        logger.info("=" * 60)


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Scraper interrupted by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
